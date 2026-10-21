#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# run_patrol.sh —— 巡考机器人"一键巡更"包装脚本（Wheeltec ROS1 Melodic / senior_akm）
#
# 它做四件事：
#   1) 兜底 ROS 环境（master/hostname 不依赖 ~/.bashrc 是否修好）
#   2) 起飞前自检：master、/odom、/scan、电池电压、车头前方净空
#   3) 自动套用安全低速默认参数（用户显式给的参数优先）
#   4) 运行 patrol_move_ros1.py 并把终端输出同时存到 ~/patrol_reports/run_*.log
#
# 用法：
#   ./run_patrol.sh --check                                  # 只自检，不动车
#   ./run_patrol.sh --forward 0.3                            # 单步前进（低速）
#   ./run_patrol.sh --route route_patrol_ros1.yaml           # 跑路线
#   ./run_patrol.sh --route route_patrol_ros1.yaml --speed 0.2 --loop 0
#   ./run_patrol.sh --route route_patrol_ros1.yaml --no-scan --dry-run
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/patrol_move_ros1.py"
ODOM_TOPIC="/odom"
SCAN_TOPIC="/scan"
VOLT_TOPIC="/PowerVoltage"
LOW_BATTERY="22.0"          # 低于此电压给出告警（24V 平台）
DEFAULT_SPEED="0.12"
DEFAULT_AKM_TURN_SPEED="0.08"
DEFAULT_STOP_DISTANCE="0.35"
SCAN_OFFSET_DEG="0"         # 本车实测 /scan 与车体同向；用 scan_align_check.py 复核

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_END=$'\033[0m'
else
  C_OK=''; C_WARN=''; C_ERR=''; C_END=''      # 输出重定向到日志时不要颜色转义
fi
ok()   { printf "%s  ✔ %s%s\n" "$C_OK"   "$1" "$C_END"; }
warn() { printf "%s  ! %s%s\n" "$C_WARN" "$1" "$C_END"; }
err()  { printf "%s  ✘ %s%s\n" "$C_ERR"  "$1" "$C_END"; }

usage() {
  cat <<'USAGE_END'
用法：
  ./run_patrol.sh --check                                  # 只自检，不动车
  ./run_patrol.sh --forward 0.3                            # 单步前进（低速）
  ./run_patrol.sh --route route_patrol_ros1.yaml           # 跑路线
  ./run_patrol.sh --route route_patrol_ros1.yaml --speed 0.2 --loop 0
  ./run_patrol.sh --route route_patrol_ros1.yaml --no-scan --dry-run

说明：
  默认套用安全低速参数：--speed 0.12 --akm-turn-speed 0.08 --stop-distance 0.35 --scan-offset-deg 180
  你自己显式传的同名参数会覆盖默认值。
  其余参数原样透传给 patrol_move_ros1.py（详见 README_移动脚本.md）。
USAGE_END
}

if [ ! -f "$PY_SCRIPT" ]; then
  err "找不到 ${PY_SCRIPT}"
  exit 3
fi

# ---------- 1) 解析参数：--check / --help + 默认值（先解析，--help 在非小车上也能用） ----------
CHECK_ONLY=0
USER_ARGS=()
has_speed=0; has_akm_speed=0; has_stop_dist=0; has_offset=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    --speed|--speed=*)        has_speed=1 ;;
    --akm-turn-speed|--akm-turn-speed=*) has_akm_speed=1 ;;
    --stop-distance|--stop-distance=*)   has_stop_dist=1 ;;
    --scan-offset-deg|--scan-offset-deg=*) has_offset=1 ;;
  esac
  USER_ARGS+=("$arg")
done

DEFAULTS=()
[ "$has_speed" -eq 0 ]      && DEFAULTS+=(--speed "$DEFAULT_SPEED")
[ "$has_akm_speed" -eq 0 ]  && DEFAULTS+=(--akm-turn-speed "$DEFAULT_AKM_TURN_SPEED")
[ "$has_stop_dist" -eq 0 ]  && DEFAULTS+=(--stop-distance "$DEFAULT_STOP_DISTANCE")
[ "$has_offset" -eq 0 ]     && DEFAULTS+=(--scan-offset-deg "$SCAN_OFFSET_DEG")

# ---------- 2) ROS 环境兜底 ----------
if [ -f /opt/ros/melodic/setup.bash ]; then
  # shellcheck disable=SC1091
  source /opt/ros/melodic/setup.bash
else
  err "未找到 /opt/ros/melodic/setup.bash（本脚本用于 ROS1 Melodic 小车）"
  exit 3
fi
if [ -f "$HOME/wheeltec_robot/devel/setup.bash" ]; then
  # shellcheck disable=SC1091
  source "$HOME/wheeltec_robot/devel/setup.bash"
fi

if [ -z "${ROS_MASTER_URI:-}" ] || [[ "${ROS_MASTER_URI}" == *"172.16.238.247"* ]]; then
  export ROS_MASTER_URI="http://localhost:11311"
fi
if [ -z "${ROS_HOSTNAME:-}" ] || [[ "${ROS_HOSTNAME}" == *"172.16.238.247"* ]]; then
  export ROS_HOSTNAME="$(hostname -I | awk '{print $1}')"
fi

echo "=============================================================="
echo "巡更一键启动  |  车型：senior_akm（阿克曼）"
echo "  ROS_MASTER_URI = ${ROS_MASTER_URI}"
echo "  ROS_HOSTNAME   = ${ROS_HOSTNAME}"
[ "$CHECK_ONLY" -eq 1 ] && echo "  模式           = 仅自检（不会驱动底盘）"
echo "=============================================================="

# ---------- 3) 起飞前自检 ----------
FAIL=0

has_topic() {  # $1=话题名
  timeout 6 rostopic list 2>/dev/null | grep -qx "$1"
}
topic_hz() {   # $1=话题名，输出平均频率（失败为空）
  timeout 8 rostopic hz -w 8 "$1" 2>/dev/null | awk '/average rate/{print $3; exit}'
}

echo "[1/5] ROS Master ..."
if timeout 6 rostopic list >/dev/null 2>&1; then
  ok "master 可连接（$(timeout 6 rostopic list 2>/dev/null | wc -l) 个话题）"
else
  err "连不上 master，请先启动底盘：roslaunch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch"
  FAIL=1
fi

echo "[2/5] 里程计 ${ODOM_TOPIC} ..."
if [ "$FAIL" -eq 0 ] && has_topic "$ODOM_TOPIC"; then
  HZ="$(topic_hz "$ODOM_TOPIC")"
  if [ -n "$HZ" ]; then ok "有数据，约 ${HZ} Hz"; else warn "话题存在但读不到频率"; fi
else
  err "没有 ${ODOM_TOPIC}：底盘 launch 起了吗？串口 /dev/wheeltec_controller 占用正常吗？"
  FAIL=1
fi

echo "[3/5] 雷达 ${SCAN_TOPIC} ..."
if [ "$FAIL" -eq 0 ] && has_topic "$SCAN_TOPIC"; then
  HZ="$(topic_hz "$SCAN_TOPIC")"
  if [ -n "$HZ" ]; then ok "有数据，约 ${HZ} Hz（避障可用）"; else warn "话题存在但读不到频率"; fi
else
  warn "没有 ${SCAN_TOPIC}：避障不可用（可加 --no-scan 通过），雷达需单独启动："
  warn "    roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch"
fi

echo "[4/5] 电池电压 ..."
if [ "$FAIL" -eq 0 ] && has_topic "$VOLT_TOPIC"; then
  V="$(timeout 6 rostopic echo -n1 "$VOLT_TOPIC" 2>/dev/null | awk '/data/{print $2; exit}')"
  if [ -n "$V" ]; then
    if awk "BEGIN{exit !($V < $LOW_BATTERY)}"; then
      warn "电池 ${V} V 偏低（< ${LOW_BATTERY} V），建议先充电，巡更可能中途断电"
    else
      ok "电池 ${V} V"
    fi
  else
    warn "读不到电压值"
  fi
else
  warn "没有 ${VOLT_TOPIC}（不影响运行，报告里电压为空）"
fi

echo "[5/5] 车头前方净空（±25°，雷达朝向补偿 ${SCAN_OFFSET_DEG}°）..."
if [ "$FAIL" -eq 0 ] && has_topic "$SCAN_TOPIC"; then
  STAT="$(timeout 20 python - "$SCAN_TOPIC" "$SCAN_OFFSET_DEG" <<'PYPROBE' 2>/dev/null
# -*- coding: utf-8 -*-
from __future__ import print_function
import math, sys
import rospy
from sensor_msgs.msg import LaserScan
topic, off_deg = sys.argv[1], float(sys.argv[2])
rospy.init_node("sector_probe", anonymous=True, disable_signals=True)
try:
    m = rospy.wait_for_message(topic, LaserScan, timeout=6)
except Exception:
    print("")
    sys.exit(0)
off = math.radians(off_deg)
arc = math.radians(25.0)
best = float("inf")
cnt = 0
for i, r in enumerate(m.ranges):
    if r != r or r == float("inf") or r <= m.range_min:
        continue
    a = m.angle_min + i * m.angle_increment + off
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    if -arc <= a <= arc:
        cnt += 1
        if r < best:
            best = r
print("%.2f %d" % (best, cnt) if best != float("inf") else "none %d" % cnt)
PYPROBE
)"
  MIN="$(echo "$STAT" | awk '{print $1}')"
  NPT="$(echo "$STAT" | awk '{print $2}')"
  case "$MIN" in
    "") warn "读不到雷达数据" ;;
    none)
      if [ "${NPT:-0}" -lt 3 ] 2>/dev/null; then
        warn "避障扇区（车体 ±25°）只有 ${NPT:-0} 个有效回波 → 避障不可靠！"
        warn "    补偿角当前 ${SCAN_OFFSET_DEG}°，请用 tools/scan_align_check.py 或 --scan-check 重新判定"
      else
        ok "扇区有效回波 ${NPT} 个，但都在量程外"
      fi ;;
    *)
      if [ "${NPT:-0}" -lt 3 ] 2>/dev/null; then
        warn "避障扇区（车体 ±25°）只有 ${NPT} 个有效回波（最近 ${MIN} m）→ 回波过少，避障可能不可靠"
      elif awk "BEGIN{exit !($MIN < $DEFAULT_STOP_DISTANCE)}"; then
        warn "前方 ${MIN} m（< 停车阈值 ${DEFAULT_STOP_DISTANCE} m）：底盘起步就会被避障拦住，请先移开障碍"
      else
        ok "前方净空 ${MIN} m（扇区有效回波 ${NPT} 个）"
      fi ;;
  esac
else
  warn "跳过（雷达不可用）"
fi

echo "[6/6] 车体姿态 ..."
if [ "$FAIL" -eq 0 ] && has_topic "/imu"; then
  TILT="$(SCRIPT_DIR="$SCRIPT_DIR" timeout 15 python - <<'PYTILT' 2>/dev/null
# -*- coding: utf-8 -*-
from __future__ import print_function
import json, math, os, rospy
from sensor_msgs.msg import Imu
rospy.init_node("tilt_probe", anonymous=True, disable_signals=True)
try:
    m = rospy.wait_for_message("/imu", Imu, timeout=6)
except Exception:
    print("")
    raise SystemExit
q = m.orientation
pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0*(q.w*q.y - q.z*q.x)))))
roll = math.degrees(math.atan2(2.0*(q.w*q.x + q.y*q.z), 1.0 - 2.0*(q.x*q.x + q.y*q.y)))
off_p = off_r = 0.0
try:
    path = os.path.join(os.environ.get("SCRIPT_DIR", "."), "imu_offset.json")
    d = json.load(open(path))
    off_p = float(d.get("pitch_deg", 0.0)); off_r = float(d.get("roll_deg", 0.0))
except Exception:
    pass
print("%.1f %.1f %.1f %.1f" % (pitch, roll, pitch - off_p, roll - off_r))
PYTILT
)"
  P0="$(echo "$TILT" | awk '{print $1}')"; R0="$(echo "$TILT" | awk '{print $2}')"
  P="$(echo "$TILT" | awk '{print $3}')";  R="$(echo "$TILT" | awk '{print $4}')"
  if [ -z "$P" ]; then
    warn "读不到姿态数据"
  elif awk "BEGIN{exit !(($P > 3) || ($P < -3) || ($R > 3) || ($R < -3))}"; then
    err "车体倾斜 pitch=${P}° roll=${R}°（>3°，已扣除 IMU 零位）：可能压在网线/障碍物上或地面不平，请先摆正"
    FAIL=1
  else
    ok "水平（pitch ${P}° / roll ${R}°，已扣除零位；原始 ${P0}° / ${R0}°）"
  fi
else
  warn "跳过（/imu 不可用）"
fi

echo "--------------------------------------------------------------"
if [ "$CHECK_ONLY" -eq 1 ]; then
  [ "$FAIL" -eq 0 ] && ok "自检完成：可以开始巡更" || err "自检未通过，请先解决上面的 ✘"
  exit "$FAIL"
fi
if [ "$FAIL" -ne 0 ]; then
  err "自检未通过，未启动巡更（修好后重试，或加 --check 只看自检）"
  exit "$FAIL"
fi

# ---------- 4) 运行 ----------
mkdir -p "$HOME/patrol_reports"
LOG="$HOME/patrol_reports/run_$(date +%Y%m%d_%H%M%S).log"
echo "安全默认参数：${DEFAULTS[*]:-（无）}"
echo "启动：python ${PY_SCRIPT##*/} ${DEFAULTS[*]} ${USER_ARGS[*]}"
echo "输出同时保存到：${LOG}"
echo "=============================================================="

cd "$SCRIPT_DIR" || exit 3
python "$PY_SCRIPT" "${DEFAULTS[@]}" "${USER_ARGS[@]}" 2>&1 | tee "$LOG"
RC=${PIPESTATUS[0]}
echo "--------------------------------------------------------------"
echo "退出码：${RC}    日志：${LOG}"
exit "$RC"
