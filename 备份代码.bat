@echo off
chcp 936 >nul
title 代码备份 - 智能考场AI巡考系统
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ============================================================
REM  代码备份
REM  对 项目树\All\ 下每个"含 .py 的实现项目"，把源码复制到
REM      <项目>\备份\<项目名>_代码备份_<时间戳>\
REM
REM  排除（重要）：
REM    data / 备份 / _tmp / tmp  运行期数据、历史备份、测试临时产物
REM        —— tests\_tmp 里有测试生成的「初始密码_*.txt」，绝不能进备份
REM    __pycache__ / .git / 虚拟环境   构建与版本控制产物
REM    *.zip *.exe *.onnx *.db *.enc *.key *.log    模型包、安装包、库、密钥、日志
REM  目的：只备份代码与文档，不含生物特征与任何口令，避免备份本身成为泄露源。
REM
REM  注意：检测必须用递归(dir /b /s)。01 的代码在**嵌套一层**的子目录里
REM  （01_人脸识别与身份核对\01_人脸识别与身份核对\*.py），只看顶层会漏掉它。
REM ============================================================

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if "%TS%"=="" (
  echo [错误] 无法获取时间戳
  pause
  exit /b 1
)

echo ============================================================
echo   代码备份   时间戳：%TS%
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
    set "DST=%%P\备份\!NAME!_代码备份_%TS%"
    echo [备份] !NAME!
    robocopy "%%P" "!DST!" /E /R:1 /W:1 /XD "data" "备份" "_tmp" "tmp" "__pycache__" ".git" ".venv" "venv" ".pytest_cache" ".mypy_cache" "node_modules" /XF "*.zip" "*.exe" "*.msi" "*.onnx" "*.pt" "*.pth" "*.db" "*.db-wal" "*.db-shm" "*.enc" "*.key" "*.log" "*.bak" "*.pyc" /NFL /NDL /NJH /NJS /NP >nul
    set /a COUNT+=1
  )
)

echo.
echo ============================================================
echo   完成：已备份 !COUNT! 个实现项目（跳过 !SKIP! 个无代码目录）
echo   位置：各项目目录下的「备份」文件夹
echo ============================================================
pause