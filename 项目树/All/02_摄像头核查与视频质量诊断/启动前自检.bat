@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,numpy,tkinter,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到同时具备 opencv / numpy / tkinter 的 Python
    pause
    exit /b 1
)

"%PY%" "%H%ui_selfcheck.py"
set "RC=%ERRORLEVEL%"
echo.
if %RC%==0 (
    echo [自检通过] 可以正常使用「启动核查界面.bat」
) else (
    echo [自检未通过] 请查看上方 [FAIL] 项，报告文件：data\logs\ui_selfcheck.txt
)
pause
exit /b %RC%
