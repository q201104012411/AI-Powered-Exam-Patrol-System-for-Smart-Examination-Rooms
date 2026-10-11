@echo off
chcp 65001 >nul
REM 注意：必须先把 %~dp0 存进变量，不能直接写 %~dp001_...（cmd 会把 001 当成参数号）
set "H=%~dp0"
set "T=%H%01_人脸识别与身份核对"
set "SRV=%T%\网页操作面板\server"
if not exist "%SRV%\app.py" (
    echo [错误] 未找到 app.py：
    echo        %SRV%
    pause
    exit /b 1
)

REM ---- 1) 找引导 Python ----
set "PY="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python，请先运行 install环境一键下载.bat
    pause
    exit /b 1
)

REM ---- 2) 关键：挑出真正装了网页依赖的解释器（默认 py 可能是没依赖的版本）----
set "PICK=%TEMP%\pypick_web_%RANDOM%.txt"
"%PY%" "%T%\pick_python.py" web --out "%PICK%" >nul 2>nul
if exist "%PICK%" for /f "usebackq delims=" %%p in ("%PICK%") do set "PY=%%p"
del "%PICK%" >nul 2>nul

REM ---- 3) 依赖校验：缺 flask 就明确报错，不要静默失败 ----
"%PY%" -c "import flask, jwt, bcrypt, flask_cors, flask_limiter" >nul 2>nul
if errorlevel 1 (
    echo ============================================
    echo  [错误] 所选 Python 缺少网页端依赖：
    echo         %PY%
    echo  请先双击：install环境一键下载.bat
    echo ============================================
    pause
    exit /b 1
)

for %%d in ("%PY%") do set "PYDIR=%%~dpd"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"

echo ============================================
echo   网页操作面板  http://127.0.0.1:5000/
echo   账号：owner / EDY_POWERQ   或   teacher1 / teacher1
echo   解释器：%PY%
echo ============================================
start "" /min powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:5000/'"

cd /d "%SRV%"
if exist "%T%\data\hide_cmd.on" (
    echo 命令行窗口已隐藏，服务在后台运行。
    echo 停止服务：停止网页版.bat    查看日志：data\logs\web_server.log
    start "" "%PYW%" app.py
    exit /b 0
)

"%PY%" app.py
echo.
echo 后端已退出。
pause
