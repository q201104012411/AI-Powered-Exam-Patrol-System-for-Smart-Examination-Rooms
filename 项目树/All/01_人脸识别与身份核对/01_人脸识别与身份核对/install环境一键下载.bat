@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================
REM  智能考场 · 依赖一键安装
REM  自动检测 Python → 补 pip → 装依赖
REM ============================================

set "PROJECT_DIR=%~dp0"
set "SERVER_DIR=%PROJECT_DIR%网页操作面板\server"
set "LOG_DIR=%PROJECT_DIR%logs"
set "LOG_FILE=%LOG_DIR%\install_deps.log"
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

call :log "=========================================="
call :log "[开始] 时间：%date% %time%"
call :log "[目录] %PROJECT_DIR%"

REM ============ 1. 找 Python ============
set "PYTHON_EXE="

REM 优先用 3.12
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :found_python
)

REM 其次 PATH 里的 python
where python >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%p in ('where python') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
    )
    goto :found_python
)

REM 再找 py 启动器
where py >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%v in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do (
        set "PYTHON_EXE=%%v"
    )
    if defined PYTHON_EXE goto :found_python
)

call :log "[错误] 未找到 Python，请先安装 Python 3.12"
echo.
echo [错误] 未找到 Python。请先安装 Python 3.12：https://www.python.org/downloads/release/python-3124/
pause
exit /b 1

:found_python
call :log "[Python] %PYTHON_EXE%"
for /f "delims=" %%v in ('"%PYTHON_EXE%" --version 2^>^&1') do (
    call :log "[版本] %%v"
)

REM ============ 2. 检测 pip ============
call :log "[检查] pip ..."
"%PYTHON_EXE%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[修复] pip 缺失，正在用 ensurepip 补装..."
    echo [修复] pip 缺失，正在补装...
    "%PYTHON_EXE%" -m ensurepip --upgrade
    if !errorlevel! neq 0 (
        call :log "[失败] ensurepip 失败，尝试 get-pip.py"
        call :install_pip_script
    )
)

"%PYTHON_EXE%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[错误] pip 安装失败，请手动处理"
    echo [错误] pip 安装失败，请检查网络或手动安装。
    pause
    exit /b 1
)

for /f "delims=" %%v in ('"%PYTHON_EXE%" -m pip --version 2^>^&1') do (
    call :log "[pip] %%v"
)

REM ============ 3. 升级 pip ============
call :log "[升级] pip ..."
"%PYTHON_EXE%" -m pip install --upgrade pip -i %PIP_MIRROR% >>"%LOG_FILE%" 2>&1

REM ============ 4. 安装依赖 ============
call :log "[安装] 人脸识别依赖 ..."
echo.
echo [安装] numpy opencv-python insightface onnxruntime
"%PYTHON_EXE%" -m pip install numpy opencv-python insightface onnxruntime -i %PIP_MIRROR%
if %errorlevel% neq 0 (
    call :log "[警告] 人脸识别依赖安装可能有问题"
)

REM ============ 5. 安装后端依赖 ============
if exist "%SERVER_DIR%\requirements.txt" (
    call :log "[安装] 后端依赖 ..."
    echo.
    echo [安装] 后端依赖（flask / pyjwt / bcrypt 等）
    "%PYTHON_EXE%" -m pip install -r "%SERVER_DIR%\requirements.txt" -i %PIP_MIRROR%
    if !errorlevel! neq 0 (
        call :log "[警告] 后端依赖安装可能有问题"
    )
) else (
    call :log "[跳过] 未找到 %SERVER_DIR%\requirements.txt"
    echo.
    echo [安装] 后端依赖（内联安装）
    "%PYTHON_EXE%" -m pip install flask flask-cors flask-limiter pyjwt bcrypt -i %PIP_MIRROR%
)

REM ============ 5b. Excel 模板工具 ============
call :log "[安装] openpyxl（Excel 导入模板工具）..."
"%PYTHON_EXE%" -m pip install openpyxl -i %PIP_MIRROR% >nul 2>&1

REM ============ 6. 验证 ============
call :log "[验证] 检查关键模块 ..."
echo.
echo [验证] 检查关键模块...
"%PYTHON_EXE%" -c "import numpy, cv2, insightface, onnxruntime; print('人脸识别依赖 OK')"
if %errorlevel% neq 0 (
    call :log "[失败] 人脸识别依赖验证失败"
    echo [失败] 人脸识别依赖验证失败
) else (
    call :log "[成功] 人脸识别依赖 OK"
)

"%PYTHON_EXE%" -c "import flask, jwt, bcrypt; print('后端依赖 OK')"
if %errorlevel% neq 0 (
    call :log "[失败] 后端依赖验证失败"
    echo [失败] 后端依赖验证失败
) else (
    call :log "[成功] 后端依赖 OK"
)

call :log "[完成] 时间：%date% %time%"
call :log "=========================================="

echo.
echo ============================================
echo  依赖安装完成
echo  日志：%LOG_FILE%
echo ============================================
echo.
echo 下一步：
echo   1. 初始化数据库：python 网页操作面板\server\db.py
echo   2. 启动后端：  网页操作面板\server\启动后端.bat
echo   3. 浏览器访问：http://127.0.0.1:5000/
echo   4. 启动界面：  start_gui(打开图形化界面).bat
echo.
pause
endlocal
exit /b 0


REM ============ 子过程：用 get-pip.py 装 pip ============
:install_pip_script
set "GETPIP=%TEMP%\get-pip.py"
call :log "[下载] get-pip.py ..."
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%GETPIP%'" >nul 2>nul
if exist "%GETPIP%" (
    "%PYTHON_EXE%" "%GETPIP%"
) else (
    call :log "[失败] get-pip.py 下载失败"
)
exit /b 0


REM ============ 日志函数 ============
:log
echo %~1
echo %~1>>"%LOG_FILE%"
exit /b 0