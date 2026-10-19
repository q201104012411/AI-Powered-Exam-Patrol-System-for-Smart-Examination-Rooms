@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  07 无线信号监测与屏蔽管控 —— 一键演示（命令行）
REM  默认写入真实 data 目录（演示数据均带"演示"标记）
REM  加 --sandbox 则用临时目录，不碰真实数据
REM ============================================================

set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python
    pause
    exit /b 1
)

"%PY%" 演示.py %*
echo.
pause
