@echo off
rem =====================================================================
rem  Shared Python finder.
rem  Called by start.bat / convert.bat / the environment-check .bat
rem
rem  On return the CALLER gets:
rem      PY      - python executable to use (full path, or a bare name)
rem      LAUNCH  - same, but prefers pythonw.exe so no console window
rem  If PY is empty, no usable Python was found on this machine.
rem
rem  -------------------------------------------------------------------
rem  *** THIS FILE MUST STAY 100%% ASCII - DO NOT PASTE CHINESE IN HERE ***
rem
rem  Why: cmd.exe decodes a .bat using the *console code page*. A .bat that
rem  contains GBK bytes breaks on any machine whose code page is not 936
rem  (e.g. "Beta: Use Unicode UTF-8" turned on -> 65001): the multi-byte
rem  sequences get mis-paired, `rem` comments leak out as commands and
rem  if/for blocks fall apart. ASCII-only is immune to all of that.
rem  Every Chinese message lives in Python (env_check.py / launcher.py).
rem  -------------------------------------------------------------------
rem =====================================================================

set "PY="
set "LAUNCH="

rem --- 1) Portable Python shipped inside the project -------------------
rem     Lets the plugin run on locked-down PCs where you cannot install
rem     anything: copy a Python folder to runtime\python\ and we use it.
rem     Always prefer python.exe here: PY must be able to print to a
rem     console (convert.bat shows its progress there). LAUNCH switches
rem     to pythonw.exe later for the silent background launcher.
if exist "%~dp0runtime\python\python.exe" set "PY=%~dp0runtime\python\python.exe"
if not defined PY if exist "%~dp0runtime\python\pythonw.exe" set "PY=%~dp0runtime\python\pythonw.exe"
if defined PY goto :configure_launch

rem --- 2) "py" launcher: exact versions first --------------------------
rem     DaVinci's fusionscript is built for 3.10 / 3.11 only, so those two
rem     are tried before falling back to whatever py -3 points at.
where py >nul 2>nul
if errorlevel 1 goto :try_paths
call :try_py -3.11
if not defined PY call :try_py -3.10
if not defined PY call :try_py -3
if defined PY goto :configure_launch

:try_paths
rem --- 3) Common install folders ---------------------------------------
rem     Covers Python installed WITHOUT "Add python.exe to PATH".
call :try_file "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try_file "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
call :try_file "C:\Python311\python.exe"
call :try_file "C:\Python310\python.exe"
call :try_file "%ProgramFiles%\Python311\python.exe"
call :try_file "%ProgramFiles%\Python310\python.exe"
if defined PY goto :configure_launch

rem --- 4) Last resort: whatever "python" is on PATH --------------------
rem     Any 3.x will do for convert.bat; env_check.py warns when the
rem     version is one DaVinci cannot talk to (3.12+).
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :configure_launch
where pythonw >nul 2>nul
if not errorlevel 1 set "PY=pythonw"
if defined PY goto :configure_launch
goto :eof

rem ---------------------------------------------------------------------
:configure_launch
rem Prefer the pythonw.exe sitting next to the interpreter: it never opens
rem a console window, which is what we want for the background launcher.
set "LAUNCH=%PY%"
if exist "%PY%" for %%d in ("%PY%") do if exist "%%~dpdpythonw.exe" set "LAUNCH="%%~dpdpythonw.exe""
if not defined LAUNCH set "LAUNCH=%PY%"
goto :eof

:try_py
rem %~1 is like "-3.11"; prints the real interpreter path if installed.
if defined PY goto :eof
for /f "delims=" %%v in ('%~1 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%v"
goto :eof

:try_file
if defined PY goto :eof
if "%~1"=="" goto :eof
if exist "%~1" set "PY=%~1"
goto :eof
