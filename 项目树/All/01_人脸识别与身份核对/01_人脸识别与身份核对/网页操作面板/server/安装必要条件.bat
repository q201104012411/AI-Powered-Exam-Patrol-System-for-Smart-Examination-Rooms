@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ============================================================
REM  网页操作面板 · 安装依赖
REM  同时安装人脸端与网页端依赖（本面板与核验端共用数据库）
REM ============================================================

set "HERE=%~dp0"
set "FACE_DIR=%HERE%..\.."
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PYTHON_EXE="

set "BOOT="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT (
    echo [错误] 未找到 Python，请先安装 Python 3.11/3.12
    pause
    exit /b 1
)
set "PYTHON_EXE=%BOOT%"
echo 使用解释器：%PYTHON_EXE%
echo.

echo [1/3] 网页端依赖 ...
"%PYTHON_EXE%" -m pip install -r "%HERE%requirements.txt" -i %MIRROR%
if errorlevel 1 goto fail

echo.
echo [2/3] 人脸端依赖 ...
if exist "%FACE_DIR%\requirements-face.txt" (
    "%PYTHON_EXE%" -m pip install -r "%FACE_DIR%\requirements-face.txt" -i %MIRROR%
) else (
    "%PYTHON_EXE%" -m pip install numpy opencv-python insightface onnxruntime -i %MIRROR%
)
if errorlevel 1 goto fail

echo.
echo [3/3] Excel 模板工具依赖 ...
"%PYTHON_EXE%" -m pip install openpyxl -i %MIRROR%

echo.
echo ============================================================
echo  依赖安装完成。下一步：
echo    1) 初始化数据库：python db.py
echo    2) 启动后端：启动后端.bat
echo    3) 浏览器访问：http://127.0.0.1:5000/
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [错误] 依赖安装失败，请检查网络或改用其他镜像源。
pause
exit /b 1
