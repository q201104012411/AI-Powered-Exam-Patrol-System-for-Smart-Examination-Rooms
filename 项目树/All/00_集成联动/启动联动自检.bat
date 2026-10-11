@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,numpy,flask,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 opencv / numpy / flask 的 Python
    echo  请执行（示例）： py -3.12 -m pip install opencv-python numpy flask
    echo ============================================
    pause
    exit /b 1
)
echo ============================================
echo  00 集成联动 · 端到端自检
echo  01 人脸核验 + 02 画质核查 + 03 终端处置
echo  解释器：%PY%
echo ============================================
"%PY%" "%H%main.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo 联动自检未通过，请查看上方 FAIL 项。
pause
