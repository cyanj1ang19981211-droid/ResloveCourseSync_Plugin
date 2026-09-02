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

echo ============================================
echo   课件数据转换 (xlsx -^> JSON)
echo   默认转换桌面上的「冠军课程课件.xlsx」
echo ============================================
echo.

if "%~1"=="" (
    echo 未指定文件，将转换桌面上的「冠军课程课件.xlsx」
    %PY% xlsx_to_json.py
) else (
    echo 转换文件: %~1
    %PY% xlsx_to_json.py "%~1"
)

echo.
echo 转换完成，生成的 JSON 已保存到 data 目录。
echo.
pause
