@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

REM ============================================================
REM  智能考场 AI 巡考系统 · pip 环境一键安装（全覆盖版）
REM
REM  覆盖范围：
REM    01 人脸识别与身份核对（桌面端 + 网页操作面板后端）
REM    02 摄像头核查与视频质量诊断（含 AI 框选识别必要条件 HOG）
REM    03 智能终端操作
REM    00 集成联动（纯标准库，无第三方依赖）
REM
REM  特性：
REM    · 环境变量集中在本文件顶部，改一处全生效
REM    · 自动挑选 Python 3.12 > 3.11 > 3.10 > 3.13
REM    · opencv-python 强制覆盖为 4.x（5.x 已移除 HOGDescriptor）
REM    · 自动卸载 opencv-python-headless（缺 HOG 的常见原因）
REM    · 安装完成自动做依赖验证（含 HOGDescriptor / tkinter）
REM ============================================================


REM ============ 【环境变量区】全部可改 ============

REM ---- 根目录与子系统目录 ----
set "ROOT_DIR=%~dp0"
set "ALL_DIR=%ROOT_DIR%项目树\All"
set "FACE_DIR=%ALL_DIR%\01_人脸识别与身份核对\01_人脸识别与身份核对"
set "CAMERA_DIR=%ALL_DIR%\02_摄像头核查与视频质量诊断"
set "TERMINAL_DIR=%ALL_DIR%\03_智能终端操作"
set "LINKAGE_DIR=%ALL_DIR%\00_集成联动"
set "SERVER_DIR=%FACE_DIR%\网页操作面板\server"
set "RES_DIR=%FACE_DIR%\资源"

REM ---- 三套系统数据目录环境变量（集成联动按此寻址，可被外部覆盖） ----
if not defined EXAM_HOME          set "EXAM_HOME=%FACE_DIR%\data"
if not defined EXAM_CAMERA_HOME   set "EXAM_CAMERA_HOME=%CAMERA_DIR%\data"
if not defined EXAM_TERMINAL_HOME set "EXAM_TERMINAL_HOME=%TERMINAL_DIR%\data"

REM ---- 日志 ----
set "LOG_DIR=%ROOT_DIR%logs"
set "LOG_FILE=%LOG_DIR%\install_pip.log"

REM ---- pip 镜像（清华源；要换官方源改成 https://pypi.org/simple） ----
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

REM ---- Python 版本优先级（从左到右探测） ----
set "PY_CANDIDATES="py -3.12" "py -3.11" "py -3.10" "py -3.13" "py -3" "python""

REM ---- 包清单 ----
REM   通用（02/03 桌面端）：opencv 必须 4.x，5.x 已移除 HOGDescriptor
set "PKG_COMMON=opencv-python<5 numpy Pillow"
REM   01 人脸核验端：优先读 requirements-face.txt，缺失时用此兜底
set "PKG_FACE_FALLBACK=numpy opencv-python<5 insightface onnxruntime pillow"
REM   01 网页操作面板后端：优先读 requirements.txt，缺失时用此兜底
set "PKG_SERVER_FALLBACK=flask flask-cors flask-limiter pyjwt bcrypt"
REM   工具类（报表导出）
set "PKG_TOOLS=openpyxl python-docx"

REM ---- 离线安装包（Python 本体 / 人脸模型） ----
set "PY_INSTALLER=%RES_DIR%\python-3.12.4-amd64.exe"
set "FACE_MODEL_ZIP=%RES_DIR%\buffalo_l.zip"

REM ============ 【环境变量区结束】 ============


if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

call :log "==================================================="
call :log " 智能考场 AI 巡考系统 · pip 环境一键安装（全覆盖版）"
call :log " 时间：%date% %time%"
call :log " 目录：%ROOT_DIR%"
call :log " 镜像：%PIP_MIRROR%"
call :log " EXAM_HOME          = %EXAM_HOME%"
call :log " EXAM_CAMERA_HOME   = %EXAM_CAMERA_HOME%"
call :log " EXAM_TERMINAL_HOME = %EXAM_TERMINAL_HOME%"
call :log "==================================================="
echo.

echo 【安装条件】
echo   1. Windows 10 / 11（64 位）
echo   2. 已安装 Python 3.10 - 3.12（推荐 3.12.4）
echo      未安装请运行：%PY_INSTALLER%
echo   3. 网络可访问 pip 镜像（默认清华源，可在本文件顶部更换）
echo.
pause

REM ============ 1. 查找 Python ============
set "PYTHON_EXE="
for %%C in (%PY_CANDIDATES%) do (
    if not defined PYTHON_EXE (
        for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable.strip())" 2^>nul') do (
            set "PYTHON_EXE=%%p"
        )
    )
)
if not defined PYTHON_EXE (
    call :log "[错误] 未找到 Python"
    echo [错误] 未找到 Python 3.10/3.11/3.12，请先安装：
    echo   离线安装包：%PY_INSTALLER%
    echo   官网下载：  https://www.python.org/downloads/release/python-3124/
    echo   注意：安装时勾选 "Add python.exe to PATH" 和 "tcl/tk"
    pause
    exit /b 1
)
call :log "[Python] %PYTHON_EXE%"
for /f "delims=" %%v in ('"%PYTHON_EXE%" --version 2^>^&1') do call :log "[版本] %%v"

REM tkinter 是 GUI 必备（官方安装包勾选 tcl/tk 才带），先验证
"%PYTHON_EXE%" -c "import tkinter" >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[错误] 该 Python 缺少 tkinter"
    echo [错误] 该 Python 缺少 tkinter，请用官方安装包重装并勾选 tcl/tk
    pause
    exit /b 1
)

REM ============ 2. 检测 / 补装 / 升级 pip ============
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
    echo [错误] pip 安装失败，请检查网络后重试
    pause
    exit /b 1
)
for /f "delims=" %%v in ('"%PYTHON_EXE%" -m pip --version 2^>^&1') do call :log "[pip] %%v"
call :log "[升级] pip ..."
"%PYTHON_EXE%" -m pip install --upgrade pip -i %PIP_MIRROR% >>"%LOG_FILE%" 2>&1

REM ============ 3. 清理 OpenCV 冲突（HOG 必要条件） ============
call :log "[清理] 卸载 opencv headless / 5.x（缺 HOGDescriptor）..."
echo.
echo [清理] 卸载 opencv-python-headless 与旧 opencv-python ...
"%PYTHON_EXE%" -m pip uninstall -y opencv-python-headless opencv-python >>"%LOG_FILE%" 2>&1

REM ============ 4. 安装通用依赖（02/03 桌面端） ============
call :log "[安装] 通用依赖：%PKG_COMMON% ..."
echo.
echo [安装] 通用依赖：%PKG_COMMON%
"%PYTHON_EXE%" -m pip install --upgrade %PKG_COMMON% -i %PIP_MIRROR%
if %errorlevel% neq 0 call :log "[警告] 通用依赖安装可能有问题，详见日志"

REM ============ 5. 安装 01 人脸核验端依赖 ============
call :log "[安装] 人脸核验端依赖 ..."
echo.
if exist "%FACE_DIR%\requirements-face.txt" (
    echo [安装] 人脸核验端依赖（requirements-face.txt）
    "%PYTHON_EXE%" -m pip install -r "%FACE_DIR%\requirements-face.txt" -i %PIP_MIRROR%
) else (
    echo [安装] 人脸核验端依赖（兜底清单）：%PKG_FACE_FALLBACK%
    "%PYTHON_EXE%" -m pip install %PKG_FACE_FALLBACK% -i %PIP_MIRROR%
)
if %errorlevel% neq 0 call :log "[警告] 人脸核验端依赖安装可能有问题，详见日志"

REM ============ 6. 安装 01 网页操作面板后端依赖 ============
call :log "[安装] 网页操作面板后端依赖 ..."
echo.
if exist "%SERVER_DIR%\requirements.txt" (
    echo [安装] 后端依赖（requirements.txt）
    "%PYTHON_EXE%" -m pip install -r "%SERVER_DIR%\requirements.txt" -i %PIP_MIRROR%
) else (
    echo [安装] 后端依赖（兜底清单）：%PKG_SERVER_FALLBACK%
    "%PYTHON_EXE%" -m pip install %PKG_SERVER_FALLBACK% -i %PIP_MIRROR%
)
if %errorlevel% neq 0 call :log "[警告] 后端依赖安装可能有问题，详见日志"

REM ============ 7. 安装工具类依赖 ============
call :log "[安装] 工具类依赖：%PKG_TOOLS% ..."
"%PYTHON_EXE%" -m pip install %PKG_TOOLS% -i %PIP_MIRROR% >>"%LOG_FILE%" 2>&1

REM ============ 8. OpenCV 强制覆盖为 4.x（兜底再装一次） ============
"%PYTHON_EXE%" -c "import cv2,sys;sys.exit(0 if hasattr(cv2,'HOGDescriptor') else 1)" >nul 2>nul
if %errorlevel% neq 0 (
    call :log "[覆盖] 强制重装 opencv-python 4.x ..."
    echo.
    echo [覆盖] 强制重装 opencv-python 4.x（恢复 HOGDescriptor）...
    "%PYTHON_EXE%" -m pip install --no-cache-dir --force-reinstall "opencv-python<5" -i %PIP_MIRROR%
)

REM ============ 9. 依赖验证 ============
call :log "[验证] 关键模块 ..."
echo.
echo [验证] 检查关键模块...
set "FAIL=0"

"%PYTHON_EXE%" -c "import numpy, cv2, insightface, onnxruntime; print('  [OK] 人脸核验端依赖')" 2>nul
if %errorlevel% neq 0 ( set "FAIL=1" & call :log "[失败] 人脸核验端依赖验证失败" & echo   [失败] 人脸核验端依赖验证失败 ) else ( call :log "[OK] 人脸核验端依赖验证通过" )

"%PYTHON_EXE%" -c "import flask, jwt, bcrypt, flask_cors, flask_limiter; print('  [OK] 后端依赖')" 2>nul
if %errorlevel% neq 0 ( set "FAIL=1" & call :log "[失败] 后端依赖验证失败" & echo   [失败] 后端依赖验证失败 ) else ( call :log "[OK] 后端依赖验证通过" )

"%PYTHON_EXE%" -c "import cv2; assert hasattr(cv2,'HOGDescriptor') and hasattr(cv2,'HOGDescriptor_getDefaultPeopleDetector'); print('  [OK] HOGDescriptor（02/03 AI 框选识别必要条件）')" 2>nul
if %errorlevel% neq 0 ( set "FAIL=1" & call :log "[失败] HOGDescriptor 缺失，AI 框选识别不可用" & echo   [失败] HOGDescriptor 缺失，AI 框选识别不可用 ) else ( call :log "[OK] HOGDescriptor 验证通过" )

"%PYTHON_EXE%" -c "import tkinter, sqlite3; print('  [OK] tkinter / sqlite3')" 2>nul
if %errorlevel% neq 0 ( set "FAIL=1" & call :log "[失败] tkinter/sqlite3 缺失" & echo   [失败] tkinter/sqlite3 缺失 ) else ( call :log "[OK] tkinter/sqlite3 验证通过" )

REM ============ 10. 完成 ============
call :log "==================================================="
call :log " 安装结束：%date% %time%  FAIL=%FAIL%"
call :log "==================================================="

echo.
echo ===================================================
if "%FAIL%"=="0" (
    echo  环境安装完成，全部验证通过！
) else (
    echo  环境安装完成，但存在 [失败] 项，请查看上方提示
)
echo  日志：%LOG_FILE%
echo ===================================================
echo.
echo 人脸识别模型部署提醒：
echo   将 buffalo_l.zip 解压到 %USERPROFILE%\.insightface\models\buffalo_l
echo   （模型文件在：%FACE_MODEL_ZIP%）
echo.
echo 下一步：
echo   1. 02 启动前自检：%CAMERA_DIR%\启动前自检.bat
echo   2. 02 桌面端：    %CAMERA_DIR%\启动核查界面.bat
echo   3. 03 智能终端：  %TERMINAL_DIR%\启动终端界面.bat
echo   4. 01 网页后端：  %FACE_DIR%\网页操作面板\打开网页.bat
echo.
pause
endlocal
exit /b %FAIL%


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
