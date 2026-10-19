@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  07 无线信号监测与屏蔽管控 —— 自检（375 项，不需要真设备/真频谱仪）
REM  默认使用临时数据目录；跨模块联动检查走 00 集成联动的沙箱
REM  运行完会打印临时目录，可直接删除，不影响真实数据
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

"%PY%" 自检.py %*
echo.
pause
