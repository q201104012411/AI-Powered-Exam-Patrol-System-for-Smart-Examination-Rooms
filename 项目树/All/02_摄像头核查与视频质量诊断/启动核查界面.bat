@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 挑一个真正装了依赖的解释器 ----
REM 注意：默认 py 可能是没装 opencv/numpy 的版本（本机踩过：py -> 3.15 无 numpy），
REM       必须逐个候选试导入，谁装齐了用谁。
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,numpy,tkinter,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 opencv / numpy / tkinter 的 Python
    echo  请执行（示例）：
    echo      py -3.12 -m pip install opencv-python numpy
    echo  若缺 tkinter，请用官方安装包重装 Python 并勾选 tcl/tk
    echo ============================================
    pause
    exit /b 1
)

REM ---- 启动前快速自检：关键源码编译检查，损坏则中止启动（完整自检用「启动前自检.bat」） ----
if not exist "%H%data\logs" mkdir "%H%data\logs" >nul 2>nul
"%PY%" -m py_compile gui.py person_detect.py linkage_bridge.py web_accounts.py storage.py data_crypto.py models.py patrol.py integration_api.py ui_selfcheck.py 2>"%H%data\logs\compile_check.log"
if errorlevel 1 (
    echo ============================================
    echo  [自检失败] 源代码编译检查未通过，已中止启动
    echo  详情见：data\logs\compile_check.log
    echo ============================================
    type "%H%data\logs\compile_check.log"
    pause
    exit /b 1
)

REM ---- 隐藏命令行窗口：所有者勾选后在 data\hide_cmd.on 写标记，这里据此改用 pythonw.exe ----
REM 数据目录可能被 EXAM_CAMERA_HOME 或 camera_home.txt 改到别处，三处都认。
set "FLAG="
if defined EXAM_CAMERA_HOME if exist "%EXAM_CAMERA_HOME%\hide_cmd.on" set "FLAG=%EXAM_CAMERA_HOME%\hide_cmd.on"
if not defined FLAG if exist "%H%camera_home.txt" (
    set "CH="
    set /p CH=<"%H%camera_home.txt"
    if defined CH if exist "%CH%\hide_cmd.on" set "FLAG=%CH%\hide_cmd.on"
)
if not defined FLAG if exist "%H%data\hide_cmd.on" set "FLAG=%H%data\hide_cmd.on"

if defined FLAG (
    REM 零黑窗口：本窗口立刻退出，界面用 pythonw/pyw 起来（要改回来双击「切换命令行窗口.bat」）
    set "PYW="
    REM 1) 尝试 pyw launcher（无窗口启动器），它通常与 python 一起安装
    for %%C in ("pyw -3.12" "pyw -3.11" "pyw -3.13" "pyw -3.10" "pyw -3") do (
        if not defined PYW for /f "delims=" %%w in ('%%~C -c "import sys;print(sys.executable.strip())" 2^>nul') do set "PYW=%%w"
    )
    REM 2) 由已选中的 python.exe 推导同目录 pythonw.exe
    if not defined PYW (
        for %%P in ("%PY%") do set "PYDIR=%%~dpP"
        set "PYW=!PYDIR!pythonw.exe"
    )
    REM 3) 兜底：用 where 找系统 PATH 里的 pythonw.exe
    if not exist "!PYW!" (
        for /f "delims=" %%w in ('where pythonw.exe 2^>nul') do set "PYW=%%w"
    )
    REM 去掉可能从 py/where 捕获到的尾部空白
    if defined PYW call :trim PYW

    if exist "!PYW!" (
        start "" "!PYW!" "%H%gui.py"
        exit /b 0
    )
    echo [警告] 找不到 pythonw.exe："!PYW!"
    echo          将改用 python.exe 显示命令行窗口启动，便于排查问题。
    pause
)

:main
setlocal DisableDelayedExpansion
echo ============================================
echo   摄像头核查与视频质量诊断
echo   解释器：%PY%
echo ============================================
"%PY%" "%H%gui.py"
set "RC=%ERRORLEVEL%"
if exist "%H%data\logs\gui_crash.log" (
    echo   ---- 最近崩溃日志 ----
    type "%H%data\logs\gui_crash.log"
)
echo 界面已退出，退出码：%RC%
pause
exit /b 0

:trim
REM 去掉变量尾部空格/制表符（参数：变量名）
call set "_t=%%%~1%%"
:trim_loop
if "%_t:~-1%"==" " set "_t=%_t:~0,-1%" & goto trim_loop
if "%_t:~-1%"=="	" set "_t=%_t:~0,-1%" & goto trim_loop
call set "%~1=%_t%"
set "_t="
goto :eof
