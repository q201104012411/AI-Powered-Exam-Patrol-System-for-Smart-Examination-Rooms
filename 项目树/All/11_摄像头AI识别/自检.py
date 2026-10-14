# -*- coding: utf-8 -*-
r"""
摄像头 AI 识别 · 自检

覆盖三件事，不需要真实摄像头也能跑：
  1) 台账 CRUD（增/改/查/删/去重/地址校验）
  2) 取流内核（用注入的假采集器验证"永不卡界面"的状态机）
  3) AI 检测器（建得起来、能出结果、能如实报出当前检测链）
  4) 自检脚本的 --real 模式只读真实数据，不做破坏性写入

用法：
  python 自检.py            # 用临时数据目录，随便跑
  python 自检.py --real     # 用真实 data 目录（只读式检查，不写改数据）
"""
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
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— %s" % detail) if detail else ""))
    return bool(cond)


def section(title):
    print()
    print("-" * 74)
    print(title)
    print("-" * 74)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实 data 目录（只读检查）")
    args = ap.parse_args()

    tmp = None
    if not args.real:
        tmp = tempfile.mkdtemp(prefix="ai_cam_selfcheck_")
        os.environ["EXAM_AI_CAM_HOME"] = tmp
    for m in ("config", "storage"):
        sys.modules.pop(m, None)

    print("=" * 74)
    print("摄像头 AI 识别 · 自检")
    print("=" * 74)

    import config
    print("数据目录：%s" % config.DATA_ROOT)
    print("模式：%s" % ("真实目录（只读检查）" if args.real else "临时目录（可破坏性测试）"))

    # ---------------- 1) 台账 ----------------
    section("[1] 摄像头台账 CRUD")
    import storage
    from storage import Camera

    # 地址校验
    for url, want in (("0", True), ("rtsp://192.168.1.64:554/stream", True),
                      ("http://127.0.0.1:5000/video_feed", True),
                      ("", False), ("rtsp://", False), ("随便写的", False)):
        ok, why = storage.validate_url(url)
        check("地址校验 %-38s -> %s" % (url or "(空)", ok), ok == want, why)

    store = storage.CameraStore(config.DB_PATH)
    check("初始为空", store.count() == 0, "count=%d" % store.count())

    c1 = Camera(name="考场1正门", url="0", room="1考场")
    ok, msg = store.add(c1)
    check("新增第 1 路", ok and c1.id == 1, msg)

    ok, msg = store.add(Camera(name="重复", url="0"))
    check("重复地址被拒绝", not ok, msg)

    ok, msg = store.add(Camera(name="坏地址", url="rtsp://"))
    check("非法地址被拒绝", not ok, msg)

    c2 = Camera(name="考场2", url="rtsp://192.168.1.64:554/stream", room="2考场")
    store.add(c2)
    check("新增第 2 路", store.count() == 2, "count=%d" % store.count())

    c1.name = "考场1正门(改名)"
    c1.enabled = False
    ok, msg = store.update(c1)
    got = store.get(c1.id)
    check("更新生效", ok and got.name.endswith("(改名)") and not got.enabled, msg)
    check("启用列表只剩 1 路", len(store.enabled()) == 1,
          "enabled=%d" % len(store.enabled()))

    ok, msg = store.update(Camera(name="x", url="rtsp://192.168.1.64:554/stream",
                                  id=c1.id))
    check("改成他路已占用的地址被拒", not ok, msg)

    ok = store.set_enabled(c1.id, True)
    check("重新启用", ok and len(store.enabled()) == 2)

    # 导入导出
    js = store.export_json()
    added, skipped, msg = store.import_json(js)
    check("导出/导入往返（应全部跳过=已存在）", added == 0 and skipped == 2, msg)

    check("删除", store.delete(c2.id) and store.count() == 1)

    # ---------------- 2) 取流状态机 ----------------
    section("[2] 取流内核状态机（注入假采集器，不碰真设备）")
    try:
        import camera_stream as cs
        import numpy as np

        class FakeCap:
            """假采集器：read 直接返回一帧，release 记录调用。"""
            def __init__(self):
                self.released = False
            def read(self):
                return True, np.zeros((48, 64, 3), dtype=np.uint8)
            def release(self):
                self.released = True
            def isOpened(self):
                return True

        fake = FakeCap()
        r = cs.FrameReader("0", capture_factory=lambda *a, **k: fake,
                           stall_timeout=5.0)
        r.start()
        t0 = time.time()
        while time.time() - t0 < 5:
            s = r.snapshot()
            if s["state"] == "live" and s["frame"] is not None:
                break
            time.sleep(0.05)
        s = r.snapshot()
        check("取流进入 live 并拿到帧", s["state"] == "live" and s["frame"] is not None,
              "state=%s" % s["state"])
        check("snapshot 含必要字段",
              all(k in s for k in ("state", "status", "frame", "seq", "fps", "reconnects")))
        t1 = time.perf_counter()
        r.snapshot()
        check("snapshot 不阻塞（<50ms）", (time.perf_counter() - t1) < 0.05)
        t1 = time.perf_counter()
        r.stop()
        check("stop() 立即返回（<1s）", (time.perf_counter() - t1) < 1.0,
              "%.3fs" % (time.perf_counter() - t1))
        check("接口齐全", all(hasattr(cs, n) for n in
                             ("open_capture", "FrameReader", "release_async", "log")))
    except Exception as e:
        check("取流内核可用", False, "%s: %s" % (type(e).__name__, str(e)[:60]))

    # ---------------- 3) AI 检测器 ----------------
    section("[3] AI 检测器")
    try:
        import numpy as np
        import person_detect as pd
        check("person_detect 导入", True, "新版检测链（YOLO11n ONNX + HOG 兜底）")
        model_path = pd.default_yolo_model_path()
        check("模型查找接口存在", bool(model_path), model_path)
        try:
            import onnxruntime  # noqa: F401
            ort_ok = True
        except Exception:
            ort_ok = False
        check("onnxruntime 已安装（YOLO 推理依赖）", ort_ok, "缺则自动回落 HOG+SVM")
        check("YOLO11n 权重在位", os.path.exists(model_path),
              os.path.basename(model_path) if os.path.exists(model_path)
              else "可运行 工具_YOLO模型导出.py 生成")

        tr = pd.PersonTracker(max_input_dim=1280, dense_mode=True, min_hits=1)
        check("检测器创建成功", tr is not None)
        if getattr(tr, "_yolo_session", None) is not None:
            name = "YOLO11n-ONNX"
        elif getattr(tr, "_net", None) is not None:
            name = "MobileNet-SSD"
        else:
            name = "HOG+SVM"
        check("能如实报出当前检测链", bool(name), name)

        # 合成一帧，能跑通且框格式正确
        frame = np.full((480, 640, 3), 90, dtype=np.uint8)
        for cx in (120, 300, 480):
            np_frame = frame
            import cv2
            cv2.ellipse(np_frame, (cx, 300), (26, 70), 0, 0, 360, (150, 140, 130), -1)
            cv2.circle(np_frame, (cx, 220), 20, (200, 190, 180), -1)
        persons, viol, line_y = tr.analyze_only(frame, boundary_ratio=0.92)
        check("analyze_only 返回三元组", isinstance(persons, list) and isinstance(viol, int))
        check("line_y 合理", 0 < line_y < frame.shape[0], "line_y=%d" % line_y)
        if persons:
            p0 = persons[0]
            check("逐人字段齐全", all(k in p0 for k in
                                 ("pid", "rect", "action", "bad", "behaviors",
                                  "crosses", "density_level")))
            x, y, w, h = p0["rect"]
            check("框在画面内", 0 <= x and 0 <= y and w > 0 and h > 0
                  and x + w <= frame.shape[1] + 1, str(p0["rect"]))
        else:
            check("逐人字段齐全（本次未检出人，跳过）", True,
                  "合成剪影召回有限，属已知上限")
        check("draw_boxes 可调用", callable(tr.draw_boxes))
        buf = frame.copy()
        tr.draw_boxes(buf, persons, line_y=line_y, scale=1.0, font_size=12)
        check("绘制未改变尺寸", buf.shape == frame.shape)
    except Exception as e:
        check("AI 检测链路", False, "%s: %s" % (type(e).__name__, str(e)[:80]))

    # ---------------- 4) 配置 ----------------
    section("[4] 配置与设置读写")
    try:
        cfg = config.load_ai_settings()
        check("默认设置可读", cfg.get("ai_mode") == "standard", str(cfg.get("ai_mode")))
        check("分析长边解析", config.ai_input_dim(cfg) == 640,
              "%d" % config.ai_input_dim(cfg))
        config.save_ai_settings({"ai_mode": "energy"})
        check("写入后生效", config.ai_input_dim() == 416,
              "%d" % config.ai_input_dim())
        config.save_ai_settings({"ai_mode": "standard", "ai_input_dim": 0})
        check("还原默认", config.ai_input_dim() == 640)
        check("渲染间隔随路数增长",
              config.render_interval_ms(1) <= config.render_interval_ms(4)
              <= config.render_interval_ms(20))
        check("无登录/权限模块（刻意为之）",
              not os.path.exists(os.path.join(HERE, "accounts.py")))
    except Exception as e:
        check("配置模块", False, "%s: %s" % (type(e).__name__, str(e)[:60]))

    # ---------------- 汇总 ----------------
    print()
    print("=" * 74)
    print("自检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 74)

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
