#!/bin/bash
# 小车上线后执行：把回退版巡线程序部署上去
CAR=172.19.12.66
SRC=/home/wheeltec/Documents/deepseek-harness/default-workspace/tools/line_follow_white.py
if ! ping -c 1 -W 2 $CAR >/dev/null 2>&1; then echo "小车离线，稍后再试"; exit 1; fi
scp "$SRC" wheeltec@$CAR:/home/wheeltec/patrol_robot/tools/ && \
ssh wheeltec@$CAR 'cd ~/patrol_robot && python -m py_compile tools/line_follow_white.py && echo "部署完成，编译通过"'
