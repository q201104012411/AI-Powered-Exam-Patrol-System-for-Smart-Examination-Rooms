@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import tkinter,sqlite3,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 未找到可用 Python（需要 tkinter 与 sqlite3，随官方 Python 自带）
    pause
    exit /b 1
)

REM ---- 默认隐藏命令行窗口；如需显示，由所有者在界面里取消勾选，或删除 data\hide_cmd.on ----
for %%d in ("%PY%") do set "PYDIR=%%~dpd"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

if exist "%H%data\hide_cmd.on" (
    REM pythonw 下没有控制台：把异常写进 data\logs\，界面本身也会弹错误框
    start "" "%PYW%" "%H%terminal_gui.py"
    exit /b 0
)

echo ============================================
echo   智能终端操作端
echo   账户：owner（所有者·最高权限） admin（管理员）
echo         patrol1（巡考员） inv01（监考员·仅A-01） check1（核验员）
echo   口令请向系统所有者索取
echo   提示：以 owner 登录后可在「运行状态与管控」里隐藏此窗口
echo ============================================
"%PY%" "%H%terminal_gui.py"
set "RC=%ERRORLEVEL%"
if exist "%H%data\logs\gui_crash.log" (
    echo   ---- 最近崩溃日志 ----
    type "%H%data\logs\gui_crash.log"
)
echo 界面已退出，退出码：%RC%
pause
