@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 挑一个真正装了 opencv / numpy 的 Python ----
REM 为什么用临时文件中转：cmd 的 for /f 是按控制台代码页解码子进程输出的，
REM       本机用户名和安装路径含中文时，Python 输出的 UTF-8 路径会被按 GBK 解成乱码，
REM       变量里就是一条不存在的路径，执行时报 The system cannot find the path specified.
REM       改成让 Python 把真实路径按 UTF-8 写进临时文件，再用 set /p 读回，中文路径不再有损。
set "PYTHONUTF8=1"
set "PYF=%TEMP%\examcam_demo12_path.txt"
del "%PYF%" >nul 2>nul
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not exist "%PYF%" %%~C -c "import cv2,numpy,sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
)
if exist "%PYF%" set /p PY=<"%PYF%"
del "%PYF%" >nul 2>nul
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 opencv / numpy 的 Python
    echo  请执行（示例）：
    echo      py -3.12 -m pip install opencv-python numpy
    echo ============================================
    pause
    exit /b 1
)

echo ============================================================
echo   12 告警上传与终端处置演示
echo   02 摄像头核查 --告警+画质可信度--^> 03 智能终端处置
echo   解释器：%PY%
echo   说明：默认在沙箱数据目录里跑，不动真实运行数据
echo ============================================================
echo.

if /i "%~1"=="selftest" (
    "%PY%" "%H%tests_selfcheck.py"
    echo.
    echo 自检退出码：%ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

"%PY%" "%H%demo_main.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo 演示退出码：%RC%  ^(0=全部检查点通过^)
pause
exit /b %RC%
