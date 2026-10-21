#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patrol_move.py —— 巡考/巡更机器人移动脚本（Wheeltec ROS2 Humble 小车）

设计依据：桌面《巡考机器人.md》
  · 定时巡更：按路线自动巡逻，巡更点位打卡留痕      -> --route 路线文件 + checkpoint
  · 遇人即停 / 防碰撞急停                        -> 激光前方扇形避障（停车等待/中止）
  · 巡查报告：巡更轨迹、事件清单                    -> JSON 巡更记录（里程/打卡/事件/完成率）
  · 定位到位精度 ≤10cm                          -> /odom 里程计闭环 + 梯形加减速

它不依赖 Nav2：直接向底盘发布 /cmd_vel，用 /odom 做闭环，
所以只要底盘 launch 起来了就能跑（适合楼道、连廊这种直线+转角路线）。

------------------------------------------------------------------
一、上车运行
------------------------------------------------------------------
# 终端 A：启动底盘（必须）
ros2 launch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch.py
# 可选：同时打开雷达（需要避障时）
ros2 launch turn_on_wheeltec_robot wheeltec_lidar.launch.py

# 终端 B：跑脚本
python3 patrol_move.py --route route_patrol.yaml          # 按路线巡更
python3 patrol_move.py --route route_patrol.yaml --loop 0 # 无限循环巡更（Ctrl+C 停）
python3 patrol_move.py --forward 0.5 --turn 90 --dwell 3  # 快速单步调试
python3 patrol_move.py --route route_patrol.yaml --dry-run # 只预演，不动车

------------------------------------------------------------------
二、路线文件格式（YAML）
------------------------------------------------------------------
route_name: "考点夜间巡更 A 线"
default_speed: 0.20        # m/s  直行/横移默认速度
default_turn_speed: 0.60   # rad/s 转向默认速度
steps:
  - {forward: 3.0, name: "一楼楼道东段"}          # +前进 / back: 后退
  - {turn: 90,    name: "东端右转"}               # +逆时针(左转) / -顺时针(右转)，单位 度
  - {strafe: 0.5, name: "麦轮横移让位"}           # +向左 / -向右（仅麦轮/全向轮车型）
  - {checkpoint: 8.0, name: "试卷保密室门口"}      # 点位停留 8s 并打卡留痕
  - {forward: 2.0, speed: 0.15, checkpoint: true, dwell: 5.0, name: "保密室门前进点"}
  - {wait: 2.0}                                  # 原地等待

------------------------------------------------------------------
三、安全机制
------------------------------------------------------------------
1. 速度限幅 + 梯形加减速，起步/停止平滑
2. 里程计看门狗：/odom 超时未更新 -> 立即停车并中止任务
3. 激光避障：前方扇形内距离小于阈值 -> 停车等待（可 abort / ignore）
4. 单步超时保护；Ctrl+C、异常、任务结束 -> 强制发零速度
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
MOTION_KEYS = ("forward", "back", "turn", "strafe")
DWELL_KEYS = ("dwell", "wait", "stop")
CTRL_HZ = 20.0


# ----------------------------------------------------------------------------
# 命令行参数
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="巡考/巡更机器人移动脚本（Wheeltec ROS2 Humble，/cmd_vel + /odom 闭环）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("------------------------------------------------------------------")[1],
    )
    # 任务来源
    g = p.add_argument_group("任务")
    g.add_argument("--route", metavar="FILE.yaml", help="巡更路线文件（YAML/JSON）")
    g.add_argument("--demo", action="store_true", help="跑一段内置安全演示动作")
    g.add_argument("--loop", type=int, default=1, metavar="N",
                   help="路线重复次数，0=无限循环（默认 1）")
    g.add_argument("--interval", type=float, default=0.0, metavar="SEC",
                   help="每轮巡更之间的间隔秒数（默认 0）")
    g.add_argument("--dry-run", action="store_true", help="预演：不发布速度，只算路线与耗时")

    g = p.add_argument_group("快速单步调试（不需要路线文件）")
    g.add_argument("--forward", type=float, metavar="M", help="前进多少米")
    g.add_argument("--back", type=float, metavar="M", help="后退多少米")
    g.add_argument("--turn", type=float, metavar="DEG", help="转向多少度（+左转/-右转）")
    g.add_argument("--strafe", type=float, metavar="M", help="横移多少米（+左/-右，麦轮）")
    g.add_argument("--dwell", type=float, default=0.0, metavar="SEC", help="结束前原地停留秒数")
    g.add_argument("--checkpoint-name", default="", metavar="NAME", help="单步模式的打卡点位名")

    g = p.add_argument_group("速度与限制")
    g.add_argument("--speed", type=float, default=0.20, metavar="MPS", help="直行/横移速度 m/s（默认 0.2）")
    g.add_argument("--turn-speed", type=float, default=0.60, metavar="RPS", help="转向角速度 rad/s（默认 0.6）")
    g.add_argument("--max-linear", type=float, default=0.40, metavar="MPS", help="线速度硬上限（默认 0.4）")
    g.add_argument("--max-angular", type=float, default=1.20, metavar="RPS", help="角速度硬上限（默认 1.2）")
    g.add_argument("--accel", type=float, default=0.30, metavar="MPS2", help="线加速度（默认 0.3）")
    g.add_argument("--ang-accel", type=float, default=1.00, metavar="RPS2", help="角加速度（默认 1.0）")
    g.add_argument("--dist-tol", type=float, default=0.02, metavar="M", help="到位距离容差（默认 0.02）")
    g.add_argument("--angle-tol", type=float, default=2.0, metavar="DEG", help="到位角度容差（默认 2.0）")
    g.add_argument("--yaw-sign", type=float, default=1.0, choices=(1.0, -1.0),
                   help="偏航方向符号：实车转向与预期相反时改成 -1（默认 1）")
    g.add_argument("--step-timeout", type=float, default=0.0, metavar="SEC",
                   help="单步超时秒数，0=按理论耗时自动计算×4+10s")

    g = p.add_argument_group("话题")
    g.add_argument("--cmd-topic", default="/cmd_vel", metavar="TOPIC")
    g.add_argument("--odom-topic", default="/odom", metavar="TOPIC")
    g.add_argument("--scan-topic", default="/scan", metavar="TOPIC")
    g.add_argument("--scan", dest="use_scan", action="store_true", default=True,
                   help="启用激光避障（默认启用）")
    g.add_argument("--no-scan", dest="use_scan", action="store_false", help="关闭激光避障")
    g.add_argument("--voltage-topic", default="/PowerVoltage", metavar="TOPIC",
                   help="电池电压话题，用于巡更报告（默认 /PowerVoltage）")

    g = p.add_argument_group("避障与安全")
    g.add_argument("--stop-distance", type=float, default=0.35, metavar="M",
                   help="前方障碍物停车距离（默认 0.35m）")
    g.add_argument("--scan-arc", type=float, default=35.0, metavar="DEG",
                   help="前方扇形半角（默认 ±35°）")
    g.add_argument("--scan-range-max", type=float, default=3.0, metavar="M",
                   help="避障只关心该距离内的数据（默认 3.0m）")
    g.add_argument("--scan-offset-deg", type=float, default=0.0, metavar="DEG",
                   help="雷达安装朝向补偿，倒装雷达填 180（Wheeltec 常见）")
    g.add_argument("--obstacle-policy", choices=("wait", "abort", "ignore"), default="wait",
                   help="遇障碍：wait=停车等待清除后继续；abort=中止任务；ignore=忽略（默认 wait）")
    g.add_argument("--obstacle-timeout", type=float, default=20.0, metavar="SEC",
                   help="等待障碍清除的最长时间，超时按 abort 处理（默认 20s）")
    g.add_argument("--odom-timeout", type=float, default=0.5, metavar="SEC",
                   help="里程计看门狗超时（默认 0.5s）")

    g = p.add_argument_group("报告")
    g.add_argument("--report", metavar="FILE.json", help="巡更记录输出路径（默认 ~/patrol_reports/patrol_*.json）")
    g.add_argument("--no-report", action="store_true", help="不写巡更记录文件")
    return p


# ----------------------------------------------------------------------------
# 路线解析
# ----------------------------------------------------------------------------
def _step(kind, **kw):
    s = {"type": kind, "name": "", "target": 0.0, "speed": None,
         "dwell": 0.0, "checkpoint": False, "note": ""}
    s.update(kw)
    return s


def _load_route_file(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml  # ROS 环境自带 python3-yaml
        data = yaml.safe_load(text)
    except ImportError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("路线文件顶层必须是字典（route_name / steps）")
    return data


def _checkpoint_info(item):
    """解析 checkpoint 字段，返回 (是否打卡, 秒数)。

    支持三种写法：checkpoint: true / checkpoint: 8.0 / 仅用 dwell: 8.0
    """
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

    steps = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("steps[%d] 必须是字典，例如 {forward: 3.0}" % i)
        name = str(item.get("name", ""))
        note = str(item.get("note", ""))
        is_cp, cp_dwell = _checkpoint_info(item)
        has_cp_key = ("checkpoint" in item and
                      item.get("checkpoint") is not None and
                      item.get("checkpoint") is not False)
        dwell = max(float(item.get("dwell", 0.0) or 0.0), cp_dwell)
        for k in DWELL_KEYS:
            if k in item:
                dwell = float(item[k] or 0.0)
                break
        speed = float(item.get("speed", 0.0) or 0.0)

        motion = next((k for k in MOTION_KEYS if k in item), None)
        if motion is None:
            # 纯停留/打卡步骤：只有写了 checkpoint 才算"点位打卡"
            if dwell > 0.0 or is_cp or any(k in item for k in DWELL_KEYS):
                steps.append(_step("dwell", name=name or "点位停留", dwell=dwell,
                                   checkpoint=has_cp_key, note=note))
                continue
            print("[warn] steps[%d] 无法识别，已跳过：%s" % (i, sorted(item.keys())))
            continue

        target = float(item[motion])
        if abs(target) < 1e-9:
            continue
        if motion == "turn":
            sp = speed or d_turn
        else:
            sp = speed or d_speed
        steps.append(_step(motion, target=target, speed=sp, name=name, note=note))
        if has_cp_key or dwell > 0.0:
            steps.append(_step("dwell", name=(name + " 打卡") if (name and has_cp_key) else (name or "到位停留"),
                               dwell=dwell, checkpoint=has_cp_key, note=note))
    if not steps:
        raise ValueError("路线解析后没有任何动作")
    return route_name, steps


def build_demo_steps(args):
    """一段安全的内置演示：前进→左转→前进→右转→后退→打卡。"""
    s = args.speed
    t = args.turn_speed
    return "内置演示路线", [
        _step("forward", target=0.5, speed=s, name="直行 0.5m"),
        _step("turn", target=90.0, speed=t, name="左转 90°"),
        _step("forward", target=0.5, speed=s, name="直行 0.5m"),
        _step("turn", target=-90.0, speed=t, name="右转 90°（回正）"),
        _step("back", target=0.5, speed=s, name="后退 0.5m"),
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
    if args.dwell or args.checkpoint_name:
        steps.append(_step("dwell", dwell=args.dwell, checkpoint=bool(args.checkpoint_name or args.dwell),
                           name=args.checkpoint_name or "点位停留"))
    return ("快速单步移动", steps) if steps else (None, None)


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def wrap_pi(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def yaw_from_quaternion(q) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def describe(step) -> str:
    t = step["type"]
    if t == "turn":
        return "转向 %.1f° @ %.2frad/s" % (step["target"], step["speed"])
    if t == "dwell":
        return "停留 %.1fs" % step["dwell"]
    cn = {"forward": "前进", "back": "后退", "strafe": "横移"}[t]
    return "%s %.2fm @ %.2fm/s" % (cn, step["target"], step["speed"])


class PatrolAbort(Exception):
    """需要中止整条巡更路线（安全原因）。"""


# ----------------------------------------------------------------------------
# 主节点
# ----------------------------------------------------------------------------
class PatrolMover:
    def __init__(self, args, route_name, steps, rclpy_mod, node):
        self.args = args
        self.route_name = route_name
        self.steps = steps
        self.rclpy = rclpy_mod
        self.node = node
        self.dry_run = bool(args.dry_run)

        # 限幅
        self.v_max = min(abs(args.speed), abs(args.max_linear))
        self.w_max = min(abs(args.turn_speed), abs(args.max_angular))
        self.accel = abs(args.accel)
        self.ang_accel = abs(args.ang_accel)
        self.v_min = 0.03
        self.w_min = 0.10

        # 状态
        self.odom = None
        self.odom_t = 0.0
        self.scan_msg = None
        self.scan_t = 0.0
        self.voltage = None
        self.voltage_first = None
        self.path_len = 0.0
        self._prev_xy = None
        self._prev_yaw = None
        self._yaw_accum = 0.0
        self._fake = (0.0, 0.0, 0.0)   # dry-run 用的虚拟位姿
        self.aborted = False
        self.abort_reason = ""
        self.events = []
        self.checkpoints = []
        self.step_records = []
        self._obstacle_active = False
        self._terminate = False

        # 话题
        self.cmd_pub = None
        if not self.dry_run:
            from geometry_msgs.msg import Twist
            from nav_msgs.msg import Odometry
            from rclpy.qos import QoSProfile, ReliabilityPolicy
            from std_msgs.msg import Float32

            self._Twist = Twist
            self.cmd_pub = node.create_publisher(Twist, args.cmd_topic, 10)
            # 用 best-effort 订阅，兼容 reliable / best-effort 两种发布端
            qos = QoSProfile(depth=10)
            qos.reliability = ReliabilityPolicy.BEST_EFFORT
            node.create_subscription(Odometry, args.odom_topic, self._on_odom, qos)
            if args.use_scan:
                from sensor_msgs.msg import LaserScan
                node.create_subscription(LaserScan, args.scan_topic, self._on_scan,
                                         self._scan_qos())
            if args.voltage_topic:
                node.create_subscription(Float32, args.voltage_topic, self._on_voltage, 10)

    # ---------------- 回调 ----------------
    def _scan_qos(self):
        from rclpy.qos import qos_profile_sensor_data
        return qos_profile_sensor_data

    def _on_odom(self, msg):
        self.odom = msg
        self.odom_t = time.monotonic()
        p = msg.pose.pose.position
        yaw = yaw_from_quaternion(msg.pose.pose.orientation)
        if self._prev_xy is not None:
            self.path_len += math.hypot(p.x - self._prev_xy[0], p.y - self._prev_xy[1])
        self._prev_xy = (p.x, p.y)
        self._update_yaw_accum(yaw)

    def _on_scan(self, msg):
        self.scan_msg = msg
        self.scan_t = time.monotonic()

    def _on_voltage(self, msg):
        self.voltage = float(msg.data)
        if self.voltage_first is None:
            self.voltage_first = self.voltage

    def _update_yaw_accum(self, yaw):
        if self._prev_yaw is None:
            self._prev_yaw = yaw
            return
        self._yaw_accum += wrap_pi(yaw - self._prev_yaw)
        self._prev_yaw = yaw

    # ---------------- 基础操作 ----------------
    def spin(self, duration=0.0):
        end = time.monotonic() + duration
        while True:
            self.rclpy.spin_once(self.node, timeout_sec=0.0)
            if time.monotonic() >= end:
                return
            time.sleep(min(0.01, max(0.0, end - time.monotonic())))

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
        msg = self._Twist()
        msg.linear.x = float(vx)
        msg.linear.y = float(vy)
        msg.angular.z = float(wz) * self.args.yaw_sign
        self.cmd_pub.publish(msg)

    def stop(self, times=3):
        for _ in range(times):
            if self.dry_run:
                break
            self.publish(0.0, 0.0, 0.0)
            self.spin(0.02)

    def install_signal_handlers(self):
        """接管 SIGINT/SIGTERM：先发零速度，再优雅退出。

        底盘 ROS 节点没有 cmd_vel 看门狗（只是把速度转发给 STM32），
        所以脚本被 Ctrl+C / timeout / kill 时必须自己把速度清零。
        rclpy 会默认在 SIGTERM 时直接 shutdown 上下文，导致最后一条
        零速度发不出去，因此这里用 SignalHandlerOptions.NO 关掉 rclpy 的
        信号处理，改由本函数负责。
        """
        import signal

        def handler(signum, _frame):
            if self._terminate:      # 第二次收到信号：不再等待
                return
            self._terminate = True
            print("\n[信号] 收到信号 %d，立即发送零速度并退出" % signum, flush=True)
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
        """在控制循环里调用：外部要求退出 / ROS 上下文被关闭 -> 安全停车。"""
        if self._terminate:
            self.publish(0.0, 0.0, 0.0)
            raise PatrolAbort("收到终止信号（SIGINT/SIGTERM），已停车退出")
        if not self.dry_run and not self.rclpy.ok():
            raise PatrolAbort("ROS 上下文已被外部关闭，已停车退出")

    def pose(self):
        """返回 (x, y, yaw)；无有效里程计时返回 None。"""
        if self.dry_run:
            return self._fake
        if self.odom is None:
            return None
        if time.monotonic() - self.odom_t > self.args.odom_timeout:
            return None
        p = self.odom.pose.pose.position
        return (p.x, p.y, yaw_from_quaternion(self.odom.pose.pose.orientation))

    def wait_for_odom(self, timeout=3.0):
        if self.dry_run:
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.spin(0.05)
            if self.pose() is not None:
                return True
        return False

    # ---------------- 避障 ----------------
    def front_min_range(self):
        if self.dry_run or not self.args.use_scan:
            return None
        if self.scan_msg is None or (time.monotonic() - self.scan_t) > 1.0:
            return None  # 雷达没数据时不拦截（fail-open），仅在报告中提示
        msg = self.scan_msg
        off = math.radians(self.args.scan_offset_deg)
        arc = math.radians(self.args.scan_arc)
        rmax = self.args.scan_range_max
        best = float("inf")
        n = len(msg.ranges)
        for i in range(n):
            r = msg.ranges[i]
            if r is None or not math.isfinite(r):
                continue
            if r <= msg.range_min + 1e-3 or r > min(msg.range_max, rmax):
                continue
            a = wrap_pi(msg.angle_min + i * msg.angle_increment + off)
            if -arc <= a <= arc and r < best:
                best = r
        return best if math.isfinite(best) else None

    def obstacle_guard(self):
        """前方有障碍/行人时停车等待；返回时确保可以继续前进。"""
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
        t0 = time.monotonic()
        while True:
            self.publish(0.0, 0.0, 0.0)
            self.spin(0.05)
            if self.args.obstacle_policy == "abort":
                raise PatrolAbort("避障策略=abort：前方障碍未清除")
            r = self.front_min_range()
            if r is None or r >= self.args.stop_distance:
                self._obstacle_active = False
                print("[避障] 前方已清空，继续巡更")
                return
            if time.monotonic() - t0 > self.args.obstacle_timeout:
                self.events.append({"time": ts(), "type": "obstacle_timeout",
                                    "detail": "等待 %.0fs 仍未清除前方障碍" % self.args.obstacle_timeout})
                raise PatrolAbort("前方障碍超过 %.0fs 未清除" % self.args.obstacle_timeout)

    # ---------------- 动作 ----------------
    def move_linear(self, axis, target, speed, step_timeout):
        """axis: 'x' 前进/后退（linear.x）；'y' 横移（linear.y，需麦轮/全向轮）。"""
        dirn = 1.0 if target > 0 else -1.0
        vmax = min(abs(speed), self.v_max)
        if abs(speed) > self.v_max:
            print("[warn] 速度 %.2f 超过上限，已限幅到 %.2f" % (abs(speed), self.v_max))
        p0 = self.pose()
        if p0 is None:
            raise PatrolAbort("尚未收到 %s 数据，无法闭环移动（请先启动底盘 launch）" %
                              self.args.odom_topic)
        x0, y0, yaw0 = p0
        target = abs(target)
        tol = max(self.args.dist_tol, 0.005)
        cmd = 0.0
        t0 = time.monotonic()
        last = t0
        while True:
            self.rclpy.spin_once(self.node, timeout_sec=0.0)
            self.check_terminate()
            now = time.monotonic()
            dt = max(1e-3, now - last)
            last = now

            p = self.pose()
            if p is None:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("%s 里程计超时（>%.1fs），已停车并中止任务" %
                                  (self.args.odom_topic, self.args.odom_timeout))
            dx, dy = p[0] - x0, p[1] - y0
            if axis == "x":
                proj = dx * math.cos(yaw0) + dy * math.sin(yaw0)
            else:
                proj = -dx * math.sin(yaw0) + dy * math.cos(yaw0)
            progress = proj * dirn
            remaining = target - progress
            if remaining <= tol:
                break
            if step_timeout > 0 and (now - t0) > step_timeout:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("单步超时（%.1fs，目标 %.2fm 实际 %.2fm），已停车" %
                                  (step_timeout, target, progress))

            if dirn > 0:  # 只在前进/向前横移时做前方避障
                self.obstacle_guard()

            # 梯形减速：剩余距离决定目标速度
            v_stop = math.sqrt(max(0.0, 2.0 * self.accel * remaining))
            v_t = min(vmax, max(self.v_min, v_stop))
            if v_stop < self.v_min and remaining > 4.0 * tol:
                v_t = self.v_min
            want = dirn * v_t
            if want > cmd:
                cmd = min(want, cmd + self.accel * dt)
            else:
                cmd = max(want, cmd - self.accel * dt)

            vx = cmd if axis == "x" else 0.0
            vy = cmd if axis == "y" else 0.0
            self.publish(vx, vy, 0.0, dt)
            time.sleep(1.0 / CTRL_HZ)

        self.publish(0.0, 0.0, 0.0, dt)
        p = self.pose() or p
        dx, dy = p[0] - x0, p[1] - y0
        if axis == "x":
            actual = (dx * math.cos(yaw0) + dy * math.sin(yaw0)) * dirn
        else:
            actual = (-dx * math.sin(yaw0) + dy * math.cos(yaw0)) * dirn
        return actual

    def move_turn(self, target_deg, speed, step_timeout):
        dirn = 1.0 if target_deg > 0 else -1.0
        wmax = min(abs(speed), self.w_max)
        if abs(speed) > self.w_max:
            print("[warn] 角速度 %.2f 超过上限，已限幅到 %.2f" % (abs(speed), self.w_max))
        if self.pose() is None:
            raise PatrolAbort("尚未收到 %s 数据，无法闭环转向" % self.args.odom_topic)

        # 重置偏航角累计
        p = self.pose()
        self._prev_yaw = p[2]
        self._yaw_accum = 0.0

        target = math.radians(abs(target_deg))
        tol = math.radians(max(self.args.angle_tol, 0.2))
        cmd = 0.0
        t0 = time.monotonic()
        last = t0
        while True:
            self.rclpy.spin_once(self.node, timeout_sec=0.0)
            self.check_terminate()
            now = time.monotonic()
            dt = max(1e-3, now - last)
            last = now

            if not self.dry_run and (self.odom is None or
                                     time.monotonic() - self.odom_t > self.args.odom_timeout):
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("%s 里程计超时（>%.1fs），已停车并中止任务" %
                                  (self.args.odom_topic, self.args.odom_timeout))

            progress = self._yaw_accum * dirn
            remaining = target - progress
            if remaining <= tol:
                break
            if step_timeout > 0 and (now - t0) > step_timeout:
                self.publish(0.0, 0.0, 0.0, dt)
                raise PatrolAbort("转向超时（%.1fs，目标 %.1f° 实际 %.1f°），已停车" %
                                  (step_timeout, abs(target_deg), math.degrees(progress)))

            w_stop = math.sqrt(max(0.0, 2.0 * self.ang_accel * remaining))
            w_t = min(wmax, max(self.w_min, w_stop))
            want = dirn * w_t
            if want > cmd:
                cmd = min(want, cmd + self.ang_accel * dt)
            else:
                cmd = max(want, cmd - self.ang_accel * dt)

            self.publish(0.0, 0.0, cmd, dt)
            time.sleep(1.0 / CTRL_HZ)

        self.publish(0.0, 0.0, 0.0, dt)
        return math.degrees(self._yaw_accum)

    def dwell(self, seconds, checkpoint, name):
        if seconds > 0:
            print("[打卡] %s：原地停留 %.1fs" % (name, seconds))
        if checkpoint:
            self.checkpoints.append({
                "name": name, "time": ts(),
                "dwell_s": round(seconds, 2),
                "odom_x": round(self.pose()[0], 3) if self.pose() else None,
                "odom_y": round(self.pose()[1], 3) if self.pose() else None,
                "voltage": self.voltage,
            })
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            self.check_terminate()
            self.publish(0.0, 0.0, 0.0)
            self.spin(0.05)

    # ---------------- 单步执行 ----------------
    def auto_timeout(self, step):
        if self.args.step_timeout > 0:
            return self.args.step_timeout
        if step["type"] == "turn":
            v = max(min(abs(step["speed"]), self.w_max), 0.05)
            return math.radians(abs(step["target"])) / v * 4.0 + 10.0
        if step["type"] in ("forward", "back", "strafe"):
            v = max(min(abs(step["speed"]), self.v_max), 0.02)
            return abs(step["target"]) / v * 4.0 + 10.0
        return step["dwell"] + 10.0

    def run_step(self, idx, total, step):
        label = step["name"] or describe(step)
        print("\n[%d/%d] %s  (%s)" % (idx, total, label, describe(step)))
        rec = {"index": idx, "name": label, "type": step["type"],
               "target": step["target"], "unit": "deg" if step["type"] == "turn" else "m",
               "actual": None, "ok": False, "duration_s": None,
               "start": ts(), "note": step["note"]}
        t0 = time.monotonic()
        try:
            if step["type"] == "forward":
                rec["actual"] = self.move_linear("x", step["target"], step["speed"],
                                                 self.auto_timeout(step))
            elif step["type"] == "back":
                rec["actual"] = self.move_linear("x", -abs(step["target"]), step["speed"],
                                                 self.auto_timeout(step))
            elif step["type"] == "strafe":
                rec["actual"] = self.move_linear("y", step["target"], step["speed"],
                                                 self.auto_timeout(step))
            elif step["type"] == "turn":
                rec["actual"] = self.move_turn(step["target"], step["speed"],
                                               self.auto_timeout(step))
            elif step["type"] == "dwell":
                self.dwell(step["dwell"], step["checkpoint"], step["name"] or "点位停留")
                rec["actual"] = step["dwell"]
                rec["ok"] = True
            else:
                raise PatrolAbort("未知动作类型：%s" % step["type"])
        finally:
            if step["type"] != "dwell":
                self.stop(times=2)
            rec["duration_s"] = round(time.monotonic() - t0, 2)
            rec["end"] = ts()

        if rec["actual"] is not None and step["type"] != "dwell":
            tol = self.args.dist_tol if step["type"] != "turn" else self.args.angle_tol
            rec["actual"] = round(rec["actual"], 3)
            # 注意：转向可能为负（右转），误差按绝对值比较
            rec["error"] = round(abs(rec["actual"]) - abs(step["target"]), 3)
            rec["ok"] = abs(rec["error"]) <= max(tol, 0.01) + 0.01
            print("      实际 %.3f%s，误差 %+.3f，耗时 %.1fs %s" %
                  (rec["actual"], "°" if step["type"] == "turn" else "m",
                   rec["error"], rec["duration_s"], "OK" if rec["ok"] else "偏差偏大"))
        elif step["type"] == "dwell":
            print("      停留完成，耗时 %.1fs" % rec["duration_s"])
        self.step_records.append(rec)
        return rec

    # ---------------- 主流程 ----------------
    def run(self):
        n = len(self.steps)
        print("=" * 66)
        print("巡更路线：%s" % self.route_name)
        print("动作数：%d    循环：%s    模式：%s" %
              (n, "无限" if self.args.loop == 0 else self.args.loop,
               "预演(dry-run，不动车)" if self.dry_run else "实车运行"))
        if self.dry_run:
            print("!! DRY-RUN：不会发布 %s，仅验证路线与流程" % self.args.cmd_topic)
        if self.args.use_scan and not self.dry_run:
            print("避障：%s 前方 ±%.0f° 内 <%.2fm 停车（策略 %s，雷达朝向补偿 %.0f°）"
                  % (self.args.scan_topic, self.args.scan_arc, self.args.stop_distance,
                     self.args.obstacle_policy, self.args.scan_offset_deg))

        self.t_start = ts()
        self.t0 = time.monotonic()
        loops_done = 0
        total = n * (self.args.loop if self.args.loop > 0 else 1)
        idx = 0
        try:
            if not self.wait_for_odom(5.0):
                raise PatrolAbort(
                    "5 秒内未收到 %s，请先启动底盘：ros2 launch turn_on_wheeltec_robot "
                    "turn_on_wheeltec_robot.launch.py" % self.args.odom_topic)
            if self.args.use_scan and not self.dry_run:
                self.spin(0.3)
                if self.front_min_range() is None and self.scan_msg is None:
                    print("[warn] 未收到 %s 雷达数据，本次巡更不启用避障（可在报告中看到）" %
                          self.args.scan_topic)
                    self.events.append({"time": ts(), "type": "no_scan",
                                        "detail": "%s 无数据，避障未启用" % self.args.scan_topic})
            self.stop(times=2)

            while self.args.loop == 0 or loops_done < self.args.loop:
                loops_done += 1
                if self.args.loop != 1:
                    print("\n---------- 第 %d 轮巡更 ----------" %
                          (loops_done if self.args.loop == 0 else loops_done))
                for i, step in enumerate(self.steps, 1):
                    idx += 1
                    self.run_step(idx, total if self.args.loop > 0 else i, step)
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
        self.t_end = ts()
        dur = time.monotonic() - self.t0
        done = [r for r in self.step_records if r["ok"]]
        total_planned = len(self.steps) * (self.args.loop if self.args.loop > 0 else loops_done)
        rate = 100.0 * len(done) / total_planned if total_planned else 0.0
        report = {
            "route_name": self.route_name,
            "robot": "Wheeltec ROS2 Humble",
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
                d = os.path.expanduser("~/patrol_reports")
                path = os.path.join(d, "patrol_%s.json" % stamp())
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print("  巡更记录    ：%s" % path)
        print("=" * 66)
        return report


# ----------------------------------------------------------------------------
def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.route:
        data = _load_route_file(args.route)
        route_name, steps = parse_route(data, args, os.path.basename(args.route))
    elif args.demo:
        route_name, steps = build_demo_steps(args)
    else:
        route_name, steps = build_cli_steps(args)
        if not steps:
            parser.print_help()
            print("\n[提示] 至少要给一个动作，例如：--forward 0.5 --turn 90 "
                  "或 --route route_patrol.yaml")
            return 2

    try:
        import rclpy
        from rclpy.node import Node
    except ImportError:
        print("[错误] 无法导入 rclpy，请先 source ROS2 环境：\n"
              "  source /opt/ros/humble/setup.bash\n"
              "  (或 source ~/wheeltec_ros2/install/setup.bash)")
        return 3

    # 关掉 rclpy 自带的信号处理：否则收到 SIGTERM 时 rclpy 会先 shutdown，
    # 导致停车用的零速度指令发不出去（底盘节点没有 cmd_vel 看门狗）。
    try:
        from rclpy.signals import SignalHandlerOptions
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    except (ImportError, TypeError):
        rclpy.init()
    node = Node("patrol_mover")
    mover = PatrolMover(args, route_name, steps, rclpy, node)
    mover.install_signal_handlers()
    code = 0
    try:
        report = mover.run()
        if report["result"] != "completed":
            code = 1
    except KeyboardInterrupt:
        print("\n[中断] 已停车退出")
        code = 130
    except PatrolAbort as e:
        print("\n[中止] %s" % e)
        code = 1
    except Exception as e:  # 兜底：任何异常都先保证停车
        print("\n[错误] %s: %s" % (type(e).__name__, e))
        code = 1
    finally:
        try:
            mover.stop(times=5)
        except Exception:
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
