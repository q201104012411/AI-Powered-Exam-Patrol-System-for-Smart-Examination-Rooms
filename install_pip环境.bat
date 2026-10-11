@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  智能考场 AI 巡考系统 · pip 环境一键安装
REM  功能：检测 Python → 补装/升级 pip → 安装全部依赖 → 验证
REM ============================================================

set "ROOT_DIR=%~dp0"
set "FACE_DIR=%ROOT_DIR%项目树\All\01_人脸识别与身份核对\01_人脸识别与身份核对"
set "SERVER_DIR=%FACE_DIR%\网页操作面板\server"
set "LOG_DIR=%ROOT_DIR%logs"
set "LOG_FILE=%LOG_DIR%\install_pip.log"
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

call :log "==================================================="
call :log " 智能考场 AI 巡考系统 · pip 环境一键安装"
call :log " 时间：%date% %time%"
call :log " 目录：%ROOT_DIR%"
call :log "==================================================="
echo.

REM ============ 0. 安装条件说明 ============
echo 【安装条件】
echo   1. Windows 10 / 11（64 位）
echo   2. 已安装 Python 3.11 或 3.12（推荐 3.12.4）
echo      未安装请运行：项目树\All\01_人脸识别与身份核对\01_人脸识别与身份核对\资源\python-3.12.4-amd64.exe
echo   3. 网络可访问 pip 镜像（默认清华源）
echo.
pause

REM ============ 1. 查找 Python ============
set "PYTHON_EXE="

REM 优先 Python 3.12 默认安装路径
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :found_python
)

REM 其次 PATH 中的 python
where python >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%p in ('where python') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
    )
    goto :found_python
)

REM 最后尝试 py 启动器
where py >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%v in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do (
        set "PYTHON_EXE=%%v"
    )
    if defined PYTHON_EXE goto :found_python
    for /f "delims=" %%v in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        set "PYTHON_EXE=%%v"
    )
    if defined PYTHON_EXE goto :found_python
)

call :log "[错误] 未找到 Python"
echo [错误] 未找到 Python 3.11/3.12，请先安装：
echo   离线安装包：%FACE_DIR%\资源\python-3.12.4-amd64.exe
echo   官网下载：  https://www.python.org/downloads/release/python-3124/
echo   注意：安装时勾选 "Add python.exe to PATH"
pause
exit /b 1

:found_python
call :log "[Python] %PYTHON_EXE%"
for /f "delims=" %%v in ('"%PYTHON_EXE%" --version 2^>^&1') do call :log "[版本] %%v"

REM ============ 2. 检测 / 补装 pip ============
call :log "[检查] pip ..."
"%PYTHON_EXE%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[修复] pip 缺失，使用 ensurepip 补装..."
    echo [修复] pip 缺失，正在补装...
    "%PYTHON_EXE%" -m ensurepip --upgrade
    if !errorlevel! neq 0 call :install_pip_script
)

"%PYTHON_EXE%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[错误] pip 安装失败"
    echo [错误] pip 安装失败，请检查网络后重试，或手动运行 get-pip.py
    pause
    exit /b 1
)
for /f "delims=" %%v in ('"%PYTHON_EXE%" -m pip --version 2^>^&1') do call :log "[pip] %%v"

REM ============ 3. 升级 pip ============
call :log "[升级] pip ..."
echo [升级] pip ...
"%PYTHON_EXE%" -m pip install --upgrade pip -i %PIP_MIRROR% >>"%LOG_FILE%" 2>&1

REM ============ 4. 安装人脸核验端依赖 ============
call :log "[安装] 人脸核验端依赖（requirements-face.txt）..."
echo.
echo [安装] numpy / opencv-python / insightface / onnxruntime / pillow
if exist "%FACE_DIR%\requirements-face.txt" (
    "%PYTHON_EXE%" -m pip install -r "%FACE_DIR%\requirements-face.txt" -i %PIP_MIRROR%
) else (
    "%PYTHON_EXE%" -m pip install numpy opencv-python insightface onnxruntime pillow -i %PIP_MIRROR%
)
if %errorlevel% neq 0 (
    call :log "[警告] 人脸核验端依赖安装可能有问题，详见日志"
    echo [警告] 人脸核验端依赖安装可能有问题
)

REM ============ 5. 安装网页操作面板后端依赖 ============
call :log "[安装] 网页操作面板后端依赖 ..."
echo.
echo [安装] flask / flask-cors / flask-limiter / pyjwt / bcrypt
if exist "%SERVER_DIR%\requirements.txt" (
    "%PYTHON_EXE%" -m pip install -r "%SERVER_DIR%\requirements.txt" -i %PIP_MIRROR%
) else (
    "%PYTHON_EXE%" -m pip install flask flask-cors flask-limiter pyjwt bcrypt -i %PIP_MIRROR%
)
if %errorlevel% neq 0 (
    call :log "[警告] 后端依赖安装可能有问题，详见日志"
    echo [警告] 后端依赖安装可能有问题
)

REM ============ 6. 安装工具类依赖 ============
call :log "[安装] 工具类依赖（openpyxl / python-docx）..."
echo.
echo [安装] openpyxl / python-docx
"%PYTHON_EXE%" -m pip install openpyxl python-docx -i %PIP_MIRROR% >>"%LOG_FILE%" 2>&1

REM ============ 7. 验证关键模块 ============
call :log "[验证] 关键模块 ..."
echo.
echo [验证] 检查关键模块...
"%PYTHON_EXE%" -c "import numpy, cv2, insightface, onnxruntime; print('[OK] 人脸核验端依赖')"
if %errorlevel% neq 0 (
    call :log "[失败] 人脸核验端依赖验证失败"
    echo [失败] 人脸核验端依赖验证失败
) else (
    call :log "[OK] 人脸核验端依赖验证通过"
)

"%PYTHON_EXE%" -c "import flask, jwt, bcrypt, flask_cors, flask_limiter; print('[OK] 后端依赖')"
if %errorlevel% neq 0 (
    call :log "[失败] 后端依赖验证失败"
    echo [失败] 后端依赖验证失败
) else (
    call :log "[OK] 后端依赖验证通过"
)

REM ============ 8. 完成 ============
call :log "==================================================="
call :log " 安装完成：%date% %time%"
call :log "==================================================="

echo.
echo ===================================================
echo  安装完成！日志：%LOG_FILE%
echo ===================================================
echo.
echo 人脸识别模型部署提醒：
echo   将 buffalo_l.zip 解压到 %USERPROFILE%\.insightface\models\buffalo_l
echo   （模型文件在：%FACE_DIR%\资源\buffalo_l.zip）
echo.
echo 下一步：
echo   1. 初始化数据库：python 网页操作面板\server\db.py
echo   2. 启动后端：  网页操作面板\打开网页.bat
echo   3. 浏览器访问：http://127.0.0.1:5000/
echo.
pause
endlocal
exit /b 0


REM ============ 子过程：get-pip.py 补装 pip ============
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
