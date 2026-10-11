@echo off
chcp 65001 >nul
title 上传到 GitHub - 智能考场AI巡考系统
setlocal
cd /d "%~dp0"

set "REPO=https://github.com/q201104012411/AI-Powered-Exam-Patrol-System-for-Smart-Examination-Rooms.git"

rem ---- 步骤 0：确保 git 可用 ----
where git >nul 2>nul
if errorlevel 1 set "PATH=%ProgramFiles%\Git\cmd;%ProgramFiles%\Git\bin;%LOCALAPPDATA%\Programs\Git\cmd;%PATH%"
where git >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 git，请先安装 Git for Windows：https://git-scm.com/download/win
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   智能考场AI巡考系统  —  上传到 GitHub
echo   仓库：%REPO%
echo ============================================================
echo.

rem ---- 步骤 1：初始化（仅首次）----
if not exist ".git" (
  echo [1/5] 初始化本地仓库 ...
  git init -b main
) else (
  echo [1/5] 本地仓库已存在，跳过初始化
)

rem ---- 步骤 2：绑定远程地址 ----
echo [2/5] 检查远程地址 ...
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin "%REPO%"
) else (
  git remote set-url origin "%REPO%"
)

rem ---- 步骤 3：暂存全部改动 ----
echo [3/5] 扫描并暂存改动 ...
git add -A

rem ---- 步骤 4：提交 ----
echo [4/5] 提交 ...
git commit -m "update %date% %time%" >nul 2>nul
if errorlevel 1 (
  echo        ^(没有新改动，直接进入推送^)
) else (
  echo        已创建新提交
)

rem ---- 步骤 5：推送（实时进度）----
echo.
echo [5/5] 正在推送到 GitHub，实时进度如下：
echo ------------------------------------------------------------
git push origin main --progress
if not errorlevel 1 goto success

echo.
echo   普通推送被拒绝（远程有本地没有的提交），改用强制推送 ...
echo ------------------------------------------------------------
git push -f origin main --progress
if not errorlevel 1 goto success

echo ------------------------------------------------------------
echo.
echo   [失败] 上传未成功
echo.
echo   常见原因与处理：
echo     1. 未登录 GitHub    -- 重新运行本脚本，在弹出的窗口完成登录
echo     2. 网络不通/需代理  -- 检查网络后重试
echo     3. 单文件超过 100MB -- 把该文件写进 .gitignore 后重试
echo.
pause
exit /b 1

:success
echo ------------------------------------------------------------
echo.
echo   [成功] 上传完成！
echo   查看：https://github.com/q201104012411/AI-Powered-Exam-Patrol-System-for-Smart-Examination-Rooms
echo.
pause
exit /b 0
