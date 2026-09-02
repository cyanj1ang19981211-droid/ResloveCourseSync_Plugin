@echo off
title 课程强度同步
cd /d "%~dp0"

rem ===== 杀干净占用 8765 端口的旧 server 进程（防止改完代码后仍连到旧进程） =====
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo [清理] 杀掉旧 server 进程 PID=%%a
    taskkill /F /PID %%a >nul 2>nul
)

rem ===== 自动定位 Python 解释器（绝对路径优先，避免 PATH 依赖） =====
set "PY="
if exist "C:\Users\M0769\.workbuddy\binaries\python\versions\3.11.9\python.exe" set "PY=C:\Users\M0769\.workbuddy\binaries\python\versions\3.11.9\python.exe"
if exist "C:\Users\M0769\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\M0769\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if "%PY%"=="" if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if "%PY%"=="" where python >nul 2>nul && set "PY=python"
if "%PY%"=="" where py >nul 2>nul && set "PY=py"

if "%PY%"=="" (
    echo [错误] 未找到 Python，请先安装 Python 3.x
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   课程强度同步插件 - 一键启动
echo   使用 Python: %PY%
echo ============================================
echo.

echo [1/2] 启动后端服务 (server.py)...
start "课程强度同步-后端" cmd /k %PY% server.py

echo [2/2] 启动悬浮窗 (overlay.py)...
timeout /t 2 /nobreak >nul
%PY% overlay.py

echo.
echo 全部启动完成。
echo - 请确认达芬奇已启动，并已开启"外部脚本"(External scripting = Local)。
echo - 悬浮窗会实时跟随达芬奇播放头显示当前强度。
echo.
pause
