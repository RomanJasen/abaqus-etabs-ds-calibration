@echo off
rem Step 3: calibration of the ETABS link acceptance criteria against Abaqus
cd /d "%~dp03_ETABS_calibration"
python ETABS_calibration_GUI.py
if errorlevel 1 pause
