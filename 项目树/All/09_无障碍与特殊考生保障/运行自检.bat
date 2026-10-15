@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  09 无障碍与特殊考生保障 —— 自检
REM  默认用临时数据目录，可破坏性测试（不碰真实数据）
REM  参数： 运行自检.bat real     -> 真实数据目录只读体检
REM         运行自检.bat nolink   -> 跳过联动组（对端专题缺失时用）
REM ============================================================

set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.10~3.13 并勾选 Add to PATH
    pause
    exit /b 1
)

set "ARGS="
if /i "%~1"=="real" set "ARGS=--real"
if /i "%~1"=="nolink" set "ARGS=--no-link"
if /i "%~1"=="--real" set "ARGS=--real"
if /i "%~1"=="--no-link" set "ARGS=--no-link"

"%PY%" 自检.py %ARGS%
echo.
pause
