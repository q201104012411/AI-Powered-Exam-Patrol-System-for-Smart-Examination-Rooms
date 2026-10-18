@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cryptography,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到具备 cryptography 的 Python（py -3.12 -m pip install cryptography）
    pause
    exit /b 1
)

echo ============================================
echo  03 智能终端 · 网页版大数据驾驶舱
echo  浏览器打开后按 F11 即可当考场大屏用；
echo  数据与终端里的「🛰 驾驶舱」是同一份（同一个库、同一套 SQL 聚合）。
echo ============================================
echo.
echo  接入令牌（跨设备访问时要填在页面右上角「连接设置」里）：
"%PY%" "%H%lan_service.py" --print-token
echo.
<nul set /p "=监听地址（回车=仅本机 127.0.0.1；输入 0.0.0.0 = 允许局域网大屏访问）："
set "LANHOST="
set /p "LANHOST="
if not defined LANHOST set "LANHOST=127.0.0.1"
<nul set /p "=端口（回车=默认 9113）："
set "LANPORT="
set /p "LANPORT="
if not defined LANPORT set "LANPORT=9113"
echo.
echo  页面地址：http://127.0.0.1:%LANPORT%/        （本机访问免令牌）
echo  数据接口：http://127.0.0.1:%LANPORT%/api/cockpit
echo.
start "" "http://127.0.0.1:%LANPORT%/"
"%PY%" "%H%lan_service.py" --host %LANHOST% --port %LANPORT%
echo.
echo 服务已退出（页面上的「连接失败」属正常，服务没起就没数据）。
pause
