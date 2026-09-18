@echo off
rem =====================================================================
rem  Course Intensity Sync - environment check
rem  (the file NAME is Chinese; the CONTENT stays plain ASCII)
rem
rem  Prints a full Chinese health report: is Python the right version, is
rem  DaVinci Resolve installed/scripting-enabled, is Edge there, are there
rem  any course files, is the port free. Ends with a verdict and what to do.
rem
rem  *** THIS FILE MUST STAY 100%% ASCII - DO NOT PASTE CHINESE IN HERE ***
rem  (see the long note in start.bat for why)
rem =====================================================================

setlocal
title Course Sync - Environment Check
cd /d "%~dp0"

call "%~dp0_find_python.bat"
if not defined PY goto :no_python

echo ==============================================================
echo   Course Intensity Sync - Environment Check
echo   (the report below is in Chinese)
echo ==============================================================
echo.
%PY% "%~dp0env_check.py"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" echo   [OK] No blocking problem found.
if not "%RC%"=="0" echo   [!!] Blocking problem(s) found - see [X] lines above.
echo.
echo   Press any key to close this window ...
pause >nul
exit /b %RC%

rem ---------------------------------------------------------------------
:no_python
echo.
echo   [X] Python was not found on this computer.
echo.
echo       This plugin needs Python 3.10 or 3.11.
echo       Two ways to fix it - both are written up in SETUP-GUIDE.txt:
echo         1) install Python 3.11 (tick "Add python.exe to PATH")
echo         2) or drop a portable Python into runtime\python\
echo.
if exist "%~dp0SETUP-GUIDE.txt" start "" notepad "%~dp0SETUP-GUIDE.txt"
if not exist "%~dp0SETUP-GUIDE.txt" echo       [!] SETUP-GUIDE.txt is missing - the folder was not copied completely.
echo.
echo   Press any key to close this window ...
pause >nul
exit /b 1
