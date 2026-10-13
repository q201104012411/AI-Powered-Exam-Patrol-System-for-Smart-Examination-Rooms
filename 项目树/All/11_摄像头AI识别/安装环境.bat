@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

echo ============================================
echo   摄像头 AI 识别 · 环境安装
echo   （只装 opencv-python 4.x + numpy + Pillow）
echo ============================================
echo.

set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 找不到 Python 解释器
    echo 请先安装 Python 3.10/3.11/3.12/3.13，安装时勾选 "Add to PATH"
    pause
    exit /b 1
)

echo 使用解释器：%PY%
echo.
"%PY%" "%~dp0安装环境.py"
echo.
if %ERRORLEVEL%==0 (
    echo [完成] 环境安装成功，双击「启动.bat」即可
) else (
    echo [完成] 安装未完全成功，请查看上方提示
)
pause
