@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ============================================================
REM  Excel 导入模板生成器
REM  生成 candidates_template.xlsx（含 consent_status 与填写说明页）
REM ============================================================

set "PYTHON_EXE="

set "BOOT="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT (
    echo [错误] 未找到 Python。
    pause
    exit /b 1
)
set "PYTHON_EXE=%BOOT%"

"%PYTHON_EXE%" -c "import openpyxl" >nul 2>nul
if errorlevel 1 (
    echo [提示] 正在安装 openpyxl ...
    "%PYTHON_EXE%" -m pip install openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [错误] openpyxl 安装失败。
        pause
        exit /b 1
    )
)

echo 使用解释器：%PYTHON_EXE%
"%PYTHON_EXE%" make_template.py
if errorlevel 1 (
    echo [错误] 模板生成失败。
    pause
    exit /b 1
)
echo.
echo 已生成：candidates_template.xlsx
pause
exit /b 0
