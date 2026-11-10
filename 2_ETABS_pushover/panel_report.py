"""
Report of a saved run (no ETABS needed): PNG plots, an Excel workbook and an ETABS results database in the
layout of the Abaqus "Panel_results" database (so Panel_plotter_GUI.py can plot ETABS and Abaqus together).

    make_report(run_dir, criteria_set_name, settings, log)  ->  <run>/Report - <criteria set>/
        Panel pushover report.xlsx
        plots/*.png
    export_database(run_dir, settings)                       ->  <run>/ETABS_results_database/<SET>/<JOB>/<JOB>.txt
"""

import os
import re
import time

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

import panel_core as pc


# damage-state colours of the user's tools (Wall identifier: ND green, LD amber, SD dark red, NC black)
DS_COLORS = {"ND": "#00B050", "LD": "#FFC000", "SD": "#AA0000", "NC": "#000000"}
DS_LIST = list(pc.DS_NAMES)

# axial-load colours of Panel_plotter_GUI.py (same colour for ETABS and Abaqus of one load)
LOAD_COLORS = [(34272, "#FF0000"), (68874, "#FF4D00"), (103485, "#FF9700"), (138087, "#FFE400"),
               (172699, "#C5FF00"), (207301, "#7CFF00"), (241913, "#08FF00"), (276515, "#00FF59"),
               (311126, "#00FFB6"), (360000, "#00DCFF"), (720000, "#0087FF"), (1080000, "#003EFF"),
               (1440000, "#0F00FF"), (1800000, "#7800FF"), (2160000, "#D500FF"), (2520000, "#FF00E4"),
               (2880000, "#900982")]


def load_color(axial_kN):
    n = axial_kN * 1000.0
    return min(LOAD_COLORS, key=lambda c: abs(np.log(max(n, 1.0) / c[0])))[1]


def safe_name(text):
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text)).strip(" .") or "set"


def _fig(w=11, h=7):
    fig = Figure(figsize=(w, h))
    FigureCanvasAgg(fig)
    return fig


def _save(fig, path):
    pc.makedirs(os.path.dirname(path))
    fig.savefig(pc.long_path(path), dpi=150, bbox_inches="tight")
    return path


def _style(ax, xlabel, ylabel, title=None):
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    if title:
        ax.set_title(title, fontsize=12)
    ax.grid(True, color="#e3e3e3", lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)


def _xlim(ax, s, zoom=True):
    """Drift axis from 0 to the user's limit, else to the automatic zoom (see ReportData); the y axis is fitted
    to the data inside that range (after the capacity the force-controlled push runs away to metres)."""
    xmax = s.get("drift_plot_max_mm") or (s.get("_zoom_mm") if zoom else None)
    if not xmax:
        return
    xmax = float(xmax)
    ax.set_xlim(min(0.0, ax.get_xlim()[0]) if not zoom else -0.02 * xmax, xmax)
    _fit_y(ax, -0.02 * xmax, xmax)


def _fit_y(ax, x0, x1):
    """y limits of the data drawn between x0 and x1 (lines, line collections, scatters)."""
    ys = []
    for ln in ax.lines:
        x, y = (np.asarray(v, dtype=float) for v in ln.get_data())
        if x.size == y.size and x.size:
            ys.append(y[(x >= x0) & (x <= x1)])
    for coll in ax.collections:
        if hasattr(coll, "get_segments"):
            for seg in coll.get_segments():
                seg = np.asarray(seg)
                if seg.size:
                    ys.append(seg[(seg[:, 0] >= x0) & (seg[:, 0] <= x1), 1])
        off = np.asarray(coll.get_offsets())
        if off.ndim == 2 and off.size:
            ys.append(off[(off[:, 0] >= x0) & (off[:, 0] <= x1), 1])
    ys = np.concatenate([y[np.isfinite(y)] for y in ys if y.size]) if ys else np.array([])
    if ys.size:
        lo, hi = float(ys.min()), float(ys.max())
        pad = 0.06 * (hi - lo or abs(hi) or 1.0)
        ax.set_ylim(lo - pad, hi + pad)


def _colored_line(ax, x, y, codes, lw=2.0, label_ends=None):
    """Line whose segments take the colour of the damage state reached at their end point."""
    from matplotlib.collections import LineCollection
    pts = np.column_stack([x, y])
    ok = np.all(np.isfinite(pts), axis=1)
    seg = np.stack([pts[:-1], pts[1:]], axis=1)
    keep = ok[:-1] & ok[1:]
    colors = [DS_COLORS[DS_LIST[min(int(c), 3)]] for c in codes[1:]]
    ax.add_collection(LineCollection(seg[keep], colors=np.array(colors, dtype=object)[keep], linewidths=lw))
    ax.autoscale()


# =====================================================================================================
# DATA OF ONE REPORT
# =====================================================================================================

class ReportData:
    """Everything the plots and the workbook need, computed once."""

    def __init__(self, run_dir, criteria_set_name, s, log=print):
        self.run_dir = run_dir
        self.s = s
        self.log = log
        self.run, self.levels = pc.load_run(run_dir)
        crit = pc.load_criteria()
        if criteria_set_name not in crit["sets"]:
            raise pc.PanelError(f"Criteria set '{criteria_set_name}' not found")
        self.criteria_name = criteria_set_name
        self.criteria = crit["sets"][criteria_set_name]
        self.qmap = pc.quantity_map(s)
        self.reference = pc.load_reference()
        self.q = {}                                         # axial -> {key: array}
        self.ev = {}                                        # axial -> evaluate_level result
        for lv in self.levels:
            self.q[lv.axial_kN] = {k: pc.quantity(lv, q) for k, q in self.qmap.items()}
            self.ev[lv.axial_kN] = pc.evaluate_level(lv, self.criteria, s)
        self.ab_curves, self.ab_ds, self.ab_env = {}, {}, {}
        if s.get("abaqus_compare", True):
            keys = ["drift", "H", "V"] + [c["quantity"] for c in self.criteria["checks"]] + \
                   ["ax_top_K8", "ax_top_K6", "sh_slide_top"]
            try:
                self.ab_curves = pc.abaqus_curves(self.reference, self.levels, keys, log)
            except Exception as e:
                log(f"Abaqus curves not available: {e}")
            try:
                self.ab_ds = pc.abaqus_ds_drifts(self.reference, log)
                self.ab_env = pc.abaqus_ds_envelopes(self.reference, self.ab_ds, log)
            except Exception as e:
                log(f"Abaqus damage states not available: {e}")
        # automatic zoom of the drift axis: 1.5 x the drift where ETABS reaches NC (or ends SD), at least the
        # Abaqus range, never more than the data
        ends = []
        for lv in self.levels:
            ev = self.ev[lv.axial_kN]
            for k in ("SD", "NC"):                      # the panel's (combined) damage states
                e = ev["combined"]["ends"].get(k)
                if e is not None and e < ev["end_of_data"] - 1e-9:
                    ends.append(e)
        ab_max = [float(np.nanmax(c["drift"])) for c in self.ab_curves.values() if "drift" in c and len(c["drift"])]
        data_max = max(float(np.nanmax(self.q[lv.axial_kN]["drift"])) for lv in self.levels)
        zoom = max([1.5 * max(ends)] if ends else [10.0])
        zoom = max(zoom, max(ab_max) * 1.05 if ab_max else 0.0, 10.0)
        self.s = dict(s, _zoom_mm=min(zoom, data_max * 1.02) if data_max > 0 else None)

    def label(self, lv):
        return f"{lv.axial_kN:g} kN"

    def ab_ds_for(self, axial):
        tol = self.reference.get("abaqus", {}).get("axial_tolerance", 0.03)
        n = pc.match_axial(axial, [int(round(k * 1000)) for k in self.ab_ds], tol)
        return self.ab_ds.get(n / 1000.0) if n is not None else None

    def at_drift(self, axial, key, drift_target):
        return pc.value_at_drift(self.q[axial]["drift"], self.q[axial][key], drift_target)


# =====================================================================================================
# PLOTS
# =====================================================================================================

def plot_pushover_all(d, path, zoom=True):
    fig = _fig()
    ax = fig.add_subplot(111)
    for lv in d.levels:
        a = lv.axial_kN
        c = load_color(a)
        ax.plot(d.q[a]["drift"], d.q[a]["H"], color=c, lw=2, label=f"ETABS {d.label(lv)}")
        ab = d.ab_curves.get(a)
        if ab and "drift" in ab and "H" in ab:
            ax.plot(ab["drift"], ab["H"], color=c, lw=1.4, ls="--", label=f"Abaqus {ab['job']}")
    _style(ax, "Drift [mm]", "Horizontal force H [kN]", "Pushover curves - ETABS (solid) and Abaqus (dashed)"
           + ("" if zoom else " - whole analysis"))
    _xlim(ax, d.s, zoom)
    ax.legend(fontsize=8, ncol=2, frameon=False)
    return _save(fig, path)


def plot_pushover_by_ds(d, path, which):
    """One subplot per axial load, curve coloured by the damage state of `which` ('combined' or check index);
    Abaqus curve dashed grey with its damage-state ends marked."""
    n = len(d.levels)
    cols = min(4, n)
    rows = int(np.ceil(n / cols))
    fig = _fig(4.2 * cols, 3.4 * rows + 0.6)
    title = "combined (checks used for DS)" if which == "combined" else d.criteria["checks"][which]["name"]
    for i, lv in enumerate(d.levels):
        a = lv.axial_kN
        ax = fig.add_subplot(rows, cols, i + 1)
        ev = d.ev[a]
        r = ev["combined"] if which == "combined" else ev["checks"][which]
        _colored_line(ax, d.q[a]["drift"], d.q[a]["H"], r["codes"])
        for name in DS_LIST[:3]:
            e = r["ends"][name]
            if e is not None and e < ev["end_of_data"] - 1e-9:
                h = d.at_drift(a, "H", e)
                ax.plot([e], [h], "o", ms=6, color=DS_COLORS[DS_LIST[DS_LIST.index(name) + 1]], mec="white",
                        zorder=4)
        ab = d.ab_curves.get(a)
        abds = d.ab_ds_for(a)
        if ab and "drift" in ab and "H" in ab:
            ax.plot(ab["drift"], ab["H"], color="#888888", lw=1.2, ls="--", zorder=1)
            if abds:
                for name in DS_LIST[:3]:
                    e = abds.get(name)
                    if isinstance(e, (int, float)) and e > 0:
                        h = pc.value_at_drift(ab["drift"], ab["H"], float(e))
                        if h is not None:
                            ax.plot([e], [h], "^", ms=7, color=DS_COLORS[DS_LIST[DS_LIST.index(name) + 1]],
                                    mec="#444444", zorder=4)
        _style(ax, "Drift [mm]", "H [kN]", d.label(lv))
        _xlim(ax, d.s)
    handles = [Patch(color=DS_COLORS[k], label=k) for k in DS_LIST] + [
        Line2D([], [], marker="o", ls="", color="#666666", mec="white", label="ETABS: state starts"),
        Line2D([], [], color="#888888", ls="--", label="Abaqus"),
        Line2D([], [], marker="^", ls="", color="#666666", mec="#444444", label="Abaqus: state starts")]
    fig.suptitle(f"Damage state by {title}", fontsize=13)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.legend(handles=handles, loc="lower center", ncol=7, frameon=False, fontsize=9)
    return _save(fig, path)


def _envelopes(d, state):
    return [e for e in d.reference.get("envelopes", []) if e.get("state") == state and e.get("show", True)
            and e.get("points")]


def plot_force_envelope(d, path):
    fig = _fig(10, 8)
    ax = fig.add_subplot(111)
    styles = ["-", "--", "-.", ":"]
    for i, env in enumerate(_envelopes(d, "ultimate")):
        p = np.array(env["points"], dtype=float)
        ax.plot(p[:, 0], p[:, 1], color="#555555", ls=styles[i % 4], lw=1.6, label=env["name"])
    for lv in d.levels:
        a = lv.axial_kN
        codes = d.ev[a]["combined"]["codes"]
        for k, name in enumerate(DS_LIST):
            sel = codes == k
            ax.scatter(d.q[a]["H"][sel], d.q[a]["V"][sel], s=4, color=DS_COLORS[name], zorder=2)
    ult = [(float(np.nanmax(d.q[lv.axial_kN]["H"])), float(np.nanmedian(d.q[lv.axial_kN]["V"]))) for lv in d.levels]
    ult = np.array(ult)
    ax.plot(ult[:, 0], ult[:, 1], "D-", color="#7800FF", ms=6, lw=1.4, label="ETABS maximum H per axial load")
    handles, labels = ax.get_legend_handles_labels()
    handles += [Patch(color=DS_COLORS[k], label=f"ETABS steps in {k}") for k in DS_LIST]
    ax.legend(handles=handles, fontsize=9, frameon=False, loc="lower center", ncol=2)
    _style(ax, "Horizontal force H [kN]", "Vertical force V [kN]", "Force envelope of the wall panel")
    return _save(fig, path)


def plot_envelope_per_ds(d, path):
    """2 x 2: for every damage state the force at the END of that state (ETABS per axial load) with the Abaqus
    envelope of the same state (computed from the database + damage-state workbook, or typed in the
    reference file), and the ultimate envelopes for orientation."""
    fig = _fig(13, 10)
    for i, name in enumerate(DS_LIST):
        ax = fig.add_subplot(2, 2, i + 1)
        for env in _envelopes(d, "ultimate"):
            p = np.array(env["points"], dtype=float)
            ax.plot(p[:, 0], p[:, 1], color="#bbbbbb", lw=1.0)
        pts = []
        for lv in d.levels:
            a = lv.axial_kN
            e = d.ev[a]["combined"]["ends"][name]
            if e is not None:
                pts.append((d.at_drift(a, "H", e), d.at_drift(a, "V", e)))
        if pts:
            p = np.array(pts, dtype=float)
            ax.plot(p[:, 0], p[:, 1], "o-", color=DS_COLORS[name], lw=2, ms=6, label=f"ETABS end of {name}")
        ab = d.ab_env.get(name) or []
        if ab:
            p = np.array([(h, v) for h, v, _ in ab], dtype=float)
            ax.plot(p[:, 0], p[:, 1], "^--", color=DS_COLORS[name], mfc="white", lw=1.5, ms=7,
                    label=f"Abaqus end of {name} ({d.reference['abaqus'].get('ds_family', '')})")
        for env in _envelopes(d, name):
            p = np.array(env["points"], dtype=float)
            ax.plot(p[:, 0], p[:, 1], "s:", color="#444444", lw=1.5, ms=4, label=env["name"])
        _style(ax, "H [kN]", "V [kN]", f"{name} envelope")
        ax.legend(fontsize=8, frameon=False)
    fig.suptitle("Force envelope for each damage state (force at the end of the state)", fontsize=13)
    fig.tight_layout()
    return _save(fig, path)


def _check_limits_for(d, key):
    """[(check name, sense, limits)] of the criteria checks on quantity `key`."""
    return [(c["name"], c.get("sense", "+"), pc._limits(c), c.get("relative_to_start", False))
            for c in d.criteria["checks"] if c["quantity"] == key]


def plot_drift_vs(d, key, path):
    q = d.qmap[key]
    fig = _fig()
    ax = fig.add_subplot(111)
    for lv in d.levels:
        a = lv.axial_kN
        c = load_color(a)
        ax.plot(d.q[a]["drift"], d.q[a][key], color=c, lw=2, label=f"ETABS {d.label(lv)}")
        ab = d.ab_curves.get(a)
        if ab and key in ab and "drift" in ab:
            n = min(len(ab["drift"]), len(ab[key]))
            ax.plot(ab["drift"][:n], ab[key][:n], color=c, lw=1.2, ls="--", label=f"Abaqus {ab['job']}")
    # the AX curves typed in the Excel (ALL AXIAL FINAL), only for the axial loads of this run
    markers = ["s", "D", "v"]
    tol = d.reference.get("abaqus", {}).get("axial_tolerance", 0.03)
    for i, (name, block) in enumerate(d.reference.get("ax_curves", {}).items()):
        if block.get("quantity") != key:
            continue
        x = np.array(block["drift_mm"], dtype=float)
        for load, ys in block["curves"].items():
            if not any(abs(float(load) - lv.axial_kN) <= max(tol * lv.axial_kN, 1.0) for lv in d.levels):
                continue
            y = np.array([np.nan if v is None else v for v in ys], dtype=float)
            if np.allclose(np.nan_to_num(y), 0):
                continue
            ax.plot(x, y, marker=markers[i % 3], ms=4, lw=0.8, ls=":", color=load_color(float(load)),
                    label=f"Excel {name} {load} kN")
    for name, sense, lim, rel in _check_limits_for(d, key):
        sign = -1 if sense == "-" else 1
        for k, l in enumerate(lim):
            if l is None:
                continue
            color = DS_COLORS[DS_LIST[min(k + 1, 3)]] if k < 3 else "#777777"
            for j, sg in enumerate((1, -1) if sense == "abs" else (sign,)):
                ax.axhline(sg * l, color=color, lw=1.1, ls=(0, (6, 3)), zorder=0,
                           label=f"{DS_LIST[k]} ends: {sg * l:g} mm{' (from step 0)' if rel else ''}"
                           if j == 0 else None)
    _style(ax, "Drift [mm]", f"{q['name']} [mm]", q["name"])
    _xlim(ax, d.s)
    ax.legend(fontsize=7, ncol=2, frameon=False)
    return _save(fig, path)


def plot_mirror(d, path):
    """Top AX links vs bottom AX links, and node displacements vs link sums: the 'top mirrors bottom' check."""
    fig = _fig(15, 5)
    pairs = [("ax_uplift", "ax_top_K8", "K3 U1 (bottom heel) [mm]", "K8 U1 (top) [mm]"),
             ("ax_crush", "ax_top_K6", "K1 U1 (bottom toe) [mm]", "K6 U1 (top) [mm]"),
             (None, None, "Drift [mm]", "Vertical displacement [mm]")]
    for i, (kx, ky, lx, lyl) in enumerate(pairs):
        ax = fig.add_subplot(1, 3, i + 1)
        for lv in d.levels:
            a = lv.axial_kN
            c = load_color(a)
            if kx:
                ax.plot(d.q[a][kx], d.q[a][ky], color=c, lw=1.6, label=d.label(lv))
            else:
                ax.plot(d.q[a]["drift"], d.q[a]["node_uplift"], color=c, lw=1.8, label=f"{d.label(lv)} node 1198-1199")
                ax.plot(d.q[a]["drift"], d.q[a]["ax_uplift"] + d.q[a]["ax_top_K6"], color=c, lw=1.0, ls="--")
        if kx:
            lims = ax.get_xlim()
            ax.plot(lims, lims, color="#999999", lw=1, ls=":", label="1 : 1")
            title = "Top link vs bottom link"
        else:
            ax.plot([], [], color="#666666", ls="--", label="K3 + K6 (heel side links)")
            title = "Node uplift vs heel-side links"
        _style(ax, lx, lyl, title)
        ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    return _save(fig, path)


def plot_ds_drifts(d, path):
    """Stacked damage-state ranges (drift where each state ends) per axial load: ETABS vs Abaqus."""
    fig = _fig(max(10, 1.6 * len(d.levels) + 3), 7)
    ax = fig.add_subplot(111)
    width = 0.38
    for i, lv in enumerate(d.levels):
        a = lv.axial_kN
        for j, (ends, who) in enumerate(((d.ev[a]["combined"]["ends"], "ETABS"), (d.ab_ds_for(a), "Abaqus"))):
            if not ends:
                continue
            x = i + (j - 0.5) * width
            prev = 0.0
            for name in DS_LIST:
                e = ends.get(name)
                if not isinstance(e, (int, float)):
                    break
                ax.bar(x, max(e - prev, 0), width * 0.95, bottom=prev, color=DS_COLORS[name],
                       edgecolor="white", hatch=None if who == "ETABS" else "//")
                prev = max(prev, e)
            ax.text(x, prev, who[0], ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(d.levels)))
    ax.set_xticklabels([d.label(lv) for lv in d.levels])
    handles = [Patch(color=DS_COLORS[k], label=k) for k in DS_LIST] + [
        Patch(facecolor="white", edgecolor="#444444", label="E = ETABS"),
        Patch(facecolor="white", edgecolor="#444444", hatch="//", label="A = Abaqus")]
    ax.legend(handles=handles, frameon=False, ncol=6, fontsize=9)
    _style(ax, "Axial load", "Drift [mm] (end of each damage state)",
           f"Damage-state drifts - ETABS ({d.criteria_name}) vs Abaqus ({d.reference['abaqus'].get('ds_family', '')})")
    return _save(fig, path)


def plot_time(d, path):
    fig = _fig(15, 4.5)
    for i, (key, lab) in enumerate((("drift", "Drift [mm]"), ("H", "H [kN]"), ("V", "V [kN]"))):
        ax = fig.add_subplot(1, 3, i + 1)
        for lv in d.levels:
            ax.plot(lv.time, d.q[lv.axial_kN][key], color=load_color(lv.axial_kN), lw=1.5, label=d.label(lv))
        _style(ax, "Time [s]", lab, lab.split(" [")[0] + " over time")
    fig.axes[0].legend(fontsize=7, frameon=False)
    fig.tight_layout()
    return _save(fig, path)


def make_plots(d, folder):
    paths = [plot_pushover_all(d, os.path.join(folder, "01 Pushover curves.png")),
             plot_pushover_all(d, os.path.join(folder, "01b Pushover curves - whole analysis.png"), zoom=False),
             plot_pushover_by_ds(d, os.path.join(folder, "02 Damage state - combined.png"), "combined")]
    for i, c in enumerate(d.criteria["checks"]):
        paths.append(plot_pushover_by_ds(d, os.path.join(folder, f"02 Damage state - check {i + 1}.png"), i))
    paths += [plot_force_envelope(d, os.path.join(folder, "03 Force envelope.png")),
              plot_envelope_per_ds(d, os.path.join(folder, "04 Force envelope per damage state.png"))]
    keys = []
    for c in d.criteria["checks"]:
        if c["quantity"] not in keys:
            keys.append(c["quantity"])
    keys += [k for k in ("ax_top_K8", "ax_top_K6", "sh_slide_top") if k in d.qmap and k not in keys]
    for k in keys:
        paths.append(plot_drift_vs(d, k, os.path.join(folder, f"05 Drift vs {k}.png")))
    paths += [plot_mirror(d, os.path.join(folder, "06 Top vs bottom links.png")),
              plot_ds_drifts(d, os.path.join(folder, "07 Damage-state drifts.png")),
              plot_time(d, os.path.join(folder, "08 Time histories.png"))]
    return paths


# =====================================================================================================
# EXCEL WORKBOOK
# =====================================================================================================

def _scatter(title, x_title, y_title):
    from openpyxl.chart import ScatterChart
    ch = ScatterChart()
    ch.title = title
    ch.style = 13
    ch.x_axis.title = x_title
    ch.y_axis.title = y_title
    ch.height, ch.width = 9, 17
    for axis in (ch.x_axis, ch.y_axis):
        axis.delete = False                 # openpyxl >= 3.1 hides axes otherwise
        axis.crosses = "min"
    ch.legend.position = "b"
    ch.legend.overlay = False
    ch.title.overlay = False if hasattr(ch.title, "overlay") else None
    return ch


def _series(ws, ch, x_col, y_col, first, last, name, color=None, width=19050):
    from openpyxl.chart import Reference, Series
    xs = Reference(ws, min_col=x_col, min_row=first, max_row=last)
    ys = Reference(ws, min_col=y_col, min_row=first - 1, max_row=last)
    ser = Series(ys, xs, title_from_data=True)
    ser.marker.symbol = "none"
    ser.smooth = False
    if color:
        ser.graphicalProperties.line.solidFill = color.lstrip("#")
    ser.graphicalProperties.line.width = width
    ch.series.append(ser)
    return ser


def write_workbook(d, path, plot_paths):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter as L

    wb = openpyxl.Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    ds_fill = {k: PatternFill("solid", fgColor=v.lstrip("#")) for k, v in DS_COLORS.items()}

    def header(ws, row, values, col=1):
        for j, v in enumerate(values):
            c = ws.cell(row, col + j, v)
            c.font = bold
            c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="top")

    # ------------------------------------------------------------- README
    ws = wb.active
    ws.title = "README"
    st = d.run.get("settings", {})
    lines = [
        ("Single-panel pushover in ETABS - damage states for the calibration of acceptance criteria", True),
        (f"Run: {os.path.basename(d.run_dir)}    created {d.run.get('created', '')}", False),
        (f"Model: {d.run.get('model', {}).get('model_file', '')}", False),
        (f"Pushover case: {st.get('case')}  (starts from {' -> '.join(d.run.get('model', {}).get('case_chain', [])[:-1])}), "
         f"horizontal pattern {st.get('horizontal_pattern')} x scale factor", False),
        (f"Axial force: pattern {st.get('vertical_pattern')}, split between joints {st.get('vertical_nodes')} "
         f"({st.get('vertical_shares')})", False),
        (f"Displacement window of joint {st.get('control_node')} ({st.get('control_dof')}): "
         f"{st.get('disp_min')} - {st.get('disp_max')} m", False),
        (f"Acceptance criteria set: {d.criteria_name}  ({d.criteria.get('note', '')})", False),
        (f"Report made {time.strftime('%Y-%m-%d %H:%M')}", False),
        ("", False),
        ("Units: displacements and drifts in mm, forces in kN. Drift = horizontal displacement of the panel top "
         "(average of joints 1198 and 1196).", False),
        ("Damage states: the limits of a check are the UPPER bounds of ND, LD, SD and NC (as in ALL AXIAL FINAL.xlsx). "
         "'End of a state' = drift where the value reaches its limit; the panel (combined) leaves a state as soon "
         "as any check used for DS leaves it. Damage never decreases (running maximum).", False),
        ("Abaqus columns: drift where each state ends, from the damage-state workbook (same meaning).", False),
        ("", False),
        ("Sheets: Summary - one row per axial load;  Criteria - the checks and limits;  Quantities - definitions;  "
         "one sheet per axial load with every step;  Envelope - forces at the end of each state;  "
         "Plots - all figures (also as PNG files in the plots folder).", False),
    ]
    for i, (text, b) in enumerate(lines, 1):
        ws.cell(i, 1, text).font = Font(bold=b, size=13 if b else 11)
    ws.column_dimensions["A"].width = 160

    # ------------------------------------------------------------- Criteria and quantities
    ws = wb.create_sheet("Criteria")
    header(ws, 1, ["Check", "Quantity", "Sense", "Relative to step 0", "Used for DS", "ND end [mm]", "LD end [mm]",
                   "SD end [mm]", "NC end [mm]"])
    for i, c in enumerate(d.criteria["checks"], 2):
        lim = pc._limits(c)
        for j, v in enumerate([c["name"], d.qmap[c["quantity"]]["name"], c.get("sense", "+"),
                               "yes" if c.get("relative_to_start") else "no",
                               "yes" if c.get("use_for_DS", True) else "no", *lim], 1):
            ws.cell(i, j, v)
    for col, w in zip("ABCDEFGHI", (34, 46, 8, 12, 10, 12, 12, 12, 12)):
        ws.column_dimensions[col].width = w
    ws = wb.create_sheet("Quantities")
    header(ws, 1, ["Key", "Name", "Expression (ETABS results, m / kN)", "Unit in report"])
    for i, q in enumerate(d.s["quantities"], 2):
        for j, v in enumerate([q["key"], q["name"], q["expr"], "mm" if q.get("kind") == "disp" else "kN"], 1):
            ws.cell(i, j, v)
    for col, w in zip("ABCD", (14, 46, 46, 14)):
        ws.column_dimensions[col].width = w

    # ------------------------------------------------------------- Summary
    ws = wb.create_sheet("Summary", 1)
    cols = ["Axial [kN]", "Job", "Scale factor", "In window", "Attempts (SF: result)", "Case status", "Steps",
            "Max drift [mm]", "Max H [kN]", "Drift at max H [mm]"]
    cols += [f"ETABS {k} end [mm]" for k in DS_LIST] + [f"Abaqus {k} end [mm]" for k in DS_LIST]
    cols += [f"ETABS - Abaqus {k} [mm]" for k in DS_LIST]
    cols += [f"H at {k} end [kN]" for k in DS_LIST] + [f"V at {k} end [kN]" for k in DS_LIST]
    for c in d.criteria["checks"]:
        cols += [f"{c['name']}: {k} end [mm]" for k in DS_LIST]
    cols += ["Notes"]
    header(ws, 1, cols)
    for i, lv in enumerate(d.levels, 2):
        a = lv.axial_kN
        ev = d.ev[a]
        H, drift = d.q[a]["H"], d.q[a]["drift"]
        imax = int(np.nanargmax(H))
        ends = ev["combined"]["ends"]
        abds = d.ab_ds_for(a) or {}
        att = "; ".join(f"{x['scale_factor']:.4g} {x.get('scheme', '')}: {x['verdict']} ({x['max_control_disp_m']:.3g} m)"
                        for x in lv.data.get("attempts", []))
        row = [a, lv.data.get("job"), lv.data.get("scale_factor"), "yes" if lv.data.get("in_window") else "NO",
               att, lv.data.get("status"), len(lv.time), float(np.nanmax(drift)), float(H[imax]), float(drift[imax])]
        row += [ends[k] for k in DS_LIST]
        row += [abds.get(k) if isinstance(abds.get(k), (int, float)) else None for k in DS_LIST]
        row += [(ends[k] - abds[k]) if ends[k] is not None and isinstance(abds.get(k), (int, float)) else None
                for k in DS_LIST]
        row += [d.at_drift(a, "H", ends[k]) for k in DS_LIST] + [d.at_drift(a, "V", ends[k]) for k in DS_LIST]
        notes = []
        for ch in ev["checks"]:
            row += [ch["ends"][k] for k in DS_LIST]
            notes += [f"{ch['check']['name']}: {n}" for n in ch["notes"]]
        row.append(" | ".join(notes))
        for j, v in enumerate(row, 1):
            ws.cell(i, j, v)
        for k_i, k in enumerate(DS_LIST):
            ws.cell(i, 11 + k_i).fill = ds_fill[k]
            if k == "NC":
                ws.cell(i, 11 + k_i).font = Font(color="FFFFFF")
    ws.row_dimensions[1].height = 45
    for j in range(1, len(cols) + 1):
        ws.column_dimensions[L(j)].width = 13
    ws.column_dimensions["E"].width = 40
    ws.freeze_panes = "B2"
    n = len(d.levels)
    if n:
        from openpyxl.chart import BarChart, Reference
        bc = BarChart()
        bc.type = "col"
        bc.grouping = "stacked"
        bc.overlap = 100
        bc.title = "ETABS: drift range of each damage state"
        bc.y_axis.title = "Drift [mm]"
        bc.x_axis.title = "Axial load [kN]"
        bc.x_axis.delete = False
        bc.y_axis.delete = False
        bc.height, bc.width = 9, 18
        # helper columns: length of each state's range
        base_col = len(cols) + 2
        header(ws, 1, [f"{k} range [mm]" for k in DS_LIST], col=base_col)
        for i, lv in enumerate(d.levels, 2):
            ends = d.ev[lv.axial_kN]["combined"]["ends"]
            prev = 0.0
            for k_i, k in enumerate(DS_LIST):
                e = ends[k]
                ws.cell(i, base_col + k_i, None if e is None else max(e - prev, 0.0))
                prev = e if e is not None else prev
        data = Reference(ws, min_col=base_col, max_col=base_col + 3, min_row=1, max_row=n + 1)
        bc.add_data(data, titles_from_data=True)
        bc.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n + 1))
        for k_i, ser in enumerate(bc.series):
            ser.graphicalProperties.solidFill = DS_COLORS[DS_LIST[k_i]].lstrip("#")
            ser.invertIfNegative = False
        ws.add_chart(bc, f"A{n + 4}")

    # ------------------------------------------------------------- one sheet per axial load
    for lv in d.levels:
        a = lv.axial_kN
        ws = wb.create_sheet(safe_name(f"{a:g} kN")[:31])
        ev = d.ev[a]
        keys = [q["key"] for q in d.s["quantities"]]
        cols = ["Step", "Time [s]"] + [f"{d.qmap[k]['name']} [{'mm' if d.qmap[k].get('kind') == 'disp' else 'kN'}]"
                                       for k in keys]
        cols += [f"{c['check']['name']} - DS" for c in ev["checks"]] + ["Combined DS"]
        cols += [f"H in {k} [kN]" for k in DS_LIST]
        header(ws, 1, cols)
        nrow = len(lv.time)
        for r in range(nrow):
            row = [r, float(lv.time[r])] + [float(d.q[a][k][r]) if np.isfinite(d.q[a][k][r]) else None for k in keys]
            row += [DS_LIST[min(int(c["codes"][r]), 3)] for c in ev["checks"]]
            code = int(ev["combined"]["codes"][r])
            row += [DS_LIST[min(code, 3)]]
            h = d.q[a]["H"][r]
            row += [float(h) if code == k else None for k in range(4)]
            ws.append(row)
        ws.row_dimensions[1].height = 60
        for j in range(1, len(cols) + 1):
            ws.column_dimensions[L(j)].width = 12
        ws.freeze_panes = "C2"
        last = nrow + 1
        c_drift = 3 + keys.index("drift")
        c_ds = len(cols) - 3
        ch = _scatter(f"Pushover by damage state - {a:g} kN", "Drift [mm]", "H [kN]")
        for k_i, k in enumerate(DS_LIST):
            _series(ws, ch, c_drift, c_ds + k_i, 2, last, None, DS_COLORS[k], 28575)
        ws.add_chart(ch, f"{L(len(cols) + 2)}2")
        ch2 = _scatter(f"Drift vs link deformations - {a:g} kN", "Drift [mm]", "Deformation [mm]")
        for k in ("ax_uplift", "ax_crush", "sh_slide", "node_uplift", "node_crush"):
            if k in keys:
                _series(ws, ch2, c_drift, 3 + keys.index(k), 2, last, None)
        zoom = d.s.get("drift_plot_max_mm") or d.s.get("_zoom_mm")
        if zoom:                                            # same drift range as the PNG plots
            for chart in (ch, ch2):
                chart.x_axis.scaling.min = 0
                chart.x_axis.scaling.max = float(round(zoom))
        ws.add_chart(ch2, f"{L(len(cols) + 2)}22")

    # ------------------------------------------------------------- envelope data
    ws = wb.create_sheet("Envelope")
    header(ws, 1, ["Axial [kN]", "State", "ETABS drift at end [mm]", "ETABS H [kN]", "ETABS V [kN]",
                   "Abaqus drift at end [mm]", "Abaqus H [kN]", "Abaqus V [kN]"])
    r = 2
    for lv in d.levels:
        a = lv.axial_kN
        abds = d.ab_ds_for(a) or {}
        for k in DS_LIST:
            e = d.ev[a]["combined"]["ends"][k]
            abh = abv = None
            for h, v, ax_ in d.ab_env.get(k, []):
                if abs(ax_ - a) <= 0.03 * a:
                    abh, abv = h, v
            ws.append([a, k, e, d.at_drift(a, "H", e), d.at_drift(a, "V", e),
                       abds.get(k) if isinstance(abds.get(k), (int, float)) else None, abh, abv])
            r += 1
    for col in "ABCDEFGH":
        ws.column_dimensions[col].width = 16

    # ------------------------------------------------------------- plots
    ws = wb.create_sheet("Plots")
    try:
        from openpyxl.drawing.image import Image
        row = 1
        for p in plot_paths:
            img = Image(pc.long_path(p))
            scale = min(1.0, 1100 / img.width)
            img.width, img.height = img.width * scale, img.height * scale
            ws.cell(row, 1, os.path.basename(p)).font = bold
            ws.add_image(img, f"A{row + 1}")
            row += int(img.height / 20) + 4
    except Exception as e:                                  # Pillow missing: the PNG files are still there
        ws.cell(1, 1, f"Plots could not be embedded ({e}); see the plots folder.")

    pc.makedirs(os.path.dirname(path))
    tmp = pc.long_path(path + ".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, pc.long_path(path))
    return path


# =====================================================================================================
# REPORT
# =====================================================================================================

def report_folder(run_dir, criteria_set_name):
    """<run>/Report - <criteria set> (name shortened so the paths stay short)."""
    return os.path.join(run_dir, f"Report - {safe_name(criteria_set_name)[:45].strip()}")


def make_report(run_dir, criteria_set_name, s, log=print):
    """Plots + workbook of a saved run with one acceptance-criteria set. Returns the report folder."""
    t0 = time.time()
    d = ReportData(run_dir, criteria_set_name, s, log)
    folder = report_folder(run_dir, criteria_set_name)
    log(f"Report of {len(d.levels)} axial load(s) with '{criteria_set_name}' -> {folder}")
    plots = make_plots(d, os.path.join(folder, "plots"))
    log(f"    {len(plots)} plots")
    xlsx = write_workbook(d, os.path.join(folder, "Panel pushover report.xlsx"), plots)
    log(f"    workbook: {xlsx}")
    for lv in d.levels:
        ends = d.ev[lv.axial_kN]["combined"]["ends"]
        ab = d.ab_ds_for(lv.axial_kN) or {}
        log(f"    {lv.axial_kN:>8g} kN  ETABS ends " + "  ".join(
            f"{k} {ends[k]:.2f}" if ends[k] is not None else f"{k} -" for k in DS_LIST)
            + ("   Abaqus " + "  ".join(f"{k} {ab[k]:g}" for k in DS_LIST if isinstance(ab.get(k), (int, float)))
               if ab else ""))
    log(f"    done in {time.time() - t0:.0f} s")
    return folder


# =====================================================================================================
# DATABASE IN THE ABAQUS "Panel_results" LAYOUT
# =====================================================================================================

def _write_txt(path, columns):
    """Comma separated, header line, every line ends with a comma (like the Abaqus export)."""
    names = list(columns)
    n = len(next(iter(columns.values())))
    pc.makedirs(os.path.dirname(path))
    with open(pc.long_path(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(names) + ",\n")
        for i in range(n):
            f.write(",".join("" if not np.isfinite(columns[c][i]) else repr(float(columns[c][i])) for c in names)
                    + ",\n")


def export_database(run_dir, s, log=print):
    """ETABS results as <run>/ETABS_results_database/<SET>/<JOB>/<JOB>.txt, in mm and N like Panel_results:
        ETABS&J<joint>  Ux = ETABS U2 (in plane), Uy = U1 (out of plane), Uz = U3, fix = R2, fiy = R1, fiz = R3,
                        supports also Fx = F2, Fy = F1, Fz = F3, Mx = M2, My = M1, Mz = M3 (reactions)
        ETABS&K<link>   U1..R3 link deformations, P..M3 link forces at end I
        ETABS&PANEL     Drift, H, V and every quantity of the settings (mm / kN)
    JOB = ETABS_PANEL_<axial force in N>. Add this folder next to Panel_results in Panel_plotter_GUI.py."""
    run, levels = pc.load_run(run_dir)
    root = os.path.join(run_dir, "ETABS_results_database")
    for lv in levels:
        job = lv.data.get("job") or pc.job_name(lv.axial_kN)
        t = lv.time
        frame = np.arange(len(t), dtype=float)
        for name, disp in lv.data["joints"].items():
            cols = {"Time": t, "frameId": frame}
            for new, old in (("Ux", "U2"), ("Uy", "U1"), ("Uz", "U3")):
                cols[new] = np.asarray(disp[old], dtype=float) * 1000.0
            for new, old in (("fix", "R2"), ("fiy", "R1"), ("fiz", "R3")):
                cols[new] = np.asarray(disp[old], dtype=float)
            react = lv.data["reactions"].get(name)
            if react:
                for new, old, f in (("Fx", "F2", 1e3), ("Fy", "F1", 1e3), ("Fz", "F3", 1e3),
                                    ("Mx", "M2", 1e6), ("My", "M1", 1e6), ("Mz", "M3", 1e6)):
                    cols[new] = np.asarray(react[old], dtype=float) * f
            _write_txt(os.path.join(root, f"ETABS&J{name}", job, f"{job}.txt"), cols)
        for name, link in lv.data["links"].items():
            cols = {"Time": t, "frameId": frame}
            for c in pc.DISP_COMPONENTS:
                v = np.asarray(link.get("deformation", {}).get(c, [np.nan] * len(t)), dtype=float)
                cols[c] = v * (1000.0 if c.startswith("U") else 1.0)
            for c in pc.LINK_FORCE_COMPONENTS:
                v = np.asarray(link.get("force_I", {}).get(c, [np.nan] * len(t)), dtype=float)
                cols[c] = v * (1e6 if c in ("T", "M2", "M3") else 1e3)
            _write_txt(os.path.join(root, f"ETABS&{name}", job, f"{job}.txt"), cols)
        cols = {"Time": t, "frameId": frame}
        for q in s["quantities"]:
            cols[q["key"]] = pc.quantity(lv, q)
        _write_txt(os.path.join(root, "ETABS&PANEL", job, f"{job}.txt"), cols)
    with open(pc.long_path(os.path.join(root, "README.txt")), "w", encoding="utf-8") as f:
        f.write(export_database.__doc__)
    log(f"Database for Panel_plotter_GUI.py: {root}")
    return root
