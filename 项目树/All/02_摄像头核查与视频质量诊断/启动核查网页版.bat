@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 挑一个真正装了网页依赖的解释器（默认 py 可能是没装 flask 的版本）----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import flask,cv2,numpy,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 flask / opencv / numpy 的 Python
    echo  请执行（示例）：
    echo      py -3.12 -m pip install flask flask-cors pyjwt opencv-python numpy
    echo ============================================
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

echo ============================================
echo   摄像头核查与视频质量诊断 · 网页端
echo   地址：http://127.0.0.1:5010/
echo   账户：owner / EDY_POWERQ（所有者）  teacher1 / teacher1（管理员）
echo   解释器：%PY%
echo ============================================
start "" /min powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:5010/'"

if defined FLAG (
    REM 零黑窗口：本窗口立刻退出，后端用 pythonw 起来（停止用「停止核查网页版.bat」）
    set "PYW=%PY:python.exe=pythonw.exe%"
    if /i "%PYW%"=="%PY%" set "PYW=pythonw.exe"
    if not exist "%PYW%" set "PYW=pythonw.exe"
    start "" "%PYW%" "%H%网页核查面板\server\app.py"
    echo 已按「隐藏命令行窗口」设置用 pythonw 启动后端（无控制台）。
    echo 需要停服务：双击「停止核查网页版.bat」；想改回显示黑窗口：双击「切换命令行窗口.bat」。
    exit /b 0
)

"%PY%" "%H%网页核查面板\server\app.py"
echo.
echo 后端已退出。
pause
