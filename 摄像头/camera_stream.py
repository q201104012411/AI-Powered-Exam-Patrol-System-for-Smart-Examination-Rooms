# -*- coding: utf-8 -*-
"""
本机摄像头推流测试服务

用法：
    python camera_stream.py              # 默认摄像头，端口 5000
    python camera_stream.py --port 8080  # 指定端口
    python camera_stream.py --device 1   # 指定摄像头索引

输出：
    http://<本机IP>:<端口>/video_feed    # OpenCV / 浏览器可直接播放的 MJPEG 流
    http://<本机IP>:<端口>/              # 简易播放页面

无物理摄像头时会自动生成移动测试彩条，方便在没有摄像头的机器上验证监控页面。
"""
import argparse
import os
import socket
import sys
import time
import platform

import cv2
import numpy as np
from flask import Flask, Response

app = Flask(__name__)


def open_camera(device_idx):
    """尝试打开指定摄像头；Windows 用 CAP_DSHOW 更快"""
    if platform.system() == "Windows":
        cap = cv2.VideoCapture(int(device_idx), cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(int(device_idx))
    return cap


def find_camera(preferred=0):
    """优先打开指定索引；失败则尝试 0~2；都没有则返回 None"""
    for idx in [preferred] + list(range(3)):
        cap = open_camera(idx)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap, idx
        cap.release()
    return None, -1


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
    """没有摄像头时生成移动彩条测试画面"""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    # 垂直彩条
    bar_w = w // 7
    colors = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
              (255, 0, 255), (255, 0, 0), (0, 0, 255)]
    for i, c in enumerate(colors):
        frame[:, i * bar_w:(i + 1) * bar_w] = c
    # 移动文字
    x = int((t * 60) % (w + 200)) - 100
    cv2.putText(frame, "TEST STREAM - NO CAMERA", (x, h // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    cv2.putText(frame, time.strftime("%Y-%m-%d %H:%M:%S"), (20, h - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return frame


def gen_frames(cap, use_test_pattern):
    import numpy as np
    t0 = time.time()
    while True:
        if use_test_pattern:
            frame = make_test_frame(t=time.time() - t0)
        else:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")


@app.route("/")
def index():
    return ('<h1>本机摄像头监控</h1>'
            '<p>在 02/03 智能终端「实时监控」页签填入下方地址即可播放：</p>'
            '<code>http://%s:%s/video_feed</code><br>'
            '<img src="/video_feed" width="640">') % (LOCAL_IP, PORT)


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(CAP, USE_TEST_PATTERN),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def main():
    global CAP, USE_TEST_PATTERN, LOCAL_IP, PORT

    parser = argparse.ArgumentParser(description="本机摄像头 MJPEG 推流")
    parser.add_argument("--device", type=int, default=0, help="摄像头索引，默认 0")
    parser.add_argument("--port", type=int, default=5000, help="服务端口，默认 5000")
    args = parser.parse_args()

    CAP, idx = find_camera(args.device)
    USE_TEST_PATTERN = CAP is None
    PORT = args.port
    LOCAL_IP = get_ip()

    if USE_TEST_PATTERN:
        print("[WARN] 未检测到可用摄像头，将输出测试彩条画面")
    else:
        print("[OK] 已打开摄像头索引：%d" % idx)

    print("本机监控地址: http://127.0.0.1:%d" % PORT)
    print("局域网监控地址: http://%s:%d" % (LOCAL_IP, PORT))
    print("视频流地址: http://%s:%d/video_feed" % (LOCAL_IP, PORT))

    # 强制 stdout 刷出，避免 pythonw 下看不到地址
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
