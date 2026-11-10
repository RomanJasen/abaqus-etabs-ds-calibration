"""
Common code of the damage-state calibration workflow (used by steps 1 and 3; no GUI).

    damage states   DS0-DS5, every value is the START of a state: DSk when the value passes threshold_k
                    (value > threshold; a negative threshold is passed when the value drops below it)
    folders         where the workflow keeps its inputs and outputs
    Abaqus          results databases <root>/<SET>/<JOB>/*.txt and {SET:COLUMN} expressions
    groups          load-case groups = name filters on the job names
    mechanisms      SH, AX+, AX-, REB: two sections, a component and a sense; their default thresholds
    criteria        drift <-> displacement of every damage state of a job (first crossing, linear interpolation)
    JSON            atomic writing, defaults files

Units: Abaqus mm, N (forces in the expressions divided by 1000 -> kN); thresholds and drifts in mm.
"""

import copy
import json
import os
import re
import time

import numpy as np

# =====================================================================================================
# FOLDERS
# =====================================================================================================

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIB_DIR)
ABAQUS_DB_DIR = os.path.join(ROOT, "0_INPUT", "Abaqus_databases")
STEP1_DIR = os.path.join(ROOT, "1_Abaqus_criteria")
CRITERIA_DIR = os.path.join(STEP1_DIR, "criteria_files")
DEFAULTS_DIR = os.path.join(STEP1_DIR, "defaults")
DEFAULTS_FILE = os.path.join(DEFAULTS_DIR, "default_mechanisms.json")
ETABS_TOOL_DIR = os.path.join(ROOT, "2_ETABS_pushover")
ETABS_RESULTS_DIR = os.path.join(ETABS_TOOL_DIR, "ETABS_results")
STEP3_DIR = os.path.join(ROOT, "3_ETABS_calibration")
ETABS_CRITERIA_DIR = os.path.join(STEP3_DIR, "etabs_criteria")


def rel_to_root(path):
    """Path stored in the JSON files: relative to the workflow folder when inside it (portable)."""
    if not path:
        return ""
    p = os.path.abspath(path)
    try:
        r = os.path.relpath(p, ROOT)
    except ValueError:                       # another drive
        return p.replace("\\", "/")
    return p.replace("\\", "/") if r.startswith("..") else r.replace("\\", "/")


def from_root(path):
    if not path:
        return ""
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(ROOT, path))


# =====================================================================================================
# DAMAGE STATES
# =====================================================================================================

DS = ("DS1", "DS2", "DS3", "DS4", "DS5")
STATES = ("DS0",) + DS
DS_COLORS = {"DS0": "#A6A6A6", "DS1": "#00B050", "DS2": "#00A6FF", "DS3": "#FFC000", "DS4": "#FF0000",
             "DS5": "#000000"}
DS_TEXT_COLORS = {"DS0": "#000000", "DS1": "#FFFFFF", "DS2": "#FFFFFF", "DS3": "#000000", "DS4": "#FFFFFF",
                  "DS5": "#FFFFFF"}
DS_MEANING = {"DS0": "no damage", "DS1": "slight", "DS2": "moderate", "DS3": "substantial", "DS4": "very heavy",
              "DS5": "destruction"}

# marker of each mechanism in the plots (colour = damage state)
MECH_MARKERS = {"AX+": "^", "AX-": "v", "SH": "s", "REB": "D"}
MECH_LINESTYLES = {"AX+": "-", "AX-": "--", "SH": ":", "REB": "-."}


def mech_marker(key):
    return MECH_MARKERS.get(key, "o")


# axial-load colours of Panel_plotter_GUI.py (by axial force in N)
LOAD_COLORS = [(34272, "#FF0000"), (68874, "#FF4D00"), (103485, "#FF9700"), (138087, "#FFE400"),
               (172699, "#C5FF00"), (207301, "#7CFF00"), (241913, "#08FF00"), (276515, "#00FF59"),
               (311126, "#00FFB6"), (360000, "#00DCFF"), (720000, "#0087FF"), (1080000, "#003EFF"),
               (1440000, "#0F00FF"), (1800000, "#7800FF"), (2160000, "#D500FF"), (2520000, "#FF00E4"),
               (2880000, "#900982")]


def load_color(axial_kN):
    n = max(float(axial_kN) * 1000.0, 1.0)
    return min(LOAD_COLORS, key=lambda c: abs(np.log(n / c[0])))[1]


def fmt_kN(axial_kN):
    return f"{axial_kN:.0f}" if axial_kN >= 10 else f"{axial_kN:.3g}"


# =====================================================================================================
# DEFAULTS: PUSHOVER CURVE, MECHANISMS, GROUPS
# =====================================================================================================

PUSHOVER_DEFAULTS = {
    "drift": "{V_PIER&RU&2&0:Ux} - {V_PIER&RU&1&0:Ux}",       # top of the specimen relative to the base
    "force": "{V_PIER&RU&1&0:Fx} / 1000",                     # lateral force at the base [kN]
    "drift_label": "Drift RU2 - RU1 [mm]",
    "force_label": "Force RU1 Fx [kN]",
    "H_mm": 2600.0,                                           # panel height, for drift in %
    "collapse_jump_mm": 5.0,                                  # collapse: a step > 5 mm and > 10x every earlier
    "collapse_jump_ratio": 10.0,                              # step ends the valid data (0 = off)
    # also tested for the collapse jump (with the drift and the mechanism's own value), so every mechanism of a
    # job is cut at the same frame: the loading point falls by metres when the panel collapses
    "collapse_probe": "{V_PIER&RU&2&0:Uz} - {V_PIER&RU&1&0:Uz}",
}

THRESHOLD_SOURCE = ("HANDOVER_element_damage_states_DS0-5.md, section 4: the values of the project's "
                    "acceptance_criteria.json (DS1 / DS3 / DS4 = MK4, AX- DS5 = calibration, DS2 = provisional "
                    "sqrt(DS1 x DS3))")


def _thr(*vals):
    return dict(zip(DS, vals))


def _loc(label, a, b, component):
    return {"label": label, "section_a": a, "section_b": b, "component": component}


RU1 = "V_PIER&RU&1&0"

DEFAULT_MECHANISMS = [
    {"key": "AX+", "name": "AX+ uplift / opening (heel)",
     "section_a": RU1, "section_b": "V_SPEC&DM&1&0", "component": "Uz", "sense": "+", "relative_to_start": False,
     "thresholds": _thr(0.5, 1.581, 5.0, 15.0, None), "provisional": ["DS2"],
     "locations": [_loc("SPEC DM1 - RU1 (heel, joint)", RU1, "V_SPEC&DM&1&0", "Uz")],
     "etabs": {"property_pattern": r"^AX|AXIAL", "components": ["U1"], "sense": "+", "side": "Max",
               "project_link_types": ["Axial_X_dir", "Axial_Y_dir"]}},
    {"key": "AX-", "name": "AX- crushing / compression (toe)",
     "section_a": RU1, "section_b": "V_SPEC&GPL&2&0", "component": "Uz", "sense": "-", "relative_to_start": False,
     "thresholds": _thr(1.0, 1.414, 2.0, 3.0, 4.6), "provisional": ["DS2"],
     "locations": [_loc("SPEC GPL2 - RU1 (toe, panel top)", RU1, "V_SPEC&GPL&2&0", "Uz"),
                   _loc("SPEC GM2 - RU1 (heel, panel top)", RU1, "V_SPEC&GM&2&0", "Uz"),
                   _loc("SPEC DPL2 - RU1 (toe, bottom slice)", RU1, "V_SPEC&DPL&2&0", "Uz")],
     "etabs": {"property_pattern": r"^AX|AXIAL", "components": ["U1"], "sense": "-", "side": "Min",
               "project_link_types": ["Axial_X_dir", "Axial_Y_dir"]}},
    {"key": "SH", "name": "SH sliding (panel bottom vs base)",
     "section_a": RU1, "section_b": "V_PIER&SREDNJI1&1&0", "component": "Ux", "sense": "abs",
     "relative_to_start": False,
     "thresholds": _thr(0.01, 0.0707, 0.5, 3.0, None), "provisional": ["DS2"],
     "locations": [_loc("SREDNJI1 - RU1 (panel bottom face)", RU1, "V_PIER&SREDNJI1&1&0", "Ux")],
     "etabs": {"property_pattern": r"^(?!.*KEY).*SHEAR", "components": ["U3"], "sense": "abs", "side": "Min=Max",
               "project_link_types": ["Shear_X_dir", "Shear_Y_dir"]}},
    {"key": "REB", "name": "REB vertical bars",
     "section_a": RU1, "section_b": "V_SPEC&DM&2&0", "component": "Uz", "sense": "abs", "relative_to_start": False,
     "thresholds": _thr(0.12, 0.537, 2.4, 12.0, None), "provisional": ["DS2"],
     "locations": [_loc("SPEC DM2 - RU1 (heel, bottom slice)", RU1, "V_SPEC&DM&2&0", "Uz"),
                   _loc("SPEC DPL2 - RU1 (toe, bottom slice)", RU1, "V_SPEC&DPL&2&0", "Uz")],
     "etabs": {"property_pattern": r"REB|ARM", "components": ["U1", "U3"], "sense": "abs", "side": "Min=Max",
               "project_link_types": ["Armatura_X", "Armatura_Y"]}},
]

DEFAULT_GROUPS = [
    {"name": "SHEAR KEY TIE", "filter": "SHEAR_KEY_S_TIE"},
    {"name": "SHEAR KEY FR05", "filter": "SHEAR_KEY_S_FR05"},
    {"name": "SHEAR NOKEY FR07", "filter": "SHEAR_NOKEY_S_FR07"},
    {"name": "BEND KEY TIE", "filter": "BEND_KEY_S_TIE"},
    {"name": "BEND KEY FR05", "filter": "BEND_KEY_S_FR05"},
    {"name": "BEND NOKEY FR07", "filter": "BEND_NOKEY_S_FR07"},
    {"name": "all SHEAR", "filter": "SHEAR_"},
    {"name": "all NOKEY", "filter": "NOKEY"},
    {"name": "all TIE", "filter": "TIE"},
]


# What the TOP chart of step 1 plots (only the plot: the damage-state drifts always use the pushover "drift").
# An axis = (section_1 - section_2).column x factor; section_2 empty = section_1 alone.
# "match": with the automatic choice on, a preset is used for a group whose jobs all contain this text.
TOP_CHART_PRESETS = [
    {"name": "SHEAR: drift vs base force Fx", "match": "SHEAR",
     "x": {"section_1": "V_PIER&RU&2&0", "section_2": "V_PIER&RU&1&0", "column": "Ux", "factor": 1.0,
           "label": "Drift RU2 - RU1 [mm]"},
     "y": {"section_1": "V_PIER&RU&1&0", "section_2": "", "column": "Fx", "factor": 0.001,
           "label": "Force RU1 Fx [kN]"}},
    {"name": "BEND: rotation fiy vs base moment My", "match": "BEND",
     "x": {"section_1": "V_PIER&RU&2&0", "section_2": "V_PIER&RU&1&0", "column": "fiy", "factor": 1000.0,
           "label": "Rotation RU2 - RU1 fiy [mrad]"},
     "y": {"section_1": "V_PIER&RU&1&0", "section_2": "", "column": "My", "factor": 1e-6,
           "label": "Moment RU1 My [kNm]"}},
    {"name": "BEND: drift vs base moment My", "match": "",
     "x": {"section_1": "V_PIER&RU&2&0", "section_2": "V_PIER&RU&1&0", "column": "Ux", "factor": 1.0,
           "label": "Drift RU2 - RU1 [mm]"},
     "y": {"section_1": "V_PIER&RU&1&0", "section_2": "", "column": "My", "factor": 1e-6,
           "label": "Moment RU1 My [kNm]"}},
]

# colour of each mechanism's label in the "lowest drift per damage state" plot
MECH_COLORS = {"AX+": "#7030A0", "AX-": "#C55A11", "SH": "#008080", "REB": "#E000E0"}


def default_defaults():
    return {"format": "dscal-defaults", "version": 1,
            "note": "Default mechanisms (section pairs, component, sense) and initial thresholds [mm] of the "
                    "damage-state calibration. Thresholds are STARTS of DS1 ... DS5 (null = not defined). "
                    "Source of the thresholds: " + THRESHOLD_SOURCE,
            "pushover": copy.deepcopy(PUSHOVER_DEFAULTS),
            "mechanisms": copy.deepcopy(DEFAULT_MECHANISMS),
            "groups": copy.deepcopy(DEFAULT_GROUPS),
            "top_chart": {"auto": True, "preset": TOP_CHART_PRESETS[0]["name"],
                          "presets": copy.deepcopy(TOP_CHART_PRESETS)},
            # axes of the "lowest drift per damage state" window (follow_top: as the top chart of step 1)
            "envelope_chart": {"follow_top": True, "auto": True, "preset": TOP_CHART_PRESETS[0]["name"],
                               "presets": copy.deepcopy(TOP_CHART_PRESETS)},
            "mechanism_colors": dict(MECH_COLORS)}


def complete_defaults(data):
    """Defaults file with every missing part taken from the script defaults."""
    out = default_defaults()
    if not data:
        return out
    out["note"] = data.get("note", out["note"])
    out["pushover"].update(data.get("pushover") or {})
    if data.get("mechanisms"):
        out["mechanisms"] = [complete_mechanism(m) for m in data["mechanisms"]]
    if data.get("groups"):
        out["groups"] = data["groups"]
    for key in ("top_chart", "envelope_chart"):
        if (data.get(key) or {}).get("presets"):
            out[key].update(data[key])
    out["mechanism_colors"].update(data.get("mechanism_colors") or {})
    return out


def axis_expr(axis):
    """{SET:COLUMN} expression of a chart axis {"section_1", "section_2", "column", "factor"}."""
    c = axis.get("column", "Ux")
    e = f"{{{axis['section_1']}:{c}}}"
    if axis.get("section_2"):
        e = f"({e} - {{{axis['section_2']}:{c}}})"
    f = float(axis.get("factor", 1.0) or 1.0)
    return e if f == 1.0 else f"{e} * {f:g}"


def axis_text(axis):
    """Readable definition: 'RU2 - RU1 fiy x 1000'."""
    s = section_short(axis.get("section_1", ""))
    if axis.get("section_2"):
        s += " - " + section_short(axis["section_2"])
    f = float(axis.get("factor", 1.0) or 1.0)
    return f"{s} {axis.get('column', '')}" + ("" if f == 1.0 else f" x {f:g}")


def same_axis(a, b):
    keys = ("section_1", "section_2", "column")
    return all((a.get(k) or "") == (b.get(k) or "") for k in keys) and \
        float(a.get("factor", 1) or 1) == float(b.get("factor", 1) or 1)


def preset_for_jobs(top, jobs):
    """Preset of the top chart for these jobs: with "auto", the first preset whose "match" text is in every job
    name; else (or if none matches) the chosen preset. Returns the preset dict (with "x", "y")."""
    presets = top.get("presets") or TOP_CHART_PRESETS
    if top.get("auto") and jobs:
        for p in presets:
            m = (p.get("match") or "").upper()
            if m and all(m in j.upper() for j in jobs):
                return p
    return next((p for p in presets if p["name"] == top.get("preset")), presets[0])


def complete_mechanism(m):
    base = next((d for d in DEFAULT_MECHANISMS if d["key"] == m.get("key")), DEFAULT_MECHANISMS[0])
    out = copy.deepcopy(base)
    if m.get("key") not in [d["key"] for d in DEFAULT_MECHANISMS]:   # user-defined mechanism
        out.update({"name": m.get("key", "?"), "locations": [], "etabs": {"property_pattern": "", "components": [],
                                                                         "sense": "abs", "side": "Min=Max",
                                                                         "project_link_types": []}})
    for k, v in m.items():
        out[k] = copy.deepcopy(v)
    out["thresholds"] = {k: out.get("thresholds", {}).get(k) for k in DS}
    out.setdefault("provisional", [])
    return out


def load_defaults(path=DEFAULTS_FILE):
    """Defaults file (created from the script defaults when it does not exist)."""
    if not os.path.isfile(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(DEFAULTS_FILE)):
            save_json(default_defaults(), path)
        else:
            raise FileNotFoundError(path)
    data = load_json(path)
    if data.get("format") != "dscal-defaults":
        raise ValueError(f"{path} is not a defaults file of this workflow")
    return complete_defaults(data)


def mechanism_by_key(defaults, key):
    return next((m for m in defaults["mechanisms"] if m["key"] == key), None)


# =====================================================================================================
# JSON
# =====================================================================================================

def save_json(data, path, indent=1):
    """Atomic write (temporary file + rename): an interrupted save never leaves a half-written file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(text):
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text)).strip(" .") or "unnamed"


def num(text):
    """Number from a text field ('' / '-' -> None); accepts a decimal comma."""
    t = str(text).strip().replace(",", ".").rstrip("*").strip()
    if t in ("", "-", "–"):
        return None
    return float(t)


def fmt(v, digits=4):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    return f"{v:.{digits}g}"


# =====================================================================================================
# AXIS LIMITS OF THE PLOTS (chosen by the user; None = automatic)
# =====================================================================================================

def limit_pair(lo_text, hi_text):
    """(lo, hi) from two text fields; empty or not a number = None (automatic)."""
    out = []
    for t in (lo_text, hi_text):
        try:
            out.append(num(t))
        except ValueError:
            out.append(None)
    return tuple(out)


def fit_y(ax, x0, x1):
    """y limits of the data drawn between x0 and x1 (lines and line collections; artists with gid 'nofit' are
    ignored, e.g. the frames after a collapse)."""
    ys = []
    for ln in ax.lines:
        if ln.get_gid() == "nofit":
            continue
        x, y = (np.asarray(v, dtype=float).ravel() for v in ln.get_data())
        if x.size == y.size and x.size:                         # curves and single markers
            ys.append(y[(x >= x0) & (x <= x1)])
    for coll in ax.collections:
        if coll.get_gid() == "nofit" or not hasattr(coll, "get_segments"):
            continue
        for seg in coll.get_segments():
            seg = np.asarray(seg)
            if seg.size:
                ys.append(seg[(seg[:, 0] >= x0) & (seg[:, 0] <= x1), 1])
    ys = np.concatenate([y[np.isfinite(y)] for y in ys if y.size]) if ys else np.array([])
    if ys.size:
        lo, hi = float(ys.min()), float(ys.max())
        pad = 0.06 * (hi - lo or abs(hi) or 1.0)
        ax.set_ylim(lo - pad, hi + pad)


def apply_limits(ax, xlim=(None, None), ylim=(None, None)):
    """User limits on an axes. Only the given bounds are fixed; the others stay automatic. When the x range is
    limited and the y axis is not (fully), y is fitted to the data inside the x range."""
    x0, x1 = xlim
    y0, y1 = ylim
    if x0 is not None or x1 is not None:
        cur = ax.get_xlim()
        ax.set_xlim(x0 if x0 is not None else cur[0], x1 if x1 is not None else cur[1])
        if y0 is None or y1 is None:
            fit_y(ax, *ax.get_xlim())
    if y0 is not None or y1 is not None:
        cur = ax.get_ylim()
        ax.set_ylim(y0 if y0 is not None else cur[0], y1 if y1 is not None else cur[1])


# =====================================================================================================
# ABAQUS RESULTS DATABASE
# =====================================================================================================

REF_RE = re.compile(r"\{([^{}]*)\}")
EXPR_FUNCS = {"abs": np.abs, "sqrt": np.sqrt, "maximum": np.maximum, "minimum": np.minimum, "hypot": np.hypot,
              "np": np}
SECTION_COLUMNS = ("Ux", "Uy", "Uz", "fix", "fiy", "fiz", "Fx", "Fy", "Fz", "Mx", "My", "Mz")
COMPONENTS = ("Ux", "Uy", "Uz", "fix", "fiy", "fiz")


def job_family(job):
    """Job name without its last '_<number>' (SHEAR_KEY_S_TIE_EX_34272 -> SHEAR_KEY_S_TIE_EX)."""
    fam, _, n = job.rpartition("_")
    return fam if fam and n.isdigit() else job


def job_axial_N(job):
    n = job.rpartition("_")[2]
    return int(n) if n.isdigit() else None


def read_table(path):
    """One result .txt -> {column: array} (comma separated, header line, every line ends with a comma)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        return {}
    headers = [h.strip() for h in lines[0].split(",")]
    while headers and not headers[-1]:
        headers.pop()
    rows = []
    for ln in lines[1:]:
        vals = ln.split(",")
        row = []
        for i in range(len(headers)):
            try:
                row.append(float(vals[i]) if i < len(vals) and vals[i].strip() else np.nan)
            except ValueError:
                row.append(np.nan)
        rows.append(row)
    arr = np.array(rows, dtype=float).reshape(len(rows), len(headers))
    return {h: arr[:, i] for i, h in enumerate(headers)}


def looks_like_database(path, max_sets=40):
    """True if path/<SET>/<JOB>/*.txt exists."""
    try:
        sets = [d for d in os.scandir(path) if d.is_dir()][:max_sets]
    except OSError:
        return False
    for s in sets:
        try:
            for j in os.scandir(s.path):
                if j.is_dir() and any(f.name.lower().endswith(".txt") for f in os.scandir(j.path)):
                    return True
        except OSError:
            continue
    return False


def find_databases(folder=ABAQUS_DB_DIR):
    """Databases in the input folder (the folder itself or its sub-folders), sorted by name."""
    if not os.path.isdir(folder):
        return []
    if looks_like_database(folder):
        return [folder]
    return sorted(d.path for d in os.scandir(folder) if d.is_dir() and looks_like_database(d.path))


class AbaqusDB:
    """<root>/<SET>/<JOB>/*.txt (a Panel_results database). Tables are read on first use and cached."""

    _shared = {}

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.mapping = {}                          # set -> job -> txt path
        self._cache = {}
        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"Abaqus database not found: {root}")
        for s in sorted(os.scandir(self.root), key=lambda d: d.name):
            if not s.is_dir():
                continue
            for j in os.scandir(s.path):
                if j.is_dir():
                    txts = sorted(f.name for f in os.scandir(j.path) if f.name.lower().endswith(".txt"))
                    if txts:
                        self.mapping.setdefault(s.name, {})[j.name] = os.path.join(j.path, txts[0])

    @classmethod
    def open(cls, root):
        """Shared instance per folder (the GUIs open the same database many times)."""
        key = os.path.normcase(os.path.abspath(root))
        if key not in cls._shared:
            cls._shared[key] = cls(root)
        return cls._shared[key]

    @property
    def name(self):
        return os.path.basename(self.root)

    def sets(self):
        return sorted(self.mapping)

    def jobs(self):
        out = set()
        for jobs in self.mapping.values():
            out.update(jobs)
        return sorted(out, key=lambda j: (job_family(j), job_axial_N(j) or 0, j))

    def families(self):
        out = {}
        for j in self.jobs():
            out.setdefault(job_family(j), []).append(j)
        return out

    def column(self, set_name, job, col):
        path = self.mapping.get(set_name, {}).get(job)
        if path is None:
            raise KeyError(f"no {set_name} / {job} in {self.name}")
        if path not in self._cache:
            self._cache[path] = read_table(path)
        table = self._cache[path]
        if col not in table:
            raise KeyError(f"column {col} not in {set_name} / {job}")
        return table[col]

    def evaluate(self, expr, job):
        """{SET:COLUMN} expression -> array (shortest length of the arrays used)."""
        names = {}

        def sub(mt):
            set_name, _, col = mt.group(1).rpartition(":")
            key = f"_r{len(names)}"
            names[key] = self.column(set_name.strip(), job, col.strip())
            return key

        code = REF_RE.sub(sub, expr)
        n = min((len(v) for v in names.values()), default=0)
        names = {k: v[:n] for k, v in names.items()}
        val = eval(code, {"__builtins__": {}}, {**EXPR_FUNCS, **names})  # noqa: S307 - the user's own formulas
        return np.broadcast_to(np.asarray(val, dtype=float), (n,)).copy()


# =====================================================================================================
# LOAD-CASE GROUPS
# =====================================================================================================

def match_filter(name, text):
    """Name filter of a load-case group (case-insensitive parts of the job name):
        'SHEAR NOKEY'    both parts must be in the name
        'TIE|FR05'       either part
        '-BEND' / '!BEND'  the part must not be in the name
    An empty filter matches every job."""
    n = name.upper()
    for tok in str(text or "").upper().split():
        if tok[0] in "-!":
            if tok[1:] and tok[1:] in n:
                return False
        elif not any(alt and alt in n for alt in tok.split("|")):
            return False
    return True


def group_jobs(db, filter_text):
    return [j for j in db.jobs() if match_filter(j, filter_text)]


# =====================================================================================================
# MECHANISM VALUES, COLLAPSE, DRIFT <-> VALUE
# =====================================================================================================

def section_expr(mech):
    """Displacement of section B relative to section A: {B:c} - {A:c}."""
    c = mech["component"]
    return f"{{{mech['section_b']}:{c}}} - {{{mech['section_a']}:{c}}}"


def section_short(name):
    """V_PIER&RU&1&0 -> RU1, V_SPEC&DM&2&0 -> SPEC DM2, V_PIER&SREDNJI1&1&0 -> SREDNJI1_1."""
    s = str(name or "")
    m = re.match(r"^V_(PIER|SPEC)&(.+)&(\d+)&\d+$", s)
    if not m:
        return s
    kind, base, point = m.groups()
    return ("SPEC " if kind == "SPEC" else "") + base + ("_" if base[-1:].isdigit() else "") + point


def section_label(mech):
    return f"{section_short(mech['section_b'])} - {section_short(mech['section_a'])} ({mech['component']})"


def damage_value(raw, sense, relative=False):
    """Value compared with the thresholds: '+' raw, '-' -raw (negative values are damage), 'abs' |raw|.
    relative: measured from frame 0 (the state after the axial load) instead of from absolute 0."""
    v = np.asarray(raw, dtype=float)
    if relative and v.size:
        v = v - v[0]
    if sense == "-":
        return -v
    if sense == "abs":
        return np.abs(v)
    return v


def value_axis_label(mech):
    s = {"+": "", "-": "-", "abs": "|"}.get(mech.get("sense", "+"), "")
    lab = section_label(mech)
    lab = f"|{lab}|" if s == "|" else f"{s}({lab})" if s else lab
    if mech.get("relative_to_start"):
        lab += " from zero drift"
    return f"{mech['key']}: {lab} [mm]"


def valid_end(arrays, jump_mm=5.0, ratio=10.0):
    """Number of valid frames: the frames before the first step where any array (mm) changes by more than
    jump_mm AND more than ratio x every earlier step (collapse: the panel falls by metres). 0 = off."""
    n = min((len(a) for a in arrays), default=0)
    if not jump_mm or n < 2:
        return n
    end = n
    for a in arrays:
        inc = np.abs(np.diff(np.nan_to_num(np.asarray(a[:n], dtype=float))))
        running = 0.0
        for i, step in enumerate(inc):
            if step > jump_mm and step > ratio * running:
                end = min(end, i + 1)
                break
            running = max(running, step)
    return end


def passed(value, threshold):
    """Where a value is past a threshold. The sign of the threshold gives the direction:
    threshold >= 0: value > threshold (rising), threshold < 0: value < threshold (falling, e.g. compression
    entered as negative values). NaN is never past."""
    v = np.asarray(value, dtype=float)
    with np.errstate(invalid="ignore"):
        hit = (v < threshold) if threshold < 0 else (v > threshold)
    return hit & np.isfinite(v)


def drift_at_value(drift, value, threshold):
    """First drift where value passes threshold (strictly, in the direction of the threshold's sign - see
    passed()), linear interpolation between frames. Returns None when the threshold is never passed."""
    if threshold is None:
        return None
    v = np.asarray(value, dtype=float)
    hits = np.flatnonzero(passed(v, threshold))
    if not hits.size:
        return None
    i = int(hits[0])
    if i == 0:
        return float(drift[0])
    v0, v1 = v[i - 1], v[i]
    if not np.isfinite(v0):                      # no value before: the state starts at this frame
        return float(drift[i])
    f = 0.0 if v1 == v0 else (threshold - v0) / (v1 - v0)
    return float(drift[i - 1] + min(max(f, 0.0), 1.0) * (drift[i] - drift[i - 1]))


def value_at_drift(drift, value, target):
    """Value at the first frame where drift reaches target (linear interpolation); None beyond the data."""
    if target is None:
        return None
    d = np.nan_to_num(np.asarray(drift, dtype=float), nan=-np.inf)
    hits = np.flatnonzero(d >= target - 1e-9)
    if not hits.size:
        return None
    i = int(hits[0])
    if i == 0:
        return float(value[0])
    d0, d1 = d[i - 1], d[i]
    f = 0.0 if d1 == d0 else (target - d0) / (d1 - d0)
    return float(value[i - 1] + f * (value[i] - value[i - 1]))


def collapse_end(db, job, pushover, arrays):
    """Valid frames of a job: collapse rule on the drift, the collapse probe and the given arrays."""
    probe = pushover.get("collapse_probe")
    extra = []
    if probe:
        try:
            extra = [db.evaluate(probe, job)]
        except (KeyError, SyntaxError, NameError):
            extra = []
    return valid_end(list(arrays) + extra, pushover.get("collapse_jump_mm", 5.0),
                     pushover.get("collapse_jump_ratio", 10.0))


def job_curves(db, job, pushover, mech):
    """Curves of one job: drift, force, raw section displacement and damage value, cut at the collapse.
    Returns dict(drift, force, raw, value, n_all, n, end_drift, value0) - arrays already cut to n."""
    drift = db.evaluate(pushover["drift"], job)
    force = db.evaluate(pushover["force"], job)
    raw = db.evaluate(section_expr(mech), job)
    n_all = min(len(drift), len(force), len(raw))
    drift, force, raw = drift[:n_all], force[:n_all], raw[:n_all]
    n = min(collapse_end(db, job, pushover, [drift, raw]), n_all)
    value = damage_value(raw, mech.get("sense", "+"), mech.get("relative_to_start", False))
    finite = drift[:n][np.isfinite(drift[:n])]
    return {"drift": drift[:n], "force": force[:n], "raw": raw[:n], "value": value[:n],
            "drift_all": drift, "force_all": force, "value_all": value, "n_all": n_all, "n": n,
            "end_drift": float(finite.max()) if finite.size else 0.0,
            "value0": float(value[0]) if n else None}


def pushover_curve(db, job, pushover):
    """(drift, force) of a job cut at the collapse of the drift."""
    drift = db.evaluate(pushover["drift"], job)
    force = db.evaluate(pushover["force"], job)
    n = min(len(drift), len(force))
    end = min(collapse_end(db, job, pushover, [drift[:n]]), n)
    return drift[:end], force[:end]


# =====================================================================================================
# CRITERIA OF ONE JOB (step 1)
# =====================================================================================================
#
# Every damage state of a row has: disp (threshold of the section displacement), drift (where it starts) and the
# source of the pair:
#     "default"   disp = threshold of the file, drift computed from it
#     "disp"      the user gave the displacement, drift computed
#     "drift"     the user gave the drift, displacement computed
#     "none"      not defined
# status: "" ok, "not reached" (disp never exceeded in the valid data), "beyond data" (drift after the end)

def new_ds_entry(source="default"):
    return {"source": source, "disp": None, "drift": None, "status": ""}


def compute_entry(entry, curves, default_disp):
    """Fill disp / drift of one damage state of a row from its source."""
    src = entry.get("source", "default")
    entry["status"] = ""
    if src == "none" or curves is None:
        if src == "none":
            entry["disp"] = entry["drift"] = None
        return entry
    if src == "default":
        entry["disp"] = default_disp
    if src in ("default", "disp"):
        if entry["disp"] is None:
            entry["drift"] = None
            return entry
        entry["drift"] = drift_at_value(curves["drift"], curves["value"], entry["disp"])
        if entry["drift"] is None:
            entry["status"] = "not reached"
    elif src == "drift":
        if entry["drift"] is None:
            entry["disp"] = None
            return entry
        if entry["drift"] > curves["end_drift"] + 1e-9:
            entry["disp"] = None
            entry["status"] = "beyond data"
        else:
            entry["disp"] = value_at_drift(curves["drift"], curves["value"], entry["drift"])
    return entry


def compute_row(row, curves, thresholds):
    row["end_drift"] = curves["end_drift"] if curves else None
    row["value_at_zero_drift"] = curves["value0"] if curves else None
    for k in DS:
        compute_entry(row["DS"].setdefault(k, new_ds_entry()), curves, thresholds.get(k))
    return row


def row_warnings(row):
    """Damage states must start in order: DS1 <= DS2 <= ... (drift) and |DS1| < |DS2| < ... (displacement)."""
    out = []
    prev_d = prev_v = None
    for k in DS:
        e = row["DS"].get(k) or {}
        d, v = e.get("drift"), e.get("disp")
        if d is not None and prev_d is not None and d < prev_d - 1e-9:
            out.append(f"{k} starts at a smaller drift than the previous state")
        if v is not None and prev_v is not None and abs(v) <= abs(prev_v):
            out.append(f"{k} displacement is not larger (in size) than the previous state")
        prev_d = d if d is not None else prev_d
        prev_v = v if v is not None else prev_v
    return out


def ds_codes_from_starts(drift, starts):
    """Damage state per frame from the start drifts {DSk: drift or None}."""
    d = np.nan_to_num(np.asarray(drift, dtype=float), nan=-np.inf)
    codes = np.zeros(d.shape, dtype=int)
    for k, name in enumerate(DS, 1):
        s = starts.get(name)
        if s is not None:
            codes[d >= s - 1e-9] = k
    return np.maximum.accumulate(codes) if codes.size else codes


def ds_codes_from_values(values, thresholds, running=True):
    """Damage state per step from values and thresholds {DSk: value or None}: DSk when the value is past the
    threshold (value > threshold, or value < threshold for a negative threshold)."""
    v = np.asarray(values, dtype=float)
    codes = np.zeros(v.shape, dtype=int)
    for k, name in enumerate(DS, 1):
        t = thresholds.get(name)
        if t is not None:
            hit = passed(v, t)
            codes[hit] = np.maximum(codes[hit], k)
    return np.maximum.accumulate(codes) if (running and codes.size) else codes


def thresholds_grow(values):
    """Defined thresholds must grow strictly in size (negative thresholds: more negative)."""
    vals = [abs(v) for v in values if v is not None]
    return all(b > a for a, b in zip(vals, vals[1:]))


# =====================================================================================================
# CRITERIA FILES (output of step 1)
# =====================================================================================================

CRITERIA_FORMAT = "abaqus-section-criteria"


def list_criteria_files(folder=None):
    """[(path, header dict)] of the criteria files, newest first."""
    folder = folder or CRITERIA_DIR
    out = []
    if not os.path.isdir(folder):
        return out
    for f in os.scandir(folder):
        if f.name.lower().endswith(".json"):
            try:
                d = load_json(f.path)
            except (OSError, ValueError):
                continue
            if d.get("format") == CRITERIA_FORMAT:
                out.append((f.path, d))
    out.sort(key=lambda t: os.path.getmtime(t[0]), reverse=True)
    return out


def criteria_title(d):
    return d.get("name") or "?"
