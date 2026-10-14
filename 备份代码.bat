@echo off
chcp 65001 >nul
title 代码备份 - 智能考场AI巡考系统
setlocal enabledelayedexpansion

REM ============================================================
REM  代码备份（2026-10-14 起备份统一收到仓库根目录的 备份\ 下）
REM  目标：备份\<项目名>\<项目名>_代码备份_<时间戳>\
REM  来源：项目树\All\ 下每个"含 .py 的实现项目"
REM
REM  排除（只备份代码与文档，不带数据/模型/日志）
REM    data / 备份 / _tmp / tmp / __pycache__ / .git / 虚拟环境 / node_modules
REM    *.zip *.exe *.onnx *.pt *.db *.enc *.key *.log *.bak
REM  目的：备份里不含考生个人信息与生物特征，避免备份本身成为泄密源
REM ============================================================

REM 本脚本位于 备份\ 内，仓库根目录是它的上一级
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
if not exist "项目树\All" (
  echo [错误] 没找到 项目树\All，请把本脚本放在仓库根目录的 备份\ 里运行
  pause
  exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if "%TS%"=="" (
  echo [错误] 无法获取时间戳
  pause
  exit /b 1
)

echo ============================================================
echo   代码备份   时间戳：%TS%
echo   输出目录：%HERE%
echo ============================================================
echo.

set /a COUNT=0
set /a SKIP=0

for /d %%P in ("项目树\All\*") do (
  dir /b /s "%%P\*.py" >nul 2>nul
  if errorlevel 1 (
    set /a SKIP+=1
  ) else (
    set "NAME=%%~nxP"
    set "DST=%HERE%!NAME!\!NAME!_代码备份_%TS%"
    echo [备份] !NAME!
    robocopy "%%P" "!DST!" /E /R:1 /W:1 /XD "data" "备份" "_tmp" "tmp" "__pycache__" ".git" ".venv" "venv" ".pytest_cache" ".mypy_cache" "node_modules" /XF "*.zip" "*.exe" "*.msi" "*.onnx" "*.pt" "*.pth" "*.db" "*.db-wal" "*.db-shm" "*.enc" "*.key" "*.log" "*.bak" "*.pyc" /NFL /NDL /NJH /NJS /NP >nul
    set /a COUNT+=1
  )
)

echo.
echo ============================================================
echo   完成：已备份 !COUNT! 个实现项目，跳过 !SKIP! 个无代码目录
echo   位置：备份\<项目名>\<项目名>_代码备份_%TS%\
echo ============================================================
pause
