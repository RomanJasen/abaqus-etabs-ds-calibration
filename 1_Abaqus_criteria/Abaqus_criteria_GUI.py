#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 1 - Abaqus acceptance criteria per mechanism
=================================================

For one mechanism (SH, AX+, AX-, REB or your own), defined by two sections of the Abaqus panel model, and one
load-case group (a name filter on the jobs, e.g. SHEAR_KEY_S_TIE or NOKEY):

    chart 1   pushover curves: drift (RU2 - RU1) vs force at RU1, every job of the group
    chart 2   displacement between the two sections vs drift
    table     per job (axial load) and damage state DS1 ... DS5: the drift where the state starts and the section
              displacement at that drift. Type either one, the other one follows from the curve of that job.
              Untouched cells take the default threshold of the file (the drift is found on the curve).

Damage states are STARTS (lower bounds): DSk when the value is greater than the DSk threshold; below DS1 = DS0.

Tab 2 (optional) compares saved criteria files: start drifts and displacements vs axial load.

Input   0_INPUT/Abaqus_databases/<database>/<SET>/<JOB>/*.txt     (Panel_results databases)
        defaults/default_mechanisms.json                           (section pairs, initial thresholds, groups)
Output  criteria_files/<name>.json                                 (one file per mechanism, location and group;
                                                                     mark the ones step 3 should use as final)
Run:    python Abaqus_criteria_GUI.py
"""

import copy
import os
import sys
import traceback
import warnings

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.figure import Figure                                              # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.lines import Line2D                                                # noqa: E402
from matplotlib import patheffects                                                 # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_library"))
import ds_common as dc                                                             # noqa: E402

APP_TITLE = "Step 1 - Abaqus acceptance criteria"
SETTINGS_FILE = os.path.join(HERE, "gui_settings.json")

BG_INPUT = "#FFF2CC"          # typed by the user
BG_COMPUTED = "#E2EFDA"       # follows from the curve
BG_DEFAULT = "#FFFFFF"        # default threshold of the file
BG_OFF = "#F2F2F2"            # not defined / not reached
BG_SELECTED_ROW = "#DDEBF7"
FG_PROVISIONAL = "#C55A11"


def short_family(fam):
    return fam.replace("_EX", "").replace("_S_", "_").replace("_", " ")


def load_settings():
    try:
        return dc.load_json(SETTINGS_FILE)
    except (OSError, ValueError):
        return {}


def open_path(path):
    if path and os.path.exists(path):
        os.startfile(path)                                    # noqa: S606


def criteria_summary(d):
    """Thresholds and start drifts per damage state: the overview at the end of a criteria file."""
    return {k: {"thresholds_mm": d["mechanism"]["thresholds"].get(k),
                "start_drift_mm_by_axial_kN": {dc.fmt_kN(r["axial_kN"]) + " " + r["family"]: r["DS"][k].get("drift")
                                               for r in d["rows"]}}
            for k in dc.DS}


# =====================================================================================================
# SMALL WIDGETS
# =====================================================================================================

class ScrollFrame(ttk.Frame):
    """Frame with scroll bars; put widgets into .inner."""

    def __init__(self, master, height=300, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, height=height)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.inner = tk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class FlowChecks(ttk.Frame):
    """Check buttons in rows of `per_row`."""

    def __init__(self, master, per_row=8):
        super().__init__(master)
        self.per_row = per_row
        self.vars = {}

    def set_items(self, items, command, keep=None):
        """items: [(key, label)]; keep: {key: bool} previous states."""
        for w in self.winfo_children():
            w.destroy()
        self.vars = {}
        for i, (key, label) in enumerate(items):
            v = tk.BooleanVar(value=(keep or {}).get(key, True))
            ttk.Checkbutton(self, text=label, variable=v, command=command).grid(
                row=i // self.per_row, column=i % self.per_row, sticky="w", padx=(0, 6))
            self.vars[key] = v

    def checked(self):
        return {k for k, v in self.vars.items() if v.get()}

    def set_all(self, on):
        for v in self.vars.values():
            v.set(on)


# =====================================================================================================
# THE APPLICATION
# =====================================================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.settings = load_settings()
        self.geometry(self.settings.get("geometry", "1650x980"))
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.defaults_path = dc.from_root(self.settings.get("defaults_file", "")) or dc.DEFAULTS_FILE
        try:
            self.defaults = dc.load_defaults(self.defaults_path)
        except Exception:
            self.defaults_path = dc.DEFAULTS_FILE
            self.defaults = dc.load_defaults(self.defaults_path)

        self.db = None
        self.doc = None
        self.path = None
        self.dirty = False
        self.curves = {}                 # job -> curves (dc.job_curves)
        self.sel_row = None
        self.sel_ds = "DS1"
        self.cells = {}                  # (row index, DS, "drift"/"disp") -> (Entry, StringVar)
        self.row_widgets = {}            # row index -> [label widgets]
        self._plot_job = None
        self._loading = False
        self._db_paths = {}
        # what the top chart plots (presets; last session, else the defaults file)
        self.top = copy.deepcopy(self.settings.get("top_chart") or self.defaults["top_chart"])
        if not self.top.get("presets"):
            self.top = copy.deepcopy(self.defaults["top_chart"])
        self._top_cache = {}             # (database, job, expression) -> array
        self._top_x_is_drift = True      # of the last plot (for clicks on the top chart)
        self.top_dialog = None
        self.envelope = None

        self._build()
        self._open_start()

    # ------------------------------------------------------------------ layout
    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        t1 = ttk.Frame(nb)
        t2 = ttk.Frame(nb)
        nb.add(t1, text="  1  Define criteria  ")
        nb.add(t2, text="  2  Compare files (optional)  ")
        self._build_define(t1)
        self.compare = CompareTab(t2, self)
        nb.bind("<<NotebookTabChanged>>", lambda e: self.compare.refresh() if nb.index("current") == 1 else None)
        self.status = ttk.Label(self, text="", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _build_define(self, parent):
        # file bar
        fb = ttk.Frame(parent)
        fb.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(fb, text="Criteria file:").pack(side="left")
        self.name_var = tk.StringVar()
        e = ttk.Entry(fb, textvariable=self.name_var, width=48)
        e.pack(side="left", padx=4)
        e.bind("<KeyRelease>", lambda ev: self._set_dirty())
        ttk.Button(fb, text="Auto name", command=self.auto_name).pack(side="left", padx=(0, 4))
        self.final_var = tk.BooleanVar()
        ttk.Checkbutton(fb, text="Final (use in step 3)", variable=self.final_var,
                        command=self._set_dirty).pack(side="left", padx=6)
        for text, cmd in (("New", self.new_file), ("Open...", self.open_file), ("Save", self.save),
                          ("Save as...", self.save_as), ("Folder", lambda: open_path(dc.CRITERIA_DIR))):
            ttk.Button(fb, text=text, command=cmd).pack(side="left", padx=2)
        self.path_label = ttk.Label(fb, text="", foreground="#555")
        self.path_label.pack(side="left", padx=8)

        # database and group
        gb = ttk.Frame(parent)
        gb.pack(fill="x", padx=6, pady=2)
        ttk.Label(gb, text="Abaqus database:").pack(side="left")
        self.db_var = tk.StringVar()
        self.db_box = ttk.Combobox(gb, textvariable=self.db_var, width=40, state="readonly")
        self.db_box.pack(side="left", padx=4)
        self.db_box.bind("<<ComboboxSelected>>", lambda e: self.set_database(self._db_paths.get(self.db_var.get())))
        ttk.Button(gb, text="Browse...", command=self._browse_db).pack(side="left")
        ttk.Separator(gb, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(gb, text="Load-case group:").pack(side="left")
        self.group_name_var = tk.StringVar()
        ttk.Entry(gb, textvariable=self.group_name_var, width=18).pack(side="left", padx=4)
        ttk.Label(gb, text="name filter:").pack(side="left")
        self.group_filter_var = tk.StringVar()
        fe = ttk.Entry(gb, textvariable=self.group_filter_var, width=26)
        fe.pack(side="left", padx=4)
        fe.bind("<Return>", lambda e: self.apply_group())
        self.preset_box = ttk.Combobox(gb, width=24, state="readonly")
        self.preset_box.pack(side="left", padx=4)
        self.preset_box.bind("<<ComboboxSelected>>", self._preset_selected)
        ttk.Button(gb, text="Apply group", command=self.apply_group).pack(side="left", padx=2)
        self.group_info = ttk.Label(gb, text="", foreground="#555")
        self.group_info.pack(side="left", padx=6)
        ttk.Label(parent, text="Filter: parts separated by spaces must all be in the job name; 'A|B' = either; "
                               "'-BEND' = must not contain. Example: 'SHEAR NOKEY', 'TIE|FR05 -BEND'.",
                  foreground="#666").pack(fill="x", padx=8)

        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=4)
        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)
        self._build_mechanism(left)
        self._build_table(left)
        self._build_plot(right)

    def _build_mechanism(self, parent):
        mf = ttk.LabelFrame(parent, text="Mechanism: displacement of section B relative to section A")
        mf.pack(fill="x", pady=(0, 4))
        r0 = ttk.Frame(mf)
        r0.pack(fill="x", padx=4, pady=2)
        ttk.Label(r0, text="Mechanism").pack(side="left")
        self.mech_var = tk.StringVar()
        self.mech_box = ttk.Combobox(r0, textvariable=self.mech_var, width=8)
        self.mech_box.pack(side="left", padx=4)
        self.mech_box.bind("<<ComboboxSelected>>", lambda e: self.change_mechanism())
        self.mech_box.bind("<Return>", lambda e: self.change_mechanism())
        ttk.Label(r0, text="Location").pack(side="left", padx=(8, 0))
        self.loc_var = tk.StringVar()
        self.loc_box = ttk.Combobox(r0, textvariable=self.loc_var, width=38, state="readonly")
        self.loc_box.pack(side="left", padx=4)
        self.loc_box.bind("<<ComboboxSelected>>", lambda e: self.change_location())
        # the locations offered for a mechanism are presets of the defaults file ("locations" of each mechanism)
        self.loc_buttons = {}
        for text, cmd in (("Save as new location...", self.save_location), ("Rename...", self.rename_location),
                          ("Delete", self.delete_location)):
            self.loc_buttons[text] = ttk.Button(r0, text=text, command=cmd)
            self.loc_buttons[text].pack(side="left", padx=1)

        r1 = ttk.Frame(mf)
        r1.pack(fill="x", padx=4, pady=2)
        self.sec_a_var, self.sec_b_var = tk.StringVar(), tk.StringVar()
        self.comp_var, self.sense_var = tk.StringVar(), tk.StringVar()
        self.rel_var = tk.BooleanVar()
        ttk.Label(r1, text="Section A").pack(side="left")
        self.sec_a_box = ttk.Combobox(r1, textvariable=self.sec_a_var, width=19, state="readonly")
        self.sec_a_box.pack(side="left", padx=3)
        ttk.Label(r1, text="Section B").pack(side="left")
        self.sec_b_box = ttk.Combobox(r1, textvariable=self.sec_b_var, width=19, state="readonly")
        self.sec_b_box.pack(side="left", padx=3)
        ttk.Label(r1, text="Comp.").pack(side="left")
        ttk.Combobox(r1, textvariable=self.comp_var, values=dc.COMPONENTS, width=4, state="readonly").pack(
            side="left", padx=3)
        ttk.Label(r1, text="Sense").pack(side="left")
        ttk.Combobox(r1, textvariable=self.sense_var, values=("+", "-", "abs"), width=4, state="readonly").pack(
            side="left", padx=3)
        ttk.Checkbutton(r1, text="from zero drift", variable=self.rel_var,
                        command=self.change_definition).pack(side="left", padx=3)
        for w in r1.winfo_children():
            if isinstance(w, ttk.Combobox):
                w.bind("<<ComboboxSelected>>", lambda e: self.change_definition())
        self.value_label = ttk.Label(mf, text="", foreground="#2F5597")
        self.value_label.pack(fill="x", padx=6)
        ttk.Label(mf, text="Sense: + value as is, - value negated, abs |value|. Values from absolute 0 unless 'from "
                           "zero drift'. A positive threshold is reached when the value rises above it, a negative "
                           "one when it drops below it. Locations are presets of the defaults file (below).",
                  foreground="#666", wraplength=640).pack(
            fill="x", padx=6, pady=(0, 2))

        tf = ttk.LabelFrame(parent, text="Default thresholds of this file [mm]: starts of DS1 ... DS5 "
                                         "(empty = not defined; prov. = provisional, a label only)")
        tf.pack(fill="x", pady=(0, 4))
        g = ttk.Frame(tf)
        g.pack(fill="x", padx=4, pady=2)
        self.thr_vars, self.prov_vars = {}, {}
        for j, k in enumerate(dc.DS):
            tk.Label(g, text=k, bg=dc.DS_COLORS[k], fg=dc.DS_TEXT_COLORS[k], width=9).grid(row=0, column=j, padx=1)
            v = tk.StringVar()
            e = ttk.Entry(g, textvariable=v, width=9, justify="center")
            e.grid(row=1, column=j, padx=1)
            e.bind("<Return>", lambda ev: self.change_thresholds())
            e.bind("<FocusOut>", lambda ev: self.change_thresholds())
            pv = tk.BooleanVar()
            ttk.Checkbutton(g, text="prov.", variable=pv, command=self.change_thresholds).grid(row=2, column=j)
            self.thr_vars[k], self.prov_vars[k] = v, pv
        bf = ttk.Frame(g)
        bf.grid(row=0, column=6, rowspan=3, padx=(10, 0), sticky="w")
        ttk.Button(bf, text="Reset all rows to these", command=self.reset_rows).grid(row=0, column=0, sticky="ew")
        ttk.Button(bf, text="Load defaults file...", command=self.load_defaults_file).grid(row=1, column=0,
                                                                                          sticky="ew")
        ttk.Button(bf, text="Save as default...", command=self.save_as_default).grid(row=2, column=0, sticky="ew")
        self.defaults_label = ttk.Label(tf, text="", foreground="#666")
        self.defaults_label.pack(fill="x", padx=6)

    def _build_table(self, parent):
        tf = ttk.LabelFrame(parent, text="Criteria per axial load: type a drift OR a displacement "
                                         "(Enter); empty = default, '-' = not defined")
        tf.pack(fill="both", expand=True)
        self.table = ScrollFrame(tf, height=330)
        self.table.pack(fill="both", expand=True)
        lg = ttk.Frame(tf)
        lg.pack(fill="x", padx=4, pady=2)
        for text, bg in (("typed", BG_INPUT), ("from the curve", BG_COMPUTED), ("default threshold", BG_DEFAULT),
                         ("'-' not defined / n.r. = never exceeded before 'valid to'", BG_OFF)):
            tk.Label(lg, text=f" {text} ", bg=bg, relief="solid", bd=1).pack(side="left", padx=2)
        tk.Label(lg, text=" orange = provisional ", fg=FG_PROVISIONAL).pack(side="left", padx=2)
        nf = ttk.Frame(tf)
        nf.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(nf, text="Notes:").pack(side="left")
        self.notes_var = tk.StringVar()
        ne = ttk.Entry(nf, textvariable=self.notes_var)
        ne.pack(side="left", fill="x", expand=True, padx=4)
        ne.bind("<KeyRelease>", lambda ev: self._set_dirty())

    def _build_plot(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Click on a chart sets the selected cell's").pack(side="left")
        self.click_var = tk.StringVar(value=self.settings.get("click", "drift"))
        ttk.Radiobutton(top, text="drift", value="drift", variable=self.click_var).pack(side="left")
        ttk.Radiobutton(top, text="displacement (lower chart)", value="disp", variable=self.click_var).pack(
            side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="Show:").pack(side="left")
        ttk.Button(top, text="All", width=5, command=lambda: self._show_all(True)).pack(side="left")
        ttk.Button(top, text="None", width=5, command=lambda: self._show_all(False)).pack(side="left")
        self.only_sel_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="selected row only", variable=self.only_sel_var,
                        command=self.schedule_plot).pack(side="left", padx=4)
        self.marks_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="DS markers on every curve", variable=self.marks_var,
                        command=self.schedule_plot).pack(side="left", padx=4)
        self.load_checks = FlowChecks(parent, per_row=9)
        self.load_checks.pack(fill="x", pady=2)
        lim = ttk.Frame(parent)
        lim.pack(fill="x", pady=(0, 2))
        ttk.Label(lim, text="Axis limits (empty = automatic):").pack(side="left")
        saved = self.settings.get("limits", {})
        self.lim_vars = {}
        for key, label, unit in (("drift", "drift", "mm"), ("force", "top chart y", ""),
                                 ("value", "section value", "mm")):
            ttk.Label(lim, text=f"   {label}").pack(side="left")
            for end in ("lo", "hi"):
                v = tk.StringVar(value=saved.get(f"{key}_{end}", ""))
                e = ttk.Entry(lim, textvariable=v, width=7, justify="right")
                e.pack(side="left", padx=1)
                e.bind("<Return>", lambda ev: self.schedule_plot())
                e.bind("<FocusOut>", lambda ev: self.schedule_plot())
                self.lim_vars[f"{key}_{end}"] = v
                if end == "lo":
                    ttk.Label(lim, text="to").pack(side="left")
            ttk.Label(lim, text=unit).pack(side="left")
        ttk.Button(lim, text="Auto", width=6, command=self._limits_auto).pack(side="left", padx=8)
        ttk.Separator(lim, orient="vertical").pack(side="left", fill="y", padx=4)
        ttk.Button(lim, text="Top chart...", command=self.open_top_dialog).pack(side="left", padx=2)
        ttk.Button(lim, text="Lowest drift...", command=self.open_envelope).pack(side="left", padx=2)

        self.fig = Figure(figsize=(8, 8))
        self.ax1 = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212, sharex=self.ax1)
        self.fig.subplots_adjust(left=0.1, right=0.97, top=0.95, bottom=0.07, hspace=0.18)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        tb = NavigationToolbar2Tk(self.canvas, parent, pack_toolbar=False)
        tb.update()
        tb.pack(fill="x")
        self.toolbar = tb
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self.on_click)

    # ------------------------------------------------------------------ helpers
    def say(self, text):
        self.status.configure(text=text)

    def _set_dirty(self, on=True):
        if self._loading:
            return
        self.dirty = on
        name = os.path.basename(self.path) if self.path else "(not saved)"
        self.path_label.configure(text=name + (" *" if on else ""))

    @property
    def mech(self):
        return self.doc["mechanism"]

    def _db_list(self):
        paths = dc.find_databases()
        cur = self.db.root if self.db else None
        if cur and all(os.path.normcase(p) != os.path.normcase(cur) for p in paths):
            paths.append(cur)
        self._db_paths = {os.path.basename(p) + ("" if os.path.dirname(p) == dc.ABAQUS_DB_DIR else
                                                 f"  ({os.path.dirname(p)})"): p for p in paths}
        self.db_box["values"] = list(self._db_paths)

    def _browse_db(self):
        d = filedialog.askdirectory(title="Abaqus results database (folder with <SET>/<JOB>/*.txt)",
                                    initialdir=dc.ABAQUS_DB_DIR)
        if d:
            if not dc.looks_like_database(d):
                messagebox.showerror(APP_TITLE, f"{d}\nis not a database (<SET>/<JOB>/*.txt)")
                return
            self.set_database(d)

    # ------------------------------------------------------------------ start / database / group
    def _open_start(self):
        last = dc.from_root(self.settings.get("last_file", ""))
        db = dc.from_root(self.settings.get("database", ""))
        dbs = dc.find_databases()
        if not db or not os.path.isdir(db):
            db = dbs[0] if dbs else None
        if last and os.path.isfile(last):
            try:
                self.load_file(last)
                return
            except Exception as e:
                self.say(f"Could not open {last}: {e}")
        if db:
            self.set_database(db, rebuild=False)
        g = self.settings.get("group") or self.defaults["groups"][0]
        self.new_doc(self.defaults["mechanisms"][0]["key"], g)

    def set_database(self, path, rebuild=True):
        if not path:
            return
        try:
            self.db = dc.AbaqusDB.open(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._db_list()
        label = next((k for k, v in self._db_paths.items() if os.path.normcase(v) == os.path.normcase(path)), "")
        self.db_var.set(label)
        sets = self.db.sets()
        self.sec_a_box["values"] = sets
        self.sec_b_box["values"] = sets
        presets = [f"{g['name']}  [{g['filter']}]" for g in self.defaults["groups"]]
        presets += [f"{short_family(f)}  [{f}]" for f in self.db.families()]
        self.preset_box["values"] = presets
        if self.doc is not None:
            self.doc["database"] = dc.rel_to_root(self.db.root)
            if rebuild:
                self.build_rows()
                self._set_dirty()
        self.say(f"Database {self.db.name}: {len(sets)} sections, {len(self.db.jobs())} jobs")

    def _preset_selected(self, _e=None):
        text = self.preset_box.get()
        if "[" in text:
            name, flt = text.rsplit("[", 1)
            self.group_name_var.set(name.strip())
            self.group_filter_var.set(flt.rstrip("]").strip())
            self.apply_group()

    def apply_group(self):
        if self.doc is None:
            return
        self.doc["group"] = {"name": self.group_name_var.get().strip() or self.group_filter_var.get().strip(),
                             "filter": self.group_filter_var.get().strip()}
        self.build_rows()
        self._set_dirty()

    # ------------------------------------------------------------------ the document
    def new_doc(self, mech_key, group):
        m = copy.deepcopy(dc.mechanism_by_key(self.defaults, mech_key) or self.defaults["mechanisms"][0])
        m["location"] = m["locations"][0]["label"] if m.get("locations") else "custom"
        self.doc = {"format": dc.CRITERIA_FORMAT, "version": 1, "name": "", "final": False,
                    "created": dc.now(), "modified": "", "units": "mm, kN",
                    "database": dc.rel_to_root(self.db.root) if self.db else "",
                    "group": {"name": group.get("name", ""), "filter": group.get("filter", "")},
                    "mechanism": m, "pushover": copy.deepcopy(self.defaults["pushover"]),
                    "defaults_file": dc.rel_to_root(self.defaults_path),
                    "explanation": "Per job: DS1 ... DS5 = drift where the state starts [mm] and the displacement "
                                   "of section B relative to section A at that drift [mm] (the threshold). source: "
                                   "default = default threshold, disp / drift = typed by the user, none = not "
                                   "defined. Damage states are starts: DSk when value > threshold.",
                    "rows": [], "notes": ""}
        self.path = None
        self._doc_to_widgets()
        self.build_rows()
        self._set_dirty(False)

    def _auto_name(self):
        """Mechanism, sections (A vs B, component) and load-case group, e.g.
        'AX- RU1 vs SPEC GPL2 Uz - SHEAR NOKEY FR07'."""
        m, g = self.mech, self.doc["group"]
        return (f"{m['key']} {dc.section_short(m['section_a'])} vs {dc.section_short(m['section_b'])} "
                f"{m['component']} - {g.get('name') or g.get('filter') or 'all jobs'}")

    def auto_name(self):
        if self.doc is not None:
            self.name_var.set(self._auto_name())
            self._set_dirty()

    def _doc_to_widgets(self):
        self._loading = True
        try:
            d, m = self.doc, self.doc["mechanism"]
            self.name_var.set(d.get("name", ""))
            self.final_var.set(bool(d.get("final")))
            self.notes_var.set(d.get("notes", ""))
            self.group_name_var.set(d["group"].get("name", ""))
            self.group_filter_var.set(d["group"].get("filter", ""))
            self.mech_box["values"] = [x["key"] for x in self.defaults["mechanisms"]]
            self.mech_var.set(m["key"])
            self._update_location_box()
            self.sec_a_var.set(m["section_a"])
            self.sec_b_var.set(m["section_b"])
            self.comp_var.set(m["component"])
            self.sense_var.set(m.get("sense", "+"))
            self.rel_var.set(bool(m.get("relative_to_start")))
            for k in dc.DS:
                self.thr_vars[k].set(dc.fmt(m["thresholds"].get(k)))
                self.prov_vars[k].set(k in m.get("provisional", []))
            self.value_label.configure(text="Value compared with the thresholds:  " + dc.value_axis_label(m))
            self.defaults_label.configure(text=f"Defaults file: {dc.rel_to_root(self.defaults_path)}")
        finally:
            self._loading = False

    def _update_location_box(self):
        presets = self._location_presets()
        locs = [x["label"] for x in presets]
        cur = self.mech.get("location", "custom")
        if cur not in locs:          # custom, or a renamed / deleted preset: the preset with these sections, if any
            same = self._preset_of_sections(presets)
            if same is not None:
                cur = self.mech["location"] = same["label"]
            else:
                locs.append(cur)
        if "custom" not in locs:
            locs.append("custom")
        self.loc_box["values"] = locs
        self.loc_var.set(cur)
        preset = cur in [x["label"] for x in presets]
        self.loc_buttons["Save as new location..."].state(["disabled" if preset else "!disabled"])
        for text in ("Rename...", "Delete"):
            self.loc_buttons[text].state(["!disabled" if preset else "disabled"])

    def _location_presets(self):
        return (dc.mechanism_by_key(self.defaults, self.mech["key"]) or {}).get("locations", [])

    def _preset_of_sections(self, presets):
        return next((x for x in presets if all(x.get(k) == self.mech.get(k)
                                               for k in ("section_a", "section_b", "component"))), None)

    def change_mechanism(self):
        key = self.mech_var.get().strip()
        if not key or self.doc is None or key == self.mech["key"]:
            return
        base = dc.mechanism_by_key(self.defaults, key)
        typed = any(e.get("source") in ("disp", "drift") for r in self.doc["rows"] for e in r["DS"].values())
        if base is not None:
            reset = typed and messagebox.askyesno(
                APP_TITLE, f"Mechanism {key}: sections and thresholds are set to its defaults.\n\n"
                           "Also reset the typed drifts / displacements of the rows to the defaults?")
            m = copy.deepcopy(base)
            m["location"] = m["locations"][0]["label"] if m.get("locations") else "custom"
            self.doc["mechanism"] = m
            if reset:
                for r in self.doc["rows"]:
                    r["DS"] = {k: dc.new_ds_entry() for k in dc.DS}
        else:                                            # a new (user-defined) mechanism: keep the definition
            self.mech["key"] = key
            self.mech["name"] = key
        if self.path:                                    # another mechanism = another file: never overwrite the old
            self.path = None
            self.doc["name"] = ""
            self.doc["final"] = False
            self.say(f"Mechanism {key}: this is a new file now (the saved file is unchanged). Save it under a new "
                     f"name - Auto name suggests one.")
        self._doc_to_widgets()
        self.recompute()
        self._set_dirty()

    def change_location(self):
        label = self.loc_var.get()
        base = dc.mechanism_by_key(self.defaults, self.mech["key"]) or {}
        loc = next((x for x in base.get("locations", []) if x["label"] == label), None)
        self.mech["location"] = label
        if loc:
            self.mech.update({k: loc[k] for k in ("section_a", "section_b", "component")})
        self._doc_to_widgets()
        self.recompute()
        self._set_dirty()

    def change_definition(self):
        if self._loading or self.doc is None:
            return
        m = self.mech
        new = {"section_a": self.sec_a_var.get(), "section_b": self.sec_b_var.get(),
               "component": self.comp_var.get(), "sense": self.sense_var.get(),
               "relative_to_start": bool(self.rel_var.get())}
        if all(m.get(k) == v for k, v in new.items()):
            return
        m.update(new)
        base = dc.mechanism_by_key(self.defaults, m["key"]) or {}
        loc = next((x for x in base.get("locations", []) if all(x[k] == m[k] for k in
                                                                 ("section_a", "section_b", "component"))), None)
        m["location"] = loc["label"] if loc else "custom"
        self._doc_to_widgets()
        self.recompute()
        self._set_dirty()

    def change_thresholds(self):
        if self._loading or self.doc is None:
            return
        m = self.mech
        new, bad = {}, []
        for k in dc.DS:
            try:
                new[k] = dc.num(self.thr_vars[k].get())
            except ValueError:
                bad.append(k)
                new[k] = m["thresholds"].get(k)
        prov = [k for k in dc.DS if self.prov_vars[k].get()]
        if bad:
            messagebox.showerror(APP_TITLE, "Not a number: " + ", ".join(bad))
            for k in bad:
                self.thr_vars[k].set(dc.fmt(m["thresholds"].get(k)))
        defined = [new[k] for k in dc.DS if new[k] is not None]
        if not dc.thresholds_grow(defined):
            self.say("Warning: the thresholds should grow strictly in size from DS1 to DS5")
        if any(v < 0 for v in defined) and any(v > 0 for v in defined):
            self.say("Warning: positive and negative thresholds mixed. A positive threshold is reached when the value "
                     "rises above it, a negative one when the value drops below it.")
        if new == m["thresholds"] and prov == m.get("provisional", []):
            return
        m["thresholds"], m["provisional"] = new, prov
        self.recompute()
        self._set_dirty()

    def reset_rows(self):
        if not messagebox.askyesno(APP_TITLE, "Reset every row to the default thresholds of this file?\n"
                                              "Typed drifts and displacements are lost."):
            return
        for r in self.doc["rows"]:
            r["DS"] = {k: dc.new_ds_entry() for k in dc.DS}
        self.recompute()
        self._set_dirty()

    # ------------------------------------------------------------------ rows and curves
    def build_rows(self):
        if self.doc is None:
            return
        if self.db is None:
            self.group_info.configure(text="no database")
            return
        jobs = dc.group_jobs(self.db, self.doc["group"].get("filter", ""))
        old = {r["job"]: r for r in self.doc["rows"]}
        rows = []
        for j in jobs:
            r = old.get(j) or {"job": j, "family": dc.job_family(j), "axial_N": dc.job_axial_N(j),
                               "DS": {k: dc.new_ds_entry() for k in dc.DS}, "note": ""}
            r["axial_kN"] = (r.get("axial_N") or 0) / 1000.0
            rows.append(r)
        rows.sort(key=lambda r: (r["family"], r.get("axial_N") or 0))
        self.doc["rows"] = rows
        fams = sorted({r["family"] for r in rows})
        self.group_info.configure(text=f"{len(rows)} jobs in {len(fams)} famil{'y' if len(fams) == 1 else 'ies'}"
                                  + (f": {', '.join(short_family(f) for f in fams)}" if len(fams) <= 3 else ""))
        if not rows:
            self.say("The group filter matches no job of this database")
        multi = len(fams) > 1
        keep = {k: v.get() for k, v in self.load_checks.vars.items()}
        self.load_checks.set_items([(r["job"], f"{dc.fmt_kN(r['axial_kN'])} kN"
                                     + (f" {short_family(r['family'])}" if multi else "")) for r in rows],
                                   self.schedule_plot, keep)
        self.sel_row = 0 if rows else None
        self.recompute(rebuild=True)

    def recompute(self, rebuild=False):
        """Curves of every job for the current mechanism, then every row."""
        self.curves = {}
        errors = []
        for r in self.doc["rows"]:
            try:
                c = dc.job_curves(self.db, r["job"], self.doc["pushover"], self.mech)
            except Exception as e:
                c = None
                errors.append(f"{r['job']}: {e}")
            self.curves[r["job"]] = c
            dc.compute_row(r, c, self.mech["thresholds"])
        self.value_label.configure(text="Value compared with the thresholds:  " + dc.value_axis_label(self.mech))
        if rebuild or len(self.row_widgets) != len(self.doc["rows"]):
            self._build_grid()
        else:
            self._refresh_grid()
        if errors:
            self.say("Cannot evaluate: " + "; ".join(errors[:3]) + (" ..." if len(errors) > 3 else ""))
        self.schedule_plot()

    # ------------------------------------------------------------------ the table
    def _build_grid(self):
        g = self.table.inner
        for w in g.winfo_children():
            w.destroy()
        self.cells, self.row_widgets = {}, {}
        hdr = {"bg": "#D9D9D9", "relief": "ridge", "bd": 1}
        for c, text, w in ((0, "kN", 6), (1, "family", 17), (2, "valid to", 7)):
            tk.Label(g, text=text, width=w, **hdr).grid(row=0, column=c, rowspan=2, sticky="nsew")
        for j, k in enumerate(dc.DS):
            tk.Label(g, text=k, bg=dc.DS_COLORS[k], fg=dc.DS_TEXT_COLORS[k], relief="ridge", bd=1).grid(
                row=0, column=3 + 2 * j, columnspan=2, sticky="nsew")
            tk.Label(g, text="drift", width=6, **hdr).grid(row=1, column=3 + 2 * j, sticky="nsew")
            tk.Label(g, text="disp", width=6, **hdr).grid(row=1, column=4 + 2 * j, sticky="nsew")
        tk.Label(g, text="note", width=18, **hdr).grid(row=0, column=13, rowspan=2, sticky="nsew")
        for i, r in enumerate(self.doc["rows"]):
            gr = i + 2
            labs = []
            for c, w in ((0, 6), (1, 17), (2, 7)):
                lab = tk.Label(g, width=w, anchor="e" if c != 1 else "w", relief="groove", bd=1)
                lab.grid(row=gr, column=c, sticky="nsew")
                lab.bind("<Button-1>", lambda e, i=i: self.select(i, self.sel_ds))
                labs.append(lab)
            self.row_widgets[i] = labs
            for j, k in enumerate(dc.DS):
                for f, col in (("drift", 3 + 2 * j), ("disp", 4 + 2 * j)):
                    v = tk.StringVar()
                    e = tk.Entry(g, textvariable=v, width=7, justify="right", relief="solid", bd=1,
                                 highlightthickness=2, highlightcolor="#2F5597", highlightbackground="#FFFFFF")
                    e.grid(row=gr, column=col, sticky="nsew")
                    e.bind("<FocusIn>", lambda ev, i=i, k=k: self.select(i, k, focus=False))
                    e.bind("<Return>", lambda ev, i=i, k=k, f=f: self.commit(i, k, f))
                    e.bind("<FocusOut>", lambda ev, i=i, k=k, f=f: self.commit(i, k, f))
                    self.cells[(i, k, f)] = (e, v)
            nv = tk.StringVar(value=r.get("note", ""))
            ne = tk.Entry(g, textvariable=nv, width=20, relief="solid", bd=1)
            ne.grid(row=gr, column=13, sticky="nsew")
            ne.bind("<FocusOut>", lambda ev, i=i, nv=nv: self._note(i, nv))
            ne.bind("<Return>", lambda ev, i=i, nv=nv: self._note(i, nv))
        self._refresh_grid()

    def _note(self, i, var):
        r = self.doc["rows"][i]
        if r.get("note", "") != var.get():
            r["note"] = var.get()
            self._set_dirty()

    def _cell_text(self, e, field):
        src, status = e.get("source"), e.get("status")
        if src == "none":
            return "-"
        v = e.get(field)
        if v is None:
            if field == "drift" and status == "not reached":
                return "n.r."
            if field == "disp" and status == "beyond data":
                return "beyond"
            return ""
        if field == "drift":
            return f"{0.0 if abs(v) < 0.005 else v:.2f}"          # no "-0.00" for frame 0
        return f"{v:.4g}"

    def _cell_style(self, e, field, k):
        src, status = e.get("source"), e.get("status")
        fg = "#000000"
        if src == "none" or (status and e.get(field) is None):
            bg = BG_OFF
            fg = "#808080"
        elif src == field:
            bg = BG_INPUT
        elif src == "default" and field == "disp":
            bg = BG_DEFAULT
            fg = FG_PROVISIONAL if k in self.mech.get("provisional", []) else "#404040"
        else:
            bg = BG_COMPUTED
        return bg, fg

    def _refresh_grid(self):
        for i, r in enumerate(self.doc["rows"]):
            if i not in self.row_widgets:
                continue
            warn = dc.row_warnings(r)
            labs = self.row_widgets[i]
            bg = BG_SELECTED_ROW if i == self.sel_row else "#FFFFFF"
            labs[0].configure(text=dc.fmt_kN(r["axial_kN"]), bg=bg, fg="#C00000" if warn else "#000000")
            labs[1].configure(text=short_family(r["family"]), bg=bg)
            labs[2].configure(text=dc.fmt(r.get("end_drift"), 3) if self.curves.get(r["job"]) else "error", bg=bg)
            for k in dc.DS:
                e = r["DS"][k]
                for f in ("drift", "disp"):
                    w, v = self.cells[(i, k, f)]
                    v.set(self._cell_text(e, f))
                    cbg, cfg = self._cell_style(e, f, k)
                    w.configure(bg=cbg, fg=cfg, highlightbackground="#2F5597" if (i == self.sel_row and
                                                                                  k == self.sel_ds) else cbg)

    def select(self, i, k, focus=True):
        changed = i != self.sel_row
        self.sel_row, self.sel_ds = i, k
        self._refresh_grid()
        if focus and (i, k, "drift") in self.cells:
            self.cells[(i, k, "drift")][0].focus_set()
        r = self.doc["rows"][i]
        e = r["DS"][k]
        H = self.doc["pushover"].get("H_mm") or 0
        msg = f"{r['job']}  {k}: source {e['source']}"
        if e.get("drift") is not None:
            msg += f", starts at drift {e['drift']:.2f} mm" + (f" = {100 * e['drift'] / H:.3f} % of H" if H else "")
        if e.get("disp") is not None:
            msg += f", displacement {e['disp']:.4g} mm"
        if e.get("status"):
            msg += f" ({e['status']})"
        warn = dc.row_warnings(r)
        if warn:
            msg += "   WARNING: " + "; ".join(warn)
        self.say(msg)
        if changed:
            self.schedule_plot()

    def commit(self, i, k, field):
        if i >= len(self.doc["rows"]) or (i, k, field) not in self.cells:
            return
        r = self.doc["rows"][i]
        e = r["DS"][k]
        w, var = self.cells[(i, k, field)]
        text = var.get().strip()
        if text == self._cell_text(e, field):
            return
        if text == "":
            e["source"] = "default"
        elif text in ("-", "–"):
            e["source"] = "none"
        else:
            try:
                val = dc.num(text)
            except ValueError:
                messagebox.showerror(APP_TITLE, f"'{text}' is not a number")
                var.set(self._cell_text(e, field))
                return
            e["source"] = field
            e[field] = val
        dc.compute_entry(e, self.curves.get(r["job"]), self.mech["thresholds"].get(k))
        self._refresh_grid()
        self._set_dirty()
        self.select(i, k, focus=False)
        self.schedule_plot()

    def on_click(self, event):
        if self.toolbar.mode or event.button != 1 or event.inaxes not in (self.ax1, self.ax2):
            return
        if self.sel_row is None or event.xdata is None:
            return
        i, k = self.sel_row, self.sel_ds
        e = self.doc["rows"][i]["DS"][k]
        if self.click_var.get() == "disp" and event.inaxes is self.ax2:
            e["source"], e["disp"] = "disp", float(event.ydata)
        elif event.inaxes is self.ax1 and not self._top_x_is_drift:
            d = self._drift_at_top_click(event, self.doc["rows"][i])     # the top chart is not drift-based
            if d is None:
                return
            e["source"], e["drift"] = "drift", max(d, 0.0)
        else:
            e["source"], e["drift"] = "drift", max(float(event.xdata), 0.0)
        dc.compute_entry(e, self.curves.get(self.doc["rows"][i]["job"]), self.mech["thresholds"].get(k))
        nxt = dc.DS[min(dc.DS.index(k) + 1, len(dc.DS) - 1)]
        self._set_dirty()
        self.select(i, nxt, focus=True)
        self.schedule_plot()

    # ------------------------------------------------------------------ top chart
    def top_preset(self):
        """The preset the top chart uses for the current group (automatic choice or the chosen preset)."""
        jobs = [r["job"] for r in self.doc["rows"]] if self.doc else []
        return dc.preset_for_jobs(self.top, jobs)

    def top_x_is_drift(self, preset, pushover):
        norm = (lambda e: e.replace(" ", "").strip("()"))
        return norm(dc.axis_expr(preset["x"])) == norm(pushover["drift"])

    def top_arrays(self, db, job, preset):
        """(x, y) of the top chart for a job, every frame (None if a section / column is missing)."""
        out = []
        for ax in ("x", "y"):
            expr = dc.axis_expr(preset[ax])
            key = (db.root, job, expr)
            if key not in self._top_cache:
                try:
                    self._top_cache[key] = db.evaluate(expr, job)
                except Exception:
                    self._top_cache[key] = None
            out.append(self._top_cache[key])
        return tuple(out)

    def _drift_at_top_click(self, event, row):
        """Drift of the point of the row's top-chart curve nearest to the click (screen distance)."""
        c = self.curves.get(row["job"])
        if c is None:
            return None
        x, y = self.top_arrays(self.db, row["job"], self.top_preset())
        if x is None or y is None:
            return None
        n = min(c["n"], len(x), len(y))
        pts = self.ax1.transData.transform(np.column_stack([x[:n], y[:n]]))
        p = np.array([event.x, event.y], dtype=float)
        best = None
        for i in range(n - 1):
            a, b = pts[i], pts[i + 1]
            if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
                continue
            ab = b - a
            ll = float(ab @ ab)
            t = 0.0 if ll == 0 else min(max(float((p - a) @ ab) / ll, 0.0), 1.0)
            dist = float(np.hypot(*(a + t * ab - p)))
            if best is None or dist < best[0]:
                best = (dist, i, t)
        if best is None:
            return None
        _, i, t = best
        return float(c["drift"][i] + t * (c["drift"][i + 1] - c["drift"][i]))

    def open_top_dialog(self):
        if self.top_dialog is not None and self.top_dialog.winfo_exists():
            self.top_dialog.lift()
            return
        self.top_dialog = TopChartDialog(self)

    # chart settings interface of TopChartDialog (the top chart)
    def get_chart_cfg(self):
        return self.top

    def set_chart_cfg(self, cfg):
        self.top = cfg

    def chart_in_use(self):
        return self.top_preset() if self.doc else None

    def chart_redraw(self):
        self.schedule_plot()
        env = self.envelope
        if env is not None and env.winfo_exists() and env.axes.get("follow_top"):
            env.refresh()

    def open_envelope(self):
        if self.envelope is not None and self.envelope.winfo_exists():
            self.envelope.lift()
            self.envelope.refresh()
            return
        self.envelope = EnvelopeWindow(self)

    def top_limits(self, preset, pushover):
        """(x limits, y limits) of the top chart: x = the drift limits when the top x is the drift, else the
        limits of the top chart dialog; y = 'top chart y' of the main window."""
        if self.top_x_is_drift(preset, pushover):
            xl = self._limits("drift")
        else:
            xl = dc.limit_pair(self.top.get("x_lo", ""), self.top.get("x_hi", ""))
        return xl, self._limits("force")

    # ------------------------------------------------------------------ plot
    def _limits(self, key):
        return dc.limit_pair(self.lim_vars[f"{key}_lo"].get(), self.lim_vars[f"{key}_hi"].get())

    def _limits_auto(self):
        for v in self.lim_vars.values():
            v.set("")
        self.schedule_plot()

    def _show_all(self, on):
        self.load_checks.set_all(on)
        self.schedule_plot()

    def schedule_plot(self):
        if self._plot_job:
            self.after_cancel(self._plot_job)
        self._plot_job = self.after(120, self.plot)

    def plot(self):
        self._plot_job = None
        po = self.doc["pushover"] if self.doc else dc.PUSHOVER_DEFAULTS
        preset = self.top_preset() if self.doc else self.top["presets"][0]
        x_drift = self.top_x_is_drift(preset, po)
        self._top_x_is_drift = x_drift
        self.fig.clear()               # new axes: shared x only when the top chart's x is the drift itself
        ax1 = self.ax1 = self.fig.add_subplot(211)
        ax2 = self.ax2 = self.fig.add_subplot(212, sharex=ax1 if x_drift else None)
        self.fig.subplots_adjust(left=0.1, right=0.97, top=0.95, bottom=0.07, hspace=0.18 if x_drift else 0.3)
        if self.doc is None:
            self.canvas.draw_idle()
            return
        rows = self.doc["rows"]
        shown = self.load_checks.checked()
        sel = rows[self.sel_row] if self.sel_row is not None and self.sel_row < len(rows) else None
        if self.only_sel_var.get():
            shown = {sel["job"]} if sel else set()
        multi = len({r["family"] for r in rows}) > 1
        styles = ["-", "--", ":", "-."]
        fams = sorted({r["family"] for r in rows})
        after = []
        missing = []
        for r in rows:
            c = self.curves.get(r["job"])
            if c is None or r["job"] not in shown:
                continue
            is_sel = sel is not None and r is sel
            col = dc.load_color(r["axial_kN"])
            ls = styles[fams.index(r["family"]) % 4] if multi else "-"
            lw = 2.6 if is_sel else 1.3
            label = f"{dc.fmt_kN(r['axial_kN'])} kN" + (f" {short_family(r['family'])}" if multi else "")
            tx, ty = self.top_arrays(self.db, r["job"], preset)
            n_all = min(c["n_all"], len(tx), len(ty)) if (tx is not None and ty is not None) else 0
            n = min(c["n"], n_all)                     # frames before the collapse
            if n:
                ax1.plot(tx[:n], ty[:n], ls, color=col, lw=lw, label=label, zorder=3 if is_sel else 2)
            else:
                missing.append(r["job"])
            ax2.plot(c["drift"], c["value"], ls, color=col, lw=lw, zorder=3 if is_sel else 2)
            if 0 < n < n_all:                          # frames after the collapse: drawn last, not scaled to
                after.append((tx[n - 1:n_all], ty[n - 1:n_all], col))
            if not (is_sel or self.marks_var.get()):
                continue
            for k in dc.DS:
                e = r["DS"][k]
                if e.get("drift") is None:
                    continue
                ms = 9 if is_sel else 6
                if n:                                  # the frame where the state starts, on the top chart
                    xm = dc.value_at_drift(c["drift"][:n], tx[:n], e["drift"])
                    ym = dc.value_at_drift(c["drift"][:n], ty[:n], e["drift"])
                    if xm is not None and ym is not None:
                        ax1.plot(xm, ym, "o", color=dc.DS_COLORS[k], mec="black", mew=0.8, ms=ms, zorder=5)
                if e.get("disp") is not None:
                    ax2.plot(e["drift"], e["disp"], "o", color=dc.DS_COLORS[k], mec="black", mew=0.8, ms=ms,
                             zorder=5)
        if sel is not None:                                # thresholds of the selected row
            ys = [y for ln in ax2.lines for y in np.asarray(ln.get_ydata(), dtype=float).ravel() if np.isfinite(y)]
            lo, hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
            margin = 0.25 * max(hi - lo, 1e-6)
            above = []
            for k in dc.DS:
                e = sel["DS"][k]
                if e.get("disp") is not None:
                    if not lo - margin <= e["disp"] <= hi + margin:   # far from the curves: listed, not drawn
                        above.append(f"{k} {e['disp']:.4g}")
                        continue
                    ax2.axhline(e["disp"], color=dc.DS_COLORS[k], ls="--", lw=1.0, zorder=1, gid="nofit")
                    ax2.annotate(f"{k} {e['disp']:.4g}", xy=(1.0, e["disp"]), xycoords=("axes fraction", "data"),
                                 xytext=(-4, 2), textcoords="offset points", ha="right", fontsize=8,
                                 color="black" if k in ("DS3", "DS0") else dc.DS_COLORS[k])
                if e.get("drift") is not None and x_drift:
                    ax1.axvline(e["drift"], color=dc.DS_COLORS[k], ls=":", lw=1.0, zorder=1, gid="nofit")
            if above:
                ax2.text(0.99, 0.98, "outside the curves: " + ", ".join(above), transform=ax2.transAxes, ha="right",
                         va="top", fontsize=8, color="#555")
        if after:                                           # limits of the valid data, then the collapse tails
            ax1.set_xlim(ax1.get_xlim())
            ax1.set_ylim(ax1.get_ylim())
            for x, y, col in after:
                ax1.plot(x, y, ":", color=col, lw=0.8, alpha=0.5, gid="nofit")
        xl, yl = self.top_limits(preset, po)                 # the user's axis limits
        dc.apply_limits(ax1, xl, yl)
        dc.apply_limits(ax2, self._limits("drift"), self._limits("value"))
        g = self.doc["group"]
        ax1.set_title(f"{g.get('name') or g.get('filter')}: {preset['name']}"
                      + (f"   (selected: {sel['job']})" if sel else ""), fontsize=10)
        if missing:
            ax1.text(0.5, 0.5, f"top chart: {dc.axis_text(preset['x'])} / {dc.axis_text(preset['y'])} not in the "
                               f"database for {len(missing)} job(s)", transform=ax1.transAxes, ha="center",
                     color="#C00000", fontsize=9)
        ax1.set_ylabel(preset["y"].get("label") or dc.axis_text(preset["y"]))
        if not x_drift:
            ax1.set_xlabel(preset["x"].get("label") or dc.axis_text(preset["x"]))
        ax2.set_ylabel(dc.value_axis_label(self.mech), fontsize=9)
        ax2.set_xlabel(po.get("drift_label", "Drift [mm]"))
        for ax in (ax1, ax2):
            ax.grid(True, color="#e3e3e3", lw=0.8)
            ax.spines[["top", "right"]].set_visible(False)
        if ax1.lines and len(shown) <= 16:
            ax1.legend(fontsize=7, ncol=2, loc="best")
        proxies = [Line2D([], [], marker="o", ls="", color=dc.DS_COLORS[k], mec="black", label=f"{k} start")
                   for k in dc.DS]
        ax2.legend(handles=proxies, fontsize=7, ncol=5, loc="upper left")
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ files
    def _confirm_discard(self):
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_TITLE, "Save the changes of the current criteria file?")
        if ans is None:
            return False
        return self.save() if ans else True

    def new_file(self):
        if not self._confirm_discard():
            return
        key = self.mech_var.get() or self.defaults["mechanisms"][0]["key"]
        self.new_doc(key, self.doc["group"] if self.doc else self.defaults["groups"][0])
        self.say("New criteria file: choose the mechanism, location and group, then save")

    def open_file(self):
        if not self._confirm_discard():
            return
        p = filedialog.askopenfilename(title="Open criteria file", initialdir=dc.CRITERIA_DIR,
                                       filetypes=[("Criteria files", "*.json")])
        if p:
            try:
                self.load_file(p)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Could not open {p}:\n{e}")

    def load_file(self, path):
        d = dc.load_json(path)
        if d.get("format") != dc.CRITERIA_FORMAT:
            raise ValueError("not a criteria file of step 1")
        d["mechanism"] = dc.complete_mechanism(d["mechanism"])
        d.setdefault("pushover", copy.deepcopy(self.defaults["pushover"]))
        for k, v in dc.PUSHOVER_DEFAULTS.items():
            d["pushover"].setdefault(k, v)
        for r in d.get("rows", []):
            r["DS"] = {k: r.get("DS", {}).get(k) or dc.new_ds_entry() for k in dc.DS}
        db = dc.from_root(d.get("database", ""))
        if db and os.path.isdir(db):
            self.set_database(db, rebuild=False)
        elif self.db is None:
            raise FileNotFoundError(f"database {d.get('database')} not found - choose one and open again")
        else:
            self.say(f"Database {d.get('database')} not found - using {self.db.name}")
        self.doc = d
        self.path = path
        self._doc_to_widgets()
        self.build_rows()
        self._set_dirty(False)
        self.say(f"Opened {path}")

    def _collect(self):
        d = self.doc
        d["name"] = self.name_var.get().strip() or self._auto_name()
        self.name_var.set(d["name"])
        d["final"] = bool(self.final_var.get())
        d["notes"] = self.notes_var.get()
        d["modified"] = dc.now()
        d["database"] = dc.rel_to_root(self.db.root) if self.db else d.get("database", "")
        d["defaults_file"] = dc.rel_to_root(self.defaults_path)
        d["summary"] = criteria_summary(d)
        return d

    def save(self):
        if self.doc is None:
            return False
        if not self.path:
            return self.save_as()
        # the file is named after the name field: a changed name (e.g. Auto name) saves a NEW file and leaves the
        # old one as it is
        name = self.name_var.get().strip() or self._auto_name()
        target = os.path.join(os.path.dirname(self.path), dc.safe_filename(name) + ".json")
        new_file = os.path.normcase(target) != os.path.normcase(self.path)
        if new_file and os.path.exists(target) and not messagebox.askyesno(
                APP_TITLE, f"{os.path.basename(target)} exists already. Overwrite it?"):
            return False
        try:
            dc.save_json(self._collect(), target)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save:\n{e}")
            return False
        old, self.path = self.path, target
        self._set_dirty(False)
        self.say(f"Saved {target}" + (f"  (new file: the name changed; {os.path.basename(old)} is unchanged)"
                                      if new_file else ""))
        self.compare.refresh()
        return True

    def save_as(self):
        if self.doc is None:
            return False
        name = self.name_var.get().strip() or self._auto_name()
        p = filedialog.asksaveasfilename(title="Save criteria file", initialdir=dc.CRITERIA_DIR,
                                         initialfile=dc.safe_filename(name) + ".json", defaultextension=".json",
                                         filetypes=[("Criteria files", "*.json")])
        if not p:
            return False
        if not self.name_var.get().strip():
            self.name_var.set(os.path.splitext(os.path.basename(p))[0])
        self.path = p
        return self.save()

    # ------------------------------------------------------------------ defaults files
    def load_defaults_file(self):
        p = filedialog.askopenfilename(title="Defaults file", initialdir=dc.DEFAULTS_DIR,
                                       filetypes=[("Defaults", "*.json")])
        if not p:
            return
        try:
            self.defaults = dc.load_defaults(p)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not read {p}:\n{e}")
            return
        self.defaults_path = p
        self.set_database(self.db.root, rebuild=False) if self.db else None
        base = dc.mechanism_by_key(self.defaults, self.mech["key"])
        if base and messagebox.askyesno(APP_TITLE, f"Use the {self.mech['key']} definition and thresholds of this "
                                                   f"defaults file for the current criteria file?"):
            m = copy.deepcopy(base)
            m["location"] = m["locations"][0]["label"] if m.get("locations") else "custom"
            self.doc["mechanism"] = m
            self.doc["pushover"] = copy.deepcopy(self.defaults["pushover"])
            self.recompute()
            self._set_dirty()
        self._doc_to_widgets()
        self.say(f"Defaults: {p}")

    def save_as_default(self):
        """The mechanism of the current file (sections, component, sense, thresholds) becomes the default of its
        key in a defaults file."""
        p = filedialog.asksaveasfilename(title="Save the mechanism as default (defaults file)",
                                         initialdir=dc.DEFAULTS_DIR, initialfile=os.path.basename(self.defaults_path),
                                         defaultextension=".json", filetypes=[("Defaults", "*.json")])
        if not p:
            return
        try:
            data = dc.load_defaults(p) if os.path.isfile(p) else copy.deepcopy(self.defaults)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"{p} is not a defaults file:\n{e}")
            return
        m = copy.deepcopy(self.mech)
        loc_label = m.pop("location", "custom")
        old = dc.mechanism_by_key(data, m["key"])
        if old is not None:
            locs = old.get("locations", [])
            here = {k: m[k] for k in ("section_a", "section_b", "component")}
            if not any(all(x[k] == here[k] for k in here) for x in locs):
                label = loc_label if loc_label != "custom" else dc.section_label(m)
                locs.insert(0, dict(label=label, **here))
            else:                                     # the current location first (= default of new files)
                locs.sort(key=lambda x: 0 if all(x[k] == here[k] for k in here) else 1)
            m["locations"] = locs
            m.setdefault("etabs", old.get("etabs"))
            data["mechanisms"][data["mechanisms"].index(old)] = m
        else:
            m["locations"] = [dict(label=dc.section_label(m), section_a=m["section_a"], section_b=m["section_b"],
                                   component=m["component"])]
            data["mechanisms"].append(m)
        data["pushover"] = copy.deepcopy(self.doc["pushover"])
        dc.save_json(data, p)
        self.defaults, self.defaults_path = dc.load_defaults(p), p
        self._doc_to_widgets()
        self.say(f"{m['key']} saved as default in {p}")

    # ------------------------------------------------------------------ location presets (in the defaults file)
    def _edit_locations(self, change):
        """change(locations) edits the location presets of the current mechanism in the defaults file in use and
        saves it; the rest of the file stays as it is. Returns True when saved."""
        key, path = self.mech["key"], self.defaults_path
        if dc.mechanism_by_key(self.defaults, key) is None:
            messagebox.showinfo(APP_TITLE, f"Mechanism {key} is not in the defaults file. Save it there first with "
                                           f"'Save as default...'.")
            return False
        try:
            data = dc.load_json(path)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_TITLE, f"Could not read the defaults file {path}:\n{e}")
            return False
        if not data.get("mechanisms"):                # the file takes the mechanisms of the script: write them out
            data["mechanisms"] = copy.deepcopy(self.defaults["mechanisms"])
        m = next((x for x in data["mechanisms"] if x.get("key") == key), None)
        if m is None:
            m = copy.deepcopy(dc.mechanism_by_key(self.defaults, key))
            data["mechanisms"].append(m)
        change(m.setdefault("locations", []))
        try:
            dc.save_json(data, path)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not save the defaults file {path}:\n{e}")
            return False
        self.defaults = dc.load_defaults(path)
        return True

    def _ask_location_name(self, prompt, initial):
        name = simpledialog.askstring(APP_TITLE, prompt, initialvalue=initial, parent=self)
        name = (name or "").strip()
        if not name:
            return None
        if name == "custom":
            messagebox.showerror(APP_TITLE, "'custom' is kept for sections that are not a preset")
            return None
        return name

    def save_location(self):
        """The current sections and component become a new location preset of the mechanism."""
        if self.doc is None:
            return
        m = self.mech
        here = {k: m[k] for k in ("section_a", "section_b", "component")}
        same = self._preset_of_sections(self._location_presets())
        if same is not None:
            messagebox.showinfo(APP_TITLE, f"These sections are the location '{same['label']}' already.")
            return
        name = self._ask_location_name(f"Name of the new {m['key']} location\n({dc.section_label(m)}):",
                                       dc.section_label(m))
        if name is None:
            return
        if any(x["label"] == name for x in self._location_presets()) and not messagebox.askyesno(
                APP_TITLE, f"{m['key']} has a location '{name}' already. Give it these sections?"):
            return

        def change(locs):
            old = next((x for x in locs if x.get("label") == name), None)
            if old is not None:
                old.update(here)
            else:
                locs.append(dict(label=name, **here))   # at the end: the first one stays the default of new files
        if self._edit_locations(change):
            m["location"] = name
            self._doc_to_widgets()
            self._set_dirty()
            self.say(f"Location '{name}' of {m['key']} saved in {dc.rel_to_root(self.defaults_path)}")

    def rename_location(self):
        old = self.loc_var.get()
        if self.doc is None or old not in [x["label"] for x in self._location_presets()]:
            return
        name = self._ask_location_name(f"New name of the {self.mech['key']} location '{old}':", old)
        if name is None or name == old:
            return
        if any(x["label"] == name for x in self._location_presets()):
            messagebox.showerror(APP_TITLE, f"{self.mech['key']} has a location '{name}' already")
            return

        def change(locs):
            for x in locs:
                if x.get("label") == old:
                    x["label"] = name
        if self._edit_locations(change):
            self.mech["location"] = name
            self._doc_to_widgets()
            self._set_dirty()
            self.say(f"Location '{old}' of {self.mech['key']} renamed to '{name}' in "
                     f"{dc.rel_to_root(self.defaults_path)}")

    def delete_location(self):
        label = self.loc_var.get()
        if self.doc is None or label not in [x["label"] for x in self._location_presets()]:
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete the location '{label}' of {self.mech['key']} from the defaults "
                                              f"file {dc.rel_to_root(self.defaults_path)}?\n\nThe open file keeps "
                                              f"its sections (shown as custom); saved criteria files are not "
                                              f"changed."):
            return

        def change(locs):
            locs[:] = [x for x in locs if x.get("label") != label]
        if self._edit_locations(change):
            self.mech["location"] = "custom"
            self._doc_to_widgets()
            self._set_dirty()
            self.say(f"Location '{label}' of {self.mech['key']} deleted from {dc.rel_to_root(self.defaults_path)}")

    # ------------------------------------------------------------------ close
    def _close(self):
        if not self._confirm_discard():
            return
        s = {"geometry": self.geometry(), "database": dc.rel_to_root(self.db.root) if self.db else "",
             "defaults_file": dc.rel_to_root(self.defaults_path), "last_file": dc.rel_to_root(self.path or ""),
             "group": self.doc["group"] if self.doc else None, "click": self.click_var.get(),
             "limits": {k: v.get() for k, v in self.lim_vars.items()}, "top_chart": self.top}
        if self.envelope is not None and self.envelope.winfo_exists():
            self.envelope.destroy()                         # stores its window size in self.settings
        for k in ("envelope_geometry", "envelope_chart"):
            if self.settings.get(k):
                s[k] = self.settings[k]
        try:
            dc.save_json(s, SETTINGS_FILE)
        except OSError:
            pass
        self.destroy()


# =====================================================================================================
# POP-UP: WHAT THE TOP CHART PLOTS
# =====================================================================================================

TOP_CHART_NOTE = ("Only the top chart changes: the damage-state drifts in the table always use the drift of the "
                  "criteria file (RU2 - RU1 Ux). The database has mm, rad, N and N.mm: factor 0.001 -> kN, 1e-6 -> "
                  "kNm, 1000 -> mrad. When X is not the drift, the two charts do not share the x axis, and a click "
                  "on the top chart takes the drift of the nearest point of the selected job's curve. The y limits "
                  "are 'top chart y' in the main window.")
ENVELOPE_CHART_NOTE = ("Axes of the lowest-drift window. The damage-state points are always placed at their start "
                       "drift (RU2 - RU1 Ux), whatever is plotted. The database has mm, rad, N and N.mm: factor "
                       "0.001 -> kN, 1e-6 -> kNm, 1000 -> mrad.")


class TopChartDialog(tk.Toplevel):
    """Presets of chart axes: X and Y = (section 1 - section 2).column x factor. Works on a target window that has
    get_chart_cfg() / set_chart_cfg(cfg) (presets, auto, preset, limits), chart_in_use() and chart_redraw():
    the top chart of the main window, or the lowest-drift window (own axes and Y limits). Only plots change; the
    damage-state drifts always use the drift of the criteria file."""

    def __init__(self, app, target=None, title="Top chart", key="top_chart", y_limits=False, note=TOP_CHART_NOTE,
                 x_limit_text="X axis limits, used when X is not the drift (empty = automatic)"):
        super().__init__(target if isinstance(target, tk.Misc) else app)
        self.app = app
        self.target = target or app
        self.key = key                                # key of the defaults file
        self.dlg_title = title
        self.title(title)
        self.transient(target if isinstance(target, tk.Misc) else app)
        self.resizable(False, False)
        top = self.cfg
        f = ttk.Frame(self, padding=8)
        f.pack(fill="both", expand=True)
        r = ttk.Frame(f)
        r.pack(fill="x")
        ttk.Label(r, text="Preset").pack(side="left")
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(r, textvariable=self.preset_var, width=42, state="readonly")
        self.preset_box.pack(side="left", padx=4)
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self._select(self.preset_var.get()))
        ttk.Button(r, text="New...", command=self._new).pack(side="left", padx=2)
        ttk.Button(r, text="Delete", command=self._delete).pack(side="left", padx=2)
        self.auto_var = tk.BooleanVar(value=bool(top.get("auto")))
        ttk.Checkbutton(f, text="Choose the preset automatically: the first preset whose match text is in every job "
                                "name of the group (e.g. BEND)", variable=self.auto_var,
                        command=self._apply).pack(anchor="w", pady=(6, 0))
        m = ttk.Frame(f)
        m.pack(fill="x", pady=2)
        ttk.Label(m, text="Match text of this preset:").pack(side="left")
        self.match_var = tk.StringVar()
        ttk.Entry(m, textvariable=self.match_var, width=14).pack(side="left", padx=4)
        self.inuse = ttk.Label(m, text="", foreground="#2F5597")
        self.inuse.pack(side="left", padx=8)

        sets = app.db.sets() if app.db else []
        self.ax_vars = {}
        for axis, title in (("x", "X axis"), ("y", "Y axis")):
            lf = ttk.LabelFrame(f, text=title)
            lf.pack(fill="x", pady=4)
            v = {k: tk.StringVar() for k in ("section_1", "section_2", "column", "factor", "label")}
            ttk.Label(lf, text="Section").grid(row=0, column=0, sticky="w", padx=2)
            ttk.Combobox(lf, textvariable=v["section_1"], values=sets, width=20, state="readonly").grid(
                row=0, column=1, padx=2)
            ttk.Label(lf, text="minus").grid(row=0, column=2)
            ttk.Combobox(lf, textvariable=v["section_2"], values=["(none)"] + sets, width=20,
                         state="readonly").grid(row=0, column=3, padx=2)
            ttk.Label(lf, text="column").grid(row=0, column=4)
            ttk.Combobox(lf, textvariable=v["column"], values=list(dc.SECTION_COLUMNS), width=5,
                         state="readonly").grid(row=0, column=5, padx=2)
            ttk.Label(lf, text="x factor").grid(row=0, column=6)
            ttk.Entry(lf, textvariable=v["factor"], width=9).grid(row=0, column=7, padx=2)
            ttk.Label(lf, text="Axis label").grid(row=1, column=0, sticky="w", padx=2)
            ttk.Entry(lf, textvariable=v["label"], width=52).grid(row=1, column=1, columnspan=5, sticky="w", padx=2,
                                                                  pady=2)
            v["expr"] = ttk.Label(lf, text="", foreground="#666")
            v["expr"].grid(row=2, column=0, columnspan=8, sticky="w", padx=2)
            self.ax_vars[axis] = v
        self.lim = {}
        for axis, text in (("x", x_limit_text), ("y", "Y axis limits (empty = automatic)")):
            if axis == "y" and not y_limits:
                continue
            lf = ttk.LabelFrame(f, text=text)
            lf.pack(fill="x", pady=4)
            for end in ("lo", "hi"):
                v = tk.StringVar(value=top.get(f"{axis}_{end}", ""))
                ttk.Entry(lf, textvariable=v, width=9).pack(side="left", padx=2, pady=2)
                if end == "lo":
                    ttk.Label(lf, text="to").pack(side="left")
                self.lim[f"{axis}_{end}"] = v
        ttk.Label(f, text=note, wraplength=660, foreground="#555", justify="left").pack(fill="x", pady=4)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(4, 0))
        for text, cmd in (("Apply", self._apply), ("Save as default", self._save_default),
                          ("Reset to defaults", self._reset), ("Close", self.destroy)):
            ttk.Button(b, text=text, command=cmd).pack(side="left", padx=2)
        self._fill_presets()
        used = self.target.chart_in_use()
        self._select(used["name"] if used else top.get("preset"), redraw=False)   # the one in use

    @property
    def cfg(self):
        return self.target.get_chart_cfg()

    def _presets(self):
        return self.cfg["presets"]

    def _fill_presets(self):
        self.preset_box["values"] = [p["name"] for p in self._presets()]

    def _current(self):
        return next((p for p in self._presets() if p["name"] == self.preset_var.get()), self._presets()[0])

    def _select(self, name, redraw=True):
        p = next((p for p in self._presets() if p["name"] == name), self._presets()[0])
        self.preset_var.set(p["name"])
        self.match_var.set(p.get("match", ""))
        for axis in ("x", "y"):
            a, v = p[axis], self.ax_vars[axis]
            v["section_1"].set(a.get("section_1", ""))
            v["section_2"].set(a.get("section_2") or "(none)")
            v["column"].set(a.get("column", "Ux"))
            v["factor"].set(f"{float(a.get('factor', 1.0) or 1.0):g}")
            v["label"].set(a.get("label", ""))
            v["expr"].configure(text="= " + dc.axis_expr(a))
        self.cfg["preset"] = p["name"]
        self._show_in_use()
        if redraw:
            self._redraw()

    def _read(self):
        out = {}
        for axis in ("x", "y"):
            v = self.ax_vars[axis]
            try:
                factor = dc.num(v["factor"].get()) or 1.0
            except ValueError:
                messagebox.showerror(self.dlg_title, f"{axis.upper()} factor is not a number", parent=self)
                return None
            if not v["section_1"].get():
                messagebox.showerror(self.dlg_title, f"Choose the section of the {axis.upper()} axis", parent=self)
                return None
            s2 = v["section_2"].get()
            out[axis] = {"section_1": v["section_1"].get(), "section_2": "" if s2 in ("", "(none)") else s2,
                         "column": v["column"].get() or "Ux", "factor": factor, "label": v["label"].get().strip()}
        return out

    def _apply(self):
        axes = self._read()
        if axes is None:
            return
        p = self._current()
        p.update(axes)
        p["match"] = self.match_var.get().strip()
        cfg = self.cfg
        cfg["auto"] = bool(self.auto_var.get())
        for k, v in self.lim.items():
            cfg[k] = v.get().strip()
        for axis in ("x", "y"):
            self.ax_vars[axis]["expr"].configure(text="= " + dc.axis_expr(p[axis]))
        self._show_in_use()
        self._redraw()

    def _redraw(self):
        self.target.chart_redraw()

    def _show_in_use(self):
        used = self.target.chart_in_use()
        if used is None:
            return
        text = f"in use: {used['name']}"
        if self.cfg.get("auto") and used["name"] != self.preset_var.get():
            text += "  (automatic choice)"
        self.inuse.configure(text=text)

    def _new(self):
        name = simpledialog.askstring(self.dlg_title, "Name of the new preset:", parent=self)
        if not name:
            return
        if any(p["name"] == name for p in self._presets()):
            messagebox.showerror(self.dlg_title, f"A preset '{name}' exists already", parent=self)
            return
        axes = self._read() or {k: copy.deepcopy(self._current()[k]) for k in ("x", "y")}
        self._presets().append(dict(name=name, match="", **axes))
        self._fill_presets()
        self._select(name)

    def _delete(self):
        if len(self._presets()) <= 1:
            return
        self._presets().remove(self._current())
        self._fill_presets()
        self._select(self._presets()[0]["name"])

    def _save_default(self):
        self._apply()
        path = self.app.defaults_path
        try:
            data = dc.load_defaults(path)
            data[self.key] = copy.deepcopy(self.cfg)
            dc.save_json(data, path)
            self.app.defaults = dc.load_defaults(path)
        except Exception as e:
            messagebox.showerror(self.dlg_title, f"Could not save {path}:\n{e}", parent=self)
            return
        self.app.say(f"{self.dlg_title}: presets saved as default in {path}")

    def _reset(self):
        cfg = copy.deepcopy(self.app.defaults.get(self.key) or self.app.defaults["top_chart"])
        self.target.set_chart_cfg(cfg)
        self.auto_var.set(bool(cfg.get("auto")))
        for k, v in self.lim.items():
            v.set(cfg.get(k, ""))
        self._fill_presets()
        used = self.target.chart_in_use()
        self._select(used["name"] if used else cfg.get("preset"))


# =====================================================================================================
# OPTIONAL WINDOW: LOWEST DRIFT PER DAMAGE STATE OVER ALL MECHANISMS
# =====================================================================================================

def ds_line(ax, x, y, codes, states_on, lw=2.0):
    """Line whose segments take the colour of the damage state at their end (grey for states not shown)."""
    from matplotlib.collections import LineCollection
    pts = np.column_stack([x, y])
    if len(pts) < 2:
        return
    ok = np.all(np.isfinite(pts), axis=1)
    seg = np.stack([pts[:-1], pts[1:]], axis=1)
    keep = ok[:-1] & ok[1:]
    cols = [dc.DS_COLORS[dc.STATES[int(c)]] if dc.STATES[int(c)] in states_on else "#D0D0D0" for c in codes[1:]]
    ax.add_collection(LineCollection(seg[keep], colors=np.array(cols, dtype=object)[keep], linewidths=lw))
    ax.update_datalim(pts[ok])
    ax.autoscale_view()


class EnvelopeWindow(tk.Toplevel):
    """Pushover curves (as in the top chart) with, for every damage state, the lowest start drift over the chosen
    criteria files of all mechanisms, labelled with the mechanism that gives it."""

    CURRENT = "__current__"

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Lowest drift per damage state - all mechanisms")
        self.geometry(self.app.settings.get("envelope_geometry", "1550x950"))
        self.colors = dict(app.defaults.get("mechanism_colors") or dc.MECH_COLORS)
        self.keys = [m["key"] for m in app.defaults["mechanisms"]]
        self.data = []
        self._files = {}
        # own axes (presets like the top chart); follow_top = plot what the top chart of the main window plots
        self.axes = copy.deepcopy(app.settings.get("envelope_chart") or app.defaults["envelope_chart"])
        if not self.axes.get("presets"):
            self.axes = copy.deepcopy(app.defaults["envelope_chart"])
        self.axes_dialog = None

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)
        ttk.Label(left, text="For every job and damage state: the lowest start drift over the criteria files below, "
                             "labelled with the mechanism that gives it. The file open in the editor can be used as it "
                             "is now (unsaved changes included).", wraplength=340, foreground="#444",
                  justify="left").pack(fill="x")

        sf = ttk.LabelFrame(left, text="Criteria per mechanism")
        sf.pack(fill="x", pady=4)
        self.src_vars, self.src_boxes, self.src_labels, self.src_map = {}, {}, {}, {}
        for i, key in enumerate(self.keys):
            lab = tk.Label(sf, text=key, width=5, font=("Segoe UI", 9, "bold"))
            lab.grid(row=i, column=0, padx=2)
            v = tk.StringVar()
            b = ttk.Combobox(sf, textvariable=v, width=36, state="readonly")
            b.grid(row=i, column=1, padx=2, pady=1)
            b.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            self.src_vars[key], self.src_boxes[key], self.src_labels[key] = v, b, lab
        ttk.Button(sf, text="Refresh", command=lambda: (self._fill_sources(keep=True), self.refresh())).grid(
            row=len(self.keys), column=1, sticky="e", pady=2)

        cf = ttk.LabelFrame(left, text="Mechanism label colours (click to change)")
        cf.pack(fill="x", pady=4)
        self.color_buttons = {}
        for i, key in enumerate(self.keys):
            btn = tk.Button(cf, text=key, width=6, command=lambda k=key: self._pick_color(k))
            btn.grid(row=0, column=i, padx=2, pady=2)
            self.color_buttons[key] = btn
        ttk.Button(cf, text="Save colours as default", command=self._save_colors).grid(
            row=1, column=0, columnspan=len(self.keys), sticky="w", padx=2, pady=2)

        af = ttk.LabelFrame(left, text="Axes")
        af.pack(fill="x", pady=4)
        self.follow_var = tk.BooleanVar(value=bool(self.axes.get("follow_top")))
        ttk.Checkbutton(af, text="same as the top chart of the main window", variable=self.follow_var,
                        command=self._follow_changed).grid(row=0, column=0, columnspan=2, sticky="w")
        self.axes_var = tk.StringVar()
        self.axes_box = ttk.Combobox(af, textvariable=self.axes_var, width=36, state="readonly")
        self.axes_box.grid(row=1, column=0, padx=2, pady=2, sticky="w")
        self.axes_box.bind("<<ComboboxSelected>>", lambda e: self._axes_selected())
        ttk.Button(af, text="Edit axes...", command=self.open_axes_dialog).grid(row=2, column=0, sticky="w", padx=2)
        self.axes_info = ttk.Label(af, text="", foreground="#2F5597", wraplength=330, justify="left")
        self.axes_info.grid(row=3, column=0, columnspan=2, sticky="w", padx=2)

        of = ttk.LabelFrame(left, text="Show")
        of.pack(fill="x", pady=4)
        self.ds_vars = {}
        for j, k in enumerate(dc.DS):
            v = tk.BooleanVar(value=True)
            tk.Checkbutton(of, text=k, variable=v, command=self.plot, bg=dc.DS_COLORS[k], fg=dc.DS_TEXT_COLORS[k],
                           selectcolor="#808080", activebackground=dc.DS_COLORS[k]).grid(row=0, column=j, padx=1)
            self.ds_vars[k] = v
        self.color_ds_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="colour the curves by damage state (else by axial load)",
                        variable=self.color_ds_var, command=self.plot).grid(row=1, column=0, columnspan=5, sticky="w")
        self.labels_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="mechanism labels", variable=self.labels_var, command=self.plot).grid(
            row=2, column=0, columnspan=5, sticky="w")

        lf = ttk.LabelFrame(left, text="Axial loads")
        lf.pack(fill="both", expand=True, pady=4)
        bb = ttk.Frame(lf)
        bb.pack(fill="x")
        ttk.Button(bb, text="All", width=6, command=lambda: (self.load_checks.set_all(True), self.plot())).pack(
            side="left")
        ttk.Button(bb, text="None", width=6, command=lambda: (self.load_checks.set_all(False), self.plot())).pack(
            side="left", padx=2)
        self.load_checks = FlowChecks(lf, per_row=2)
        self.load_checks.pack(fill="both", expand=True)

        vp = ttk.PanedWindow(right, orient="vertical")
        vp.pack(fill="both", expand=True)
        pf = ttk.Frame(vp)
        tf = ttk.Frame(vp)
        vp.add(pf, weight=3)
        vp.add(tf, weight=1)
        self.fig = Figure(figsize=(9, 6))
        self.canvas = FigureCanvasTkAgg(self.fig, master=pf)
        tb = NavigationToolbar2Tk(self.canvas, pf, pack_toolbar=False)
        tb.update()
        tb.pack(fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        cols = [("load", "Axial load", 90), ("family", "Family", 150)] + [(k, k, 120) for k in dc.DS]
        self.table = ttk.Treeview(tf, columns=[c[0] for c in cols], show="headings", height=8)
        for k, t, w in cols:
            self.table.heading(k, text=t + (" start drift [mm]  mechanism" if k in dc.DS else ""))
            self.table.column(k, width=w if k not in dc.DS else 190, anchor="center")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        self._update_colors()
        self._fill_sources(keep=False)
        self.refresh()

    # ------------------------------------------------------------------ axes
    def _jobs(self):
        return [d["job"] for d in self.data]

    def get_chart_cfg(self):                       # chart settings interface of TopChartDialog
        return self.axes

    def set_chart_cfg(self, cfg):
        cfg["follow_top"] = False
        self.axes = cfg

    def chart_in_use(self):
        return self._preset_for(self._jobs()) if self.data else None

    def chart_redraw(self):
        self.refresh()

    def _preset_for(self, jobs):
        cfg = self.app.top if self.axes.get("follow_top") else self.axes
        return dc.preset_for_jobs(cfg, jobs)

    def _update_axes_widgets(self):
        follow = bool(self.axes.get("follow_top"))
        self.follow_var.set(follow)
        self.axes_box["values"] = [p["name"] for p in self.axes["presets"]]
        p = getattr(self, "preset", None)
        self.axes_var.set(p["name"] if p else self.axes.get("preset", ""))
        self.axes_box.configure(state="disabled" if follow else "readonly")
        if p:
            self.axes_info.configure(text=f"X: {p['x'].get('label') or dc.axis_text(p['x'])}\n"
                                          f"Y: {p['y'].get('label') or dc.axis_text(p['y'])}"
                                          + ("  (as the top chart)" if follow else ""))

    def _follow_changed(self):
        self.axes["follow_top"] = bool(self.follow_var.get())
        self.refresh()

    def _axes_selected(self):
        self.axes["preset"] = self.axes_var.get()
        self.axes["auto"] = False                  # an explicit choice is not overridden by the automatic one
        self.refresh()

    def open_axes_dialog(self):
        if self.axes_dialog is not None and self.axes_dialog.winfo_exists():
            self.axes_dialog.lift()
            return
        if self.axes.get("follow_top"):            # editing = own axes from now on
            self.axes["follow_top"] = False
            self.refresh()
        self.axes_dialog = TopChartDialog(self.app, target=self, title="Lowest-drift chart axes",
                                          key="envelope_chart", y_limits=True, note=ENVELOPE_CHART_NOTE,
                                          x_limit_text="X axis limits (empty = automatic)")

    # ------------------------------------------------------------------ sources
    def _fill_sources(self, keep):
        self._files = {p: d for p, d in dc.list_criteria_files()}
        doc = self.app.doc
        flt = (doc or {}).get("group", {}).get("filter", "")
        for key in self.keys:
            opts = {"(none)": None}
            if doc is not None and doc["mechanism"]["key"] == key:
                opts["(open in the editor) " + (doc.get("name") or self.app._auto_name())] = self.CURRENT
            files = sorted(((p, d) for p, d in self._files.items() if d.get("mechanism", {}).get("key") == key),
                           key=lambda t: (not t[1].get("final"), t[1].get("name", "")))
            for p, d in files:
                opts[("★ " if d.get("final") else "") + d.get("name", os.path.basename(p))] = p
            self.src_map[key] = opts
            self.src_boxes[key]["values"] = list(opts)
            cur = self.src_vars[key].get()
            if keep and cur in opts:
                continue
            choice = next((k for k, v in opts.items() if v == self.CURRENT), None)
            if choice is None:                     # same group (final first), else any final file, else none
                same = [k for k, v in opts.items() if v not in (None, self.CURRENT)
                        and self._files[v].get("group", {}).get("filter", "") == flt]
                finals = [k for k in opts if k.startswith("★ ")]
                choice = (same or finals or ["(none)"])[0]
            self.src_vars[key].set(choice)

    def _source(self, key):
        target = self.src_map.get(key, {}).get(self.src_vars[key].get())
        if target is None:
            return None
        if target == self.CURRENT:
            doc = self.app.doc
            return {"rows": {r["job"]: r for r in doc["rows"]}, "db": self.app.db, "pushover": doc["pushover"]}
        d = self._files.get(target) or dc.load_json(target)
        return {"rows": {r["job"]: r for r in d.get("rows", [])},
                "db": dc.AbaqusDB.open(dc.from_root(d.get("database", ""))),
                "pushover": dict(dc.PUSHOVER_DEFAULTS, **(d.get("pushover") or {}))}

    # ------------------------------------------------------------------ colours
    def _update_colors(self):
        for key in self.keys:
            c = self.colors.get(key, "#404040")
            self.color_buttons[key].configure(bg=c, fg="white", activebackground=c)
            self.src_labels[key].configure(fg=c)

    def _pick_color(self, key):
        c = colorchooser.askcolor(color=self.colors.get(key, "#404040"), parent=self, title=f"Colour of {key}")[1]
        if c:
            self.colors[key] = c
            self._update_colors()
            self.plot()

    def _save_colors(self):
        path = self.app.defaults_path
        try:
            data = dc.load_defaults(path)
            data["mechanism_colors"] = dict(self.colors)
            dc.save_json(data, path)
            self.app.defaults = dc.load_defaults(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save {path}:\n{e}", parent=self)
            return
        self.app.say(f"Mechanism colours saved as default in {path}")

    # ------------------------------------------------------------------ data, plot, table
    def refresh(self):
        srcs = {}
        for key in self.keys:
            try:
                s = self._source(key)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"{key}: {e}", parent=self)
                s = None
            if s:
                srcs[key] = s
        jobs = sorted({j for s in srcs.values() for j in s["rows"]},
                      key=lambda j: (dc.job_family(j), dc.job_axial_N(j) or 0))
        self.preset = self._preset_for(jobs)
        self.data = []
        for job in jobs:
            src = next(s for s in srcs.values() if job in s["rows"])
            db, po = src["db"], src["pushover"]
            try:
                drift = db.evaluate(po["drift"], job)
                x, y = self.app.top_arrays(db, job, self.preset)
            except Exception:
                continue
            if x is None or y is None:
                continue
            n = min(dc.collapse_end(db, job, po, [drift]), len(drift), len(x), len(y))
            env = {}
            for k in dc.DS:
                cands = [((s["rows"][job]["DS"].get(k) or {}).get("drift"), key) for key, s in srcs.items()
                         if job in s["rows"]]
                cands = [(d, key) for d, key in cands if d is not None]
                if cands:
                    dmin = min(d for d, _ in cands)
                    env[k] = (dmin, [key for d, key in cands if abs(d - dmin) < 1e-9])
            self.data.append({"job": job, "family": dc.job_family(job), "kN": (dc.job_axial_N(job) or 0) / 1000.0,
                              "drift": drift[:n], "x": x[:n], "y": y[:n], "env": env, "pushover": po})
        multi = len({d["family"] for d in self.data}) > 1
        keep = {k: v.get() for k, v in self.load_checks.vars.items()}
        self.load_checks.set_items([(d["job"], f"{dc.fmt_kN(d['kN'])} kN" + (f" {short_family(d['family'])}"
                                                                             if multi else "")) for d in self.data],
                                   self.plot, keep)
        self._update_axes_widgets()
        self.plot()
        self.fill_table()

    def plot(self):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.08, right=0.86, top=0.94, bottom=0.09)
        shown = self.load_checks.checked()
        states_on = {"DS0"} | {k for k in dc.DS if self.ds_vars[k].get()}
        by_ds = self.color_ds_var.get()
        load_handles = []
        labels = []                             # (x, y, mechanisms) of every marker
        for d in self.data:
            if d["job"] not in shown:
                continue
            col = dc.load_color(d["kN"])
            if by_ds:
                codes = dc.ds_codes_from_starts(d["drift"], {k: v[0] for k, v in d["env"].items()})
                ds_line(ax, d["x"], d["y"], codes, states_on)
            else:
                ax.plot(d["x"], d["y"], "-", color=col, lw=1.6)
                load_handles.append(Line2D([], [], color=col, lw=2, label=f"{dc.fmt_kN(d['kN'])} kN"))
            for k in dc.DS:
                if k not in d["env"] or not self.ds_vars[k].get():
                    continue
                dk, keys = d["env"][k]
                xm = dc.value_at_drift(d["drift"], d["x"], dk)
                ym = dc.value_at_drift(d["drift"], d["y"], dk)
                if xm is None or ym is None:
                    continue
                ax.plot(xm, ym, "o", color=dc.DS_COLORS[k], mec="black", mew=0.8, ms=8, zorder=5)
                labels.append((xm, ym, keys))
        if self.labels_var.get() and labels:
            # one label per spot: markers closer than 1 % of the data range share it (states that start at the
            # same drift, or several loads starting at zero drift)
            xs = [v for v, _, _ in labels]
            ys = [v for _, v, _ in labels]
            sx, sy = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
            spots = {}
            for xm, ym, keys in labels:
                spot = spots.setdefault((round(xm / sx, 2), round(ym / sy, 2)), [xm, ym, []])
                spot[2].extend(keys)
            for xm, ym, keys in spots.values():
                keys = list(dict.fromkeys(keys))
                ax.annotate("/".join(keys), (xm, ym), xytext=(4, 4), textcoords="offset points", fontsize=7,
                            fontweight="bold", color=self.colors.get(keys[0], "#404040"), zorder=6)
        handles = [Line2D([], [], marker="o", ls="", color=dc.DS_COLORS[k], mec="black", ms=7, label=f"{k} start")
                   for k in dc.DS if self.ds_vars[k].get()]
        handles += [Line2D([], [], marker="s", ls="", color=self.colors.get(k, "#404040"), ms=8, label=k)
                    for k in self.keys]
        ax.legend(handles=handles, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0)
        if load_handles:
            ax.add_artist(ax.legend(handles=load_handles, fontsize=7, loc="lower left", bbox_to_anchor=(1.01, 0.0),
                                    borderaxespad=0))
            ax.legend(handles=handles, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0)
        p = getattr(self, "preset", None) or self._preset_for([])
        po = self.data[0]["pushover"] if self.data else dc.PUSHOVER_DEFAULTS
        if self.axes.get("follow_top"):                  # the limits of the top chart
            xl, yl = self.app.top_limits(p, po)
        else:                                            # own limits (Edit axes...)
            xl = dc.limit_pair(self.axes.get("x_lo", ""), self.axes.get("x_hi", ""))
            yl = dc.limit_pair(self.axes.get("y_lo", ""), self.axes.get("y_hi", ""))
        dc.apply_limits(ax, xl, yl)
        ax.set_xlabel(p["x"].get("label") or dc.axis_text(p["x"]))
        ax.set_ylabel(p["y"].get("label") or dc.axis_text(p["y"]))
        ax.set_title(f"Lowest start drift per damage state over the mechanisms - {p['name']}", fontsize=10)
        ax.grid(True, color="#e3e3e3", lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        if not self.data:
            ax.text(0.5, 0.5, "Choose criteria files on the left", ha="center", va="center", transform=ax.transAxes,
                    color="#888")
        self.canvas.draw_idle()

    def fill_table(self):
        t = self.table
        t.delete(*t.get_children())
        for d in self.data:
            vals = [f"{dc.fmt_kN(d['kN'])} kN", short_family(d["family"])]
            for k in dc.DS:
                if k in d["env"]:
                    dk, keys = d["env"][k]
                    vals.append(f"{0.0 if abs(dk) < 0.005 else dk:.2f}   {'/'.join(keys)}")
                else:
                    vals.append("n.r.")
            t.insert("", "end", values=vals)

    def destroy(self):
        try:
            self.app.settings["envelope_geometry"] = self.geometry()
        except tk.TclError:
            pass
        self.app.settings["envelope_chart"] = self.axes
        super().destroy()


# =====================================================================================================
# TAB 2: COMPARE CRITERIA FILES (optional)
# =====================================================================================================

STAT_CHOICES = ["none", "min", "max", "mean"]                    # statistic of the Compare plot
# exported value: (shown, in file names, statistic, k of mean + k sigma)
VALUE_CHOICES = [("mean", "mean", "mean", 0), ("mean − 1σ", "mean - 1 sigma", "mean", -1),
                 ("mean + 1σ", "mean + 1 sigma", "mean", 1), ("mean − 2σ", "mean - 2 sigma", "mean", -2),
                 ("mean + 2σ", "mean + 2 sigma", "mean", 2), ("min", "min", "min", 0), ("max", "max", "max", 0)]


def pooled_stats(pool):
    """{DS: {axial kN: {n, mean, sigma, min, max, min_files, max_files}}} of the pooled start drifts. sigma is the
    sample standard deviation (n - 1), None for one value; min_files / max_files = the files with that drift."""
    out = {}
    for k, by_load in pool.items():
        for a, vals in by_load.items():
            v = np.array([x[0] for x in vals], float)
            lo, hi = float(v.min()), float(v.max())
            out.setdefault(k, {})[a] = {
                "n": len(v), "mean": float(v.mean()), "sigma": float(v.std(ddof=1)) if len(v) > 1 else None,
                "min": lo, "max": hi,
                "min_files": sorted({x[2] for x in vals if x[0] == lo}),
                "max_files": sorted({x[2] for x in vals if x[0] == hi})}
    return out


def stat_value(s, stat, k_sigma=0):
    """(value, clipped): min, max or mean + k_sigma * sigma (sigma taken as 0 for one value); never below 0."""
    val = s[stat] if stat in ("min", "max") else s["mean"] + k_sigma * (s["sigma"] or 0.0)
    return max(val, 0.0), val < 0


def short_file_name(name):
    """'AX+ SPEC GM2 vs RU1 Uz - SHEAR NOKEY FR07' -> 'AX+ SPEC GM2 vs RU1 Uz' (the group is the same for most)"""
    return name.split(" - ")[0].strip() or name


def system_decimal():
    """Decimal sign of the Windows number format (copied tables paste into Excel as numbers)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as key:
            return winreg.QueryValueEx(key, "sDecimal")[0] or "."
    except Exception:
        return "."


class StatExportDialog(tk.Toplevel):
    """The mean (± k σ), minimum or maximum start drifts of the files selected in the Compare tab, saved as a
    criteria file of step 1. Mechanism, sections, database and jobs come from one of the files ('definition from');
    every drift is a typed drift, so the file opens in tab 1 and works in step 3 like any other."""

    def __init__(self, tab, chosen, stat="mean"):
        super().__init__(tab.app)
        self.tab, self.chosen = tab, chosen
        self.title("Export a statistic as a criteria file")
        self.transient(tab.app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        groups = sorted({self._group(d) for _, d in chosen})
        mechs = sorted({d.get("mechanism", {}).get("key", "?") for _, d in chosen})
        info = (f"{len(chosen)} selected files ({', '.join(mechs)}). Per damage state and axial load, the start drifts "
                f"of the files where the state starts are pooled, as in the plot and its table. All of DS1 - DS5 are "
                f"exported, whatever is shown.")
        if len(groups) > 1:
            info += (f"\nThe files come from {len(groups)} load-case groups ({', '.join(groups)}): jobs of the same "
                     f"axial load are pooled together.")
        ttk.Label(f, text=info, wraplength=560, justify="left").grid(row=0, column=0, columnspan=3, sticky="w",
                                                                     pady=(0, 8))
        ttk.Label(f, text="Value").grid(row=1, column=0, sticky="w")
        self.value_var = tk.StringVar(value=stat if stat in ("min", "max") else "mean")
        box = ttk.Combobox(f, textvariable=self.value_var, values=[c[0] for c in VALUE_CHOICES], state="readonly",
                           width=14)
        box.grid(row=1, column=1, sticky="w", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self._follow_name())
        ttk.Label(f, text="Definition from").grid(row=2, column=0, sticky="w", padx=(0, 6))
        self.names = [d.get("name") or os.path.basename(p) for p, d in chosen]
        most = max(range(len(chosen)), key=lambda i: len(chosen[i][1].get("rows", [])))      # the most jobs
        self.ref_var = tk.StringVar(value=self.names[most])
        box = ttk.Combobox(f, textvariable=self.ref_var, values=self.names, state="readonly", width=62)
        box.grid(row=2, column=1, columnspan=2, sticky="we", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self._follow_name())
        ttk.Label(f, text="The new file takes the mechanism key, sections, database and jobs of this file. Its drifts "
                          "are replaced; the displacements are those of its sections at the new drifts.",
                  foreground="#555", wraplength=520, justify="left").grid(row=3, column=1, columnspan=2, sticky="w")
        ttk.Label(f, text="Name").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.name_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.name_var, width=62).grid(row=4, column=1, sticky="we", pady=(6, 0))
        ttk.Button(f, text="Auto name", command=self.auto_name).grid(row=4, column=2, padx=4, pady=(6, 0))
        self.final_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="final", variable=self.final_var).grid(row=5, column=1, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bf, text="Export", command=self.export).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left")
        self._auto_text = ""
        self.auto_name()

    @staticmethod
    def _group(d):
        g = d.get("group", {})
        return g.get("name") or g.get("filter") or "all jobs"

    def _choice(self):
        return next(c for c in VALUE_CHOICES if c[0] == self.value_var.get())

    def _ref(self):
        return self.chosen[self.names.index(self.ref_var.get())]

    def auto_name(self):
        """e.g. 'AX+ mean - 1 sigma of 4 files - SHEAR NOKEY FR07'"""
        _, ref = self._ref()
        self._auto_text = (f"{ref.get('mechanism', {}).get('key', '')} {self._choice()[1]} of {len(self.chosen)} "
                           f"files - {self._group(ref)}")
        self.name_var.set(self._auto_text)

    def _follow_name(self):
        if self.name_var.get().strip() in ("", self._auto_text):        # not typed by the user: keep it matching
            self.auto_name()

    def build(self, name):
        """(criteria document, counts of the remarks, axial loads that have no job in the definition file)"""
        _, text, stat, k_sigma = self._choice()
        _, ref = self._ref()
        stats = pooled_stats(CompareTab.pool(self.chosen, dc.DS))
        for by_load in stats.values():
            for s in by_load.values():
                s["value"], s["clipped"] = stat_value(s, stat, k_sigma)
        mech = dc.complete_mechanism(copy.deepcopy(ref["mechanism"]))
        pushover = dict(dc.PUSHOVER_DEFAULTS, **(ref.get("pushover") or {}))
        root = dc.from_root(ref.get("database", ""))
        db = self.tab.app.db
        if db is None or os.path.normcase(os.path.normpath(db.root)) != os.path.normcase(root):
            db = dc.AbaqusDB.open(root)
        count = dict.fromkeys(("defined", "single", "clipped", "beyond", "no_curve", "order"), 0)
        rows = []
        for r in ref.get("rows", []):
            a = round((r.get("axial_N") or 0) / 1000.0, 6)
            row = {"job": r["job"], "family": r.get("family") or dc.job_family(r["job"]), "axial_N": r.get("axial_N"),
                   "axial_kN": a, "DS": {}, "note": ""}
            ns = []
            for k in dc.DS:
                s = stats.get(k, {}).get(a)
                if s is None:                                   # no file starts this state here
                    row["DS"][k] = dc.new_ds_entry("none")
                    ns.append("-")
                    continue
                e = row["DS"][k] = dc.new_ds_entry("drift")
                e["drift"] = s["value"]
                ns.append(str(s["n"]))
                count["defined"] += 1
                count["single"] += stat == "mean" and k_sigma != 0 and s["n"] == 1
                count["clipped"] += s["clipped"]
            row["note"] = "n " + "/".join(ns)
            try:
                curves = dc.job_curves(db, r["job"], pushover, mech)
            except Exception:
                curves = None
                count["no_curve"] += 1
            dc.compute_row(row, curves, mech["thresholds"])     # displacement of these sections at the drift
            count["beyond"] += sum(e.get("status") == "beyond data" for e in row["DS"].values())
            count["order"] += bool(dc.row_warnings(row))
            rows.append(row)
        ref_loads = {r["axial_kN"] for r in rows}
        loads = sorted({a for by in stats.values() for a in by})
        doc = copy.deepcopy(ref)
        doc.update({
            "name": name, "final": bool(self.final_var.get()), "created": dc.now(), "modified": dc.now(),
            "rows": rows,
            "notes": f"{text} of {len(self.chosen)} criteria files (Compare tab); definition and jobs from "
                     f"'{self.ref_var.get()}'. Row note: n = files where DS1/DS2/DS3/DS4/DS5 start.",
            "statistics": {
                "value": text, "statistic": stat, "k_sigma": k_sigma,
                "method": "per damage state and axial load: the start drifts of the files where the state starts "
                          "(never-reached states left out); sigma = sample standard deviation (n - 1), taken as 0 "
                          "for one file; a value below 0 is set to 0; min_files / max_files = files with that drift",
                "files": self.names, "definition_from": self.ref_var.get(),
                "by_axial_kN": {dc.fmt_kN(a): {k: stats[k][a] for k in dc.DS if a in stats.get(k, {})}
                                for a in loads}}})
        doc["summary"] = criteria_summary(doc)
        return doc, count, [a for a in loads if a not in ref_loads]

    def export(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(self.title(), "Give the file a name", parent=self)
            return
        target = os.path.join(dc.CRITERIA_DIR, dc.safe_filename(name) + ".json")
        if os.path.exists(target) and not messagebox.askyesno(
                self.title(), f"{os.path.basename(target)} exists already. Overwrite it?", parent=self):
            return
        try:
            doc, count, dropped = self.build(name)
            dc.save_json(doc, target)
        except Exception as e:
            messagebox.showerror(self.title(), f"Could not export:\n{e}", parent=self)
            return
        lines = [f"Saved {target}", "",
                 f"{len(doc['rows'])} jobs, {count['defined']} damage-state starts ({doc['statistics']['value']})."]
        if count["single"]:
            lines.append(f"{count['single']} starts come from one file only: σ is taken as 0 there.")
        if count["clipped"]:
            lines.append(f"{count['clipped']} values were below 0 and are set to 0.")
        if count["order"]:
            lines.append(f"{count['order']} jobs have states out of order (e.g. DS3 before DS2) - see tab 1.")
        if count["beyond"]:
            lines.append(f"{count['beyond']} drifts lie beyond the data of the definition file's curve: no "
                         f"displacement there.")
        if count["no_curve"]:
            lines.append(f"{count['no_curve']} jobs could not be evaluated: no displacements.")
        if dropped:
            lines.append("Axial loads without a job in the definition file (not exported): "
                         + ", ".join(dc.fmt_kN(a) for a in dropped) + " kN")
        app = self.tab.app
        self.destroy()
        self.tab.refresh()
        app.say(f"Exported {target}")
        messagebox.showinfo(APP_TITLE, "\n".join(lines), parent=app)


class CompareTab:
    MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*", "<", ">"]
    STYLES = ["-", "--", ":", "-."]
    HALO = [patheffects.withStroke(linewidth=4.5, foreground="white")]     # sets the statistic apart from the files
    EXTREME_MARKER = {"min": "v", "max": "^"}

    def __init__(self, parent, app):
        self.app = app
        self.files = []
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Optional: compare saved criteria files. Select one or more files (Ctrl / Shift + "
                            "click). Plotted vs the axial load of each job.", foreground="#444").pack(side="left")
        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=4)
        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)

        cols = (("final", "Final", 40), ("name", "Name", 260), ("mech", "Mech.", 50), ("loc", "Location", 170),
                ("group", "Group", 120), ("jobs", "Jobs", 40), ("modified", "Modified", 120))
        self.tree = ttk.Treeview(left, columns=[c[0] for c in cols], show="headings", height=22,
                                 selectmode="extended")
        for key, text, w in cols:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=w, anchor="w" if key in ("name", "loc", "group") else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.plot())
        bf = ttk.Frame(left)
        bf.pack(fill="x", pady=4)
        ttk.Button(bf, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bf, text="Open in tab 1", command=self.open_selected).pack(side="left", padx=4)
        ttk.Button(bf, text="Folder", command=lambda: open_path(dc.CRITERIA_DIR)).pack(side="left")
        of = ttk.LabelFrame(left, text="Show")
        of.pack(fill="x", pady=4)
        self.ds_vars = {}
        for j, k in enumerate(dc.DS):
            v = tk.BooleanVar(value=True)
            tk.Checkbutton(of, text=k, variable=v, command=self.plot, bg=dc.DS_COLORS[k], fg=dc.DS_TEXT_COLORS[k],
                           selectcolor="#888888", activebackground=dc.DS_COLORS[k]).grid(row=0, column=j, padx=2)
            self.ds_vars[k] = v
        self.log_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="log axial axis", variable=self.log_var, command=self.plot).grid(
            row=1, column=0, columnspan=3, sticky="w")
        self.thr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="default thresholds as lines", variable=self.thr_var, command=self.plot).grid(
            row=1, column=3, columnspan=3, sticky="w")
        ttk.Label(of, text="statistic").grid(row=2, column=0, sticky="w", padx=2)
        self.stat_var = tk.StringVar(value="none")
        box = ttk.Combobox(of, textvariable=self.stat_var, values=STAT_CHOICES, state="readonly", width=7)
        box.grid(row=2, column=1, columnspan=2, sticky="w", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self.plot())
        ttk.Label(of, text="of the shown files (+ table)", foreground="#555").grid(row=2, column=3, columnspan=3,
                                                                                   sticky="w")
        ttk.Button(of, text="Export a statistic as a criteria file...", command=self.export_stat).grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(2, 0))
        lf = ttk.LabelFrame(left, text="Axis limits (empty = automatic)")
        lf.pack(fill="x", pady=2)
        self.lim_vars = {}
        for i, (key, label) in enumerate((("drift", "start drift [mm]"), ("disp", "displacement [mm]"))):
            ttk.Label(lf, text=label).grid(row=i, column=0, sticky="w", padx=2)
            for j, end in enumerate(("lo", "hi")):
                v = tk.StringVar()
                e = ttk.Entry(lf, textvariable=v, width=8, justify="right")
                e.grid(row=i, column=1 + 2 * j, padx=2)
                e.bind("<Return>", lambda ev: self.plot())
                self.lim_vars[f"{key}_{end}"] = v
            ttk.Label(lf, text="to").grid(row=i, column=2)
        ttk.Button(lf, text="Apply", command=self.plot).grid(row=0, column=4, rowspan=2, padx=6)

        self.fig = Figure(figsize=(8, 8))
        self.ax1 = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212, sharex=self.ax1)
        self.fig.subplots_adjust(left=0.1, right=0.97, top=0.95, bottom=0.08, hspace=0.2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        tb = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        tb.update()
        tb.pack(fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        # table of the statistic under the plot, shown with a statistic only
        self.table_box = ttk.Frame(right)
        th = ttk.Frame(self.table_box)
        th.pack(fill="x")
        ttk.Button(th, text="Copy table", command=self.copy_table).pack(side="right")    # first: never squeezed out
        self.table_title = ttk.Label(th, foreground="#444")
        self.table_title.pack(side="left")
        self.table = ScrollFrame(self.table_box, height=190)
        self.table.pack(fill="x")
        self.table_rows = []                         # the table as values, for the clipboard

    def refresh(self):
        sel = {self.tree.item(i, "values")[1] for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        self.files = dc.list_criteria_files()
        for i, (p, d) in enumerate(self.files):
            m = d.get("mechanism", {})
            g = d.get("group", {})
            mod = d.get("modified") or ""
            iid = self.tree.insert("", "end", iid=str(i), values=(
                "yes" if d.get("final") else "", d.get("name", os.path.basename(p)), m.get("key", ""),
                m.get("location", ""), g.get("name") or g.get("filter", ""), len(d.get("rows", [])), mod))
            if d.get("name") in sel:
                self.tree.selection_add(iid)
        self.plot()

    def open_selected(self):
        s = self.tree.selection()
        if s and self.app._confirm_discard():
            self.app.load_file(self.files[int(s[0])][0])
            self.app.nb.select(0)

    def selected(self):
        return [self.files[int(i)] for i in self.tree.selection() if int(i) < len(self.files)]

    def export_stat(self):
        chosen = self.selected()
        if not chosen:
            messagebox.showinfo(APP_TITLE, "Select the criteria files first", parent=self.app)
            return
        StatExportDialog(self, chosen, self.stat_var.get())

    @staticmethod
    def pool(chosen, keys):
        """{DS: {axial kN: [(drift, disp, file name), ...]}} over every row of the chosen files, only where the state
        starts."""
        out = {}
        for p, d in chosen:
            name = d.get("name") or os.path.basename(p)
            for r in d.get("rows", []):
                a = round((r.get("axial_N") or 0) / 1000.0, 6)
                for k in keys:
                    e = r.get("DS", {}).get(k) or {}
                    if e.get("drift") is not None:
                        out.setdefault(k, {}).setdefault(a, []).append((e["drift"], e.get("disp"), name))
        return out

    def plot(self):
        with warnings.catch_warnings():              # clearing a shared log axis warns about its 0..1 reset
            warnings.simplefilter("ignore", UserWarning)
            self.fig.clear()                         # new axes: a log axis cannot be cleared back to linear quietly
        ax1 = self.ax1 = self.fig.add_subplot(211)
        ax2 = self.ax2 = self.fig.add_subplot(212, sharex=ax1)
        chosen = self.selected()
        ds_on = [k for k in dc.DS if self.ds_vars[k].get()]
        stat = self.stat_var.get()
        alpha = 0.5 if stat != "none" else 1.0
        pool = self.pool(chosen, ds_on)
        handles = []
        xs_all = set()
        for n, (p, d) in enumerate(chosen):
            mk, ls = self.MARKERS[n % len(self.MARKERS)], self.STYLES[n % len(self.STYLES)]
            rows = d.get("rows", [])
            fams = sorted({r.get("family", "") for r in rows})
            for fam in fams:
                fr = sorted((r for r in rows if r.get("family", "") == fam), key=lambda r: r.get("axial_N") or 0)
                x = [(r.get("axial_N") or 0) / 1000.0 for r in fr]
                for k in ds_on:
                    yd = [r["DS"].get(k, {}).get("drift") for r in fr]
                    yv = [r["DS"].get(k, {}).get("disp") for r in fr]
                    for ax, y in ((ax1, yd), (ax2, yv)):
                        pts = [(a, b) for a, b in zip(x, y) if b is not None]
                        if pts:
                            ax.plot(*zip(*pts), ls=ls, color=dc.DS_COLORS[k], lw=1.3, alpha=alpha)
                        # filled = the state starts in the analysis, hollow = threshold never reached
                        for a, b, start in zip(x, y, yd):
                            if b is not None:
                                ax.plot(a, b, marker=mk, ms=6, mew=0.8, ls="", mec="black" if start is not None else
                                        dc.DS_COLORS[k], mfc=dc.DS_COLORS[k] if start is not None else "none",
                                        alpha=alpha)
                    xs_all.update(x)
            if self.thr_var.get():
                for k in ds_on:
                    t = d.get("mechanism", {}).get("thresholds", {}).get(k)
                    if t is not None:
                        ax2.axhline(t, color=dc.DS_COLORS[k], ls=ls, lw=0.8, alpha=0.6 * alpha)
            label = d.get("name", os.path.basename(p)) + (" (final)" if d.get("final") else "")
            handles.append(Line2D([], [], ls=ls, marker=mk, color="#404040", label=label))
        handles += [Line2D([], [], marker="s", ls="", color=dc.DS_COLORS[k], mec="black", label=k) for k in ds_on]
        if stat == "mean" and pool:
            self._plot_mean((ax1, ax2), pool, [k for k in ds_on if k in pool])
            handles += [Line2D([], [], color="#404040", lw=2.2, marker="D", ms=6, mec="white",
                               path_effects=self.HALO, label="mean of the files where the state starts"),
                        Line2D([], [], color="#404040", lw=4, label="± 1σ (n ≥ 2)"),
                        Line2D([], [], color="#404040", lw=1.1, marker="_", ms=9, mew=1.1, label="± 2σ")]
        elif stat in ("min", "max") and pool:
            self._plot_extreme((ax1, ax2), pool, [k for k in ds_on if k in pool], stat)
            handles.append(Line2D([], [], color="#404040", lw=2.2, marker=self.EXTREME_MARKER[stat], ms=7,
                                  mec="white", path_effects=self.HALO,
                                  label=f"{'minimum' if stat == 'min' else 'maximum'} start drift of the files "
                                        f"(lower chart: the displacement of that start)"))
        self._fill_table(pooled_stats(pool) if stat != "none" and chosen else {}, ds_on, stat)
        ax1.set_ylabel("Drift where the state starts [mm]")
        ax2.set_ylabel("Section displacement at the start [mm]")
        ax2.set_xlabel("Axial load [kN]")
        ax1.set_title("Damage-state starts of the selected criteria files", fontsize=10)
        for ax in (ax1, ax2):
            ax.grid(True, color="#e3e3e3", lw=0.8)
            ax.spines[["top", "right"]].set_visible(False)
        handles.append(Line2D([], [], marker="o", ls="", mfc="none", mec="#404040", label="threshold not reached"))
        if chosen:
            if self.log_var.get():
                ax1.set_xscale("log")
            ticks = sorted(x for x in xs_all if x > 0)
            if ticks:
                ax2.set_xticks(ticks)
                ax2.set_xticklabels([dc.fmt_kN(t) for t in ticks], rotation=90, fontsize=7)
                ax2.minorticks_off()
                ax1.tick_params(labelbottom=False)
            leg = ax1.legend(handles=handles, fontsize=7, loc="best")
            leg.set_zorder(20)                       # above the mean lines
            leg.set_draggable(True)                  # can be moved off the points with the mouse
            for ax, key in ((ax1, "drift"), (ax2, "disp")):
                dc.apply_limits(ax, ylim=dc.limit_pair(self.lim_vars[f"{key}_lo"].get(),
                                                        self.lim_vars[f"{key}_hi"].get()))
        else:
            ax1.text(0.5, 0.5, "Select criteria files on the left", ha="center", va="center",
                     transform=ax1.transAxes, color="#888")
        self.canvas.draw_idle()

    def _plot_extreme(self, axes, pool, ds_on, stat):
        """Minimum or maximum start drift per axial load and damage state over the shown files, whatever their
        mechanism; the lower chart shows the displacement of that same start (a real point of one file)."""
        pick = min if stat == "min" else max
        for k in ds_on:
            loads = sorted(pool[k])
            best = [pick(pool[k][a], key=lambda v: v[0]) for a in loads]
            for col, ax in enumerate(axes):
                pts = [(a, v[col]) for a, v in zip(loads, best) if v[col] is not None]
                if pts:
                    ax.plot(*zip(*pts), "-", color=dc.DS_COLORS[k], lw=2.2, marker=self.EXTREME_MARKER[stat], ms=7,
                            mec="white", mew=1.0, path_effects=self.HALO, zorder=10)

    def _fill_table(self, stats, ds_on, stat):
        """Table under the plot: per axial load and shown damage state n and mean / σ, or n, min / max and the
        file it comes from. Hidden without a statistic."""
        g = self.table.inner
        for w in g.winfo_children():
            w.destroy()
        ds = [k for k in ds_on if k in stats]
        self.table_rows = []
        if not ds:
            self.table_box.pack_forget()
            return
        third = "σ" if stat == "mean" else "file"
        self.table_title.configure(text=f"{stat} start drift [mm] of the shown files · n = values where the state "
                                        f"starts · " + ("σ = sample standard deviation (n − 1)" if stat == "mean" else
                                                        f"file = where the {stat} comes from (+1 = one more file)"))
        hdr = {"bg": "#D9D9D9", "relief": "ridge", "bd": 1}
        tk.Label(g, text="kN", width=7, **hdr).grid(row=0, column=0, rowspan=2, sticky="nsew")
        for j, k in enumerate(ds):
            tk.Label(g, text=k, bg=dc.DS_COLORS[k], fg=dc.DS_TEXT_COLORS[k], relief="ridge", bd=1).grid(
                row=0, column=1 + 3 * j, columnspan=3, sticky="nsew")
            for c, (text, w) in enumerate((("n", 3), (stat, 7), (third, 6 if third == "σ" else 22))):
                tk.Label(g, text=text, width=w, **hdr).grid(row=1, column=1 + 3 * j + c, sticky="nsew")
        self.table_rows.append(["kN"] + [f"{k} {t}" for k in ds for t in ("n", stat, third)])
        for i, a in enumerate(sorted({a for k in ds for a in stats[k]})):
            vals = [a]
            for k in ds:
                s = stats[k].get(a)
                if s is None:
                    vals += [None, None, None]
                elif stat == "mean":
                    vals += [s["n"], s["mean"], s["sigma"]]
                else:
                    files = s[f"{stat}_files"]
                    vals += [s["n"], s[stat], short_file_name(files[0]) + (f" +{len(files) - 1}" if len(files) > 1
                                                                           else "")]
            self.table_rows.append(vals)
            for c, v in enumerate(vals):
                text = (dc.fmt_kN(v) if c == 0 else "-" if v is None else v if isinstance(v, (str, int))
                        else f"{v:.2f}")
                tk.Label(g, text=text, bg="white", relief="ridge", bd=1, padx=3,
                         anchor="w" if isinstance(v, str) else "e").grid(row=2 + i, column=c, sticky="nsew")
        g.update_idletasks()
        self.table.canvas.configure(height=min(g.winfo_reqheight(), 190))     # a short table leaves room to the plot
        self.table_box.pack(side="bottom", fill="x", pady=(4, 0), before=self.canvas.get_tk_widget())

    def copy_table(self):
        """The table to the clipboard, tab-separated with the decimal sign of Windows (pastes into Excel)."""
        if not self.table_rows:
            return
        dec = system_decimal()
        text = "\n".join("\t".join("" if v is None else str(v) if isinstance(v, (str, int))
                                   else f"{v:.4f}".replace(".", dec) for v in row) for row in self.table_rows)
        self.app.clipboard_clear()
        self.app.clipboard_append(text)
        self.app.say("Compare table copied to the clipboard")

    def _plot_mean(self, axes, pool, ds_on):
        """Mean and ±1σ / ±2σ per axial load and damage state over the shown files, whatever their mechanism.
        σ is the sample standard deviation (n - 1), so it needs two files or more. Hollow points (threshold never
        reached) are left out. The states are shifted a little sideways so their bars do not hide each other."""
        loads_all = sorted({a for k in ds_on for a in pool[k]})
        span = (loads_all[-1] - loads_all[0]) or 1.0
        log = self.log_var.get()
        for j, k in enumerate(ds_on):
            loads = sorted(pool[k])
            step = (j - (len(ds_on) - 1) / 2) * 0.012
            xp = np.array([a * 10 ** step if log and a > 0 else a + step * span for a in loads])
            c = dc.DS_COLORS[k]
            for col, ax in enumerate(axes):                     # col 0 = start drift, col 1 = displacement
                # displacements of opposite sign (e.g. AX+ and AX-) get a mean each, never one mixed mean
                for sign in ((None,) if col == 0 else (1, -1)):
                    vals = [np.array([v[col] for v in pool[k][a] if v[col] is not None and
                                      (sign is None or (v[col] >= 0) == (sign > 0))], float) for a in loads]
                    keep = np.array([len(v) > 0 for v in vals])
                    if not keep.any():
                        continue
                    mean = np.array([v.mean() if len(v) else np.nan for v in vals])
                    sd = np.array([v.std(ddof=1) if len(v) > 1 else np.nan for v in vals])
                    bars = np.isfinite(sd)
                    if bars.any():
                        ax.errorbar(xp[bars], mean[bars], yerr=2 * sd[bars], fmt="none", ecolor=c, elinewidth=1.1,
                                    capsize=4, capthick=1.1, zorder=8)
                        ax.errorbar(xp[bars], mean[bars], yerr=sd[bars], fmt="none", ecolor=c, elinewidth=4,
                                    zorder=9)
                    ax.plot(xp[keep], mean[keep], "-", color=c, lw=2.2, marker="D", ms=6, mec="white", mew=1.0,
                            path_effects=self.HALO, zorder=10)


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
