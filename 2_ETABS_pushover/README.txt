SINGLE-PANEL PUSHOVER IN ETABS - calibration of acceptance criteria against Abaqus
==================================================================================

STEP 2 of the workflow (see ../README.md). Tab 1 runs the pushover and writes the ETABS results database
ETABS_results/<run name>/, which step 3 (../3_ETABS_calibration/ETABS_calibration_GUI.py) reads. Tab 2 is the
older report with ND / LD / SD / NC criteria; step 3 replaces it for the DS0-DS5 calibration.
The ETABS model AX_API_attempt.EDB is in "../The ETABS pushover set of scripts/" (found through the running ETABS).

Start:   python Panel_pushover_GUI.py          (ETABS running with AX_API_attempt.EDB open)

FILES
    Panel_pushover_GUI.py            the window (2 tabs)
    panel_core.py                    ETABS control, saved runs, quantities, damage states, Abaqus reading
    panel_report.py                  plots, Excel workbook, database for Panel_plotter_GUI.py
    panel_pushover_settings.json     everything set in the window (written when you run / close)
    acceptance_criteria_sets.json    the acceptance-criteria sets (edited in tab 2)
    abaqus_reference.json            Abaqus envelopes (ultimate + one per damage state), the AX curves of
                                     ALL AXIAL FINAL.xlsx, where the Abaqus database / damage-state workbook are,
                                     and which Abaqus quantity is compared with which ETABS quantity
    ETABS_results/<run name>/        the ETABS results database (see below)

TAB 1 - RUN ETABS PUSHOVER  (changes the open model; ETABS saves it before each analysis)
    For every axial force with Run ticked:
      1. pattern G: Fz = -N/2 at joints 1198 and 1196 (the case Stalno_NL applies G before the push)
      2. case PSH_test_DIN_sila: scale factor of HOR_sila (and the solution scheme, if set) - written through
         the table "Load Case Definitions - Time History - Nonlin Direct Integration" (the API has no Set
         function for this case type); the table is read back and checked after every edit
      3. only Stalno_NL -> PSH_test_DIN_sila are run (importing the case table switches every time-history
         case on, so the run flags are set before every analysis and restored at the end)
      4. every result per step is read (joints, reactions, link deformations and forces, base reactions)
      5. the horizontal displacement of joint 1198 must end between 0.2 and 1.0 m:
           too high -> the next scale factor applies at the end of the ramp the force at which the curve passed
                       the middle of the window (0.45 m), so the runaway starts later
           too low  -> scale factor x 1.4 (or the geometric mean with a known too-high factor)
      6. a STALLED analysis (the .LOG shows the analysis time advancing less than 0.1 s in 2 min - the solver
         crawls with substeps of ~1e-7 s) is cancelled with the Cancel button of ETABS. If it stalled BELOW the
         window, the same scale factor is tried again with the fallback scheme (Events Only, as you do by hand);
         the solver settings of each scheme are stored and written back (ETABS blanks the line search settings
         when the scheme changes), so your iterative settings survive.
      7. VALID DATA: the results end before the first spurious jump (a step moving 1198 by > 5 mm and > 10x every
         earlier step - the rule of Damage_states_GUI.py) or the first step where the vertical force differs
         more than 10 % from its start value. Events Only analyses of this model chatter (vertical force
         swinging by +-1000 kN) and are cut there.
    The scale factor that worked is written back into the table, so the next run starts from it.
    The model keeps the loads / scale factor of its last analysis.

TAB 2 - DAMAGE STATES & REPORT  (no ETABS needed)
    Choose a saved run and a criteria set, edit the checks (double click), Make report:
        ETABS_results/<run>/Report - <set>/Panel pushover report.xlsx      Summary, Criteria, Quantities, one sheet per axial
                                                                  force (every step, DS per check), Envelope, Plots
        ETABS_results/<run>/Report - <set>/plots/*.png
    Damage states, as in ALL AXIAL FINAL.xlsx: the limits of a check are the UPPER bounds of ND, LD, SD, NC
    (value < ND -> ND, < LD -> LD, < SD -> SD, else NC; the NC limit is the end of NC). "End of a state" =
    drift where the value reaches its limit - the same meaning as the drift columns of the Abaqus damage-state
    workbook, so ETABS and Abaqus are compared directly (plot 07 and the Summary sheet). The panel (combined)
    leaves a state as soon as any check marked DS leaves it. Damage never decreases.
    Sense: + positive values are damage (opening), - negative values (closing), abs both.
    Rel.: measured from step 0 (the state after the axial force).
    "Export database" writes ETABS_results/<run>/ETABS_results_database in the Panel_results layout (mm, N; job
    ETABS_PANEL_<axial force in N>) - add it next to Panel_results in Panel_plotter_GUI.py to plot ETABS and
    Abaqus together.

QUANTITIES (panel_pushover_settings.json -> "quantities"; {J1198:U2} joint, {K3:U1} link deformation,
{K2:V3} link force at end I, {R1199:F3} reaction, {BASE:FY} base reaction)
    drift          (U2 of 1198 + U2 of 1196) / 2              H   K2 V3 + K4 V3 (bottom shear links)
    V              K2 P + K4 P (compression < 0)             ETABS gives NO horizontal support reaction in this
                                                             model (fixed link DOFs act as constraints), so the
                                                             forces come from the links, as in the Excel
    ax_uplift      K3 U1 (bottom heel)                       ax_crush   K1 U1 (bottom toe)
    ax_top_K8      K8 U1 (top, mirrors K3)                   ax_top_K6  K6 U1 (top, mirrors K1)
    sh_slide       (K2 U3 + K4 U3) / 2                       sh_slide_top (K5 U3 + K7 U3) / 2
    node_uplift    Uz 1198 - Uz 1199                         node_crush Uz 1196 - Uz 1201
    (pushing in +Y: heel = minus side 1198 / K3 / K6, toe = plus side 1196 / K1 / K8)

PLOTS
    01  pushover curves ETABS and Abaqus (01b: the whole analysis, incl. the runaway)
    02  pushover coloured by damage state: combined and every check; ETABS and Abaqus state starts marked
    03  force envelope H-V: every ETABS step coloured by damage state, Abaqus envelopes, ETABS maximum H
    04  envelope for each damage state: force at the end of ND / LD / SD / NC, ETABS vs Abaqus
    05  drift vs every checked quantity (+ top links), with the limits, Abaqus curves and the Excel AX curves
    06  top vs bottom links and node uplift vs K3 + K6 (the "top mirrors bottom" check)
    07  drift range of each damage state per axial force, ETABS vs Abaqus
    08  drift, H and V over time
