@echo off
rem ============================================================
rem  run_review.bat - one-command local review for Windows
rem  Uses only the Python 3.12 standard library.
rem  Generates + independently verifies the two witnesses and
rem  runs the small exhaustive and large differential checks.
rem ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto trypy
python handoff_review.py all
exit /b %errorlevel%

:trypy
echo [run_review] python not on PATH, trying the py -3.12 launcher ...
py -3.12 handoff_review.py all
exit /b %errorlevel%
