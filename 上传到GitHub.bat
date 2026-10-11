@echo off
chcp 936 >nul
title 智能考场AI巡考系统 - 上传到 GitHub
setlocal
cd /d "%~dp0"

set "REPO=https://github.com/q201104012411/AI-Powered-Exam-Patrol-System-for-Smart-Examination-Rooms.git"

where git >nul 2>nul
if errorlevel 1 set "PATH=%ProgramFiles%\Git\cmd;%ProgramFiles%\Git\bin;%LOCALAPPDATA%\Programs\Git\cmd;%PATH%"
where git >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 git，请先安装 Git for Windows
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   智能考场AI巡考系统 - 上传到 GitHub
echo   仓库: %REPO%
echo ============================================================
echo.

if not exist ".git" (
  echo [1/5] 初始化本地仓库 ...
  git init -b main
) else (
  echo [1/5] 本地仓库已存在，跳过初始化
)

echo [2/5] 检查远程地址 ...
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin "%REPO%"
) else (
  git remote set-url origin "%REPO%"
)

echo [3/5] 暂存改动 ...
git add -A

echo [4/5] 提交 ...
git commit -m "update" >nul 2>nul
if errorlevel 1 (
  echo       没有新改动，直接推送
) else (
  echo       已创建新提交
)

echo.
echo [5/5] 正在推送，实时进度如下:
echo ------------------------------------------------------------
git push origin main --progress
if not errorlevel 1 goto success

echo.
echo   普通推送被拒绝，改用强制推送 ...
echo ------------------------------------------------------------
git push -f origin main --progress
if not errorlevel 1 goto success

echo ------------------------------------------------------------
echo.
echo   [失败] 上传未成功
echo.
echo   可能原因:
echo     1. 未登录 GitHub - 重新运行，在弹出的窗口完成登录
echo     2. 网络不通 - 检查网络后重试
echo     3. 单文件超过 100MB - 加入 .gitignore 后重试
echo.
pause
exit /b 1

:success
echo ------------------------------------------------------------
echo.
echo   [成功] 上传完成!
echo   查看: https://github.com/q201104012411/AI-Powered-Exam-Patrol-System-for-Smart-Examination-Rooms
echo.
pause
exit /b 0