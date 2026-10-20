@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0gui.py"
  exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
  start "" python "%~dp0gui.py"
  exit /b 0
)
echo [错误] 未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH
pause
exit /b 1