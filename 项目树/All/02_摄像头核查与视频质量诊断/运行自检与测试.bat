@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 挑一个真正装了依赖的解释器（默认 py 可能是没装 numpy/cv2 的版本）----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,numpy,tkinter,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 opencv / numpy / tkinter 的 Python
    echo  请执行（示例）：
    echo      py -3.12 -m pip install opencv-python numpy
    echo ============================================
    pause
    exit /b 1
)
echo 使用解释器：%PY%

echo.
echo ============================================
echo  [1/3] 编译检查
echo ============================================
"%PY%" -m py_compile config.py models.py storage.py data_crypto.py web_accounts.py diagnose.py camera_stream.py person_detect.py probe.py baseline.py patrol.py workorder.py report.py gui.py main.py
if errorlevel 1 ( echo 编译未通过 & pause & exit /b 1 )
echo 编译通过

echo.
echo ============================================
echo  [2/3] 单元测试（128 项，临时数据目录）
echo ============================================
"%PY%" -m unittest discover -s tests -t .
if errorlevel 1 ( echo. & echo 单元测试未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  [3/3] 端到端自检（108 项，合成缺陷图，临时数据目录）
echo ============================================
"%PY%" main.py
if errorlevel 1 ( echo. & echo 自检未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  全部通过。
echo  注意：自检用合成画面验证算法判定；真实在线率/故障时延/覆盖率
echo        必须靠现场巡检样本积累，统计为 0 不代表达标。
echo  下一步：双击「启动核查界面.bat」用 owner / EDY_POWERQ 登录
echo          （黑窗口想隐藏：双击「切换命令行窗口.bat」，或在所有者界面里勾选）
echo ============================================
pause
