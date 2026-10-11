@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ============================================================
REM  网页操作面板 · 启动后端
REM  首次运行会自动建库并生成随机初始口令
REM ============================================================

set "HERE=%~dp0"
set "PICKER=%HERE%..\..\pick_python.py"
set "PYTHON_EXE="

set "BOOT="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT (
    echo [错误] 未找到 Python，请先运行 ..\..\install环境一键下载.bat
    pause
    exit /b 1
)

set "PICKFILE=%TEMP%\pypick_%RANDOM%.txt"
"%BOOT%" "%PICKER%" web --out "%PICKFILE%" >nul 2>nul
if exist "%PICKFILE%" for /f "usebackq delims=" %%p in ("%PICKFILE%") do set "PYTHON_EXE=%%p"
del "%PICKFILE%" >nul 2>nul
if not defined PYTHON_EXE set "PYTHON_EXE=%BOOT%"

"%PYTHON_EXE%" -c "import flask, jwt, bcrypt, flask_cors, flask_limiter" >nul 2>nul
if errorlevel 1 (
    echo [提示] 后端依赖缺失，正在安装 ...
    "%PYTHON_EXE%" -m pip install -r "%HERE%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo ============================================================
echo  解释器：%PYTHON_EXE%
echo  服务地址：http://127.0.0.1:5000/
echo  初始口令：数据目录下的 init_admin_password.txt（改密后自动删除）
echo  关闭本窗口即停止服务。
echo ============================================================
cd /d "%HERE%"
"%PYTHON_EXE%" app.py
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [提示] 后端退出，退出码 %EXIT_CODE%
    pause
)
endlocal
exit /b %EXIT_CODE%
