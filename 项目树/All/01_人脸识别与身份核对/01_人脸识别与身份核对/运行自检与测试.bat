@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ============================================================
REM  一键体检：路径 -> 参考照 -> 数据库 -> 测试 -> 端到端自检 -> 状态
REM  解释器选择交给 pick_python.py（它会挑出真正装了依赖的那个 Python）
REM ============================================================

set "HERE=%~dp0"
set "PICKER=%HERE%pick_python.py"

REM ---------- 1) 找一个能启动的 Python 作为引导 ----------
set "BOOT="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT for /f "delims=" %%p in ('python3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined BOOT set "BOOT=%%p"
if not defined BOOT (
    echo [错误] 本机没有找到任何 Python。
    echo        请先运行：install环境一键下载.bat
    pause
    exit /b 1
)

REM ---------- 2) 用引导解释器挑出"依赖齐全"的解释器 ----------
set "PYTHON_EXE="
set "PICKFILE=%TEMP%\pypick_%RANDOM%.txt"
"%BOOT%" "%PICKER%" face --out "%PICKFILE%" >nul 2>nul
if exist "%PICKFILE%" for /f "usebackq delims=" %%p in ("%PICKFILE%") do set "PYTHON_EXE=%%p"
del "%PICKFILE%" >nul 2>nul
if not defined PYTHON_EXE set "PYTHON_EXE=%BOOT%"

"%PYTHON_EXE%" -c "import numpy, cv2, insightface, onnxruntime" >nul 2>nul
if errorlevel 1 (
    echo [错误] 选中的解释器缺少人脸识别依赖：
    echo        %PYTHON_EXE%
    echo.
    echo        可用解释器一览：
    "%BOOT%" "%PICKER%" face --all
    echo.
    echo        解决办法：运行 install环境一键下载.bat，或手动执行
    echo        "%PYTHON_EXE%" -m pip install -r requirements-face.txt
    pause
    exit /b 1
)

echo 使用解释器：%PYTHON_EXE%
echo.

echo [1/6] 运行路径体检（中文/空格/超长路径）...
"%PYTHON_EXE%" enroll.py check-paths
if errorlevel 1 goto fail

echo.
echo [2/6] 参考照体检（损坏 / 无人脸 / 文件截断）...
"%PYTHON_EXE%" enroll.py check-photos
echo        （本项异常不阻断后续流程）

echo.
echo [3/6] 初始化 / 迁移数据库 ...
"%PYTHON_EXE%" "网页操作面板\server\db.py"
if errorlevel 1 goto fail

echo.
echo [4/6] 单元测试 + 网页端集成测试 ...
"%PYTHON_EXE%" -m unittest discover -s tests -t .
if errorlevel 1 goto fail

echo.
echo [5/6] 真实模型端到端自检（12 场景：含昏暗/撞脸/仰拍/灰区/证件核对）...
"%PYTHON_EXE%" main.py
if errorlevel 1 goto fail

echo.
echo [6/6] 运行状态一览 ...
"%PYTHON_EXE%" enroll.py status

echo.
echo ============================================================
echo  全部通过。若参考照体检有告警，请换照片后重新建档。
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ============================================================
echo  上一步未通过，请查看上方输出定位问题。
echo ============================================================
pause
exit /b 1
