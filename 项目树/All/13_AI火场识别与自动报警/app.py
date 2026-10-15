# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 程序入口

用法：
    python app.py                      # 打开监控界面（默认启动即开始监控）
    python app.py --background         # 启动后直接隐藏到托盘，只在后台盯（平时就这么用）
    python app.py --simulate           # 打开界面并立刻做一次"模拟火警"演练
    python app.py --headless           # **没有界面**：只检测+落库+写日志（适合开机自启/服务化）
    python app.py --status             # 打印当前配置与历史统计（不启动）
    python app.py --test-alarm         # 造一条演练事件并走完整处置留痕（自检用）

说明：
    · "平时只在后台运行" = `python app.py --background`（或双击 启动.bat）
    · 发现火情时，无论界面是否隐藏，都会弹出火警窗口 + 响铃 + 留证据
    · --headless 不弹窗（没有 GUI 环境时用），但事件、证据、日志一样留
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)

import config  # noqa: E402


def cmd_status():
    """打印当前配置与历史统计。"""
    store_mod = __import__("storage")
    det_mod = __import__("detector")
    cfg = config.load_settings()
    store = store_mod.EventStore()
    print("=" * 74)
    print("AI 火场识别与自动报警 · 当前状态")
    print("=" * 74)
    for k, v in config.describe(cfg).items():
        print("  %-12s %s" % (k, v))
    print("  %-12s %s" % ("检测后端说明", det_mod.describe_choice(cfg)))
    s = store.summary()
    print("-" * 74)
    print("  事件总数 %(total)d ｜ 火警 %(alarm)d ｜ 疑似 %(warning)d ｜ "
          "报警中 %(firing)d ｜ 已解除 %(cleared)d ｜ 误报 %(false_alarm)d" % s)
    if s["last_epoch"]:
        print("  最近一次事件：%s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(s["last_epoch"])))
    print("=" * 74)
    return 0


def cmd_test_alarm(sim_seconds=6.0):
    """
    演练：不开界面，直接跑"检测器 -> 闸门 -> 落库 -> 证据"整条链。
    用合成火焰，且**不伪造事件**（真的靠检测器判出来）。
    """
    sys.path.insert(0, os.path.join(HERE))
    import detect as detect_mod
    import detector as detector_mod
    import storage as storage_mod
    import video as video_mod
    import numpy as np
    import cv2

    cfg = config.load_settings()
    store = storage_mod.EventStore()
    recorder = video_mod.EvidenceRecorder(cfg, store)
    recorder.start()

    fired = {"n": 0, "cleared": 0}

    def on_alarm(det, ev):
        fired["n"] += 1
        store.save_event(ev)
        store.log("火警触发（演练）", "%s 峰值 %.2f" % (ev.event_id, ev.peak_score),
                  "test-alarm")
        recorder.active_event = ev
        frame = _synth_fire(0, 480, 640)
        recorder.capture_event(ev, frame)

    def on_clear(ev):
        fired["cleared"] += 1
        store.save_event(ev)
        recorder.close_event(ev.event_id, save_clip=True)

    det, why = detector_mod.build_detector(cfg, on_alarm=on_alarm, on_clear=on_clear)
    print("检测后端：%s" % why)
    print("注入合成火焰 %.1f 秒，观察是否真的报警…" % sim_seconds)
    t0 = time.time()
    i = 0
    while time.time() - t0 < sim_seconds:
        frame = _synth_fire(time.time() - t0, 480, 640)
        det.update(frame, camera_name="演练合成源", camera_url="synthetic")
        recorder.push_frame(frame)
        i += 1
        time.sleep(0.08)
    last = det.last_detection
    print("-" * 74)
    print("注入 %d 帧 ｜ 末帧分数 %.3f ｜ 火色占比 %.3f%% ｜ 判定 %s"
          % (i, last.score, last.fire_ratio * 100, last.level_name()))
    print("报警次数 %d ｜ 解除次数 %d" % (fired["n"], fired["cleared"]))
    if det.active_event is not None:
        ev = det.active_event
        # 关键顺序：先让证据线程把路径回填完（它会写库并回填这个对象），
        # 再整行保存。反过来做的话，这次 save 会用"还没有路径的对象"覆盖掉库里刚写好的路径
        # （storage.save_event 现在也保护证据列，双保险）。
        recorder.active_event = ev
        _wait_evidence(recorder, store, event_id=ev.event_id)
        ev.clear(force=True)
        store.save_event(ev)
        recorder.close_event(ev.event_id, save_clip=True)
        print("（演练收尾：已把进行中的事件标记为已解除）")
    _wait_evidence(recorder, store)
    recorder.stop()
    evs = store.list_events(limit=3)
    if evs:
        e = evs[0]
        print("-" * 74)
        print("最近事件：%s ｜ %s ｜ 状态 %s" % (e.event_id, e.level_name(), e.status_text()))
        print("  抓拍图：%s" % _say_file(e.snapshot))
        print("  标注图：%s" % _say_file(e.marked))
        print("  录像  ：%s" % _say_file(e.clip))
        print("  证据目录：%s"
              % os.path.join(str(config.EVENT_DIR), e.event_id))
    print("=" * 74)
    return 0 if fired["n"] > 0 else 1


def _say_file(path):
    if path and os.path.isfile(path):
        return "%s（%.1f KB）" % (path, os.path.getsize(path) / 1024.0)
    return "（无）"


def _wait_evidence(recorder, store, timeout=6.0, event_id=None):
    """
    等证据写盘完成：队列空 + 没有进行中的录像 + 目标事件已有抓拍图。
    必须在"整行保存事件"之前调用 —— 否则会用没有路径的对象覆盖掉刚写好的路径。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = recorder.stats()
        if st.get("queued") == 0 and st.get("open_clips") == 0:
            ev = store.get_event(event_id) if event_id else (
                (store.list_events(limit=1) or [None])[0])
            if ev is None or ev.snapshot:
                # 把库里的路径同步回内存对象：它才是随后整行保存的来源，
                # 不同步的话那次保存仍会带上空路径（storage 层已兜底，这里是第一道）。
                live = recorder.active_event
                if ev is not None and live is not None and \
                        (event_id is None or live.event_id == event_id):
                    live.snapshot = ev.snapshot or live.snapshot
                    live.marked = ev.marked or live.marked
                    live.clip = ev.clip or live.clip
                return True
        time.sleep(0.2)
    return False


def _synth_fire(t, h, w):
    """合成一团会抖动的火（与 ui_main/*_SyntheticFireSource 同一画法）。"""
    import numpy as np
    import cv2
    img = np.full((h, w, 3), 190, np.uint8)
    cv2.rectangle(img, (30, 40), (w - 30, h - 40), (120, 120, 120), -1)
    cx, cy = w // 2, int(h * 0.62)
    j = int(7 * np.sin(t * 6.0)) + int(5 * np.cos(t * 9.7))
    rw, rh = 62, 104
    pts = np.array([[cx - rw, cy + rh], [cx - rw + 12 + j, cy - rh // 3],
                    [cx, cy - rh - 12 - j], [cx + rw - 8, cy - rh // 4],
                    [cx + rw, cy + rh]], np.int32)
    cv2.fillPoly(img, [pts], (0, 205, 255))
    cv2.circle(img, (cx + j // 2, cy + rh // 3), int(rh * 0.44), (60, 255, 255), -1)
    cv2.circle(img, (cx, cy + rh // 2), int(rh * 0.24), (255, 255, 255), -1)
    return img


def cmd_headless():
    """
    无界面常驻：只检测 + 落库 + 写日志（发现火情写一条醒目的日志）。
    没有弹窗 —— 需要弹窗请用带界面的 --background。
    """
    import storage as storage_mod
    import detector as detector_mod
    import video as video_mod

    cfg = config.load_settings()
    store = storage_mod.EventStore()
    recorder = video_mod.EvidenceRecorder(cfg, store)
    recorder.start()

    def on_alarm(det, ev):
        store.save_event(ev)
        store.log("火警触发", "%s %s 峰值 %.2f" % (ev.event_id, ev.level_name(),
                                                  ev.peak_score), "headless")
        print("\a[火警] %s %s 分值 %.2f —— %s"
              % (ev.created_at, ev.camera_name, ev.score, ev.reason))
        sys.stdout.flush()

    def on_clear(ev):
        store.save_event(ev)
        recorder.close_event(ev.event_id, save_clip=True)
        print("[解除] %s 持续 %.1f 秒" % (ev.event_id, ev.duration_sec))
        sys.stdout.flush()

    det, why = detector_mod.build_detector(cfg, on_alarm=on_alarm, on_clear=on_clear)
    worker = video_mod.VideoWorker(cfg, store=store, detector=det, recorder=recorder)
    print("AI 火场识别 · 无界面常驻模式")
    print("  摄像头：%s（%s）" % (cfg["camera_url"], cfg["camera_name"]))
    print("  检测后端：%s" % why)
    print("  按 Ctrl+C 退出")
    worker.start()
    try:
        while True:
            time.sleep(1.0)
            snap = worker.snapshot()
            det_now = snap.get("det")
            if det_now is not None and det_now.has_fire:
                print("[监视] %s ｜ %s" % (det_now.level_name(),
                                          snap.get("detector_status")))
    except KeyboardInterrupt:
        print("\n退出中…")
    finally:
        worker.stop("headless 退出")
        recorder.stop()
    return 0


def cmd_gui(background=False, simulate=False):
    import ui_main
    # 隐藏窗口的判定：
    #   --background  显式要求后台运行
    #   start_minimized  设置里勾了"启动即最小化"（默认开，符合"平时只在后台"）
    #   但 --simulate 是"我要看演练"，此时无论如何都把窗口显示出来
    hide = not simulate and (bool(background) or
                             bool(config.load_settings()["start_minimized"]))
    app = ui_main.FireApp(hidden=hide)
    if simulate:
        app.root.after(800, app.simulate_alarm)
    app.run()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI 火场识别与自动报警",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--background", action="store_true",
                    help="启动后隐藏到托盘，只在后台运行（平时这么用）")
    ap.add_argument("--simulate", action="store_true", help="打开界面并立刻演练一次火警")
    ap.add_argument("--headless", action="store_true", help="无界面常驻（只检测+落库+日志）")
    ap.add_argument("--status", action="store_true", help="打印当前状态，不启动")
    ap.add_argument("--test-alarm", action="store_true", help="造一次演练火警并走完整链路")
    ap.add_argument("--print-settings", action="store_true", help="打印当前设置 JSON")
    ns = ap.parse_args(argv)

    config.ensure_stdio()          # pythonw 启动时 stdout 是 None，先兜住再 print

    if ns.status:
        return cmd_status()
    if ns.print_settings:
        import json
        print(json.dumps(config.load_settings(), ensure_ascii=False, indent=2))
        return 0
    if ns.test_alarm:
        return cmd_test_alarm()
    if ns.headless:
        return cmd_headless()
    return cmd_gui(background=ns.background, simulate=ns.simulate)


if __name__ == "__main__":
    sys.exit(main())
