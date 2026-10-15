@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import tkinter,sqlite3,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到带 tkinter / sqlite3 的 Python 3
    pause
    exit /b 1
)

echo ============================================================
echo  启动前自检：① 端到端自检 ② 界面无头自检
echo  （两关都过再开界面，避免"打不开"却看不出原因）
echo ============================================================
echo.
"%PY%" "%H%自检.py"
set "RC1=%ERRORLEVEL%"
echo.
"%PY%" "%H%界面自检.py"
set "RC2=%ERRORLEVEL%"
echo.
echo ============================================================
if %RC1%==0 if %RC2%==0 (
    echo [全部通过] 可以启动「启动考场应用界面.bat」
) else (
    echo [未通过] 端到端自检 RC=%RC1%，界面自检 RC=%RC2%
)
echo ============================================================
pause
if %RC1%==0 if %RC2%==0 (exit /b 0) else (exit /b 1)
