@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

echo ============================================
echo   智能考场 AI 巡考系统 · 环境安装
echo ============================================
echo.

REM ---- 找一个有 pip 的 Python ----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到 Python 解释器
    echo 请先安装 Python 3.10/3.11/3.12/3.13，安装时勾选 "Add to PATH"
    pause
    exit /b 1
)

echo 使用解释器：%PY%
echo.

"%PY%" "%~dp0install_env.py"
set "RC=%ERRORLEVEL%"
echo.
if %RC%==0 (
    echo [完成] 环境安装成功
) else (
    echo [完成] 环境安装未完全成功，请查看上方提示
)
pause
exit /b %RC%
