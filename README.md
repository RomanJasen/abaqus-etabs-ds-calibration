# Damage-state calibration: Abaqus panel → ETABS links

This workflow does two things:
1. It defines the element damage states DS0–DS5 of the JU61 wall panel from the Abaqus panel models.
2. It calibrates the acceptance criteria of the ETABS link elements so that the ETABS panel reaches the same damage
   states.

The engineer chooses the damage states. The tools start from initial values taken only from the section
displacements and the default thresholds.

```
0_INPUT/Abaqus_databases/<database>/         Abaqus "Panel_results" databases (input, not changed)
        │
        ▼
STEP 1  1_Abaqus_criteria/Abaqus_criteria_GUI.py
        per mechanism (section pair) and load-case group: drift ⇄ section displacement at the start of DS1…DS5,
        per axial load
        → 1_Abaqus_criteria/criteria_files/<name>.json   (mark the ones to use as "final")
        │
STEP 2  2_ETABS_pushover/Panel_pushover_GUI.py     (tab 1: pushover of the panel model for every axial load)
        → 2_ETABS_pushover/ETABS_results/<run>/    (the ETABS results database)
        │
        ▼
STEP 3  3_ETABS_calibration/ETABS_calibration_GUI.py
        ETABS and Abaqus pushover curves together, damage states per mechanism, ONE set of ETABS link criteria
        → 3_ETABS_calibration/etabs_criteria/<name>.json
          (+ optional export acceptance_criteria_from_calibration.json in the project format)
```

Start the tools with the `.bat` files in this folder, or with `python <script>` in the tool's folder.
- They need Python 3 with numpy, matplotlib and openpyxl.
- The ETABS pushover also needs comtypes and a running ETABS with the panel model open.

**Input files.** The Abaqus databases in `0_INPUT/Abaqus_databases/` come from the
[Abaqus-scripts-for-output-processing](https://github.com/RomanJasen/Abaqus-scripts-for-output-processing)
repository: the database is extracted from the Abaqus results with the resources of that repository.

**This repository** holds the code: the three tools, `_library/`, the `.bat` launchers, the step-1 defaults file
and the step-2 settings. The input and the results are not in it: no Abaqus databases (`0_INPUT/`), ETABS model or
ETABS results, `_docs/` or `_previous_tools/`. `examples/` holds a few output files of step 1 (criteria files,
among them a statistic of several mechanisms) to show the format; copy them into `1_Abaqus_criteria/criteria_files/`
to use them, which needs the Abaqus database they name.

## Folders

| folder | content |
|---|---|
| `0_INPUT/Abaqus_databases/` | Abaqus results databases `<SET>/<JOB>/*.txt`. Put new databases here; step 1 lists every sub-folder with that layout. |
| `1_Abaqus_criteria/` | Step 1 GUI. `defaults/` holds the default mechanisms and thresholds. `criteria_files/` holds the output, one JSON file per mechanism, location and group. |
| `2_ETABS_pushover/` | ETABS pushover tool (unchanged apart from paths). `ETABS_results/` is the ETABS results database. |
| `3_ETABS_calibration/` | Step 3 GUI. `etabs_criteria/` holds the ETABS link criteria. |
| `_library/ds_common.py` | Shared code: DS scheme, Abaqus database reader, groups, mechanisms, drift ⇄ value. |
| `_docs/` | Handovers: DS0–DS5 definitions, the project format, the Abaqus model, the older workflow. `Damage_state_criteria_literature.xlsx`: damage-state criteria from the survey and RC damage literature, per document and per failure mode, with references. Numbered copies `_v2` to `_v5`; `_v5` is the latest and holds walls and precast joints only, `_v5_BC` the beam, column and frame criteria for reference. `DS_ladder_demo.xlsx`: a suggested DS ladder with its sources and a live test on the Abaqus results; a proposal, not applied to Step 1. `DS_definitions_explainer.md`: how each kind of damage is placed on DS0–DS5, with the demo's thresholds. `Literature from XLS/`: copies of its sources sorted A, B, C, with `Literature sources.xlsx` (source of each file) and `HANDOVER_literature_summaries.md`. |
| `_previous_tools/` | The earlier tools (`Damage_states_GUI.py`, `Panel_plotter_GUI.py`, legacy scripts, Excel workbooks) and an old copy of them from the ETABS folder. |
| `The ETABS pushover set of scripts/` | The ETABS model `AX_API_attempt.EDB` and `ALL AXIAL FINAL.xlsx`. ETABS had the model open, so it was left here; see the notes. |

## Damage states (same scheme everywhere)

Source: `_docs/HANDOVER_element_damage_states_DS0-5.md`.

- Every value is the **start** of a state (a lower bound): an element is DSk when its value is **greater than** the
  DSk threshold. Below DS1 it is DS0.
- A **negative** threshold (e.g. compression entered as −1, −2, …) is reached when the value drops **below** it.
  Thresholds must grow in size from DS1 to DS5. The project export writes them as sizes.
- Empty or `null` means not defined. `*` marks a provisional value, shown in orange.

| state | colour |
|---|---|
| DS0 | grey `#A6A6A6` |
| DS1 | green `#00B050` |
| DS2 | blue `#00A6FF` |
| DS3 | amber `#FFC000` |
| DS4 | red `#FF0000` |
| DS5 | black `#000000` |

## Step 1: Abaqus acceptance criteria (`1_Abaqus_criteria/Abaqus_criteria_GUI.py`)

**Mechanism.** A mechanism is the displacement of section B relative to section A in one component. Its **sense**
says which values are damage: `+` positive values (opening), `-` negative values (closing), `abs` both.

Values are measured from **absolute 0**. Tick "from zero drift" to measure them from frame 0 instead, i.e. from the
state after the axial load. The defaults are below; change them in the GUI or in `defaults/*.json`.

| mechanism | section B − section A | comp. | sense | initial thresholds DS1…DS5 [mm] | other locations |
|---|---|---|---|---|---|
| AX+ (uplift, heel) | SPEC DM1 − RU1 | Uz | + | 0.5, 1.581\*, 5, 15, – | – |
| AX- (crushing, toe) | SPEC GPL2 − RU1 | Uz | − | 1.0, 1.414\*, 2.0, 3.0, 4.6 | GM2 − RU1, DPL2 − RU1 |
| SH (sliding) | SREDNJI1 (point 1) − RU1 | Ux | abs | 0.01, 0.0707\*, 0.5, 3.0, – | – |
| REB (vertical bars) | SPEC DM2 − RU1 | Uz | abs | 0.12, 0.537\*, 2.4, 12.0, – | DPL2 − RU1 (toe) |

- **Locations** (the dropdown next to the mechanism) are presets of the defaults file: the `locations` list of each
  mechanism in `defaults/default_mechanisms.json` (or the defaults file in use, shown under the thresholds).
  - Choosing one sets section A, section B and the component. Other sections show as *custom*.
  - *Save as new location…* stores the current sections and component under a name you give. *Rename…* and
    *Delete* act on the location shown. Only that list in the defaults file changes; saved criteria files keep
    their sections. A file whose sections match a preset shows that preset's name.
- The thresholds are the project's current link thresholds: `HANDOVER_element_damage_states_DS0-5.md` §4.
  - DS1 / DS3 / DS4 come from MK4.
  - AX- DS5 comes from the calibration file.
  - DS2 is a provisional √(DS1·DS3).
- Pushover curve: drift = RU2.Ux − RU1.Ux, force = RU1.Fx / 1000 [kN].
- Collapse: the data end before the first step that jumps more than 5 mm **and** more than 10 × every earlier
  step. The step is tested on the drift, the loading point (RU2.Uz − RU1.Uz) and the mechanism value, so every
  mechanism of a job is cut at the same frame.

**Load-case group.** A group is a name filter on the job names:
- `SHEAR NOKEY`: every part must be in the name;
- `TIE|FR05`: either part;
- `-BEND`: the part must not be in the name.

Presets come from the defaults file and from the families of the database. If a group holds several families,
there is one row per job.

**The table.** It has one row per job (axial load). For each of DS1…DS5 it has a **drift** and a **disp** cell.
- Type either one and press Enter; the other follows from the job's curve.
  - disp → drift: the first drift where the value **exceeds** the threshold (linear interpolation).
  - drift → disp: the value at that drift.
- An empty cell goes back to the default threshold of the file. `-` means not defined.
- Cell colours:

  | colour | meaning |
  |---|---|
  | yellow | typed |
  | green | from the curve |
  | white | default threshold |
  | grey | not defined / not reached (`n.r.`) |

- Clicking a chart sets the selected cell (drift, or displacement on the lower chart) and moves to the next state.

**Charts.**
- Upper: the pushover curves.
- Lower: the mechanism value vs drift, with the thresholds of the selected row.
- Axial loads can be ticked on and off.
- **Axis limits** (empty = automatic). With only the drift range limited, y is fitted to the curves inside it. The
  frames after a collapse are dotted and never set the scale.

**Top chart…** (pop-up) chooses what the top chart plots.
- Each axis is (section − section).column × factor, with an axis label.
- Presets: *SHEAR: drift vs base force Fx* and *BEND: rotation fiy [mrad] vs base moment My [kNm]*. You can add
  your own.
- With *automatic choice* on, a group whose jobs all contain the preset's match text (e.g. BEND) gets that preset.
- *Save as default* stores the presets in the defaults file.
- Only the plot changes: the drifts in the table always use the drift of the criteria file.
- When the top x is not the drift, a click on the top chart takes the drift of the nearest point of the selected
  job's curve.

**Lowest drift…** (optional window) uses one criteria file per mechanism; the file open in the editor can be used
as it is now.
- It shows the pushover curves (as in the top chart), with the **lowest start drift of every damage state over
  all mechanisms**.
- Each point is labelled with the mechanism that gives it, and a table lists the same values.
- **Axes:** by default the window plots what the top chart plots. Untick *same as the top chart* to choose a preset
  of its own, e.g. *BEND: drift vs base moment My*. *Edit axes…* defines your own axes with X and Y limits; *Save
  as default* stores them in the defaults file under `envelope_chart`. The points always sit at their start drift.
- The label colours can be changed; *Save colours as default* stores them in the defaults file.

**Files.**
- *Auto name* forms the name from the mechanism, the sections and the group, e.g.
  `AX- RU1 vs SPEC GPL2 Uz - SHEAR NOKEY FR07`.
- *Save* writes `criteria_files/<name>.json`.
- *Final (use in step 3)* marks the file for step 3 (the calibration).
- *Save as default…* stores the current mechanism (sections, component, sense, thresholds) in a defaults file.
- *Load defaults file…* switches to another defaults file.

**Compare files (optional tab).** Plots the start drift and the displacement of every state vs axial load for one
or more criteria files. It is marked optional for now.
- Filled markers mean the state starts in the analysis.
- Hollow markers mean the threshold is never reached.
- *statistic* (none / min / max / mean) adds a statistic of all shown files (whatever the mechanism) per damage
  state and axial load, fades the files to 50 % and shows a table under the plot.
  - Only the files where the state starts count; hollow points (never reached) are left out.
  - *min* / *max*: the smallest / largest start drift (triangles down / up). The lower chart shows the displacement
    of that same start, a real point of one file. Table: n, min or max, and the file it comes from (`+1` = one more
    file has the same drift).
  - *mean*: the mean with bars of one sigma (thick) and two sigma (thin, with caps). σ is the sample standard
    deviation (n − 1), so the bars need two files or more; a single file gives the mean point only. Displacements
    of opposite sign (e.g. AX+ and AX-) get a mean each, never one mixed mean. The damage states are shifted a
    little sideways so their bars do not hide each other. Table: n, mean and σ.
  - The table has the shown damage states only. *Copy table* puts it on the clipboard tab-separated, with the
    decimal sign of Windows, so it pastes into Excel as numbers.
- *Export a statistic as a criteria file…* saves a statistic of the selected files as a normal criteria file in
  `criteria_files/`. It starts with the statistic shown (mean when none is shown).
  - Value: mean, mean − 1σ, mean + 1σ, mean − 2σ, mean + 2σ, min or max of the start drifts. All of DS1–DS5 are
    exported, whatever the Show boxes say.
  - *Definition from*: the new file takes the mechanism key, sections, database, load-case group and jobs of this
    selected file. Every drift becomes a typed drift; the displacement is that of these sections at that drift.
    The file therefore opens in tab 1 and is offered in step 3 like any other.
  - Name: *Auto name* gives e.g. `AX+ mean - 1 sigma of 4 files - SHEAR NOKEY FR07` and follows the choices until
    you type your own name. *final* marks it for step 3.
  - A state that no selected file reaches at an axial load is "not defined" there. One file only: σ is taken as 0.
    A value below 0 is set to 0. The note of each row gives n per state (e.g. `n 4/4/3/1/-`), and the
    `statistics` block of the file lists the files, n, mean, σ, min, max (with their files) and the exported value
    per axial load.
  - After the export a message lists any remarks: σ taken as 0, values set to 0, states out of order (see tab 1),
    drifts beyond the end of the curve, and axial loads without a job in the definition file.
- The legend can be dragged with the mouse.

## Step 2: ETABS pushover (`2_ETABS_pushover/Panel_pushover_GUI.py`, tab 1)

- The tool is unchanged, except that the results now go to `ETABS_results/<run>/`. Details are in
  `2_ETABS_pushover/README.txt`.
- Tab 2 of this tool is the older report with ND/LD/SD/NC criteria. Step 3 replaces it.

The ETABS results database:

```
2_ETABS_pushover/ETABS_results/<run name>/
    run.json                 settings, model (joints, links + properties), per axial load every attempt
    run_log.txt
    levels/<axial>kN.json    every result per step of the kept analysis (kN, m): joints, reactions, link
                             deformations and forces, base reactions; "valid_steps" = end of the valid data
    levels/attempts/...      every analysis, also the rejected ones
    ETABS_results_database/  optional export in the Panel_results layout (tab 2, "Export database")
```

Step 3 reads `run.json` and `levels/*.json`. The quantities come from
`2_ETABS_pushover/panel_pushover_settings.json`:
- drift = average U2 of nodes 1198 / 1196;
- H = K2 + K4 V3.

## Step 3: ETABS calibration (`3_ETABS_calibration/ETABS_calibration_GUI.py`)

**Inputs.**
- One ETABS run.
- **One** step-1 criteria file per mechanism (AX+, AX-, SH, REB). Final files are marked ★ and preselected.
- Optional: a **Custom** criteria file, any criteria file of step 1, e.g. a statistic of several mechanisms
  exported in the Compare tab (`AX- min of 10 files …`). It is Abaqus only (marker ★, dash-dot-dot line) and is
  compared with every ticked ETABS mechanism and the panel. Choosing it ticks *Custom* under Failure mechanisms.
- **ETABS link criteria, link by link** (double click to edit): link, mechanism, component, sense, DS1…DS5 [mm],
  provisional.
- **Signs** (note next to the table). ETABS gives compression as a negative deformation (U1 < 0). The sense turns
  the deformation into the value: `+` as is, `-` negated, `abs` |value|. A positive threshold is reached when the
  value rises above it, a negative one when it drops below it. So compression is sense `-` with **positive**
  thresholds (1 means U1 < −1 mm); sense `-` with negative thresholds tests tension. The column *Checks* gives
  what each row tests in ETABS signs, e.g. `U1 < -|DS| (compression)`; ⚠ (row in red) = the other side than its
  mechanism (AX+ tension, AX- compression, the `side` of the defaults file).

**Default rows.** One row per link of the run whose ETABS property fits a mechanism. The patterns are in the
defaults file:

| property | rows |
|---|---|
| `AX` | AX+ (U1, +) and AX- (U1, −) on K1, K3, K6, K8 |
| `Test_SHEAR_link` | SH (U3, abs) on K2, K4, K5, K7 |
| property containing `REB` / `ARM` | REB (U1 and U3, abs) |

- The current model has no rebar link yet, so REB gets placeholder rows `REB?`. Type the link name once the model
  has it, or press *New from run links* with a run that has it.
- The thresholds are the sizes of the step-1 defaults, with the sign that makes the row test its mechanism's side
  (AX- with sense `-`: positive). The step-1 sign belongs to the Abaqus value, not to the ETABS link.
- There is **one set of criteria for all axial loads**.

**Damage states.**
- A link takes the highest threshold its value exceeds; damage never decreases during the push.
- A mechanism takes its worst link, and the panel its worst mechanism.

**Views.** They only change what is shown; the criteria stay as typed.

| view | shows |
|---|---|
| *Pushover* | ETABS (solid, filled markers) and Abaqus (dashed, hollow markers); marker shape = mechanism (AX+ ▲, AX- ▼, SH ■, REB ◆), colour = damage state; curves coloured by the panel's damage state or by axial load |
| *Mechanism vs drift* | ETABS link values with their thresholds (filled = ETABS start) and hollow markers where Abaqus starts the state. The ETABS value at those points is a candidate threshold. Below them, the Abaqus section values with the step-1 starts. *Abaqus = the custom criteria* takes the hollow markers and the lower chart from the custom file instead. |
| *DS drift comparison* | start drift of every state vs axial load, ETABS (filled markers) vs Abaqus (hollow markers); here the line style marks the mechanism (AX+ solid, AX- dashed, SH dotted, REB dash-dot, Custom dash-dot-dot). The table gives the difference and the ETABS link value at the Abaqus start drift (a suggested threshold per axial load), plus the panel (all ticked mechanisms except Custom). Rows *Custom vs AX+* … *Custom vs panel* compare the custom starts with each ETABS mechanism, with that mechanism's link value at the custom start. |

**Filters.**
- Tick axial loads (E = ETABS, A = Abaqus), damage states DS0…DS5, mechanisms, and ETABS / Abaqus.
- Axis limits work as in step 1. The drift axis defaults to 1.15 × the Abaqus data, because the ETABS push runs on
  to 0.2–1 m.

**Output.**
- `etabs_criteria/<name>.json` (mm).
- *Export project format…* writes `acceptance_criteria_from_calibration.json`.
  - Format: version 2, units m.
  - It uses the first used row of each mechanism.
  - Mapping: AX+ → Axial_X/Y_dir U1 Max; AX- → U1 Min; SH → Shear_X/Y_dir U3 Min = Max; REB → Armatura_X/Y U1 and
    U3.
  - As the handover asks, open it in the project's `acceptance_criteria_editor.py` instead of copying it over
    `acceptance_criteria.json`.

## Notes

- **Data not changed.** The Abaqus databases and the ETABS results were only moved. File sizes and SHA-1 hashes
  were compared before and after the move: identical.
- **The two Abaqus databases.** `Panel_results more NOKEY cases` (66 jobs) contains every file of
  `Panel_results same cases as ETABS` (60 jobs) unchanged. It adds SHEAR_NOKEY jobs at 103, 138, 207, 242, 277 and
  311 kN.
- **The ETABS model** (`The ETABS pushover set of scripts/`) was open in ETABS, so it was not moved. The pushover
  tool finds the model through the running ETABS, not through the folder. Once ETABS is closed, the folder can be
  moved, for example to `0_INPUT/ETABS_model/`.
- **Drift.** ETABS and Abaqus drifts are compared in mm. ETABS uses the top nodes of the panel; Abaqus uses the
  loading point of the specimen (RU2, 2840 mm above the base).
- **Previous tools.** `Damage_states_GUI.py` (Excel output of DS start drifts) and `Panel_plotter_GUI.py` still
  work from `_previous_tools/`; choose the database with *Browse*.
