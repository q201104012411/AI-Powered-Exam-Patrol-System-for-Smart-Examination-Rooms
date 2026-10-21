#!/bin/bash
# ============================================================
#  run_line_patrol.sh —— 白线绕圈巡线（一键启动）
#
#  默认行为：跟着地上的白线一直转圈（无限循环）
#  拐角动作：K-turn = 前进弧 45° + 倒车弧 45°（约 86°），净漂移 ~0.66m
#            因为本车最小转弯半径约 1m，而胶带弯只有 0.15~0.2m，跟不了弯
#
#  用法：
#    ./run_line_patrol.sh                 # 无限绕圈
#    ./run_line_patrol.sh --max-corners 4 # 只跑一圈（4 个弯）就停
#    ./run_line_patrol.sh --corner-dir right   # 顺时针的圈用 right
#    ./run_line_patrol.sh --speed 0.06    # 更慢
# ============================================================
set -u

WS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$WS_DIR/tools/line_follow_white.py"
LOG_DIR="$HOME/patrol_reports"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/line_$(date +%Y%m%d_%H%M%S).log"

if [ ! -f "$SCRIPT" ]; then
    echo "[错误] 找不到 $SCRIPT"
    exit 2
fi

source /opt/ros/melodic/setup.bash 2>/dev/null
if [ -f "$HOME/wheeltec_robot/devel/setup.bash" ]; then
    source "$HOME/wheeltec_robot/devel/setup.bash"
fi
export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311}
export ROS_HOSTNAME=${ROS_HOSTNAME:-$(hostname -I | awk '{print $1}')}
export ROS_IP=$ROS_HOSTNAME

echo "=============================================================="
echo " 白线绕圈巡线"
echo "--------------------------------------------------------------"
echo " 工作区 : $WS_DIR"
echo " 日志   : $LOG"
echo "--------------------------------------------------------------"

# --- 自检 1：ROS master ---
if ! timeout 3 rosnode list >/dev/null 2>&1; then
    echo "[错误] 连不上 ROS master（$ROS_MASTER_URI）"
    echo "       先启动底盘：roslaunch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch"
    exit 3
fi
echo "[1/4] ROS master 正常"

# --- 自检 2：相机 ---
if ! timeout 6 rostopic hz -w 3 /camera/rgb/image_raw 2>/dev/null | grep -q "average rate"; then
    echo "[错误] /camera/rgb/image_raw 没有图像"
    echo "       先启动相机：roslaunch turn_on_wheeltec_robot wheeltec_camera.launch"
    exit 4
fi
echo "[2/4] 相机图像正常（巡线必需；注意相机和雷达会抢 USB，巡线期间别开雷达）"

# --- 自检 3：里程计 ---
if ! timeout 6 rostopic echo -n1 /odom/pose/pose/position >/dev/null 2>&1; then
    echo "[错误] 没有 /odom（拐角 K-turn 需要闭环转向）"
    exit 5
fi
echo "[3/4] 里程计正常"

# --- 自检 4：白线可见性（用 dry-run 看白色占比） ---
COVER=$(timeout -s INT 8 python "$SCRIPT" --dry-run 2>&1 | tee /tmp/_line_selfcheck.log | grep -c "不在灰色地胶上" || true)
if [ "$COVER" != "0" ]; then
    echo "[错误] 地面白色占比过高：车不在灰色地胶上（或脚下是白瓷砖）"
    echo "       请把车摆到地胶上、车身压白线、车头顺线"
    exit 6
fi
echo "[4/4] 白线可见，场地正常"

echo "--------------------------------------------------------------"
echo " 参数：--line-color ${LINE_COLOR:-black} --speed 0.07 --kp 1.1 --corner-mode kturn"
echo "       --corner-speed 0.06 --reacquire-time 5 --max-corners 0(无限)"
echo " 提示：Ctrl+C 停车；本脚本退出时一定发零速"
echo "=============================================================="
echo

trap 'echo; echo "[退出] 发送停车指令"; fuser -k 8081/tcp 2>/dev/null; exit 0' INT TERM

python "$SCRIPT" \
    --line-color "${LINE_COLOR:-black}" \
    --speed "${LINE_SPEED:-0.07}" \
    --kp "${LINE_KP:-1.1}" \
    --corner-mode kturn \
    --corner-dir "${LINE_DIR:-left}" \
    --corner-speed "${LINE_CORNER_SPEED:-0.06}" \
    --corner-timeout 35 \
    --reacquire-time 5 \
    --max-corners "${LINE_MAX_CORNERS:-0}" \
    "$@" 2>&1 | tee "$LOG"

echo
echo "日志已保存：$LOG"
