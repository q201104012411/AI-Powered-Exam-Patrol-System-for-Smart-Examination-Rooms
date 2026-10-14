@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  06 智能安检与手机作弊器材检测 —— 启动
REM  零第三方依赖（只用标准库 tkinter/sqlite3），无需 pip 安装
REM  data\hide_cmd.on 存在 -> 用 pythonw.exe 零黑窗口启动
REM ============================================================

set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.10~3.13 并勾选 Add to PATH
    pause
    exit /b 1
)

"%PY%" -c "import tkinter, sqlite3" >nul 2>nul
if errorlevel 1 (
    echo ============================================
    echo  [错误] 当前 Python 缺少 tkinter 或 sqlite3
    echo         本模块只用标准库，无需 pip 安装；
    echo         请改用官方 python.org 安装包（自带 tkinter）
    echo ============================================
    pause
    exit /b 1
)

for %%d in ("%PY%") do set "PYDIR=%%~dpd"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

if exist "%H%data\hide_cmd.on" (
    start "" "%PYW%" app.py
    exit /b 0
)
"%PY%" app.py
if errorlevel 1 pause
