@echo off
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  考生数据保护 · 修复 / 重置密钥环
REM   用在：项目目录从别的电脑或别的 Windows 账户拷过来后，
REM         DPAPI 报 0x8009000B，界面里敏感字段全部解不开。
REM   本脚本先体检，再经确认后整目录备份 + 重建密钥与演示数据。
REM ============================================================

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 未找到 Python，请先双击「安装依赖.bat」。
    pause
    exit /b 1
)

echo 使用解释器：%PY%
echo.
echo ============================================
echo  密钥环体检
echo ============================================
"%PY%" cli.py keyring
if not errorlevel 1 (
    echo.
    echo 密钥环正常，无需重置。
    pause
    exit /b 0
)

echo.
echo ============================================
echo  密钥环不可用，需要重置
echo ============================================
echo  将要执行：
echo    1) 把 data\ 整目录备份成同级的 data_backup_时间戳\ 目录
echo    2) 删除旧密钥，以及用旧密钥加密、本机已解不开的数据
echo       （dpp.db、data\vault、data\exports、data\reports）
echo    3) 重新生成主密钥与签名密钥，并重灌演示数据
echo  说明：data\logs 会保留；所有旧文件都在备份目录里，可随时回查。
echo.
set "OK="
set /p "OK=确认请输入 YES 再回车（直接回车=取消）："
if /i not "%OK%"=="YES" (
    echo.
    echo 已取消，未做任何改动。
    pause
    exit /b 1
)

echo.
"%PY%" cli.py keyring --reset --yes --with-demo
if errorlevel 1 (
    echo.
    echo 重置失败，请查看上方输出。
    pause
    exit /b 1
)

echo.
echo ============================================
echo  修复完成：双击「启动服务.bat」即可用 owner / owner 登录
echo ============================================
pause
