@echo off
chcp 65001 >nul
set "H=%~dp0"
cd /d "%H%"

REM 不需要登录就能切换：隐藏/显示启动器（启动核查界面.bat / 启动核查网页版.bat）的黑窗口
REM 数据目录可被 EXAM_CAMERA_HOME 或 camera_home.txt 改到别处，优先用实际在用的那个
set "D=%H%data"
if defined EXAM_CAMERA_HOME set "D=%EXAM_CAMERA_HOME%"
if not defined EXAM_CAMERA_HOME if exist "%H%camera_home.txt" (
    set "CH="
    set /p CH=<"%H%camera_home.txt"
    if defined CH set "D=%CH%"
)
set "FLAG=%D%\hide_cmd.on"

echo ============================================
echo   命令行窗口：切换隐藏 / 显示
echo   标记文件：%FLAG%
echo ============================================
if exist "%FLAG%" (
    del "%FLAG%" >nul 2>nul
    if exist "%FLAG%" (
        echo  [失败] 标记文件删不掉，请检查权限或文件是否被占用。
    ) else (
        echo  已切换为：显示命令行窗口（下次用启动器启动会看到黑窗口，便于看报错）
    )
) else (
    if not exist "%D%" mkdir "%D%" >nul 2>nul
    >"%FLAG%" echo 1
    if exist "%FLAG%" (
        echo  已切换为：隐藏命令行窗口（下次用启动器启动零黑窗口，界面/后端改用 pythonw）
    ) else (
        echo  [失败] 标记文件写不进去，请检查目录权限：%D%
    )
)
echo.
pause
