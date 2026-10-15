@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cryptography,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到具备 cryptography 的 Python（py -3.12 -m pip install cryptography）
    pause
    exit /b 1
)

echo ============================================
echo  03 智能终端 · 局域网接入服务
echo  1) 接收另一台设备（01 人脸识别 / 02 摄像头核查）送上来的
echo     操作记录、核验/核查结论、整改工单与业务告警
echo  2) 向 01/02 下发命令并接收回执（📡 命令台）
echo ============================================
echo.
echo  接入令牌（对端要填同一个）：
"%PY%" "%H%lan_service.py" --print-token
echo.
<nul set /p "=监听地址（回车=仅本机 127.0.0.1；输入 0.0.0.0 = 允许局域网设备访问）："
set "LANHOST="
set /p "LANHOST="
if not defined LANHOST set "LANHOST=127.0.0.1"
<nul set /p "=端口（回车=默认 9113）："
set "LANPORT="
set /p "LANPORT="
if not defined LANPORT set "LANPORT=9113"
echo.
echo  提示：跨设备时请在 Windows 防火墙放行该端口；
echo        本服务开放：记录接入 + 告警接入 + 只读查询 + 发布命令。
echo        命令通道只接受固定白名单（ping/status/logs_tail/selftest/rebuild_index），
echo        不是任意命令执行入口；跨设备一律校验 X-Exam-Token。
echo.
echo  对端（01/02）命令执行器：00_集成联动\启动命令执行器.bat
echo.
"%PY%" "%H%lan_service.py" --host %LANHOST% --port %LANPORT%
echo.
echo 服务已退出。
pause
