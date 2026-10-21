#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
avoid.py —— 避开障碍的移动策略（阿克曼车专用）

核心行为（每一步都闭环、都带保护）：
  1. 前进前先看三处：雷达正前方 ±35°、**深度相机正前方（高度过滤，忽略地面）**、左右前方 ±25~70°
  2. 任何一处不够 → **先倒车**（后方够才倒），再**朝更开阔的一侧转向**，然后重试
  3. 前进/转向过程中若"推不动"（进度看门狗）或"打滑"（雷达场景不变），立即停车并按同样策略倒退 + 转向
  4. 反复受阻超过上限 → 停车收工，写明原因

为什么深度相机要"高度过滤"：相机在小车上是略微上仰的，照片下半部全是地面；
如果直接把画面中位距离当障碍距离，会把地板误判成 0.4m 的障碍（我们真踩过这个坑）。
这里把像素反投影到相机坐标系，只保留"高于光轴 −12cm"的点，地面就被排除掉了。

用法（小车上，python2.7）：
  python tools/avoid.py                       # 走 8 步，每步 0.5m
  python tools/avoid.py --steps 12 --leg 0.4 --speed 0.08
  python tools/avoid.py --dry-run             # 只算不动
"""
from __future__ import print_function

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import rospy
from sensor_msgs.msg import CameraInfo, Image

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import patrol_move_ros1 as P


class DepthObstacle(object):
    """深度相机障碍测距（带高度过滤，忽略地面）。"""

    def __init__(self):
        self.msg = None
        self.t = 0.0
        self.fx = self.fy = self.cx = self.cy = None
        rospy.Subscriber("/camera/depth/image_raw", Image, self._on_img, queue_size=2)
        rospy.Subscriber("/camera/depth/camera_info", CameraInfo, self._on_info, queue_size=2)

    def _on_img(self, m):
        self.msg = m
        self.t = time.time()

    def _on_info(self, m):
        self.fx, self.fy, self.cx, self.cy = m.K[0], m.K[4], m.K[2], m.K[5]

    def ready(self):
        return (self.msg is not None and self.fx is not None
                and (time.time() - self.t) < 1.0)

    def nearest(self, y_min=-0.12, half_deg=25.0, z_max=5.0, step=4):
        """返回正前方 wedge 内、且高于光轴 y_min 的最近距离（米）；无数据 None。"""
        if not self.ready():
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
        vs = np.arange(0, h, step)
        us = np.arange(0, w, step)
        sub = img[np.ix_(vs, us)]
        ok = (sub > 0.15) & (sub < z_max)
        if not ok.any():
            return None
        z = sub[ok]
        u = us[np.where(ok)[1]]
        v = vs[np.where(ok)[0]]
        x = (u - self.cx) * z / self.fx
        y = (v - self.cy) * z / self.fy
        keep = (y > y_min) & (np.abs(x) < math.tan(math.radians(half_deg)) * z)
        if not keep.any():
            return None
        return float(z[keep].min())


def make_args(a):
    base = P.build_parser()
    ma = base.parse_args(["--forward", "0.1", "--no-report"])
    ma.car_mode = "auto"
    ma.speed = a.speed
    ma.akm_turn_speed = a.turn_speed
    ma.turn_speed = a.turn_speed
    ma.max_linear = max(0.2, a.speed)
    ma.max_angular = 0.8
    ma.accel = 0.25
    ma.ang_accel = 0.9
    ma.dist_tol = 0.01
    ma.angle_tol = a.angle_tol
    ma.step_timeout = 25.0
    ma.progress_timeout = a.progress_timeout
    ma.progress_min_ratio = 0.30
    ma.use_scan = True
    ma.scan_arc = 35.0
    ma.scan_range_max = 6.0
    ma.scan_offset_deg = 0.0
    ma.stop_distance = a.front_clear * 0.8
    ma.obstacle_policy = "abort"
    ma.obstacle_timeout = 3.0
    ma.odom_timeout = 0.5
    ma.max_tilt = 3.0
    ma.heading_hold = True
    ma.heading_gain = 0.8
    ma.heading_max_yaw = 0.25
    ma.heading_deadband = 1.0
    ma.yaw_sign = 1.0
    ma.yaw_source = "imu"
    ma.slip_check = True
    ma.dry_run = a.dry_run
    ma.verbose = False
    return ma


def msg_bundle():
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
    ap = argparse.ArgumentParser(description="阿克曼小车避障漫游（受阻就倒退+转向）")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--leg", type=float, default=0.50, help="每步前进 m（默认 0.5）")
    ap.add_argument("--speed", type=float, default=0.10)
    ap.add_argument("--turn-speed", type=float, default=0.07)
    ap.add_argument("--turn", type=float, default=45.0, help="受阻后转向角度（默认 45°）")
    ap.add_argument("--angle-tol", type=float, default=2.0)
    ap.add_argument("--front-clear", type=float, default=1.00, help="雷达正前方门限 m")
    ap.add_argument("--depth-clear", type=float, default=0.90, help="深度相机正前方门限 m")
    ap.add_argument("--diag-clear", type=float, default=0.70, help="左右前方门限 m")
    ap.add_argument("--rear-clear", type=float, default=0.50, help="允许倒车的最小后方净空 m")
    ap.add_argument("--turn-stop-distance", type=float, default=0.45,
                    help="转向时用的避障停车距离 m（转向是低速圆弧，可比直行更松，默认 0.45）")
    ap.add_argument("--back-max", type=float, default=0.35, help="单次倒车最大距离 m")
    ap.add_argument("--max-escape", type=int, default=5, help="连续受挫上限，超了收工")
    ap.add_argument("--progress-timeout", type=float, default=7.0, help="推进受阻判据时间 s")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    rospy.init_node("avoid", anonymous=True, disable_signals=False)
    mover = P.PatrolMover(make_args(args), "避障漫游", [], rospy, msg_bundle())
    mover.load_imu_offset()
    depth = DepthObstacle()

    if not mover.wait_for_odom(5.0):
        print("[错误] 无 /odom")
        return 3
    time.sleep(0.6)
    start = mover.pose()

    print("=" * 64)
    print("避障漫游：%d 步 × %.2fm，速度 %.2fm/s，受阻转向 %.0f°"
          % (args.steps, args.leg, args.speed, args.turn))
    print("门限：雷达前 %.2fm ｜ 深度前 %.2fm ｜ 左右前 %.2fm ｜ 后方倒车 ≥ %.2fm（单次最多倒 %.2fm）"
          % (args.front_clear, args.depth_clear, args.diag_clear, args.rear_clear, args.back_max))
    print("起点 odom = (%.2f, %.2f)" % (start[0], start[1]))
    print("=" * 64)

    events = []
    path = []

    def log(tag, extra=""):
        p = mover.pose()
        if p:
            path.append({"x": round(p[0], 3), "y": round(p[1], 3),
                         "yaw": round(math.degrees(p[2]), 1), "tag": tag})
        print("%s%s" % (tag, extra))

    def measure():
        rf = mover.front_sector_stat()[0]
        df = depth.nearest()
        ld = mover.sector(25, 70)
        rd = mover.sector(-70, -25)
        return rf, df, ld, rd

    def escape(reason):
        """受阻处理：先倒车，再朝更开阔的一侧转。返回 True 表示已成功腾挪。"""
        events.append({"t": time.strftime("%H:%M:%S"), "type": "blocked", "detail": reason})
        print("   ↳ 受阻：%s" % reason)
        # 1) 倒车
        rear = min(mover.sector(140, 181) or 0.0, mover.sector(-181, -140) or 0.0)
        if rear > args.rear_clear:
            back = min(args.back_max, max(0.20, rear * 0.45))
            print("   ↳ 倒车 %.2fm（后方 %.2fm）" % (back, rear))
            try:
                mover.move_linear("x", -back, args.speed, 25.0)
                log("倒车", " %.2fm" % back)
            except P.PatrolAbort as e:
                print("   ↳ 倒车失败：%s" % e)
        else:
            print("   ↳ 后方仅 %.2fm，不倒车" % rear)
        # 2) 转向
        left = mover.sector(25, 95) or 0.0
        right = mover.sector(-95, -25) or 0.0
        order = [args.turn, -args.turn] if left >= right else [-args.turn, args.turn]
        old_stop = mover.args.stop_distance
        mover.args.stop_distance = args.turn_stop_distance
        try:
            for t in order:
                try:
                    print("   ↳ 转向 %+.0f°（左 %.2f / 右 %.2f，转向阈值 %.2fm）"
                          % (t, left, right, args.turn_stop_distance))
                    got = mover.move_turn_akm(t, args.turn_speed, 45.0)
                    log("转向", " %+.2f°" % got)
                    return True
                except P.PatrolAbort as e:
                    print("   ↳ 转向失败：%s" % e)
        finally:
            mover.args.stop_distance = old_stop
        return False

    log("起点")
    escapes = 0
    done = 0
    for i in range(args.steps):
        rf, df, ld, rd = measure()
        ok = ((rf is None or rf >= args.front_clear)
              and (df is None or df >= args.depth_clear)
              and (ld is None or ld >= args.diag_clear)
              and (rd is None or rd >= args.diag_clear))
        if rf is None and not args.dry_run:
            print("\n[停止] 雷达无数据（避障不可用），不前进")
            break

        if ok:
            print("\n第 %d/%d 步：畅通（雷达前 %s / 深度前 %s / 左前 %s / 右前 %s）→ 前进 %.2fm"
                  % (i + 1, args.steps, _f(rf), _f(df), _f(ld), _f(rd), args.leg))
            try:
                got = mover.move_linear("x", args.leg, args.speed, 40.0)
                print("      实走 %.3f m" % got)
                log("前进")
                done += 1
                continue
            except P.PatrolAbort as e:
                reason = str(e)
        else:
            reason = "空间不足（雷达前 %s / 深度前 %s / 左前 %s / 右前 %s）" % (_f(rf), _f(df), _f(ld), _f(rd))

        escapes += 1
        if escapes > args.max_escape:
            print("\n[收工] 已连续受阻 %d 次，环境太窄，停车" % (escapes - 1))
            events.append({"t": time.strftime("%H:%M:%S"), "type": "give_up",
                           "detail": "连续受阻 %d 次" % (escapes - 1)})
            break
        if not escape(reason):
            print("\n[收工] 腾挪失败（前后都太窄），停车")
            break

    mover.stop(times=3)
    end = mover.pose()
    out = {"start": {"x": start[0], "y": start[1]},
           "end": {"x": end[0], "y": end[1]} if end else None,
           "steps_done": done, "escapes": escapes, "events": events, "path": path}
    d = os.path.expanduser("~/patrol_reports")
    if not os.path.isdir(d):
        os.makedirs(d)
    fp = os.path.join(d, "avoid_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    with open(fp, "wb") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n完成 %d/%d 步，腾挪 %d 次，轨迹：%s" % (done, args.steps, escapes, fp))
    print("=" * 64)
    return 0


def _f(v):
    return ("%.2f" % v) if v is not None else "--"


if __name__ == "__main__":
    sys.exit(main())
