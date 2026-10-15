@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  AI 火场识别与自动报警 —— 启动
REM  为什么用临时文件中转：cmd 的 for /f 是按控制台代码页解码子进程输出的，
REM  本机用户名和安装路径含中文时，Python 输出的 UTF-8 路径会被按 GBK 解成乱码，
REM  变量里就是一条不存在的路径，执行时报 The system cannot find the path specified.
REM ============================================================
set "PYTHONUTF8=1"
set "PYF=%TEMP%\examfire_PY_path.txt"
del "%PYF%" >nul 2>nul
if not exist "%PYF%" py -3.12 -c "import cv2,numpy,PIL,sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if not exist "%PYF%" py -3 -c "import cv2,numpy,PIL,sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if not exist "%PYF%" python -c "import cv2,numpy,PIL,sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if exist "%PYF%" set /p PY=<"%PYF%"
del "%PYF%" >nul 2>nul

if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 opencv / numpy / Pillow 的 Python
    echo  请先双击同目录的「安装环境.bat」，或手动执行：
    echo      py -3.12 -m pip install "opencv-python<5" numpy Pillow
    echo ============================================
    pause
    exit /b 1
)

REM ---- 选启动模式 ----
REM  默认：后台运行（窗口隐藏到托盘，发现火情照样弹窗报警）
REM  要开机自启，把本文件的快捷方式放进 shell:startup 即可
set "MODE=--background"
if /i "%~1"=="gui"      set "MODE="
if /i "%~1"=="headless" set "MODE=--headless"
if /i "%~1"=="sim"      set "MODE=--simulate"
if /i "%~1"=="status"   set "MODE=--status"

REM data\hide_cmd.on 存在 -> 用 pythonw.exe 零黑窗口启动
for %%d in ("%PY%") do set "PYDIR=%%~dpd"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

echo ============================================================
echo   AI 火场识别与自动报警
echo   解释器：%PY%
echo   模式  ：%MODE%
echo   提示  ：托盘图标可召回窗口 / 开始停止监控 / 退出程序
echo ============================================================

if /i "%MODE%"=="--status" (
    "%PY%" app.py %MODE%
    pause
    exit /b 0
)

if exist "%H%data\hide_cmd.on" (
    start "" "%PYW%" app.py %MODE%
    exit /b 0
)
"%PY%" app.py %MODE%
if errorlevel 1 pause
exit /b 0
