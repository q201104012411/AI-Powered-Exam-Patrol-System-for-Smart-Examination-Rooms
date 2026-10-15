@echo off
set "H=%~dp0"
cd /d "%H%"

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
