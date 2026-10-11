@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM ============================================================
REM  人脸核验图形界面启动器
REM  解释器选择交给 pick_python.py（避免选到没装 insightface 的 Python）
REM ============================================================

set "HERE=%~dp0"
set "PICKER=%HERE%pick_python.py"
set "GUI_SCRIPT=%HERE%gui.py"
set "PYTHON_EXE="

if not exist "%GUI_SCRIPT%" (
    echo [错误] 找不到 gui.py
    pause
    exit /b 1
)

set "BOOT="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT (
    echo [错误] 未找到 Python，请先运行 install环境一键下载.bat
    pause
    exit /b 1
)

set "PICKFILE=%TEMP%\pypick_%RANDOM%.txt"
"%BOOT%" "%PICKER%" face --out "%PICKFILE%" >nul 2>nul
if exist "%PICKFILE%" for /f "usebackq delims=" %%p in ("%PICKFILE%") do set "PYTHON_EXE=%%p"
del "%PICKFILE%" >nul 2>nul
if not defined PYTHON_EXE set "PYTHON_EXE=%BOOT%"

"%PYTHON_EXE%" -c "import numpy, cv2, insightface, onnxruntime" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [警告] 当前解释器缺少人脸识别依赖：
    echo        %PYTHON_EXE%
    echo        建议先运行：install环境一键下载.bat
    echo.
    choice /c YN /m "仍然尝试启动 GUI 吗"
    if errorlevel 2 exit /b 1
)

echo [启动] 解释器：%PYTHON_EXE%
"%PYTHON_EXE%" "%GUI_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [提示] GUI 异常退出，退出码：%EXIT_CODE%
    echo        若提示缺少模块，请运行 install环境一键下载.bat
    pause
)
endlocal
exit /b %EXIT_CODE%
