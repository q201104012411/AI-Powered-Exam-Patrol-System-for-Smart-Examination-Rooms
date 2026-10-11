@echo off
chcp 65001 >nul
REM 快捷启动：自动核验程序（无人值守、无需点击，仅所有者可开启）
cd /d "%~dp001_人脸识别与身份核对"
echo ============================================
echo   自动核验程序（仅所有者可开启）
echo   默认：1:N 连续核验，每 2 秒一次
echo   首次需输入 owner 口令；--remember 已启用，8 小时内免再输
echo   停止：Ctrl+C
echo ============================================
set "PY="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python，请先运行 install环境一键下载.bat
    pause
    exit /b 1
)
"%PY%" auto_verify.py --remember %*
echo.
echo 自动核验程序已退出。
pause
