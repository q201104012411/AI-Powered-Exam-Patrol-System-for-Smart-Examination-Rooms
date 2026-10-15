# -*- coding: utf-8 -*-
"""生成 05 的 .bat（GBK + CRLF）。仅在打包脚本时用，不参与运行时。"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 05_考生数据保护\

# ---------------------------------------------------------------- 公共片段
FIND_PY_GUI = r'''REM ---- 挑一个「有 tkinter 且有加密/签名后端」的解释器 ----
REM 桌面界面用 tkinter（官方 Python 自带，无需 pip 安装）。
REM 加密优先带 gmssl（国密 SM4/SM3/SM2）；否则退而求其次要有 cryptography。
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3") do (
    if not defined PY call :pick "%%~C"
)
if defined PY goto :py_ready
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "C:\Python312\python.exe"
) do if not defined PY if exist %%P call :pick %%P
if defined PY goto :py_ready
REM 最后才试 PATH 上的 python
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :pick "%%p"
:py_ready
if not defined PY (
    echo ============================================
    echo  [错误] 找不到同时具备 tkinter 与 加密库 的 Python
    echo  请先双击「安装依赖.bat」，或执行：
    echo      py -3.12 -m pip install gmssl cryptography
    echo ============================================
    pause
    exit /b 1
)
goto :found_py

:pick
REM %~1 = 候选解释器；必须 (tkinter + sqlite3) + (gmssl 或 cryptography)
"%~1" -c "import tkinter,sqlite3" >nul 2>&1 || goto :eof
"%~1" -c "import gmssl" >nul 2>&1 && set "PY=%~1"
if defined PY goto :eof
"%~1" -c "import cryptography" >nul 2>&1 && set "PY=%~1"
goto :eof

:found_py
'''

FIND_PY_CRYPTO = r'''REM ---- 挑一个具备加密/签名后端的解释器（gmssl 或 cryptography）----
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
'''

TITLE = "@echo off\nset \"H=%~dp0\"\ncd /d \"%H%\"\n"


def w(name, text):
    """
    写 .bat。

    编码用 **GBK（cp936，即中文 Windows 的 ANSI）+ CRLF**，并且**不写 chcp**。

    为什么不是 UTF-8 + `chcp 65001`（旧做法，已实测证明会坏）：
    cmd 是逐行读 .bat 的，读的时候按**当时**的控制台代码页解码。双击 .bat 时
    控制台初始代码页是 936，脚本里那句 `chcp 65001` 一生效，cmd 的读偏移就与
    已解码字符数错位，后面的中文行会被从半个字中间截断、当成命令执行，
    屏幕上就是一串 `'owner' is not recognized as an internal or external command`。

    实测对照（同一份内容，全新 936 控制台，输出重定向后比对）：
      A UTF-8 + chcp 65001  -> 解析错乱（与现场报错逐字一致）
      B GBK   + 无 chcp     -> 解析正确、中文显示正确   <== 采用
      C UTF-8 + 无 chcp     -> 解析错乱（中文按 GBK 误读）
      D UTF-8(BOM) + chcp   -> 解析错乱
      F 纯 ASCII            -> 正常
    结论：中文 .bat 必须与"双击时的默认代码页"一致（中文 Windows = GBK），
    并且脚本内部**不要碰 chcp**。命令行参数里的中文路径不受影响：
    参数由 CreateProcess 以 UTF-16 传递。
    """
    p = os.path.join(BASE, name)
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(p, "wb") as f:
        f.write(data.encode("gbk"))
    print("wrote %s (%d bytes, gbk)" % (name, len(data.encode("gbk"))))


# ================================================================ 安装依赖.bat
install = TITLE + r'''
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
'''

# ================================================================ 启动服务.bat
start = TITLE + r'''
REM ============================================================
REM  考生数据保护 · 启动桌面管理界面（Python / tkinter）
REM    账户：owner / EDY_POWERQ（所有者）
REM          dpo1/dpo1 · oper1/oper1 · auditor1/auditor1 · inv01/inv01
REM  停止：双击「停止服务.bat」
REM  提示：以 owner 登录后可在界面里切换「隐藏命令行窗口」。
REM ============================================================
''' + FIND_PY_GUI + r'''
echo 使用解释器：%PY%
echo ============================================
echo   考生数据保护 · 桌面管理界面
echo   账户：owner / EDY_POWERQ（所有者）
echo         dpo1/dpo1  oper1/oper1  auditor1/auditor1  inv01/inv01
echo   加密：国密优先（SM4/SM3），无国密库时回退 AES-GCM
echo   完整性：SM3 + SM2 签名（无国密库时回退 SHA-256 + RSA）
echo   只读铁律：加密数据不可修改，只能删除或新增
echo ============================================

REM ---- 「隐藏命令行窗口」开关：写 data\hide_cmd.on ----
set "FLAG="
if defined EXAM_DPP_HOME if exist "%EXAM_DPP_HOME%\hide_cmd.on" set "FLAG=%EXAM_DPP_HOME%\hide_cmd.on"
if not defined FLAG if exist "%H%dpp_home.txt" (
    set "CH="
    set /p CH=<"%H%dpp_home.txt"
    if defined CH if exist "%CH%\hide_cmd.on" set "FLAG=%CH%\hide_cmd.on"
)
if not defined FLAG if exist "%H%data\hide_cmd.on" set "FLAG=%H%data\hide_cmd.on"

REM pythonw 路径必须在**块外**算好：写进下面 if (...) 块里的 %VAR% 会在整块解析时
REM 就展开成空值（cmd 经典坑），start 会拿到空程序名，结果"什么也没启动"。
for %%d in ("%PY%") do set "PYW=%%~dpdpythonw.exe"
if exist "%PYW%" goto :have_pyw
set "PYW=%PY%"
echo [提示] 未找到 pythonw.exe，「隐藏命令行窗口」将退化为普通启动（会显示控制台）。
:have_pyw

if defined FLAG (
    REM pythonw 下没有控制台：界面自身会弹错误框
    echo 已按「隐藏命令行窗口」设置启动（不显示控制台）：%PYW%
    echo 需要停界面：双击「停止服务.bat」。
    start "" "%PYW%" "%H%gui.py"
    exit /b 0
)

"%PY%" "%H%gui.py"
set "RC=%ERRORLEVEL%"
echo.
echo 界面已退出，退出码：%RC%
pause
'''

# ================================================================ 停止服务.bat
stop = TITLE + r'''
echo 正在关闭 05 考生数据保护桌面管理界面（gui.py）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*gui.py*' -and $_.CommandLine -like '*05_*' } | ForEach-Object { Write-Host ('已关闭界面进程 ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo 完成。
pause
'''

# ================================================================ 隐藏命令行窗口开关.bat
toggle = TITLE + r'''
REM 在「显示命令行窗口」与「隐藏命令行窗口」之间切换（写/删 data\hide_cmd.on）
set "D=%H%data"
if defined EXAM_DPP_HOME set "D=%EXAM_DPP_HOME%"
if exist "%H%dpp_home.txt" (
    set "CH="
    set /p CH=<"%H%dpp_home.txt"
    if defined CH set "D=%CH%"
)
if not exist "%D%" mkdir "%D%"
if exist "%D%\hide_cmd.on" (
    del "%D%\hide_cmd.on"
    echo 已切换为：显示命令行窗口（下次启动可见黑窗口）
) else (
    REM 开关文件里写个 1 就行。不要用 echo on 重定向：那是 ECHO ON 命令，
    REM 只会留下空文件并把命令回显打开；也不要写 echo 1 加重定向（会被当成重定向句柄 1）。
    REM 重定向符号写在命令前面最稳。（注释里也别出现尖括号，块内可能被当成重定向。）
    > "%D%\hide_cmd.on" echo 1
    echo 已切换为：隐藏命令行窗口（下次启动用 pythonw，无黑窗口）
)
pause
'''


# ================================================================ 运行自检与测试.bat
selftest = TITLE + r'''
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
''' + FIND_PY_CRYPTO + r'''
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
echo  账户：owner/EDY_POWERQ（所有者）、dpo1/dpo1（数据保护负责人）、
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
'''


# ================================================================ 修复密钥环.bat
repair = TITLE + r'''
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
echo  修复完成：双击「启动服务.bat」即可用 owner / EDY_POWERQ 登录
echo ============================================
pause
'''


if __name__ == "__main__":
    w("安装依赖.bat", install)
    w("启动服务.bat", start)
    w("停止服务.bat", stop)
    w("切换命令行窗口.bat", toggle)
    w("运行自检与测试.bat", selftest)
    w("修复密钥环.bat", repair)
    print("done")
