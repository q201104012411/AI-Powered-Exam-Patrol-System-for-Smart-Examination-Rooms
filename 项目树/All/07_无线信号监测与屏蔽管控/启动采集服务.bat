@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  07 无线信号监测与屏蔽管控 —— 频谱采集服务（真设备接入口）
REM  默认只监听 127.0.0.1:9127（回环不需要令牌）
REM  跨机： 启动采集服务.bat --host 0.0.0.0 --token 你的令牌
REM  可选： 再加 --tcp-port 9128 同时开 TCP 行协议
REM ============================================================

set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python
    pause
    exit /b 1
)

"%PY%" ingest_service.py %*
echo.
pause
