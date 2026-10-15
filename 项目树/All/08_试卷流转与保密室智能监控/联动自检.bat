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
echo ============================================
echo  08 试卷流转与保密室智能监控 · 联动自检
echo  01 人脸核验 + 02 画质核查 + 03 终端处置 + 08 保密室
echo  解释器：%PY%
echo ============================================
"%PY%" "%H%tests\linkage_selfcheck.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo 联动自检未通过，请查看上方 FAIL 项。
pause
