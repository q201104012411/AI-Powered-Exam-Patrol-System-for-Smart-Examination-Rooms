@echo off
set "H=%~dp0"
cd /d "%H%"

REM ============================================================
REM  考生数据保护 · 一键自检与测试
REM     [1/5] 语法检查
REM     [2/5] 单元测试（临时目录，不污染真实数据）
REM     [3/5] 端到端自检（临时目录，覆盖 110 个检查点）
REM     [4/5] 桌面界面冒烟（无头构造界面并跑通全流程）
REM     [5/5] 验收指标（六项 KPI）
REM  说明：单元测试与自检都落临时目录，脚本不会往 data\ 写数据。
REM        需要 Python 3.10+（含 tkinter），且装 gmssl 与 cryptography（加密与签名后端）。
REM ============================================================
REM ---- 挑一个具备加密/签名后端的解释器（gmssl 或 cryptography）----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY call :accept "%%~C"
)
if defined PY goto :after_pick
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "C:\Python312\python.exe"
) do if not defined PY if exist %%P call :accept %%P
:after_pick

if not defined PY (
    echo [错误] 未找到装了 gmssl / cryptography 的 Python
    echo        请先双击「安装依赖.bat」
    pause
    exit /b 1
)

echo 使用解释器：%PY%
echo.

echo ============================================
echo  [1/5] 语法检查
echo ============================================
"%PY%" -m py_compile cli.py main.py demo_data.py integration_api.py gui.py dpp\__init__.py dpp\config.py dpp\catalog.py dpp\crypto.py dpp\integrity.py dpp\secure_store.py dpp\keyring.py dpp\desensitize.py dpp\models.py dpp\storage.py dpp\access.py dpp\lifecycle.py dpp\rights.py dpp\governance.py dpp\report.py dpp\demo_flags.py
if errorlevel 1 (
    echo 语法检查未通过
    pause
    exit /b 1
)
echo 语法通过
echo.

echo ============================================
echo  [2/5] 单元测试（临时目录）
echo ============================================
"%PY%" -m unittest discover -s tests -t .
if errorlevel 1 (
    echo.
    echo 单元测试未通过，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  [3/5] 端到端自检（临时目录）
echo ============================================
"%PY%" main.py
if errorlevel 1 (
    echo.
    echo 自检未通过，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  [4/5] 桌面界面冒烟（无头构造 + 六标签页 + 全流程）
echo ============================================
"%PY%" tests\_gui_smoke.py
if errorlevel 1 (
    echo.
    echo 桌面界面冒烟未通过，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  [5/5] 验收指标（六项 KPI）
echo ============================================
"%PY%" cli.py demo >nul 2>&1
"%PY%" cli.py kpi
if errorlevel 1 (
    echo.
    echo 验收指标未全部达标，请查看上方输出
    pause
    exit /b 1
)

echo.
echo ============================================
echo  全部通过。
echo  下一步：双击「启动服务.bat」打开桌面管理界面。
echo  账户：owner/owner（所有者）、dpo1/dpo1（数据保护负责人）、
echo        auditor1/auditor1（审计员）、oper1/oper1（业务操作员）、inv01/inv01（监考员）
echo ============================================
pause
exit /b 0


REM ------------------------------------------------------------
:accept
REM 参数 %~1 = 候选解释器路径；有加密后端（gmssl 或 cryptography）才采纳
"%~1" -c "import gmssl" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
if defined PY goto :eof
"%~1" -c "import cryptography" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof
