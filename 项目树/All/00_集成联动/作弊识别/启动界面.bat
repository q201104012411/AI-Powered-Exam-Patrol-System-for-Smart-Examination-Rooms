@echo off
rem 本文件为 GBK(cp936) 编码：前两行必须是纯 ASCII，
rem 以便在任意代码页下都能正确解析，随后再切换代码页。
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if not errorlevel 1 goto run
set "PY=py"
where py >nul 2>nul
if not errorlevel 1 goto run
echo [错误] 未找到 python，请先安装 Python 3.8 以上版本并加入 PATH。
pause
exit /b 1

:run
"%PY%" "%~dp0界面.py"
if errorlevel 1 (
  echo.
  echo [错误] 界面启动失败，请查看上方错误信息。
  pause
)
