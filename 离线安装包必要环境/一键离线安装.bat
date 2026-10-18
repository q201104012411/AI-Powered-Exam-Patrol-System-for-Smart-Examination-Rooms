@echo off
REM ============================================================
REM  Smart Exam Room AI Patrol System
REM  pip dependencies - OFFLINE one-click installer (launcher)
REM
REM  All Chinese UI text lives in install_offline.py (UTF-8 safe).
REM  This launcher is intentionally pure ASCII: cmd.exe parses
REM  .bat files in the system ANSI code page, so non-ASCII text
REM  here can corrupt command parsing on some machines.
REM
REM  Usage:
REM    double-click            -^> interactive menu
REM    this.bat -c             -^> check only
REM    this.bat -v             -^> verify only
REM    this.bat --help         -^> full options
REM ============================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "HERE=%~dp0"
cd /d "%HERE%"
set "RC=1"

REM ---------- find CPython 3.12 (wheelhouse is cp312 / win_amd64) ----------
set "PY="
for %%C in ("py -3.12" "py -3.12-64") do (
    if not defined PY (
        for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable)" 2^>nul') do (
            if exist "%%p" set "PY=%%p"
        )
    )
)
if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe"
        "C:\Python312\python.exe"
        "%ProgramFiles%\Python312\python.exe"
        "%ProgramFiles(x86)%\Python312\python.exe"
    ) do (
        if not defined PY if exist %%P set "PY=%%~P"
    )
)

if not defined PY (
    REM locate the bundled Python installer without embedding CJK literals
    set "PYEXE="
    for /f "delims=" %%F in ('dir /b /s "%HERE%..\python-3.12*-amd64.exe" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%F"
    )
    echo.
    echo ============================================================
    echo  [ERROR] Python 3.12 not found
    echo ============================================================
    echo.
    echo  This offline package ships CPython 3.12 + win_amd64 wheels.
    echo  numpy / scipy / opencv / onnxruntime here will NOT install
    echo  on Python 3.10 / 3.11 / 3.13 / 3.15.
    echo.
    echo  Please install Python 3.12 first ^(an offline installer is
    echo  bundled with this project^):
    echo.
    if defined PYEXE (
        echo    !PYEXE!
    ) else (
        echo    ^(not found under the project root^)
    )
    echo.
    echo  Official: https://www.python.org/downloads/release/python-3124/
    echo.
    echo  During setup you MUST tick:
    echo    [x] Add python.exe to PATH
    echo    [x] tcl/tk and IDLE
    echo.
    pause
    exit /b 1
)

"%PY%" "%HERE%install_offline.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" (
    echo  DONE - all checks passed
) else (
    echo  FINISHED with return code %RC% - see [FAIL] lines above
)
echo  Log: %HERE%..\logs\install_offline.log
echo ============================================================
echo.
pause
REM expand RC BEFORE endlocal clears the scope (standard cmd idiom)
endlocal & exit /b %RC%
