@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sqlite3,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到可用的 Python 3
    pause
    exit /b 1
)

echo ============================================================
echo  体检：01/02/03/06 的真实运行库有没有被本专题的自检污染
echo  （专题 10 只允许"单向读取 + 登记机位"，不该往别人库里写业务数据）
echo ============================================================
echo.
"%PY%" "%H%工具_检查其他模块是否被污染.py"
echo.
pause
