@echo off
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
set "H=%~dp0"
cd /d "%H%"

echo ============================================
echo   试卷流转与保密室智能监控 · 环境安装
echo   （opencv-python 4.x + numpy + Pillow + flask）
echo   依赖与 02/03/11 完全一致，可共用同一解释器
echo   (auto-selects the Python that has tkinter = the one this project uses)
echo ============================================
echo.

REM prefer-interpreter-with-tkinter: that is the one this project really uses.
REM If a tkinter-less python (e.g. injected into PATH by another tool) is picked
REM first, deps get installed into the wrong place and the app still fails.
set "PY="
call :probe_via_path "tkinter,cv2,numpy"
call :probe_via_dir "tkinter,cv2,numpy"
call :probe_via_path "cv2,numpy"
call :probe_via_dir "cv2,numpy"
call :probe_via_path "sys"
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
echo 使用解释器：%PY%
echo.
"%PY%" "%~dp0安装环境.py"
set "RC=%ERRORLEVEL%"
echo.
if %RC%==0 (
    echo [完成] 环境安装成功，双击「运行自检与测试.bat」验证，或直接启动界面
) else (
    echo [完成] 安装未完全成功，请查看上方提示
)
pause
exit /b %RC%
