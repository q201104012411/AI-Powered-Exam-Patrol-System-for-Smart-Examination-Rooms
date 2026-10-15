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
echo  02 摄像头核查与视频质量诊断 · 上报到 03 智能终端
echo  上传：审计留痕 + 机位核查结论 + 整改工单
echo ============================================
echo.

set "CFG=%H%终端上报配置.bat"
if exist "%CFG%" call "%CFG%"
if not defined EXAM_TERMINAL_URL (
    <nul set /p "=03 智能终端接入地址（如 http://192.168.1.20:9113）："
    set /p "EXAM_TERMINAL_URL="
)
if not defined EXAM_TERMINAL_TOKEN (
    <nul set /p "=接入令牌（在终端的「启动局域网接入服务.bat」里显示）："
    set /p "EXAM_TERMINAL_TOKEN="
)
if defined EXAM_TERMINAL_URL (
    > "%CFG%" echo @echo off
    >> "%CFG%" echo set "EXAM_TERMINAL_URL=%EXAM_TERMINAL_URL%"
    >> "%CFG%" echo set "EXAM_TERMINAL_TOKEN=%EXAM_TERMINAL_TOKEN%"
    echo 配置已保存到「终端上报配置.bat」（下次不用再填）
)
echo.

echo ---- 当前状态 ----
"%PY%" "%H%push_oplog.py" --status
echo.

<nul set /p "=是否常驻上报？(y=每 120 秒一次 / 回车=只跑一次)："
set "LOOPMODE="
set /p "LOOPMODE="
echo.
if /i "%LOOPMODE%"=="y" (
    "%PY%" "%H%push_oplog.py" --loop 120
) else (
    "%PY%" "%H%push_oplog.py"
)
echo.
echo 断网时记录会留在本地队列，网络恢复后下次运行自动补传（不丢数据）。
pause
