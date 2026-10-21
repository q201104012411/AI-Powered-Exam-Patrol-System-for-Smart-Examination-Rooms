#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
robot_web.py —— 小车网页遥控面板（浏览器里开车 + 看状态 + 看摄像头）

特点（安全优先）：
  · 网页"按住才动"：松开鼠标/手离开按钮、或浏览器标签被关掉，服务器 0.6s 内自动停车（看门狗）
  · 急停（E-STOP）：页面上一键锁死，除了解除急停，任何指令都被忽略
  · 前方避障：车头 ±25° 内 < stop_distance 时，前进指令被拒绝（后退不受限）
  · 倾角保护：车体倾斜超过 max_tilt 时拒绝移动（配合 imu_offset.json 零位）
  · 速度硬限幅：网页最多只能到 --max-linear / --max-angular
  · --dry-run：只记录不发布，用来远程验证接口（车不会动）

依赖：只用 Python 标准库 + rospy（不需要 flask）。
兼容 python2.7 / python3。

用法（在小车上）：
  source /opt/ros/melodic/setup.bash
  source ~/wheeltec_robot/devel/setup.bash
  export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=172.19.12.66
  python3 robot_web.py                     # 默认监听 0.0.0.0:8080
  python3 robot_web.py --dry-run           # 只测接口，不动车
  python3 robot_web.py --port 8080 --max-linear 0.15 --max-angular 0.5

  # 摄像头（可选，另开终端）：web_video_server 提供 MJPEG
  ~/wheeltec_robot/devel/lib/web_video_server/web_video_server _port:=8081

然后浏览器打开： http://<小车IP>:8080/
"""

from __future__ import print_function

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time

try:                                     # py3
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import urlparse, parse_qs
except ImportError:                      # py2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
    from urlparse import urlparse, parse_qs

import rospy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import Float32

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(HERE, "index.html")


def wrap_pi(a):
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def yaw_of(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def tilt_of(q):
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (q.w * q.y - q.z * q.x)))))
    roll = math.degrees(math.atan2(2.0 * (q.w * q.x + q.y * q.z),
                                   1.0 - 2.0 * (q.x * q.x + q.y * q.y)))
    return pitch, roll


class RobotState(object):
    """所有共享状态；HTTP 线程 / rospy 回调 / 控制线程都读写它。"""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()

        # 传感器
        self.odom = None          # (x, y, yaw)
        self.odom_t = 0.0
        self.imu = None           # (yaw, pitch, roll) 已扣除零位
        self.imu_t = 0.0
        self.scan = None
        self.scan_t = 0.0
        self.voltage = None

        # 控制
        self.cmd_v = 0.0
        self.cmd_w = 0.0
        self.last_cmd_t = 0.0
        self.estop = False
        self.reason = ""          # 当前被拦下的原因
        self.started = time.time()
        self.imu_offset = self._load_offset()

        # 白线巡线（外部进程）：它接管 /cmd_vel 时网页不再发布速度
        self.line_proc = None
        self.line_log = ""
        self.line_started = 0.0

    # ---------- 零位 ----------
    def _load_offset(self):
        """零位文件可能在 webui/ 或它的上一级（~/patrol_robot/）里。"""
        pitch = roll = 0.0
        for path in (os.path.join(HERE, "imu_offset.json"),
                     os.path.join(os.path.dirname(HERE), "imu_offset.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
                pitch = float(d.get("pitch_deg", 0.0))
                roll = float(d.get("roll_deg", 0.0))
                self.offset_src = path
                break
            except Exception:
                continue
        return (pitch, roll)

    # ---------- 回调 ----------
    def on_odom(self, m):
        p = m.pose.pose.position
        self.odom = (p.x, p.y, yaw_of(m.pose.pose.orientation))
        self.odom_t = time.time()

    def on_imu(self, m):
        yaw = yaw_of(m.orientation)
        pitch, roll = tilt_of(m.orientation)
        self.imu = (math.degrees(yaw), pitch - self.imu_offset[0], roll - self.imu_offset[1])
        self.imu_t = time.time()

    def on_scan(self, m):
        self.scan = m
        self.scan_t = time.time()

    def on_voltage(self, m):
        self.voltage = float(m.data)

    # ---------- 派生量 ----------
    def fresh(self, t, timeout=1.0):
        return (time.time() - t) < timeout

    def sector(self, lo_deg, hi_deg):
        """车体角扇区内的最近距离（补偿角 0，本车实测 /scan 与车体同向）。"""
        m = self.scan
        if m is None or not self.fresh(self.scan_t):
            return None
        best = float("inf")
        for i, r in enumerate(m.ranges):
            if r != r or r == float("inf") or r <= m.range_min:
                continue
            a = math.degrees(m.angle_min + i * m.angle_increment)
            if lo_deg <= a <= hi_deg and r < best:
                best = r
        return best if best != float("inf") else None

    def clearances(self):
        return {
            "front": self.sector(-25, 25),
            "left": self.sector(45, 90),
            "right": self.sector(-90, -45),
            "rear": max(self.sector(150, 181) or 0.0, self.sector(-181, -150) or 0.0) or None,
        }

    def tilt_ok(self):
        if self.imu is None or not self.fresh(self.imu_t):
            return True, 0.0, 0.0
        pitch, roll = self.imu[1], self.imu[2]
        lim = abs(self.args.max_tilt)
        if lim > 0 and (abs(pitch) > lim or abs(roll) > lim):
            return False, pitch, roll
        return True, pitch, roll

    def set_cmd(self, v, w):
        with self.lock:
            self.cmd_v = v
            self.cmd_w = w
            self.last_cmd_t = time.time()

    def snapshot(self):
        ok_tilt, pitch, roll = self.tilt_ok()
        clr = self.clearances()
        front = clr["front"]
        blocked_by = ""
        with self.lock:
            v, w, last = self.cmd_v, self.cmd_w, self.last_cmd_t
            estop = self.estop
            reason = self.reason
        if estop:
            blocked_by = "急停"
        elif not ok_tilt:
            blocked_by = "车体倾斜 %.1f°" % max(abs(pitch), abs(roll))
        elif v > 0 and front is not None and front < self.args.stop_distance:
            blocked_by = "前方 %.2fm 障碍" % front
        odom = self.odom if self.fresh(self.odom_t) else None
        imu = self.imu if self.fresh(self.imu_t) else None
        return {
            "ok": 1,
            "estop": 1 if estop else 0,
            "blocked": blocked_by,
            "reason": reason,
            "dry_run": 1 if self.args.dry_run else 0,
            "battery": self.voltage,
            "odom": ({"x": odom[0], "y": odom[1], "yaw": math.degrees(odom[2])} if odom else None),
            "imu": ({"yaw": imu[0], "pitch": imu[1], "roll": imu[2]} if imu else None),
            "clear": clr,
            "cmd": {"v": v, "w": w, "age": (time.time() - last) if last else 99.0},
            "limits": {"max_linear": self.args.max_linear, "max_angular": self.args.max_angular,
                       "stop_distance": self.args.stop_distance, "max_tilt": self.args.max_tilt},
            "uptime": time.time() - self.started,
            "line": self.line_snapshot(),
        }

    # ---------- 白线巡线 ----------
    def line_running(self):
        return self.line_proc is not None and self.line_proc.poll() is None

    def line_status_file(self):
        for path in ("/tmp/line_status.json",):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                continue
        return None

    def line_snapshot(self):
        running = self.line_running()
        st = self.line_status_file() if running else None
        if st and (time.time() - st.get("ts", 0) > 3.0):
            st = None
        rc = None if running or self.line_proc is None else self.line_proc.returncode
        # 进程输出（含退出原因）从日志文件尾部取，方便网页直接看到
        tail = ""
        try:
            with open("/tmp/line_web.log") as f:
                tail = f.read()[-800:]
        except Exception:
            tail = self.line_log or ""
        return {"running": 1 if running else 0, "rc": rc,
                "status": st,
                "log": tail}

    def set_line_color(self, c):
        self.line_color = c if c in ("white", "black") else "white"
        return self.line_color

    def line_start(self):
        if self.line_running():
            return {"ok": 1, "already": 1}
        if self.estop:
            return {"ok": 0, "err": "急停锁定中，先解除急停"}
        script = os.path.join(os.path.dirname(HERE), "tools", "line_follow_white.py")
        if not os.path.exists(script):
            script = os.path.join(HERE, "..", "tools", "line_follow_white.py")
        if not os.path.exists(script):
            return {"ok": 0, "err": "找不到 tools/line_follow_white.py"}
        # 先停掉网页自己的速度，避免两边抢 /cmd_vel
        self.set_cmd(0.0, 0.0)
        # 注意：Melodic 的 cv_bridge 只有 python2.7 版本，巡线节点必须用 py2 启动
        py = os.environ.get("LINE_PYTHON", "python")
        color = getattr(self, "line_color", "black")
        cmd = [py, "-u", script,
               "--line-color", color,
               "--speed", "0.07", "--kp", "1.1",
               "--corner-mode", "kturn", "--corner-dir", "left",
               "--corner-speed", "0.06", "--corner-timeout", "35",
               "--reacquire-time", "6", "--max-corners", "0"]
        try:
            f = open("/tmp/line_web.log", "w")
            self.line_proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                              cwd=os.path.dirname(script))
        except Exception as e:
            return {"ok": 0, "err": "启动失败: %s（可用环境变量 LINE_PYTHON 指定解释器）" % e}
        self.line_started = time.time()
        self.line_log = "已启动：%s" % " ".join(cmd[2:])
        return {"ok": 1, "pid": self.line_proc.pid}

    def line_stop(self):
        if not self.line_running():
            return {"ok": 1, "already": 1}
        try:
            self.line_proc.send_signal(signal.SIGINT)   # 节点会自己发零速
        except Exception:
            pass
        for _ in range(30):
            if self.line_proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.line_proc.poll() is None:
            try:
                self.line_proc.kill()
            except Exception:
                pass
        self.line_log = "已停止"
        return {"ok": 1}


def control_loop(state, pub):
    """20Hz 控制线程：限幅 + 看门狗 + 安全拦截，然后把指令发出去。"""
    rate_hz = 20.0
    dt = 1.0 / rate_hz
    while not rospy.is_shutdown():
        now = time.time()
        with state.lock:
            v, w, last = state.cmd_v, state.cmd_w, state.last_cmd_t
            estop = state.estop

        reason = ""
        if estop:
            v = w = 0.0
            reason = "急停锁定"
        elif last <= 0 or (now - last) > state.args.watchdog:
            v = w = 0.0
            reason = ""                      # 正常空闲
        else:
            ok_tilt, pitch, roll = state.tilt_ok()
            front = state.sector(-25, 25)
            if not ok_tilt:
                v = w = 0.0
                reason = "车体倾斜 %.1f°" % max(abs(pitch), abs(roll))
            elif v > 0 and front is not None and front < state.args.stop_distance:
                v = w = 0.0
                reason = "前方 %.2fm 障碍" % front
            elif not state.fresh(state.odom_t, 1.0):
                v = w = 0.0
                reason = "里程计无数据"

        state.reason = reason
        # 巡线进程在跑时，/cmd_vel 由它独占，网页不能同时发速度
        if state.line_running():
            if v or w:
                state.reason = "白线巡线中（手动控制已锁）"
            time.sleep(dt)
            continue
        if not state.args.dry_run:
            tw = Twist()
            tw.linear.x = v
            tw.angular.z = w
            pub.publish(tw)
        time.sleep(dt)


class Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, fmt, *args):
        if self.state.args.verbose:
            sys.stderr.write("[http] " + (fmt % args) + "\n")

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _q(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        return u.path, dict((k, v[0]) for k, v in q.items())

    # ---------- 路由 ----------
    def do_GET(self):
        path, q = self._q()

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, "index.html 读取失败: %s" % e, "text/plain; charset=utf-8")

        if path == "/api/state":
            return self._json(self.state.snapshot())

        if path == "/api/cmd":
            try:
                v = float(q.get("v", 0.0))
                w = float(q.get("w", 0.0))
            except ValueError:
                return self._json({"ok": 0, "err": "v/w 必须是数字"}, 400)
            a = self.state.args
            v = max(-abs(a.max_linear), min(abs(a.max_linear), v))
            w = max(-abs(a.max_angular), min(abs(a.max_angular), w))
            self.state.set_cmd(v, w)
            self.state.reason = ""
            return self._json({"ok": 1, "cmd": {"v": v, "w": w}})

        if path == "/api/stop":
            self.state.set_cmd(0.0, 0.0)
            return self._json({"ok": 1, "stopped": 1})

        if path == "/api/line":
            on = q.get("on", "0") in ("1", "true", "yes")
            if q.get("color"):
                self.state.set_line_color(q.get("color"))
            return self._json(self.state.line_start() if on else self.state.line_stop())

        if path == "/api/estop":
            on = q.get("on", "1") in ("1", "true", "yes")
            self.state.estop = on
            if on:
                self.state.set_cmd(0.0, 0.0)
                try:
                    self.state.line_stop()
                except Exception:
                    pass
            return self._json({"ok": 1, "estop": 1 if self.state.estop else 0})

        # 非 API 的任意路径都给首页（避免 /**、/index 等写法 404）
        if not path.startswith("/api"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, "index.html 读取失败: %s" % e, "text/plain; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        return self.do_GET()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_parser():
    p = argparse.ArgumentParser(description="小车网页遥控面板（rospy + 标准库 HTTP）")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--cmd-topic", default="/cmd_vel")
    p.add_argument("--odom-topic", default="/odom")
    p.add_argument("--scan-topic", default="/scan")
    p.add_argument("--voltage-topic", default="/PowerVoltage")
    p.add_argument("--max-linear", type=float, default=0.20, help="网页允许的最大线速度 m/s（默认 0.2）")
    p.add_argument("--max-angular", type=float, default=0.60, help="网页允许的最大角速度 rad/s（默认 0.6）")
    p.add_argument("--stop-distance", type=float, default=0.35, help="前方避障停车距离（默认 0.35m）")
    p.add_argument("--max-tilt", type=float, default=3.0, help="倾角上限（默认 3°，0=关闭）")
    p.add_argument("--watchdog", type=float, default=0.6, help="指令看门狗（默认 0.6s 无指令就停车）")
    p.add_argument("--dry-run", action="store_true", help="只记录不发布（车不会动）")
    p.add_argument("--no-scan", dest="use_scan", action="store_false", default=True,
                   help="不用雷达避障（调试用）")
    p.add_argument("--verbose", action="store_true", help="打印每个 HTTP 请求")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    rospy.init_node("robot_web", anonymous=False, disable_signals=False)
    state = RobotState(args)
    pub = rospy.Publisher(args.cmd_topic, Twist, queue_size=5)

    rospy.Subscriber(args.odom_topic, Odometry, state.on_odom, queue_size=10)
    rospy.Subscriber("/imu", Imu, state.on_imu, queue_size=10)
    if args.use_scan:
        rospy.Subscriber(args.scan_topic, LaserScan, state.on_scan, queue_size=5)
    if args.voltage_topic:
        rospy.Subscriber(args.voltage_topic, Float32, state.on_voltage, queue_size=5)

    th = threading.Thread(target=control_loop, args=(state, pub))
    th.daemon = True
    th.start()

    Handler.state = state
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = os.environ.get("ROS_HOSTNAME") or "127.0.0.1"
    print("=" * 62)
    print("小车网页遥控面板已启动")
    print("  控制页：  http://%s:%d/" % (ip, args.port))
    print("  限速：    线速度 ≤ %.2f m/s  角速度 ≤ %.2f rad/s" % (args.max_linear, args.max_angular))
    print("  安全：    看门狗 %.1fs ｜ 前方避障 %.2fm ｜ 倾角 %.1f° ｜ 模式：%s"
          % (args.watchdog, args.stop_distance, args.max_tilt,
             "DRY-RUN（不动车）" if args.dry_run else "实车"))
    print("  摄像头：  用 web_video_server 另外提供 MJPEG（默认页面里指向 8081 端口）")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 停车并关闭")
    finally:
        try:
            pub.publish(Twist())
        except Exception:
            pass
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
