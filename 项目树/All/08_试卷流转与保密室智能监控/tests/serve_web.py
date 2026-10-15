# -*- coding: utf-8 -*-
"""以 Windows 原生「分离进程」方式启动网页面板，不随本 shell 退出而终止。

用法：
    python tests/serve_web.py            # 启动（已在跑则直接返回）
    python tests/serve_web.py --stop     # 停止
    python tests/serve_web.py --port 5020
"""
import argparse
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
APP = os.path.join(ROOT, "网页监控面板", "server", "app.py")
PIDFILE = os.path.join(ROOT, "data", "web.pid")
LOGFILE = os.path.join(ROOT, "data", "web.log")

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def port_open(host, port):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect((host, port))
        return True
    except Exception:                                        # noqa: BLE001
        return False
    finally:
        s.close()


def stop(host, port):                                        # noqa: ARG001
    pid = None
    if os.path.exists(PIDFILE):
        try:
            pid = int(open(PIDFILE).read().strip())
        except Exception:                                    # noqa: BLE001
            pid = None
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        print("已停止进程树 PID=%s" % pid)
    else:
        print("没有记录到 PID；如仍有占用，请手动结束监听 5020 的进程")
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()

    if a.stop:
        stop(a.host, a.port)
        return 0

    if port_open(a.host, a.port):
        print("端口 %d 已在监听，服务应该已经在跑，无需重复启动" % a.port)
        return 0

    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    logf = open(LOGFILE, "ab", buffering=0)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen([PY, APP, "--host", a.host, "--port", str(a.port)],
                            cwd=ROOT, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                            **kw)
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))

    for _ in range(40):
        if port_open(a.host, a.port):
            print("已启动：http://%s:%d/  PID=%d" % (a.host, a.port, proc.pid))
            print("日志：%s" % LOGFILE)
            return 0
        time.sleep(0.25)
    print("启动超时，请查看日志：%s" % LOGFILE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
