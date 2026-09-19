@echo off
REM ===========================================================================
REM  run_gamer_friends_sync.bat
REM  Wrapper that Windows Task Scheduler calls. Runs the Python export from the
REM  folder this file lives in and appends all console output to output\run.log.
REM  Automation-safe: it never opens a browser window (no --open-report).
REM  Last Updated: 2026-09-18
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Locate a Python launcher: prefer the py launcher, fall back to python.exe.
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE (
    echo [%date% %time%] Python not found on PATH. Install Python 3 or edit this file with a full path.>&2
    exit /b 1
)

if not exist "%~dp0output" mkdir "%~dp0output"

%PYEXE% "%~dp0gamer_friends_sync.py" >> "%~dp0output\run.log" 2>&1
set "RC=%errorlevel%"
echo [%date% %time%] Exit code %RC% >> "%~dp0output\run.log"
endlocal & exit /b %RC%
