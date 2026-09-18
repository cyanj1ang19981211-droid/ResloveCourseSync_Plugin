@echo off
rem =====================================================================
rem  Course Intensity Sync - one-click launcher
rem
rem  Double-click this file. It starts the background server and opens the
rem  always-on-top overlay window. Closing the overlay shuts everything
rem  down again - there is no stray process left behind.
rem
rem  -------------------------------------------------------------------
rem  *** THIS FILE MUST STAY 100%% ASCII - DO NOT PASTE CHINESE IN HERE ***
rem
rem  cmd.exe decodes a .bat using the console code page. A .bat holding GBK
rem  bytes falls apart on any machine whose code page is not 936 (e.g.
rem  "Beta: Use Unicode UTF-8" enabled -> 65001): bytes get mis-paired,
rem  comments leak out as commands, if/for blocks break. ASCII is immune.
rem  All Chinese messages live in Python (env_check.py / launcher.py),
rem  which writes to the console through the Win32 API and never garbles.
rem  -------------------------------------------------------------------
rem =====================================================================

setlocal

rem Console title intentionally in ASCII and intentionally NOT the overlay
rem caption (a Chinese string defined in overlay.py): launcher.py decides
rem "the user closed the overlay" by scanning for a window with that caption,
rem so this console must not carry it, otherwise the launcher would never
rem notice that the overlay was closed.
title Course Sync - Launcher

cd /d "%~dp0"

call "%~dp0_find_python.bat"
if not defined PY goto :no_python

rem Start the launcher minimised in the background. It hides its own console
rem right away and takes over from here (start server -> open overlay ->
rem watch the overlay -> clean up when it closes).
start "" /min %LAUNCH% "%~dp0launcher.py"

rem This window has done its job. The launcher is a separate process.
exit /b 0

rem ---------------------------------------------------------------------
:no_python
rem Chinese cannot be echoed from a .bat safely (see the note above), so
rem show a short ASCII hint and open the Chinese guide in Notepad instead.
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
