@echo off
chcp 65001 >nul
REM ============================================================
REM  设备端命令执行器（被智能终端 03 指挥）
REM  适用：本机是 01 人脸识别 或 02 摄像头核查
REM  用法：把下面的 MODULE 改成 01 或 02，再双击本文件
REM ============================================================
setlocal
set "MODULE=02"
set "INTERVAL=15"

REM ---- 智能终端地址与令牌（按现场改）----
if not defined EXAM_TERMINAL_URL set "EXAM_TERMINAL_URL=http://127.0.0.1:9113"
REM 令牌在 03 上执行  python lan_service.py --print-token  获取
if not defined EXAM_TERMINAL_TOKEN set "EXAM_TERMINAL_TOKEN="

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到 Python 解释器
    pause
    exit /b 1
)

echo ============================================
echo   设备端命令执行器（模块 %MODULE%）
echo   智能终端：%EXAM_TERMINAL_URL%
echo ============================================
if not defined EXAM_TERMINAL_TOKEN (
    echo [提示] 未设置 EXAM_TERMINAL_TOKEN，无法连接。
    echo        请到 03 上执行：python lan_service.py --print-token
    echo        然后执行：setx EXAM_TERMINAL_TOKEN 粘贴令牌
    pause
    exit /b 2
)

"%PY%" "%~dp0command_runner.py" --module %MODULE% --loop %INTERVAL%
pause
endlocal
