@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sqlite3,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到可用的 Python 3
    pause
    exit /b 1
)

echo ============================================================
echo  10 普通考场+特殊考场应用 · 端到端自检
echo  全程临时数据目录；跨系统调用走沙箱，不污染 01/02/03/06
echo ============================================================
echo.
"%PY%" "%H%自检.py" %*
set "RC=%ERRORLEVEL%"
echo.
if %RC%==0 (
    echo [自检通过] 可以正常使用「启动考场应用界面.bat」
) else (
    echo [自检未通过] 请查看上方 FAIL 项
)
pause
exit /b %RC%
