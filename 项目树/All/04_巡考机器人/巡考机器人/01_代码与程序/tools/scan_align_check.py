#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scan_align_check.py —— 判定 /scan 与车体朝向的角度对应关系（判断 --scan-offset-deg 该填多少）

为什么需要它：
  避障扇区算错方向 = 对着盲区看 = 等于没有避障。
  Wheeltec 车的 URDF 里 base_to_laser 带 3.14 偏航，但实际安装/驱动发布角度可能不一致，
  所以必须用数据判定，而不是照抄 URDF。

原理：
  深度相机（Astra，装在车头、光轴朝车体 +x）能给出"车体角度 → 距离"的剖面；
  雷达给出"雷达原始角度 → 距离"的剖面。两者看的是同一个场景，
  谁的对应关系吻合（平均绝对差最小），车头就在哪个雷达角度上。

用法（在小车上，底盘+相机+雷达都启动后）：
  python tools/scan_align_check.py                # 用 depth 图像
  python tools/scan_align_check.py --fixed 180    # 只验证某个补偿角（不做匹配）
兼容 python2.7 / python3。
"""
from __future__ import print_function

import argparse
import math
import sys
import time

import numpy as np
import rospy
from sensor_msgs.msg import CameraInfo, Image, LaserScan

DEPTH_TOPICS = ["/camera/depth/image_raw", "/camera/depth/image", "/camera/depth/image_rect_raw"]
INFO_TOPICS = ["/camera/depth/camera_info", "/camera/depth/camera_info_raw"]


def wait_first(topics, cls, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout and not rospy.is_shutdown():
        for t in topics:
            try:
                return t, rospy.wait_for_message(t, cls, timeout=2.0)
            except Exception:
                continue
    return None, None


def depth_profile(img_msg, fx, cx, step=6, zmin=0.15, zmax=8.0):
    """返回 {车体角度(度, 左正): 距离(m)}，角度由相机内参换算。"""
    w, h = img_msg.width, img_msg.height
    enc = (img_msg.encoding or "").lower()
    if enc in ("16uc1", "mono16"):
        arr = np.frombuffer(img_msg.data, dtype=np.uint16)
        scale = 0.001
    elif enc in ("32fc1", "32fc1"):
        arr = np.frombuffer(img_msg.data, dtype=np.float32)
        scale = 1.0
    else:
        raise ValueError("不支持的深度编码：%s" % img_msg.encoding)
    arr = arr.reshape(h, w).astype(np.float32) * scale

    out = {}
    r0, r1 = h // 3, 2 * h // 3          # 取中间 1/3 行，避开地面与天花板
    for u in range(0, w, step):
        col = arr[r0:r1, u]
        v = col[(col > zmin) & (col < zmax)]
        if v.size < max(3, (r1 - r0) // 20):
            continue
        # 相机光学系：x 向右；车体 ROS 系：y 向左 → 车体角度 = -atan((u-cx)/fx)
        ang = -math.degrees(math.atan((u - cx) / fx))
        out[round(ang, 1)] = float(np.median(v))
    return out


def lidar_at(scan, body_angle_deg, offset_deg, win_deg=2.0):
    """取雷达在 raw = body_angle + offset 附近的最近距离（窗口内最小）。"""
    target = body_angle_deg + offset_deg
    best = None
    for i, r in enumerate(scan.ranges):
        if r != r or r == float("inf") or r <= scan.range_min:
            continue
        a = math.degrees(scan.angle_min + i * scan.angle_increment)
        d = (a - target + 180.0) % 360.0 - 180.0
        if abs(d) <= win_deg and (best is None or r < best):
            best = r
    return best


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scan-topic", default="/scan")
    p.add_argument("--fixed", type=float, default=None,
                   help="只验证指定补偿角（度），不做自动匹配")
    args = p.parse_args()

    rospy.init_node("scan_align_check", anonymous=True, disable_signals=True)

    dtopic, dimg = wait_first(DEPTH_TOPICS, Image)
    itopic, info = wait_first(INFO_TOPICS, CameraInfo)
    if dimg is None or info is None:
        print("[错误] 拿不到深度图或内参，请先启动相机：roslaunch turn_on_wheeltec_robot wheeltec_camera.launch")
        return 3
    print("深度图：%s（%dx%d, %s）  内参：%s" % (dtopic, dimg.width, dimg.height,
                                                dimg.encoding, itopic))
    fx, cx = info.K[0], info.K[2]
    print("fx=%.1f cx=%.1f  → 可用视角约 ±%.0f°" % (fx, cx, math.degrees(math.atan(cx / fx))))

    try:
        prof = depth_profile(dimg, fx, cx)
    except ValueError as e:
        print("[错误] %s" % e)
        return 3
    if len(prof) < 5:
        print("[错误] 深度有效点太少（%d），检查相机是否被挡" % len(prof))
        return 3
    print("相机剖面：%d 个角度采样，距离范围 %.2f~%.2f m"
          % (len(prof), min(prof.values()), max(prof.values())))

    scan = rospy.wait_for_message(args.scan_topic, LaserScan, timeout=10)

    def score(offset):
        diffs = []
        for ang, d_cam in sorted(prof.items()):
            d_lidar = lidar_at(scan, ang, offset)
            if d_lidar is None:
                continue
            diffs.append(abs(d_lidar - d_cam))
        if len(diffs) < 4:
            return None, 0
        return float(np.median(diffs)), len(diffs)

    if args.fixed is not None:
        cands = [args.fixed]
    else:
        cands = [0.0, 90.0, 180.0, -90.0, 45.0, -45.0, 135.0, -135.0]

    print("\n补偿角假设  与深度相机的吻合度（中位绝对差，越小越吻合）")
    results = []
    for off in cands:
        m, n = score(off)
        if m is None:
            print("  %+7.1f°   —— 雷达该方向几乎无有效点（%d 个可比角度）" % (off, n))
            continue
        results.append((m, off, n))
        print("  %+7.1f°   中位差 %.2f m（%d 个可比角度）" % (off, m, n))

    if not results:
        print("\n结论：所有假设都对不上 → 雷达与相机可能不是同一朝向，请人工确认。")
        return 1

    results.sort()
    best_m, best_off, n = results[0]
    print("\n► 最吻合的补偿角：%+.0f°（中位差 %.2f m，%d 个角度）" % (best_off, best_m, n))
    print("  请这样运行巡更脚本：--scan-offset-deg %d" % int(round(best_off)))
    print("  （若该方向雷达点很少，说明是盲区，避障不可靠，需检查雷达是否被遮挡）")

    # 打印两侧对照，便于人工复核
    print("\n角度对照（车体角 ← 相机 vs 雷达两个方向）：")
    print("  车体角   相机距离   raw=车体角    raw=车体角+180")
    for ang in sorted(prof.keys()):
        if abs(ang) > 28:
            continue
        d0 = lidar_at(scan, ang, 0.0)
        d180 = lidar_at(scan, ang, 180.0)
        print("  %+6.1f°  %6.2f m   %10s   %12s"
              % (ang, prof[ang],
                 ("%.2f m" % d0) if d0 is not None else "无点",
                 ("%.2f m" % d180) if d180 is not None else "无点"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
