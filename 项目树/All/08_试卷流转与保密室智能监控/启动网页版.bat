@echo off
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
set "H=%~dp0"
cd /d "%H%"

set "PY="
call :probe_via_path "cv2,numpy"
call :probe_via_path "sys"
call :probe_via_dir "cv2,numpy"
call :probe_via_dir "sys"
if not defined PY (
    echo [错误] 找不到 Python 解释器
    echo 请先安装 Python 3.10 ~ 3.13，安装时勾选 "Add to PATH"
    pause
    exit /b 1
)
goto :probe_done

REM 在 PATH 里按候选名找（先试带 cv2 的，再退到只要能跑）
:probe_via_path
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import %~1,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
exit /b 0

REM 兜底：扫常见安装位置里的 Python3xx（绕开 py launcher 缺失 / PATH 未配）
:probe_via_dir
for /d %%V in ("%LocalAppData%\Programs\Python\Python3*") do call :take_dir "%%~V" %~1
for /d %%V in ("%SystemDrive%\Python3*") do call :take_dir "%%~V" %~1
for /d %%V in ("%ProgramFiles%\Python3*") do call :take_dir "%%~V" %~1
exit /b 0

REM 取某个目录下的 python.exe（先确认能 import 目标模块）
:take_dir
if defined PY exit /b 0
if not exist "%~1\python.exe" exit /b 0
"%~1\python.exe" -c "import %~2" >nul 2>nul || exit /b 0
if not defined PY set "PY=%~1\python.exe"
exit /b 0

:probe_done
REM ---- 隐藏命令行窗口：所有者勾选后在 data\hide_cmd.on 写标记，这里据此改用 pythonw.exe ----
set "FLAG="
if defined EXAM_SECURE_HOME if exist "%EXAM_SECURE_HOME%\hide_cmd.on" set "FLAG=%EXAM_SECURE_HOME%\hide_cmd.on"
if not defined FLAG if exist "%H%secure_home.txt" (
    set "SH="
    set /p SH=<"%H%secure_home.txt"
    if defined SH if exist "%SH%\hide_cmd.on" set "FLAG=%SH%\hide_cmd.on"
)
if not defined FLAG if exist "%H%data\hide_cmd.on" set "FLAG=%H%data\hide_cmd.on"

echo ============================================
echo   试卷流转与保密室智能监控 · 网页端
echo   地址：http://127.0.0.1:5020/
echo   账户：owner / EDY_POWERQ（所有者）  teacher1 / teacher1（管理员）
echo   解释器：%PY%
echo ============================================
start "" /min powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:5020/'"

if defined FLAG (
    set "PYW=%PY:python.exe=pythonw.exe%"
    if /i "%PYW%"=="%PY%" set "PYW=pythonw.exe"
    if not exist "%PYW%" set "PYW=pythonw.exe"
    start "" "%PYW%" "%H%网页监控面板\server\app.py"
    echo 已按「隐藏命令行窗口」设置用 pythonw 启动后端（无控制台）。
    echo 需要停服务：双击「停止网页版.bat」；想改回显示黑窗口：双击「切换命令行窗口.bat」。
    exit /b 0
)

"%PY%" "%H%网页监控面板\server\app.py"
echo.
echo 后端已退出。
pause
