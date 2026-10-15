@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  09 无障碍与特殊考生保障 —— 启动（默认**不显示命令行黑窗口**）
REM
REM   双击本文件                     -> 用 pythonw.exe 无窗口启动界面
REM   命令行里：启动.bat console     -> 保留黑窗口（看报错/调试时才用）
REM   环境变量 EXAM_ACC09_CONSOLE=1  -> 同上（保留黑窗口）
REM
REM  零第三方依赖（只用标准库 tkinter/sqlite3），无需 pip 安装
REM  启动失败会自动写 data\logs\app_console.log 并弹窗提示，不会"静默打不开"
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

if /i "%~1"=="console" goto console
if /i "%~1"=="--console" goto console
if /i "%~1"=="-c" goto console
if "%EXAM_ACC09_CONSOLE%"=="1" goto console

REM ---- 默认：无窗口启动（pythonw 起完立即退出，本命令行窗口不驻留）----
if /i "%PYW%"=="%PY%" (
    echo [提示] 未找到 pythonw.exe，将以带窗口方式启动（换用官方 Python 安装包可解决）
    "%PY%" app.py
    if errorlevel 1 pause
    exit /b 0
)
start "" "%PYW%" "%H%app.py"
exit /b 0

:console
echo [调试模式] 保留命令行窗口；关闭界面后本窗口才会结束
"%PY%" app.py
if errorlevel 1 pause
exit /b 0
