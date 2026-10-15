@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"
set "PYTHONUTF8=1"

set "PYF=%TEMP%\examfire_ENV_path.txt"
del "%PYF%" >nul 2>nul
if not exist "%PYF%" py -3.12 -c "import sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if not exist "%PYF%" py -3 -c "import sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if not exist "%PYF%" python -c "import sys,io;io.open(r'%PYF%','w',encoding='utf-8').write(sys.executable.strip())" 2>nul
if exist "%PYF%" set /p PY=<"%PYF%"
del "%PYF%" >nul 2>nul

if not defined PY (
    echo [错误] 没找到 Python。请安装 Python 3.10~3.13 并在安装时勾选 Add to PATH。
    pause
    exit /b 1
)

"%PY%" 安装环境.py %*
pause
exit /b %ERRORLEVEL%
