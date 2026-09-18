@echo off
rem =====================================================================
rem  Course Intensity Sync - course workbook converter  (xlsx -> JSON)
rem
rem  TURNING A COURSE WORKBOOK INTO OVERLAY DATA
rem     double-click            -> pick one or MORE .xlsx in the dialog
rem                                (Ctrl / Shift multi-select works)
rem     drag files onto this    -> converts exactly those files
rem
rem  Output goes to the data\ folder. The plugin re-scans it automatically,
rem  so there is no need to restart anything afterwards.
rem
rem  -------------------------------------------------------------------
rem  *** THIS FILE MUST STAY 100%% ASCII - DO NOT PASTE CHINESE IN HERE ***
rem
rem  cmd.exe decodes a .bat using the console code page. A .bat holding GBK
rem  bytes falls apart on any machine whose code page is not 936 (e.g.
rem  "Beta: Use Unicode UTF-8" enabled -> 65001): bytes get mis-paired,
rem  comments leak out as commands, if/for blocks break. ASCII is immune.
rem  All Chinese messages live in Python (convert_course.py / env_check.py),
rem  which writes to the console through the Win32 API and never garbles.
rem  -------------------------------------------------------------------
rem =====================================================================

setlocal
title Course Sync - Convert
cd /d "%~dp0"

call "%~dp0_find_python.bat"
if not defined PY goto :no_python

rem No arguments -> let convert_course.py open the file picker.
rem Note %PY% (not %LAUNCH%): the converter talks to the user, so it needs
rem a real console - we must not run it under pythonw.exe.
if not "%~1"=="" goto :with_args
%PY% "%~dp0convert_course.py"
goto :after

:with_args
%PY% "%~dp0convert_course.py" %*

:after
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [!] Conversion did not fully succeed - please read the messages above.
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
