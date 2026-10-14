# -*- coding: utf-8 -*-
r"""
USB 摄像头推流服务（camera_stream.py 的 USB 专用版）

和 camera_stream.py 的唯一区别是**挑摄像头的方式**：
    camera_stream.py    ：默认开 0 号，能打开就推流（0 号经常是内置摄像头，
                          隐私挡板一关就给你**全黑画面**，服务却报"已打开成功"）。
    camera_streamUSB.py ：逐个序号试，必须满足"能打开 + 能读到帧 + **画面不是全黑**"，
                          才认为这一路可用；优先试 1 号（实测本机 USB 摄像头是 1 号），
                          挑到就按 USB 摄像头的方式打开（DirectShow + 请求 MJPG +
                          指定分辨率/帧率 + 丢掉开头几帧）。

对外接口与 camera_stream.py **完全一致**：
    http://<本机IP>:<端口>/video_feed     MJPEG 流（02/03/11 里直接填这个地址）
    http://127.0.0.1:<端口>/              简易播放页
端口默认同样是 5000 —— 想换成 USB 摄像头，**停掉旧的 camera_stream.py，起这个就行**，
系统里已经填好的地址不用改。

用法：
    python camera_streamUSB.py                 # 自动挑 USB 摄像头（优先 1 号），端口 5000
    python camera_streamUSB.py --scan          # 只扫描：每个序号能不能用、是否全黑，不启服务
    python camera_streamUSB.py --device 1      # 指定序号
    python camera_streamUSB.py --port 5001     # 换端口（旧的 5000 还留着时用这个）
    python camera_streamUSB.py --width 1280 --height 720 --fps 30
    python camera_streamUSB.py --test-pattern  # 强制彩条（没有摄像头时验证链路）

注意：摄像头同一时刻只能被一个程序占用。启动前请先关掉 camera_stream.py / 巡考界面，
否则会看到"打不开（被占用）"或"读不到画面"。
"""
import argparse
import os
import platform
import socket
import sys
import time

import cv2
import numpy as np
from flask import Flask, Response

app = Flask(__name__)

DEFAULT_PORT = 5000           # 与 camera_stream.py 一致：换文件不用改系统里的地址
DEFAULT_DEVICE = 1            # 实测本机 USB 摄像头是 1 号（0 号是内置、可能全黑）
SCAN_MAX_INDEX = 5            # 最多试到几号
WARMUP = 10                   # USB 摄像头开头几帧常是黑的/没曝光，丢掉
BLACK_MEAN = 10.0             # 平均亮度低于这个值 + 几乎全黑像素 → 判"全黑"
BLACK_RATIO = 0.97

CAP = None                    # 当前使用的 VideoCapture（None = 用彩条）
USE_TEST_PATTERN = False
LOCAL_IP = "127.0.0.1"
PORT = DEFAULT_PORT
CAP_INDEX = -1
CAP_DESC = ""


def log(msg):
    print("[USB摄像头] %s" % msg, flush=True)


# ---------------------------------------------------------------------
#  设备识别：能打开 ≠ 看得见东西，必须查"是不是全黑"
# ---------------------------------------------------------------------
def windows_camera_devices():
    """Windows 认到的摄像头设备名（仅用于打印，帮助人对号入座）"""
    if platform.system() != "Windows":
        return []
    try:
        import json
        import subprocess
        ps = ("Get-PnpDevice -Class Camera,Image -ErrorAction SilentlyContinue | "
              "Where-Object { $_.Status -eq 'OK' } | "
              "Select-Object -ExpandProperty FriendlyName | ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25)
        text = (out.stdout or "").strip()
        if not text:
            return []
        data = json.loads(text)
        return [data] if isinstance(data, str) else list(data)
    except Exception:
        return []


def _open_capture(idx, backend_dshow=True):
    """按 USB 摄像头的方式打开：Windows 优先 DirectShow"""
    if backend_dshow and platform.system() == "Windows" and hasattr(cv2, "CAP_DSHOW"):
        cap = cv2.VideoCapture(int(idx), cv2.CAP_DSHOW)
        if cap is not None and cap.isOpened():
            return cap, "DirectShow"
        try:
            cap.release()
        except Exception:
            pass
    cap = cv2.VideoCapture(int(idx))
    return cap, "默认后端"


def _apply_params(cap, width, height, fps):
    """请求 MJPG + 分辨率/帧率：USB 摄像头不请求 MJPG 时 720p 往往掉到几帧"""
    if cap is None:
        return
    try:
        if hasattr(cv2, "VideoWriter_fourcc"):
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    if fps:
        cap.set(cv2.CAP_PROP_FPS, int(fps))


def _frame_stats(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    dark = float((gray < 12).mean())
    return mean, dark


def probe(idx, width=0, height=0, fps=0, warmup=WARMUP):
    """试一个序号 → dict(ok, black, w, h, fps, backend, mean, frame, note)"""
    t0 = time.time()
    cap, backend = _open_capture(idx)
    if cap is None or not cap.isOpened():
        try:
            cap.release()
        except Exception:
            pass
        return {"index": idx, "ok": False, "black": False, "backend": backend,
                "note": "打不开（没这个设备，或被其它程序占用）",
                "ms": (time.time() - t0) * 1000, "frame": None}
    _apply_params(cap, width, height, fps)
    frame, got = None, 0
    t_read = time.time()
    for _ in range(max(1, warmup)):
        ok, f = cap.read()
        if ok and f is not None and getattr(f, "size", 0) > 0:
            frame, got = f, got + 1
    read_s = max(1e-3, time.time() - t_read)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    try:
        cap.release()
    except Exception:
        pass
    info = {"index": idx, "ok": frame is not None, "w": w, "h": h,
            "fps": got / read_s, "backend": backend, "frame": frame,
            "ms": (time.time() - t0) * 1000, "note": "", "black": False,
            "mean": None}
    if frame is None:
        info["note"] = "能打开但读不到画面（%d 帧 0 帧有效）" % warmup
        return info
    mean, dark = _frame_stats(frame)
    info["mean"] = mean
    if mean < BLACK_MEAN and dark > BLACK_RATIO:
        info["black"] = True
        info["note"] = ("画面全黑（平均亮度 %.1f）：物理隐私挡板 / 镜头被遮 / 红外设备"
                        % mean)
    return info


def scan(width=0, height=0, fps=0, max_index=SCAN_MAX_INDEX, preferred=DEFAULT_DEVICE):
    """打印每个序号的可用性 → 返回可用列表（非全黑优先）"""
    names = windows_camera_devices()
    print("=" * 78)
    print("USB 摄像头扫描  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)
    if names:
        print("Windows 认到的摄像头设备：")
        for i, n in enumerate(names, 1):
            print("  %d) %s" % (i, n))
        print("  （设备清单顺序 ≠ OpenCV 序号，看下面逐个试的结果）")
    print()
    usable, dark, results = [], [], []
    for idx in range(0, max(0, max_index) + 1):
        info = probe(idx, width, height, fps)
        results.append(info)
        if info["ok"] and not info["black"]:
            usable.append(info)
            print("  %d 号：可用 ✅  %dx%d  实测 %.1f fps  %s"
                  % (idx, info["w"], info["h"], info["fps"], info["backend"]))
        elif info["ok"] and info["black"]:
            dark.append(info)
            print("  %d 号：⚠ 全黑   %dx%d  %s" % (idx, info["w"], info["h"], info["note"]))
        else:
            print("  %d 号：—      %s" % (idx, info["note"]))
    if not usable:
        print("\n没有「能看到画面」的摄像头。")
        if dark:
            print("  全黑序号：%s —— 先检查内置摄像头的物理隐私挡板 / "
                  "Windows 隐私设置 → 摄像头" % "、".join(str(d["index"]) for d in dark))
        print("  也可以先 --test-pattern 起彩条，确认链路通不通。")
    else:
        pick = usable[0]
        for u in usable:
            if u["index"] == preferred:
                pick = u
                break
        print("\n能出画面的序号：%s"
              % "、".join("%d 号(%dx%d@%.0ffps)" % (u["index"], u["w"], u["h"], u["fps"])
                          for u in usable))
        print("本工具默认会用：%d 号（--device 可改）；"
              "哪个是 USB 外接、哪个是内置，用 --preview 或 摄像头检测.py 的快照确认"
              % pick["index"])
    return usable, dark, results


def pick_usb_camera(preferred, width, height, fps, max_index):
    """挑一路能出画面的摄像头：先试 preferred，再 0..max；跳过全黑。"""
    order = [preferred] + [i for i in range(0, max_index + 1) if i != preferred]
    first_any = None
    for idx in order:
        info = probe(idx, width, height, fps)
        if info["ok"] and not info["black"]:
            cap, backend = _open_capture(idx)
            if cap is not None and cap.isOpened():
                _apply_params(cap, width, height, fps)
                for _ in range(WARMUP):          # 丢掉开头几帧（USB 常见黑帧）
                    cap.read()
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                return cap, idx, "%s  %dx%d  约%.1f fps" % (backend, w, h, info["fps"])
            continue
        if info["ok"] and first_any is None:
            first_any = (idx, info)
    if first_any is not None:                    # 全是黑帧：如实说明后仍给一个（可当占位）
        idx, info = first_any
        cap, backend = _open_capture(idx)
        if cap is not None and cap.isOpened():
            _apply_params(cap, width, height, fps)
            for _ in range(WARMUP):
                cap.read()
            return cap, idx, "%s（⚠ 画面全黑：%s）" % (backend, info["note"])
    return None, -1, ""


# ---------------------------------------------------------------------
#  推流
# ---------------------------------------------------------------------
def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def make_test_frame(w=640, h=480, t=0):
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    bar_w = max(1, w // 7)
    colors = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
              (255, 0, 255), (255, 0, 0), (0, 0, 255)]
    for i, c in enumerate(colors):
        frame[:, i * bar_w:(i + 1) * bar_w] = c
    x = int((t * 60) % (w + 200)) - 100
    cv2.putText(frame, "USB CAM TEST PATTERN", (max(0, x), h // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 3)
    cv2.putText(frame, time.strftime("%Y-%m-%d %H:%M:%S"), (20, h - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return frame


def gen_frames(cap, use_test_pattern):
    t0 = time.time()
    fails = 0
    while True:
        if use_test_pattern or cap is None:
            frame = make_test_frame(t=time.time() - t0)
        else:
            ok, frame = cap.read()
            if not ok or frame is None:
                fails += 1
                if fails in (30, 300):           # 不刷屏，但如实报
                    log("读不到画面（已连续 %d 次）——摄像头可能被抢占或断开了" % fails)
                time.sleep(0.02)
                continue
            fails = 0
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")


@app.route("/")
def index():
    desc = CAP_DESC or ("测试彩条（没有可用摄像头）" if USE_TEST_PATTERN else "未知")
    return ('<h1>USB 摄像头推流</h1>'
            '<p>当前设备：<b>%s</b>（序号 %s）</p>'
            '<p>在 02/03/11 的「实时监控」里填这个地址即可播放：</p>'
            '<code>http://%s:%s/video_feed</code><br>'
            '<img src="/video_feed" width="640">'
            '<p style="color:#888">换摄像头：停掉本服务，改 --device 序号后再启动；'
            '先用 <code>python camera_streamUSB.py --scan</code> 看哪个序号有画面。</p>'
            ) % (desc, CAP_INDEX, LOCAL_IP, PORT)


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(CAP, USE_TEST_PATTERN),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def main():
    global CAP, USE_TEST_PATTERN, LOCAL_IP, PORT, CAP_INDEX, CAP_DESC

    parser = argparse.ArgumentParser(description="USB 摄像头 MJPEG 推流（自动挑有画面的那一路）")
    parser.add_argument("--device", type=int, default=None,
                        help="摄像头序号；不填=自动挑（优先 %d 号）" % DEFAULT_DEVICE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口，默认 5000")
    parser.add_argument("--width", type=int, default=0, help="请求宽度（如 1280）")
    parser.add_argument("--height", type=int, default=0, help="请求高度（如 720）")
    parser.add_argument("--fps", type=int, default=0, help="请求帧率（如 30）")
    parser.add_argument("--max-index", dest="max_index", type=int, default=SCAN_MAX_INDEX,
                        help="自动挑时最多试到几号（默认 %d）" % SCAN_MAX_INDEX)
    parser.add_argument("--scan", action="store_true", help="只扫描，不启动服务")
    parser.add_argument("--test-pattern", dest="test_pattern", action="store_true",
                        help="强制输出彩条（没有摄像头时验证链路）")
    args = parser.parse_args()

    env_dev = os.environ.get("EXAM_USB_CAMERA_INDEX", "").strip()
    preferred = args.device if args.device is not None else (
        int(env_dev) if env_dev.isdigit() else DEFAULT_DEVICE)

    if args.scan:
        usable, dark, _ = scan(args.width, args.height, args.fps, args.max_index,
                               preferred)
        return 0 if usable else 1

    PORT = args.port
    LOCAL_IP = get_ip()

    if args.test_pattern:
        USE_TEST_PATTERN, CAP, CAP_INDEX, CAP_DESC = True, None, -1, "测试彩条"
    else:
        CAP, CAP_INDEX, CAP_DESC = pick_usb_camera(preferred, args.width, args.height,
                                                   args.fps, args.max_index)
        black = "全黑" in CAP_DESC
        if CAP is None or black:
            # 挑不到"能看到画面"的摄像头时**不推黑屏**：
            # 推黑屏会让人对着黑画面反复排查系统，而真正的原因往往是隐私挡板/被占用。
            if CAP is not None:
                try:
                    CAP.release()
                except Exception:
                    pass
            CAP, USE_TEST_PATTERN, CAP_INDEX = None, True, -1
            why = CAP_DESC if black else "没有可用的摄像头（打不开或被占用）"
            CAP_DESC = "测试彩条（原因为：%s）" % why
            log("⚠ %s" % why)
            log("⚠ 因此改用**彩条测试画面**，避免你对着黑屏排查。处理办法：")
            log("   1) 内置摄像头：推开旁边的物理隐私挡板；Windows 设置 → 隐私和安全性 → "
                "摄像头 → 允许桌面应用访问")
            log("   2) 若是被占用：关掉巡考界面/相机 App 后重跑")
            log("   3) 先看清哪个序号有画面：python camera_streamUSB.py --scan")
        else:
            log("✅ 已开始推流：序号 %d（%s）" % (CAP_INDEX, CAP_DESC))

    print()
    print("本机监控地址   : http://127.0.0.1:%d" % PORT)
    print("局域网监控地址 : http://%s:%d" % (LOCAL_IP, PORT))
    print("视频流地址     : http://%s:%d/video_feed" % (LOCAL_IP, PORT))
    print("（02/03/11 里就填「视频流地址」；与 camera_stream.py 端口一致，换文件不用改配置）")
    sys.stdout.flush()
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    except OSError as e:
        # 最常见的失败就是端口被占：旧的 camera_stream.py 还在跑
        log("启动失败：端口 %d 起不来（%s）" % (PORT, e))
        log("处理：先停掉旧的 camera_stream.py / 其它推流服务；"
            "或换端口启动：python camera_streamUSB.py --port 5001")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
