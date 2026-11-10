"""
Core of the single-panel pushover tool (no GUI). Used by Panel_pushover_GUI.py.

ETABS part   run the pushover of the wall panel model for several axial loads and save every result per step
             (joint displacements and reactions, link deformations and forces, base reactions).
Data part    saved runs, derived quantities (drift, forces, link deformations, node uplift ...),
             damage states from acceptance criteria.
Abaqus part  the Abaqus results database (Panel_results) and damage-state workbook, for comparison.

Units: ETABS results are saved in kN and m (ETABS units kN, m, C).
       Quantities, criteria and reports use mm and kN.

ETABS API notes (CSI API ETABS v1, checked on the running ETABS):
    - comtypes returns ByRef outputs as a tuple with the return code LAST.
    - The nonlinear direct-integration case has no Set functions in the API; its load scale factor is changed by
      editing the table "Load Case Definitions - Time History - Nonlin Direct Integration"
      (GetTableForEditingArray -> SetTableForEditingArray -> ApplyEditedTables, model saved before, as the help says).
    - SetOptionDirectHist(2) = step-by-step results.
"""

import copy
import json
import math
import os
import re
import time

import numpy as np


TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(TOOL_DIR, "panel_pushover_settings.json")
CRITERIA_FILE = os.path.join(TOOL_DIR, "acceptance_criteria_sets.json")
REFERENCE_FILE = os.path.join(TOOL_DIR, "abaqus_reference.json")

DS_NAMES = ("ND", "LD", "SD", "NC")
DISP_COMPONENTS = ("U1", "U2", "U3", "R1", "R2", "R3")
REACT_COMPONENTS = ("F1", "F2", "F3", "M1", "M2", "M3")
LINK_FORCE_COMPONENTS = ("P", "V2", "V3", "T", "M2", "M3")
BASE_COMPONENTS = ("FX", "FY", "FZ", "MX", "MY", "MZ")

NL_DIRHIST_TABLE = "Load Case Definitions - Time History - Nonlin Direct Integration"
KN_M_C = 6                                       # eUnits.kN_m_C


class PanelError(Exception):
    """A problem the user can fix (shown as a message, not as a traceback)."""


# =====================================================================================================
# SETTINGS AND INPUT FILES
# =====================================================================================================

def default_levels():
    """Axial loads (kN) and horizontal scale factors. Loads equal to the Abaqus jobs where they exist
    (34 272, 172 699, 360 000 ... N) so ETABS and Abaqus pair one to one; 68.874 kN has no Abaqus job."""
    return [{"run": True, "axial_kN": a, "scale_factor": sf} for a, sf in
            [(34.272, 40), (68.874, 40), (172.699, 40), (360, 90), (720, 160), (1080, 200), (1440, 290),
             (1800, 330)]]


DEFAULT_QUANTITIES = [
    # key, name, expression in ETABS results (m, kN), kind ("disp" -> shown in mm, "force" -> kN)
    {"key": "drift", "name": "Drift (average of nodes 1198 / 1196)", "expr": "({J1198:U2} + {J1196:U2}) / 2",
     "kind": "disp"},
    {"key": "drift_1198", "name": "Horizontal displacement node 1198", "expr": "{J1198:U2}", "kind": "disp"},
    # ETABS reports no horizontal support reaction here (the links' fixed DOFs act as constraints), so the
    # forces are taken from the bottom shear links, as in the Excel ("Pushover DOWN" = K2 SH + K4 SH)
    {"key": "H", "name": "Horizontal force, bottom shear links (K2 + K4 V3)", "expr": "{K2:V3} + {K4:V3}",
     "kind": "force"},
    {"key": "V", "name": "Vertical force, bottom links (K2 + K4 P, compression < 0)", "expr": "{K2:P} + {K4:P}",
     "kind": "force"},
    {"key": "H_top", "name": "Horizontal force, top shear links (K5 + K7 V3)", "expr": "{K5:V3} + {K7:V3}",
     "kind": "force"},
    {"key": "V_top", "name": "Vertical force, top links (K5 + K7 P)", "expr": "{K5:P} + {K7:P}", "kind": "force"},
    {"key": "ax_uplift", "name": "AX link uplift, bottom heel (K3 U1)", "expr": "{K3:U1}", "kind": "disp"},
    {"key": "ax_crush", "name": "AX link crushing, bottom toe (K1 U1)", "expr": "{K1:U1}", "kind": "disp"},
    {"key": "ax_top_K8", "name": "AX link top K8 U1 (mirror of K3)", "expr": "{K8:U1}", "kind": "disp"},
    {"key": "ax_top_K6", "name": "AX link top K6 U1 (mirror of K1)", "expr": "{K6:U1}", "kind": "disp"},
    {"key": "sh_slide", "name": "SH link sliding, bottom (average K2 / K4 U3)", "expr": "({K2:U3} + {K4:U3}) / 2",
     "kind": "disp"},
    {"key": "sh_slide_top", "name": "SH link sliding, top (average K5 / K7 U3)", "expr": "({K5:U3} + {K7:U3}) / 2",
     "kind": "disp"},
    {"key": "node_uplift", "name": "Uplift, Uz node 1198 - node 1199", "expr": "{J1198:U3} - {J1199:U3}",
     "kind": "disp"},
    {"key": "node_crush", "name": "Crushing, Uz node 1196 - node 1201", "expr": "{J1196:U3} - {J1201:U3}",
     "kind": "disp"},
]


def default_settings():
    return {
        "format": "panel-pushover-settings", "version": 1,
        # ETABS model
        "etabs_instance": "AX_API_attempt",      # model file name (or a unique part) or ETABS process ID
        "case": "PSH_test_DIN_sila",              # pushover case (nonlinear direct integration)
        "vertical_pattern": "G",                  # pattern of the axial force (applied by the initial case)
        "vertical_nodes": ["1198", "1196"],       # the axial force is split between these nodes
        "vertical_shares": [0.5, 0.5],
        "horizontal_pattern": "HOR_sila",         # its load in the case gets the scale factor
        "control_node": "1198",                   # horizontal displacement of this node is checked
        "control_dof": "U2",                      # ETABS global Y = in plane of the panel
        # displacement window and automatic scale factor
        "disp_min": 0.2,                          # m - less: the pushover did not reach the failure mechanism
        "disp_max": 1.0,                          # m - more: unrealistic displacements
        "auto_adjust": True,
        "max_attempts": 8,
        "factor_down": 0.75,                      # scale factor x this when the displacement is too large
        "factor_up": 1.4,                         # scale factor x this when it is too small
        "run_only_needed_cases": True,            # run only the pushover case and its initial-condition cases
        "restore_run_flags": True,                # put the case run flags back at the end
        # stalled analyses (scale factor too high: the solver crawls with tiny substeps after the capacity)
        "cancel_stalled": True,                   # press Cancel in ETABS when the analysis stalls
        "stall_minutes": 2.0,                     # stalled = the analysis time advanced less than ...
        "stall_min_progress_s": 0.1,              # ... this many seconds of analysis time in stall_minutes
        "max_analysis_minutes": 30.0,             # cancel any analysis that runs longer
        "stall_overshoot": 0.03,                  # next factor: force at the stall x (1 + this) at the ramp end
        # solver: solution scheme of the case ("" = as in the model; per level the "scheme" column wins)
        "solution_scheme": "",
        # when an analysis fails or stalls BELOW the window, the same scale factor is tried again with these
        # schemes, in this order, before the scale factor is changed (Iterative Events solved 1080 / 1440 kN,
        # where Iterative Only stopped early and Events Only chattered)
        "fallback_schemes": ["Iterative Events", "Events Only"],
        "fallback_on_stall": True,
        # valid data: the results end before the first spurious jump or vertical-force excursion
        "jump_mm": 5.0,                           # a step that moves the control node more than this ...
        "jump_ratio": 10.0,                       # ... and more than this x every earlier step is a jump
        "vertical_tolerance": 0.10,               # vertical force (quantity V) may deviate this much from step 0
        "levels": default_levels(),
        "runs_folder": "ETABS_results",           # the ETABS results database of the workflow (read by step 3)
        "run_name": "",
        # results / report
        "quantities": copy.deepcopy(DEFAULT_QUANTITIES),
        "report_run": "",
        "criteria_set": "",
        "panel_height_mm": 2720.0,                # for drift in % (ETABS nodes 1199 -> 1198)
        "running_damage": True,                   # damage never decreases during the push
        "drift_plot_max_mm": None,                # x axis limit of drift plots, None = automatic
        "abaqus_compare": True,
    }


def merge_defaults(data, defaults):
    """data with every missing key taken from defaults (one level deep for dicts)."""
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        out[k] = v
    return out


def load_settings(path=SETTINGS_FILE):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return merge_defaults(json.load(f), default_settings())
    return default_settings()


def long_path(path):
    """Path usable beyond the 260-character limit of Windows (\\\\?\\ prefix for long absolute paths)."""
    if os.name != "nt" or not path:
        return path
    p = os.path.abspath(path)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def makedirs(path):
    os.makedirs(long_path(path), exist_ok=True)


def save_json(data, path, indent=1):
    """Atomic JSON write (temporary file + rename): an interrupted run never leaves a half-written file."""
    makedirs(os.path.dirname(os.path.abspath(path)))
    tmp = long_path(path + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, long_path(path))
    return path


def load_json(path):
    with open(long_path(path), encoding="utf-8") as f:
        return json.load(f)


def resolve(path, base=TOOL_DIR):
    """Relative paths in the input files are relative to the tool folder."""
    if not path:
        return ""
    path = os.path.expanduser(str(path))
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


# --------------------------------------------------------------------- acceptance criteria sets

def _check(name, quantity, sense, nd, ld, sd, nc, use=True, relative=False):
    return {"name": name, "quantity": quantity, "sense": sense, "relative_to_start": relative,
            "use_for_DS": use, "limits": {"ND": nd, "LD": ld, "SD": sd, "NC": nc}}


def _set(note, tension, compression, use_nodes=False):
    return {"note": note, "checks": [
        _check("AX uplift (K3, bottom heel)", "ax_uplift", "+", *tension),
        _check("AX crushing (K1, bottom toe)", "ax_crush", "-", *compression),
        _check("SH sliding (K2/K4, bottom)", "sh_slide", "abs", 0.01, 0.5, 3.0, None),
        _check("Node uplift (1198 - 1199)", "node_uplift", "+", 0.5, 5.0, 15.0, None, use_nodes),
        _check("Node crushing (1196 - 1201)", "node_crush", "-", *compression, use=use_nodes),
    ]}


def default_criteria():
    """Sets from 'ALL AXIAL FINAL.xlsx' (sheet 'lin panel drift to AX monoton'), in mm.
    Limits are the UPPER bound of each state, as in the Excel ('U1 vlak ND' = end of ND):
    value < ND -> ND, < LD -> LD, < SD -> SD, otherwise NC; the NC limit is the end of NC (collapse)."""
    comp = (1.0, 2.0, 3.0, 4.6)
    return {
        "format": "panel-acceptance-criteria", "version": 1, "units": "mm",
        "explanation": "Limits are upper bounds of each damage state: value < ND -> ND, < LD -> LD, < SD -> SD, "
                       "otherwise NC; NC = end of NC (empty = to the end of the analysis). sense '+' = positive "
                       "values are damage (opening), '-' = negative values (closing), 'abs' = both. "
                       "relative_to_start: subtract the value at step 0 (state after the axial load).",
        "sets": {
            "Excel - used in the DS columns (AR..AY)": _set(
                "Limits of row 3 of the sheet (used by the DS formulas)", (0.31546, 1.57729, 6.30916, 15.77290),
                comp),
            "Excel - read from Abaqus": _set("'Granice ocitane iz abaqusa'", (0.1, 0.5, 2.0, 5.0), comp),
            "Excel - Abaqus with MAX crack correction": _set(
                "'... ALI sa korekcijom MAX dimenzije pukotine za vlak'", (0.2, 1.37, 3.3, 5.5), comp),
            "Excel - tension x2 (MAX / residual crack)": _set(
                "'Korekcija vlaka jos za faktor MAX i res pukotina, uzeto konzervativno 2x'", (0.2, 1.0, 4.0, 10.0),
                comp),
            "Excel - tension x5": _set("'Skaliranje za MAX/residual pukotine, scale factor 5'",
                                       (0.5, 2.5, 10.0, 25.0), comp),
        },
    }


def load_criteria(path=CRITERIA_FILE):
    if not os.path.isfile(path):
        save_json(default_criteria(), path, indent=2)
    data = load_json(path)
    if data.get("format") != "panel-acceptance-criteria":
        raise PanelError(f"{path} is not an acceptance criteria file")
    return data


def save_criteria(data, path=CRITERIA_FILE):
    save_json(data, path, indent=2)


# --------------------------------------------------------------------- Abaqus reference

def default_reference():
    """Abaqus data for the comparison plots. Envelopes and curves read from 'ALL AXIAL FINAL.xlsx'."""
    fr06 = [(29, -34), (48, -69), (60, -103), (75, -138), (88.5, -173), (101, -207), (115, -242), (126, -277),
            (139, -311), (156, -360), (267, -750), (300, -1080), (300, -1440), (270, -1800), (220, -2160),
            (50, -2520), (-50, -2520), (-220, -2160), (-270, -1800), (-300, -1440), (-300, -1080), (-265, -750),
            (-158, -360), (-140, -311), (-125, -277), (-115, -242), (-103, -207), (-85, -173), (-72, -138),
            (-60, -103), (-45, -69), (-28, -34)]
    tie = [(-90, -34), (-100, -69), (-110, -103), (-120, -138), (-130, -173), (-143, -207), (-155, -242),
           (-165, -277), (-170, -311), (-180, -360), (-260, -750), (-300, -1080), (-300, -1440), (-270, -1800),
           (-220, -2160), (-50, -2520), (50, -2520), (220, -2160), (270, -1800), (300, -1440), (300, -1080),
           (260, -750), (180, -360), (170, -311), (165, -277), (155, -242), (143, -207), (130, -173), (120, -138),
           (110, -103), (100, -69), (90, -34)]
    drift = [0, 2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20]

    def mm(col):
        return [None if v is None else round(v * 1000, 6) for v in col]

    return {
        "format": "panel-abaqus-reference", "version": 1, "units": "kN, mm",
        "explanation": "envelopes: horizontal force H [kN] vs vertical force V [kN, compression < 0]. "
                       "state 'ultimate' = capacity; 'ND' / 'LD' / 'SD' / 'NC' = force at the end of that damage "
                       "state (fill the points, or let the tool compute them from the Abaqus database + "
                       "damage-state workbook). ax_curves: AX displacement [mm] vs drift [mm] per axial load [kN].",
        "envelopes": [
            {"name": "Abaqus envelope (friction coef 0.6)", "state": "ultimate", "show": True,
             "points": [list(p) for p in fr06]},
            {"name": "Abaqus envelope (TIE)", "state": "ultimate", "show": True, "points": [list(p) for p in tie]},
            {"name": "Abaqus ND envelope", "state": "ND", "show": True, "points": []},
            {"name": "Abaqus LD envelope", "state": "LD", "show": True, "points": []},
            {"name": "Abaqus SD envelope", "state": "SD", "show": True, "points": []},
            {"name": "Abaqus NC envelope", "state": "NC", "show": True, "points": []},
        ],
        "ax_curves": {
            "Tension (FR06 vlak)": {"quantity": "ax_uplift", "drift_mm": drift, "curves": {
                "34": mm([0] * 9),
                "173": mm([-0.00012, 0.00026, 0.00096, 0.00171, 0.00242, 0.00314, 0.00389, 0.0046, 0.0053]),
                "360": mm([-0.00033, -0.0001, 0.00044, 0.001, 0.00165, 0.00227, 0.00295, 0.00354, 0.0042]),
                "720": mm([-0.00072, -0.00087, -0.00065, -0.0003, 0, 0.00055, 0.001, 0.0017, 0.0022]),
                "1080": mm([-0.0014, -0.00171, -0.0019, -0.0021, -0.0022, -0.0021, -0.0023, None, None])}},
            "Compression (FR05 tlak)": {"quantity": "ax_crush", "drift_mm": drift, "curves": {
                "34": mm([0] * 9),
                "173": mm([-0.00013, 0, 0.00011, 0.00026, 0.00045, 0.00065, 0.00084, 0.00104, 0.00127]),
                "360": mm([-0.00034, -0.00034, -0.00034, -0.00026, -0.00015, 0, 0.00013, 0.00028, 0.00045]),
                "720": mm([-0.0007, -0.001, -0.0011, -0.0012, -0.00123, -0.00123, -0.0012, -0.0012, -0.0012]),
                "1080": mm([-0.00141, -0.00175, -0.0021, -0.00242, -0.00255, -0.00268, -0.003, None, None])}},
            "TIE (vlak & tlak)": {"quantity": "ax_uplift", "drift_mm": drift, "curves": {
                "34": mm([0, 0.0005, 0.00135, 0.00221, 0.00315, 0.00402, 0.005, 0.006, 0.0068]),
                "173": mm([-0.00013, 0.00017, 0.00081, 0.00156, 0.0024, 0.0033, 0.0041, 0.0051, 0.006]),
                "360": mm([-0.00034, -0.00025, 0.0002, 0.00085, 0.0015, 0.0022, 0.0029, 0.0038, 0.00455]),
                "720": mm([-0.0007, -0.001, -0.001, -0.0008, -0.00075, -0.00045, -0.00014, 0.00014, 0.00046]),
                "1080": mm([-0.00141, -0.00175, -0.0021, -0.00242, -0.00242, -0.0025, -0.003, None, None])}},
        },
        "abaqus": {
            "database": "../0_INPUT/Abaqus_databases/Panel_results same cases as ETABS",
            "family": "SHEAR_KEY_S_TIE_EX",
            "ds_workbook": "../_previous_tools/Old copy of the plotter tools (from the ETABS folder)/"
                           "Claude excell acceptance criteria/Damage_states_extracted_example.xlsx",
            "ds_family": "SHEAR_KEY_S_TIE_EX",
            "axial_tolerance": 0.03,
            "expressions": {
                "drift": "{V_PIER&RU&2&0:Ux} - {V_PIER&RU&1&0:Ux}",
                "H": "{V_PIER&RU&1&0:Fx} / 1000",
                "V": "-{V_PIER&RU&1&0:Fz} / 1000",
                "ax_uplift": "{V_SPEC&DM&1&0:Uz} - {V_PIER&RU&1&0:Uz}",
                "ax_crush": "{V_SPEC&DPL&2&0:Uz} - {V_SPEC&DPL&1&0:Uz}",
                "sh_slide": "{V_PIER&SREDNJI1&1&0:Ux} - {V_PIER&RU&1&0:Ux}",
                "node_uplift": "{V_PIER&RU&2&0:Uz} - {V_PIER&RU&1&0:Uz}",
                "node_crush": "{V_PIER&RU&2&0:Uz} - {V_PIER&RU&1&0:Uz}",
            },
            "note": "Abaqus quantity matched to each ETABS quantity (mm, kN), {SET:COLUMN} as in the plotter GUIs. "
                    "Uplift = opening at the heel joint (DM1 - RU1), crushing = shortening of the toe slice "
                    "(DPL2 - DPL1), sliding = panel bottom vs base (SREDNJI1 - RU1), node uplift / crushing = "
                    "vertical displacement of the loading point (RU2 - RU1). Change them if another mapping fits "
                    "better.",
        },
    }


def load_reference(path=REFERENCE_FILE):
    if not os.path.isfile(path):
        save_json(default_reference(), path, indent=1)
    data = load_json(path)
    if data.get("format") != "panel-abaqus-reference":
        raise PanelError(f"{path} is not an Abaqus reference file")
    return data


# =====================================================================================================
# ETABS CONNECTION
# =====================================================================================================

PROG_ID = "CSI.ETABS.API.ETABSObject"


def _ret(r):
    """Return code of an API call (comtypes gives a tuple with the code last for ByRef outputs)."""
    return r[-1] if isinstance(r, (list, tuple)) else r


def _helper():
    import comtypes.client
    helper = comtypes.client.CreateObject("ETABSv1.Helper")
    import comtypes.gen.ETABSv1 as ETABSv1
    return helper.QueryInterface(ETABSv1.cHelper)


def etabs_processes():
    """Process IDs of the running ETABS programs."""
    import csv
    import io
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ETABS.exe", "/FO", "CSV", "/NH"], capture_output=True,
                         text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return [int(r[1]) for r in csv.reader(io.StringIO(out)) if len(r) > 1 and r[0].lower() == "etabs.exe"]


def attach(pid):
    try:
        obj = _helper().GetObjectProcess(PROG_ID, int(pid))
    except Exception as e:
        raise PanelError(f"Could not attach to ETABS process {pid}: {e}") from e
    if obj is None:
        raise PanelError(f"ETABS process {pid} does not answer the API (still starting?)")
    return obj.SapModel


def list_instances():
    """[{"pid", "model_file", "model"}] of every running ETABS."""
    out = []
    for pid in etabs_processes():
        try:
            path = str(attach(pid).GetModelFilename(True) or "")
        except Exception:
            path = None
        out.append({"pid": pid, "model_file": path, "model": os.path.splitext(os.path.basename(path or ""))[0]})
    return out


def connect(instance=None):
    """SapModel of the ETABS meant by `instance` (model file name or a unique part of it, or a process ID).
    With several ETABS running and no instance: error listing them (never picks one silently)."""
    return connect_pid(instance)[0]


def connect_pid(instance=None):
    """(SapModel, process ID) - see connect()."""
    instances = list_instances()
    text = "\n".join(f"    process {i['pid']}: {i['model_file'] or '(no model open)'}" for i in instances) or "    none"
    if not instances:
        raise PanelError("No running ETABS found. Start ETABS and open the panel model.")
    key = str(instance or "").strip()
    if not key:
        if len(instances) > 1:
            raise PanelError("Several ETABS are running - choose one:\n" + text)
        hits = instances
    elif key.isdigit():
        hits = [i for i in instances if i["pid"] == int(key)]
    else:
        k = os.path.splitext(os.path.basename(key))[0].lower()
        hits = [i for i in instances if i["model"].lower() == k] or \
            [i for i in instances if k in i["model"].lower()]
    if len(hits) != 1:
        raise PanelError(f"{'No' if not hits else 'More than one'} running ETABS matches '{instance}'. "
                         f"Running:\n{text}")
    return attach(hits[0]["pid"]), hits[0]["pid"]


# =====================================================================================================
# STALLED ANALYSES: WATCH THE .LOG FILE, PRESS CANCEL IN ETABS
# =====================================================================================================

def press_cancel(pid):
    """Click the Cancel button of the ETABS analysis panel (as the user would). True if a button was found."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def text(h):
        n = user32.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, b, n + 1)
        return b.value

    def cls(h):
        b = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, b, 256)
        return b.value

    found = []

    def child(c, _):
        if user32.IsWindowVisible(c) and text(c) == "Cancel" and "Button" in cls(c):
            found.append(c)
        return True

    def top(h, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            user32.EnumChildWindows(h, enum_proc(child), 0)
        return True

    user32.EnumWindows(enum_proc(top), 0)
    if found:
        user32.PostMessageW(found[0], 0x00F5, 0, 0)        # BM_CLICK, posted so a dialog cannot block us
    return bool(found)


class StallWatch:
    """Watches the ETABS .LOG during RunAnalysis (in a thread). The solver writes one line per (sub)step, the last
    number being the analysis time; when the time of `case` stops advancing, the analysis has stalled
    (with a too high scale factor the solver crawls with substeps of ~1e-7 s after the capacity)."""

    def __init__(self, log_path, case, pid, s, log=print):
        self.path, self.case, self.pid, self.s, self.log = log_path, case.upper(), pid, s, log
        self.sim_time = None              # last analysis time of the case
        self.stalled_at = None            # analysis time when it was cancelled for stalling
        self.reason = ""
        self._stop = None
        self._thread = None

    def start(self):
        import threading
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()
            self._thread.join(timeout=5)

    def _run(self):
        def head():
            try:
                with open(self.path, "rb") as f:
                    return f.read(400)
            except OSError:
                return b""

        start = time.time()
        try:
            offset = os.path.getsize(self.path)           # skip the log of the previous analysis
        except OSError:
            offset = 0
        old_head = head()
        active, buf = False, ""
        ref_sim, ref_wall = None, time.time()
        while not self._stop.wait(5.0):
            try:
                size = os.path.getsize(self.path)
                h = head()
                if size < offset or (h[:len(old_head)] != old_head):   # the new run rewrote the log: start over
                    offset, active, buf, old_head = 0, False, "", h
                    self.sim_time, ref_sim, ref_wall = None, None, time.time()
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except OSError:
                continue
            lines = (buf + chunk).split("\n")
            buf = lines.pop()                             # incomplete last line
            for ln in lines:
                t = ln.strip()
                if t.startswith("CASE:"):
                    active = t[5:].strip().upper() == self.case
                elif active and t.startswith("Curr"):
                    try:
                        self.sim_time = float(t.split()[-1])
                    except ValueError:
                        pass
            now = time.time()
            if self.sim_time is not None:
                if ref_sim is None or self.sim_time - ref_sim >= self.s["stall_min_progress_s"]:
                    ref_sim, ref_wall = self.sim_time, now
            reason = ""
            if self.sim_time is not None and now - ref_wall > 60 * self.s["stall_minutes"]:
                reason = (f"stalled: analysis time {self.sim_time:.4f} s advanced less than "
                          f"{self.s['stall_min_progress_s']} s in {self.s['stall_minutes']:g} min")
            elif now - start > 60 * self.s["max_analysis_minutes"]:
                reason = f"longer than {self.s['max_analysis_minutes']:g} min"
            if reason:
                self.reason = reason
                self.stalled_at = self.sim_time
                if self.s.get("cancel_stalled", True):
                    ok = press_cancel(self.pid)
                    self.log(f"    {reason} -> {'pressed Cancel in ETABS' if ok else 'Cancel button NOT found'}")
                    if ok:
                        return
                else:
                    self.log(f"    {reason} - cancel the analysis in ETABS (the tool waits)")
                ref_wall = now                            # do not repeat the message every 5 s


# =====================================================================================================
# ETABS MODEL: READ, CHANGE LOADS / SCALE FACTOR, RUN
# =====================================================================================================

def point_loads(m, node):
    """{pattern: [F1, F2, F3, M1, M2, M3]} of the point loads at `node` (global)."""
    r = m.PointObj.GetLoadForce(node, 0)
    out = {}
    if _ret(r) == 0:
        for i in range(r[0]):
            vals = [r[k][i] for k in range(5, 11)]
            prev = out.get(r[2][i], [0.0] * 6)
            out[r[2][i]] = [a + b for a, b in zip(prev, vals)]
    return out


def read_case_table(m):
    """(version, fields, rows) of the nonlinear direct-integration case table (read only)."""
    r = m.DatabaseTables.GetTableForEditingArray(NL_DIRHIST_TABLE, "", 0, [], 0, [])
    ver, fields, n, data, ret = r
    if ret != 0:
        raise PanelError(f"Could not read the table '{NL_DIRHIST_TABLE}'")
    fields = list(fields)
    w = len(fields)
    return ver, fields, [list(data[i * w:(i + 1) * w]) for i in range(n)]


def case_loads(m, case):
    """[{"type", "name", "function", "sf"}] of a nonlinear direct-integration case."""
    r = m.LoadCases.DirHistNonlinear.GetLoads(case)
    if _ret(r) != 0:
        raise PanelError(f"Load case '{case}' not found (or not a nonlinear direct-integration case)")
    return [{"type": r[1][i], "name": r[2][i], "function": r[3][i], "sf": r[4][i]} for i in range(r[0])]


def case_type(m, case):
    r = m.LoadCases.GetTypeOAPI_1(case)
    return r[0] if _ret(r) == 0 else None


def initial_case(m, case):
    """Name of the nonlinear case `case` starts from ('' = zero initial conditions)."""
    t = case_type(m, case)
    if t == 2:                                          # nonlinear static
        r = m.LoadCases.StaticNonlinear.GetInitialCase(case)
        name = r[0] if _ret(r) == 0 else ""
    elif t == 8:                                        # nonlinear direct integration: only in the table
        _, fields, rows = read_case_table(m)
        i_name, i_cond, i_case = fields.index("Name"), fields.index("InitialCond"), fields.index("NonlinCase")
        name = next((row[i_case] for row in rows if row[i_name] == case and row[i_cond] != "Zero"), "")
    else:
        name = ""
    return "" if not name or name.lower() == "none" else name


def case_chain(m, case):
    """The case and every case it starts from, in run order (initial case first)."""
    chain = [case]
    while True:
        prev = initial_case(m, chain[0])
        if not prev or prev in chain:
            return chain
        chain.insert(0, prev)


def model_info(m, s):
    """What the tool needs from the model, plus a list of problems (empty = OK). Only reads."""
    problems = []
    info = {"model_file": str(m.GetModelFilename(True) or ""), "units": m.GetPresentUnits(),
            "locked": bool(m.GetModelIsLocked())}
    joints = {}
    for p in m.PointObj.GetNameList()[1]:
        xyz = m.PointObj.GetCoordCartesian(p)
        lab = m.PointObj.GetLabelFromName(p)
        joints[p] = {"label": lab[0], "story": lab[1], "xyz": list(xyz[:3]),
                     "restraint": list(m.PointObj.GetRestraint(p)[0])}
    links = {}
    for k in m.LinkObj.GetNameList()[1]:
        pts = m.LinkObj.GetPoints(k)
        links[k] = {"points": [pts[0], pts[1]], "property": m.LinkObj.GetProperty(k)[0]}
    info["joints"], info["links"] = joints, links
    patterns = list(m.LoadPatterns.GetNameList()[1])
    cases = list(m.LoadCases.GetNameList()[1])
    info["patterns"], info["cases"] = patterns, cases

    for key in ("vertical_pattern", "horizontal_pattern"):
        if s[key] not in patterns:
            problems.append(f"Load pattern '{s[key]}' ({key}) is not in the model")
    for node in list(s["vertical_nodes"]) + [s["control_node"]]:
        if node not in joints:
            problems.append(f"Joint '{node}' is not in the model")
    if len(s["vertical_nodes"]) != len(s["vertical_shares"]) or abs(sum(s["vertical_shares"]) - 1) > 1e-6:
        problems.append("vertical_shares must have one value per vertical node and add up to 1")
    if s["case"] not in cases:
        problems.append(f"Load case '{s['case']}' is not in the model")
    elif case_type(m, s["case"]) != 8:
        problems.append(f"Load case '{s['case']}' is not a nonlinear direct-integration history")
    else:
        info["case_loads"] = case_loads(m, s["case"])
        hor = [ld for ld in info["case_loads"] if ld["type"] == "Load" and ld["name"] == s["horizontal_pattern"]]
        if not hor:
            problems.append(f"Case '{s['case']}' has no load of pattern '{s['horizontal_pattern']}'")
        info["case_chain"] = case_chain(m, s["case"])
        if len(info["case_chain"]) < 2:
            problems.append(f"Case '{s['case']}' does not start from a nonlinear case - the axial force of pattern "
                            f"'{s['vertical_pattern']}' would not be applied before the push")
    info["vertical_loads"] = {n: point_loads(m, n) for n in s["vertical_nodes"] if n in joints}
    info["problems"] = problems
    return info


def describe_model(info, s):
    """Readable summary of model_info for the log."""
    lines = [f"Model: {info['model_file']}", f"Joints: {len(info['joints'])}, links: {len(info['links'])}"]
    for k, v in info["links"].items():
        lines.append(f"    link {k}: {v['points'][0]} -> {v['points'][1]}  ({v['property']})")
    if "case_loads" in info:
        for ld in info["case_loads"]:
            lines.append(f"Case {s['case']}: {ld['type']} {ld['name']} x {ld['sf']:g}  (function {ld['function']})")
        lines.append("Run order: " + " -> ".join(info.get("case_chain", [])))
    for n, loads in info["vertical_loads"].items():
        lines.append(f"Point loads at {n}: " + ", ".join(f"{p}: " + " ".join(f"{v:g}" for v in vals)
                                                         for p, vals in loads.items()))
    lines += [f"PROBLEM: {p}" for p in info["problems"]] or ["Model check: OK"]
    return "\n".join(lines)


def unlock(m):
    if m.GetModelIsLocked():
        m.SetModelIsLocked(False)


def set_vertical_load(m, s, axial_kN):
    """Axial force (compression, kN) split between the vertical nodes in the vertical pattern.
    Replaces every point force of that pattern at those nodes."""
    unlock(m)
    for node, share in zip(s["vertical_nodes"], s["vertical_shares"]):
        r = m.PointObj.SetLoadForce(node, s["vertical_pattern"], [0.0, 0.0, -axial_kN * share, 0.0, 0.0, 0.0],
                                    True, "Global", 0)
        if _ret(r) != 0:
            raise PanelError(f"Could not set the vertical load at joint {node}")
        got = point_loads(m, node).get(s["vertical_pattern"], [0] * 6)[2]
        if abs(got + axial_kN * share) > 1e-6 * max(1.0, axial_kN):
            raise PanelError(f"Vertical load at joint {node} is {got:g} kN after setting it, "
                             f"expected {-axial_kN * share:g} kN")


def _fmt(x):
    return f"{x:.10g}"


SOLUTION_SCHEMES = ("Iterative Only", "Iterative Events", "Events Only")
# fields ETABS may fill in by itself when the solution scheme changes (blank -> its defaults)
SCHEME_FIELDS = ("EventTol", "MaxEvents", "StopAnalysis", "MinEvStepSz", "MaxNullEvStp", "UseCorrStep",
                 "MaxSize", "MinSize", "MaxCSIters", "MaxNRIters", "IterTol", "LineSearch", "MaxSearches",
                 "SearchTol", "SearchFact")
HHT_DERIVED = ("Gamma", "Beta")


def _same_number(a, b):
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return False


def case_scheme(m, s):
    """Solution scheme of the pushover case (from the case table)."""
    _, fields, rows = read_case_table(m)
    i_name, i_sch = fields.index("Name"), fields.index("SolScheme")
    return next((r[i_sch] for r in rows if r[i_name] == s["case"] and r[i_sch]), "")


def solver_settings(m, s):
    """{"SolScheme": ..., <solver field>: value} of the pushover case, as text from the case table."""
    _, fields, rows = read_case_table(m)
    i_name, i_sch = fields.index("Name"), fields.index("SolScheme")
    row = next((r for r in rows if r[i_name] == s["case"] and r[i_sch]), None)
    if row is None:
        return {}
    return {k: row[fields.index(k)] for k in ("SolScheme",) + SCHEME_FIELDS if k in fields}


def set_scale_factor(m, s, sf, log=print, scheme=None, profiles=None, solver=None):
    """Scale factor of the horizontal pattern in the pushover case and, if given, its solution scheme
    ('Iterative Only', 'Iterative Events', 'Events Only'), by editing the case table in one import. Nothing is
    imported when both are already set. The model is saved first, as the API help asks for ApplyEditedTables.

    profiles {scheme: solver settings}: ETABS blanks the settings of the other schemes when the scheme changes
    (e.g. line search 'Yes' -> '' -> 'No' after Events Only and back). Before the scheme is changed, the current
    settings are stored under the current scheme; when a scheme is set that has stored settings, they are written
    back, so the user's iterative settings survive the switch to Events Only and back.
    Afterwards the table is read back: only the scale factor and the solver settings may have changed."""
    case, pattern = s["case"], s["horizontal_pattern"]
    if scheme and scheme not in SOLUTION_SCHEMES:
        raise PanelError(f"Unknown solution scheme '{scheme}' (use one of {', '.join(SOLUTION_SCHEMES)})")
    current = [ld["sf"] for ld in case_loads(m, case) if ld["type"] == "Load" and ld["name"] == pattern]
    sf_ok = bool(current) and all(abs(c - sf) <= 1e-9 * max(1.0, abs(sf)) for c in current)
    now = solver_settings(m, s)
    target = None
    if scheme and now.get("SolScheme") != scheme:
        if profiles is not None and now.get("SolScheme"):
            profiles.setdefault(now["SolScheme"], dict(now))
        target = dict(profiles[scheme]) if profiles and scheme in profiles else {"SolScheme": scheme}
    if solver:                                       # explicit solver settings (e.g. to restore the original ones)
        target = dict(solver)
        if all(str(now.get(k, "")) == str(v) or _same_number(now.get(k), v) for k, v in target.items()):
            target = None
    if sf_ok and target is None:
        return
    unlock(m)
    ver, fields, rows = read_case_table(m)
    i_name, i_type, i_load, i_sf, i_sch = (fields.index(k) for k in ("Name", "LoadType", "LoadName", "LoadSF",
                                                                    "SolScheme"))
    before = [list(r) for r in rows]
    hits = 0
    for row in rows:
        if row[i_name] == case and row[i_type] == "Load" and row[i_load] == pattern:
            row[i_sf] = _fmt(sf)
            hits += 1
        if target and row[i_name] == case and row[i_sch]:
            for k, v in target.items():
                if k in fields:
                    row[fields.index(k)] = v
    if not hits:
        raise PanelError(f"Case '{case}' has no row with load '{pattern}' in '{NL_DIRHIST_TABLE}'")
    if _ret(m.File.Save()) != 0:
        raise PanelError("Could not save the model before editing the load case table")
    flat = [v if v is not None else "" for row in rows for v in row]
    r = m.DatabaseTables.SetTableForEditingArray(NL_DIRHIST_TABLE, ver, fields, len(rows), flat)
    if _ret(r) != 0:
        raise PanelError("SetTableForEditingArray failed for the load case table")
    r = m.DatabaseTables.ApplyEditedTables(True, 0, 0, 0, 0, "")
    n_fatal, n_err, n_warn, _n_info, import_log, ret = r
    if ret != 0 or n_fatal or n_err:
        raise PanelError(f"ETABS could not import the edited load case table (fatal {n_fatal}, errors {n_err}).\n"
                         f"{import_log}\nThe help recommends closing the model WITHOUT saving and reopening it.")
    if n_warn:
        log(f"    table import warnings: {n_warn}\n{import_log}")
    got = [ld["sf"] for ld in case_loads(m, case) if ld["type"] == "Load" and ld["name"] == pattern]
    if not got or any(abs(g - sf) > 1e-6 * max(1.0, abs(sf)) for g in got):
        raise PanelError(f"Scale factor of '{pattern}' in '{case}' is {got} after the edit, expected {sf:g}")
    # nothing else in the table may have changed
    _, fields2, rows2 = read_case_table(m)
    if fields2 != fields or len(rows2) != len(before):
        raise PanelError("The load case table changed its layout after the edit - check the model")
    problems, filled = [], []
    for r1, r2 in zip(before, rows2):
        for j, (a, b) in enumerate(zip(r1, r2)):
            if a == b or _same_number(a, b):
                continue
            if r1[i_name] == case and (j == i_sf or (target and j == i_sch)):
                continue
            if fields[j] in HHT_DERIVED and r2[fields.index("IntType")] == "Hilber-Hughes-Taylor":
                continue                     # ETABS fills gamma / beta from alpha on import (0 -> 0.5 / 0.25)
            if target and r1[i_name] == case and fields[j] in SCHEME_FIELDS:
                filled.append(f"{fields[j]} '{a}' -> '{b}'")    # settings of the new solution scheme
                continue
            problems.append(f"{r1[i_name]}: {fields[j]} '{a}' -> '{b}'")
    if problems:
        raise PanelError("The load case table changed in more than the scale factor - check the model:\n    "
                         + "\n    ".join(problems))
    if target:
        after = solver_settings(m, s)
        if after.get("SolScheme") != target["SolScheme"]:
            raise PanelError(f"Solution scheme of '{case}' is '{after.get('SolScheme')}' after the edit, "
                             f"expected '{target['SolScheme']}'")
        differ = [f"{k} = '{after.get(k)}' instead of '{v}'" for k, v in target.items()
                  if k != "SolScheme" and str(after.get(k, "")) != str(v) and not _same_number(after.get(k), v)]
        log(f"    solution scheme '{target['SolScheme']}'" + (" with the stored settings" if len(target) > 1 else "")
            + (": " + ", ".join(filled) if filled else ""))
        if differ:
            log("    WARNING: ETABS did not keep " + ", ".join(differ))


def run_flags(m):
    r = m.Analyze.GetRunCaseFlag()
    return dict(zip(r[1], r[2]))


def set_run_flags(m, flags):
    for case, run in flags.items():
        m.Analyze.SetRunCaseFlag(case, bool(run), False)


def case_status(m, case):
    """1 not run, 2 could not start, 3 not finished, 4 finished."""
    r = m.Analyze.GetCaseStatus()
    return dict(zip(r[1], r[2])).get(case, 0)


STATUS_TEXT = {0: "unknown", 1: "not run", 2: "could not start", 3: "not finished", 4: "finished"}


# =====================================================================================================
# ETABS RESULTS PER STEP
# =====================================================================================================

def _select_step_by_step(m, case):
    setup = m.Results.Setup
    setup.DeselectAllCasesAndCombosForOutput()
    if _ret(setup.SetCaseSelectedForOutput(case)) != 0:
        raise PanelError(f"Could not select case '{case}' for output")
    setup.SetOptionDirectHist(2)                        # 2 = step-by-step


def _key(t):
    return round(float(t), 6)


def _by_step(steps, step_values, columns):
    """Align result columns to the master step list (NaN where a step is missing)."""
    index = {}
    for i, t in enumerate(step_values):
        index.setdefault(_key(t), i)
    out = {}
    for name, col in columns.items():
        arr = np.full(len(steps), np.nan)
        for j, t in enumerate(steps):
            i = index.get(_key(t))
            if i is not None:
                arr[j] = col[i]
        out[name] = arr
    return out


def extract_results(m, case, joints, links):
    """Every result of `case` per step, in kN and m. joints: {name: {..., "restraint"}}, links: {name: {"points"}}"""
    units = m.GetPresentUnits()
    try:
        if units != KN_M_C:
            m.SetPresentUnits(KN_M_C)
        _select_step_by_step(m, case)
        b = m.Results.BaseReact()
        if _ret(b) != 0 or not b[0]:
            raise PanelError(f"No results for case '{case}' (did the analysis run?)")
        steps = [float(t) for t in b[3]]
        order = np.argsort(steps, kind="stable")
        steps = [steps[i] for i in order]
        base = {c: [b[4 + k][i] for i in order] for k, c in enumerate(BASE_COMPONENTS)}

        out = {"steps": steps, "base": {c: list(map(float, v)) for c, v in base.items()},
               "joints": {}, "reactions": {}, "links": {}}
        for name, j in joints.items():
            r = m.Results.JointDispl(name, 0)
            if _ret(r) == 0 and r[0]:
                cols = _by_step(steps, r[5], {c: r[6 + k] for k, c in enumerate(DISP_COMPONENTS)})
                out["joints"][name] = {c: v.tolist() for c, v in cols.items()}
            if any(j.get("restraint", [])):
                r = m.Results.JointReact(name, 0)
                if _ret(r) == 0 and r[0]:
                    cols = _by_step(steps, r[5], {c: r[6 + k] for k, c in enumerate(REACT_COMPONENTS)})
                    out["reactions"][name] = {c: v.tolist() for c, v in cols.items()}
        for name, lk in links.items():
            entry = {"points": lk["points"], "property": lk.get("property", "")}
            r = m.Results.LinkDeformation(name, 0)
            if _ret(r) == 0 and r[0]:
                cols = _by_step(steps, r[5], {c: r[6 + k] for k, c in enumerate(DISP_COMPONENTS)})
                entry["deformation"] = {c: v.tolist() for c, v in cols.items()}
            r = m.Results.LinkForce(name, 0)
            if _ret(r) == 0 and r[0]:
                pts = list(r[3])
                for end, pt in (("I", lk["points"][0]), ("J", lk["points"][1])):
                    sel = [i for i, p in enumerate(pts) if p == pt]
                    if not sel:                     # point element names differ: ends alternate I, J
                        sel = list(range(0 if end == "I" else 1, len(pts), 2))
                    cols = _by_step(steps, [r[6][i] for i in sel],
                                    {c: [r[7 + k][i] for i in sel] for k, c in enumerate(LINK_FORCE_COMPONENTS)})
                    entry["force_" + end] = {c: v.tolist() for c, v in cols.items()}
            out["links"][name] = entry
        return out
    finally:
        if units != KN_M_C:
            m.SetPresentUnits(units)


# =====================================================================================================
# THE RUN: ALL AXIAL LOADS
# =====================================================================================================

def level_tag(axial_kN):
    return f"{axial_kN:g}kN"


def job_name(axial_kN):
    """Job name in the Abaqus database style: axial force in N at the end (ETABS_PANEL_360000)."""
    return f"ETABS_PANEL_{int(round(axial_kN * 1000))}"


def valid_steps(s, results):
    """(number of valid steps, reason) - the results end before the first step that
       - moves the control node more than jump_mm AND more than jump_ratio x every earlier step (a spurious jump,
         the same rule as the collapse detection of Damage_states_GUI.py), or
       - takes the vertical force (quantity "V") more than vertical_tolerance away from its value at step 0
         (the axial force is constant in this test; large swings are numerical chatter).
    The step before such a step is the last valid one."""
    d = np.asarray(results["joints"][s["control_node"]][s["control_dof"]], dtype=float) * 1000.0
    n = len(d)
    end, reason = n, ""
    inc = np.abs(np.diff(np.nan_to_num(d)))
    running = 0.0
    for i, step in enumerate(inc):
        if step > s["jump_mm"] and step > s["jump_ratio"] * running:
            end, reason = i + 1, (f"jump of {step:.1f} mm in one step at t = {results['steps'][i + 1]:g} s "
                                  f"(largest earlier step {running:.2f} mm)")
            break
        running = max(running, step)
    qv = next((q for q in s.get("quantities", []) if q["key"] == "V"), None)
    tol = s.get("vertical_tolerance")
    if qv and tol:
        try:
            v = Level({"axial_kN": 0, "steps": results["steps"], **results}).evaluate(qv["expr"])
            v0 = v[0]
            bad = np.flatnonzero(np.abs(v - v0) > tol * max(abs(v0), 1e-9))
            if bad.size and bad[0] < end:
                end, reason = int(bad[0]), (f"vertical force {v[bad[0]]:.1f} kN at t = {results['steps'][bad[0]]:g} s "
                                            f"differs more than {tol:.0%} from {v0:.1f} kN")
        except PanelError:
            pass
    return max(end, 1), reason


def verdict(s, results, stalled):
    """('ok' / 'too_high' / 'too_low', max |displacement| of the control node in the valid data (m),
    valid steps, reason). The window decides; an analysis that stopped (stalled, not finished or invalid data)
    below the window was loaded too fast."""
    if not results:
        return "too_high", float("nan"), 0, "no results"
    n, reason = valid_steps(s, results)
    d = np.asarray(results["joints"][s["control_node"]][s["control_dof"]], dtype=float)[:n]
    dmax = float(np.nanmax(np.abs(d))) if d.size and np.isfinite(d).any() else 0.0
    if dmax > s["disp_max"]:
        return "too_high", dmax, n, reason
    if dmax < s["disp_min"]:
        return ("too_high" if (stalled or reason) else "too_low"), dmax, n, reason
    return "ok", dmax, n, reason


def ramp_info(m, s):
    """Horizontal load function of the case: (times, values, time factor, arrival, case duration)."""
    _, fields, rows = read_case_table(m)
    get = {f: i for i, f in enumerate(fields)}
    row = next(r for r in rows if r[get["Name"]] == s["case"] and r[get["LoadName"]] == s["horizontal_pattern"])

    def num(key, default):
        try:
            return float(row[get[key]])
        except (KeyError, ValueError, TypeError):
            return default

    r = m.Func.GetValues(row[get["Function"]])
    times, values = np.asarray(r[1], dtype=float), np.asarray(r[2], dtype=float)
    return times, values, num("TimeFactor", 1.0), num("ArrivalTime", 0.0), num("NumSteps", 0) * num("StepSize", 0)


def ramp_at(ramp, t):
    times, values, factor, arrival, _ = ramp
    return float(np.interp((t - arrival) / (factor or 1.0), times, values))


def next_scale_factor(attempts, s, ramp=None):
    """Next scale factor.
    - Too high and the valid curve passed the middle of the window (geometric mean of disp_min and disp_max) at
      time t: the factor that applies the force of time t at the end of the ramp (the runaway then starts later).
    - Stalled below the window: the force at the stall is about the capacity -> reach that force x
      (1 + stall_overshoot) at the end of the ramp.
    - Otherwise bracketing: geometric mean of the largest 'too_low' and the smallest 'too_high' factor, or the last
      factor x factor_up / factor_down.
    The result always stays inside the bracket."""
    current = [a for a in attempts if not a.get("superseded")]      # retried with another solution scheme
    lows = [a["scale_factor"] for a in current if a["verdict"] == "too_low"]
    highs = [a["scale_factor"] for a in current if a["verdict"] == "too_high"]
    lo, hi = (max(lows) if lows else None), (min(highs) if highs else None)
    last = attempts[-1]
    guess = None
    end = ramp_at(ramp, ramp[4]) if ramp is not None else 0.0
    if last["verdict"] == "too_high" and end > 0:
        if last.get("time_at_target_s") is not None:
            guess = last["scale_factor"] * ramp_at(ramp, last["time_at_target_s"]) / end
        elif last.get("stalled_at_s"):
            guess = last["scale_factor"] * ramp_at(ramp, last["stalled_at_s"]) * (1 + s["stall_overshoot"]) / end
    if guess is None:
        if lo is not None and hi is not None:
            guess = math.sqrt(lo * hi)
        elif hi is not None:
            guess = hi * s["factor_down"]
        else:
            guess = lo * s["factor_up"]
    if hi is not None and guess >= hi * 0.995:
        guess = math.sqrt(lo * hi) if lo is not None else hi * s["factor_down"]
    if lo is not None and guess <= lo * 1.005:
        guess = math.sqrt(lo * hi) if hi is not None else lo * s["factor_up"]
    return guess


def time_at_displacement(s, results, n_valid, target):
    """First time (s) the control node reaches |target| (m) within the valid steps, or None."""
    d = np.abs(np.asarray(results["joints"][s["control_node"]][s["control_dof"]], dtype=float)[:n_valid])
    hits = np.flatnonzero(np.nan_to_num(d) >= target)
    return float(results["steps"][hits[0]]) if hits.size else None


def new_run_dir(s):
    name = s.get("run_name") or time.strftime("Run %Y-%m-%d %H-%M")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")
    return os.path.join(resolve(s["runs_folder"]), name)


def model_log_path(m):
    """The ETABS analysis log (.LOG next to the model file)."""
    return os.path.splitext(str(m.GetModelFilename(True) or ""))[0] + ".LOG"


def _best_attempt(attempts, s):
    """Index of the attempt to keep: in the window, else the largest displacement not above disp_max, else last."""
    ok = [i for i, a in enumerate(attempts) if a["verdict"] == "ok" and a["saved"]]
    if ok:
        return ok[-1]
    below = [i for i, a in enumerate(attempts) if a["saved"] and np.isfinite(a["max_control_disp_m"])
             and a["max_control_disp_m"] <= s["disp_max"]]
    if below:
        return max(below, key=lambda i: attempts[i]["max_control_disp_m"])
    saved = [i for i, a in enumerate(attempts) if a["saved"]]
    return saved[-1] if saved else None


def run_levels(s, run_dir, log=print, stop=None, SapModel=None, pid=None):
    """
    Run the pushover for every level with "run": True and save the results to run_dir:
        run.json                          settings, model description, per level all attempts
        run_log.txt                       the log of every run in this folder
        levels/<axial>kN.json             every result per step of the kept analysis of each level
        levels/attempts/<axial>kN - attempt <n> SF <sf>.json   every analysis (also the rejected ones)
    A level already in run_dir is run again (its file is replaced). stop: threading.Event - stop after the
    current analysis. Returns the path of run.json.
    """
    makedirs(run_dir)
    log_file = open(long_path(os.path.join(run_dir, "run_log.txt")), "a", encoding="utf-8")

    def say(text):
        log(text)
        log_file.write(text + "\n")
        log_file.flush()

    try:
        if SapModel is None:
            m, pid = connect_pid(s["etabs_instance"])
        else:
            m = SapModel
        say(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        info = model_info(m, s)
        say(describe_model(info, s))
        if info["problems"]:
            raise PanelError("The model does not fit the settings:\n" + "\n".join(info["problems"]))

        run_file = os.path.join(run_dir, "run.json")
        run = load_json(run_file) if os.path.isfile(long_path(run_file)) else {
            "format": "panel-pushover-run", "version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "levels": {}}
        run["settings"] = {k: v for k, v in s.items() if k not in ("quantities",)}
        run["model"] = {k: info[k] for k in ("model_file", "joints", "links", "case_chain", "case_loads")}
        save_json(run, run_file)

        flags_before = run_flags(m)
        chain = info["case_chain"]
        ramp = ramp_info(m, s)
        # solver settings per scheme: the model's settings are kept and written back after a fallback
        start_solver = solver_settings(m, s)
        profiles = dict(run.get("solver_profiles", {}))
        profiles.setdefault(start_solver["SolScheme"], start_solver)
        run["solver_profiles"] = profiles
        say("Solver settings at the start: " + ", ".join(f"{k} {v}" for k, v in start_solver.items() if v))
        log_path = model_log_path(m)
        todo = [lv for lv in s["levels"] if lv.get("run", True)]
        try:
            for n_level, lv in enumerate(todo, 1):
                axial = float(lv["axial_kN"])
                sf = float(lv["scale_factor"])
                tag = level_tag(axial)
                attempts, kept = [], {}
                primary = lv.get("scheme") or s.get("solution_scheme") or start_solver["SolScheme"]
                scheme = primary
                fallbacks = [f for f in (s.get("fallback_schemes") or []) if f in SOLUTION_SCHEMES] \
                    if s.get("fallback_on_stall") else []
                tried_here = {primary}            # schemes tried at the current scale factor
                good_scheme = None                # last scheme that gave a complete, valid analysis
                max_attempts = s["max_attempts"] if s["auto_adjust"] else 1 + len(fallbacks)
                for attempt in range(1, max_attempts + 1):
                    if stop is not None and stop.is_set():
                        say("Stopped by the user.")
                        return run_file
                    t0 = time.time()
                    set_vertical_load(m, s, axial)
                    set_scale_factor(m, s, sf, say, scheme, profiles)
                    used_scheme = case_scheme(m, s)
                    say(f"\n[{n_level}/{len(todo)}] axial {axial:g} kN, scale factor {sf:.5g}, {used_scheme} "
                        f"(attempt {attempt})  {time.strftime('%H:%M:%S')}")
                    if s["run_only_needed_cases"]:            # after the table import, which resets the flags
                        set_run_flags(m, {c: (c in chain) for c in run_flags(m)})
                    watch = StallWatch(log_path, s["case"], pid, s, say) if pid else None
                    if watch:
                        watch.start()
                    try:
                        ret = m.Analyze.RunAnalysis()
                    finally:
                        if watch:
                            watch.stop()
                    status = case_status(m, s["case"])
                    seconds = time.time() - t0
                    stalled = bool(watch and watch.reason)
                    if _ret(ret) != 0:
                        say(f"    RunAnalysis returned {_ret(ret)}, case status: {STATUS_TEXT.get(status)}")
                    try:
                        results = extract_results(m, s["case"], info["joints"], info["links"])
                    except PanelError as e:
                        say(f"    {e}")
                        results = None
                    v, dmax, n_valid, why = verdict(s, results, stalled or status != 4)
                    a = {"scale_factor": sf, "scheme": used_scheme, "verdict": v, "max_control_disp_m": dmax,
                         "status": STATUS_TEXT.get(status), "stalled": watch.reason if stalled else "",
                         "stalled_at_s": watch.stalled_at if stalled else None,
                         "last_time_s": results["steps"][-1] if results else None,
                         "steps": len(results["steps"]) if results else 0, "valid_steps": n_valid,
                         "valid_end_s": results["steps"][n_valid - 1] if results else None, "invalid_reason": why,
                         "time_at_target_s": time_at_displacement(s, results, n_valid, math.sqrt(
                             s["disp_min"] * s["disp_max"])) if results else None,
                         "seconds": round(seconds, 1), "saved": bool(results)}
                    attempts.append(a)
                    if results:
                        path = os.path.join(run_dir, "levels", "attempts", f"{tag} - attempt {attempt} SF {sf:.5g}.json")
                        kept[len(attempts) - 1] = path
                        save_json({"format": "panel-pushover-level", "version": 1, "axial_kN": axial,
                                   "scale_factor": sf, "case": s["case"], "status": a["status"], "attempt": a,
                                   "valid_steps": n_valid, "units": "kN, m", "job": job_name(axial), **results},
                                  path, indent=None)
                    say(f"    {a['status']}{' (cancelled: ' + a['stalled'] + ')' if stalled else ''}, "
                        f"{a['steps']} steps to t = {a['last_time_s']}"
                        + (f"; valid data to t = {a['valid_end_s']:g} s ({why})" if why else "")
                        + f"; max |{s['control_dof']}| of {s['control_node']} = {dmax:.4g} m -> {v}  ({seconds:.0f} s)")
                    if v == "ok":
                        break
                    failed = stalled or status != 4 or bool(why)
                    if not failed:
                        good_scheme = used_scheme
                    below = not (dmax >= s["disp_min"])          # also NaN
                    nxt = next((f for f in fallbacks if f not in tried_here and f != used_scheme), None)
                    if failed and below and nxt:
                        # as done by hand: same scale factor, another solution scheme
                        a["superseded"] = True
                        scheme = nxt
                        tried_here.add(nxt)
                        say(f"    -> same scale factor again with solution scheme '{nxt}'")
                        continue
                    if not s["auto_adjust"]:
                        break
                    sf = next_scale_factor(attempts, s, ramp)
                    scheme = good_scheme or primary               # the scheme that worked for this force
                    tried_here = {scheme}
                best = _best_attempt(attempts, s)
                accepted = best is not None and attempts[best]["verdict"] == "ok"
                if best is not None:
                    data = load_json(kept[best])
                    data.update({"in_window": accepted, "attempts": attempts, "kept_attempt": best + 1,
                                 "scheme": attempts[best]["scheme"]})
                    data.pop("attempt", None)
                    save_json(data, os.path.join(run_dir, "levels", tag + ".json"), indent=None)
                run["levels"][tag] = {"axial_kN": axial,
                                      "scale_factor": attempts[best]["scale_factor"] if best is not None else None,
                                      "in_window": accepted, "kept_attempt": None if best is None else best + 1,
                                      "attempts": attempts, "saved": best is not None}
                run["modified"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_json(run, run_file)
                if not accepted:
                    say(f"    WARNING: no scale factor in {len(attempts)} attempt(s) kept the displacement between "
                        f"{s['disp_min']} and {s['disp_max']} m"
                        + (f" - attempt {best + 1} is kept" if best is not None else " - nothing to keep"))
        finally:
            if s.get("restore_run_flags", True):
                try:                                   # solver settings as at the start (loads / SF stay)
                    cur = [ld["sf"] for ld in case_loads(m, s["case"]) if ld["name"] == s["horizontal_pattern"]]
                    if cur:
                        set_scale_factor(m, s, cur[0], say, solver=start_solver)
                except Exception as e:
                    say(f"Could not restore the solver settings: {e}")
                try:                                   # after the table import, which switches the flags on
                    set_run_flags(m, flags_before)
                except Exception:
                    pass
        say("\nAll levels done.")
        return run_file
    finally:
        log_file.close()


# =====================================================================================================
# SAVED RUNS AND QUANTITIES
# =====================================================================================================

REF_RE = re.compile(r"\{([^{}]*)\}")
EXPR_FUNCS = {"abs": np.abs, "sqrt": np.sqrt, "maximum": np.maximum, "minimum": np.minimum, "np": np}


class Level:
    """Results of one axial load. Reference syntax in expressions:
        {J1198:U2}  joint displacement    {R1199:F3} joint reaction    {BASE:FY} base reaction
        {K3:U1}     link deformation      {K3:P} link force at end I   {K3:P_J} at end J      {time}"""

    def __init__(self, data, path="", valid_only=True):
        self.data = data
        self.path = path
        self.axial_kN = float(data["axial_kN"])
        steps = np.asarray(data["steps"], dtype=float)
        self.n_all = len(steps)
        self.n = int(data.get("valid_steps") or self.n_all) if valid_only else self.n_all
        self.time = steps[:self.n]               # only the valid steps (see valid_steps); data keeps everything

    def ref(self, text):
        text = text.strip()
        if text.lower() == "time":
            return self.time
        if ":" not in text:
            raise PanelError(f"Reference {{{text}}} must look like {{J1198:U2}}, {{K3:U1}} or {{BASE:FY}}")
        obj, comp = (t.strip() for t in text.split(":", 1))
        d = self.data
        try:
            if obj.upper() == "BASE":
                vals = d["base"][comp.upper()]
            elif obj[:1].upper() == "J":
                vals = d["joints"][obj[1:]][comp.upper()]
            elif obj[:1].upper() == "R":
                vals = d["reactions"][obj[1:]][comp.upper()]
            else:
                link = d["links"][obj]
                c = comp.upper()
                if c in DISP_COMPONENTS:
                    vals = link["deformation"][c]
                elif c.endswith("_J"):
                    vals = link["force_J"][c[:-2]]
                else:
                    vals = link["force_I"][c[:-2] if c.endswith("_I") else c]
        except KeyError:
            raise PanelError(f"{{{text}}} is not in the results of {level_tag(self.axial_kN)}") from None
        return np.asarray(vals, dtype=float)[:self.n]

    def evaluate(self, expr):
        names = {}

        def sub(mt):
            key = f"_r{len(names)}"
            names[key] = self.ref(mt.group(1))
            return key

        code = REF_RE.sub(sub, expr)
        try:
            val = eval(code, {"__builtins__": {}}, {**EXPR_FUNCS, **names})  # noqa: S307 - user's own formulas
        except Exception as e:
            raise PanelError(f"Cannot evaluate '{expr}': {e}") from None
        return np.broadcast_to(np.asarray(val, dtype=float), self.time.shape).copy()


def quantity_map(s):
    return {q["key"]: q for q in s["quantities"]}


def quantity(level, q):
    """Values of quantity q in report units (mm or kN)."""
    v = level.evaluate(q["expr"])
    return v * 1000.0 if q.get("kind") == "disp" else v


def load_run(run_dir):
    """(run.json content, [Level sorted by axial load])."""
    run_file = os.path.join(run_dir, "run.json")
    if not os.path.isfile(long_path(run_file)):
        raise PanelError(f"{run_dir} is not a saved run (no run.json)")
    run = load_json(run_file)
    levels = []
    folder = os.path.join(run_dir, "levels")
    for name in sorted(os.listdir(long_path(folder))) if os.path.isdir(long_path(folder)) else []:
        if name.endswith(".json"):
            p = os.path.join(folder, name)
            levels.append(Level(load_json(p), p))
    levels.sort(key=lambda lv: lv.axial_kN)
    if not levels:
        raise PanelError(f"{run_dir} has no saved levels yet")
    return run, levels


def list_runs(runs_folder):
    folder = resolve(runs_folder)
    if not os.path.isdir(folder):
        return []
    return sorted((n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n, "run.json"))),
                  key=lambda n: os.path.getmtime(os.path.join(folder, n, "run.json")), reverse=True)


# =====================================================================================================
# DAMAGE STATES
# =====================================================================================================

def check_values(level, check, qmap):
    """Values of a check (mm), positive = damaging direction."""
    q = qmap.get(check["quantity"])
    if q is None:
        raise PanelError(f"Check '{check['name']}': unknown quantity '{check['quantity']}'")
    v = quantity(level, q)
    if check.get("relative_to_start"):
        v = v - v[0]
    sense = check.get("sense", "+")
    if sense == "-":
        return -v
    if sense == "abs":
        return np.abs(v)
    return v


def _limits(check):
    lim = check["limits"]
    return [lim.get(k) for k in DS_NAMES]


def ds_codes(values, limits, running=True):
    """Damage state per step: 0 ND, 1 LD, 2 SD, 3 NC (limits = upper bounds of ND, LD, SD, NC)."""
    v = np.nan_to_num(values, nan=-np.inf)
    codes = np.zeros(v.shape, dtype=int)
    for k, lim in enumerate(limits[:3], 1):
        if lim is not None:
            codes[v >= lim] = k
    return np.maximum.accumulate(codes) if running else codes


def _crossing(x, v, threshold):
    """First x where v reaches threshold (linear interpolation), or None."""
    hits = np.flatnonzero(np.nan_to_num(v, nan=-np.inf) >= threshold)
    if not hits.size:
        return None
    i = hits[0]
    if i == 0:
        return float(x[0])
    v0, v1 = v[i - 1], v[i]
    f = 0.0 if v1 == v0 else (threshold - v0) / (v1 - v0)
    return float(x[i - 1] + f * (x[i] - x[i - 1]))


def crossings(drift, values, limits):
    """{state: drift (mm) where the value first reaches that state's limit, or None}."""
    return {name: (_crossing(drift, values, lim) if lim is not None else None)
            for name, lim in zip(DS_NAMES, limits)}


def ends_from_crossings(cross_list, end_of_data):
    """Drift where each damage state ENDS, as in the Abaqus damage-state workbook, from the limit crossings of
    one or more checks (the panel leaves a state as soon as any check leaves it):
    ND ends at the first ND-limit crossing, LD at the first LD-limit crossing, SD at the first SD-limit crossing,
    NC at the first NC-limit crossing or at the end of the data. The state reached last lasts to the end of the
    data; the states after it are None (not reached)."""
    ends, prev = {}, 0.0
    for k, name in enumerate(DS_NAMES):
        hits = [c[name] for c in cross_list if c[name] is not None]
        if hits:
            ends[name] = max(min(hits), prev)          # a state cannot end before the previous one
            prev = ends[name]
            continue
        ends[name] = max(end_of_data, prev)            # never left: lasts to the end of the analysis
        for later in DS_NAMES[k + 1:]:
            ends[later] = None
        break
    return ends


def _end_of_data(drift):
    finite = np.flatnonzero(np.isfinite(drift))
    return float(drift[finite[-1]]) if finite.size else 0.0


def evaluate_level(level, criteria_set, s):
    """Damage states of one level: per check and combined (checks with use_for_DS)."""
    qmap = quantity_map(s)
    drift = quantity(level, qmap["drift"])
    eod = _end_of_data(drift)
    checks = []
    for ch in criteria_set["checks"]:
        v = check_values(level, ch, qmap)
        lim = _limits(ch)
        cross = crossings(drift, v, lim)
        ends = ends_from_crossings([cross], eod)
        notes = [f"{name} limit {lim[k]:g} mm not reached (max {np.nanmax(v):.3g} mm): {name} lasts to the end "
                 f"of the analysis" for k, name in enumerate(DS_NAMES[:3])
                 if lim[k] is not None and cross[name] is None and ends[name] is not None]
        checks.append({"check": ch, "values": v, "codes": ds_codes(v, lim, s["running_damage"]),
                       "crossings": cross, "ends": ends, "notes": notes})
    used = [c for c in checks if c["check"].get("use_for_DS", True)]
    if used:
        codes = np.max([c["codes"] for c in used], axis=0)
        ends = ends_from_crossings([c["crossings"] for c in used], eod)
    else:
        codes, ends = np.zeros(drift.shape, dtype=int), {k: None for k in DS_NAMES}
    return {"drift": drift, "end_of_data": eod, "checks": checks, "combined": {"codes": codes, "ends": ends}}


def value_at_drift(drift, values, target):
    """Value at the first step where drift reaches target (linear interpolation)."""
    if target is None:
        return None
    hits = np.flatnonzero(np.nan_to_num(drift, nan=-np.inf) >= target - 1e-9)
    if not hits.size:
        return None
    i = hits[0]
    if i == 0:
        return float(values[0])
    d0, d1 = drift[i - 1], drift[i]
    f = 0.0 if d1 == d0 else (target - d0) / (d1 - d0)
    return float(values[i - 1] + f * (values[i] - values[i - 1]))


# =====================================================================================================
# ABAQUS
# =====================================================================================================

def read_abaqus_table(path):
    """One Abaqus result .txt -> {column: array} (comma separated, every line ends with a comma)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
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


class AbaqusDB:
    """<root>/<SET>/<JOB>/*.txt (the Panel_results database)."""

    def __init__(self, root):
        self.root = root
        self.mapping = {}
        self._cache = {}
        if not os.path.isdir(root):
            raise PanelError(f"Abaqus database not found: {root}")
        for set_name in os.listdir(root):
            sp = os.path.join(root, set_name)
            if not os.path.isdir(sp):
                continue
            for job in os.listdir(sp):
                jp = os.path.join(sp, job)
                if os.path.isdir(jp):
                    txts = sorted(f for f in os.listdir(jp) if f.lower().endswith(".txt"))
                    if txts:
                        self.mapping.setdefault(set_name, {})[job] = os.path.join(jp, txts[0])

    def jobs(self, family):
        """{axial force in N: job} of a family."""
        out = {}
        for jobs in self.mapping.values():
            for job in jobs:
                fam, _, num = job.rpartition("_")
                if fam.upper() == family.upper() and num.isdigit():
                    out[int(num)] = job
        return dict(sorted(out.items()))

    def column(self, set_name, job, col):
        path = self.mapping.get(set_name, {}).get(job)
        if path is None:
            raise PanelError(f"Abaqus: no {set_name} / {job}")
        if path not in self._cache:
            self._cache[path] = read_abaqus_table(path)
        table = self._cache[path]
        if col not in table:
            raise PanelError(f"Abaqus: column {col} not in {set_name} / {job}")
        return table[col]

    def evaluate(self, expr, job):
        names = {}

        def sub(mt):
            set_name, _, col = mt.group(1).rpartition(":")
            key = f"_r{len(names)}"
            names[key] = self.column(set_name.strip(), job, col.strip())
            return key

        code = REF_RE.sub(sub, expr)
        n = min((len(v) for v in names.values()), default=0)
        names = {k: v[:n] for k, v in names.items()}
        return np.asarray(eval(code, {"__builtins__": {}}, {**EXPR_FUNCS, **names}), dtype=float)  # noqa: S307


def match_axial(axial_kN, options_N, tolerance):
    """Abaqus axial force (N) matching an ETABS axial load (kN), or None."""
    best = None
    for n in options_N:
        rel = abs(n / 1000.0 - axial_kN) / max(axial_kN, 1e-9)
        if rel <= tolerance and (best is None or rel < best[0]):
            best = (rel, n)
    return best[1] if best else None


def abaqus_curves(reference, levels, keys, log=print):
    """{axial_kN: {key: array}} from the Abaqus database for the ETABS levels (empty when not available)."""
    ab = reference.get("abaqus", {})
    root = resolve(ab.get("database", ""))
    if not root or not os.path.isdir(root):
        log(f"Abaqus database not found ({root}) - no Abaqus curves")
        return {}
    db = AbaqusDB(root)
    jobs = db.jobs(ab.get("family", ""))
    if not jobs:
        log(f"Abaqus family '{ab.get('family')}' not found in {root}")
        return {}
    out = {}
    for lv in levels:
        n = match_axial(lv.axial_kN, jobs, ab.get("axial_tolerance", 0.03))
        if n is None:
            continue
        curves = {"job": jobs[n]}
        for key in keys:
            expr = ab.get("expressions", {}).get(key)
            if expr:
                try:
                    curves[key] = db.evaluate(expr, jobs[n])
                except Exception as e:
                    log(f"Abaqus {key} for {jobs[n]}: {e}")
        # after collapse the Abaqus panel falls by metres: keep the frames before the first collapse step
        mm = [v for k, v in curves.items() if k not in ("job", "H", "V") and isinstance(v, np.ndarray)]
        end = abaqus_valid_end(mm, ab.get("collapse_jump_mm", 5.0), ab.get("collapse_jump_ratio", 10.0))
        for k, v in list(curves.items()):
            if isinstance(v, np.ndarray):
                curves[k] = v[:end]
        curves["valid_frames"] = end
        out[lv.axial_kN] = curves
    return out


def abaqus_valid_end(arrays, jump_mm=5.0, ratio=10.0):
    """Number of valid frames: the frames before the first step where any of the arrays (mm) changes by more
    than jump_mm AND more than ratio x every earlier step (collapse rule of Damage_states_GUI.py; 0 = off)."""
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


def abaqus_ds_drifts(reference, log=print):
    """{axial_kN: {"ND", "LD", "SD", "NC", "job", "failure"}} from the damage-state workbook (family block 1)."""
    ab = reference.get("abaqus", {})
    path = resolve(ab.get("ds_workbook", ""))
    if not path or not os.path.isfile(path):
        log(f"Abaqus damage-state workbook not found ({path})")
        return {}
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    family = ab.get("ds_family") or ab.get("family", "")
    meta = {}
    if "_meta" in wb.sheetnames:
        for row in list(wb["_meta"].iter_rows(values_only=True))[1:]:
            if row and row[0]:
                meta[str(row[0])] = row
    if family not in meta:
        log(f"Family '{family}' not in {os.path.basename(path)} (has: {', '.join(meta)})")
        return {}
    _, sheet, first, n = meta[family][:4]
    ws = wb[sheet]
    out = {}
    for row in ws.iter_rows(min_row=int(first), max_row=int(first) + int(n) - 1, max_col=14, values_only=True):
        if row[0] is None:
            continue
        job = str(row[13] or "")
        num = job.rpartition("_")[2]
        axial = int(num) / 1000.0 if num.isdigit() else float(row[0])
        out[axial] = {"ND": row[4], "LD": row[5], "SD": row[6], "NC": row[7], "failure_drift": row[2],
                      "end_of_data": row[3], "failure": row[12], "job": job}
    return out


def abaqus_ds_envelopes(reference, ds_drifts, log=print):
    """Abaqus force (H, V) at the end of each damage state, per axial load -> {state: [(H, V, axial_kN)]}.
    Needs the database (H and V expressions) and the damage-state drifts."""
    ab = reference.get("abaqus", {})
    root = resolve(ab.get("database", ""))
    if not ds_drifts or not root or not os.path.isdir(root):
        return {}
    db = AbaqusDB(root)
    jobs = db.jobs(ab.get("ds_family") or ab.get("family", ""))
    ex = ab.get("expressions", {})
    out = {k: [] for k in DS_NAMES}
    for axial, row in sorted(ds_drifts.items()):
        n = match_axial(axial, jobs, ab.get("axial_tolerance", 0.03))
        if n is None:
            continue
        try:
            drift = db.evaluate(ex["drift"], jobs[n])
            H = db.evaluate(ex["H"], jobs[n])
            V = db.evaluate(ex["V"], jobs[n])
        except Exception as e:
            log(f"Abaqus envelope {jobs[n]}: {e}")
            continue
        # frames after the collapse are not used; a drift beyond the last valid frame takes that frame
        end = abaqus_valid_end([drift], ab.get("collapse_jump_mm", 5.0), ab.get("collapse_jump_ratio", 10.0))
        drift, H, V = drift[:end], H[:end], V[:end]
        last = float(np.nanmax(drift)) if drift.size else 0.0
        for k in DS_NAMES:
            d = row.get(k)
            if d is None or (isinstance(d, (int, float)) and d <= 0 and k != "ND"):
                continue
            d = min(float(d), last)
            h, v = value_at_drift(drift, H, d), value_at_drift(drift, V, d)
            if h is not None:
                out[k].append((h, v if v is not None else -axial, axial))
    return out
