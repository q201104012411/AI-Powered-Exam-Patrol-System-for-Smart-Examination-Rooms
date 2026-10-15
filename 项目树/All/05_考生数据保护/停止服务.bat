@echo off
set "H=%~dp0"
cd /d "%H%"

echo 正在关闭 05 考生数据保护桌面管理界面（gui.py）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*gui.py*' -and $_.CommandLine -like '*05_*' } | ForEach-Object { Write-Host ('已关闭界面进程 ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo 完成。
pause
