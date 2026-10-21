#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
line_follow_white.py —— 白线巡线 + 拐角用里程计补（方案 B）

为什么是混合：
  阿克曼车最小转弯半径约 0.9m，而地胶上贴的白线拐角半径只有 0.1~0.2m，
  几何上不可能沿线转过那个弯。所以：
    · 直线段：纯视觉巡线（白线 = 低饱和 + 高亮度，实测 S<30 & V>230 可分得很干净）
    · 拐角：检测到"丢线"后，用 IMU 闭环打一个 90° 弧（车自己知道转了多少度），
            转完再让视觉重新找线、把车拉回线上

安全：
  · 丢线 >0.35s → 先停车再决定（不会瞎冲）
  · 拐角转向用 patrol_move_ros1 的闭环（IMU 反馈 + 进度看门狗）
  · 连续丢线超过 N 次 → 收工

用法：
  python tools/line_follow_white.py --dry-run     # 只检测不动车（先验证识别）
  python tools/line_follow_white.py --speed 0.06  # 实跑
"""
from __future__ import print_function

import argparse
import json
import math
import os
import signal
import sys
import time

import cv2
import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import patrol_move_ros1 as P

BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.99)]
BAND_W = [0.5, 0.8, 1.0, 1.0, 0.7]
ROI_TOP = 0.45          # 只看地平线以下的地面


def detect_line(img, v_thresh, s_thresh, min_area=120, color="white",
                max_w=45, jump=65):
    """返回 (err_norm, n_bands, mask, cover)。err_norm>0 表示线在中心右侧。

    只认"细长且连续"的线：
      1) 每带里只取宽度 3~max_w 的细段（宽黑块=其他物品，丢掉）
      2) 跨带相邻段 x 差 < jump 才连成一条链（断开的黑物连不起来）
      3) 真线一定经过靠近车头的带（band>=3），只出现在远处的不认
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if color == "black":
        mask = ((hsv[:, :, 2] < v_thresh) & (hsv[:, :, 1] < s_thresh)).astype(np.uint8) * 255
    else:
        mask = ((hsv[:, :, 1] < s_thresh) & (hsv[:, :, 2] > v_thresh)).astype(np.uint8) * 255
    mask[:int(h * ROI_TOP)] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[lab == i] = 255
    mask = keep

    roi_area = float(mask.size - int(h * ROI_TOP) * w)
    cover = float((mask > 0).sum()) / max(1.0, roi_area)

    # --- 每带取最细长的一段 ---
    cands = []
    for bi, (r0, r1) in enumerate(BANDS):
        sub = mask[int(h * r0):int(h * r1), :]
        cols = np.where(sub.any(axis=0))[0]
        if len(cols) == 0:
            continue
        segs = np.split(cols, np.where(np.diff(cols) > 12)[0] + 1)
        best = None
        for seg in segs:
            if len(seg) < 3 or len(seg) > max_w:
                continue
            if best is None or len(seg) > len(best):
                best = seg
        if best is not None:
            cands.append((bi, float(best.mean()), len(best)))

    # --- 跨带连线，取最长链 ---
    chain = []
    if cands:
        runs, cur = [], [cands[0]]
        for c in cands[1:]:
            if abs(c[1] - cur[-1][1]) < jump and c[0] - cur[-1][0] <= 2:
                cur.append(c)
            else:
                runs.append(cur); cur = [c]
        runs.append(cur)
        chain = max(runs, key=len)

    nb = len(chain)
    if nb < 2:
        return None, nb, mask, cover, None, None
    if not any(bi >= 3 for bi, _cx, _wid in chain):
        return None, nb, mask, cover, None, None

    num = den = 0.0
    errs = {}
    for bi, cx, _wid in chain:
        wb = BAND_W[bi]
        e = (cx - w / 2.0) / (w / 2.0)
        num += wb * e
        den += wb
        errs[bi] = e
    if den <= 0:
        return None, nb, mask, cover, None, None
    far = errs.get(min(errs), 0.0)
    near = errs.get(max(errs), 0.0)
    return num / den, nb, mask, cover, far, near


def build_args(a):
    ma = P.build_parser().parse_args(["--forward", "0.1", "--no-report"])
    ma.car_mode = "akm"
    ma.speed = a.corner_speed
    ma.akm_turn_speed = a.corner_speed
    ma.turn_speed = a.corner_speed
    ma.max_linear = 0.2
    ma.max_angular = 0.6
    ma.accel = 0.3
    ma.ang_accel = 1.0
    ma.dist_tol = 0.01
    ma.angle_tol = 2.0
    ma.step_timeout = 60.0
    ma.progress_timeout = a.corner_timeout      # 本车偏航率很慢，90° 要给足时间
    ma.progress_min_ratio = 0.3
    ma.use_scan = False          # 巡线时不开雷达（相机抢 USB），靠视觉+看护
    ma.require_scan = False
    ma.stop_distance = 0.30
    ma.obstacle_policy = "abort"
    ma.odom_timeout = 0.5
    ma.max_tilt = 4.0
    ma.heading_hold = False
    ma.yaw_sign = 1.0
    ma.yaw_source = "imu"
    ma.slip_check = False        # 低速巡线，避免误报
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


class LineFollower(object):
    def __init__(self, args):
        self.a = args
        self.bridge = CvBridge()
        self.img = None
        self.img_t = 0.0
        self.mover = None
        rospy.Subscriber("/camera/rgb/image_raw", Image, self._on_img, queue_size=1)
        self.pub = rospy.Publisher("/cmd_vel", msg_bundle().Twist, queue_size=1)
        self.twist = msg_bundle().Twist()
        self.state = "FOLLOW"
        self.lost_since = None
        self.corners = 0
        self.last_err = 0.0
        self.last_w = 0.0
        self._plog = 0.0             # 日志限频
        self.err_s = None
        self.search_odom0 = None
        self.last_dir = 0
        self.reacq_t = 0.0

    def _on_img(self, m):
        self.img = m
        self.img_t = time.time()

    def fresh(self):
        return self.img is not None and (time.time() - self.img_t) < 0.5

    def publish(self, v, w):
        if self.a.dry_run:
            return
        self.twist.linear.x = float(v)
        self.twist.angular.z = float(w)
        self.pub.publish(self.twist)

    def stop(self, n=3):
        for _ in range(n):
            self.publish(0.0, 0.0)
            time.sleep(1.0 / 20.0)

    def run(self):
        rate = rospy.Rate(20)
        t_dbg = 0.0
        print("=" * 62)
        if self.a.line_color == "black":
            print("黑线巡线（方案B）：v=%.2f m/s  Kp=%.2f  阈值 S<%d V<%d（暗线）"
                  % (self.a.speed, self.a.kp, self.a.s_thresh, self.a.v_thresh))
        else:
            print("白线巡线（方案B）：v=%.2f m/s  Kp=%.2f  阈值 S<%d V>%d"
                  % (self.a.speed, self.a.kp, self.a.s_thresh, self.a.v_thresh))
        if self.a.corner_mode == "none":
            print("模式：纯跟线（丢线滑行 %.2fs → 低速搜索 %.1fs，不转大弯）"
                  % (self.a.lost_time, self.a.search_time))
            print("=" * 62)
        else:
            print("拐角：丢线 %.2fs 后 %s %.0f°（%s，%.2f m/s）"
              % (self.a.lost_time, self.a.corner_dir, self.a.corner_angle,
                 "前进弧+倒车弧" if self.a.corner_mode == "kturn" else "单个弧", self.a.corner_speed))
        print("=" * 62)
        while not rospy.is_shutdown():
            if self.state in ("FOLLOW", "REACQUIRE"):
                if not self.fresh():
                    self.publish(0, 0)
                    rate.sleep()
                    continue
                img = self.bridge.imgmsg_to_cv2(self.img, "bgr8")
                err, nb, mask, cover, err_far, err_near = detect_line(
                    img, self.a.v_thresh, self.a.s_thresh, color=self.a.line_color)
                if cover > self.a.cover_limit():
                    self.publish(0, 0)
                    print("  [停止] 地面%s占比 %.0f%% > %.0f%% → 场地不对（line_color=%s）"
                          % ("暗色" if self.a.line_color == "black" else "白色",
                             100 * cover, 100 * self.a.cover_limit(), self.a.line_color))
                    break
                if err is None:
                    if self.state == "REACQUIRE":
                        # 转完的找线阶段：先直行，再左右小幅扫掠；不许再打弧
                        el = time.time() - self.reacq_t
                        if el > self.a.reacquire_time:
                            self.publish(0, 0)
                            print("  [收工] 拐角后 %.1fs 没找到线" % self.a.reacquire_time)
                            break
                        if el < 1.5:
                            self.publish(self.a.reacquire_speed, 0.0)
                        else:
                            sweep = 0.15 if int(el * 0.6) % 2 == 0 else -0.15
                            self.publish(self.a.reacquire_speed * 0.8, sweep)
                        rate.sleep()
                        continue
                    if self.lost_since is None:
                        self.lost_since = time.time()
                    if time.time() - self.lost_since <= self.a.lost_time:
                        # 短暂丢线：沿上一次的转向继续滑行，避免走走停停
                        v = self.a.speed * 0.95
                        w = self.last_w * 0.8
                        self.publish(v, w)
                        rate.sleep()
                        continue
                    if self.a.corner_mode == "none":
                        # 看不到线 → 一直后退找（不自己停），左右交替扫
                        el = time.time() - self.lost_since
                        seg = int(el / self.a.back_time) % 2
                        w = (0.25 if seg == 0 else -0.25)
                        if time.time() - self._plog > 0.5:
                            self._plog = time.time()
                            print("  [后退找线] %.1fs（v=-%.2f, w=%+.2f）"
                                  % (el, self.a.back_speed, w))
                        self.publish(-self.a.back_speed, w)      # 负速度 = 后退
                        rate.sleep()
                        continue
                    if time.time() - self.lost_since > self.a.lost_time:
                        print("  [拐角] 丢线 %.2fs → 打 %.0f° 弧" % (self.a.lost_time, self.a.corner_angle))
                        self.stop(2)
                        if not self.do_corner():
                            print("  [收工] 拐角转向失败")
                            break
                        self.lost_since = None
                        self.state = "REACQUIRE"
                        self.reacq_t = time.time()
                        if self.a.max_corners > 0 and self.corners >= self.a.max_corners:
                            print("  [完成] 已过 %d 个弯，收工" % self.corners)
                            break
                        print("  [绕圈] 已完成 %d 个弯，继续" % self.corners)
                    rate.sleep()
                    continue
                self.search_odom0 = None
                if self.state == "REACQUIRE":
                    if nb < self.a.reacquire_min_bands:
                        self.publish(self.a.reacquire_speed, 0.0)
                        rate.sleep()
                        continue
                    self.state = "FOLLOW"
                    print("  [咬回线] err=%+.2f bands=%d（第%d个弯后）" % (err, nb, self.corners))
                self.lost_since = None
                self.last_err = err
                curv = (err_far or 0.0) - (err_near or 0.0)      # >0 表示前方线往右拐
                # PD：比例项 + 微分项（提前打方向，过弯不冲出去）
                e = err
                if abs(e) < self.a.deadband:
                    e = 0.0
                elif e > 0:
                    e -= self.a.deadband
                else:
                    e += self.a.deadband
                # 误差平滑（抑制单帧抖动带来的抽动）
                if self.err_s is None:
                    self.err_s = e
                else:
                    self.err_s = (1.0 - self.a.err_alpha) * self.err_s + self.a.err_alpha * e
                # 先算弯道程度（用于降速）
                bend = min(1.0, abs(curv) * 2.5)
                lvl = max(min(1.0, abs(err) / 0.35), bend)        # 0=直道 1=急弯
                w_raw = -self.a.kp * self.err_s - self.a.kd * (err - self.last_err)
                w_raw = max(-self.a.max_angular, min(self.a.max_angular, w_raw))
                w = (1.0 - self.a.w_alpha) * self.last_w + self.a.w_alpha * w_raw
                # 变化率限制：每帧最多变 w_slew*dt（防止突然抽一下）
                dw = w - self.last_w
                lim = self.a.w_slew * (1.0 / 20.0)
                if abs(dw) > lim:
                    w = self.last_w + (lim if dw > 0 else -lim)
                self.last_w = w
                if lvl < 0.35:
                    v = self.a.speed                              # 直道全速
                elif lvl < 0.75:
                    v = self.a.speed * 0.72                       # 缓弯
                else:
                    v = max(self.a.v_min, self.a.speed * 0.45)    # 急弯
                v = min(v, self.a.speed)
                v = max(self.a.v_min, v)
                self.publish(v, w)
                if time.time() - t_dbg > 0.5:
                    t_dbg = time.time()
                    if self.a.debug:
                        self._debug(img, mask, err, nb, v, w, cover)
                    else:
                        try:
                            with open("/tmp/line_status.json", "w") as f:
                                json.dump({"ts": time.time(), "state": self.state,
                                           "err": round(err, 3), "bands": nb,
                                           "cover": round(cover, 3), "corners": self.corners,
                                           "v": round(v, 3), "w": round(w, 3)}, f)
                        except Exception:
                            pass
            rate.sleep()
        self.stop(3)
        print("巡线结束：转过 %d 个弯" % self.corners)

    def do_corner(self):
        if self.mover is None:
            self.mover = P.PatrolMover(build_args(self.a), "巡线拐角", [], rospy, msg_bundle())
            self.mover.load_imu_offset()
            if not self.mover.wait_for_odom(3.0):
                print("  [错误] 等不到 /odom，无法闭环拐弯")
                return False
            time.sleep(0.4)
        d = self.a.corner_dir
        if d == "auto":
            d = "right" if self.last_dir >= 0 else "left"
        sgn = -1 if d == "right" else 1
        ang = sgn * abs(self.a.corner_angle)
        try:
            self.mover.args.angle_tol = 0.8
            if self.a.corner_mode == "kturn":
                half = (ang + (self.a.corner_comp if ang > 0 else -self.a.corner_comp)) / 2.0
                print("        第1段 前进弧 %+.1f°（%.2f m/s）" % (half, self.a.corner_speed))
                g1 = self.mover.move_turn_akm(half, self.a.corner_speed, 60.0)
                print("        第2段 倒车弧 %+.1f°" % half)
                g2 = self.mover.move_turn_akm(half, self.a.corner_speed, 60.0, reverse=True)
                got = g1 + g2
                print("        合计实转 %.1f°（目标 %+.0f°）" % (got, ang))
            else:
                got = self.mover.move_turn_akm(ang, self.a.corner_speed, 60.0)
                print("        实转 %.1f°（目标 %+.0f°）" % (got, ang))
            p = self.mover.pose()
            self.corners += 1
            self.last_dir = 1 if sgn < 0 else -1
            return True
        except P.PatrolAbort as e:
            print("        转向中止：%s" % e)
            return False

    def _debug(self, img, mask, err, nb, v, w, cover):
        vis = img.copy()
        vis[mask > 0] = (0, 255, 255)
        h, ww = vis.shape[:2]
        cv2.line(vis, (ww // 2, int(h * ROI_TOP)), (ww // 2, h), (255, 128, 0), 1)
        for r0, r1 in BANDS:
            cv2.line(vis, (0, int(h * r0)), (ww, int(h * r0)), (110, 110, 110), 1)
        txt = "err=%+.2f bands=%d cover=%.0f%% v=%.2f w=%+.2f %s" % (err, nb, cover, v, w, self.state)
        cv2.putText(vis, txt, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(vis, txt, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imwrite("/tmp/line_dbg.jpg", vis)
        try:
            with open("/tmp/line_status.json", "w") as f:
                json.dump({"ts": time.time(), "state": self.state, "err": round(err, 3),
                           "bands": nb, "cover": round(cover, 3), "corners": self.corners,
                           "v": round(v, 3), "w": round(w, 3)}, f)
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="白线巡线（直线视觉 + 拐角里程计）")
    ap.add_argument("--speed", type=float, default=0.18, help="直道速度 m/s（弯道自动降）")
    ap.add_argument("--err-alpha", type=float, default=0.45,
                    help="误差平滑系数（越小越稳，默认0.45）")
    ap.add_argument("--w-alpha", type=float, default=0.30,
                    help="转向低通系数（越小越稳，默认0.30）")
    ap.add_argument("--w-slew", type=float, default=2.0,
                    help="转向变化率上限 rad/s²（默认2.0，防止突然抽动）")
    ap.add_argument("--deadband", type=float, default=0.07,
                    help="偏差小于此值不打方向（默认0.06，减少无谓抽动）")
    ap.add_argument("--v-min", type=float, default=0.035, help="最小速度")
    ap.add_argument("--kp", type=float, default=0.85)
    ap.add_argument("--kd", type=float, default=0.18,
                    help="微分项：提前打方向，减少过弯冲出去")
    ap.add_argument("--max-angular", type=float, default=0.50)
    ap.add_argument("--line-color", choices=["white", "black"], default="white",
                    help="线条颜色：white=亮线暗底；black=暗线亮底（黑胶带贴白瓷砖）")
    ap.add_argument("--v-thresh", type=float, default=230.0)
    ap.add_argument("--s-thresh", type=float, default=30.0)
    ap.add_argument("--lost-time", type=float, default=0.70, help="短暂丢线先滑行这么久（不刹车）")
    ap.add_argument("--max-cover", type=float, default=0.45,
                    help="地面白色像素占比超过此值 → 判定开出地胶并停车（默认0.45）")
    ap.add_argument("--corner-dir", choices=["right", "left", "auto"], default="auto")
    ap.add_argument("--corner-angle", type=float, default=90.0)
    ap.add_argument("--corner-mode", choices=["none", "kturn", "arc"], default="none",
                    help="拐角动作：none=只跟线（丢线就低速找）；kturn=前进弧+倒车弧；arc=单个90°弧")
    ap.add_argument("--corner-speed", type=float, default=0.06)
    ap.add_argument("--corner-comp", type=float, default=4.0,
                    help="K-turn 补偿角：每段多转这么多度，抵消闭环提前停（实测 90° 只到 86°）")
    ap.add_argument("--reacquire-min-bands", type=int, default=3,
                    help="找线阶段要求至少几条带命中才切回巡线（默认3，避免半残检测）")
    ap.add_argument("--back-speed", type=float, default=0.15,
                    help="丢线后退的速度（默认0.15；底盘低于0.12不动）")
    ap.add_argument("--back-time", type=float, default=1.5,
                    help="每段后退持续多久（默认1.5s），段间左右交替扫")
    ap.add_argument("--max-back", type=int, default=3,
                    help="最多后退几段（默认3，约 4.5 秒）")
    ap.add_argument("--search-speed", type=float, default=0.06,
                    help="corner-mode=none 时，丢线后低速搜索的速度")
    ap.add_argument("--search-time", type=float, default=12.0,
                    help="丢线后最多找多久，超时就停（默认12s）")
    ap.add_argument("--search-dist", type=float, default=1.2,
                    help="丢线搜索阶段最多走这么远（米），防止走出去")
    ap.add_argument("--reacquire-time", type=float, default=6.0,
                    help="拐角转完后，以低速直行找线的最长时间（默认5s）")
    ap.add_argument("--reacquire-speed", type=float, default=0.05,
                    help="找线阶段的速度（默认0.05）")
    ap.add_argument("--max-corners", type=int, default=8,
                    help="最多过几个弯就收工（0=无限绕圈）")
    ap.add_argument("--corner-timeout", type=float, default=35.0,
                    help="拐角转向的受阻看门狗时间（本车 90° 弧约 20~30s）")
    ap.add_argument("--debug", dest="debug", action="store_true", default=True)
    ap.add_argument("--no-debug", dest="debug", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.line_color == "black" and args.v_thresh >= 230:
        args.v_thresh = 75.0          # 黑线默认阈值（阴影地面约 85~110，75 以下才算线）
        args.s_thresh = max(args.s_thresh, 90.0)
    args.cover_limit = lambda: (0.85 if args.line_color == "black" else args.max_cover)

    rospy.init_node("line_follow_white", anonymous=True, disable_signals=False)
    lf = LineFollower(args)

    def _bye(sig, frm):
        print("\n[退出] 收到信号 %s，发零速" % sig)
        try:
            lf.stop(5)
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)
    try:
        signal.signal(signal.SIGHUP, _bye)
    except Exception:
        pass

    try:
        lf.run()
    finally:
        lf.stop(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
