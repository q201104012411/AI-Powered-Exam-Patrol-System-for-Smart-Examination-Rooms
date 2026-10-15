@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for /f "delims=" %%p in ('py -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python
    pause
    exit /b 1
)

echo ============================================
echo  [1/3] 编译检查
echo ============================================
"%PY%" -m py_compile config.py models.py storage.py data_crypto.py access.py alerts.py review.py offline.py fleet.py clock.py bridge.py lan_service.py _face_bridge.py person_detect.py terminal_gui.py main.py
if errorlevel 1 (
    echo 编译未通过
    pause
    exit /b 1
)
echo 编译通过

echo.
echo ============================================
echo  [2/3] 单元测试（124 项，临时数据目录）
echo ============================================
"%PY%" -m unittest discover -s tests -t .
if errorlevel 1 (
    echo.
    echo 单元测试未通过，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  [3/3] 端到端自检（74 项，临时数据目录）
echo ============================================
"%PY%" main.py
if errorlevel 1 (
    echo.
    echo 自检未通过，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  全部通过。
echo  下一步：双击「启动终端界面.bat」打开界面
echo  （账户：owner 所有者，patrol1 巡考员，inv01 监考员）
echo ============================================
pause
