@echo off
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  考生数据保护 · 查看验收报告
REM   1. 生成/刷新验收报告（读取当前运行目录数据）
REM   2. 导出处理活动登记表、销毁记录台账
REM   3. 用默认程序打开报告文件
REM  提示：若要报告展示"完整数据"，可先运行  python cli.py demo  生成演示数据。
REM         写入的是真实运行目录（data\），演示数据带 demo 标记，不冒充真实接入。
REM         如需清空重来，删除 data\ 整个目录即可。
REM ============================================================

call :find_python
if not defined PY goto :no_python

echo 使用解释器：%PY%
echo.

echo ============================================
echo  生成/刷新验收报告
echo ============================================
"%PY%" cli.py report
if errorlevel 1 (
    echo 生成失败，请查看上方输出
    pause
    exit /b 1
)

REM ---- 找到最新生成的报告文件并打开 ----
set "RPT="
for /f "delims=" %%f in ('dir /b /o-d "%H%data\reports\考生数据保护验收报告_*.md" 2^>nul') do (
    if not defined RPT set "RPT=%%f"
)
if defined RPT (
    echo.
    echo 打开报告：%RPT%
    start "" "%H%data\reports\%RPT%"
) else (
    echo.
    echo 未找到报告文件，请确认 data\reports\ 目录已生成
)

echo.
echo 报告目录：%H%data\reports\
echo   - 考生数据保护验收报告_*.md
echo   - 数据处理活动登记表_*.csv
echo   - 销毁记录台账_*.csv
echo.
pause
exit /b 0

REM ------------------------------------------------------------
:find_python
set "PY="
for /f "delims=" %%p in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :accept "%%p"
if defined PY goto :eof
for /f "delims=" %%p in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :accept "%%p"
if defined PY goto :eof
for /f "delims=" %%p in ('py -3.13 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :accept "%%p"
if defined PY goto :eof
for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :accept "%%p"
if defined PY goto :eof
for %%C in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "C:\Python312\python.exe"
) do if not defined PY if exist %%C call :accept %%C
if defined PY goto :eof
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :accept "%%p"
if defined PY goto :eof
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%p"
if defined PY (
    echo [警告] 找到 Python 但缺少加密后端（gmssl / cryptography）。
    echo        请执行： "%PY%" -m pip install gmssl cryptography
    echo.
)
goto :eof

:accept
REM 参数 %~1 = 候选解释器路径；有加密后端（gmssl 或 cryptography）才采纳
"%~1" -c "import gmssl" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
if defined PY goto :eof
"%~1" -c "import cryptography" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof

:no_python
echo [错误] 未找到 Python。请安装 Python 3.10+ 并加入 PATH。
echo        安装后还需： python -m pip install gmssl cryptography
pause
exit /b 1
