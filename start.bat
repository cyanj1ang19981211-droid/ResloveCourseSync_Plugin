@echo off
rem 注意：本窗口标题不要包含「课程强度同步」——launcher 靠窗口标题判断悬浮窗是否被关闭。
title 插件启动器
cd /d "%~dp0"

rem ===== 自动定位 Python 解释器（可移植：不再写死本机用户路径） =====
rem 达芬奇 fusionscript 仅兼容 Python 3.10 / 3.11，因此优先找 3.11，其次 3.10。
set "PY="
rem 1) 通过 py 启动器按版本精确查找（最可靠）
for /f "delims=" %%v in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not "%%v"=="" set "PY=%%v"
if "%PY%"=="" for /f "delims=" %%v in ('py -3.10 -c "import sys;print(sys.executable)" 2^>nul') do if not "%%v"=="" set "PY=%%v"
rem 2) 回退：系统 PATH 里的 python（若恰好是 3.10/3.11 也可用）
if "%PY%"=="" where python >nul 2>nul && set "PY=python"
rem 3) 最后回退：py 启动器默认版本
if "%PY%"=="" where py >nul 2>nul && set "PY=py -3"

if "%PY%"=="" (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或 3.11
    echo.
    pause
    exit /b 1
)

rem ===== 选择启动命令 =====
rem PY 是完整路径时，优先用同目录的 pythonw.exe —— 完全不会出现控制台窗口；
rem 否则（PY 形如 py -3）直接用 PY 跑，launcher 会立刻把自己的控制台隐藏。
rem 注意引号：完整路径可能带空格，必须加引号；"py -3" 这种带参数的命令则不能加。
set "LAUNCH=%PY%"
if exist "%PY%" for %%d in ("%PY%") do if exist "%%~dpdpythonw.exe" set "LAUNCH="%%~dpdpythonw.exe""

echo ============================================
echo   课程强度同步插件 - 一键启动
echo   使用 Python: %PY%
echo ============================================
echo.
echo 后端会在后台静默运行，稍等片刻会自动弹出悬浮窗。
echo 关掉悬浮窗，后端会自动退出（不会留下后台进程）。
echo.

rem ===== 启动 launcher：它负责「后台起后端 + 开悬浮窗 + 关窗自动收尾」 =====
start "" /min %LAUNCH% launcher.py

rem 本窗口使命完成，立刻关掉（launcher 是独立进程，不受影响）
exit /b 0
