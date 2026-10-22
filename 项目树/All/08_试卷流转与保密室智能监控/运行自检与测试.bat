@echo off
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
set "H=%~dp0"
cd /d "%H%"

REM 整套自检含桌面端相关项（GUI 冒烟），所以优先选**带 tkinter** 的解释器；
REM 再依次退到能跑网页接口（flask）的、能跑取流（cv2,numpy）的、能跑就行。
set "PY="
call :probe_via_path "tkinter,cv2,numpy,flask"
call :probe_via_dir "tkinter,cv2,numpy,flask"
call :probe_via_path "cv2,numpy,flask"
call :probe_via_dir "cv2,numpy,flask"
call :probe_via_path "cv2,numpy"
call :probe_via_dir "cv2,numpy"
call :probe_via_path "sys"
call :probe_via_dir "sys"
if not defined PY (
    echo [错误] 找不到 Python 解释器
    echo 请先安装 Python 3.10 ~ 3.13，安装时勾选 "Add to PATH"
    pause
    exit /b 1
)
goto :probe_done

REM 在 PATH 里按候选名找（先试带 cv2 的，再退到只要能跑）
:probe_via_path
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import %~1,sys;print(sys.executable.strip())" 2^>nul') do set "PY=%%p"
)
exit /b 0

REM 兜底：扫常见安装位置里的 Python3xx（绕开 py launcher 缺失 / PATH 未配）
:probe_via_dir
for /d %%V in ("%LocalAppData%\Programs\Python\Python3*") do call :take_dir "%%~V" %~1
for /d %%V in ("%SystemDrive%\Python3*") do call :take_dir "%%~V" %~1
for /d %%V in ("%ProgramFiles%\Python3*") do call :take_dir "%%~V" %~1
exit /b 0

REM 取某个目录下的 python.exe（先确认能 import 目标模块）
:take_dir
if defined PY exit /b 0
if not exist "%~1\python.exe" exit /b 0
"%~1\python.exe" -c "import %~2" >nul 2>nul || exit /b 0
if not defined PY set "PY=%~1\python.exe"
exit /b 0

:probe_done
echo 使用解释器：%PY%

echo.
echo ============================================
echo  [1/5] 编译检查
echo ============================================
"%PY%" -m py_compile config.py models.py storage.py web_accounts.py alarm_hub.py camera_stream.py vault.py flow.py report.py gui.py main.py integration_api.py 网页监控面板/server/app.py
if errorlevel 1 ( echo 编译未通过 & pause & exit /b 1 )
echo 编译通过

echo.
echo ============================================
echo  [2/5] 单元测试（122 项，临时数据目录）
echo ============================================
"%PY%" -m unittest discover -s tests -t .
if errorlevel 1 ( echo. & echo 单元测试未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  [3/5] 端到端自检（114 项，临时数据目录）
echo ============================================
"%PY%" main.py
if errorlevel 1 ( echo. & echo 自检未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  [4/5] 网页面板接口实测（86 项，进程内起服务）
echo ============================================
"%PY%" tests\e2e_web_inproc.py
if errorlevel 1 ( echo. & echo 网页接口实测未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  [5/5] 与 00_集成联动 的联动自检（30 项，临时沙箱）
echo ============================================
"%PY%" tests\linkage_selfcheck.py
if errorlevel 1 ( echo. & echo 联动自检未通过 & pause & exit /b 1 )

echo.
echo ============================================
echo  全部通过：122 单元 + 114 自检 + 86 网页接口 + 30 联动。
echo  注意：自检用本地合成画面验证取流与判定逻辑；真实摄像头 / 门禁 /
echo        红外 / 电子锁 / 北斗定位 / 录像平台需现场联调后复测。
echo  下一步：
echo    · 双击「启动保密室监控界面.bat」用 owner / owner 登录桌面端
echo    · 或双击「启动网页版.bat」在浏览器打开 http://127.0.0.1:5020/
echo ============================================
pause
