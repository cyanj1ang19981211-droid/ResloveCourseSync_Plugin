@echo off
title 课件数据转换
cd /d "%~dp0"

rem ===== 自动定位 Python（可移植：不再写死本机用户路径） =====
set "PY="
for /f "delims=" %%v in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not "%%v"=="" set "PY=%%v"
if "%PY%"=="" for /f "delims=" %%v in ('py -3.10 -c "import sys;print(sys.executable)" 2^>nul') do if not "%%v"=="" set "PY=%%v"
if "%PY%"=="" where python >nul 2>nul && set "PY=python"
if "%PY%"=="" where py >nul 2>nul && set "PY=py -3"

if "%PY%"=="" (
    echo [错误] 未找到 Python，请先安装 Python 3.x
    echo.
    pause
    exit /b 1
)

rem 带参数时（把 xlsx 拖到本文件上，可以一次拖多个）→ 直接转换这些文件；
rem 不带参数时（双击）→ 弹出文件选择框，支持按住 Ctrl / Shift 多选课件。
if "%~1"=="" (
    %PY% convert_course.py
) else (
    %PY% convert_course.py %*
)

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo 转换未完全成功，请把上面的提示截图反馈。
echo 按任意键关闭本窗口...
pause >nul
exit /b %RC%
