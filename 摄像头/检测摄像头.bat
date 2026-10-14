@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 找一个装了 opencv 的解释器（与项目其它 .bat 一致的做法）----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到装了 opencv 的 Python：请先运行「install_pip环境.bat」
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo   本机摄像头检测 / 切换   ^(两个摄像头时用它对号入座^)
echo ============================================================
echo   1) 检测全部序号（列设备 + 逐个试 + 存快照 + 给结论）
echo   2) 实时预览 0 号（按 0-9 切换序号，S 存图，Q 退出）
echo   3) 实时预览 1 号
echo   4) 只看 Windows 设备清单（不看序号）
echo   5) 打开快照目录
echo   0) 退出
echo.
echo   注意：摄像头同一时刻只能被一个程序占用 —— 检测前请先关掉巡考界面。
echo.
choice /c 123450 /n /m "请选择（按数字键）："
set "RC=%errorlevel%"
if "%RC%"=="1" "%PY%" 摄像头检测.py & pause & goto menu
if "%RC%"=="2" "%PY%" 摄像头检测.py --preview 0 & pause & goto menu
if "%RC%"=="3" "%PY%" 摄像头检测.py --preview 1 & pause & goto menu
if "%RC%"=="4" "%PY%" 摄像头检测.py --list & pause & goto menu
if "%RC%"=="5" if not exist "摄像头快照" mkdir "摄像头快照" & start "" "摄像头快照" & goto menu
if "%RC%"=="6" exit /b 0
exit /b 0
