@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set "H=%~dp0"
set "PYTHONIOENCODING=utf-8"
cd /d "%H%"

rem ===========================================================
rem  Start with pythonw.exe  ->  NO console window at all.
rem  Keep this file pure ASCII and with CRLF line endings:
rem  cmd.exe parses .bat byte-wise, so non-ASCII text or LF-only
rem  line endings make it fail with "not recognized as a command".
rem  All Chinese messages come from Python (dialog) / PowerShell.
rem ===========================================================

set "PYW="

rem ---- 1) pythonw.exe already on PATH ----
for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%p"
)

rem ---- 2) python.exe on PATH -> pythonw.exe sits next to it ----
if not defined PYW (
    set "PYEXE="
    for /f "delims=" %%p in ('where python.exe 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
    if defined PYEXE (
        for %%d in ("!PYEXE!") do set "CAND=%%~dpdpythonw.exe"
        if exist "!CAND!" set "PYW=!CAND!"
    )
)

rem ---- 3) py launcher (query only when py.exe really exists) ----
if not defined PYW (
    where py.exe >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%p in ('py -3 -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do (
            if not defined PYW if exist "%%p" set "PYW=%%p"
        )
    )
)

rem ---- 4) common per-user install locations ----
if not defined PYW (
    for %%v in (312 311 313 310 39) do (
        if not defined PYW (
            for %%r in ("%LOCALAPPDATA%\Programs\Python\Python%%v"
                        "%ProgramFiles%\Python%%v") do (
                if not defined PYW if exist "%%~r\pythonw.exe" set "PYW=%%~r\pythonw.exe"
            )
        )
    )
)

if not defined PYW goto :nopython
if not exist "%PYW%" goto :nopython
if not exist "%H%launcher.py" goto :nofile

start "" /d "%H%" "%PYW%" "%H%launcher.py"
exit /b 0

:nopython
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName System.Windows.Forms; $m = [string]::Join([char]10, @('Python 3 with tkinter was not found.', '', 'Install Python 3.10+ and tick both boxes in the installer:', '    tcl/tk and IDLE', '    Add python.exe to PATH', '', 'Then double-click this file again.')); [System.Windows.Forms.MessageBox]::Show($m, 'Exam Room App - cannot start', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null"
exit /b 1

:nofile
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('launcher.py not found next to this .bat. Please keep the whole folder together.', 'Exam Room App - cannot start', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null"
exit /b 1
