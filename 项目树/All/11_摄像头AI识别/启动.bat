@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  摄像头 AI 识别 —— 启动（无登录，打开即用）
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

"%PY%" -c "import cv2, numpy, PIL" >nul 2>nul
if errorlevel 1 (
    echo ============================================
    echo  [错误] 缺少依赖，请先运行同目录的「安装环境.bat」
    echo         或手动执行：
    echo         "%PY%" -m pip install "opencv-python<5" numpy Pillow
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
