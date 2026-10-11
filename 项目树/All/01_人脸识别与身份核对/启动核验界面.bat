@echo off
chcp 65001 >nul
REM 注意：必须先把 %~dp0 存进变量，不能直接写 %~dp001_...（cmd 会把 001 当成参数号）
set "H=%~dp0"
set "T=%H%01_人脸识别与身份核对"
if not exist "%T%\gui.py" (
    echo [错误] 未找到 gui.py：
    echo        %T%
    pause
    exit /b 1
)
cd /d "%T%"

REM ---- 定位 Python 解释器 ----
set "PY="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python，请先运行 install环境一键下载.bat
    pause
    exit /b 1
)
set "PICK=%TEMP%\pypick_%RANDOM%.txt"
"%PY%" "%T%\pick_python.py" face --out "%PICK%" >nul 2>nul
if exist "%PICK%" for /f "usebackq delims=" %%p in ("%PICK%") do set "PY=%%p"
del "%PICK%" >nul 2>nul

REM ---- 是否隐藏命令行窗口（所有者面板可切换：data\hide_cmd.on）----
for %%d in ("%PY%") do set "PYDIR=%%~dpd"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

if exist "%T%\data\hide_cmd.on" (
    start "" "%PYW%" "%T%\gui.py"
    exit /b 0
)

echo ============================================
echo   正在启动人脸核验界面（Python GUI）
echo   提示：可在「操作记录 - 所有者设置」里隐藏此窗口
echo ============================================
"%PY%" "%T%\gui.py"
set "RC=%ERRORLEVEL%"
echo.
echo ============================================
echo   GUI 已退出，退出码：%RC%
if exist "%T%\data\logs\gui_crash.log" (
    echo   ---- 最近崩溃日志 ----
    type "%T%\data\logs\gui_crash.log"
)
echo ============================================
pause
