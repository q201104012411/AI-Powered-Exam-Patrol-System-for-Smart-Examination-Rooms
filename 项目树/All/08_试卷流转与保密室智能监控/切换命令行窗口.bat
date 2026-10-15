@echo off
setlocal EnableDelayedExpansion
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
REM 当前状态
set "CUR=显示命令行窗口"
for %%P in ("%PY%") do set "PYW=%%~dpPpythonw.exe"
if exist "%H%data\hide_cmd.on" set "CUR=隐藏命令行窗口"
if defined EXAM_SECURE_HOME if exist "%EXAM_SECURE_HOME%\hide_cmd.on" set "CUR=隐藏命令行窗口"
if exist "%H%secure_home.txt" (
    set "SH="
    set /p SH=<"%H%secure_home.txt"
    if defined SH if exist "!SH!\hide_cmd.on" set "CUR=隐藏命令行窗口"
)

echo ============================================
echo   命令行窗口显示方式切换
echo   当前：!CUR!
echo ============================================
echo    [1] 隐藏命令行窗口（启动时用 pythonw，无黑窗口）
echo    [2] 显示命令行窗口（默认，便于排查问题）
echo    [0] 取消
echo.
set /p CH=请输入序号后回车：
if "!CH!"=="1" goto hide
if "!CH!"=="2" goto show
if "!CH!"=="0" exit /b 0
echo 输入无效。
pause
exit /b 0

:hide
del /q "%H%data\hide_cmd.on" >nul 2>nul
echo on>"%H%data\hide_cmd.on"
if exist "%H%secure_home.txt" (
    set "SH="
    set /p SH=<"%H%secure_home.txt"
    if defined SH (
        if not exist "!SH!" mkdir "!SH!" >nul 2>nul
        echo on>"!SH!\hide_cmd.on"
    )
)
echo 已切换为：隐藏命令行窗口。下次双击启动脚本即生效。
pause
exit /b 0

:show
del /q "%H%data\hide_cmd.on" >nul 2>nul
if exist "%H%secure_home.txt" (
    set "SH="
    set /p SH=<"%H%secure_home.txt"
    if defined SH del /q "!SH!\hide_cmd.on" >nul 2>nul
)
if defined EXAM_SECURE_HOME del /q "%EXAM_SECURE_HOME%\hide_cmd.on" >nul 2>nul
echo 已切换为：显示命令行窗口。
pause
exit /b 0
