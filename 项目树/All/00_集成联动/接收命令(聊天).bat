@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到 Python。
    pause
    exit /b 1
)

echo ============================================
echo  01 / 02 设备端 · 接收命令（聊天窗口）
echo  智能终端（03）下发的命令会出现在这个窗口里，
echo  它只在白名单内执行并自动回执：
echo    ping / status / logs_tail / selftest / rebuild_index
echo ============================================
echo.
<nul set /p "=本机是哪个模块（01 或 02，回车=02）："
set "MOD="
set /p "MOD="
if not defined MOD set "MOD=02"
if not defined EXAM_TERMINAL_URL (
    <nul set /p "=03 智能终端地址（如 http://192.168.1.20:9113）："
    set /p "EXAM_TERMINAL_URL="
)
if not defined EXAM_TERMINAL_TOKEN (
    <nul set /p "=接入令牌（在 03 的「启动局域网接入服务.bat」里显示）："
    set /p "EXAM_TERMINAL_TOKEN="
)
echo.
"%PY%" "%H%cmd_chat.py" --module %MOD%
echo.
echo 窗口已关闭。
pause
