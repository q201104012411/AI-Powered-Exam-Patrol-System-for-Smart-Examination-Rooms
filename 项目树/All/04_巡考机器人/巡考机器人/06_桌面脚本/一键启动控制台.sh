#!/bin/bash
# ============================================================
#  巡考机器人 · 一键启动控制台
#  双击后：检查小车 → 启动所有服务 → 自动打开浏览器
# ============================================================
CAR=172.19.12.66
U=wheeltec
B="http://$CAR:8080/"

echo "=============================================="
echo "  巡考机器人 · 控制台一键启动"
echo "  目标小车: $CAR"
echo "=============================================="

# ---------- 1) 连通性 ----------
echo "[1/3] 检查小车连通性..."
if ! ping -c 1 -W 2 $CAR >/dev/null 2>&1; then
    echo "  ✗ 小车离线（检查是否开机 / WiFi 是否连 303WIFI）"
    echo
    read -p "按回车关闭..."
    exit 1
fi
echo "  ✓ 小车在线"

# ---------- 2) 远端拉起服务 ----------
echo "[2/3] 启动小车上的服务..."
ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no $U@$CAR 'bash -s' <<'REMOTE'
source /opt/ros/melodic/setup.bash 2>/dev/null
source ~/wheeltec_robot/devel/setup.bash 2>/dev/null
export ROS_MASTER_URI=http://localhost:11311
export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')
A=turn_on_wheeltec_robot

# ROS master
if ! timeout 3 rosnode list >/dev/null 2>&1; then
    echo "  · 启动 ROS master"
    setsid nohup roscore > /tmp/rc.log 2>&1 < /dev/null &
    for i in $(seq 1 12); do sleep 1; ss -ltn | grep -q 11311 && break; done
fi
echo "  ✓ ROS master"

# 底盘
if ! timeout 4 rostopic list 2>/dev/null | grep -q "^/odom$"; then
    echo "  · 启动底盘驱动"
    setsid bash -c "source /opt/ros/melodic/setup.bash; source ~/wheeltec_robot/devel/setup.bash; export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=$ROS_HOSTNAME; exec roslaunch $A ${A}.launch" > /tmp/chassis.log 2>&1 < /dev/null &
    for i in $(seq 1 25); do sleep 1; rostopic list 2>/dev/null | grep -q "^/odom$" && break; done
fi
echo "  ✓ 底盘（/odom）"

# Astra 相机
if ! timeout 4 rostopic list 2>/dev/null | grep -q "^/camera/rgb/image_raw$"; then
    echo "  · 启动 Astra 相机"
    setsid bash -c "source /opt/ros/melodic/setup.bash; source ~/wheeltec_robot/devel/setup.bash; export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=$ROS_HOSTNAME; exec roslaunch $A wheeltec_camera.launch" > /tmp/astra.log 2>&1 < /dev/null &
    sleep 15
fi
echo "  ✓ Astra 相机"

# USB 广角相机（MJPEG）
if ! timeout 4 rostopic list 2>/dev/null | grep -q "^/usb_cam/image_raw$"; then
    echo "  · 启动 USB 相机（MJPEG）"
    setsid bash -c "source /opt/ros/melodic/setup.bash; source ~/wheeltec_robot/devel/setup.bash; export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=$ROS_HOSTNAME; exec rosrun usb_cam usb_cam_node _video_device:=/dev/video0 _image_width:=800 _image_height:=480 _pixel_format:=mjpeg _framerate:=30" > /tmp/usbcam.log 2>&1 < /dev/null &
    sleep 6
fi
echo "  ✓ USB 相机"

# 快照服务
if ! pgrep -f "snapshot_serve[r]" >/dev/null; then
    echo "  · 启动快照服务"
    setsid bash -c "source /opt/ros/melodic/setup.bash; source ~/wheeltec_robot/devel/setup.bash; export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=$ROS_HOSTNAME; cd ~/patrol_robot && exec python -u tools/snapshot_server.py --fps 10" > /tmp/snap.log 2>&1 < /dev/null &
    sleep 4
fi
echo "  ✓ 快照服务（三路画面）"

# 网页
if ! ss -ltn 2>/dev/null | grep -q ":8080"; then
    echo "  · 启动控制网页"
    setsid bash -c "source /opt/ros/melodic/setup.bash; source ~/wheeltec_robot/devel/setup.bash; export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=$ROS_HOSTNAME; cd ~/patrol_robot/webui && exec python3 robot_web.py" > /tmp/webui.log 2>&1 < /dev/null &
    sleep 6
fi
echo "  ✓ 控制网页 8080"
REMOTE

if [ $? -ne 0 ]; then
    echo "  ✗ SSH 执行失败"
    read -p "按回车关闭..."; exit 1
fi

# ---------- 3) 打开浏览器 ----------
echo "[3/3] 打开控制台网页..."
sleep 1
firefox --new-window "$B" >/dev/null 2>&1 &
sleep 3
echo
echo "=============================================="
echo "  ✓ 完成！网页地址：$B"
echo "=============================================="
sleep 2
