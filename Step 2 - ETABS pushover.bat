@echo off
rem Step 2: ETABS pushover of the panel model (ETABS must be running with the model open)
cd /d "%~dp02_ETABS_pushover"
python Panel_pushover_GUI.py
if errorlevel 1 pause
