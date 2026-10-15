@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  09 无障碍与特殊考生保障 —— 一键演示
REM  1) 往**演示数据目录**（%TEMP%\acc09_demo）造演示数据
REM  2) 用该目录启动界面（不动真实 data\ 里的数据）
REM  所有演示记录 source=demo，界面与报告里都会写明"演示"
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

set "EXAM_ACCESS_HOME=%TEMP%\acc09_demo"
"%PY%" 工具_演示数据.py --yes
echo.
echo 演示数据目录：%EXAM_ACCESS_HOME%
echo 正在用演示数据启动界面...
"%PY%" app.py
if errorlevel 1 pause
