@echo off
rem 本文件为 GBK(cp936) 编码：前两行必须是纯 ASCII，
rem 以便在任意代码页下都能正确解析，随后再切换代码页。
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   考生作弊智能识别 - 核心逻辑演示与自检
echo ============================================================
echo.

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.8 以上版本并加入 PATH。
    pause
    exit /b 1
  )
  set "PY=py"
)

echo [1/2] 运行演示（共八节）...
echo ------------------------------------------------------------
%PY% "%~dp0演示.py"
if errorlevel 1 (
  echo.
  echo [错误] 演示运行失败。
  pause
  exit /b 1
)

echo.
echo [2/2] 运行自检（共 48 项）...
echo ------------------------------------------------------------
%PY% "%~dp0自检.py"
if errorlevel 1 (
  echo.
  echo [错误] 自检存在失败项，请查看上方 FAIL 行。
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   全部完成。
echo   单元测试请运行: python -m unittest discover -s tests -v
echo ============================================================
pause
