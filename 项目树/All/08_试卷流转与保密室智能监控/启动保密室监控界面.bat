@echo off
setlocal EnableDelayedExpansion
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
set "H=%~dp0"
cd /d "%H%"

set "PY="
REM 桌面端必须有 tkinter（部分环境把 tk 拆掉了，如精简版 Python）
call :probe_via_path "tkinter,cv2,numpy"
call :probe_via_dir "tkinter,cv2,numpy"
call :probe_via_path "tkinter"
call :probe_via_dir "tkinter"
call :probe_via_path "cv2,numpy"
call :probe_via_path "sys"
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
REM ---- 启动前快速自检：关键源码编译检查，损坏则中止启动 ----
if not exist "%H%data\logs" mkdir "%H%data\logs" >nul 2>nul
"%PY%" -m py_compile config.py models.py storage.py web_accounts.py alarm_hub.py camera_stream.py vault.py flow.py report.py gui.py main.py 2>"%H%data\logs\compile_check.log"
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
REM 数据目录可能被 EXAM_SECURE_HOME 或 secure_home.txt 改到别处，三处都认。
set "FLAG="
if defined EXAM_SECURE_HOME if exist "%EXAM_SECURE_HOME%\hide_cmd.on" set "FLAG=%EXAM_SECURE_HOME%\hide_cmd.on"
if not defined FLAG if exist "%H%secure_home.txt" (
    set "SH="
    set /p SH=<"%H%secure_home.txt"
    if defined SH if exist "%SH%\hide_cmd.on" set "FLAG=%SH%\hide_cmd.on"
)
if not defined FLAG if exist "%H%data\hide_cmd.on" set "FLAG=%H%data\hide_cmd.on"

if defined FLAG (
    set "PYW="
    for %%C in ("pyw -3.12" "pyw -3.11" "pyw -3.13" "pyw -3.10" "pyw -3") do (
        if not defined PYW for /f "delims=" %%w in ('%%~C -c "import sys;print(sys.executable.strip())" 2^>nul') do set "PYW=%%w"
    )
    if not defined PYW (
        for %%P in ("%PY%") do set "PYDIR=%%~dpP"
        set "PYW=!PYDIR!pythonw.exe"
    )
    if not exist "!PYW!" (
        for /f "delims=" %%w in ('where pythonw.exe 2^>nul') do set "PYW=%%w"
    )
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
echo   试卷流转与保密室智能监控 · 桌面界面
echo   解释器：%PY%
echo   账户：owner / owner（所有者）  teacher1 / teacher1（管理员）
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
call set "_t=%%%~1%%"
:trim_loop
if "%_t:~-1%"==" " set "_t=%_t:~0,-1%" & goto trim_loop
if "%_t:~-1%"=="	" set "_t=%_t:~0,-1%" & goto trim_loop
call set "%~1=%_t%"
set "_t="
goto :eof
