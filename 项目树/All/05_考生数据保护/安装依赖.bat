@echo off
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  考生数据保护 · 安装运行依赖
REM    · 界面：tkinter / sqlite3（Python 官方自带，无需 pip 安装）
REM    · 加密：gmssl（SM4/SM3/SM2，国密优先） 或 cryptography（合规回退）
REM    · 完整性：SM3 哈希 + SM2 签名（缺国密库时回退 SHA-256 + RSA）
REM  与 All 下其他专题（01/02/03/11）保持同一套依赖口径。
REM ============================================================

set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)
echo 使用解释器：%PY%
echo.

echo [1/3] 升级 pip ...
"%PY%" -m pip install --upgrade pip
echo.

echo [2/3] 安装国密库 gmssl（SM4/SM3/SM2）...
"%PY%" -m pip install gmssl
echo.

echo [3/3] 安装 cryptography（无国密库时的合规回退）...
"%PY%" -m pip install cryptography
echo.

echo ============================================
echo  依赖安装完成，自检加密/签名后端：
echo ============================================
"%PY%" -c "import sys;print('Python:',sys.version.split()[0])"
"%PY%" -c "import tkinter,sqlite3;print('[OK] tkinter + sqlite3 已就绪 -> 桌面管理界面可用')" 2>nul || echo [警告] tkinter 不可用（请安装带 tkinter 的官方 Python）
"%PY%" -c "import gmssl;print('[OK] gmssl 已安装 -> SM4/SM3/SM2 可用')" 2>nul || echo [提示] gmssl 未装成功，将回退 cryptography
"%PY%" -c "import cryptography;print('[OK] cryptography 已安装 -> AES-GCM / RSA 可用')" 2>nul || echo [警告] cryptography 未装成功
echo.
echo 加密算法：SM4-CBC + SM3（国密优先）；无国密库时回退 AES-GCM
echo 完整性算法：SM3 + SM2 签名（无国密库时回退 SHA-256 + RSA-2048）
echo.
echo 下一步：双击「启动服务.bat」打开桌面管理界面。
pause
