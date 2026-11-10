@echo off
rem Step 1: Abaqus acceptance criteria per mechanism and load-case group
cd /d "%~dp01_Abaqus_criteria"
python Abaqus_criteria_GUI.py
if errorlevel 1 pause
