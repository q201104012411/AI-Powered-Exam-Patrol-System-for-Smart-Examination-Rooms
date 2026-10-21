#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wander.py —— 教室里的"安全漫游"（乱走但不撞）

和 patrol_move_ros1.py 共用同一套闭环/保护（直接 import 复用），额外加：
  · 双重净空门限：雷达 ±35°（量程放开到 6m）+ 深度相机正前方（桌板/桌面）
    —— 2D 雷达会从桌子底下"穿过去"看远墙，所以家具必须靠深度相机兜底
  · 每一步随机/择优选方向：直行优先，前方不够就朝更开阔的一侧转
  · 走完自动回家（用 odom 算回起点的方位角与距离）
  · 全过程记录轨迹到 JSON，便于事后画路径图

用法（小车上，python2.7，因为 numpy 只在 py2 下确认可用）：
  python wander.py                        # 默认 6 步、每步 0.5m
  python wander.py --steps 10 --leg 0.6 --speed 0.10
  python wander.py --steps 4 --dry-run    # 只算不动
"""
from __future__ import print_function

import argparse
import json
import math
import os
import random
import sys
import time

import numpy as np
import rospy
from sensor_msgs.msg import Image

# 复用上一级的 patrol_move_ros1.py（脚本在 tools/ 里，需要把父目录加入 sys.path）
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import patrol_move_ros1 as P


class DepthProbe(object):
    """深度相机：给"正前方有没有家具"提供独立判据。"""

    def __init__(self):
        self.msg = None
        self.t = 0.0
        rospy.Subscriber("/camera/depth/image_raw", Image, self._cb, queue_size=2)

    def _cb(self, m):
        self.msg = m
        self.t = time.time()

    def fresh(self):
        return self.msg is not None and (time.time() - self.t) < 1.0

    def nearest(self, r0=0.45, r1=0.82, c0=0.28, c1=0.72, pct=10.0):
        """返回正前方区域的下分位距离（米），没有数据返回 None。"""
        if not self.fresh():
            return None
        m = self.msg
        try:
            if (m.encoding or "").lower() in ("16uc1", "mono16"):
                img = np.frombuffer(m.data, dtype=np.uint16).reshape(m.height, m.width).astype(np.float32) / 1000.0
            else:
                img = np.frombuffer(m.data, dtype=np.float32).reshape(m.height, m.width)
        except Exception:
            return None
        h, w = img.shape
        band = img[int(h * r0):int(h * r1), int(w * c0):int(w * c1)]
        v = band[(band > 0.15) & (band < 8.0)]
        if v.size < 50:
            return None
        return float(np.percentile(v, pct))


def build_mover_args(wander_args):
    """借 patrol_move_ros1 的参数体系，构造一套安全的漫游参数。"""
    base = P.build_parser()
    a = base.parse_args(["--forward", "0.1", "--no-report"])
    # 覆盖成漫游用的设置
    a.car_mode = "auto"
    a.speed = wander_args.speed
    a.akm_turn_speed = wander_args.turn_speed
    a.turn_speed = wander_args.turn_speed
    a.max_linear = max(0.2, wander_args.speed)
    a.max_angular = 0.8
    a.accel = 0.25
    a.ang_accel = 0.9
    a.dist_tol = 0.01
    a.angle_tol = wander_args.angle_tol
    a.step_timeout = 0
    a.use_scan = True
    a.scan_arc = 35.0
    a.scan_range_max = wander_args.scan_range_max      # 开阔教室要放开量程
    a.scan_offset_deg = 0.0
    a.stop_distance = wander_args.min_clear * 0.7      # 兜底停车距离比门限更紧
    a.obstacle_policy = "abort"
    a.obstacle_timeout = 3.0
    a.odom_timeout = 0.5
    a.max_tilt = 3.0
    a.heading_hold = True
    a.heading_gain = 0.8
    a.heading_max_yaw = 0.25
    a.heading_deadband = 1.0
    a.yaw_sign = 1.0
    a.yaw_source = "imu"
    a.dry_run = wander_args.dry_run
    a.verbose = False
    return a


def load_msg_bundle():
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import LaserScan, Imu
    from std_msgs.msg import Float32

    class M(object):
        pass
    m = M()
    m.Twist, m.Odometry, m.LaserScan, m.Float32, m.Imu = Twist, Odometry, LaserScan, Float32, Imu
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description="教室安全漫游（雷达 + 深度双门限）")
    ap.add_argument("--steps", type=int, default=6, help="移动步数（默认 6）")
    ap.add_argument("--leg", type=float, default=0.50, help="每步直线距离 m（默认 0.5）")
    ap.add_argument("--speed", type=float, default=0.10, help="线速度 m/s（默认 0.10）")
    ap.add_argument("--turn-speed", type=float, default=0.06, help="转弯时前进速度 m/s（默认 0.06）")
    ap.add_argument("--turn", type=float, default=45.0, help="转向角度（默认 45°）")
    ap.add_argument("--angle-tol", type=float, default=2.0)
    ap.add_argument("--min-clear", type=float, default=1.2, help="正前方双净空门限 m（默认 1.2）")
    ap.add_argument("--diag-min", type=float, default=0.8,
                    help="左右前方(±25~70°)门限 m，防止钻进桌椅过道（默认 0.8）")
    ap.add_argument("--scan-range-max", type=float, default=6.0, help="雷达避障量程 m（默认 6）")
    ap.add_argument("--home", dest="home", action="store_true", default=True, help="走完回家")
    ap.add_argument("--no-home", dest="home", action="store_false")
    ap.add_argument("--allow-no-lidar", action="store_true",
                    help="雷达没数据时仍允许前进（默认禁止：fail-closed）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    rospy.init_node("wander", anonymous=True, disable_signals=False)
    ma = build_mover_args(args)
    mover = P.PatrolMover(ma, "教室安全漫游", [], rospy, load_msg_bundle())
    mover.load_imu_offset()
    depth = DepthProbe()

    mover.wait_for_odom(5.0)
    time.sleep(0.5)
    start = mover.pose()
    if start is None:
        print("[错误] 没有 /odom，先启动底盘")
        return 3

    print("=" * 62)
    print("教室安全漫游：步数 %d，每步 %.2fm，速度 %.2fm/s，转向 %+.0f°"
          % (args.steps, args.leg, args.speed, args.turn))
    print("双重门限：雷达 %.1fm 量程/±35° 与 深度相机 均需 ≥ %.2fm 才前进"
          % (args.scan_range_max, args.min_clear))
    print("起点 odom = (%.2f, %.2f)" % (start[0], start[1]))
    print("=" * 62)

    track = []
    try:
        mover.check_tilt("（漫游启动前）")
    except P.PatrolAbort as e:
        print("[中止] %s" % e)
        return 1

    def snap(tag):
        p = mover.pose()
        r_min, n_pts = mover.front_sector_stat()
        d_min = depth.nearest()
        rec = {"t": round(time.time(), 2), "tag": tag,
               "x": round(p[0], 3) if p else None, "y": round(p[1], 3) if p else None,
               "yaw": round(math.degrees(p[2]), 1) if p else None,
               "lidar_front": round(r_min, 2) if r_min is not None else None,
               "lidar_pts": n_pts,
               "depth_front": round(d_min, 2) if d_min is not None else None}
        track.append(rec)
        print("   [%s] odom=(%.2f,%.2f) yaw=%s° | 雷达前 %s m(%d点) | 深度前 %s m"
              % (tag, rec["x"], rec["y"], rec["yaw"],
                 rec["lidar_front"], rec["lidar_pts"], rec["depth_front"]))
        return rec

    snap("起点")

    moved = 0
    for i in range(args.steps):
        p = mover.pose()
        if p is None:
            print("[中止] odom 丢失")
            break
        r_min, n_pts = mover.front_sector_stat()
        d_min = depth.nearest()
        ld = mover.sector(25, 70)
        rd = mover.sector(-70, -25)
        if r_min is None and not args.allow_no_lidar:
            print("\n[停止] 雷达 /scan 无数据（避障不可用）→ 不前进。先重启雷达："
                  "roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch")
            break
        ok_front = (r_min is None or r_min >= args.min_clear)
        ok_depth = (d_min is None or d_min >= args.min_clear)
        ok_diag = ((ld is None or ld >= args.diag_min) and (rd is None or rd >= args.diag_min))

        if ok_front and ok_depth and ok_diag:
            print("\n第 %d/%d 步：前方畅通（雷达 %s / 深度 %s / 左前 %s / 右前 %s）→ 直行 %.2fm"
                  % (i + 1, args.steps,
                     ("%.2f" % r_min) if r_min is not None else "--",
                     ("%.2f" % d_min) if d_min is not None else "--",
                     ("%.2f" % ld) if ld is not None else "--",
                     ("%.2f" % rd) if rd is not None else "--", args.leg))
            try:
                got = mover.move_linear("x", args.leg, args.speed, 60.0)
                print("        实走 %.3f m" % got)
                moved += 1
            except P.PatrolAbort as e:
                print("        [中止] %s" % e)
                break
            snap("直行%d" % (i + 1))
            continue

        # 前方不够 → 选更空的一侧转
        left = mover.sector(45, 90) or 0.0
        right = mover.sector(-90, -45) or 0.0
        turn = args.turn if left >= right else -args.turn
        print("\n第 %d/%d 步：前方不够（雷达 %s / 深度 %s / 左前 %s / 右前 %s）→ 朝%s转 %.0f°（左 %.2f / 右 %.2f）"
              % (i + 1, args.steps,
                 ("%.2f" % r_min) if r_min is not None else "--",
                 ("%.2f" % d_min) if d_min is not None else "--",
                 ("%.2f" % ld) if ld is not None else "--",
                 ("%.2f" % rd) if rd is not None else "--",
                 "左" if turn > 0 else "右", abs(turn), left, right))
        try:
            got = mover.move_turn_akm(turn, args.turn_speed, 90.0, reverse=False)
            print("        实转 %.2f°" % got)
        except P.PatrolAbort as e:
            print("        [中止] %s" % e)
            break
        snap("转向%d" % (i + 1))

    # ---------- 回家 ----------
    if args.home:
        print("\n---- 回家 ----")
        for k in range(3):
            p = mover.pose()
            if p is None:
                break
            dx, dy = start[0] - p[0], start[1] - p[1]
            dist = math.hypot(dx, dy)
            if dist < 0.20:
                print("  已在起点附近（%.2fm）" % dist)
                break
            bearing = math.degrees(math.atan2(dy, dx)) - math.degrees(p[2])
            bearing = (bearing + 180.0) % 360.0 - 180.0
            print("  距起点 %.2fm，需转 %+.0f°" % (dist, bearing))
            try:
                rf = mover.front_sector_stat()[0]
                if rf is not None and rf < args.min_clear:
                    # 前方太窄，先倒车腾出空间再转
                    rr = mover.sector(150, 181) or mover.sector(-181, -150) or 0.0
                    back = min(max(rf * 0.6, 0.25), 0.5)
                    print("  前方仅 %.2fm，先倒车 %.2fm（后方 %.2fm）" % (rf, back, rr))
                    mover.move_linear("x", -back, args.speed, 40.0)
                if abs(bearing) > 8.0:
                    mover.move_turn_akm(bearing, args.turn_speed, 120.0)
                mover.move_linear("x", min(dist * 0.85, 1.0), args.speed, 90.0)
            except P.PatrolAbort as e:
                print("  [回家中止] %s" % e)
                break
        snap("回家")

    mover.stop(times=3)

    end = mover.pose()
    out = {"start": {"x": start[0], "y": start[1]},
           "end": {"x": end[0], "y": end[1]} if end else None,
           "moved_legs": moved, "steps_requested": args.steps,
           "params": {"leg": args.leg, "speed": args.speed, "min_clear": args.min_clear},
           "track": track}
    path = os.path.expanduser("~/patrol_reports/wander_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n轨迹已保存：%s" % path)
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
