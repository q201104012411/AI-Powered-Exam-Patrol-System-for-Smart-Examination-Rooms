#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
patrol_move_ros1.py —— 巡考/巡更机器人移动脚本（Wheeltec ROS1 Melodic 小车专用）

适配的真车（172.19.12.55 / Jetson + ROS Melodic + wheeltec_robot 工作空间）：
  · 底盘话题   /cmd_vel (geometry_msgs/Twist)   ← 驱动 wheeltec_robot.cpp 直接转发给 STM32
  · 里程计     /odom    (nav_msgs/Odometry)     闭环反馈
  · 雷达       /scan    (sensor_msgs/LaserScan) 避障（本车 lsn10，雷达倒装 180°）
  · 电池       /PowerVoltage (std_msgs/Float32) 巡更报告里记录电压
  · 车型       car_mode = senior_akm → 阿克曼前轮转向：不能原地转圈、不能横移，
               所以 turn 用"边走边转"的圆弧执行（航向闭环），turn_rev 为倒车转向
  · Python     同时兼容 python2.7 / python3（Melodic 的 rospy 是 py2.7）

设计依据：桌面《巡考机器人.md》
  · 定时巡更：按路线自动巡逻，巡更点位打卡留痕   -> --route + checkpoint
  · 遇人即停 / 防碰撞急停                     -> 前方扇形激光避障（wait/abort/ignore）
  · 巡查报告：轨迹、事件清单、完成率             -> JSON 巡更记录
  · 定位到位精度 ≤10cm                        -> /odom 闭环 + 梯形加减速（实测 ≈1~2cm）

------------------------------------------------------------------
一、上车运行（在小车本机终端里）
------------------------------------------------------------------
# 0) 修好 ROS Master 地址（小车 ~/.bashrc 里写的是旧 IP 172.16.238.247，连不上）
#    每个终端都执行，或直接把 ~/.bashrc 里那两行改成 localhost：
export ROS_MASTER_URI=http://localhost:11311
export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')

# 1) 启动底盘（终端 A）
ros2 不适用（这是 ROS1）：
roslaunch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch
# 需要避障时再开雷达（若 turn_on_wheeltec_robot.launch 未包含雷达）
roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch

# 2) 跑脚本（终端 B）
python patrol_move_ros1.py --route route_patrol.yaml            # 按路线巡更
python patrol_move_ros1.py --route route_patrol.yaml --loop 0   # 无限循环巡更
python patrol_move_ros1.py --forward 0.5 --turn 90 --dwell 3     # 快速调试
python patrol_move_ros1.py --route route_patrol.yaml --dry-run    # 只预演不动车
python patrol_move_ros1.py --scan-check                          # 雷达朝向自检（不发动车）

------------------------------------------------------------------
二、路线文件格式（YAML）—— 阿克曼车型请按圆弧设计转弯
------------------------------------------------------------------
route_name: "考点夜间巡更 A 线"
default_speed: 0.20         # m/s   直行速度
default_turn_speed: 0.60    # rad/s 差速/全向车型的原地转向速度
akm_turn_speed: 0.12        # m/s   阿克曼车型转弯时的前进速度
steps:
  - {forward: 3.00, name: "楼道直行"}
  - {turn: -90, name: "右转进连廊"}        # 阿克曼=右转圆弧（约需要前方 1m 净空）
  - {turn_rev: -90, name: "倒车右转"}      # 阿克曼=倒车圆弧（三点调头用）
  - {checkpoint: 8.0, name: "保密室门口"}  # 停留 8s 并打卡留痕
  - {wait: 2.0}                            # 原地等待

------------------------------------------------------------------
三、安全机制
------------------------------------------------------------------
1. 速度限幅 + 梯形加减速
2. 里程计看门狗：/odom 超时 -> 立即停车并中止
3. 激光避障：前方扇形距离过小 -> 停车等待（wait / abort / ignore）
4. 单步超时保护
5. Ctrl+C、SIGTERM、异常、正常结束 -> 都强制补发零速度
   （注意：底盘驱动节点没有 cmd_vel 看门狗，只是把速度转发给 STM32，
     所以"最后一条零速度"必须由本脚本发出；请勿用 kill -9）
"""

from __future__ import print_function

import argparse
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime

IMU_OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imu_offset.json")

MOTION_KEYS = ("forward", "back", "turn", "turn_rev", "strafe")
DWELL_KEYS = ("dwell", "wait", "stop")
CTRL_HZ = 20.0

# ----------------------------------------------------------------------------
# python2.7 / python3 文本兼容层
#   py2：外部读进来的 YAML/文件内容是 unicode，若直接与源码里的中文(utf-8 str)
#        做 % 拼接会抛 UnicodeDecodeError；统一转成 utf-8 str。
#   报告输出时再统一转 unicode，用 ensure_ascii=False 写 utf-8，保证中文可读。
# ----------------------------------------------------------------------------
PY2 = sys.version_info[0] == 2
if PY2:
    STRING_TYPES = (str, unicode)          # noqa: F821  (py2)
else:
    STRING_TYPES = (str,)


def _tostr(s):
    """统一转成"本地 str"：py2 是 utf-8 bytes，py3 是 str。"""
    if PY2 and isinstance(s, unicode):      # noqa: F821
        return s.encode("utf-8")
    if not isinstance(s, str):
        return str(s)
    return s


def _tounicode(s):
    """统一转成 unicode，用于 JSON 输出（中文直读）。"""
    if PY2 and isinstance(s, str):
        return s.decode("utf-8", "replace")
    if not PY2 and not isinstance(s, str):
        return str(s)
    return s


def _map_obj(obj, fn, convert_keys=True):
    if isinstance(obj, dict):
        return dict((fn(k) if convert_keys else k, _map_obj(v, fn, convert_keys))
                    for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_map_obj(v, fn, convert_keys) for v in obj]
    if isinstance(obj, STRING_TYPES):
        return fn(obj)
    return obj


def _tostr_obj(obj):
    return _map_obj(obj, _tostr)


def _tounicode_obj(obj):
    return _map_obj(obj, _tounicode)


# ----------------------------------------------------------------------------
# 小工具（兼容 python2.7）
# ----------------------------------------------------------------------------
def is_finite(x):
    try:
        return not (math.isnan(x) or math.isinf(x))
    except AttributeError:          # 理论上不会发生
        return True


def wrap_pi(a):
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def yaw_from_quaternion(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class PatrolAbort(Exception):
    """需要中止整条巡更路线（安全原因）。"""


# ----------------------------------------------------------------------------
# 命令行参数
# ----------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="巡考/巡更机器人移动脚本（Wheeltec ROS1 Melodic，/cmd_vel + /odom 闭环）")
    g = p.add_argument_group("任务")
    g.add_argument("--route", metavar="FILE.yaml", help="巡更路线文件（YAML/JSON）")
    g.add_argument("--demo", action="store_true", help="跑一段内置安全演示动作")
    g.add_argument("--loop", type=int, default=1, metavar="N",
                   help="路线重复次数，0=无限循环（默认 1）")
    g.add_argument("--interval", type=float, default=0.0, metavar="SEC",
                   help="每轮之间的间隔秒数（默认 0）")
    g.add_argument("--dry-run", action="store_true", help="预演：不发布速度，只算路线与耗时")
    g.add_argument("--scan-check", action="store_true",
                   help="雷达朝向自检：分扇区打印最近距离，确认哪边是车头（不发动车）")

    g = p.add_argument_group("快速单步调试")
    g.add_argument("--forward", type=float, metavar="M", help="前进多少米")
    g.add_argument("--back", type=float, metavar="M", help="后退多少米")
    g.add_argument("--turn", type=float, metavar="DEG", help="转向多少度（+左/-右）")
    g.add_argument("--turn-rev", type=float, metavar="DEG", help="倒车转向多少度（阿克曼调头用）")
    g.add_argument("--strafe", type=float, metavar="M", help="横移多少米（仅麦轮/全向）")
    g.add_argument("--dwell", type=float, default=0.0, metavar="SEC", help="结束前停留秒数")
    g.add_argument("--checkpoint-name", default="", metavar="NAME", help="单步模式的打卡点位名")

    g = p.add_argument_group("车型与速度")
    g.add_argument("--car-mode", default="auto", choices=("auto", "akm", "diff", "omni"),
                   help="底盘类型：auto=读小车 launch 里的 car_mode（默认 auto，本车为 senior_akm）")
    g.add_argument("--speed", type=float, default=0.20, metavar="MPS", help="直行速度 m/s（默认 0.2）")
    g.add_argument("--turn-speed", type=float, default=0.60, metavar="RPS",
                   help="差速/全向原地转向角速度 rad/s（默认 0.6）")
    g.add_argument("--akm-turn-speed", type=float, default=0.12, metavar="MPS",
                   help="阿克曼转弯时的前进速度 m/s（默认 0.12，越慢圆弧越小越安全）")
    g.add_argument("--max-linear", type=float, default=0.40, metavar="MPS", help="线速度硬上限")
    g.add_argument("--max-angular", type=float, default=1.00, metavar="RPS", help="角速度硬上限")
    g.add_argument("--accel", type=float, default=0.30, metavar="MPS2", help="线加速度（默认 0.3）")
    g.add_argument("--ang-accel", type=float, default=1.00, metavar="RPS2", help="角加速度（默认 1.0）")
    g.add_argument("--dist-tol", type=float, default=0.02, metavar="M", help="到位距离容差（默认 0.02m）")
    g.add_argument("--angle-tol", type=float, default=2.0, metavar="DEG", help="到位角度容差（默认 2°）")
    g.add_argument("--step-timeout", type=float, default=0.0, metavar="SEC",
                   help="单步超时，0=按理论耗时自动计算×4+10s")
    g.add_argument("--akm-yaw-gain", type=float, default=1.20,
                   help="阿克曼转弯航向 P 控制器增益（默认 1.2）")
    g.add_argument("--yaw-source", choices=("imu", "odom"), default="imu",
                   help="转向用的航向反馈源：imu=陀螺角增量（默认，实测 odom 高估约 15%%）；odom=里程计")
    g.add_argument("--imu-calibrate", action="store_true",
                   help="零位校准：车停平后执行，把当前姿态记为 0°，存到 imu_offset.json")
    g.add_argument("--imu-pitch-offset", type=float, default=None, metavar="DEG",
                   help="直接指定 pitch 零位（度），覆盖校准文件")
    g.add_argument("--imu-roll-offset", type=float, default=None, metavar="DEG",
                   help="直接指定 roll 零位（度），覆盖校准文件")
    g.add_argument("--progress-timeout", type=float, default=8.0, metavar="SEC",
                   help="推进受阻看门狗：这么多秒还没完成 30%% 目标 → 停车报错（默认 8s，0=关闭）")
    g.add_argument("--progress-min-ratio", type=float, default=0.30,
                   help="推进受阻判据：完成比例低于此值（默认 0.30）")
    g.add_argument("--slip-check", dest="slip_check", action="store_true", default=True,
                   help="打滑/被卡检测（默认开启）：odom 在走但雷达场景几乎不变 → 立即停车")
    g.add_argument("--no-slip-check", dest="slip_check", action="store_false",
                   help="关闭打滑检测")
    g.add_argument("--slip-min-move", type=float, default=0.25, metavar="M",
                   help="走够这么多米才开始判打滑（默认 0.25）")
    g.add_argument("--slip-threshold", type=float, default=0.03, metavar="M",
                   help="雷达场景平均变化小于此值视为没动（默认 0.03）")
    g.add_argument("--slip-count", type=int, default=5,
                   help="连续多少次判定为打滑（默认 5 次 ≈ 0.5s）")
    g.add_argument("--max-tilt", type=float, default=3.0, metavar="DEG",
                   help="车体倾角上限（默认 3°，平地实测约 0.6°）。超过则拒绝移动——"
                        "压在网线/障碍物上或地面不平时保护；0=关闭")
    g.add_argument("--heading-hold", dest="heading_hold", action="store_true", default=True,
                   help="直行时用小幅度打方向修正航向漂移（默认开启，阿克曼车实测直行会跑偏）")
    g.add_argument("--no-heading-hold", dest="heading_hold", action="store_false",
                   help="关闭直行航向修正")
    g.add_argument("--heading-gain", type=float, default=0.80,
                   help="航向修正增益（rad/s per rad，默认 0.8）")
    g.add_argument("--heading-max-yaw", type=float, default=0.25, metavar="RPS",
                   help="航向修正的最大角速度（默认 0.25 rad/s）")
    g.add_argument("--heading-deadband", type=float, default=1.0, metavar="DEG",
                   help="航向死区（默认 1.0°，避免抖动）")
    g.add_argument("--yaw-sign", type=float, default=1.0, choices=(1.0, -1.0),
                   help="偏航方向符号：万一实车左转变右转（角速度方向相反），改成 -1（默认 1）")

    g = p.add_argument_group("话题")
    g.add_argument("--cmd-topic", default="/cmd_vel", metavar="TOPIC")
    g.add_argument("--odom-topic", default="/odom", metavar="TOPIC")
    g.add_argument("--scan-topic", default="/scan", metavar="TOPIC")
    g.add_argument("--voltage-topic", default="/PowerVoltage", metavar="TOPIC")
    g.add_argument("--scan", dest="use_scan", action="store_true", default=True,
                   help="启用激光避障（默认启用）")
    g.add_argument("--require-scan", dest="require_scan", action="store_true", default=True,
                   help="雷达没数据/中途断开就拒绝移动（默认开启，fail-closed）")
    g.add_argument("--allow-no-scan", dest="require_scan", action="store_false",
                   help="允许在没有雷达数据的情况下继续跑（危险，仅调试）")
    g.add_argument("--no-scan", dest="use_scan", action="store_false", help="关闭激光避障")

    g = p.add_argument_group("避障与安全")
    g.add_argument("--stop-distance", type=float, default=0.35, metavar="M",
                   help="前方障碍物停车距离（默认 0.35m）")
    g.add_argument("--scan-arc", type=float, default=35.0, metavar="DEG",
                   help="前方扇形半角（默认 ±35°）")
    g.add_argument("--scan-range-max", type=float, default=3.0, metavar="M",
                   help="避障只关心该距离内的数据（默认 3.0m）")
    g.add_argument("--scan-offset-deg", type=float, default=0.0, metavar="DEG",
                   help="雷达朝向补偿。**本车实测 /scan 与车体同向，用默认 0**；"
                        "若工具（--scan-check / tools/scan_align_check.py）判定倒装再填 180")
    g.add_argument("--obstacle-policy", choices=("wait", "abort", "ignore"), default="wait",
                   help="遇障碍：wait=停车等待清除；abort=中止；ignore=忽略（默认 wait）")
    g.add_argument("--obstacle-timeout", type=float, default=20.0, metavar="SEC",
                   help="等待障碍清除的最长时间，超时按 abort 处理")
    g.add_argument("--odom-timeout", type=float, default=0.5, metavar="SEC",
                   help="里程计看门狗超时（默认 0.5s）")

    g = p.add_argument_group("报告")
    g.add_argument("--report", metavar="FILE.json",
                   help="巡更记录输出路径（默认 ~/patrol_reports/patrol_*.json）")
    g.add_argument("--no-report", action="store_true", help="不写巡更记录文件")
    g.add_argument("--skip-master-check", action="store_true",
                   help="跳过启动前的 ROS Master 连通性检查")
    return p


# ----------------------------------------------------------------------------
# 车型检测 & 路线解析
# ----------------------------------------------------------------------------
def detect_car_mode():
    cands = [
        os.path.expanduser("~/wheeltec_robot/src/turn_on_wheeltec_robot/launch/turn_on_wheeltec_robot.launch"),
        "/opt/ros/melodic/share/turn_on_wheeltec_robot/launch/turn_on_wheeltec_robot.launch",
    ]
    for path in cands:
        if not os.path.exists(path):
            continue
        try:
            with io.open(path, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except Exception:
            continue
        m = re.search(r'<arg\s+name="car_mode"\s+default="([^"]*)"', txt)
        if m and m.group(1):
            return _tostr(m.group(1))
    return ""


def resolve_car_mode(arg_mode):
    """返回 (归一化车型, 说明文本)"""
    raw = detect_car_mode()
    if arg_mode != "auto":
        return arg_mode, "命令行指定（检测到的 car_mode=%s）" % (raw or "未知")
    if not raw:
        return "akm", "未检测到 car_mode，按本车默认 akm 处理"
    low = raw.lower()
    if "akm" in low:
        return "akm", "由 launch 检测：car_mode=%s（阿克曼）" % raw
    if "mec" in low or "omni" in low:
        return "omni", "由 launch 检测：car_mode=%s（麦轮/全向）" % raw
    return "diff", "由 launch 检测：car_mode=%s（差速）" % raw


def _step(kind, **kw):
    s = {"type": kind, "name": "", "target": 0.0, "speed": None,
         "dwell": 0.0, "checkpoint": False, "reverse": False, "note": ""}
    s.update(kw)
    return s


def _master_reachable(uri, timeout=1.5):
    """检查 ROS Master 是否可连，避免 rospy.init_node 静默挂死。"""
    m = re.match(r"https?://([^:/]+):(\d+)", uri or "")
    if not m:
        return True
    host, port = m.group(1), int(m.group(2))
    try:
        import socket
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def _load_route_file(path):
    with io.open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml
        data = yaml.safe_load(text)
    except ImportError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("路线文件顶层必须是字典（route_name / steps）")
    return _tostr_obj(data)


def _checkpoint_info(item):
    cp = item.get("checkpoint", None)
    if cp is None:
        return False, 0.0
    if isinstance(cp, bool):
        return cp, 0.0
    if isinstance(cp, (int, float)):
        return True, float(cp)
    return bool(cp), 0.0


def parse_route(data, args, source=""):
    route_name = str(data.get("route_name") or data.get("name") or source or "巡更路线")
    d_speed = float(data.get("default_speed", args.speed))
    d_turn = float(data.get("default_turn_speed", args.turn_speed))
    raw = data.get("steps") or data.get("route") or data.get("waypoints") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError("路线文件里没有 steps 列表")

    if "akm_turn_speed" in data:
        args.akm_turn_speed = float(data["akm_turn_speed"])

    steps = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("steps[%d] 必须是字典，例如 {forward: 3.0}" % i)
        name = str(item.get("name", ""))
        note = str(item.get("note", ""))
        is_cp, cp_dwell = _checkpoint_info(item)
        has_cp_key = ("checkpoint" in item and item.get("checkpoint") is not None
                      and item.get("checkpoint") is not False)
        dwell = max(float(item.get("dwell", 0.0) or 0.0), cp_dwell)
        for k in DWELL_KEYS:
            if k in item:
                dwell = float(item[k] or 0.0)
                break
        speed = float(item.get("speed", 0.0) or 0.0)
        reverse = bool(item.get("reverse", False))

        motion = next((k for k in MOTION_KEYS if k in item), None)
        if motion is None:
            if dwell > 0.0 or is_cp or any(k in item for k in DWELL_KEYS):
                steps.append(_step("dwell", name=name or "点位停留", dwell=dwell,
                                   checkpoint=has_cp_key, note=note))
                continue
            print("[warn] steps[%d] 无法识别，已跳过：%s" % (i, sorted(item.keys())))
            continue

        target = float(item[motion])
        if abs(target) < 1e-9:
            continue
        sp = (speed or d_turn) if "turn" in motion else (speed or d_speed)
        steps.append(_step(motion, target=target, speed=sp, name=name,
                           reverse=reverse, note=note))
        if has_cp_key or dwell > 0.0:
            steps.append(_step("dwell",
                               name=(name + " 打卡") if (name and has_cp_key) else (name or "到位停留"),
                               dwell=dwell, checkpoint=has_cp_key, note=note))
    if not steps:
        raise ValueError("路线解析后没有任何动作")
    return route_name, steps


def build_demo_steps(args):
    s, t = args.speed, args.turn_speed
    return "内置演示路线", [
        _step("forward", target=0.6, speed=s, name="直行 0.6m"),
        _step("turn", target=90.0, speed=t, name="左转 90°"),
        _step("forward", target=0.6, speed=s, name="直行 0.6m"),
        _step("turn_rev", target=-90.0, speed=t, name="倒车右转 90°（回正）"),
        _step("back", target=0.6, speed=s, name="后退 0.6m"),
        _step("dwell", dwell=2.0, checkpoint=True, name="演示打卡点"),
    ]


def build_cli_steps(args):
    steps = []
    if args.forward:
        steps.append(_step("forward", target=args.forward, speed=args.speed, name="前进"))
    if args.back:
        steps.append(_step("back", target=args.back, speed=args.speed, name="后退"))
    if args.strafe:
        steps.append(_step("strafe", target=args.strafe, speed=args.speed, name="横移"))
    if args.turn:
        steps.append(_step("turn", target=args.turn, speed=args.turn_speed, name="转向"))
    if args.turn_rev:
        steps.append(_step("turn_rev", target=args.turn_rev, speed=args.turn_speed, name="倒车转向"))
    if args.dwell or args.checkpoint_name:
        steps.append(_step("dwell", dwell=args.dwell,
                           checkpoint=bool(args.checkpoint_name or args.dwell),
                           name=args.checkpoint_name or "点位停留"))
    return ("快速单步移动", steps) if steps else (None, None)


def describe(step):
    t = step["type"]
    if t in ("turn", "turn_rev"):
        return "%s %.1f°" % ("倒车转向" if (t == "turn_rev" or step["reverse"]) else "转向",
                            step["target"])
    if t == "dwell":
        return "停留 %.1fs" % step["dwell"]
    cn = {"forward": "前进", "back": "后退", "strafe": "横移"}[t]
    return "%s %.2fm @ %.2fm/s" % (cn, step["target"], step["speed"])


# ----------------------------------------------------------------------------
# 主控制器
# ----------------------------------------------------------------------------
class PatrolMover(object):
    def __init__(self, args, route_name, steps, rospy, msg):
        self.args = args
        self.route_name = route_name
        self.steps = steps
        self.rospy = rospy
        self.msg = msg
        self.dry_run = bool(args.dry_run)
        self.car_mode, self.car_mode_src = resolve_car_mode(args.car_mode)
        self.imu_offset = (0.0, 0.0)

        self.v_max = min(abs(args.speed), abs(args.max_linear))
        self.w_max = min(abs(args.turn_speed), abs(args.max_angular))
        self.accel = abs(args.accel)
        self.ang_accel = abs(args.ang_accel)
        self.v_min = 0.03
        self.w_min = 0.10

        self.odom = None
        self.odom_t = 0.0
        self.scan_msg = None
        self.scan_t = 0.0
        self.voltage = None
        self.voltage_first = None
        self.imu = None
        self.imu_t = 0.0
        self.path_len = 0.0
        self._prev_xy = None
        self._prev_yaw = None
        self._yaw_accum = 0.0
        self._yaw_accum_imu = 0.0
        self._prev_yaw_imu = None
        self._fake = (0.0, 0.0, 0.0)
        self.aborted = False
        self.abort_reason = ""
        self.events = []
        self.checkpoints = []
        self.step_records = []
        self._obstacle_active = False
        self._terminate = False
        self.last_heading_err = 0.0
        self._slip_hits = 0

        self.cmd_pub = None
        if not self.dry_run:
            if not self.args.scan_check:      # 自检模式不创建速度发布者，绝不驱动底盘
                self.cmd_pub = rospy.Publisher(args.cmd_topic, msg.Twist, queue_size=10)
                rospy.Subscriber(args.odom_topic, msg.Odometry, self._on_odom, queue_size=20)
                if args.voltage_topic:
                    rospy.Subscriber(args.voltage_topic, msg.Float32, self._on_voltage, queue_size=5)
            rospy.Subscriber("/imu", msg.Imu, self._on_imu, queue_size=5)
            if args.use_scan:
                rospy.Subscriber(args.scan_topic, msg.LaserScan, self._on_scan, queue_size=5)

    # ---------------- 回调 ----------------
    def _on_odom(self, m):
        self.odom = m
        self.odom_t = time.time()
        p = m.pose.pose.position
        yaw = yaw_from_quaternion(m.pose.pose.orientation)
        if self._prev_xy is not None:
            self.path_len += math.hypot(p.x - self._prev_xy[0], p.y - self._prev_xy[1])
        self._prev_xy = (p.x, p.y)
        self._update_yaw_accum(yaw)

    def _on_scan(self, m):
        self.scan_msg = m
        self.scan_t = time.time()

    def load_imu_offset(self):
        """读取 IMU 零位（pitch, roll），命令行参数优先于校准文件。"""
        pitch = roll = 0.0
        try:
            with io.open(IMU_OFFSET_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            pitch = float(d.get("pitch_deg", 0.0))
            roll = float(d.get("roll_deg", 0.0))
        except Exception:
            pass
        if self.args.imu_pitch_offset is not None:
            pitch = float(self.args.imu_pitch_offset)
        if self.args.imu_roll_offset is not None:
            roll = float(self.args.imu_roll_offset)
        self.imu_offset = (pitch, roll)
        return self.imu_offset

    def _on_imu(self, m):
        self.imu = m
        self.imu_t = time.time()
        q = m.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        if self._prev_yaw_imu is None:
            self._prev_yaw_imu = yaw
        else:
            self._yaw_accum_imu += wrap_pi(yaw - self._prev_yaw_imu)
            self._prev_yaw_imu = yaw

    def reset_yaw_accum(self):
        """转向开始时把 odom / IMU 两套偏航累计都清零。"""
        p = self.pose()
        if p is not None:
            self._prev_yaw = p[2]
        self._yaw_accum = 0.0
        self._yaw_accum_imu = 0.0
        if self.imu is not None:
            q = self.imu.orientation
            self._prev_yaw_imu = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                            1.0 - 2.0 * (q.y * q.y + q.z * q.z))

    def yaw_progress(self):
        """当前转向已完成的角增量（按 --yaw-source 选择反馈源）。"""
        if self.args.yaw_source == "imu" and self.imu is not None \
                and (time.time() - self.imu_t) < 1.0:
            return self._yaw_accum_imu
        return self._yaw_accum

    def tilt_deg(self):
        """返回 (pitch, roll)，单位度；无数据时返回 None。"""
        if self.imu is None or (time.time() - self.imu_t) > 1.0:
            return None
        q = self.imu.orientation
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (q.w * q.y - q.z * q.x)))))
        roll = math.degrees(math.atan2(2.0 * (q.w * q.x + q.y * q.z),
                                       1.0 - 2.0 * (q.x * q.x + q.y * q.y)))
        pitch -= self.imu_offset[0]
        roll -= self.imu_offset[1]
        return pitch, roll

    def check_tilt(self, where=""):
        """倾角保护：整度超过 --max-tilt 就拒绝动作（防止压在障碍物上继续开）。"""
        if self.args.max_tilt <= 0 or self.dry_run:
            return
        t = self.tilt_deg()
        if t is None:
            return
        pitch, roll = t
        if abs(pitch) > self.args.max_tilt or abs(roll) > self.args.max_tilt:
            raise PatrolAbort("车体倾斜 pitch=%.1f° roll=%.1f°（上限 %.1f°）%s："
                              "可能压在障碍物上或地面不平，请先摆正再跑"
                              % (pitch, roll, self.args.max_tilt, where))

    def _on_voltage(self, m):
        self.voltage = float(m.data)
        if self.voltage_first is None:
            self.voltage_first = self.voltage

    def _update_yaw_accum(self, yaw):
        if self._prev_yaw is None:
            self._prev_yaw = yaw
            return
        self._yaw_accum += wrap_pi(yaw - self._prev_yaw)
        self._prev_yaw = yaw

    # ---------------- 基础 ----------------
    def publish(self, vx, vy, wz, dt=0.0):
        if self.dry_run:
            x, y, yaw = self._fake
            yaw = wrap_pi(yaw + wz * dt)
            x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
            y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
            self._fake = (x, y, yaw)
            self._update_yaw_accum(yaw)
            self.path_len += math.hypot(vx, vy) * dt
            return
        if self.cmd_pub is None:
            return
        tw = self.msg.Twist()
        tw.linear.x = float(vx)
        tw.linear.y = float(vy)
        tw.angular.z = float(wz) * self.args.yaw_sign
        self.cmd_pub.publish(tw)

    def stop(self, times=3):
        for _ in range(times):
            if self.dry_run:
                break
            self.publish(0.0, 0.0, 0.0)
            time.sleep(0.02)

    def pose(self):
        if self.dry_run:
            return self._fake
        if self.odom is None:
            return None
        if time.time() - self.odom_t > self.args.odom_timeout:
            return None
        p = self.odom.pose.pose.position
        return (p.x, p.y, yaw_from_quaternion(self.odom.pose.pose.orientation))

    def wait_for_odom(self, timeout=5.0):
        if self.dry_run:
            return True
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.pose() is not None:
                return True
            time.sleep(0.05)
        return False

    def install_signal_handlers(self):
        import signal

        def handler(signum, _frame):
            if self._terminate:
                return
            self._terminate = True
            print("\n[信号] 收到信号 %d，立即发送零速度并退出" % signum)  # py2 无 print(flush=)
            sys.stdout.flush()
            try:
                for _ in range(5):
                    self.publish(0.0, 0.0, 0.0)
            except Exception:
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except Exception:
                pass

    def check_terminate(self):
        if self._terminate:
            self.publish(0.0, 0.0, 0.0)
            raise PatrolAbort("收到终止信号（SIGINT/SIGTERM），已停车退出")
        if not self.dry_run and self.rospy.is_shutdown():
            raise PatrolAbort("ROS 已 shutdown，已停车退出")

    def check_master(self):
        """提醒 ROS_MASTER_URI 配置问题（小车 .bashrc 里可能是旧 IP）。"""
        uri = os.environ.get("ROS_MASTER_URI", "")
        m = re.match(r"http://([^:/]+):(\d+)", uri or "")
        if not m or self.dry_run:
            return
        host, port = m.group(1), int(m.group(2))
        if host in ("localhost", "127.0.0.1"):
            return
        try:
            import socket
            s = socket.socket()
            s.settimeout(1.0)
            s.connect((host, port))
            s.close()
        except Exception:
            print("[warn] ROS_MASTER_URI=%s 连不上。若 roscore 在本机，请先执行：\n"
                  "       export ROS_MASTER_URI=http://localhost:11311\n"
                  "       export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')" % uri)

    # ---------------- 雷达 ----------------
    def front_sector_stat(self):
        """返回避障扇区统计 (最近距离或None, 有效点数)。

        有效点数很重要：如果扇区里一个有效回波都没有，说明这个方向是盲区/被遮挡，
        避障等于失效（对着盲区看），必须报警而不是静默通过。
        """
        if self.dry_run or not self.args.use_scan:
            return None, 0
        if self.scan_msg is None or (time.time() - self.scan_t) > 1.0:
            return None, 0
        m = self.scan_msg
        off = math.radians(self.args.scan_offset_deg)
        arc = math.radians(self.args.scan_arc)
        rmax = self.args.scan_range_max
        best = float("inf")
        cnt = 0
        n = len(m.ranges)
        for i in range(n):
            r = m.ranges[i]
            if r is None or not is_finite(r):
                continue
            if r <= m.range_min + 1e-3 or r > min(m.range_max, rmax):
                continue
            a = wrap_pi(m.angle_min + i * m.angle_increment + off)
            if -arc <= a <= arc:
                cnt += 1
                if r < best:
                    best = r
        return (best if is_finite(best) else None), cnt

    def sector(self, lo_deg, hi_deg):
        """车体角区间内的最近距离（None=无有效回波）。"""
        if self.dry_run or self.scan_msg is None or (time.time() - self.scan_t) > 1.0:
            return None
        m = self.scan_msg
        off = math.radians(self.args.scan_offset_deg)
        best = float("inf")
        for i, r in enumerate(m.ranges):
            if r is None or not is_finite(r):
                continue
            if r <= m.range_min + 1e-3 or r > m.range_max:
                continue
            a = math.degrees(wrap_pi(m.angle_min + i * m.angle_increment + off))
            if lo_deg <= a <= hi_deg and r < best:
                best = r
        return best if is_finite(best) else None

    def scan_signature(self, bins=36, rmax=8.0):
        """把一帧 /scan 压成 bins 个"最小距离"指纹，用于判断车有没有真的动。"""
        if self.scan_msg is None or not self.fresh_scan():
            return None
        m = self.scan_msg
        sig = [rmax] * bins
        for i, r in enumerate(m.ranges):
            if r is None or not is_finite(r) or r <= m.range_min + 1e-3 or r > rmax:
                continue
            a = m.angle_min + i * m.angle_increment
            k = int((a + math.pi) / (2.0 * math.pi) * bins) % bins
            if r < sig[k]:
                sig[k] = r
        return sig

    def fresh_scan(self, timeout=1.0):
        return self.scan_msg is not None and (time.time() - self.scan_t) < timeout

    @staticmethod
    def sig_diff(a, b):
        if not a or not b or len(a) != len(b):
            return None
        return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a))

    def check_slip(self, sig0, moved, kind, extra=""):
        """moved=已完成的位移/角度；返回 True 表示判断为打滑/被卡。"""
        if not self.args.slip_check or sig0 is None or self.dry_run:
            return False
        min_move = 20.0 if kind == "转角" else self.args.slip_min_move
        if moved < min_move:
            return False
        sig1 = self.scan_signature()
        d = self.sig_diff(sig0, sig1)
        if d is None:
            return False
        if d < self.args.slip_threshold:
            self._slip_hits += 1
            if self._slip_hits >= self.args.slip_count:
                msg = ("疑似打滑/被卡住：odom 已%s %.2f%s，但雷达场景几乎没变（平均变化 %.3f m）%s"
                       % (kind, moved, "m" if kind == "位移" else "°", d, extra))
                self.events.append({"time": ts(), "type": "slip_suspected", "detail": msg})
                raise PatrolAbort(msg)
        else:
            self._slip_hits = 0
        return False

    def front_min_range(self):
        return self.front_sector_stat()[0]

    def sector_report(self):
        """--scan-check：按 30° 扇区打印最近距离，用于确认雷达朝向。"""
        if self.scan_msg is None:
            print("[自检] 还没收到 %s，请先确认雷达已启动" % self.args.scan_topic)
            return
        m = self.scan_msg
        sectors = 12
        arc = 2.0 * math.pi / sectors
        bests = [float("inf")] * sectors
        for i in range(len(m.ranges)):
            r = m.ranges[i]
            if r is None or not is_finite(r):
                continue
            if r <= m.range_min + 1e-3 or r > m.range_max:
                continue
            a = wrap_pi(m.angle_min + i * m.angle_increment)
            idx = int((a + math.pi) / arc) % sectors
            if r < bests[idx]:
                bests[idx] = r
        print("[自检] 雷达各扇区最近距离（雷达自身坐标系，0°=雷达正前方）：")
        for k in range(sectors):
            center = -180.0 + (k + 0.5) * (360.0 / sectors)
            v = bests[k]
            print("   %+7.1f° : %s" % (center, ("%.2f m" % v) if is_finite(v) else "无回波"))
        print("   -> 把纸箱放在车头前方 0.5m，看最近距离出现在哪个角度：")
        print("      出现在 0° 附近  -> 雷达与车体同向，用 --scan-offset-deg 0（本车实测即此）")
        print("      出现在 ±180° 附近 -> 雷达倒装，用 --scan-offset-deg 180")
        print("   更准的判定（用相机做交叉验证）：python tools/scan_align_check.py")

    def obstacle_guard(self):
        if self.dry_run or not self.args.use_scan or self.args.obstacle_policy == "ignore":
            return
        r = self.front_min_range()
        if r is None or r >= self.args.stop_distance:
            return
        detail = "前方 %.2fm 处检测到障碍/行人，已停车等待" % r
        if not self._obstacle_active:
            self._obstacle_active = True
            self.events.append({"time": ts(), "type": "obstacle", "detail": detail})
            print("[避障] %s" % detail)
        t0 = time.time()
        while True:
            self.publish(0.0, 0.0, 0.0)
            time.sleep(0.05)
            if self.args.obstacle_policy == "abort":
                raise PatrolAbort("避障策略=abort：前方障碍未清除")
            r = self.front_min_range()
            if r is None or r >= self.args.stop_distance:
                self._obstacle_active = False
                print("[避障] 前方已清空，继续巡更")
                return
            if time.time() - t0 > self.args.obstacle_timeout:
                self.events.append({"time": ts(), "type": "obstacle_timeout",
                                    "detail": "等待 %.0fs 仍未清除前方障碍" % self.args.obstacle_timeout})
                raise PatrolAbort("前方障碍超过 %.0fs 未清除" % self.args.obstacle_timeout)

    # ---------------- 动作 ----------------
    def _project(self, p0, p):
        dx, dy = p[0] - p0[0], p[1] - p0[1]
        fx = dx * math.cos(p0[2]) + dy * math.sin(p0[2])
        ly = -dx * math.sin(p0[2]) + dy * math.cos(p0[2])
        return fx, ly

    def move_linear(self, axis, target, speed, step_timeout):
        dirn = 1.0 if target > 0 else -1.0
        vmax = min(abs(speed), self.v_max)
        p0 = self.pose()
        if p0 is None:
            raise PatrolAbort("尚未收到 %s 数据，无法闭环移动（请先启动底盘 roslaunch）"
                              % self.args.odom_topic)
        target = abs(target)
        tol = max(self.args.dist_tol, 0.005)
        cmd = 0.0
        t0 = time.time()
        last = t0
        sig0 = self.scan_signature()
        self._slip_hits = 0
        while True:
            self.check_terminate()
            now = time.time()
            dt = max(1e-3, now - last)
            last = now

            p = self.pose()
            if p is None:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("%s 里程计超时（>%.1fs），已停车并中止任务"
                                  % (self.args.odom_topic, self.args.odom_timeout))
            fx, ly = self._project(p0, p)
            progress = (fx if axis == "x" else ly) * dirn
            remaining = target - progress
            if remaining <= tol:
                break
            if (self.args.progress_timeout > 0 and (now - t0) > self.args.progress_timeout
                    and progress < target * self.args.progress_min_ratio):
                self.publish(0.0, 0.0, 0.0, dt)
                msg = ("推进受阻：%.1fs 只走了 %.2fm / 目标 %.2fm（可能顶到障碍物或轮子打滑），已停车"
                       % (now - t0, progress, target))
                self.events.append({"time": ts(), "type": "blocked", "detail": msg})
                raise PatrolAbort(msg)
            if step_timeout > 0 and (now - t0) > step_timeout:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("单步超时（%.1fs，目标 %.2fm 实际 %.2fm），已停车"
                                  % (step_timeout, target, progress))

            if (self.args.use_scan and self.args.require_scan and not self.fresh_scan(1.0)):
                self.publish(0.0, 0.0, 0.0, dt)
                msg = "雷达数据中断（>1.0s），为保证安全已停车"
                self.events.append({"time": ts(), "type": "scan_lost", "detail": msg})
                raise PatrolAbort(msg)
            self.check_slip(sig0, progress, "位移")
            if dirn > 0:
                self.obstacle_guard()

            v_stop = math.sqrt(max(0.0, 2.0 * self.accel * remaining))
            v_t = min(vmax, max(self.v_min, v_stop))
            if v_stop < self.v_min and remaining > 4.0 * tol:
                v_t = self.v_min
            want = dirn * v_t
            cmd = min(want, cmd + self.accel * dt) if want > cmd else max(want, cmd - self.accel * dt)

            # 直行航向保持：阿克曼车直行会缓慢跑偏，用 odom 航向做小幅度打方向修正。
            # 只对"前进"生效——倒车时同样的打方向会产生反向偏航，符号容易搞反，故不启用。
            wz = 0.0
            if (self.args.heading_hold and axis == "x" and dirn > 0):
                err = wrap_pi(p0[2] - p[2])
                if abs(err) > math.radians(self.args.heading_deadband):
                    lim = abs(self.args.heading_max_yaw)
                    wz = max(-lim, min(lim, self.args.heading_gain * err))
                self.last_heading_err = err

            self.publish(cmd if axis == "x" else 0.0, cmd if axis == "y" else 0.0, wz, dt)
            time.sleep(1.0 / CTRL_HZ)

        self.publish(0.0, 0.0, 0.0, dt)
        p = self.pose() or p
        fx, ly = self._project(p0, p)
        if self.args.heading_hold and axis == "x" and dirn > 0:
            drift = math.degrees(wrap_pi(p0[2] - p[2]))
            print("      航向：结束偏差 %+.2f°%s" % (drift,
                  "（已启用直行航向修正）" if abs(drift) > self.args.heading_deadband else ""))
        return (fx if axis == "x" else ly) * dirn

    def move_turn_inplace(self, target_deg, speed, step_timeout):
        """差速/全向：原地旋转（阿克曼车型禁用）。"""
        dirn = 1.0 if target_deg > 0 else -1.0
        wmax = min(abs(speed), self.w_max)
        p = self.pose()
        if p is None:
            raise PatrolAbort("尚未收到 %s 数据，无法闭环转向" % self.args.odom_topic)
        self.reset_yaw_accum()
        sig0 = self.scan_signature()
        self._slip_hits = 0
        target = math.radians(abs(target_deg))
        tol = math.radians(max(self.args.angle_tol, 0.2))
        cmd = 0.0
        t0 = time.time()
        last = t0
        while True:
            self.check_terminate()
            now = time.time()
            dt = max(1e-3, now - last)
            last = now
            if not self.dry_run and (self.odom is None or
                                     time.time() - self.odom_t > self.args.odom_timeout):
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("%s 里程计超时（>%.1fs），已停车并中止任务"
                                  % (self.args.odom_topic, self.args.odom_timeout))
            progress = self.yaw_progress() * dirn
            remaining = target - progress
            if remaining <= tol:
                break
            if (self.args.progress_timeout > 0 and (now - t0) > self.args.progress_timeout
                    and abs(progress) < target * self.args.progress_min_ratio):
                self.publish(0.0, 0.0, 0.0, dt)
                msg = ("转向受阻：%.1fs 只转了 %.1f° / 目标 %.1f°（可能顶住障碍），已停车"
                       % (now - t0, math.degrees(progress), abs(target_deg)))
                self.events.append({"time": ts(), "type": "blocked", "detail": msg})
                raise PatrolAbort(msg)
            if step_timeout > 0 and (now - t0) > step_timeout:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("转向超时（%.1fs，目标 %.1f° 实际 %.1f°），已停车"
                                  % (step_timeout, abs(target_deg), math.degrees(progress)))

            w_stop = math.sqrt(max(0.0, 2.0 * self.ang_accel * remaining))
            w_t = min(wmax, max(self.w_min, w_stop))
            want = dirn * w_t
            cmd = min(want, cmd + self.ang_accel * dt) if want > cmd else max(want, cmd - self.ang_accel * dt)
            self.publish(0.0, 0.0, cmd, dt)
            time.sleep(1.0 / CTRL_HZ)

        self.publish(0.0, 0.0, 0.0, dt)
        return math.degrees(self.yaw_progress())

    def move_turn_akm(self, target_deg, speed, step_timeout, reverse=False):
        """阿克曼：边走边转的圆弧，航向用 P 控制闭环。

        angular.z 是偏航角速度（本车 teb 参数 cmd_angle_instead_rotvel=False），
        但阿克曼前轮转向在 v=0 时无法改变航向，所以必须同时给前进（或倒车）速度。
        """
        dirn = 1.0 if target_deg > 0 else -1.0
        v = min(abs(self.args.akm_turn_speed), self.v_max)
        kp = abs(self.args.akm_yaw_gain)
        p = self.pose()
        if p is None:
            raise PatrolAbort("尚未收到 %s 数据，无法闭环转向" % self.args.odom_topic)
        self.reset_yaw_accum()
        sig0 = self.scan_signature()
        self._slip_hits = 0
        target = math.radians(abs(target_deg))
        tol = math.radians(max(self.args.angle_tol, 0.2))
        slow = math.radians(12.0)        # 最后 12° 减速
        vdir = -1.0 if reverse else 1.0
        cmd_v = 0.0
        cmd_w = 0.0
        t0 = time.time()
        last = t0
        while True:
            self.check_terminate()
            now = time.time()
            dt = max(1e-3, now - last)
            last = now
            if not self.dry_run and (self.odom is None or
                                     time.time() - self.odom_t > self.args.odom_timeout):
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("%s 里程计超时（>%.1fs），已停车并中止任务"
                                  % (self.args.odom_topic, self.args.odom_timeout))

            progress = self.yaw_progress() * dirn
            remaining = target - progress
            if remaining <= tol:
                break
            if (self.args.progress_timeout > 0 and (now - t0) > self.args.progress_timeout
                    and abs(progress) < target * self.args.progress_min_ratio):
                self.publish(0.0, 0.0, 0.0, dt)
                msg = ("转向受阻：%.1fs 只转了 %.1f° / 目标 %.1f°（可能顶住障碍），已停车"
                       % (now - t0, math.degrees(progress), abs(target_deg)))
                self.events.append({"time": ts(), "type": "blocked", "detail": msg})
                raise PatrolAbort(msg)
            if step_timeout > 0 and (now - t0) > step_timeout:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("转向超时（%.1fs，目标 %.1f° 实际 %.1f°）：阿克曼车型需要"
                                  "前方/后方留有转弯空间，可减小 --akm-turn-speed 重试"
                                  % (step_timeout, abs(target_deg), math.degrees(progress)))

            if (self.args.use_scan and self.args.require_scan and not self.fresh_scan(1.0)):
                self.publish(0.0, 0.0, 0.0, dt)
                msg = "雷达数据中断（>1.0s），为保证安全已停车"
                self.events.append({"time": ts(), "type": "scan_lost", "detail": msg})
                raise PatrolAbort(msg)
            self.check_slip(sig0, abs(math.degrees(progress)), "转角")
            # 前方避障（只在前进转弯时判断）
            if vdir > 0:
                self.obstacle_guard()

            w_want = dirn * min(self.w_max, max(0.0, kp * remaining))
            if w_want > cmd_w:
                cmd_w = min(w_want, cmd_w + self.ang_accel * dt)
            else:
                cmd_w = max(w_want, cmd_w - self.ang_accel * dt)

            v_t = v if remaining > slow else max(0.05, v * remaining / slow)
            v_want = vdir * v_t
            if v_want > cmd_v:
                cmd_v = min(v_want, cmd_v + self.accel * dt)
            else:
                cmd_v = max(v_want, cmd_v - self.accel * dt)

            self.publish(cmd_v, 0.0, cmd_w, dt)
            time.sleep(1.0 / CTRL_HZ)

        self.publish(0.0, 0.0, 0.0, dt)
        return math.degrees(self.yaw_progress())

    def dwell(self, seconds, checkpoint, name):
        if seconds > 0:
            print("[%s] %s：原地停留 %.1fs" % ("打卡" if checkpoint else "停留", name, seconds))
        if checkpoint:
            p = self.pose()
            self.checkpoints.append({
                "name": name, "time": ts(), "dwell_s": round(seconds, 2),
                "odom_x": round(p[0], 3) if p else None,
                "odom_y": round(p[1], 3) if p else None,
                "voltage": self.voltage,
            })
        t0 = time.time()
        while time.time() - t0 < seconds:
            self.check_terminate()
            self.publish(0.0, 0.0, 0.0)
            time.sleep(0.05)

    # ---------------- 单步 ----------------
    def auto_timeout(self, step):
        if self.args.step_timeout > 0:
            return self.args.step_timeout
        t = step["type"]
        if t in ("turn", "turn_rev"):
            if self.car_mode == "akm":
                v = max(min(abs(self.args.akm_turn_speed), self.v_max), 0.03)
                # 圆弧路程 ≈ 2*R*θ，粗略按 1.2m 半径估；再给 2 倍余量
                arc_len = 1.2 * math.radians(abs(step["target"]))
                return arc_len / v * 3.0 + 15.0
            v = max(min(abs(step["speed"] or self.args.turn_speed), self.w_max), 0.05)
            return math.radians(abs(step["target"])) / v * 4.0 + 10.0
        if t in ("forward", "back", "strafe"):
            v = max(min(abs(step["speed"] or self.args.speed), self.v_max), 0.02)
            return abs(step["target"]) / v * 4.0 + 10.0
        return step["dwell"] + 10.0

    def run_step(self, idx, total, step):
        label = step["name"] or describe(step)
        print("\n[%d/%d] %s  (%s)" % (idx, total, label, describe(step)))
        t = step["type"]
        rec = {"index": idx, "name": label, "type": t, "target": step["target"],
               "unit": "deg" if t in ("turn", "turn_rev") else "m",
               "actual": None, "ok": False, "duration_s": None, "start": ts(),
               "note": step["note"]}
        t0 = time.time()
        timeout = self.auto_timeout(step)
        if t not in ("dwell",):
            self.check_tilt("（动作前）")
        try:
            if t == "forward":
                rec["actual"] = self.move_linear("x", step["target"], step["speed"], timeout)
            elif t == "back":
                rec["actual"] = self.move_linear("x", -abs(step["target"]), step["speed"], timeout)
            elif t == "strafe":
                if self.car_mode != "omni":
                    raise PatrolAbort("当前车型 %s 不支持横移（linear.y），请从路线里删除 strafe"
                                      % self.car_mode)
                rec["actual"] = self.move_linear("y", step["target"], step["speed"], timeout)
            elif t in ("turn", "turn_rev"):
                reverse = (t == "turn_rev") or bool(step.get("reverse"))
                if self.car_mode == "akm":
                    print("      阿克曼车型：按 %.2fm/s 的圆弧执行（%s），需要转弯空间"
                          % (min(abs(self.args.akm_turn_speed), self.v_max),
                             "倒车" if reverse else "前进"))
                    rec["actual"] = self.move_turn_akm(step["target"], step["speed"], timeout, reverse)
                else:
                    if reverse:
                        raise PatrolAbort("turn_rev 只用于阿克曼车型；差速/全向请用 turn")
                    rec["actual"] = self.move_turn_inplace(step["target"], step["speed"], timeout)
            elif t == "dwell":
                self.dwell(step["dwell"], step["checkpoint"], step["name"] or "点位停留")
                rec["actual"] = step["dwell"]
                rec["ok"] = True
            else:
                raise PatrolAbort("未知动作类型：%s" % t)
        finally:
            if t != "dwell":
                self.stop(times=2)
            rec["duration_s"] = round(time.time() - t0, 2)
            rec["end"] = ts()

        if rec["actual"] is not None and t != "dwell":
            tol = self.args.dist_tol if t not in ("turn", "turn_rev") else self.args.angle_tol
            rec["actual"] = round(rec["actual"], 3)
            rec["error"] = round(abs(rec["actual"]) - abs(step["target"]), 3)
            rec["ok"] = abs(rec["error"]) <= max(tol, 0.01) + 0.01
            print("      实际 %.3f%s，误差 %+.3f，耗时 %.1fs %s" %
                  (rec["actual"], "°" if t in ("turn", "turn_rev") else "m",
                   rec["error"], rec["duration_s"], "OK" if rec["ok"] else "偏差偏大"))
        elif t == "dwell":
            print("      停留完成，耗时 %.1fs" % rec["duration_s"])
        self.step_records.append(rec)
        return rec

    # ---------------- 主流程 ----------------
    def run_scan_check(self):
        print("=" * 66)
        print("雷达朝向自检（不会驱动底盘）")
        print("=" * 66)
        self.check_master()
        t0 = time.time()
        while time.time() - t0 < 8.0 and not self.rospy.is_shutdown():
            if self.scan_msg is not None:
                break
            time.sleep(0.1)
        self.sector_report()
        return {"result": "completed"}

    def run(self):
        n = len(self.steps)
        print("=" * 66)
        print("巡更路线：%s" % self.route_name)
        print("车型：%s（%s）" % (self.car_mode, self.car_mode_src))
        print("动作数：%d    循环：%s    模式：%s" %
              (n, "无限" if self.args.loop == 0 else self.args.loop,
               "预演(dry-run)" if self.dry_run else "实车运行"))
        if self.dry_run:
            print("!! DRY-RUN：不会发布 %s，仅验证路线与流程" % self.args.cmd_topic)
        if self.car_mode == "akm":
            print("提示：阿克曼车型不能原地转圈，转弯按圆弧执行；三点调头请用 turn_rev")
        if self.args.yaw_sign < 0:
            print("注意：已启用 --yaw-sign -1（角速度方向取反）")
        if self.args.use_scan and not self.dry_run:
            print("避障：%s 前方 ±%.0f° 内 <%.2fm 停车（策略 %s，雷达朝向补偿 %.0f°，"
                  "如方向不对请用 --scan-check 自检）"
                  % (self.args.scan_topic, self.args.scan_arc, self.args.stop_distance,
                     self.args.obstacle_policy, self.args.scan_offset_deg))
        self.load_imu_offset()
        if abs(self.imu_offset[0]) > 0.1 or abs(self.imu_offset[1]) > 0.1:
            print("IMU 零位已扣除：pitch %+.1f° / roll %+.1f°（来源 imu_offset.json 或命令行）"
                  % self.imu_offset)
        self.check_master()

        self.t_start = ts()
        self.t0 = time.time()
        loops_done = 0
        idx = 0
        try:
            if not self.wait_for_odom(5.0):
                raise PatrolAbort("5 秒内未收到 %s，请先启动底盘：roslaunch turn_on_wheeltec_robot "
                                  "turn_on_wheeltec_robot.launch" % self.args.odom_topic)
            if self.args.use_scan and not self.dry_run:
                time.sleep(0.3)
                if self.scan_msg is None:
                    msg = ("%s 无雷达数据：避障不可用，拒绝启动（先重启雷达："
                           "roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch；"
                           "确实要无雷达跑请加 --allow-no-scan）" % self.args.scan_topic)
                    self.events.append({"time": ts(), "type": "no_scan", "detail": msg})
                    if self.args.require_scan:
                        raise PatrolAbort(msg)
                    print("[warn] 未收到 %s 雷达数据，按 --allow-no-scan 继续（无避障）"
                          % self.args.scan_topic)
                else:
                    # 扇区有效性自检：补偿角填错/雷达被遮挡时该扇区会一个点都没有，
                    # 那样"避障"就是对着盲区看，必须显式报警而不是静默失效。
                    r_min, n_pts = self.front_sector_stat()
                    sector = "(车体 -%.0f°~+%.0f°)" % (self.args.scan_arc, self.args.scan_arc)
                    if n_pts < 3:
                        print("[warn] 避障扇区 %s 只有 %d 个有效回波 → 避障不可靠！"
                              % (sector, n_pts))
                        print("       补偿角当前为 %.0f°，请用 --scan-check（或 tools/scan_align_check.py）"
                              "确认正确值；若该方向本就是盲区，请检查雷达是否被遮挡。"
                              % self.args.scan_offset_deg)
                        self.events.append({"time": ts(), "type": "scan_sector_empty",
                                            "detail": "避障扇区 %s 仅 %d 个有效点（补偿角 %.0f°）"
                                                      % (sector, n_pts, self.args.scan_offset_deg)})
                    else:
                        print("[避障自检] 扇区 %s 有效回波 %d 个，最近 %s"
                              % (sector, n_pts,
                                 ("%.2f m" % r_min) if r_min is not None else "无"))
                        if r_min is not None and r_min < self.args.stop_distance:
                            print("       注意：当前最近距离 %.2f m 已小于停车阈值 %.2f m，"
                                  "起步就会被避障拦住" % (r_min, self.args.stop_distance))
            self.check_tilt("（启动时）")
            self.stop(times=2)

            while self.args.loop == 0 or loops_done < self.args.loop:
                loops_done += 1
                if self.args.loop != 1:
                    print("\n---------- 第 %d 轮巡更 ----------" % loops_done)
                for i, step in enumerate(self.steps, 1):
                    idx += 1
                    self.run_step(idx, n if self.args.loop > 0 else i, step)
                if self.args.interval > 0 and (self.args.loop == 0 or loops_done < self.args.loop):
                    print("\n[轮次间隔] 等待 %.1fs" % self.args.interval)
                    self.dwell(self.args.interval, False, "")
        except KeyboardInterrupt:
            self.aborted = True
            self.abort_reason = "用户 Ctrl+C 中断"
            self.events.append({"time": ts(), "type": "interrupt", "detail": self.abort_reason})
            print("\n[中断] 收到 Ctrl+C，已停车")
        except PatrolAbort as e:
            self.aborted = True
            self.abort_reason = str(e)
            self.events.append({"time": ts(), "type": "abort", "detail": self.abort_reason})
            print("\n[中止] %s" % self.abort_reason)
        finally:
            self.stop(times=5)

        return self.finish(loops_done)

    def finish(self, loops_done):
        dur = time.time() - self.t0
        done = [r for r in self.step_records if r["ok"]]
        total_planned = len(self.steps) * (self.args.loop if self.args.loop > 0 else max(1, loops_done))
        rate = 100.0 * len(done) / total_planned if total_planned else 0.0
        report = {
            "route_name": self.route_name,
            "robot": "Wheeltec ROS1 Melodic",
            "car_mode": self.car_mode,
            "car_mode_source": self.car_mode_src,
            "cmd_topic": self.args.cmd_topic,
            "odom_topic": self.args.odom_topic,
            "dry_run": self.dry_run,
            "start_time": self.t_start,
            "end_time": ts(),
            "duration_s": round(dur, 2),
            "total_distance_m": round(self.path_len, 3),
            "loops_requested": self.args.loop,
            "loops_done": loops_done,
            "battery_voltage_start": self.voltage_first,
            "battery_voltage_end": self.voltage,
            "steps": self.step_records,
            "checkpoints": self.checkpoints,
            "events": self.events,
            "steps_ok": len(done),
            "steps_total": total_planned,
            "task_completion_rate": round(rate, 1),
            "result": "aborted" if self.aborted else "completed",
            "abort_reason": self.abort_reason or None,
        }
        print("\n" + "=" * 66)
        print("巡更摘要：%s" % self.route_name)
        print("  结果        ：%s%s" % ("已完成" if not self.aborted else "已中止",
                                       ("（%s）" % self.abort_reason) if self.aborted else ""))
        print("  完成率      ：%.1f%% (%d/%d 个动作)" % (rate, len(done), total_planned))
        print("  总里程      ：%.2f m    总耗时：%.1f s" % (self.path_len, dur))
        print("  打卡点位    ：%d 个" % len(self.checkpoints))
        for c in self.checkpoints:
            print("      · %s  %s  停留 %.1fs" % (c["time"], c["name"], c["dwell_s"]))
        if self.events:
            print("  异常事件    ：%d 条" % len(self.events))
            for e in self.events:
                print("      · [%s] %s %s" % (e["type"], e["time"], e["detail"]))
        if self.voltage_first is not None:
            print("  电池电压    ：%.2f V -> %.2f V" % (self.voltage_first, self.voltage))
        path = None
        if not self.args.no_report:
            path = self.args.report
            if not path:
                path = os.path.join(os.path.expanduser("~/patrol_reports"),
                                    "patrol_%s.json" % stamp())
            d = os.path.dirname(os.path.abspath(path))
            if d and not os.path.isdir(d):
                os.makedirs(d)
            text = json.dumps(_tounicode_obj(report), ensure_ascii=False, indent=2)
            if not isinstance(text, bytes):
                text = text.encode("utf-8")
            with open(path, "wb") as f:
                f.write(text)
            print("  巡更记录    ：%s" % path)
        print("=" * 66)
        return report


# ----------------------------------------------------------------------------
def run_imu_calibration(args, seconds=4.0):
    """把车当前姿态记为 0°：采样若干帧姿态求均值，写入 imu_offset.json。"""
    try:
        import rospy
        from sensor_msgs.msg import Imu
    except ImportError:
        print("[错误] 无法导入 rospy，请先 source ROS1 环境")
        return 3
    uri = os.environ.get("ROS_MASTER_URI", "http://localhost:11311")
    if not _master_reachable(uri):
        print("[错误] 连不上 ROS Master（%s），请先启动底盘 roslaunch" % uri)
        return 3

    rospy.init_node("imu_calibrate", anonymous=True, disable_signals=True)
    print("IMU 零位校准：请保持小车静止、车轮都落地、水平停放，采样 %.0f 秒 ..." % seconds)
    samples = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            m = rospy.wait_for_message("/imu", Imu, timeout=2.0)
        except Exception:
            continue
        q = m.orientation
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (q.w * q.y - q.z * q.x)))))
        roll = math.degrees(math.atan2(2.0 * (q.w * q.x + q.y * q.z),
                                       1.0 - 2.0 * (q.x * q.x + q.y * q.y)))
        samples.append((pitch, roll))
    if len(samples) < 10:
        print("[错误] 只采到 %d 帧姿态，/imu 是否正常？" % len(samples))
        return 1

    pitch_off = sum(p for p, _ in samples) / len(samples)
    roll_off = sum(r for _, r in samples) / len(samples)
    dev = max(max(abs(p - pitch_off), abs(r - roll_off)) for p, r in samples)
    if dev > 1.5:
        print("[warn] 采样期间姿态波动 %.1f°（车可能在动/被碰），建议静止后重测" % dev)

    data = {"pitch_deg": round(pitch_off, 2), "roll_deg": round(roll_off, 2),
            "samples": len(samples), "spread_deg": round(dev, 2),
            "calibrated_at": ts()}
    text = json.dumps(_tounicode_obj(data), ensure_ascii=False, indent=2)
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    with open(IMU_OFFSET_FILE, "wb") as f:
        f.write(text)

    print("零位已保存：pitch %+.2f° / roll %+.2f°（%d 帧，波动 %.2f°）"
          % (pitch_off, roll_off, len(samples), dev))
    print("文件：%s" % IMU_OFFSET_FILE)
    print("之后运行会自动扣除；倾角保护比较的是相对于此零位的偏差。")
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.imu_calibrate:
        return run_imu_calibration(args)

    if args.route:
        data = _load_route_file(args.route)
        route_name, steps = parse_route(data, args, os.path.basename(args.route))
    elif args.demo:
        route_name, steps = build_demo_steps(args)
    elif args.scan_check:
        route_name, steps = "雷达自检", []
    else:
        route_name, steps = build_cli_steps(args)
        if not steps:
            parser.print_help()
            print("\n[提示] 至少要给一个动作，例如：--forward 0.5 --turn 90 "
                  "或 --route route_patrol.yaml")
            return 2

    try:
        import rospy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from sensor_msgs.msg import LaserScan, Imu
        from std_msgs.msg import Float32
    except ImportError:
        print("[错误] 无法导入 rospy，请先 source ROS1 环境：\n"
              "  source /opt/ros/melodic/setup.bash\n"
              "  source ~/wheeltec_robot/devel/setup.bash")
        return 3

    class _Msg(object):
        pass

    msg = _Msg()
    msg.Twist = Twist
    msg.Odometry = Odometry
    msg.LaserScan = LaserScan
    msg.Float32 = Float32
    msg.Imu = Imu

    # dry-run 不需要 ROS：不初始化节点，整车没启动也能先预演路线
    if args.dry_run:
        mover = PatrolMover(args, route_name, steps, rospy, msg)
        mover.install_signal_handlers()
        try:
            report = mover.run()
            return 0 if report["result"] == "completed" else 1
        except PatrolAbort as e:
            print("\n[中止] %s" % e)
            return 1

    # 真车运行前先体检 ROS Master，避免 rospy.init_node 连不上时静默挂死
    if not args.skip_master_check:
        uri = os.environ.get("ROS_MASTER_URI", "http://localhost:11311")
        if not _master_reachable(uri):
            print("[错误] 连不上 ROS Master：%s\n"
                  "  请先启动底盘： roslaunch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch\n"
                  "  若小车 ~/.bashrc 里 ROS_MASTER_URI 还是旧 IP，请在每个终端执行：\n"
                  "    export ROS_MASTER_URI=http://localhost:11311\n"
                  "    export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')\n"
                  "  （确认要跳过此检查可加 --skip-master-check）" % uri)
            return 3

    # disable_signals=True：自己接管 SIGINT/SIGTERM，保证退出前一定补发零速度
    rospy.init_node("patrol_mover", anonymous=False, disable_signals=True)
    mover = PatrolMover(args, route_name, steps, rospy, msg)
    mover.install_signal_handlers()

    code = 0
    try:
        if args.scan_check:
            mover.run_scan_check()
        else:
            report = mover.run()
            if report["result"] != "completed":
                code = 1
    except KeyboardInterrupt:
        print("\n[中断] 已停车退出")
        code = 130
    except PatrolAbort as e:
        print("\n[中止] %s" % e)
        code = 1
    except Exception as e:
        print("\n[错误] %s: %s" % (type(e).__name__, e))
        code = 1
    finally:
        try:
            mover.stop(times=5)
        except Exception:
            pass
        try:
            rospy.signal_shutdown("patrol finished")
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
