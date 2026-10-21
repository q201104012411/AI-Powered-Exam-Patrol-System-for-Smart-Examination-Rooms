#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
snapshot_server.py —— 三路相机快照生成器（py2 + cv_bridge）

为什么需要它：
  · web_video_server 的 MJPEG 在浏览器里多路并发会挂
  · 深度图是 16UC1，MJPEG 编码器直接报 "is not a color format"
  · 网页（py3）用不了 cv_bridge（Melodic 只给了 py2 版本）

做法：
  订阅三路原始图像 → 编码成 JPEG 写到 /tmp → 由网页的 /api/snap 接口按需读取
  · Astra RGB  : 直接 JPEG
  · USB 广角   : 直接 JPEG
  · Astra 深度 : 归一化到 0.3~3m 后上伪彩色（Turbo），便于肉眼看远近

用法：
  python tools/snapshot_server.py            # 默认 10Hz 刷新
  python tools/snapshot_server.py --fps 5
"""
from __future__ import print_function

import argparse
import os
import time

import cv2
import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image

OUT = "/tmp"
FILES = {"astra": "snap_astra.jpg", "depth": "snap_depth.jpg", "usb": "snap_usb.jpg"}


class Snapshoter(object):
    def __init__(self, args):
        self.args = args
        self.bridge = CvBridge()
        self.last = {}
        self.count = {}

        rospy.Subscriber(args.astra_topic, Image, self._astra, queue_size=1)
        rospy.Subscriber(args.usb_topic, Image, self._usb, queue_size=1)
        if args.with_depth:
            rospy.Subscriber(args.depth_topic, Image, self._depth, queue_size=1)

    def _astra(self, m):
        self._save("astra", m, False)

    def _usb(self, m):
        self._save("usb", m, False)

    def _depth(self, m):
        self._save("depth", m, True)

    def _save(self, key, m, is_depth):
        now = time.time()
        if now - self.last.get(key, 0) < 1.0 / max(1, self.args.fps):
            return
        self.last[key] = now
        try:
            if is_depth:
                d = self.bridge.imgmsg_to_cv2(m, desired_encoding="passthrough")
                d = d.astype(np.float32) / 1000.0          # mm -> m
                v = np.clip((d - 0.3) / (3.0 - 0.3), 0, 1)
                v = (v * 255).astype(np.uint8)
                try:
                    img = cv2.applyColorMap(v, cv2.COLORMAP_TURBO)
                except AttributeError:
                    img = cv2.applyColorMap(v, cv2.COLORMAP_JET)   # 老版本 OpenCV 没有 TURBO
                # 无效点（0 或超范围）涂黑
                bad = (d <= 0.05) | (d > 6.0)
                img[bad] = (0, 0, 0)
            else:
                img = self.bridge.imgmsg_to_cv2(m, "bgr8")
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
            if ok:
                path = os.path.join(OUT, FILES[key])
                with open(path + ".tmp", "wb") as f:
                    f.write(buf.tostring())
                os.rename(path + ".tmp", path)          # 原子替换，避免读到半个文件
                self.count[key] = self.count.get(key, 0) + 1
        except Exception as e:
            if not self.args.quiet:
                print("[warn] %s: %s" % (key, e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--astra-topic", default="/camera/rgb/image_raw")
    ap.add_argument("--usb-topic", default="/usb_cam/image_raw")
    ap.add_argument("--depth-topic", default="/camera/depth/image_raw")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--no-depth", dest="with_depth", action="store_false", default=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rospy.init_node("snapshot_server", anonymous=True, disable_signals=False)
    print("=" * 58)
    print("快照服务器：Astra RGB / USB / Astra 深度 → /tmp/snap_*.jpg")
    print("  话题：%s ｜ %s ｜ %s" % (args.astra_topic, args.usb_topic, args.depth_topic))
    print("=" * 58)
    s = Snapshoter(args)
    r = rospy.Rate(2)
    while not rospy.is_shutdown():
        r.sleep()
    print("退出（各相机帧数：%s）" % s.count)


if __name__ == "__main__":
    main()
