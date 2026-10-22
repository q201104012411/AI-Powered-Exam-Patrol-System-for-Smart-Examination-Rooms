@echo off
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  考生数据保护 · 启动桌面管理界面（Python / tkinter）
REM    账户：owner / owner（所有者）
REM          dpo1/dpo1 · oper1/oper1 · auditor1/auditor1 · inv01/inv01
REM  停止：双击「停止服务.bat」
REM  提示：以 owner 登录后可在界面里切换「隐藏命令行窗口」。
REM ============================================================
REM ---- 挑一个「有 tkinter 且有加密/签名后端」的解释器 ----
REM 桌面界面用 tkinter（官方 Python 自带，无需 pip 安装）。
REM 加密优先带 gmssl（国密 SM4/SM3/SM2）；否则退而求其次要有 cryptography。
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3") do (
    if not defined PY call :pick "%%~C"
)
if defined PY goto :py_ready
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "C:\Python312\python.exe"
) do if not defined PY if exist %%P call :pick %%P
if defined PY goto :py_ready
REM 最后才试 PATH 上的 python
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :pick "%%p"
:py_ready
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 tkinter 与 加密库 的 Python
    echo  请先双击「安装依赖.bat」，或执行：
    echo      py -3.12 -m pip install gmssl cryptography
    echo ============================================
    pause
    exit /b 1
)
goto :found_py

:pick
REM %~1 = 候选解释器；必须 (tkinter + sqlite3) + (gmssl 或 cryptography)
"%~1" -c "import tkinter,sqlite3" >nul 2>&1 || goto :eof
"%~1" -c "import gmssl" >nul 2>&1 && set "PY=%~1"
if defined PY goto :eof
"%~1" -c "import cryptography" >nul 2>&1 && set "PY=%~1"
goto :eof

:found_py

echo 使用解释器：%PY%
echo ============================================
echo   考生数据保护 · 桌面管理界面
echo   账户：owner / owner（所有者）
echo         dpo1/dpo1  oper1/oper1  auditor1/auditor1  inv01/inv01
echo   加密：国密优先（SM4/SM3），无国密库时回退 AES-GCM
echo   完整性：SM3 + SM2 签名（无国密库时回退 SHA-256 + RSA）
echo   只读铁律：加密数据不可修改，只能删除或新增
echo ============================================

REM ---- 「隐藏命令行窗口」开关：写 data\hide_cmd.on ----
set "FLAG="
if defined EXAM_DPP_HOME if exist "%EXAM_DPP_HOME%\hide_cmd.on" set "FLAG=%EXAM_DPP_HOME%\hide_cmd.on"
if not defined FLAG if exist "%H%dpp_home.txt" (
    set "CH="
    set /p CH=<"%H%dpp_home.txt"
    if defined CH if exist "%CH%\hide_cmd.on" set "FLAG=%CH%\hide_cmd.on"
)
if not defined FLAG if exist "%H%data\hide_cmd.on" set "FLAG=%H%data\hide_cmd.on"

REM pythonw 路径必须在**块外**算好：写进下面 if (...) 块里的 %VAR% 会在整块解析时
REM 就展开成空值（cmd 经典坑），start 会拿到空程序名，结果"什么也没启动"。
for %%d in ("%PY%") do set "PYW=%%~dpdpythonw.exe"
if exist "%PYW%" goto :have_pyw
set "PYW=%PY%"
echo [提示] 未找到 pythonw.exe，「隐藏命令行窗口」将退化为普通启动（会显示控制台）。
:have_pyw

if defined FLAG (
    REM pythonw 下没有控制台：界面自身会弹错误框
    echo 已按「隐藏命令行窗口」设置启动（不显示控制台）：%PYW%
    echo 需要停界面：双击「停止服务.bat」。
    start "" "%PYW%" "%H%gui.py"
    exit /b 0
)

"%PY%" "%H%gui.py"
set "RC=%ERRORLEVEL%"
echo.
echo 界面已退出，退出码：%RC%
pause
