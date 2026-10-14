@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM ---- 挑一个真正装了 opencv 的解释器（与项目其它 .bat 一致的做法）----
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python") do (
    if not defined PY for /f "delims=" %%p in ('%%~C -c "import cv2,sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
)
if not defined PY (
    echo [错误] 找不到具备 opencv 的 Python，请先运行「安装环境.bat」/「启动前自检.bat」
    pause
    exit /b 1
)

REM ---- 也支持"带参数直接执行"（脚本化、或用不方便按键时）----
REM     多模态检测.bat check / pull [模型名] / local / api / off / prompt / image <图片路径>
if not "%~1"=="" set "ONESHOT=1"
if /i "%~1"=="check"  goto check
if /i "%~1"=="pull"   goto pull
if /i "%~1"=="local"  goto local
if /i "%~1"=="api"    goto api
if /i "%~1"=="image"  goto image
if /i "%~1"=="off"    goto off
if /i "%~1"=="prompt" goto prompt
if /i "%~1"=="0"      exit /b 0

:menu
cls
echo ============================================================
echo   多模态大模型（VLM）检测 · 设置与验证
echo ============================================================
echo   1) 检查配置与连通性（含"本机有哪些模型"）
echo   2) 拉取视觉模型（ollama pull，需联网几 GB）
echo   3) 写入设置：本机 Ollama（默认 qwen2.5vl:3b）
echo   4) 写入设置：云端 API（通义千问 VL / GLM-4V 等）
echo   5) 用一张图片验证（输入图片路径，看框画得准不准）
echo   6) 关闭多模态（退回纯 YOLO11n，等于原版）
echo   7) 打印发给模型的提示词
echo   0) 退出
echo.
REM 用 choice 读键：chcp 之后 set /p 在部分系统上读不到输入，choice 不受影响
choice /c 12345670 /n /m "请选择（按数字键即可）："
set "RC=%errorlevel%"
if "%RC%"=="1" goto check
if "%RC%"=="2" goto pull
if "%RC%"=="3" goto local
if "%RC%"=="4" goto api
if "%RC%"=="5" goto image
if "%RC%"=="6" goto off
if "%RC%"=="7" goto prompt
if "%RC%"=="8" exit /b 0
echo.
echo [提示] 没读到按键。可以带参数直接运行，例如：
echo        多模态检测.bat check
echo        多模态检测.bat image data\captures\考场1.jpg
pause
exit /b 1

:check
"%PY%" 工具_多模态检测.py --check
if defined ONESHOT exit /b 0
pause
goto menu

:pull
set "M=%~2"
if not defined M set /p M=模型名（直接回车 = qwen2.5vl:3b）：
if not defined M set "M=qwen2.5vl:3b"
"%PY%" 工具_多模态检测.py --pull %M%
if defined ONESHOT exit /b 0
pause
goto menu

:local
set "M=%~2"
if not defined M set /p M=模型名（直接回车 = qwen2.5vl:3b）：
if not defined M set "M=qwen2.5vl:3b"
"%PY%" 工具_多模态检测.py --provider ollama --model "%M%" --write-settings
"%PY%" 工具_多模态检测.py --check
if defined ONESHOT exit /b 0
pause
goto menu

:api
set "BU="
set "MDL="
set "KEY="
set /p BU=接口地址（例 https://dashscope.aliyuncs.com/compatible-mode/v1）：
set /p MDL=模型名（例 qwen-vl-max）：
set /p KEY=API Key：
if not defined BU echo [跳过] 没填接口地址。& pause & goto menu
"%PY%" 工具_多模态检测.py --provider api --model "%MDL%" --base-url "%BU%" --api-key "%KEY%" --write-settings
"%PY%" 工具_多模态检测.py --check
if defined ONESHOT exit /b 0
pause
goto menu

:image
set "IMG=%~2"
if not defined IMG set /p IMG=图片路径（可把图片拖进本窗口）：
if not defined IMG echo [跳过] 没给图片路径。& pause & goto menu
set IMG=%IMG:"=%
"%PY%" 工具_多模态检测.py --image "%IMG%"
if defined ONESHOT exit /b 0
pause
goto menu

:off
"%PY%" 工具_多模态检测.py --provider off --write-settings
echo 已关闭多模态：界面里回到纯 YOLO11n（不影响原有识别）。
if defined ONESHOT exit /b 0
pause
goto menu

:prompt
"%PY%" 工具_多模态检测.py --prompt
if defined ONESHOT exit /b 0
pause
goto menu
