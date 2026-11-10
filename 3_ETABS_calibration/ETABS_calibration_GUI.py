#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3 - Calibration of the ETABS link acceptance criteria against Abaqus
=========================================================================

Inputs
    ETABS   a saved pushover run of step 2: 2_ETABS_pushover/ETABS_results/<run>/ (run.json + levels/*.json)
    Abaqus  ONE criteria file of step 1 per mechanism (AX+, AX-, SH, REB): the Abaqus damage-state starts per
            axial load, the section pair and the Abaqus database they were made from
            optional: a CUSTOM criteria file, any file of step 1 (e.g. the minimum of several mechanisms), compared
            with every ticked ETABS mechanism and the panel
    ETABS   acceptance criteria per link (link-to-link): link, component, sense, DS1 ... DS5 thresholds [mm].
            Defaults = the thresholds of the default mechanisms (step 1 defaults file, from the handover).
            ONE set of thresholds for all axial loads.

Views (only change what is shown; the criteria stay the same)
    Pushover               ETABS (solid) and Abaqus (dashed) pushover curves; damage-state starts per mechanism
                           (marker = mechanism, colour = damage state; filled = ETABS, hollow = Abaqus)
    Mechanism vs drift     ETABS link values with their thresholds, Abaqus section values with the step-1 starts
    DS drift comparison    start drift of every state vs axial load, ETABS vs Abaqus, and a table with the ETABS
                           link value at the Abaqus start drift (a suggestion for the ETABS threshold)
    Filters                axial loads, damage states DS0 ... DS5, mechanisms, ETABS / Abaqus, colour by DS or load

Damage states are STARTS (lower bounds): DSk when the value is greater than the DSk threshold; below DS1 = DS0.
A link takes the highest state reached so far (damage does not decrease); a mechanism takes its worst link,
the panel its worst mechanism.

Output  3_ETABS_calibration/etabs_criteria/<name>.json                   the ETABS link criteria (mm)
        3_ETABS_calibration/etabs_criteria/acceptance_criteria_from_calibration.json
                                                                         optional export in the project format
                                                                         (version 2, units m)
Run:    python ETABS_calibration_GUI.py
"""

import copy
import os
import re
import sys
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.figure import Figure                                              # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.collections import LineCollection                                 # noqa: E402
from matplotlib.lines import Line2D                                                # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "_library"))
sys.path.insert(0, os.path.join(ROOT, "2_ETABS_pushover"))
import ds_common as dc                                                             # noqa: E402
import panel_core as pc                                                            # noqa: E402

APP_TITLE = "Step 3 - ETABS calibration against Abaqus"
SETTINGS_FILE = os.path.join(HERE, "gui_settings.json")
CRIT_FORMAT = "etabs-link-criteria"
AXIAL_TOLERANCE = 0.03                       # ETABS level and Abaqus job of the same axial load (relative)
LINK_COMPONENTS = ("U1", "U2", "U3", "R1", "R2", "R3")
GREY = "#D0D0D0"
LIMITS = (("drift", "Drift [mm]"), ("force", "Force [kN]"), ("etabs", "ETABS link value"),
          ("abaqus", "Abaqus section value"), ("start", "Start drift (comparison)"))
# the custom criteria: any criteria file of step 1 (e.g. a statistic of several mechanisms), Abaqus only
CUSTOM = "Custom"
CUSTOM_MARKER = "*"
CUSTOM_LINESTYLE = (0, (6, 2, 1, 2, 1, 2))
SIGN_NOTE = ("ETABS: compression = NEGATIVE deformation (U1 < 0).\n"
             "Value = deformation with the sense (+ as is, - negated, abs |value|). A positive threshold is reached "
             "when the value rises above it, a negative one when it drops below it.\n"
             "Compression: sense - with POSITIVE thresholds (1 = U1 < -1 mm), or sense + with negative ones. "
             "Sense - with negative thresholds tests TENSION.\n"
             "Checks = what a row tests; ⚠ = the other side than its mechanism (AX+ tension, AX- compression).")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def open_path(path):
    if path and os.path.exists(path):
        os.startfile(path)                                    # noqa: S606


def load_settings():
    try:
        return dc.load_json(SETTINGS_FILE)
    except (OSError, ValueError):
        return {}


def mech_marker(key):
    return CUSTOM_MARKER if key == CUSTOM else dc.mech_marker(key)


def mech_linestyle(key):
    return CUSTOM_LINESTYLE if key == CUSTOM else dc.MECH_LINESTYLES.get(key, "-")


# =====================================================================================================
# ETABS CRITERIA ROWS
# =====================================================================================================

SIDE_SIGN = {"Max": 1, "Min": -1}             # ETABS side of a mechanism: positive / negative link deformation


def etabs_threshold(t, sense, side):
    """Default threshold of an ETABS row: the size of the step-1 default, with the sign that makes the row test the
    side of its mechanism (step-1 thresholds carry the sign of the Abaqus value, not of the ETABS link)."""
    if t is None:
        return None
    want = SIDE_SIGN.get(side)
    if want is None or sense == "abs":
        return abs(t)
    return abs(t) * (want if sense == "+" else -want)


def row_check(r, side=None):
    """What a row tests in ETABS signs, e.g. 'U1 < -|DS| (compression)'; '⚠' first when it tests the other side
    than its mechanism (side of the defaults file: Max = positive, Min = negative deformation)."""
    vals = [r.get(k) for k in dc.DS if r.get(k) is not None]
    if not vals:
        return ""
    if min(vals) < 0 <= max(vals):
        return "⚠ mixed signs"
    c, sense = r.get("component", "U1"), r.get("sense", "abs")
    if sense == "abs":
        if vals[0] < 0:
            return "⚠ never (|value| below a negative threshold)"
        sign, text = 0, f"|{c}| > DS"
    else:
        sign = (1 if sense == "+" else -1) * (1 if vals[0] >= 0 else -1)
        text = f"{c} > +|DS|" if sign > 0 else f"{c} < -|DS|"
    if c == "U1" and sign:
        text += " (tension)" if sign > 0 else " (compression)"
    if r.get("relative_to_start"):
        text += " from step 0"
    return ("⚠ " if sign and sign == -SIDE_SIGN.get(side, 0) else "") + text


def new_row(link, prop, mech, component=None, in_run=True):
    et = mech.get("etabs") or {}
    sense = et.get("sense", "abs")
    return {"use": True, "link": link, "property": prop, "mechanism": mech["key"],
            "component": component or (et.get("components") or ["U1"])[0], "sense": sense,
            "relative_to_start": False,
            **{k: etabs_threshold(mech["thresholds"].get(k), sense, et.get("side")) for k in dc.DS},
            "provisional": " ".join(mech.get("provisional", [])), "in_run": "yes" if in_run else "not in run",
            "note": ""}


def default_rows(links, defaults):
    """Rows for every link of the run whose property fits a mechanism (pattern in the defaults file). A mechanism
    without any link in the run (e.g. REB before the rebar links are in the model) gets a placeholder row."""
    rows = []
    found = set()
    for name in sorted(links, key=natural_key):
        prop = links[name].get("property", "")
        for m in defaults["mechanisms"]:
            pat = (m.get("etabs") or {}).get("property_pattern")
            if pat and re.search(pat, prop, re.I):
                for comp in m["etabs"].get("components") or ["U1"]:
                    rows.append(new_row(name, prop, m, comp))
                found.add(m["key"])
    for m in defaults["mechanisms"]:
        if m["key"] not in found:
            for comp in (m.get("etabs") or {}).get("components") or ["U1"]:
                r = new_row(f"{m['key']}?", "(no such link in this run)", m, comp, in_run=False)
                r["note"] = "set the link name when the model has it"
                rows.append(r)
    order = {m["key"]: i for i, m in enumerate(defaults["mechanisms"])}
    rows.sort(key=lambda r: (order.get(r["mechanism"], 99), natural_key(r["link"]), r["component"]))
    return rows


def row_thresholds(r):
    return {k: r.get(k) for k in dc.DS}


def row_provisional(r):
    return [t for t in str(r.get("provisional") or "").replace(",", " ").split() if t in dc.DS]


# =====================================================================================================
# DATA: ETABS RUN AND ABAQUS FILES
# =====================================================================================================

class EtabsRun:
    """A saved run of step 2 in mm / kN: drift and H per level, link values on demand."""

    def __init__(self, run_dir):
        self.dir = run_dir
        self.name = os.path.basename(run_dir)
        self.run, self.levels = pc.load_run(run_dir)
        s = pc.load_settings()                        # quantities of step 2 (drift and H)
        qmap = pc.quantity_map(s)
        self.drift_q, self.force_q = qmap["drift"], qmap["H"]
        self.links = (self.run.get("model") or {}).get("links", {})
        self.drift, self.force = {}, {}
        for lv in self.levels:
            self.drift[lv.axial_kN] = pc.quantity(lv, self.drift_q)
            self.force[lv.axial_kN] = pc.quantity(lv, self.force_q)
        self._cache = {}

    def has_link(self, link):
        return link in self.links or any(link in lv.data.get("links", {}) for lv in self.levels)

    def values(self, lv, row):
        """Damage value of a row at every step (mm for U components, rad for R)."""
        key = (lv.axial_kN, row["link"], row["component"], row["sense"], bool(row.get("relative_to_start")))
        if key not in self._cache:
            try:
                raw = lv.evaluate(f"{{{row['link']}:{row['component']}}}")
            except pc.PanelError:
                self._cache[key] = None
            else:
                raw = raw * (1000.0 if row["component"].startswith("U") else 1.0)
                self._cache[key] = dc.damage_value(raw, row["sense"], row.get("relative_to_start"))
        return self._cache[key]


class AbaqusFile:
    """One criteria file of step 1 with the curves of its jobs."""

    def __init__(self, path):
        self.path = path
        self.d = dc.load_json(path)
        if self.d.get("format") != dc.CRITERIA_FORMAT:
            raise ValueError(f"{path} is not a criteria file of step 1")
        self.mech = dc.complete_mechanism(self.d["mechanism"])
        self.key = self.mech["key"]
        self.pushover = dict(dc.PUSHOVER_DEFAULTS, **(self.d.get("pushover") or {}))
        db = dc.from_root(self.d.get("database", ""))
        self.db = dc.AbaqusDB.open(db)
        self.rows = {r["job"]: r for r in self.d.get("rows", [])}
        self.curves = {}
        for job in self.rows:
            try:
                self.curves[job] = dc.job_curves(self.db, job, self.pushover, self.mech)
            except Exception:
                self.curves[job] = None

    @property
    def name(self):
        return self.d.get("name") or os.path.basename(self.path)

    def starts(self, job):
        r = self.rows.get(job)
        return {k: (r["DS"].get(k) or {}).get("drift") for k in dc.DS} if r else {}

    def disps(self, job):
        r = self.rows.get(job)
        return {k: (r["DS"].get(k) or {}).get("disp") for k in dc.DS} if r else {}


def group_loads(etabs, files, tol=AXIAL_TOLERANCE):
    """[{"kN", "etabs": level or None, "jobs": [(file, job)]}] sorted by axial load: ETABS levels and Abaqus jobs
    of the same axial load (within tol) together."""
    groups = []

    def find(kN):
        for g in groups:
            if abs(g["kN"] - kN) <= tol * max(g["kN"], kN, 1e-9):
                return g
        g = {"kN": kN, "etabs": None, "jobs": []}
        groups.append(g)
        return g

    for lv in (etabs.levels if etabs else []):
        find(lv.axial_kN)["etabs"] = lv
    for f in files:
        for job, r in f.rows.items():
            n = r.get("axial_N") or dc.job_axial_N(job)
            if n is not None:
                find(n / 1000.0)["jobs"].append((f, job))
    groups.sort(key=lambda g: g["kN"])
    return groups


# =====================================================================================================
# EDITABLE TABLE
# =====================================================================================================

class EditableTable(ttk.Frame):
    """Treeview whose cells are edited in place (double click). columns: [(key, heading, width, kind)]
    kind: "text", "optfloat" (empty allowed), "bool" (click toggles), "ro" (read only) or ("choice", [values])."""

    def __init__(self, master, columns, height=8):
        super().__init__(master)
        self.columns = columns
        self.rows = []
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height,
                                 selectmode="browse")
        for key, head, width, _kind in columns:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, anchor="center" if width < 100 else "w", stretch=width >= 100)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree.tag_configure("off", foreground="#A0A0A0")
        self.tree.tag_configure("missing", foreground="#C55A11")
        self.tree.tag_configure("warn", foreground="#C00000")
        self.tree.bind("<Double-1>", self._edit)
        self.tree.bind("<Button-1>", self._click, add=True)
        self.editor = None
        self.on_change = None

    def _fmt(self, value, kind):
        if kind == "bool":
            return "✓" if value else ""
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def refresh(self):
        sel = self.selected_index()
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            tags = ("off",) if not row.get("use") else ("missing",) if row.get("in_run") != "yes" else \
                ("warn",) if str(row.get("check", "")).startswith("⚠") else ()
            self.tree.insert("", "end", iid=str(i), tags=tags,
                             values=[self._fmt(row.get(k), kind) for k, _, _, kind in self.columns])
        if sel is not None and sel < len(self.rows):
            self.tree.selection_set(str(sel))

    def set_rows(self, rows):
        self.rows = [dict(r) for r in rows]
        self.refresh()

    def get_rows(self):
        self._close_editor(save=True)
        return [dict(r) for r in self.rows]

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _changed(self):
        if self.on_change:
            self.on_change()

    def _cell(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or not col:
            return None, None
        return int(row), int(col[1:]) - 1

    def _click(self, event):
        r, c = self._cell(event)
        if r is None:
            return
        key, _, _, kind = self.columns[c]
        if kind == "bool":
            self.rows[r][key] = not self.rows[r].get(key)
            self.refresh()
            self._changed()

    def _edit(self, event):
        r, c = self._cell(event)
        if r is None:
            return
        key, _, _, kind = self.columns[c]
        if kind in ("bool", "ro"):
            return
        self._close_editor(save=True)
        x, y, w, h = self.tree.bbox(str(r), self.tree["columns"][c])
        value = self._fmt(self.rows[r].get(key), kind)
        if isinstance(kind, tuple):
            widget = ttk.Combobox(self.tree, values=kind[1])
            widget.set(value)
            widget.bind("<<ComboboxSelected>>", lambda e: self._close_editor(True))
        else:
            widget = ttk.Entry(self.tree)
            widget.insert(0, value)
            widget.select_range(0, "end")
        widget.place(x=x, y=y, width=max(w, 70), height=h)
        widget.focus_set()
        widget.bind("<Return>", lambda e: self._close_editor(True))
        widget.bind("<Escape>", lambda e: self._close_editor(False))
        widget.bind("<FocusOut>", lambda e: self._close_editor(True))
        self.editor = (widget, r, key, kind)

    def _close_editor(self, save):
        if not self.editor:
            return
        widget, r, key, kind = self.editor
        self.editor = None
        text = widget.get().strip()
        widget.destroy()
        if not save:
            return
        try:
            value = dc.num(text) if kind == "optfloat" else text
        except ValueError:
            messagebox.showerror(APP_TITLE, f"'{text}' is not a number")
            return
        if self.rows[r].get(key) == value:
            return
        self.rows[r][key] = value
        self.refresh()
        self._changed()


# =====================================================================================================
# THE APPLICATION
# =====================================================================================================

COLUMNS = [("use", "Use", 38, "bool"), ("link", "Link", 60, "text"), ("property", "Property", 150, "ro"),
           ("mechanism", "Mech.", 55, ("choice", ["AX+", "AX-", "SH", "REB"])),
           ("component", "Comp.", 50, ("choice", list(LINK_COMPONENTS))),
           ("sense", "Sense", 45, ("choice", ["+", "-", "abs"])), ("relative_to_start", "Rel.", 38, "bool")] + \
          [(k, f"{k} [mm]", 66, "optfloat") for k in dc.DS] + \
          [("check", "Checks (ETABS signs)", 190, "ro"), ("provisional", "Provisional", 80, "text"),
           ("in_run", "In run", 80, "ro"), ("note", "Note", 160, "text")]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.settings = load_settings()
        self.geometry(self.settings.get("geometry", "1700x1000"))
        self.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.defaults = dc.load_defaults(dc.from_root(self.settings.get("defaults_file", "")) or
                                             dc.DEFAULTS_FILE)
        except Exception:
            self.defaults = dc.load_defaults(dc.DEFAULTS_FILE)
        self.mech_keys = [m["key"] for m in self.defaults["mechanisms"]]
        self.etabs = None
        self.files = {}                  # mechanism -> AbaqusFile
        self.loads = []                  # group_loads()
        self.crit_path = None
        self.crit_name = tk.StringVar(value="ETABS link criteria")
        self.dirty = False
        self._plot_job = None
        self._file_paths = {}            # mechanism -> {display: path}
        self._loads_touched = False      # the user changed the axial-load ticks (else defaults / last session)
        self._build()
        self.after(100, self._start)

    # ------------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(top, text="ETABS run:").pack(side="left")
        self.run_var = tk.StringVar()
        self.run_box = ttk.Combobox(top, textvariable=self.run_var, width=34, state="readonly")
        self.run_box.pack(side="left", padx=4)
        self.run_box.bind("<<ComboboxSelected>>", lambda e: self.load_run(os.path.join(dc.ETABS_RESULTS_DIR,
                                                                                     self.run_var.get())))
        ttk.Button(top, text="Folder", command=lambda: open_path(dc.ETABS_RESULTS_DIR)).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="Abaqus criteria (step 1):").pack(side="left")
        self.file_vars, self.file_boxes = {}, {}
        for key in self.mech_keys + [CUSTOM]:
            ttk.Label(top, text=key).pack(side="left", padx=(8, 2))
            v = tk.StringVar()
            b = ttk.Combobox(top, textvariable=v, width=24, state="readonly")
            b.pack(side="left")
            b.bind("<<ComboboxSelected>>", lambda e, k=key: self.load_file(k))
            self.file_vars[key], self.file_boxes[key] = v, b
        ttk.Button(top, text="Refresh lists", command=self.refresh_lists).pack(side="left", padx=6)

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=6, pady=4)
        upper = ttk.Frame(pw)
        lower = ttk.Frame(pw)
        pw.add(upper, weight=3)
        pw.add(lower, weight=1)

        hp = ttk.PanedWindow(upper, orient="horizontal")
        hp.pack(fill="both", expand=True)
        opts = ttk.Frame(hp)
        plots = ttk.Frame(hp)
        hp.add(opts, weight=0)
        hp.add(plots, weight=1)
        self._build_options(opts)
        self._build_plots(plots)
        self._build_table(lower)
        self.status = ttk.Label(self, text="", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _build_options(self, f):
        sv = self.settings.get("view", {})
        g = ttk.LabelFrame(f, text="Show")
        g.pack(fill="x", pady=2)
        self.show_etabs = tk.BooleanVar(value=sv.get("etabs", True))
        self.show_abaqus = tk.BooleanVar(value=sv.get("abaqus", True))
        ttk.Checkbutton(g, text="ETABS (solid, filled markers)", variable=self.show_etabs,
                        command=self.schedule_plot).pack(anchor="w")
        ttk.Checkbutton(g, text="Abaqus (dashed, hollow markers)", variable=self.show_abaqus,
                        command=self.schedule_plot).pack(anchor="w")
        self.color_by = tk.StringVar(value=sv.get("color_by", "ds"))
        ttk.Label(g, text="Colour the curves by").pack(anchor="w")
        ttk.Radiobutton(g, text="damage state (panel)", value="ds", variable=self.color_by,
                        command=self.schedule_plot).pack(anchor="w", padx=12)
        ttk.Radiobutton(g, text="axial load", value="load", variable=self.color_by,
                        command=self.schedule_plot).pack(anchor="w", padx=12)
        g = ttk.LabelFrame(f, text="Axis limits (empty = automatic)")
        g.pack(fill="x", pady=2)
        saved = sv.get("limits", {})
        self.lim_vars = {}
        for i, (key, label) in enumerate(LIMITS):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w", padx=2)
            for j, end in enumerate(("lo", "hi")):
                v = tk.StringVar(value=saved.get(f"{key}_{end}", ""))
                e = ttk.Entry(g, textvariable=v, width=7, justify="right")
                e.grid(row=i, column=1 + 2 * j, padx=1, pady=1)
                e.bind("<Return>", lambda ev: self.schedule_plot())
                e.bind("<FocusOut>", lambda ev: self.schedule_plot())
                self.lim_vars[f"{key}_{end}"] = v
            ttk.Label(g, text="to").grid(row=i, column=2)
        ttk.Button(g, text="Auto", width=6, command=self._limits_auto).grid(row=len(LIMITS), column=0, sticky="w",
                                                                             padx=2, pady=2)
        ttk.Label(g, text="drift auto = 1.15 x the Abaqus data", foreground="#666").grid(
            row=len(LIMITS), column=1, columnspan=4, sticky="w")

        g = ttk.LabelFrame(f, text="Failure mechanisms")
        g.pack(fill="x", pady=2)
        self.mech_vars = {}
        for i, key in enumerate(self.mech_keys + [CUSTOM]):
            v = tk.BooleanVar(value=key in sv.get("mechs", self.mech_keys + [CUSTOM]))
            ttk.Checkbutton(g, text=f"{key}  ({mech_marker(key)})", variable=v, command=self._mechs_changed).grid(
                row=i // 2, column=i % 2, sticky="w", padx=2)
            self.mech_vars[key] = v

        g = ttk.LabelFrame(f, text="Damage states")
        g.pack(fill="x", pady=2)
        self.ds_vars = {}
        for i, k in enumerate(dc.STATES):
            v = tk.BooleanVar(value=k in sv.get("states", list(dc.STATES)))
            tk.Checkbutton(g, text=k, variable=v, command=self.schedule_plot, bg=dc.DS_COLORS[k],
                           fg=dc.DS_TEXT_COLORS[k], selectcolor="#808080", activebackground=dc.DS_COLORS[k],
                           width=5, anchor="w").grid(row=i // 3, column=i % 3, padx=2, pady=1)
            self.ds_vars[k] = v

        g = ttk.LabelFrame(f, text="Axial loads  (E = ETABS, A = Abaqus)")
        g.pack(fill="both", expand=True, pady=2)
        b = ttk.Frame(g)
        b.pack(fill="x")
        ttk.Button(b, text="All", width=6, command=lambda: self._loads_all(True)).pack(side="left")
        ttk.Button(b, text="None", width=6, command=lambda: self._loads_all(False)).pack(side="left", padx=2)
        ttk.Button(b, text="ETABS + Abaqus", command=self._loads_both).pack(side="left")
        self.load_frame = ttk.Frame(g)
        self.load_frame.pack(fill="both", expand=True)
        self.load_vars = {}

    def _build_plots(self, f):
        self.nb = ttk.Notebook(f)
        self.nb.pack(fill="both", expand=True)
        self.figs = {}
        for key, title in (("push", "Pushover"), ("mech", "Mechanism vs drift"), ("cmp", "DS drift comparison")):
            fr = ttk.Frame(self.nb)
            self.nb.add(fr, text=f"  {title}  ")
            if key == "mech":
                bar = ttk.Frame(fr)
                bar.pack(fill="x")
                ttk.Label(bar, text="Mechanism:").pack(side="left")
                self.mech_view = tk.StringVar(value=self.settings.get("view", {}).get("mech_view", self.mech_keys[0]))
                for k in self.mech_keys:
                    ttk.Radiobutton(bar, text=k, value=k, variable=self.mech_view,
                                    command=self.schedule_plot).pack(side="left", padx=4)
                self.mech_vs_custom = tk.BooleanVar(value=self.settings.get("view", {}).get("mech_vs_custom", False))
                ttk.Checkbutton(bar, text="Abaqus = the custom criteria", variable=self.mech_vs_custom,
                                command=self.schedule_plot).pack(side="left", padx=(10, 0))
                ttk.Label(bar, text="  hollow marker on an ETABS curve = where Abaqus starts that state (the ETABS "
                                    "value there is a candidate threshold)", foreground="#555").pack(side="left")
            if key == "cmp":
                pw = ttk.PanedWindow(fr, orient="vertical")
                pw.pack(fill="both", expand=True)
                holder = ttk.Frame(pw)
                tab = ttk.Frame(pw)
                pw.add(holder, weight=3)
                pw.add(tab, weight=2)
                cols = (("kN", "Axial [kN]", 75), ("mech", "Mechanism", 110), ("ds", "DS", 45),
                        ("ab", "Abaqus start drift [mm]", 150), ("et", "ETABS start drift [mm]", 150),
                        ("diff", "ETABS - Abaqus [mm]", 140), ("sug", "ETABS link value at the Abaqus start [mm]",
                                                                  260), ("links", "ETABS link(s)", 120))
                self.cmp_tree = ttk.Treeview(tab, columns=[c[0] for c in cols], show="headings", height=8)
                for k, t, w in cols:
                    self.cmp_tree.heading(k, text=t)
                    self.cmp_tree.column(k, width=w, anchor="center")
                sb = ttk.Scrollbar(tab, orient="vertical", command=self.cmp_tree.yview)
                self.cmp_tree.configure(yscrollcommand=sb.set)
                self.cmp_tree.pack(side="left", fill="both", expand=True)
                sb.pack(side="left", fill="y")
                fr = holder
            fig = Figure(figsize=(9, 6))
            canvas = FigureCanvasTkAgg(fig, master=fr)
            tb = NavigationToolbar2Tk(canvas, fr, pack_toolbar=False)
            tb.update()
            tb.pack(fill="x")
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.figs[key] = (fig, canvas)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.schedule_plot())

    def _build_table(self, f):
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(2, 2))
        ttk.Label(bar, text="ETABS acceptance criteria per link (double click to edit; DS values are STARTS, mm; "
                            "one set for all axial loads):").pack(side="left")
        ttk.Label(bar, text="  Name").pack(side="left")
        ttk.Entry(bar, textvariable=self.crit_name, width=28).pack(side="left", padx=4)
        for text, cmd in (("New from run links", self.reset_rows), ("Add row", self.add_row),
                          ("Remove row", self.remove_row), ("Copy thresholds to the same mechanism", self.copy_to_mech),
                          ("Open...", self.open_criteria), ("Save", self.save_criteria),
                          ("Save as...", self.save_criteria_as), ("Export project format...", self.export_project)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.crit_label = ttk.Label(bar, text="", foreground="#555")
        self.crit_label.pack(side="left", padx=6)
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        note = ttk.LabelFrame(body, text="Signs of the thresholds")
        note.pack(side="right", fill="y", padx=(4, 0))
        ttk.Label(note, text=SIGN_NOTE, wraplength=430, justify="left", foreground="#444").pack(
            anchor="nw", padx=4, pady=2)
        self.table = EditableTable(body, COLUMNS, height=8)
        self.table.pack(side="left", fill="both", expand=True)
        self.table.on_change = self._table_changed

    # ------------------------------------------------------------------ helpers
    def say(self, text):
        self.status.configure(text=text)
        self.update_idletasks()

    def _set_dirty(self, on=True):
        self.dirty = on
        name = os.path.basename(self.crit_path) if self.crit_path else "(not saved)"
        self.crit_label.configure(text=name + (" *" if on else ""))

    def _table_changed(self):
        rows = self.table.rows
        for r in rows:
            r["in_run"] = "yes" if (self.etabs and self.etabs.has_link(r["link"])) else "not in run"
            if self.etabs and r["link"] in self.etabs.links:
                r["property"] = self.etabs.links[r["link"]].get("property", "")
            m = dc.mechanism_by_key(self.defaults, r["mechanism"]) or {}
            r["check"] = row_check(r, (m.get("etabs") or {}).get("side"))
        self.table.refresh()
        self._set_dirty()
        self.schedule_plot()

    # ------------------------------------------------------------------ start, lists, loading
    def _start(self):
        self.refresh_lists(first=True)
        last = dc.from_root(self.settings.get("criteria_file", ""))
        if last and os.path.isfile(last):
            try:
                self._read_criteria(last)
            except Exception as e:
                self.say(f"Could not open {last}: {e}")
        if not self.table.rows:
            self.reset_rows(ask=False)
            self._set_dirty(False)
        self.schedule_plot()

    def refresh_lists(self, first=False):
        runs = pc.list_runs(dc.ETABS_RESULTS_DIR)
        self.run_box["values"] = runs
        want = self.settings.get("run") if first else self.run_var.get()
        if runs and (first or self.etabs is None):
            self.run_var.set(want if want in runs else runs[0])
            self.load_run(os.path.join(dc.ETABS_RESULTS_DIR, self.run_var.get()))
        files = dc.list_criteria_files()
        saved = self.settings.get("files", {}) if first else {}
        for key in self.mech_keys + [CUSTOM]:                # custom: every criteria file, whatever its mechanism
            mine = [(p, d) for p, d in files if key == CUSTOM or d.get("mechanism", {}).get("key") == key]
            mine.sort(key=lambda t: (not t[1].get("final"), t[1].get("name", "")))
            disp = {"(none)": None}
            for p, d in mine:
                disp[("★ " if d.get("final") else "") + d.get("name", os.path.basename(p))] = p
            self._file_paths[key] = disp
            self.file_boxes[key]["values"] = list(disp)
            cur = self.file_vars[key].get()
            if first:
                path = dc.from_root(saved.get(key, "")) if saved.get(key) else None
                cur = next((k for k, v in disp.items() if v and path and os.path.normcase(v) ==
                            os.path.normcase(path)), None)
                if cur is None:                           # the first final file, else none
                    cur = next((k for k in disp if k.startswith("★ ")), "(none)")
            if cur not in disp:
                cur = "(none)"
            self.file_vars[key].set(cur)
            self.load_file(key, redraw=False)
        self._update_loads()
        self.schedule_plot()

    def load_run(self, run_dir):
        try:
            self.say(f"Reading {run_dir} ...")
            self.config(cursor="watch")
            self.update()
            self.etabs = EtabsRun(run_dir)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not read the ETABS run:\n{e}")
            self.etabs = None
        finally:
            self.config(cursor="")
        if self.etabs:
            props = sorted({v.get("property", "") for v in self.etabs.links.values()})
            self.say(f"ETABS run '{self.etabs.name}': {len(self.etabs.levels)} axial loads, links "
                     f"{', '.join(sorted(self.etabs.links, key=natural_key))} (properties {', '.join(props)})")
        if self.table.rows:
            self._table_changed()
            self._set_dirty(self.dirty)
        self._update_loads()
        self.schedule_plot()

    def load_file(self, key, redraw=True):
        path = self._file_paths.get(key, {}).get(self.file_vars[key].get())
        if not path:
            self.files.pop(key, None)
        else:
            try:
                f = AbaqusFile(path)
                if key == CUSTOM:
                    f.key = CUSTOM               # plotted as the custom criteria, whatever the mechanism of the file
                    if redraw:                   # chosen now: show it
                        self.mech_vars[CUSTOM].set(True)
                elif f.key != key:
                    raise ValueError(f"the file is for mechanism {f.key}, not {key}")
                self.files[key] = f
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Could not read {path}:\n{e}")
                self.files.pop(key, None)
                self.file_vars[key].set("(none)")
        if redraw:
            self._update_loads()
            self.schedule_plot()

    def _update_loads(self):
        self.loads = group_loads(self.etabs, list(self.files.values()))
        keep = {k: v.get() for k, v in self.load_vars.items()} if self._loads_touched else {}
        saved = set(self.settings.get("view", {}).get("loads", []))
        paired = any(g["etabs"] and g["jobs"] for g in self.loads)
        for w in self.load_frame.winfo_children():
            w.destroy()
        self.load_vars = {}
        for i, g in enumerate(self.loads):
            tag = ("E" if g["etabs"] else "") + ("A" if g["jobs"] else "")
            key = f"{g['kN']:.3f}"
            if key in keep:
                on = keep[key]
            elif saved and not self._loads_touched:          # as in the last session
                on = key in saved
            else:                     # default: loads with ETABS and Abaqus results (every load when none has both)
                on = bool(g["etabs"] and g["jobs"]) if paired else True
            v = tk.BooleanVar(value=on)
            ttk.Checkbutton(self.load_frame, text=f"{dc.fmt_kN(g['kN'])} kN  {tag}", variable=v,
                            command=self._loads_clicked).grid(row=i // 2, column=i % 2, sticky="w", padx=2)
            self.load_vars[key] = v

    def _loads_clicked(self):
        self._loads_touched = True
        self.schedule_plot()

    def _loads_all(self, on):
        for v in self.load_vars.values():
            v.set(on)
        self._loads_clicked()

    def _loads_both(self):
        for g in self.loads:
            self.load_vars[f"{g['kN']:.3f}"].set(bool(g["etabs"] and g["jobs"]))
        self._loads_clicked()

    def _mechs_changed(self):
        self.schedule_plot()

    # ------------------------------------------------------------------ criteria table
    def reset_rows(self, ask=True):
        if ask and self.table.rows and not messagebox.askyesno(
                APP_TITLE, "Replace the table by one row per link of the run (default thresholds)?"):
            return
        links = self.etabs.links if self.etabs else {}
        self.table.set_rows(default_rows(links, self.defaults))
        self._table_changed()

    def add_row(self):
        i = self.table.selected_index()
        base = dict(self.table.rows[i]) if i is not None else new_row("K?", "", self.defaults["mechanisms"][0])
        base["note"] = ""
        self.table.rows.insert((i + 1) if i is not None else len(self.table.rows), base)
        self._table_changed()

    def remove_row(self):
        i = self.table.selected_index()
        if i is not None:
            del self.table.rows[i]
            self._table_changed()

    def copy_to_mech(self):
        i = self.table.selected_index()
        if i is None:
            return
        src = self.table.rows[i]
        n = 0
        for r in self.table.rows:
            if r is not src and r["mechanism"] == src["mechanism"]:
                for k in dc.DS:
                    r[k] = src[k]
                r["provisional"] = src.get("provisional", "")
                n += 1
        self.say(f"Thresholds of {src['link']} {src['mechanism']} copied to {n} row(s)")
        self._table_changed()

    def _criteria_doc(self):
        rows = []
        for r in self.table.get_rows():
            rows.append({"use": bool(r.get("use")), "link": r["link"], "property": r.get("property", ""),
                         "mechanism": r["mechanism"], "component": r["component"], "sense": r["sense"],
                         "relative_to_start": bool(r.get("relative_to_start")),
                         "DS": {k: r.get(k) for k in dc.DS}, "provisional": row_provisional(r),
                         "note": r.get("note", "")})
        return {"format": CRIT_FORMAT, "version": 1, "name": self.crit_name.get().strip(), "units": "mm",
                "modified": dc.now(),
                "explanation": "ETABS link acceptance criteria, one set for all axial loads. DS1 ... DS5 = STARTS "
                               "of the states [mm] (rad for R components): DSk when the value is greater than the "
                               "threshold; null = not defined. value = link deformation (component) with sense: + "
                               "positive, - negative values, abs both; relative_to_start: from step 0.",
                "etabs_run": dc.rel_to_root(self.etabs.dir) if self.etabs else "",
                "abaqus_criteria_files": {k: dc.rel_to_root(f.path) for k, f in self.files.items()},
                "rows": rows}

    def _read_criteria(self, path):
        d = dc.load_json(path)
        if d.get("format") != CRIT_FORMAT:
            raise ValueError("not an ETABS link criteria file")
        rows = []
        for r in d.get("rows", []):
            row = {"use": r.get("use", True), "link": r["link"], "property": r.get("property", ""),
                   "mechanism": r["mechanism"], "component": r.get("component", "U1"), "sense": r.get("sense", "abs"),
                   "relative_to_start": r.get("relative_to_start", False), "provisional": " ".join(r.get("provisional",
                                                                                                         [])),
                   "note": r.get("note", "")}
            row.update({k: r.get("DS", {}).get(k) for k in dc.DS})
            rows.append(row)
        self.table.set_rows(rows)
        self.crit_name.set(d.get("name", os.path.splitext(os.path.basename(path))[0]))
        self.crit_path = path
        self._table_changed()
        self._set_dirty(False)

    def open_criteria(self):
        if not self._confirm_discard():
            return
        p = filedialog.askopenfilename(title="ETABS link criteria", initialdir=dc.ETABS_CRITERIA_DIR,
                                       filetypes=[("ETABS criteria", "*.json")])
        if p:
            try:
                self._read_criteria(p)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Could not open {p}:\n{e}")

    def save_criteria(self):
        if not self.crit_path:
            return self.save_criteria_as()
        bad = self._threshold_problems()
        if bad:
            self.say("Warning: " + "; ".join(bad[:3]))
        dc.save_json(self._criteria_doc(), self.crit_path)
        self._set_dirty(False)
        self.say(f"Saved {self.crit_path}")
        return True

    def save_criteria_as(self):
        name = self.crit_name.get().strip() or "ETABS link criteria"
        p = filedialog.asksaveasfilename(title="Save ETABS link criteria", initialdir=dc.ETABS_CRITERIA_DIR,
                                         initialfile=dc.safe_filename(name) + ".json", defaultextension=".json",
                                         filetypes=[("ETABS criteria", "*.json")])
        if not p:
            return False
        self.crit_path = p
        return self.save_criteria()

    def _threshold_problems(self):
        out = []
        for r in self.table.rows:
            if not dc.thresholds_grow([r.get(k) for k in dc.DS]):
                out.append(f"{r['link']} {r['mechanism']}: thresholds do not grow")
        return out

    def export_project(self):
        """acceptance_criteria.json version 2 of the project (units m), from the first used row of every
        mechanism. Link types per mechanism come from the defaults file (etabs.project_link_types)."""
        rows = [r for r in self.table.get_rows() if r.get("use")]
        lt_out, used = {}, []
        for m in self.defaults["mechanisms"]:
            et = m.get("etabs") or {}
            mine = [r for r in rows if r["mechanism"] == m["key"]]
            if not mine or not et.get("project_link_types"):
                continue
            r = mine[0]
            used.append(f"{m['key']} <- {r['link']} {r['component']}")
            # the project stores sizes (Min = |negative deformation|), so negative thresholds are exported as sizes
            vals = {k: (None if r.get(k) is None else round(abs(r[k]) / 1000.0, 9)) for k in dc.DS}
            prov = row_provisional(r)
            entry = dict(vals, **({"provisional": prov} if prov else {}))
            sides = ["Min", "Max"] if et.get("side", "Min=Max") == "Min=Max" else [et["side"]]
            for lt in et["project_link_types"]:
                t = lt_out.setdefault(lt, {"note": f"ETABS calibration '{self.crit_name.get()}' {dc.now()[:10]}",
                                           "components": {}})
                for comp in et.get("components") or ["U1"]:
                    for s in sides:
                        t["components"].setdefault(comp, {})[s] = dict(entry)
        if not lt_out:
            messagebox.showinfo(APP_TITLE, "No used rows to export")
            return
        data = {"format": "zaprudje-acceptance-criteria", "version": 2, "units": "m",
                "description": "Exported from step 3 (ETABS calibration against Abaqus). One row per mechanism: "
                               + "; ".join(used) + ". Open it in acceptance_criteria_editor.py of the project and "
                               "check it before saving it as acceptance_criteria.json. Link types not covered here "
                               "(e.g. SHEAR_key) are copied from the current file.",
                "link_types": lt_out}
        p = filedialog.asksaveasfilename(title="Export in the project format (version 2, m)",
                                         initialdir=dc.ETABS_CRITERIA_DIR,
                                         initialfile="acceptance_criteria_from_calibration.json",
                                         defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            dc.save_json(data, p, indent=2)
            self.say(f"Exported {p}  ({'; '.join(used)})")

    # ------------------------------------------------------------------ damage states
    def _mechs_on(self):
        return [k for k in self.mech_keys if self.mech_vars[k].get()] + \
            sorted({r["mechanism"] for r in self.table.rows} - set(self.mech_keys) - {CUSTOM}) + \
            ([CUSTOM] if self.mech_vars[CUSTOM].get() else [])

    @staticmethod
    def _panel_mechs(mechs):
        """Mechanisms of the panel's damage state: the ticked ones without the custom criteria (it is compared with
        them), unless it is the only one ticked."""
        return [m for m in mechs if m != CUSTOM] or list(mechs)

    def etabs_states(self, lv, mechs):
        """{mech: {"starts": {DS: drift}, "rows": [(row, values, starts)]}, "codes": combined per step}."""
        drift = self.etabs.drift[lv.axial_kN]
        out = {}
        codes = np.zeros(drift.shape, dtype=int)
        for r in self.table.rows:
            if not r.get("use") or r["mechanism"] not in mechs:
                continue
            v = self.etabs.values(lv, r)
            if v is None:
                continue
            n = min(len(v), len(drift))
            thr = row_thresholds(r)
            starts = {k: dc.drift_at_value(drift[:n], v[:n], thr[k]) for k in dc.DS}
            m = out.setdefault(r["mechanism"], {"starts": {k: None for k in dc.DS}, "rows": []})
            m["rows"].append((r, v[:n], starts))
            for k in dc.DS:
                if starts[k] is not None and (m["starts"][k] is None or starts[k] < m["starts"][k]):
                    m["starts"][k] = starts[k]
            c = dc.ds_codes_from_values(v[:n], thr)
            codes[:n] = np.maximum(codes[:n], c)
        out["codes"] = np.maximum.accumulate(codes) if codes.size else codes
        return out

    def abaqus_states(self, job_files, mechs):
        """Combined start drifts of the selected mechanisms for one Abaqus job: {DS: drift}."""
        starts = {k: None for k in dc.DS}
        for f, job in job_files:
            if f.key not in mechs:
                continue
            for k, d in f.starts(job).items():
                if d is not None and (starts[k] is None or d < starts[k]):
                    starts[k] = d
        return starts

    def _visible_loads(self):
        return [g for g in self.loads if self.load_vars.get(f"{g['kN']:.3f}") and
                self.load_vars[f"{g['kN']:.3f}"].get()]

    def _abaqus_jobs(self, g):
        """{(db root, job): [(file, job)]} of a load group (the same job can be in several files)."""
        out = {}
        for f, job in g["jobs"]:
            out.setdefault((f.db.root, job), []).append((f, job))
        return out

    # ------------------------------------------------------------------ plots
    def schedule_plot(self):
        if self._plot_job:
            self.after_cancel(self._plot_job)
        self._plot_job = self.after(150, self.plot)

    def plot(self):
        self._plot_job = None
        tab = ("push", "mech", "cmp")[self.nb.index("current")]
        try:
            {"push": self.plot_pushover, "mech": self.plot_mechanism, "cmp": self.plot_comparison}[tab]()
        except Exception as e:
            traceback.print_exc()
            self.say(f"Plot error: {e}")

    def _limits(self, key):
        return dc.limit_pair(self.lim_vars[f"{key}_lo"].get(), self.lim_vars[f"{key}_hi"].get())

    def _limits_auto(self):
        for v in self.lim_vars.values():
            v.set("")
        self.schedule_plot()

    def _drift_limits(self, groups):
        """(lo, hi) of the drift axis: the user's limits, else 0 ... 1.15 x the Abaqus data (the ETABS push runs
        on to the end of its displacement window, far beyond the Abaqus range)."""
        lo, hi = self._limits("drift")
        if hi is None:
            ends = []
            for g in groups:
                for f, job in g["jobs"]:
                    c = f.curves.get(job)
                    if c is not None:
                        ends.append(c["end_drift"])
            hi = 1.15 * max(ends) if ends else 40.0
        if lo is None:
            lo = -0.01 * hi
        return lo, hi

    def _ds_line(self, ax, x, y, codes, ls, lw, states_on, alpha=1.0):
        pts = np.column_stack([x, y])
        ok = np.all(np.isfinite(pts), axis=1)
        if len(pts) < 2:
            return
        seg = np.stack([pts[:-1], pts[1:]], axis=1)
        keep = ok[:-1] & ok[1:]
        cols = [dc.DS_COLORS[dc.STATES[min(int(c), 5)]] if dc.STATES[min(int(c), 5)] in states_on else GREY
                for c in codes[1:len(pts)]]
        lc = LineCollection(seg[keep], colors=np.array(cols, dtype=object)[keep], linewidths=lw, linestyles=ls,
                            alpha=alpha)
        ax.add_collection(lc)
        ax.update_datalim(pts[ok])

    def _style(self, ax, xlabel, ylabel, title=None):
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        if title:
            ax.set_title(title, fontsize=10)
        ax.grid(True, color="#e3e3e3", lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)

    def _legend(self, ax, mechs, states_on, extra=(), mech_lines=False):
        """mech_lines: the line style marks the mechanism and the marker fill ETABS / Abaqus (DS drift comparison);
        otherwise the line style marks ETABS / Abaqus."""
        h = [] if mech_lines else [Line2D([], [], color="#404040", ls="-", lw=2, label="ETABS"),
                                   Line2D([], [], color="#404040", ls="--", lw=1.5, label="Abaqus")]
        h += [Line2D([], [], marker=mech_marker(m), ls=mech_linestyle(m) if mech_lines else "",
                     lw=1.6, color="#808080", mec="black", ms=8, label=m + (" (Abaqus)" if m == CUSTOM else ""))
              for m in mechs]
        h += [Line2D([], [], marker="o", ls="", color=dc.DS_COLORS[k], mec="black", ms=7, label=k)
              for k in dc.DS if k in states_on]
        etabs, abaqus = ("ETABS (filled)", "Abaqus (hollow)") if mech_lines else ("ETABS start", "Abaqus start")
        h += [Line2D([], [], marker="o", ls="", mfc="none", mec="#404040", mew=2, ms=7, label=abaqus),
              Line2D([], [], marker="o", ls="", color="#404040", mec="black", ms=7, label=etabs)]
        h += list(extra)
        ax.legend(handles=h, fontsize=7, ncol=1, loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0,
                  handlelength=6 if mech_lines else 2)

    def plot_pushover(self):
        fig, canvas = self.figs["push"]
        fig.clear()
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=0.07, right=0.87, top=0.94, bottom=0.08)
        groups = self._visible_loads()
        mechs = self._mechs_on()
        states_on = {k for k in dc.STATES if self.ds_vars[k].get()}
        by_ds = self.color_by.get() == "ds"
        xl = self._drift_limits(groups)
        load_handles = []
        for g in groups:
            col = dc.load_color(g["kN"])
            load_handles.append(Line2D([], [], color=col, lw=2, label=f"{dc.fmt_kN(g['kN'])} kN"))
            if self.show_etabs.get() and g["etabs"] is not None and self.etabs:
                lv = g["etabs"]
                x, y = self.etabs.drift[lv.axial_kN], self.etabs.force[lv.axial_kN]
                st = self.etabs_states(lv, mechs)
                if by_ds:
                    self._ds_line(ax, x, y, st["codes"], "-", 2.4, states_on)
                else:
                    ax.plot(x, y, "-", color=col, lw=1.8)
                for m in mechs:
                    for k in dc.DS:
                        d = st.get(m, {}).get("starts", {}).get(k)
                        if d is None or k not in states_on:
                            continue
                        h = dc.value_at_drift(x, y, d)
                        if h is not None:
                            ax.plot(d, h, marker=mech_marker(m), ls="", color=dc.DS_COLORS[k], mec="black",
                                    mew=0.8, ms=9, zorder=6)
            if self.show_abaqus.get():
                for (_root, job), jf in self._abaqus_jobs(g).items():
                    f = jf[0][0]
                    try:
                        x, y = dc.pushover_curve(f.db, job, f.pushover)
                    except Exception:
                        continue
                    if by_ds:
                        starts = self.abaqus_states(jf, self._panel_mechs(mechs))
                        self._ds_line(ax, x, y, dc.ds_codes_from_starts(x, starts), "--", 1.6, states_on)
                    else:
                        ax.plot(x, y, "--", color=col, lw=1.4)
                    for ff, jj in jf:
                        if ff.key not in mechs:
                            continue
                        for k, d in ff.starts(jj).items():
                            if d is None or k not in states_on:
                                continue
                            h = dc.value_at_drift(x, y, d)
                            if h is not None:
                                big = ff.key == CUSTOM                     # a star needs more size to read
                                ax.plot(d, h, marker=mech_marker(ff.key), ls="", mfc="none",
                                        mec=dc.DS_COLORS[k], mew=1.6 if big else 2.0, ms=14 if big else 10, zorder=7)
        dc.apply_limits(ax, xl, self._limits("force"))
        self._style(ax, "Drift [mm]   (ETABS: average U2 of the top nodes; Abaqus: RU2 - RU1)",
                    "Horizontal force [kN]   (ETABS: bottom shear links; Abaqus: RU1 Fx)",
                    f"Pushover - ETABS run '{self.etabs.name if self.etabs else '-'}'; damage states of the "
                    f"mechanisms {', '.join(mechs) or '-'}")
        if not by_ds and load_handles:
            ax.add_artist(ax.legend(handles=load_handles, fontsize=7, loc="lower left", bbox_to_anchor=(1.01, 0.0),
                                    borderaxespad=0))
        self._legend(ax, mechs, states_on)
        if not groups:
            ax.text(0.5, 0.5, "Tick axial loads on the left", ha="center", va="center", transform=ax.transAxes,
                    color="#888")
        canvas.draw_idle()

    def plot_mechanism(self):
        fig, canvas = self.figs["mech"]
        fig.clear()
        ax1 = fig.add_subplot(211)
        ax2 = fig.add_subplot(212, sharex=ax1)
        fig.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.08, hspace=0.25)
        key = self.mech_view.get()
        groups = self._visible_loads()
        states_on = {k for k in dc.STATES if self.ds_vars[k].get()}
        xl = self._drift_limits(groups)
        rows = [r for r in self.table.rows if r.get("use") and r["mechanism"] == key]
        vs_custom = self.mech_vs_custom.get()
        f = self.files.get(CUSTOM if vs_custom else key)
        ab_name = "the custom criteria" if vs_custom else "Abaqus"
        link_styles = {}
        styles = ["-", "--", "-.", ":"]
        for g in groups:
            col = dc.load_color(g["kN"])
            if self.show_etabs.get() and g["etabs"] is not None and self.etabs:
                lv = g["etabs"]
                x = self.etabs.drift[lv.axial_kN]
                ab_starts = {}
                if f is not None:
                    for ff, job in g["jobs"]:
                        if ff is f:
                            ab_starts = f.starts(job)
                            break
                for r in rows:
                    v = self.etabs.values(lv, r)
                    if v is None:
                        continue
                    n = min(len(v), len(x))
                    ls = link_styles.setdefault(r["link"] + r["component"], styles[len(link_styles) % 4])
                    ax1.plot(x[:n], v[:n], ls, color=col, lw=1.5)
                    thr = row_thresholds(r)
                    for k in dc.DS:
                        if k not in states_on:
                            continue
                        d = dc.drift_at_value(x[:n], v[:n], thr[k])
                        if d is not None:
                            ax1.plot(d, thr[k], "o", color=dc.DS_COLORS[k], mec="black", ms=7, zorder=6)
                        da = ab_starts.get(k)
                        if da is not None:
                            va = dc.value_at_drift(x[:n], v[:n], da)
                            if va is not None:
                                ax1.plot(da, va, "o", mfc="none", mec=dc.DS_COLORS[k], mew=2, ms=9, zorder=7)
            if self.show_abaqus.get() and f is not None:
                for ff, job in g["jobs"]:
                    if ff is not f:
                        continue
                    c = f.curves.get(job)
                    if c is None:
                        continue
                    ax2.plot(c["drift"], c["value"], "--", color=col, lw=1.4)
                    for k in dc.DS:
                        e = f.rows[job]["DS"].get(k) or {}
                        if k in states_on and e.get("drift") is not None and e.get("disp") is not None:
                            ax2.plot(e["drift"], e["disp"], "o", mfc="none", mec=dc.DS_COLORS[k], mew=2, ms=9,
                                     zorder=7)
        # thresholds of the ETABS rows (each distinct value once)
        seen = set()
        for r in rows:
            for k in dc.DS:
                t = r.get(k)
                if t is None or k not in states_on or (k, t) in seen:
                    continue
                seen.add((k, t))
                ax1.axhline(t, color=dc.DS_COLORS[k], ls=":", lw=1.0, zorder=1, gid="nofit")
                ax1.annotate(f"{k} {t:g}", xy=(1.0, t), xycoords=("axes fraction", "data"), xytext=(-4, 2),
                             textcoords="offset points", ha="right", fontsize=7, color="#333",
                             annotation_clip=True)
        dc.apply_limits(ax1, xl, self._limits("etabs"))
        dc.apply_limits(ax2, xl, self._limits("abaqus"))
        links = ", ".join(sorted({f"{r['link']} {r['component']} ({r['sense']})" for r in rows}, key=natural_key))
        self._style(ax1, "", f"ETABS {key} link value [mm]",
                    f"{key}: ETABS links {links or '-'}  (filled = ETABS start, hollow = start of {ab_name})")
        self._style(ax2, "Drift [mm]", dc.value_axis_label(f.mech) if f else f"Abaqus {key} [mm]",
                    (f"Abaqus{' (custom criteria)' if vs_custom else ''}: {f.name}") if f else
                    "Abaqus: no custom criteria chosen" if vs_custom else
                    f"Abaqus: no criteria file chosen for {key}")
        h = [Line2D([], [], color=dc.load_color(g["kN"]), lw=2, label=f"{dc.fmt_kN(g['kN'])} kN") for g in groups]
        h += [Line2D([], [], color="#404040", ls=s, label=k[:-2] + " " + k[-2:]) for k, s in link_styles.items()]
        if h:
            ax1.legend(handles=h, fontsize=7, ncol=3, loc="upper left")
        canvas.draw_idle()

    def _suggestion(self, g, st, m, drift):
        """(ETABS link value of mechanism m at this drift, its links): the worst link reaches the state first."""
        if not st.get(m) or drift is None:
            return None, ""
        vals = []
        for r, v, _s in st[m]["rows"]:
            x = self.etabs.drift[g["etabs"].axial_kN][:len(v)]
            val = dc.value_at_drift(x, v, drift)
            if val is not None:
                vals.append(val)
        return (max(vals) if vals else None), ", ".join(r["link"] for r, _v, _s in st[m]["rows"])

    def comparison_rows(self):
        """[(kN, mech, DS, abaqus drift, etabs drift, suggestion, links)] for the visible loads / mechanisms /
        states, plus the panel (all ticked mechanisms together). The custom criteria has no ETABS rows of its own:
        it is compared with every ticked mechanism and the panel ('Custom vs AX-' ...)."""
        out = []
        mechs = self._mechs_on()
        own = [m for m in mechs if m != CUSTOM]
        panel = self._panel_mechs(mechs)
        states = [k for k in dc.DS if self.ds_vars[k].get()]

        def etabs_start(st, m, k):
            ms = panel if m == "panel" else [m]
            cands = [st[x]["starts"][k] for x in ms if x in st and st[x]["starts"][k] is not None]
            return min(cands) if cands else None

        for g in self._visible_loads():
            st = self.etabs_states(g["etabs"], mechs) if (g["etabs"] is not None and self.etabs) else {}
            for m in own + (["panel"] if own else []):
                ab = self.abaqus_states(g["jobs"], panel if m == "panel" else [m]) if g["jobs"] else {}
                for k in states:
                    et = etabs_start(st, m, k)
                    sug, links = (None, "") if m == "panel" else self._suggestion(g, st, m, ab.get(k))
                    if et is None and ab.get(k) is None:
                        continue
                    out.append((g["kN"], m, k, ab.get(k), et, sug, links))
            if CUSTOM not in mechs:
                continue
            cu = self.abaqus_states(g["jobs"], [CUSTOM]) if g["jobs"] else {}
            for m in (own + ["panel"]) if own else [None]:
                for k in states:
                    et = etabs_start(st, m, k) if m else None
                    sug, links = (None, "") if m in (None, "panel") else self._suggestion(g, st, m, cu.get(k))
                    if et is None and cu.get(k) is None:
                        continue
                    out.append((g["kN"], f"{CUSTOM} vs {m}" if m else CUSTOM, k, cu.get(k), et, sug, links))
        return out

    def plot_comparison(self):
        fig, canvas = self.figs["cmp"]
        fig.clear()
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=0.07, right=0.87, top=0.93, bottom=0.12)
        rows = self.comparison_rows()
        mechs = self._mechs_on()
        states_on = {k for k in dc.STATES if self.ds_vars[k].get()}
        series = {}
        for kN, m, k, ab, et, _s, _l in rows:
            if m == "panel":
                continue
            custom = m.startswith(CUSTOM)                # 'Custom vs AX-' ...: the custom (Abaqus) line once
            s = series.setdefault((CUSTOM if custom else m, k), {"ab": set(), "et": set()})
            if ab is not None and self.show_abaqus.get():
                s["ab"].add((kN, ab))
            if et is not None and self.show_etabs.get() and not custom:
                s["et"].add((kN, et))
        for (m, k), s in series.items():
            mk, col, ls = mech_marker(m), dc.DS_COLORS[k], mech_linestyle(m)
            if s["et"]:
                ax.plot(*zip(*sorted(s["et"])), ls=ls, marker=mk, color=col, mec="black", ms=8, lw=1.6)
            if s["ab"]:
                big = m == CUSTOM
                ax.plot(*zip(*sorted(s["ab"])), ls=ls, marker=mk, color=col, mfc="none", mec=col,
                        mew=1.6 if big else 2, ms=14 if big else 10, lw=1.2 if big else 0.9, alpha=0.8)
        ticks = sorted({r[0] for r in rows})
        if ticks:
            ax.set_xscale("log")
            ax.set_xticks(ticks)
            ax.set_xticklabels([dc.fmt_kN(t) for t in ticks], rotation=90, fontsize=8)
            ax.minorticks_off()
        self._style(ax, "Axial load [kN]", "Drift where the state starts [mm]",
                    "Damage-state starts per mechanism: ETABS (filled) vs Abaqus (hollow)")
        self._legend(ax, mechs, states_on, mech_lines=True)
        lo, hi = self._limits("start")
        if lo is None and hi is None:                    # default: the drift range of the other views
            hi = self._drift_limits(self._visible_loads())[1]
            lo = -0.02 * hi
            beyond = sum(1 for r in rows if r[1] != "panel" and not r[1].startswith(CUSTOM) and r[4] is not None
                         and r[4] > hi)
            if beyond and self.show_etabs.get():
                ax.text(0.01, 0.98, f"{beyond} ETABS start(s) beyond {hi:.3g} mm (in the table; set "
                                    f"'Start drift' limits to see them)", transform=ax.transAxes, va="top",
                        fontsize=8, color="#555")
        dc.apply_limits(ax, ylim=(lo, hi))
        canvas.draw_idle()
        t = self.cmp_tree
        t.delete(*t.get_children())
        for kN, m, k, ab, et, sug, links in rows:
            diff = (et - ab) if (et is not None and ab is not None) else None
            t.insert("", "end", values=(dc.fmt_kN(kN), m, k, dc.fmt(ab, 4), dc.fmt(et, 4), dc.fmt(diff, 3),
                                        dc.fmt(sug, 4), links))

    # ------------------------------------------------------------------ close
    def _confirm_discard(self):
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_TITLE, "Save the ETABS link criteria?")
        if ans is None:
            return False
        return self.save_criteria() if ans else True

    def _close(self):
        if not self._confirm_discard():
            return
        s = {"geometry": self.geometry(), "run": self.run_var.get(),
             "files": {k: dc.rel_to_root(f.path) for k, f in self.files.items()},
             "criteria_file": dc.rel_to_root(self.crit_path or ""),
             "view": {"etabs": self.show_etabs.get(), "abaqus": self.show_abaqus.get(), "color_by": self.color_by.get(),
                      "limits": {k: v.get() for k, v in self.lim_vars.items()},
                      "mechs": [k for k, v in self.mech_vars.items() if v.get()],
                      "states": [k for k, v in self.ds_vars.items() if v.get()],
                      "loads": [k for k, v in self.load_vars.items() if v.get()], "mech_view": self.mech_view.get(),
                      "mech_vs_custom": self.mech_vs_custom.get()}}
        try:
            dc.save_json(s, SETTINGS_FILE)
        except OSError:
            pass
        self.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()

    def report(exc, val, tb):
        traceback.print_exception(exc, val, tb)
        messagebox.showerror(APP_TITLE, f"{exc.__name__}: {val}")

    app.report_callback_exception = report
    app.mainloop()


if __name__ == "__main__":
    main()
