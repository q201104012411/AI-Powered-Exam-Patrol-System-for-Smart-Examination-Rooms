# -*- coding: utf-8 -*-
"""02 fire_detect 功能冒烟：真的能检出火、且不误报。

用合成画面验证（不依赖摄像头）：
  1) 纯色/空白画面 → 不报警（静态火色封顶 / 无火色）
  2) 一块"跳动的橙红火焰" + 面积增长 → 确认火警（连续帧闸门通过）
  3) 连续空白 → 判"已解除"
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                     # noqa: E402
import fire_detect as fd               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def blank(h=240, w=320, bgr=(90, 90, 90)):
    return np.full((h, w, 3), bgr, dtype=np.uint8)


def fire_frame(h=240, w=320, seed=0, size=42, jitter=True):
    """橙红色跳动火焰块（BGR：蓝低、绿中、红高 → 颜色偏 (30,90,255)）"""
    img = blank(h, w)
    rng = np.random.RandomState(seed)
    cx, cy = w // 2, h // 2
    s = size + (rng.randint(-6, 7) if jitter else 0)
    x0, y0 = cx - s // 2, cy - s // 2
    img[max(0, y0):y0 + s, max(0, x0):x0 + s] = (25, 130, 255)   # BGR 橙红
    # 加一点亮度抖动（闪烁信号）
    if jitter:
        img[max(0, y0):y0 + s:3, max(0, x0):x0 + s] = (40, 190, 255)
    return img


print("后端信息：%s" % fd.load_settings().get("backend"))
w = fd.FireWatch()
print("检测后端：%s\n" % w.backend_name)

print("=== 1) 空白画面不报警 ===")
r = None
for i in range(8):
    r = w.push_frame(blank(seed=i) if False else blank(), camera_name="CAM-TEST")
    time.sleep(0.21)          # 越过 detect_interval_ms 限频
check("空白画面 has_fire=False", not (r or {}).get("just_triggered"),
      "level=%s score=%.3f" % ((r or {}).get("level"), (r or {}).get("score") or 0))
det = fd.VisionFireBackend().analyze(blank())
check("纯灰画面判定无火", not det.has_fire, "score=%.3f" % det.score)

print("\n=== 2) 火焰画面能检出（需连续 confirm_frames 帧）===")
w2 = fd.FireWatch()
hit = False
best = 0.0
for i in range(14):
    r = w2.push_frame(fire_frame(seed=i, size=44 + i * 3), camera_name="CAM-FIRE")
    best = max(best, r.get("score") or 0.0)
    if r.get("just_triggered"):
        hit = True
        check("触发一次新火警", True,
              "level=%s score=%.3f reason=%s" % (r.get("level"), r.get("score"), r.get("reason")))
        check("火警级别为 alarm 或 warning", r.get("level") in ("alarm", "warning"),
              "level=%s" % r.get("level"))
        break
    time.sleep(0.21)
check("连续火焰帧最终触发报警", hit, "最高分 %.3f" % best)
check("事件已记入 FireWatch.alarms", len(w2.alarms) >= 1, "%d 条" % len(w2.alarms))

print("\n=== 3) 火焰消失后判解除 ===")
cleared = False
for i in range(20):
    r = w2.push_frame(blank(), camera_name="CAM-FIRE")
    if r.get("cleared") and not (r.get("det") and r["det"].has_fire):
        cleared = True
        break
    time.sleep(0.21)
check("画面恢复后判为已解除", cleared)

print("\n=== 4) just_triggered 不重复刷（同一次火只报一次）===")
w3 = fd.FireWatch()
fires = 0
for i in range(20):
    r = w3.push_frame(fire_frame(seed=i, size=46), camera_name="CAM-DUP")
    if r.get("just_triggered"):
        fires += 1
    time.sleep(0.21)
check("同一次持续火焰只报一次（事件风暴防护）", fires <= 1, "共报 %d 次" % fires)

print("\n=== 5) 接口契约 ===")
for k in ("ok", "level", "score", "reason", "just_triggered", "cleared", "rect", "det"):
    check("返回字段 %s" % k, k in (r or {}))
check("无画面时不崩且如实返回", w3.push_frame(None).get("ok") is False)

print("\n" + "=" * 60)
print("火检功能冒烟：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
