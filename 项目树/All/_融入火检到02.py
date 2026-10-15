# -*- coding: utf-8 -*-
"""Merge module 13 fire detection into module 02 as one self-contained file.

Why: 13's detect.py/detector.py import `config` and `models`, which clash by name
with 02's own modules (the reason this repo uses subprocess isolation). Module 13
is also an island: its integration_api was never called by anyone.

What this does: read 13's detect.py + detector.py, inline the ~31 threshold defaults
and the Detection class, and append a FireWatch wrapper matching the shape of 02's
existing CheatWatch (lazy build + rate limit + only just_triggered reports).

NOTE: all Chinese prose in this file is kept OUT of the generated header to avoid
quote-normalisation damage; the generated module's Chinese text comes verbatim from
13's own source files, which are known-good UTF-8.
"""
import os
import re
import sys

ALL = os.path.dirname(os.path.abspath(__file__))
SRC13 = os.path.join(ALL, "13_AI火场识别与自动报警")
DST02 = os.path.join(ALL, "02_摄像头核查与视频质量诊断")
OUT = os.path.join(DST02, "fire_detect.py")

DEFAULTS = [
    ("hsv_h_low_max", 35), ("hsv_h_red_min", 160), ("hsv_s_min", 90), ("hsv_v_min", 170),
    ("skin_y_min", 60), ("skin_y_max", 255), ("skin_cb_min", 77), ("skin_cb_max", 133),
    ("skin_cr_min", 133), ("skin_cr_max", 180), ("skin_dilate_px", 9),
    ("morph_kernel", 5), ("min_fire_px", 220), ("min_fire_ratio", 0.0012),
    ("max_fire_ratio", 0.60), ("max_regions", 6),
    ("smoke_enable", True), ("smoke_v_min", 70), ("smoke_v_max", 235), ("smoke_s_max", 70),
    ("smoke_edge_max", 0.06), ("smoke_min_ratio", 0.06), ("smoke_rise_ratio", 0.03),
    ("flicker_window", 12), ("flicker_v_min", 2.0), ("flicker_area_min", 0.02),
    ("growth_min", -0.15), ("confirm_frames", 4), ("clear_frames", 12),
    ("warning_threshold", 0.40), ("alarm_threshold", 0.62), ("event_cooldown_sec", 20),
    ("detect_interval_ms", 200), ("backend", "auto"), ("onnx_model_path", ""),
    ("onnx_conf", 0.35), ("onnx_input_dim", 640), ("onnx_fire_classes", "fire,smoke"),
]

HEAD = '''# -*- coding: utf-8 -*-
# Fire / smoke detection, self-contained. Merged from 13_AI火场识别与自动报警
# (2026-10-15). See 13_AI火场识别与自动报警/README.md for the move record.
#
# Source: 13/detect.py + 13/detector.py (five-signal weighted fusion: fire colour,
# saturation, brightness, flicker, region growth, plus a smoke signal).
# They imported 13's own config/models, which clash by name with 02's modules, so
# the 31 threshold defaults and the Detection class are inlined here. No dependency
# on the 13 directory and no onnxruntime requirement: without models/fire*.onnx the
# classic multi-signal backend is used (same as 13's original design).
#
# Two safety rules carried over from 13 (do not change):
#   1. A single frame never counts: confirm_frames (default 4) consecutive hits.
#   2. Static fire colour is capped below the warning line when there is no flicker
#      and no growth, so a red wall or red clothing cannot raise an alarm.
#
# Public API:
#   FireWatch.push_frame(frame, camera_name="", camera_url="") -> dict
#   FireWatch.reset()
#   VisionFireBackend / FireDetector / build_backend / build_detector

from __future__ import annotations

import math
import os
import time

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False
try:
    import numpy as np
    HAS_NUMPY = True
except Exception:
    np = None
    HAS_NUMPY = False


SETTINGS_DEFAULT = {
'''

MID = '''}


def load_settings():
    """Return a mutable threshold dict (built-in defaults, optionally overridden by
    data/fire_settings.json in the 02 data directory)."""
    cfg = dict(SETTINGS_DEFAULT)
    try:
        import json
        base = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(base, "data", "fire_settings.json"),
                     os.path.join(base, "fire_settings.json")):
            if os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k in cfg:
                            try:
                                cfg[k] = bool(v) if isinstance(cfg[k], bool) else type(cfg[k])(v)
                            except Exception:
                                pass
                break
    except Exception:
        pass
    return cfg


def onnx_model_path(cfg=None):
    """Find models/fire*.onnx; empty string means use the classic backend."""
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "models")
    if not os.path.isdir(d):
        return ""
    for name in sorted(os.listdir(d)):
        if name.lower().startswith("fire") and name.lower().endswith(".onnx"):
            return os.path.join(d, name)
    return ""


class Detection(object):
    """Single-frame result (same fields as 13's models.Detection)."""

    __slots__ = ("has_fire", "score", "level", "parts", "fire_ratio", "smoke_ratio",
                 "regions", "smoke_regions", "sharpness", "backend", "notes")

    def __init__(self, has_fire=False, score=0.0, level=None, parts=None,
                 fire_ratio=0.0, smoke_ratio=0.0, regions=None, smoke_regions=None,
                 sharpness=0.0, backend="vision", notes=None):
        self.has_fire = bool(has_fire)
        self.score = float(score)
        self.level = level
        self.parts = dict(parts or {})
        self.fire_ratio = float(fire_ratio)
        self.smoke_ratio = float(smoke_ratio)
        self.regions = list(regions or [])
        self.smoke_regions = list(smoke_regions or [])
        self.sharpness = float(sharpness)
        self.backend = backend
        self.notes = list(notes or [])

    def to_dict(self):
        return {"has_fire": self.has_fire, "score": round(self.score, 4),
                "level": self.level, "parts": self.parts,
                "fire_ratio": round(self.fire_ratio, 5),
                "smoke_ratio": round(self.smoke_ratio, 5),
                "regions": self.regions, "smoke_regions": self.smoke_regions,
                "sharpness": round(self.sharpness, 2),
                "backend": self.backend, "notes": self.notes}

    def reason_text(self):
        if not self.parts:
            return "no signal"
        top = sorted(self.parts.items(), key=lambda kv: -kv[1])[:3]
        return ", ".join("%s %.2f" % (k, v) for k, v in top if v > 0) or "no signal"


def ok_detection(det=None, backend="vision", note=""):
    """Build a no-fire result (used by backend fallbacks / error paths)."""
    d = det or Detection()
    d.has_fire = False
    d.level = None
    d.backend = backend
    if note:
        d.notes = list(d.notes) + [note]
    return d


def now_epoch():
    return time.time()


class FireEvent(object):
    """Fire event record (13's models.FireEvent, trimmed to what the detector uses).

    The host (02) keeps its own evidence/DB; this object exists so FireDetector's
    gate and callbacks keep working unchanged.
    """

    __slots__ = ("event_id", "camera_name", "camera_url", "level", "score",
                 "created_at", "created_epoch", "peak_score", "peak_epoch",
                 "frames_hit", "reason", "parts", "fire_ratio", "smoke_ratio",
                 "regions", "snapshot", "marked", "clip", "status",
                 "acked_by", "acked_at", "acked_note", "cleared_at", "duration_sec")

    def __init__(self, camera_name="", camera_url="", level=None, score=0.0,
                 reason="", parts=None, fire_ratio=0.0, smoke_ratio=0.0,
                 regions=None, snapshot="", marked="", clip="", status="firing"):
        import uuid
        self.event_id = "FE" + uuid.uuid4().hex[:12]
        self.camera_name = camera_name
        self.camera_url = camera_url
        self.level = level
        self.score = float(score)
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.created_epoch = now_epoch()
        self.peak_score = float(score)
        self.peak_epoch = self.created_epoch
        self.frames_hit = 1
        self.reason = reason
        self.parts = dict(parts or {})
        self.fire_ratio = float(fire_ratio)
        self.smoke_ratio = float(smoke_ratio)
        self.regions = list(regions or [])
        self.snapshot = snapshot
        self.marked = marked
        self.clip = clip
        self.status = status
        self.acked_by = ""
        self.acked_at = ""
        self.acked_note = ""
        self.cleared_at = ""
        self.duration_sec = 0.0

    def clear(self):
        self.status = "cleared"
        self.cleared_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.duration_sec = max(0.0, now_epoch() - self.created_epoch)
        return self


# ---------------------------------------------------------------------------
# Detection core (verbatim from 13/detect.py, only config/models references
# rewritten to the inlined equivalents)
# ---------------------------------------------------------------------------
'''

TAIL = '''

# ---------------------------------------------------------------------------
# Backend assembly (verbatim from 13/detector.py)
# ---------------------------------------------------------------------------
'''

WATCH = '''

# ---------------------------------------------------------------------------
# FireWatch: thin wrapper for the host detection loop
# ---------------------------------------------------------------------------
class FireWatch(object):
    """One instance per camera. Same shape as 02's CheatWatch: lazy build,
    rate-limited by detect_interval_ms, and only just_triggered reports, so a
    sustained fire produces one alert rather than one per frame."""

    def __init__(self, cfg=None, on_alarm=None, on_clear=None):
        self.cfg = cfg or load_settings()
        self._detector = None
        self._last_ms = 0.0
        self._last_report_epoch = 0.0          # 上次上报火警的时间（冷却用）
        self._on_alarm = on_alarm
        self._on_clear = on_clear
        self.alarms = []

    def _ensure(self):
        if self._detector is None:
            try:
                # build_detector 返回 (detector, 说明) —— 必须解包，否则拿到的是元组
                built = build_detector(self.cfg, on_alarm=self._on_alarm,
                                       on_clear=self._on_clear)
                self._detector = built[0] if isinstance(built, tuple) else built
            except Exception:
                # 装配失败也要能降级到传统视觉（13 原设计：永不因缺模型而不可用）
                self._detector = FireDetector(backend=VisionFireBackend(self.cfg),
                                              cfg=self.cfg, on_alarm=self._on_alarm,
                                              on_clear=self._on_clear)
        return self._detector

    @property
    def backend_name(self):
        try:
            return describe_backend(self._ensure())
        except Exception:
            return "unknown"

    def reset(self):
        if self._detector is not None:
            self._detector.reset()

    def push_frame(self, frame, camera_name="", camera_url=""):
        out = {"ok": False, "level": None, "score": 0.0, "reason": "",
               "just_triggered": False, "cleared": False, "rect": None, "det": None}
        if frame is None or not HAS_CV2 or not HAS_NUMPY:
            out["reason"] = "no frame or opencv/numpy missing"
            return out
        now_ms = time.time() * 1000.0
        interval = float(self.cfg.get("detect_interval_ms") or 200)
        if now_ms - self._last_ms < interval:
            out["reason"] = "rate limited"
            return out
        self._last_ms = now_ms
        try:
            det = self._ensure().update(frame, camera_name=camera_name,
                                        camera_url=camera_url)
        except Exception as e:
            out["reason"] = "detect error: %s" % e
            return out
        out["ok"] = True
        out["det"] = det
        out["level"] = det.level
        out["score"] = det.score
        out["reason"] = det.reason_text()
        if det.regions:
            x, y, w, h = det.regions[0][:4]
            out["rect"] = (int(x), int(y), int(w), int(h))
        if getattr(det, "has_fire", False):
            detector = self._ensure()
            # active_event 在 13 里是 property（不是方法），这里两种写法都兼容
            ev = getattr(detector, "active_event", None)
            if callable(ev):
                try:
                    ev = ev()
                except Exception:
                    ev = None
            seen = [a.get("event_id") for a in self.alarms]
            cooldown = float(self.cfg.get("event_cooldown_sec") or 20)
            cooled = (time.time() - self._last_report_epoch) >= cooldown
            if ev is not None and getattr(ev, "event_id", "") not in seen and cooled:
                out["just_triggered"] = True
                self._last_report_epoch = time.time()
                self.alarms.append({"event_id": getattr(ev, "event_id", ""),
                                    "level": det.level, "score": det.score,
                                    "reason": det.reason_text(),
                                    "at": getattr(ev, "created_at", "")})
            elif ev is None and det.level == "alarm" and cooled:
                # 后端报了 alarm 但没有活跃事件对象（例如上层强制清事件后重来），
                # 也要如实告知调用方，避免"检出了却不报警"。
                out["just_triggered"] = True
                self._last_report_epoch = time.time()
                self.alarms.append({"event_id": "", "level": det.level,
                                    "score": det.score, "reason": det.reason_text(),
                                    "at": ""})
        else:
            out["cleared"] = True
        return out
'''


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def strip_imports(src):
    """Drop import lines and the leading module docstring (handles r\"\"\" prefixes)."""
    out, in_doc = [], False
    for i, line in enumerate(src.splitlines()):
        s = line.strip()
        if not in_doc and i < 60 and re.match(r'^[rRuUbB]{0,2}("""|\'\'\')', s):
            # opening of module docstring: skip it, and close on the next triple-quote
            body = re.sub(r'^[rRuUbB]{0,2}', '', s)
            q = '"""' if body.startswith('"""') else "'''"
            if body.count(q) >= 2:          # single-line docstring
                continue
            in_doc = True
            continue
        if in_doc:
            if '"""' in s or "'''" in s:
                in_doc = False
            continue
        if re.match(r'^(import|from)\s+', line):
            continue
        out.append(line)
    # 函数体内的局部 import（缩进）也要去掉：13 里有 `if ...: import models` 这种写法
    out = [l for l in out
           if not re.match(r'^\s+(import|from)\s+(models|config|detect|detector)\b', l)]
    return "\n".join(out)


def main():
    if not os.path.isdir(SRC13):
        print("13 source dir not found: %s" % SRC13)
        return 1
    detect_body = strip_imports(read(os.path.join(SRC13, "detect.py")))
    detector_body = strip_imports(read(os.path.join(SRC13, "detector.py")))

    # detect.py: config/models -> inlined equivalents
    for pat, rep in (("config.load_settings()", "load_settings()"),
                     ("models.Detection", "Detection"),
                     # 13 的 ok_detection(backend_name) 传的是"后端名"这一位置参，
                     # 内联版签名是 ok_detection(det=None, backend=...)，必须显式交给 backend
                     ("models.ok_detection(", "ok_detection(backend="),
                     ("models.FireEvent", "FireEvent"),
                     ("models.now_epoch()", "now_epoch()"),
                     ("config.LEVEL_ALARM", '"alarm"'),
                     ("config.LEVEL_WARNING", '"warning"')):
        detect_body = detect_body.replace(pat, rep)
    # detector.py: same, plus it used to import the sibling `detect` module
    for pat, rep in (("config.load_settings()", "load_settings()"),
                     ("config.onnx_model_path(cfg)", "onnx_model_path(cfg)"),
                     ("config.onnx_model_path()", "onnx_model_path()"),
                     ("config.onnx_model_path", "onnx_model_path"),
                     ("models.Detection", "Detection"),
                     ("models.ok_detection", "ok_detection"),
                     ("models.FireEvent", "FireEvent"),
                     ("models.now_epoch()", "now_epoch()"),
                     ("import detect as detect_mod", ""),
                     ("detect_mod.VisionFireBackend", "VisionFireBackend"),
                     ("detect_mod.FireDetector", "FireDetector"),
                     ("detect_mod.", "")):
        detector_body = detector_body.replace(pat, rep)

    # --- FIX ---
    # 13 上游缺陷：analyze() 构造 Detection 时**没有给 level 赋值**，而 FireDetector
    # 的确认闸门是按 `det.level is not None` 计数的 —— 于是 level 恒为 None、
    # 连击永远为 0、事件永不创建、on_alarm 永不回调（13 的集成接口因此"死线"）。
    # 这里按阈值补上分级（与 13 的 config.level_for_score 语义一致）。
    OLD_ANALYZE = '''        det = Detection(has_fire=score >= float(cfg["warning_threshold"]),
                               score=score, parts=parts,'''
    NEW_ANALYZE = '''        _warn = float(cfg["warning_threshold"])
        _alarm = float(cfg.get("alarm_threshold") or 0.62)
        _level = "alarm" if score >= _alarm else ("warning" if score >= _warn else None)
        det = Detection(has_fire=score >= _warn,
                               level=_level,
                               score=score, parts=parts,'''
    if OLD_ANALYZE in detect_body:
        detect_body = detect_body.replace(OLD_ANALYZE, NEW_ANALYZE, 1)
    else:
        print("WARN: analyze() 的 Detection 构造锚点未命中，请人工检查 level 赋值")

    # 残留的跨模块引用一律先拦下（除非已经是内联名）
    leftovers = []
    for body, tag in ((detect_body, "detect"), (detector_body, "detector")):
        for l in body.splitlines():
            s = l.strip()
            if s.startswith("#"):
                continue
            if ("models." in s or "config." in s or "detect_mod" in s
                    or "import detect" in s or "import config" in s or "import models" in s):
                leftovers.append("%s: %s" % (tag, s))
    if leftovers:
        print("still has cross-module references:")
        for l in leftovers[:10]:
            print("   " + l)
        return 1

    head = HEAD
    for k, v in DEFAULTS:
        head += '    "%s": %r,\n' % (k, v)

    content = head + MID + detect_body + "\n" + TAIL + detector_body + WATCH
    try:
        compile(content, OUT, "exec")
    except SyntaxError as e:
        print("generated file has a syntax error: %s" % e)
        bad = content.splitlines()
        lo = max(0, (e.lineno or 1) - 5)
        for i in range(lo, min(len(bad), (e.lineno or 1) + 3)):
            print("  %5d| %s" % (i + 1, bad[i]))
        return 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(content)
    print("written: %s" % OUT)
    print("  lines: %d" % len(content.splitlines()))
    for tag in ("class Detection", "def load_settings", "class FireWatch",
                "def build_detector", "class VisionFireBackend"):
        print("  %-24s %s" % (tag, tag in content))
    return 0


if __name__ == "__main__":
    sys.exit(main())
