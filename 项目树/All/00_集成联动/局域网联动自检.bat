@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cryptography,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo ============================================
    echo  [错误] 找不到具备 cryptography 的 Python
    echo  请执行（示例）： py -3.12 -m pip install cryptography
    echo ============================================
    pause
    exit /b 1
)

echo ============================================
echo  00 集成联动 · 局域网联动端到端自检
echo  真起 03 的接入服务，真跑 01/02 的上报客户端
echo  （临时数据目录，不碰真实运行数据）
echo  解释器：%PY%
echo ============================================
"%PY%" "%H%lan_selftest.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo 自检未通过，请查看上方 FAIL 项。
pause
