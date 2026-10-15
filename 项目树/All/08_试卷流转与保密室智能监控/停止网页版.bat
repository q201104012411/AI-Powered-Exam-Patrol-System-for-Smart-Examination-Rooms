@echo off
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
echo 正在停止 08 网页服务（app.py，端口 5020）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*网页监控面板*app.py*' } | ForEach-Object { Write-Host ('已停止进程 ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
if exist "%~dp0data\web.pid" del /q "%~dp0data\web.pid" >nul 2>nul
echo 完成。
pause
