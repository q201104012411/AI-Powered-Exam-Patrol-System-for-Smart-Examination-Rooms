import cv2
import socket
import platform
from flask import Flask, Response

app = Flask(__name__)

# Windows 用 CAP_DSHOW 打开更快
if platform.system() == "Windows":
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
else:
    cap = cv2.VideoCapture(0)

if not cap.isOpened():
    raise RuntimeError("摄像头打开失败，检查权限或是否被其他程序占用")

def gen_frames():
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")

@app.route("/")
def index():
    return '<h1>本机摄像头监控</h1><img src="/video_feed" width="640">'

@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

if __name__ == "__main__":
    ip = get_ip()
    print("本机监控地址: http://127.0.0.1:5000")
    print("局域网监控地址: http://%s:5000" % ip)
    print("视频流地址: http://%s:5000/video_feed" % ip)
    app.run(host="0.0.0.0", port=5000, threaded=True)