# -*- coding: utf-8 -*-
r"""
06 设备联调工具（接真设备之前先确认通路）

两种用场：

  ① 当"接收端"：把 06 的接入服务单独跑起来，真设备往这里推，屏幕上实时打印
        python 工具_设备联调.py --device GATE-A01 --proto http --port 9106
     然后在安检门侧把上报地址配成  http://<本机IP>:9106/api/pass
     （TCP 设备则连  <本机IP>:<端口>  丢一行 JSON 即可）

  ② 当"设备端"：验证对方接口能不能收（把 3 条样例推过去）
        python 工具_设备联调.py --send http://127.0.0.1:9106/api/pass

报文格式（sensors 也可平铺在顶层）：
  {"device_code":"GATE-A01","candidate_id":"...","seat_no":"07",
   "sensors":{"metal":0.71,"magnetic":0.20,"phone":0.05,
              "earphone":0.02,"millimeter":0.10,"shoe":0.03}}

说明：本工具写入的都是**真实来源 real**（因为它就是给真机联调用的）；
测试完可以用「⑦ 设置」页的清理功能按天清掉，或者直接删掉那几条过检记录。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config                                                # noqa: E402
import gate                                                  # noqa: E402
import models                                                # noqa: E402
import storage                                               # noqa: E402

SAMPLES = [
    {"device_code": "GATE-A01", "candidate_id": "DEMO-1", "candidate_name": "联调-放行",
     "seat_no": "01", "duration_ms": 820,
     "sensors": {"metal": 0.10, "magnetic": 0.05, "phone": 0.02, "earphone": 0.01,
                 "millimeter": 0.03, "shoe": 0.01}},
    {"device_code": "GATE-A01", "candidate_id": "DEMO-2", "candidate_name": "联调-手机",
     "seat_no": "02", "duration_ms": 900,
     "sensors": {"metal": 0.48, "magnetic": 0.12, "phone": 0.88, "earphone": 0.04,
                 "millimeter": 0.30, "shoe": 0.02}},
    {"device_code": "GATE-A01", "candidate_id": "DEMO-3", "candidate_name": "联调-强磁",
     "seat_no": "03", "duration_ms": 870,
     "sensors": {"metal": 0.72, "magnetic": 0.92, "phone": 0.10, "earphone": 0.03,
                 "millimeter": 0.20, "shoe": 0.02}},
]


def on_event(ev, alarm):
    line = "[%s] %s %s -> %s %s" % (
        time.strftime("%H:%M:%S"), ev.device_code,
        ev.candidate_name or ev.candidate_id or "-",
        "报警" if ev.result == "alarm" else "放行",
        ("%s %s" % (alarm.level_text(), alarm.kinds_text())) if alarm else "")
    print(line)
    print("        感应值：%s" % json.dumps(ev.scores, ensure_ascii=False))


def send(url, timeout=3.0):
    """当设备端：把样例推给对端接口。"""
    import urllib.error
    import urllib.request
    ok_all = True
    for s in SAMPLES:
        try:
            req = urllib.request.Request(
                url, data=json.dumps(s, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            print("PASS %s -> %s" % (s["candidate_name"], body.strip()[:120]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            ok_all = False
            print("FAIL %s -> %s" % (s["candidate_name"], str(e)[:120]))
    return 0 if ok_all else 1


def main():
    # 现场联调时输出必须**立刻可见**（默认块缓冲会让人以为没收到数据）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="GATE-A01", help="设备编码（接收端模式）")
    ap.add_argument("--proto", default="http", choices=["http", "tcp"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9106)
    ap.add_argument("--send", default="", help="设备端模式：把样例推给这个 URL")
    ns = ap.parse_args()

    if ns.send:
        print("设备端模式：推送 3 条样例到 %s" % ns.send)
        return send(ns.send)

    store = storage.Store()
    dev = store.get_device(code=ns.device)
    if dev is None:
        dev = models.Device(code=ns.device, name="联调设备 %s" % ns.device,
                            kind=config.DEV_GATE, proto=ns.proto,
                            address="%s:%d" % (ns.host, ns.port))
        store.save_device(dev)
        print("已登记联调设备：%s" % ns.device)

    cls = gate.HttpIngest if ns.proto == "http" else gate.TcpIngest
    runner = cls(store, ns.device, host=ns.host, port=ns.port, on_event=on_event)
    ok = runner.start()
    if ok is False:
        print("启动失败：%s" % getattr(runner, "reason", "端口被占用？"))
        return 1
    print("=" * 70)
    print("接收端已启动：%s" % runner.status()["listen"])
    if ns.proto == "http":
        print("设备侧上报地址：http://<本机IP>:%d/api/pass   （POST，一行 JSON）"
              % ns.port)
        print("探活：http://<本机IP>:%d/health" % ns.port)
    print("数据目录：%s" % config.DATA_ROOT)
    print("按 Ctrl+C 退出（收到的过检会真实入库，source=real）")
    print("=" * 70)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止接收。")
    finally:
        runner.stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
