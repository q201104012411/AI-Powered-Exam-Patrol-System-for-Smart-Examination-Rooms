@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,flask,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到装了 opencv + flask 的 Python：请先运行「install_pip环境.bat」
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo   USB 摄像头推流（自动挑"真的有画面"的那一路）
echo ============================================================
echo   1) 先扫描：哪个序号能用 / 哪个全黑
echo   2) 启动推流（端口 5000，替换旧的 camera_stream.py）
echo   3) 启动推流（端口 5001，与旧的 5000 并存）
echo   4) 用 1280x720@30 启动（USB 摄像头常用档位）
echo   5) 彩条测试画面（没有摄像头时验证链路）
echo   0) 退出
echo.
echo   注意：摄像头同一时刻只能被一个程序占用；端口 5000 被占说明旧的还在跑。
echo.
choice /c 123450 /n /m "请选择（按数字键）："
set "RC=%errorlevel%"
if "%RC%"=="1" "%PY%" camera_streamUSB.py --scan & pause & goto menu
if "%RC%"=="2" "%PY%" camera_streamUSB.py --device 1 --port 5000 & pause & goto menu
if "%RC%"=="3" "%PY%" camera_streamUSB.py --device 1 --port 5001 & pause & goto menu
if "%RC%"=="4" "%PY%" camera_streamUSB.py --device 1 --width 1280 --height 720 --fps 30 & pause & goto menu
if "%RC%"=="5" "%PY%" camera_streamUSB.py --test-pattern & pause & goto menu
if "%RC%"=="6" exit /b 0
exit /b 0
