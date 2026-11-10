#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single-panel pushover in ETABS - GUI
====================================

Tab 1  "Run ETABS pushover"
       Runs the pushover of the open panel model (default AX_API_attempt) for several axial loads:
       axial force in pattern G at joints 1198 / 1196, horizontal pattern HOR_sila x scale factor in the case
       PSH_test_DIN_sila, only Stalno_NL -> PSH_test_DIN_sila are run. The scale factor is adjusted until the
       horizontal displacement of joint 1198 ends between 0.2 and 1.0 m; stalled analyses are cancelled.
       Every result per step is saved to ETABS_results/<run name>/ (the ETABS results database of the workflow).

Tab 2  "Damage states & report"
       Applies a set of acceptance criteria to a saved run (no ETABS needed) and writes
       ETABS_results/<run>/Report - <criteria set>/  (Excel workbook + PNG plots), compared with Abaqus.
       Also exports the ETABS results as a database for Panel_plotter_GUI.py.

INPUT   panel_pushover_settings.json     everything set in the window (saved automatically)
        acceptance_criteria_sets.json    the criteria sets (edited in tab 2)
        abaqus_reference.json            Abaqus envelopes / curves and where the Abaqus results are
OUTPUT  ETABS_results/<run name>/...

Run:    python Panel_pushover_GUI.py
"""

import os
import queue
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel_core as pc             # noqa: E402

APP_TITLE = "Single-panel pushover in ETABS"


def open_path(path):
    if path and os.path.exists(path):
        os.startfile(path)                                  # noqa: S606


# =====================================================================================================
# EDITABLE TABLE
# =====================================================================================================

class EditableTable(ttk.Frame):
    """Treeview whose cells are edited in place (double click). columns: [(key, heading, width, kind)]
    kind: "text", "float", "optfloat" (empty allowed), "bool" (click toggles) or ("choice", [values])."""

    def __init__(self, master, columns, height=8):
        super().__init__(master)
        self.columns = columns
        self.rows = []
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height,
                                 selectmode="browse")
        for key, head, width, _kind in columns:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, anchor="center" if width < 110 else "w", stretch=width >= 110)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
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
            self.tree.insert("", "end", iid=str(i), values=[self._fmt(row.get(k), kind) for k, _, _, kind in self.columns])
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

    def add(self, row):
        self.rows.append(dict(row))
        self.refresh()
        self.tree.selection_set(str(len(self.rows) - 1))
        self._changed()

    def remove(self):
        i = self.selected_index()
        if i is not None:
            del self.rows[i]
            self.refresh()
            self._changed()

    def move(self, step):
        i = self.selected_index()
        if i is None or not 0 <= i + step < len(self.rows):
            return
        self.rows[i], self.rows[i + step] = self.rows[i + step], self.rows[i]
        self.refresh()
        self.tree.selection_set(str(i + step))
        self._changed()

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
        if kind == "bool":
            return
        self._close_editor(save=True)
        x, y, w, h = self.tree.bbox(str(r), self.tree["columns"][c])
        value = self._fmt(self.rows[r].get(key), kind)
        if isinstance(kind, tuple):
            widget = ttk.Combobox(self.tree, values=kind[1], state="readonly")
            widget.set(value)
            widget.bind("<<ComboboxSelected>>", lambda e: self._close_editor(True))
        else:
            widget = ttk.Entry(self.tree)
            widget.insert(0, value)
            widget.select_range(0, "end")
        widget.place(x=x, y=y, width=max(w, 60), height=h)
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
            if kind == "float":
                value = float(text.replace(",", "."))
            elif kind == "optfloat":
                value = float(text.replace(",", ".")) if text else None
            else:
                value = text
        except ValueError:
            messagebox.showerror(APP_TITLE, f"'{text}' is not a number")
            return
        self.rows[r][key] = value
        self.refresh()
        self._changed()


# =====================================================================================================
# THE WINDOW
# =====================================================================================================

LEVEL_COLUMNS = [("run", "Run", 45, "bool"), ("axial_kN", "Axial force [kN]", 110, "float"),
                 ("scale_factor", "Scale factor", 95, "float"),
                 ("scheme", "Solution scheme", 130, ("choice", [""] + list(pc.SOLUTION_SCHEMES)))]

SCALARS_RUN = [
    # key, label, type, width
    ("case", "Pushover case", str, 26), ("vertical_pattern", "Axial force pattern", str, 10),
    ("vertical_nodes", "Axial force at joints", list, 14), ("vertical_shares", "Shares", "floatlist", 10),
    ("horizontal_pattern", "Horizontal pattern", str, 10), ("control_node", "Control joint", str, 8),
    ("control_dof", "Control DOF", ("choice", ["U1", "U2", "U3"]), 5),
]
SCALARS_CONTROL = [
    ("disp_min", "Min displacement [m]", float, 7), ("disp_max", "Max displacement [m]", float, 7),
    ("auto_adjust", "Adjust the scale factor automatically", bool, 0), ("max_attempts", "Max analyses per load", int, 5),
    ("factor_down", "Factor when too high", float, 6), ("factor_up", "Factor when too low", float, 6),
    ("fallback_on_stall", "Failed / stalled below the window: retry with ->", bool, 0),
    ("fallback_schemes", "Fallback schemes, in order", list, 30),
    ("solution_scheme", "Solution scheme (empty = as in model)", ("choice", [""] + list(pc.SOLUTION_SCHEMES)), 15),
    ("cancel_stalled", "Press Cancel in ETABS when an analysis stalls", bool, 0),
    ("stall_minutes", "Stalled after [min] without progress", float, 6),
    ("stall_min_progress_s", "Progress needed [s of analysis time]", float, 6),
    ("jump_mm", "Valid data: no jump > [mm]", float, 6), ("jump_ratio", "... and > x every earlier step", float, 6),
    ("vertical_tolerance", "Vertical force tolerance [-]", float, 6),
    ("max_analysis_minutes", "Cancel any analysis after [min]", float, 6),
    ("run_only_needed_cases", "Run only the pushover case and its initial cases", bool, 0),
    ("restore_run_flags", "At the end restore run flags and solver settings", bool, 0),
]
CHECK_COLUMNS = [("use_for_DS", "DS", 35, "bool"), ("name", "Check", 210, "text"),
                 ("quantity", "Quantity", 120, ("choice", [])), ("sense", "Sense", 50, ("choice", ["+", "-", "abs"])),
                 ("relative_to_start", "Rel.", 40, "bool"), ("ND", "ND end", 65, "optfloat"),
                 ("LD", "LD end", 65, "optfloat"), ("SD", "SD end", 65, "optfloat"), ("NC", "NC end", 65, "optfloat")]


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        w = min(1400, self.winfo_screenwidth() - 40)
        h = min(940, self.winfo_screenheight() - 90)          # stay above the taskbar
        self.geometry(f"{w}x{h}+10+5")
        self.minsize(1000, 600)
        self.s = pc.load_settings()
        self.criteria = pc.load_criteria()
        self.reference = pc.load_reference()
        self.q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.vars = {}

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Go.TButton", font=("Segoe UI", 10, "bold"))

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=6, pady=6)
        self.nb = ttk.Notebook(panes)
        panes.add(self.nb, weight=3)
        logf = ttk.LabelFrame(panes, text="Log")
        panes.add(logf, weight=1)
        self.log_text = ScrolledText(logf, height=7, font=("Consolas", 9), wrap="none")
        self.log_text.pack(fill="both", expand=True)

        self._build_run_tab()
        self._build_report_tab()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.after(300, self.refresh_instances)
        self.after(400, self.refresh_runs)

    # ------------------------------------------------------------------ helpers
    def log(self, text):
        self.q.put(("log", str(text)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                elif kind == "done":
                    self._set_busy(False)
                    if callable(payload):
                        payload()
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in self.busy_buttons:
            b.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def run_task(self, title, fn, then=None):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Something is still running - wait or press Stop.")
            return
        self.stop_event.clear()
        self._set_busy(True)
        self.log(f"\n--- {title} ---")

        def target():
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                comtypes = None
            try:
                fn()
            except pc.PanelError as e:
                self.log(f"ERROR: {e}")
            except Exception:
                self.log("ERROR:\n" + traceback.format_exc())
            finally:
                if comtypes:
                    try:
                        comtypes.CoUninitialize()
                    except Exception:
                        pass
                self.q.put(("done", then))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _scalar_widgets(self, parent, specs, columns=2):
        for i, (key, label, kind, width) in enumerate(specs):
            r, c = divmod(i, columns)
            value = self.s.get(key)
            if kind is bool:
                var = tk.BooleanVar(value=bool(value))
                ttk.Checkbutton(parent, text=label, variable=var).grid(row=r, column=2 * c, columnspan=2, sticky="w",
                                                                       padx=4, pady=2)
            else:
                ttk.Label(parent, text=label).grid(row=r, column=2 * c, sticky="w", padx=4, pady=2)
                if kind in (list, "floatlist"):
                    text = ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in (value or []))
                else:
                    text = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
                var = tk.StringVar(value=text)
                if isinstance(kind, tuple):
                    w = ttk.Combobox(parent, textvariable=var, values=kind[1], width=width, state="readonly")
                else:
                    w = ttk.Entry(parent, textvariable=var, width=width)
                w.grid(row=r, column=2 * c + 1, sticky="w", padx=4, pady=2)
            self.vars[key] = (var, kind)

    def collect(self):
        """Settings from the window (raises PanelError on a bad number)."""
        s = dict(self.s)
        for key, (var, kind) in self.vars.items():
            value = var.get()
            try:
                if kind is bool:
                    s[key] = bool(value)
                elif kind is float:
                    s[key] = float(str(value).replace(",", "."))
                elif kind is int:
                    s[key] = int(float(value))
                elif kind is list:
                    s[key] = [v.strip() for v in str(value).split(",") if v.strip()]
                elif kind == "floatlist":
                    s[key] = [float(v) for v in str(value).replace(";", ",").split(",") if v.strip()]
                elif kind == "optfloat":
                    s[key] = float(value) if str(value).strip() else None
                else:
                    s[key] = str(value).strip()
            except ValueError:
                raise pc.PanelError(f"'{value}' is not a valid value for {key}") from None
        s["levels"] = self.levels.get_rows()
        s["etabs_instance"] = self.instance_var.get().split(" - ")[0].strip()
        s["runs_folder"] = self.runs_folder_var.get().strip() or "ETABS_results"
        s["run_name"] = self.run_name_var.get().strip()
        s["report_run"] = self.report_run_var.get()
        s["criteria_set"] = self.set_var.get()
        self.s = s
        return s

    def save_settings(self):
        try:
            pc.save_json(self.collect(), pc.SETTINGS_FILE, indent=2)
        except pc.PanelError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return False
        return True

    def _close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "An analysis or report is still running. Close anyway?\n"
                                                  "(ETABS finishes the current analysis by itself.)"):
                return
        try:
            pc.save_json(self.collect(), pc.SETTINGS_FILE, indent=2)
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------ tab 1
    def _build_run_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  1 · Run ETABS pushover  ")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)

        f = ttk.LabelFrame(tab, text="ETABS")
        f.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Label(f, text="Model (running ETABS):").pack(side="left", padx=4)
        self.instance_var = tk.StringVar(value=self.s.get("etabs_instance", ""))
        self.instance_box = ttk.Combobox(f, textvariable=self.instance_var, width=70)
        self.instance_box.pack(side="left", padx=4, pady=4)
        ttk.Button(f, text="Refresh", command=self.refresh_instances).pack(side="left", padx=2)
        b_check = ttk.Button(f, text="Check model", command=self.check_model)
        b_check.pack(side="left", padx=2)

        f = ttk.Frame(tab)
        f.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Label(f, text="Results folder:").pack(side="left")
        self.runs_folder_var = tk.StringVar(value=self.s.get("runs_folder", "ETABS_results"))
        ttk.Entry(f, textvariable=self.runs_folder_var, width=24).pack(side="left", padx=4)
        ttk.Button(f, text="...", width=3, command=self._browse_runs).pack(side="left")
        ttk.Label(f, text="   Run name (empty = date and time; an existing run gets the forces added):").pack(
            side="left")
        self.run_name_var = tk.StringVar(value=self.s.get("run_name", ""))
        ttk.Entry(f, textvariable=self.run_name_var, width=26).pack(side="left", padx=4)
        b_run = ttk.Button(f, text="Run selected axial forces", style="Go.TButton", command=self.run_pushover)
        b_run.pack(side="right", padx=4)
        self.stop_button = ttk.Button(f, text="Stop after this analysis", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=4)
        self.busy_buttons = [b_check, b_run]

        f = ttk.LabelFrame(tab, text="Model")
        f.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        self._scalar_widgets(f, SCALARS_RUN, columns=2)

        f = ttk.LabelFrame(tab, text="Axial forces and scale factors (double click to edit, click Run to toggle)")
        f.grid(row=2, column=1, rowspan=2, sticky="nsew", padx=4, pady=4)
        tab.rowconfigure(3, weight=1)
        self.levels = EditableTable(f, LEVEL_COLUMNS, height=12)
        self.levels.pack(fill="both", expand=True, padx=4, pady=4)
        self.levels.set_rows([{"scheme": "", **lv} for lv in self.s["levels"]])
        bf = ttk.Frame(f)
        bf.pack(fill="x", padx=4, pady=2)
        ttk.Button(bf, text="Add", command=lambda: self.levels.add(
            {"run": True, "axial_kN": 100.0, "scale_factor": 40.0, "scheme": ""})).pack(side="left")
        ttk.Button(bf, text="Remove", command=self.levels.remove).pack(side="left", padx=2)
        ttk.Button(bf, text="Up", command=lambda: self.levels.move(-1)).pack(side="left", padx=2)
        ttk.Button(bf, text="Down", command=lambda: self.levels.move(1)).pack(side="left", padx=2)
        ttk.Button(bf, text="All on", command=lambda: self._all_levels(True)).pack(side="left", padx=8)
        ttk.Button(bf, text="All off", command=lambda: self._all_levels(False)).pack(side="left")
        ttk.Button(bf, text="Defaults", command=lambda: self.levels.set_rows(
            [{"scheme": "", **lv} for lv in pc.default_levels()])).pack(side="right")
        ttk.Label(f, text="Scale factor: multiplies the horizontal pattern in the case. It is adjusted per load until "
                          "the control joint ends between the min and max displacement; the factor that worked is "
                          "written back into this table.", wraplength=520, foreground="#555555").pack(fill="x", padx=4)

        f = ttk.LabelFrame(tab, text="Analysis control")
        f.grid(row=3, column=0, sticky="nsew", padx=4, pady=4)
        self._scalar_widgets(f, SCALARS_CONTROL, columns=2)

    def _all_levels(self, on):
        rows = self.levels.get_rows()
        for r in rows:
            r["run"] = on
        self.levels.set_rows(rows)

    def _browse_runs(self):
        d = filedialog.askdirectory(initialdir=pc.resolve(self.runs_folder_var.get() or "ETABS_results"))
        if d:
            self.runs_folder_var.set(d)
            self.refresh_runs()

    def _stop(self):
        self.stop_event.set()
        self.log("Stop requested - the tool stops after the current analysis (cancel it in ETABS to stop now).")

    def refresh_instances(self):
        """List the running ETABS in a thread (a busy ETABS answers only after its analysis)."""
        def work():
            try:
                import comtypes
                comtypes.CoInitialize()
                inst = pc.list_instances()
            except Exception as e:
                self.log(f"Could not list ETABS: {e}")
                return
            self.q.put(("call", lambda: self._show_instances(inst)))

        threading.Thread(target=work, daemon=True).start()

    def _show_instances(self, inst):
        values = [f"{i['model'] or i['pid']} - process {i['pid']}: {i['model_file'] or '(no model)'}" for i in inst]
        self.instance_box["values"] = values
        current = self.instance_var.get().split(" - ")[0].strip().lower()
        match = [v for v in values if v.split(" - ")[0].lower() == current]
        if match:
            self.instance_var.set(match[0])
        elif values and not current:
            self.instance_var.set(values[0])
        if not values:
            self.log("No running ETABS found.")

    def check_model(self):
        if not self.save_settings():
            return
        s = dict(self.s)

        def job():
            m = pc.connect(s["etabs_instance"])
            info = pc.model_info(m, s)
            self.log(pc.describe_model(info, s))
            self.log(f"Solution scheme of {s['case']}: {pc.case_scheme(m, s)}")

        self.run_task("Check model", job)

    def run_pushover(self):
        if not self.save_settings():
            return
        s = dict(self.s)
        todo = [lv for lv in s["levels"] if lv.get("run")]
        if not todo:
            messagebox.showinfo(APP_TITLE, "No axial force is selected (column Run).")
            return
        run_dir = pc.new_run_dir(s)
        text = "\n".join(f"    {lv['axial_kN']:g} kN, scale factor {lv['scale_factor']:g}" for lv in todo)
        if not messagebox.askyesno(APP_TITLE, f"Run the pushover in ETABS for\n{text}\n\nThe model is changed "
                                              f"(loads in pattern {s['vertical_pattern']}, scale factor and solution "
                                              f"scheme of {s['case']}) and saved by ETABS before each analysis.\n\n"
                                              f"Results: {run_dir}"):
            return
        stop = self.stop_event

        def job():
            pc.run_levels(s, run_dir, log=self.log, stop=stop)

        def then():
            self._update_scale_factors(run_dir)
            self.refresh_runs(select=os.path.basename(run_dir))

        self.run_task("Pushover", job, then)

    def _update_scale_factors(self, run_dir):
        """Write the scale factors that worked back into the table (the next run starts from them)."""
        try:
            run = pc.load_json(os.path.join(run_dir, "run.json"))
        except Exception:
            return
        rows = self.levels.get_rows()
        changed = []
        for r in rows:
            done = run.get("levels", {}).get(pc.level_tag(float(r["axial_kN"])))
            if done and done.get("in_window") and done.get("scale_factor"):
                if abs(done["scale_factor"] - r["scale_factor"]) > 1e-9:
                    changed.append(f"{r['axial_kN']:g} kN: {r['scale_factor']:g} -> {done['scale_factor']:.4g}")
                r["scale_factor"] = round(done["scale_factor"], 4)
        self.levels.set_rows(rows)
        if changed:
            self.log("Scale factors updated in the table: " + "; ".join(changed))
        self.save_settings()

    # ------------------------------------------------------------------ tab 2
    def _build_report_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  2 · Damage states & report  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        f = ttk.LabelFrame(tab, text="Saved run")
        f.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self.report_run_var = tk.StringVar(value=self.s.get("report_run", ""))
        self.run_box = ttk.Combobox(f, textvariable=self.report_run_var, width=60, state="readonly")
        self.run_box.pack(side="left", padx=4, pady=4)
        self.run_box.bind("<<ComboboxSelected>>", lambda e: self._show_run_info())
        ttk.Button(f, text="Refresh", command=self.refresh_runs).pack(side="left", padx=2)
        ttk.Button(f, text="Open run folder", command=lambda: open_path(self._run_dir())).pack(side="left", padx=2)
        self.run_info = ttk.Label(f, text="", foreground="#444444")
        self.run_info.pack(side="left", padx=8)

        f = ttk.LabelFrame(tab, text="Acceptance criteria (limits in mm = END of each damage state; double click to "
                                     "edit; DS = the check decides the damage state of the panel)")
        f.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        top = ttk.Frame(f)
        top.pack(fill="x", padx=4, pady=4)
        ttk.Label(top, text="Set:").pack(side="left")
        self.set_var = tk.StringVar(value=self.s.get("criteria_set") or next(iter(self.criteria["sets"]), ""))
        self.set_box = ttk.Combobox(top, textvariable=self.set_var, width=48, state="readonly",
                                    values=list(self.criteria["sets"]))
        self.set_box.pack(side="left", padx=4)
        self.set_box.bind("<<ComboboxSelected>>", lambda e: self._load_set())
        ttk.Button(top, text="Save set", command=self._save_set).pack(side="left", padx=2)
        ttk.Button(top, text="Save as new set...", command=self._save_set_as).pack(side="left", padx=2)
        ttk.Button(top, text="Delete set", command=self._delete_set).pack(side="left", padx=2)
        ttk.Label(top, text="  Note:").pack(side="left")
        self.note_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.note_var, width=50).pack(side="left", padx=4, fill="x", expand=True)
        cols = [list(c) for c in CHECK_COLUMNS]
        cols[2][3] = ("choice", [q["key"] for q in self.s["quantities"]])
        self.checks = EditableTable(f, [tuple(c) for c in cols], height=7)
        self.checks.pack(fill="both", expand=True, padx=4)
        bf = ttk.Frame(f)
        bf.pack(fill="x", padx=4, pady=2)
        ttk.Button(bf, text="Add check", command=lambda: self.checks.add(
            {"use_for_DS": True, "name": "new check", "quantity": "ax_uplift", "sense": "+",
             "relative_to_start": False, "ND": None, "LD": None, "SD": None, "NC": None})).pack(side="left")
        ttk.Button(bf, text="Remove", command=self.checks.remove).pack(side="left", padx=2)
        ttk.Button(bf, text="Up", command=lambda: self.checks.move(-1)).pack(side="left", padx=2)
        ttk.Button(bf, text="Down", command=lambda: self.checks.move(1)).pack(side="left", padx=2)
        quantities = "   ".join(f"{q['key']} = {q['name']}" for q in self.s["quantities"])
        ttk.Label(f, text="Sense: + positive values are damage (opening), - negative values (closing), abs both.   "
                          "Rel.: from the value at step 0 (after the axial force).\nQuantities: " + quantities,
                  wraplength=1200, foreground="#555555").pack(fill="x", padx=4, pady=2)
        self._load_set()

        f = ttk.LabelFrame(tab, text="Abaqus comparison")
        f.grid(row=2, column=0, sticky="ew", padx=4, pady=4)
        ab = self.reference.get("abaqus", {})
        self.ab_compare = tk.BooleanVar(value=self.s.get("abaqus_compare", True))
        ttk.Checkbutton(f, text="Compare with Abaqus", variable=self.ab_compare).grid(row=0, column=0, sticky="w")
        self.ab_db = tk.StringVar(value=ab.get("database", ""))
        self.ab_family = tk.StringVar(value=ab.get("family", ""))
        self.ab_wb = tk.StringVar(value=ab.get("ds_workbook", ""))
        self.ab_ds_family = tk.StringVar(value=ab.get("ds_family", ""))
        ttk.Label(f, text="Results database:").grid(row=1, column=0, sticky="w", padx=4)
        ttk.Entry(f, textvariable=self.ab_db, width=70).grid(row=1, column=1, sticky="w")
        ttk.Button(f, text="...", width=3, command=lambda: self._browse_dir(self.ab_db)).grid(row=1, column=2)
        ttk.Label(f, text="Family (curves):").grid(row=1, column=3, sticky="w", padx=4)
        self.fam_box = ttk.Combobox(f, textvariable=self.ab_family, width=26)
        self.fam_box.grid(row=1, column=4, sticky="w")
        ttk.Label(f, text="Damage-state workbook:").grid(row=2, column=0, sticky="w", padx=4)
        ttk.Entry(f, textvariable=self.ab_wb, width=70).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="...", width=3, command=lambda: self._browse_file(self.ab_wb)).grid(row=2, column=2)
        ttk.Label(f, text="Family (damage states):").grid(row=2, column=3, sticky="w", padx=4)
        self.dsfam_box = ttk.Combobox(f, textvariable=self.ab_ds_family, width=26)
        self.dsfam_box.grid(row=2, column=4, sticky="w")
        ttk.Button(f, text="Find families", command=self._find_families).grid(row=1, column=5, padx=4)
        ttk.Button(f, text="Envelopes / curves file", command=lambda: open_path(pc.REFERENCE_FILE)).grid(
            row=2, column=5, padx=4)

        f = ttk.Frame(tab)
        f.grid(row=3, column=0, sticky="ew", padx=4, pady=6)
        self.running_var = tk.BooleanVar(value=self.s.get("running_damage", True))
        ttk.Checkbutton(f, text="Damage never decreases", variable=self.running_var).pack(side="left")
        ttk.Label(f, text="   Drift axis max [mm] (empty = automatic):").pack(side="left")
        self.xmax_var = tk.StringVar(value="" if self.s.get("drift_plot_max_mm") is None
                                     else f"{self.s['drift_plot_max_mm']:g}")
        ttk.Entry(f, textvariable=self.xmax_var, width=8).pack(side="left", padx=4)
        b_rep = ttk.Button(f, text="Make report", style="Go.TButton", command=self.make_report)
        b_rep.pack(side="right", padx=4)
        b_db = ttk.Button(f, text="Export database for Panel_plotter_GUI", command=self.export_db)
        b_db.pack(side="right", padx=4)
        ttk.Button(f, text="Open report folder", command=self._open_report).pack(side="right", padx=4)
        self.busy_buttons_report = [b_rep, b_db]

    def _browse_dir(self, var):
        d = filedialog.askdirectory(initialdir=pc.resolve(var.get()) or pc.TOOL_DIR)
        if d:
            var.set(d)

    def _browse_file(self, var):
        p = filedialog.askopenfilename(initialdir=os.path.dirname(pc.resolve(var.get()) or pc.TOOL_DIR),
                                       filetypes=[("Excel", "*.xlsx"), ("All", "*.*")])
        if p:
            var.set(p)

    def _find_families(self):
        try:
            db = pc.AbaqusDB(pc.resolve(self.ab_db.get()))
            fams = sorted({j.rpartition("_")[0] for jobs in db.mapping.values() for j in jobs})
            self.fam_box["values"] = fams
            self.log(f"Abaqus database: {len(db.mapping)} sets, families: {', '.join(fams)}")
        except Exception as e:
            self.log(f"Abaqus database: {e}")
        try:
            import openpyxl
            wb = openpyxl.load_workbook(pc.resolve(self.ab_wb.get()), read_only=True)
            fams = [r[0] for r in list(wb["_meta"].iter_rows(values_only=True))[1:] if r and r[0]]
            self.dsfam_box["values"] = fams
            self.log(f"Damage-state workbook families: {', '.join(fams)}")
        except Exception as e:
            self.log(f"Damage-state workbook: {e}")

    def _run_dir(self):
        name = self.report_run_var.get()
        return os.path.join(pc.resolve(self.runs_folder_var.get() or "ETABS_results"), name) if name else ""

    def refresh_runs(self, select=None):
        runs = pc.list_runs(self.runs_folder_var.get() or "ETABS_results")
        self.run_box["values"] = runs
        if select and select in runs:
            self.report_run_var.set(select)
        elif self.report_run_var.get() not in runs:
            self.report_run_var.set(runs[0] if runs else "")
        self._show_run_info()

    def _show_run_info(self):
        d = self._run_dir()
        try:
            run = pc.load_json(os.path.join(d, "run.json"))
            lv = sorted(run.get("levels", {}).values(), key=lambda x: x["axial_kN"])
            text = "   ".join(f"{x['axial_kN']:g} kN: SF {x['scale_factor']:.4g}{'' if x['in_window'] else ' (!)'}"
                               for x in lv if x.get("scale_factor"))
            self.run_info.configure(text=text or "no levels yet")
        except Exception:
            self.run_info.configure(text="")

    def _load_set(self):
        cs = self.criteria["sets"].get(self.set_var.get())
        if not cs:
            self.checks.set_rows([])
            return
        self.note_var.set(cs.get("note", ""))
        rows = []
        for c in cs["checks"]:
            rows.append({"use_for_DS": c.get("use_for_DS", True), "name": c["name"], "quantity": c["quantity"],
                         "sense": c.get("sense", "+"), "relative_to_start": c.get("relative_to_start", False),
                         **{k: c["limits"].get(k) for k in pc.DS_NAMES}})
        self.checks.set_rows(rows)

    def _set_from_table(self):
        checks = []
        for r in self.checks.get_rows():
            checks.append({"name": r["name"], "quantity": r["quantity"], "sense": r["sense"],
                           "relative_to_start": bool(r.get("relative_to_start")),
                           "use_for_DS": bool(r.get("use_for_DS")),
                           "limits": {k: r.get(k) for k in pc.DS_NAMES}})
        return {"note": self.note_var.get(), "checks": checks}

    def _save_set(self, name=None):
        name = name or self.set_var.get()
        if not name:
            return self._save_set_as()
        self.criteria["sets"][name] = self._set_from_table()
        pc.save_criteria(self.criteria)
        self.set_box["values"] = list(self.criteria["sets"])
        self.set_var.set(name)
        self.log(f"Criteria set '{name}' saved to {os.path.basename(pc.CRITERIA_FILE)}")

    def _save_set_as(self):
        win = tk.Toplevel(self)
        win.title("New criteria set")
        ttk.Label(win, text="Name of the new set:").pack(padx=8, pady=6)
        var = tk.StringVar(value=self.set_var.get() + " (copy)")
        e = ttk.Entry(win, textvariable=var, width=50)
        e.pack(padx=8)
        e.focus_set()

        def ok():
            name = var.get().strip()
            if name:
                win.destroy()
                self._save_set(name)

        ttk.Button(win, text="Save", command=ok).pack(pady=6)
        win.bind("<Return>", lambda ev: ok())

    def _delete_set(self):
        name = self.set_var.get()
        if name and messagebox.askyesno(APP_TITLE, f"Delete the criteria set '{name}'?"):
            self.criteria["sets"].pop(name, None)
            pc.save_criteria(self.criteria)
            self.set_box["values"] = list(self.criteria["sets"])
            self.set_var.set(next(iter(self.criteria["sets"]), ""))
            self._load_set()

    def _report_settings(self):
        if not self.save_settings():
            return None
        s = dict(self.s)
        s["running_damage"] = self.running_var.get()
        s["abaqus_compare"] = self.ab_compare.get()
        x = self.xmax_var.get().strip()
        s["drift_plot_max_mm"] = float(x) if x else None
        self.s.update({k: s[k] for k in ("running_damage", "abaqus_compare", "drift_plot_max_mm")})
        pc.save_json(self.s, pc.SETTINGS_FILE, indent=2)
        ref = pc.load_reference()
        ref["abaqus"].update({"database": self.ab_db.get(), "family": self.ab_family.get(),
                              "ds_workbook": self.ab_wb.get(), "ds_family": self.ab_ds_family.get()})
        pc.save_json(ref, pc.REFERENCE_FILE, indent=1)
        return s

    def make_report(self):
        s = self._report_settings()
        if s is None:
            return
        run_dir = self._run_dir()
        if not run_dir:
            messagebox.showinfo(APP_TITLE, "Choose a saved run first.")
            return
        self._save_set()
        name = self.set_var.get()
        import panel_report

        def job():
            folder = panel_report.make_report(run_dir, name, s, log=self.log)
            self.last_report = folder

        self.run_task("Report", job)

    def export_db(self):
        s = self._report_settings()
        run_dir = self._run_dir()
        if s is None or not run_dir:
            return
        import panel_report
        self.run_task("Database export", lambda: panel_report.export_database(run_dir, s, log=self.log))

    def _open_report(self):
        d = self._run_dir()
        import panel_report
        p = panel_report.report_folder(d, self.set_var.get()) if d else ""
        open_path(p if os.path.isdir(p) else d)


def main():
    app = App()
    app.busy_buttons = app.busy_buttons + app.busy_buttons_report
    app.mainloop()


if __name__ == "__main__":
    main()
