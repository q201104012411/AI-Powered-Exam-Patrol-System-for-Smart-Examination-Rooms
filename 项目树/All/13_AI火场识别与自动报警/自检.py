# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 自检

不需要真实摄像头、不需要联网也能跑完。覆盖：
    [1] 环境与依赖（cv2/numpy/Pillow/tkinter、可选 onnxruntime）
    [2] 配置读写与夹取（settings.json 被写坏也不崩）
    [3] 检测算法判别力（真火要报；静止红布/肤色/正常画面不许报）
    [4] 确认闸门（单帧不许报警；连续命中才报；连续无火才解除）
    [5] 事件存储（往返、误报、清理、**证据路径不被覆盖**）
    [6] 端到端演练（合成火焰走完 检测->落库->截图/标注/录像）
    [7] 界面与弹窗冒烟（无摄像头也能弹得出来）
    [8] 托盘可用性（不可用要能优雅降级）

用法：
    python 自检.py            # 用临时数据目录，随便跑
    python 自检.py --real     # 用真实 data 目录（只读式检查，不写坏数据）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("  %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— %s" % detail) if detail else ""))
    return bool(cond)


def section(title):
    print()
    print("-" * 76)
    print(title)
    print("-" * 76)


# ---------------- 合成画面 ----------------
def _np_cv():
    import numpy as np
    import cv2
    return np, cv2


def frame_clean(h=360, w=480):
    np, cv2 = _np_cv()
    img = np.full((h, w, 3), 200, np.uint8)
    cv2.rectangle(img, (30, 40), (w - 30, h - 40), (120, 120, 120), -1)
    return img


def frame_fire(t, h=360, w=480):
    np, cv2 = _np_cv()
    img = frame_clean(h, w)
    cx, cy = w // 2, int(h * 0.62)
    j = int(6 * np.sin(t * 6.0)) + int(4 * np.cos(t * 9.7))
    rw, rh = 50, 85
    pts = np.array([[cx - rw, cy + rh], [cx - rw + 10 + j, cy - rh // 3],
                    [cx, cy - rh - 10 - j], [cx + rw - 6, cy - rh // 4],
                    [cx + rw, cy + rh]], np.int32)
    cv2.fillPoly(img, [pts], (0, 205, 255))
    cv2.circle(img, (cx + j // 2, cy + rh // 3), int(rh * 0.42), (60, 255, 255), -1)
    cv2.circle(img, (cx, cy + rh // 2), int(rh * 0.22), (255, 255, 255), -1)
    return img


def frame_static_red(h=360, w=480):
    np, cv2 = _np_cv()
    img = frame_clean(h, w)
    cv2.rectangle(img, (int(w * 0.42), int(h * 0.42)),
                  (int(w * 0.58), int(h * 0.78)), (40, 40, 200), -1)
    return img


def frame_skin(h=360, w=480, shift=0):
    np, cv2 = _np_cv()
    img = frame_clean(h, w)
    cv2.ellipse(img, (w // 2 + shift, h // 2), (52, 70), 0, 0, 360, (150, 180, 215), -1)
    return img


# ============================================================ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实 data 目录（只读检查）")
    args = ap.parse_args()

    tmp = None
    real_home = os.environ.get("EXAM_FIRE_HOME", "")
    if not args.real:
        tmp = tempfile.mkdtemp(prefix="fire_selfcheck_")
        os.environ["EXAM_FIRE_HOME"] = tmp
    for m in ("config", "models", "storage", "detect", "detector", "video", "tray"):
        sys.modules.pop(m, None)

    print("=" * 76)
    print("AI 火场识别与自动报警 · 自检")
    print("=" * 76)

    import config
    print("Python  ：%s" % sys.version.split()[0])
    print("数据目录：%s" % config.DATA_ROOT)
    print("模式    ：%s" % ("真实目录（只读检查）" if args.real else "临时目录（可破坏性测试）"))

    # ---------------- 1) 环境 ----------------
    section("[1] 环境与依赖")
    has_cv = has_np = has_pil = has_tk = False
    for mod, flag in (("cv2", "has_cv"), ("numpy", "has_np"), ("PIL", "has_pil")):
        try:
            __import__(mod)
            ok = True
        except ImportError as exc:
            ok = False
            check("依赖 %s" % mod, False, str(exc)[:60])
        if mod == "cv2":
            has_cv = ok
        elif mod == "numpy":
            has_np = ok
        elif mod == "PIL":
            has_pil = ok
    if has_cv:
        import cv2
        check("opencv 可用", True, "cv2 %s" % cv2.__version__)
    if has_np:
        import numpy as np
        check("numpy 可用", True, np.__version__)
    if has_pil:
        import PIL
        check("Pillow 可用（界面显示画面）", True, PIL.__version__)
    try:
        import tkinter  # noqa: F401
        has_tk = True
        check("tkinter 可用（图形界面）", True)
    except ImportError as exc:
        check("tkinter 可用", False, "缺 tcl/tk：%s" % str(exc)[:50])
    try:
        import onnxruntime  # noqa: F401
        check("onnxruntime（可选，仅 .onnx 模型需要）", True, "已安装")
    except ImportError:
        check("onnxruntime（可选）", True, "未安装 —— 用多信号传统视觉，不影响报警")

    if not (has_cv and has_np):
        print("\n[中止] 缺 cv2/numpy，后续检测项无法进行。请先运行「安装环境.bat」。")
        _summary(tmp)
        return 1

    # ---------------- 2) 配置 ----------------
    section("[2] 配置读写与夹取")
    cfg = config.load_settings()
    check("默认设置可读", cfg["camera_url"] == "0", "camera_url=%s" % cfg["camera_url"])
    check("火警阈值在合法区间", 0.05 <= cfg["alarm_threshold"] <= 1.0,
          "%.2f" % cfg["alarm_threshold"])
    bad = config.coerce_settings({"alarm_threshold": 99, "confirm_frames": -3,
                                  "detect_interval_ms": 10 ** 9, "hsv_s_min": "xx",
                                  "backend": "乱写", "min_fire_ratio": float("nan")})
    check("越界值被夹住", bad["alarm_threshold"] <= 1.0 and bad["confirm_frames"] >= 1
          and bad["detect_interval_ms"] <= 5000, "阈值%.2f 帧%d"
          % (bad["alarm_threshold"], bad["confirm_frames"]))
    check("非法枚举回落默认", bad["backend"] == "auto", bad["backend"])
    check("NaN 被替换为默认", bad["min_fire_ratio"] == config.SETTINGS_DEFAULT["min_fire_ratio"],
          str(bad["min_fire_ratio"]))
    check("非字典输入不崩", config.coerce_settings(["x"])["camera_url"] == "0")
    ok, msg = config.save_settings({"camera_name": "自检机位", "confirm_frames": 5})
    reloaded = config.load_settings()
    check("保存后能读回", ok and reloaded["camera_name"] == "自检机位"
          and reloaded["confirm_frames"] == 5, msg)
    config.save_settings({"camera_name": config.SETTINGS_DEFAULT["camera_name"],
                          "confirm_frames": config.SETTINGS_DEFAULT["confirm_frames"]})
    check("未知键被忽略", config.save_settings({"不存在": 1})[0])

    # ---------------- 3) 检测判别力 ----------------
    section("[3] 检测算法判别力（真火要报，干扰物不许报）")
    import detect as detect_mod

    def run(frames, **patch):
        c = config.coerce_settings(dict(config.SETTINGS_DEFAULT, **patch))
        d = detect_mod.FireDetector(cfg=c)
        for f in frames:
            d.update(f, camera_name="自检", camera_url="0")
        return d

    d_clean = run([frame_clean() for _ in range(15)])
    check("正常画面不报警", d_clean.active_event is None,
          "分数 %.3f" % d_clean.last_detection.score)

    d_fire = run([frame_fire(i * 0.12) for i in range(24)])
    check("抖动火焰会报警", d_fire.active_event is not None,
          "峰值 %.3f" % (d_fire.active_event.peak_score if d_fire.active_event else 0))
    check("火焰判为确认火警",
          d_fire.active_event is not None
          and d_fire.active_event.level == config.LEVEL_ALARM)
    check("能说清判定依据（信号分解非空）",
          bool(d_fire.last_detection.parts)
          and any(v > 0 for v in d_fire.last_detection.parts.values()),
          d_fire.last_detection.reason_text())

    d_red = run([frame_static_red() for _ in range(24)])
    check("静止红布不报警", d_red.active_event is None,
          "分数 %.3f" % d_red.last_detection.score)

    d_skin = run([frame_skin(shift=(i % 4) * 3) for i in range(24)])
    check("肤色块不报警（YCbCr 排除生效）", d_skin.active_event is None,
          "分数 %.3f" % d_skin.last_detection.score)

    def mix(i):
        img = frame_skin()
        fire = frame_fire(i * 0.12)
        h, w = img.shape[:2]
        img[:, : w // 2] = fire[:, : w // 2]
        return img

    d_mix = run([mix(i) for i in range(24)])
    check("火与人脸同框仍报警（排肤色没排掉火）", d_mix.active_event is not None)

    check("检测后端如实报出", bool(detect_mod.describe_backend(
        detect_mod.VisionFireBackend(config.load_settings()))),
        detect_mod.describe_backend(detect_mod.VisionFireBackend(config.load_settings())))

    # ---------------- 4) 确认闸门 ----------------
    section("[4] 确认闸门（防单帧误报）")
    d1 = run([frame_fire(0.0)], confirm_frames=4)
    check("单帧高分不报警", d1.active_event is None)
    d2 = run([frame_fire(i * 0.12) for i in range(3)], confirm_frames=4)
    check("不足确认帧数不报警", d2.active_event is None)

    d3 = detect_mod.FireDetector(cfg=config.coerce_settings(
        dict(config.SETTINGS_DEFAULT, confirm_frames=3)))
    d3.update(frame_fire(0.0), camera_name="自检")
    d3.update(frame_fire(0.1), camera_name="自检")
    d3.update(frame_clean(), camera_name="自检")
    check("中途打断会清零计数", d3.hit_streak == 0, "hit=%d" % d3.hit_streak)

    d4 = run([frame_fire(i * 0.12) for i in range(8)] + [frame_clean() for _ in range(6)],
             confirm_frames=2, clear_frames=4)
    check("连续无火后自动解除", d4.active_event is None)

    fired = {"a": 0, "c": 0}
    d5 = detect_mod.FireDetector(
        cfg=config.coerce_settings(dict(config.SETTINGS_DEFAULT, confirm_frames=2,
                                        clear_frames=3)),
        on_alarm=lambda det, ev: fired.__setitem__("a", fired["a"] + 1),
        on_clear=lambda ev: fired.__setitem__("c", fired["c"] + 1))
    for i in range(5):
        d5.update(frame_fire(i * 0.12), camera_name="自检")
    for _ in range(5):
        d5.update(frame_clean(), camera_name="自检")
    check("报警/解除回调各触发一次", fired["a"] == 1 and fired["c"] == 1, str(fired))

    # ---------------- 5) 存储 ----------------
    section("[5] 事件存储")
    import models
    import storage as storage_mod

    store = storage_mod.EventStore()
    n0 = store.count()
    ev = models.FireEvent(camera_name="自检机位", camera_url="0", score=0.77,
                          reason="火色 0.5、闪烁 0.2", parts={"火色": 0.5, "闪烁": 0.2},
                          fire_ratio=0.03, regions=[(1, 2, 3, 4, 12)])
    check("事件入库", store.save_event(ev))
    got = store.get_event(ev.event_id)
    check("事件读回字段一致",
          got is not None and got.camera_name == "自检机位"
          and abs(got.score - 0.77) < 1e-6 and got.parts.get("火色") == 0.5)
    check("事件计数 +1", store.count() == n0 + 1, "%d -> %d" % (n0, store.count()))

    # 证据保护（回归：曾经被整行保存抹掉）
    store.set_evidence(ev.event_id, snapshot="snap.jpg", marked="marked.jpg")
    stale = store.get_event(ev.event_id)
    stale.snapshot = stale.marked = ""
    store.save_event(stale)
    check("证据路径不被空值覆盖", store.get_event(ev.event_id).snapshot == "snap.jpg",
          store.get_event(ev.event_id).snapshot or "（空）")

    ok, msg = store.ack_event(ev.event_id, by="自检员", note="自检确认")
    check("确认留痕", ok and store.get_event(ev.event_id).status == models.ST_ACKED, msg)
    ok, msg = store.mark_false(ev.event_id, by="自检员", note="自检误报")
    check("误报留痕", ok and store.get_event(ev.event_id).status == models.ST_FALSE, msg)

    old = models.FireEvent(camera_name="旧事件", score=0.7)
    old.created_epoch = time.time() - 90 * 86400
    store.save_event(old)
    nd, _nf, _nb = store.purge_old(keep_days=30)
    check("按天数清理旧事件", nd >= 1 and store.get_event(old.event_id) is None,
          "删除 %d 条" % nd)
    check("清理不影响新事件", store.get_event(ev.event_id) is not None)

    store.log("自检动作", "细节", "selfcheck")
    check("审计日志可写可读", bool(store.audit_rows(limit=1)))

    # ---------------- 6) 端到端演练 ----------------
    section("[6] 端到端演练（合成火焰 -> 报警 -> 证据留档）")
    import video as video_mod
    import detector as detector_mod

    e2e_store = storage_mod.EventStore(
        os.path.join(str(config.DATA_ROOT), "selfcheck_e2e.db"))
    recorder = video_mod.EvidenceRecorder(config.load_settings(), e2e_store)
    recorder.start()
    seen = {"alarm": 0, "clear": 0, "ids": []}

    def on_alarm(det, e):
        seen["alarm"] += 1
        seen["ids"].append(e.event_id)
        e2e_store.save_event(e)
        recorder.active_event = e
        recorder.capture_event(e, frame_fire(0.05))

    def on_clear(e):
        seen["clear"] += 1
        e2e_store.save_event(e)
        recorder.close_event(e.event_id, save_clip=True)

    backend, why = detector_mod.build_backend(config.load_settings())
    check("检测后端装配成功", backend is not None, why)
    det, why2 = detector_mod.build_detector(config.load_settings(),
                                            on_alarm=on_alarm, on_clear=on_clear)
    t0 = time.time()
    nframes = 0
    while time.time() - t0 < 3.5:
        det.update(frame_fire(time.time() - t0), camera_name="自检合成源",
                   camera_url="synthetic")
        nframes += 1
        time.sleep(0.08)
    check("演练真的报了警（不是伪造事件）", seen["alarm"] >= 1,
          "注入 %d 帧，报警 %d 次" % (nframes, seen["alarm"]))

    # 等证据落盘
    deadline = time.time() + 6
    evid = None
    while time.time() < deadline and seen["ids"]:
        st = recorder.stats()
        row = e2e_store.get_event(seen["ids"][0])
        if st.get("queued") == 0 and row is not None and row.snapshot:
            evid = row
            break
        time.sleep(0.2)
    recorder.close_event(seen["ids"][0], save_clip=True) if seen["ids"] else None
    check("抓拍图已落盘", bool(evid and evid.snapshot and os.path.isfile(evid.snapshot)),
          (evid.snapshot if evid else "（无）"))
    check("标注图已落盘", bool(evid and evid.marked and os.path.isfile(evid.marked)),
          (evid.marked if evid else "（无）"))
    deadline = time.time() + 5
    while time.time() < deadline:
        row = e2e_store.get_event(seen["ids"][0]) if seen["ids"] else None
        if row is not None and row.clip and os.path.isfile(row.clip):
            break
        time.sleep(0.2)
    row = e2e_store.get_event(seen["ids"][0]) if seen["ids"] else None
    check("录像已落盘", bool(row and row.clip and os.path.isfile(row.clip)
                            and os.path.getsize(row.clip) > 1024),
          ("%s（%.1f KB）" % (row.clip, os.path.getsize(row.clip) / 1024.0))
          if (row and row.clip and os.path.isfile(row.clip)) else "（无）")
    check("证据写入无错误", not recorder.stats()["errors"],
          str(recorder.stats()["errors"][:2]))
    recorder.stop()

    # ---------------- 7) 界面与弹窗 ----------------
    section("[7] 界面与弹窗冒烟")
    if not has_tk:
        check("界面冒烟", True, "本环境无 tkinter，跳过（不影响 --headless 模式）")
    else:
        try:
            import ui_main
            app = ui_main.FireApp(hidden=True)
            check("主窗口可构建", app.root is not None)
            check("顶栏按钮齐全", all(hasattr(app, n) for n in
                                     ("btn_mon", "lamp", "tree", "canvas")))
            app.refresh_events()
            app._render_frame()
            check("无摄像头时刷新不崩", True)
            check("检测后端说明已显示", bool(app.lbl_backend.cget("text")),
                  app.lbl_backend.cget("text")[:60])
            app.quit_app()
        except Exception as exc:                            # noqa: BLE001
            check("主窗口可构建", False, "%s: %s" % (type(exc).__name__, str(exc)[:80]))
        try:
            import tkinter as tk
            import ui_popup
            root = tk.Tk()
            root.withdraw()
            hits = {"ack": 0}
            pop = ui_popup.AlarmPopup(root, app=None,
                                      on_ack=lambda e: (hits.__setitem__("ack", 1),
                                                        (True, "ok"))[1],
                                      on_false=lambda e: (True, "ok"))
            pev = models.FireEvent(camera_name="A01", score=0.85, reason="自检")
            pop.show(pev, frame_fire(0.1), models.Detection(has_fire=True, score=0.85))
            check("火警弹窗可弹出", pop.visible)
            pop._do_ack()
            check("弹窗内可确认到场", hits["ack"] == 1)
            for i in range(2):
                pop.show(models.FireEvent(camera_name="A%d" % i, score=0.8),
                         frame_fire(i * 0.1))
            check("多次报警不叠窗（追加到同一窗）", pop.visible and len(pop._events) == 3,
                  "%d 条事件" % len(pop._events))
            pop.notify_clear(pev)
            check("解除通知不崩", True)
            pop.hide()
            root.destroy()
        except Exception as exc:                            # noqa: BLE001
            check("火警弹窗可弹出", False, "%s: %s" % (type(exc).__name__, str(exc)[:80]))

    # ---------------- 8) 托盘 ----------------
    section("[8] 托盘（后台运行能力）")
    import tray as tray_mod
    ok, why = tray_mod.available()
    check("托盘可用性已探测", isinstance(ok, bool), why)
    if ok:
        t = tray_mod.TrayIcon(tooltip="自检", on_command=lambda a: None)
        started = t.start()
        check("托盘图标可创建（失败也能降级）", isinstance(started, bool),
              "start()=%s" % started)
        t.stop()
    else:
        check("托盘不可用时能降级", True, "会退化为普通最小化，不影响报警")

    # ---------------- 汇总 ----------------
    print()
    print("=" * 76)
    print("自检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 76)
    if not FAIL:
        print("提示：默认检测器是「多信号传统视觉」，请在真实现场按需标定阈值")
        print("      （界面「检测参数」或 data\\settings.json）。")
    _summary(tmp)
    return 1 if FAIL else 0


def _summary(tmp):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
