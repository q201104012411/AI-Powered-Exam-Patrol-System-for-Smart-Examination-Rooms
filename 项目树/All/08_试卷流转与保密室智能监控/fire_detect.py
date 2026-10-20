# -*- coding: utf-8 -*-
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
    "hsv_h_low_max": 35,
    "hsv_h_red_min": 160,
    "hsv_s_min": 90,
    "hsv_v_min": 170,
    "skin_y_min": 60,
    "skin_y_max": 255,
    "skin_cb_min": 77,
    "skin_cb_max": 133,
    "skin_cr_min": 133,
    "skin_cr_max": 180,
    "skin_dilate_px": 9,
    "morph_kernel": 5,
    "min_fire_px": 220,
    "min_fire_ratio": 0.0012,
    "max_fire_ratio": 0.6,
    "max_regions": 6,
    "smoke_enable": True,
    "smoke_v_min": 70,
    "smoke_v_max": 235,
    "smoke_s_max": 70,
    "smoke_edge_max": 0.06,
    "smoke_min_ratio": 0.06,
    "smoke_rise_ratio": 0.03,
    "flicker_window": 12,
    "flicker_v_min": 2.0,
    "flicker_area_min": 0.02,
    "growth_min": -0.15,
    "confirm_frames": 4,
    "clear_frames": 12,
    "warning_threshold": 0.4,
    "alarm_threshold": 0.62,
    "event_cooldown_sec": 20,
    "detect_interval_ms": 200,
    "backend": 'auto',
    "onnx_model_path": '',
    "onnx_conf": 0.35,
    "onnx_input_dim": 640,
    "onnx_fire_classes": 'fire,smoke',
}


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
# -*- coding: utf-8 -*-


try:
    import cv2
except ImportError:                       # 允许无 OpenCV 时 import（自检会如实报缺依赖）
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None


#: 判定用的固定权重（改这些等于改算法本身；现场只调 config 里的阈值）
W_COLOR_MAX = 0.50        # 火色（面积 × 鲜艳度）
W_SAT_MAX = 0.15          # 饱和度
W_VAL_MAX = 0.15          # 亮度
W_FLICKER_MAX = 0.25      # 闪烁
W_GROWTH_MAX = 0.15       # 增长
W_SMOKE_MAX = 0.30        # 只有烟没有火时的上限（早期提示）

#: 掩膜最小像素数（小图防止"3 个像素也算火"）
_MIN_MASK_PX = 4


class FireSignals(object):

    __slots__ = ("fire_ratio", "fire_px", "smoke_ratio", "smoke_px",
                 "vivid_ratio", "mean_v", "mean_s", "flicker", "growth",
                 "edge_density", "regions", "smoke_regions", "sharpness")

    def __init__(self):
        self.fire_ratio = 0.0
        self.fire_px = 0
        self.smoke_ratio = 0.0
        self.smoke_px = 0
        self.vivid_ratio = 0.0
        self.mean_v = 0.0
        self.mean_s = 0.0
        self.flicker = 0.0
        self.growth = 0.0
        self.edge_density = 0.0
        self.regions = []
        self.smoke_regions = []
        self.sharpness = 0.0


class VisionFireBackend(object):
    """多信号融合后端。analyze(frame) -> Detection"""

    name = "vision"

    def __init__(self, cfg=None):
        self.cfg = cfg or load_settings()
        self.reset()

    # ---------------- 状态 ----------------
    def reset(self):
        """清空时序历史（换摄像头/重启监控时调用）。"""
        self._hist_v = []          # 火区平均亮度的历史
        self._hist_area = []       # 火色面积占比的历史
        self._hist_smoke = []      # 烟雾面积占比的历史
        self._hist_edge = []       # 边缘密度历史
        self._frames = 0

    @property
    def cfg_now(self):
        return self.cfg

    # ---------------- 主入口 ----------------
    def analyze(self, frame):
        cfg = self.cfg
        if frame is None or np is None or cv2 is None:
            return ok_detection(backend=self.name)
        if getattr(frame, "ndim", 0) != 3 or frame.shape[0] < 16 or frame.shape[1] < 16:
            return ok_detection(backend=self.name)

        sig = self._measure(frame, cfg)
        score, parts, notes = self._score(sig, cfg)
        _warn = float(cfg["warning_threshold"])
        _alarm = float(cfg.get("alarm_threshold") or 0.62)
        _level = "alarm" if score >= _alarm else ("warning" if score >= _warn else None)
        det = Detection(has_fire=score >= _warn,
                               level=_level,
                               score=score, parts=parts,
                               fire_ratio=sig.fire_ratio, smoke_ratio=sig.smoke_ratio,
                               regions=sig.regions, smoke_regions=sig.smoke_regions,
                               sharpness=sig.sharpness, backend=self.name, notes=notes)
        self._push_history(sig, cfg)
        return det

    # ---------------- 测量 ----------------
    def _measure(self, frame, cfg):
        sig = FireSignals()
        h, w = frame.shape[:2]
        total = float(max(1, h * w))

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        # ---- 1) 火色掩膜（红/橙/黄 两段色相 + 高饱和高亮） ----
        s_min, v_min = int(cfg["hsv_s_min"]), int(cfg["hsv_v_min"])
        low = cv2.inRange(hsv, (0, s_min, v_min), (int(cfg["hsv_h_low_max"]), 255, 255))
        high = cv2.inRange(hsv, (int(cfg["hsv_h_red_min"]), s_min, v_min), (179, 255, 255))
        fire = cv2.bitwise_or(low, high)

        # ---- 2) 去肤色：YCbCr 上的人脸/手臂也落在红黄区间 ----
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)          # 通道序 Y, Cr, Cb
        Y, Cr, Cb = ycrcb[:, :, 0], ycrcb[:, :, 1], ycrcb[:, :, 2]
        skin = cv2.inRange(ycrcb,
                           (int(cfg["skin_y_min"]), int(cfg["skin_cr_min"]),
                            int(cfg["skin_cb_min"])),
                           (int(cfg["skin_y_max"]), int(cfg["skin_cr_max"]),
                            int(cfg["skin_cb_max"])))
        k = int(cfg["skin_dilate_px"])
        if k >= 3:
            kk = k if k % 2 == 1 else k + 1
            skin = cv2.dilate(skin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
        fire = cv2.bitwise_and(fire, cv2.bitwise_not(skin))

        # ---- 形态学：开运算去椒盐，闭运算把火苗连成片 ----
        mk = int(cfg["morph_kernel"])
        if mk >= 3:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
            fire = cv2.morphologyEx(fire, cv2.MORPH_OPEN, kernel)
            fire = cv2.morphologyEx(fire, cv2.MORPH_CLOSE, kernel)

        fire_px = int(cv2.countNonZero(fire))
        sig.fire_px = fire_px
        sig.fire_ratio = fire_px / total

        # 选出足够大的连通块（小块不算火）
        min_px = max(int(cfg["min_fire_px"]), int(_MIN_MASK_PX))
        regions, keep_mask = self._regions_of(fire, min_px, int(cfg["max_regions"]))
        sig.regions = regions
        if keep_mask is not None:
            use = keep_mask
        else:
            use = fire
        used_px = int(cv2.countNonZero(use)) if use is not None else 0
        sig.fire_ratio = used_px / total

        # 鲜艳度：保留区域里的平均饱和度与亮度（归一化 0~1）
        if used_px > 0:
            sig.mean_s = float(cv2.mean(S, mask=use)[0]) / 255.0
            sig.mean_v = float(cv2.mean(V, mask=use)[0]) / 255.0
            sat_need = max(1.0, float(s_min))
            val_need = max(1.0, float(v_min))
            sig.vivid_ratio = max(0.0, min(1.0, ((sig.mean_s * 255.0) / sat_need - 1.0) / 0.35)) * \
                              max(0.0, min(1.0, ((sig.mean_v * 255.0) / val_need - 1.0) / 0.30))

        # ---- 3) 清晰度（糊成一片时任何判据都不可靠，如实记录） ----
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sig.sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # ---- 5) 烟雾：灰白 + 低饱和 + 极低边缘密度 ----
        if cfg["smoke_enable"]:
            smoke = cv2.inRange(hsv, (0, 0, int(cfg["smoke_v_min"])),
                                (179, int(cfg["smoke_s_max"]), int(cfg["smoke_v_max"])))
            if mk >= 3:
                smoke = cv2.morphologyEx(smoke, cv2.MORPH_OPEN, kernel)
            smoke_px = int(cv2.countNonZero(smoke))
            sig.smoke_px = smoke_px
            sig.smoke_ratio = smoke_px / total
            edges = cv2.Canny(gray, 60, 160)
            sig.edge_density = float(cv2.countNonZero(edges)) / total
            sregions, _ = self._regions_of(smoke, max(min_px, int(total * 0.01)), 4)
            sig.smoke_regions = sregions

        # ---- 3)+4) 时序：闪烁与增长 ----
        sig.flicker, sig.growth = self._temporal(sig, cfg)
        return sig

    @staticmethod
    def _regions_of(mask, min_px, max_regions):
        """返回 ([(x,y,w,h,area_px)...], 仅含合格块的掩膜)"""
        if mask is None or cv2 is None:
            return [], None
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        keep = []
        for c in contours:
            a = int(cv2.contourArea(c))
            if a >= min_px:
                keep.append((a, cv2.boundingRect(c)))
        keep.sort(key=lambda t: -t[0])
        keep = keep[:max(1, max_regions)]
        if not keep:
            return [], None
        out = [(int(x), int(y), int(w), int(h), int(a)) for a, (x, y, w, h) in keep]
        clean = np.zeros_like(mask) if np is not None else None
        for _a, (x, y, w, h) in keep:
            clean[y:y + h, x:x + w] = 255      # 用外接矩形近似，够用于面积与标注
        clean = cv2.bitwise_and(clean, mask)   # 与原始掩膜求交，避免矩形把背景算进去
        return out, clean

    def _temporal(self, sig, cfg):
        """闪烁强度（0~1）与增长率（-1~1）。历史不足时返回 0（不猜）。"""
        win = int(cfg["flicker_window"])
        v_now = sig.mean_v * 255.0
        a_now = sig.fire_ratio

        # 火区平均亮度的帧间抖动：火苗抖，红布不抖
        flicker = 0.0
        if len(self._hist_v) >= 4:
            recent = self._hist_v[-(win - 1):] + [v_now]
            mean = sum(recent) / len(recent)
            var = sum((x - mean) ** 2 for x in recent) / len(recent)
            std = math.sqrt(max(0.0, var))
            base = max(1.0, float(cfg["flicker_v_min"]))
            flicker = max(0.0, min(1.0, (std / base) * 0.6))
            # 面积也要在抖（只亮度抖可能是自动曝光在动）
            areas = self._hist_area[-(win - 1):] + [a_now]
            if areas and max(areas) > 0:
                am = sum(areas) / len(areas)
                avar = sum((x - am) ** 2 for x in areas) / len(areas)
                astd = math.sqrt(max(0.0, avar))
                rel = astd / max(1e-6, am)
                need = max(1e-6, float(cfg["flicker_area_min"]))
                flicker = min(1.0, flicker + max(0.0, min(1.0, rel / need)) * 0.4)

        # 面积增长：最小二乘斜率相对均值
        growth = 0.0
        if len(self._hist_area) >= 4:
            areas = self._hist_area[-(win - 1):] + [a_now]
            n = len(areas)
            xs = list(range(n))
            mx = sum(xs) / n
            my = sum(areas) / n
            den = sum((x - mx) ** 2 for x in xs)
            if den > 0:
                slope = sum((xs[i] - mx) * (areas[i] - my) for i in range(n)) / den
                growth = slope / max(1e-6, my)
                growth = max(-1.0, min(1.0, growth * 4.0))   # 放大到可用区间
        return flicker, growth

    def _push_history(self, sig, cfg):
        win = int(cfg["flicker_window"])
        self._hist_v.append(sig.mean_v * 255.0)
        self._hist_area.append(sig.fire_ratio)
        self._hist_smoke.append(sig.smoke_ratio)
        self._hist_edge.append(sig.edge_density)
        cutoff = max(win * 3, 24)
        for h in (self._hist_v, self._hist_area, self._hist_smoke, self._hist_edge):
            if len(h) > cutoff:
                del h[:len(h) - cutoff]
        self._frames += 1

    # ---------------- 打分 ----------------
    def _score(self, sig, cfg):
        """
        返回 (score 0~1, parts 各信号贡献, notes 说明)。

        关键设计：**火色面积本身不足以定案**。
        一块静止的红布/红灯在 HSV 上与火几乎不可分，所以当区域性特征没有
        "闪烁 / 增长 / 高饱和高亮"任何一条支撑时，火色项要被打折 ——
        这就是静态红布进不了火警线、而真火能进的原因（见 tests/test_detect.py）。
        """
        parts = {}
        notes = []
        cfg_low = float(cfg["min_fire_ratio"])
        hot = sig.fire_ratio >= cfg_low

        # ---- 火色 ----
        color = 0.0
        temporal = 0.0
        oversize = False
        if hot:
            area_f = min(1.0, 0.35 + 0.65 * min(1.0, sig.fire_ratio / max(1e-6, cfg_low * 8.0)))
            vivid = 0.5 + 0.5 * sig.vivid_ratio       # 不够艳也不至于 0 分，但也不给满分
            color = W_COLOR_MAX * area_f * vivid
            oversize = sig.fire_ratio > float(cfg["max_fire_ratio"])

            # ---- 区域性证据：闪烁 / 增长 / 高饱和高亮 ----
            flicker_ev = min(1.0, sig.flicker / 0.45)
            growth_ev = max(0.0, min(1.0, sig.growth / 0.12))
            intense = 1.0 if (sig.mean_v >= 0.86 and sig.mean_s >= 0.60) else 0.0
            temporal = max(flicker_ev, growth_ev, intense)

            # 火色占比过大（多半是整片偏红：红灯直射、红色幕布糊满画面）：
            # 把面积权重压到"占比达标"那一档，不让"面积大"本身变成高分的理由。
            if oversize:
                color = min(color, W_COLOR_MAX * min(1.0, 0.35 + 0.65 * 1.0) * vivid * 0.30)
                notes.append("火色占比 %.0f%% 超出常规（多半是整片偏红），"
                             "按最高「疑似」上报，需人工确认" % (sig.fire_ratio * 100))

            # 没有闪烁/增长/突出亮度支撑时打折；且整体封顶在疑似线附近，
            # 保证静止红色物体最多只到"疑似"，不会进"确认火警"。
            if temporal < 0.2:
                color *= 0.30 if temporal <= 0.0 else 0.55
                notes.append("火色区域无闪烁、无增长、亮度/饱和度也不算突出："
                             "按静止红色物体处理（不计火警）")
            elif temporal < 0.35:
                color *= 0.55 + 0.45 * temporal
                notes.append("区域性证据偏弱（闪烁/增长不足），已打折")
        parts["火色"] = color

        # ---- 饱和度 / 亮度（仅在确有火色区域时计分） ----
        parts["饱和度"] = (W_SAT_MAX * max(0.0, min(1.0, sig.mean_s))) if hot else 0.0
        parts["亮度"] = (W_VAL_MAX * max(0.0, min(1.0, sig.mean_v))) if hot else 0.0

        # ---- 闪烁 / 增长 ----
        parts["闪烁"] = (W_FLICKER_MAX * min(1.0, flicker_ev)) if hot else 0.0
        if hot and sig.growth < float(cfg["growth_min"]):
            # 面积在明显收缩：可能是镜头前飘过的红色物体，扣一点分
            parts["增长"] = -W_GROWTH_MAX * 0.5
            notes.append("火色区域明显收缩（增长 %.2f）" % sig.growth)
        else:
            parts["增长"] = (W_GROWTH_MAX * growth_ev) if hot else 0.0

        # ---- 烟雾（早期征兆；只有烟没有火时**最多只到疑似**，不单独定火警） ----
        smoke = 0.0
        if cfg["smoke_enable"] and sig.smoke_ratio >= float(cfg["smoke_min_ratio"]):
            base, trend, enough = self._smoke_evidence(sig, cfg)
            rise = sig.smoke_ratio - base
            rising = max(sig.smoke_ratio - max(self._hist_smoke[-8:] or [0.0]), 0.0)
            # 必须"比平静基线高且仍在上升"，否则就是把灰墙/白墙当烟
            if enough and sig.edge_density <= float(cfg["smoke_edge_max"]) and \
                    rise > float(cfg["smoke_rise_ratio"]) and \
                    trend + rising > float(cfg["smoke_rise_ratio"]):
                area_f = min(1.0, sig.smoke_ratio / max(1e-6, float(cfg["smoke_min_ratio"]) * 3.0))
                smoke = W_SMOKE_MAX * (0.5 + 0.5 * area_f)
                notes.append("烟雾较基线上升 %.1f 个百分点且仍在增加" % (rise * 100))
        parts["烟雾"] = smoke

        fire_score = sum(v for k, v in parts.items() if k != "烟雾" and v > 0)
        fire_score = max(0.0, min(1.0, fire_score))
        smoke_score = max(0.0, min(1.0, smoke))

        if fire_score > 0:
            score = min(1.0, fire_score + smoke_score * 0.25)   # 有火又有烟：互相印证
        elif smoke_score > 0:
            # 只有烟雾：封顶在"疑似"线附近，绝不让它单独触发确认火警
            cap = max(float(cfg["warning_threshold"]), 0.45)
            score = min(smoke_score, cap)
        else:
            score = 0.0

        # ---- 最终封顶：没有闪烁/增长支撑的"静态火色"只允许到疑似级**以下** ----
        # 这条是"红布/红灯不报警"的最后一道闸。
        # 注意上限取 0.8×疑似阈值：如果正好卡在疑似线上，静止红物会连续命中、
        # 照样把确认闸门喂满（实测 0.400 触发了 4 帧确认）—— 留出余量才真的挡得住。
        # 真火必然有闪烁或增长（火苗本就不稳定），所以不会被它挡掉。
        if hot and temporal < 0.25:
            max_static = float(cfg["warning_threshold"]) * 0.8 * (1.0 + 0.5 * temporal / 0.25)
            if score > max_static:
                notes.append("无时序证据（闪烁/增长不足），判定上限压到疑似线以下 %.2f"
                             % max_static)
                score = max_static

        if sig.fire_px and sig.fire_px < int(cfg["min_fire_px"]) and not hot:
            notes.append("火色像素少于阈值，未计入")
        return max(0.0, min(1.0, score)), parts, notes

    def _smoke_evidence(self, sig, cfg):
        """
        烟雾基线、趋势与"够不够判"。

        返回 (baseline, trend, enough)：
          baseline 取历史里**最平静**那一档的中位数（烟雾会累积，不能用上一帧当基线，
                   否则烟起来后 rise≈0，永远不报 —— 这是早期版本的真实缺陷）；
          trend    最近几帧的上升量（正=在起烟，负=在散）；
          enough   历史帧数够不够（不够时**一律不判烟雾**，宁可晚一帧也不要误报）。
        """
        hist = self._hist_smoke
        win = max(4, int(cfg["flicker_window"]))
        if len(hist) < win:
            return 0.0, 0.0, False
        past = hist[:-1] or [0.0]
        quiet = sorted(past)[:max(1, len(past) // 10)]
        base = quiet[len(quiet) // 2]
        recent = past[-win:]
        trend = sig.smoke_ratio - (sum(recent) / len(recent))
        return base, trend, True


# ============================================================ 确认闸门
class FireDetector(object):
    """
    带"确认/解除"闸门的检测器：后端每帧给分，本类负责**去抖动**。

    单帧高分不算火警 —— 闪光灯、车灯扫过、红色衣服飘过都会造成单帧高分。
    必须连续 confirm_frames 帧 ≥ 阈值才出告警；已报警后要连续 clear_frames 帧
    无火才判解除（避免火还在就反复"报警-解除"刷屏）。
    """

    def __init__(self, backend=None, cfg=None, on_alarm=None, on_clear=None):
        self.cfg = cfg or load_settings()
        self.backend = backend or VisionFireBackend(self.cfg)
        self.on_alarm = on_alarm            # 回调(det, event)：确认火警时
        self.on_clear = on_clear            # 回调(event)：解除时
        self.reset()

    def reset(self):
        self._hit = 0                        # 连续命中帧数
        self._miss = 0                       # 连续未命中帧数
        self._active = None                  # 当前进行中的事件
        self.last_detection = ok_detection(backend=getattr(self.backend, "name", "vision"))
        self._last_alarm_epoch = 0.0         # 上次建事件的时刻（冷却用）
        if hasattr(self.backend, "reset"):
            self.backend.reset()

    # ---------------- 查询 ----------------
    @property
    def active_event(self):
        return self._active

    @property
    def hit_streak(self):
        return self._hit

    def status_text(self):
        if self._active is not None:
            return "%s（%s，峰值 %.2f）" % (
                self._active.level_name(), self._active.status_text(),
                self._active.peak_score)
        if self._hit:
            return "疑似特征累计 %d/%d 帧" % (self._hit, int(self.cfg["confirm_frames"]))
        return "监控中，未发现火情"

    # ---------------- 主入口 ----------------
    def update(self, frame, camera_name="", camera_url=""):
        """
        喂一帧，返回本次的 Detection；需要建/解除事件时同步触发回调。
        本方法不做任何 IO（截图/录像由调用方在回调里做），所以可以在采集线程里直接调。
        """
        cfg = self.cfg
        det = self.backend.analyze(frame)
        self.last_detection = det
        need = int(cfg["confirm_frames"])
        clear_need = int(cfg["clear_frames"])

        if det.level is not None:            # 达到疑似线或火警线
            self._hit += 1
            self._miss = 0
        else:
            self._miss += 1
            if self._hit:
                self._hit = 0                # 中间断了就重新计数（防"隔帧凑数"）

        # ---- 确认：连续命中达标 ----
        if self._hit >= need:
            if self._active is None:
                now = now_epoch()
                cooldown = float(cfg["event_cooldown_sec"])
                if now - self._last_alarm_epoch >= cooldown:
                    ev = self._open_event(det, camera_name, camera_url)
                    self._active = ev
                    self._last_alarm_epoch = now
                    if self.on_alarm:
                        self.on_alarm(det, ev)
            else:
                self._active.observe(det)

        # ---- 解除：连续无火达标 ----
        if self._active is not None and self._miss >= clear_need:
            ev = self._active
            ev.clear()
            self._active = None
            self._hit = 0
            if self.on_clear:
                self.on_clear(ev)

        return det

    def _open_event(self, det, camera_name, camera_url):
        return FireEvent(camera_name=camera_name, camera_url=camera_url,
                                level=det.level or "alarm",
                                score=det.score, reason=det.reason_text(),
                                parts=det.parts, fire_ratio=det.fire_ratio,
                                smoke_ratio=det.smoke_ratio, regions=det.regions)

    # ---------------- 人工介入 ----------------
    def force_clear(self):
        """人工把当前事件判为已解除（例如确认无火）。"""
        if self._active is None:
            return None
        ev = self._active
        ev.clear(force=True)
        self._active = None
        self._hit = 0
        if self.on_clear:
            self.on_clear(ev)
        return ev


# ============================================================ 可视化
def draw_overlay(frame, det, cfg=None, extra_lines=None):
    """
    在帧上画检测结果（界面预览与事件标注图共用同一套画法，避免"看到的"和"存的"不一致）。
    返回新帧（不修改入参）。
    """
    if frame is None or cv2 is None or np is None:
        return frame
    out = frame.copy()
    cfg = cfg or load_settings()
    h, w = out.shape[:2]
    level = det.level if det is not None else None
    color = (0, 140, 255)
    if level == "alarm":
        color = (0, 0, 255)
    elif level == "warning":
        color = (0, 165, 255)

    # 火色区域
    for r in (det.regions if det else []) or []:
        x, y, bw, bh = r[0], r[1], r[2], r[3]
        cv2.rectangle(out, (x, y), (x + bw, y + bh), color, 2)
    # 烟雾区域
    for r in (det.smoke_regions if det else []) or []:
        x, y, bw, bh = r[0], r[1], r[2], r[3]
        cv2.rectangle(out, (x, y), (x + bw, y + bh), (180, 180, 180), 1)

    if det is not None and det.has_fire:
        text = "%s %.2f" % (det.level_name(), det.score)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(out, (6, 6), (16 + tw, 16 + th), (0, 0, 0), -1)
        cv2.putText(out, text, (10, 12 + th), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    color, 2, cv2.LINE_AA)

    lines = list(extra_lines or [])
    if det is not None:
        lines.append("火色 %.2f%%  烟 %.2f%%  闪烁 %.2f  增长 %+.2f"
                     % (det.fire_ratio * 100, det.smoke_ratio * 100,
                        det.parts.get("闪烁", 0.0) / max(1e-6, W_FLICKER_MAX),
                        (det.parts.get("增长", 0.0)) / max(1e-6, W_GROWTH_MAX)))
    y0 = h - 10 - 18 * max(0, len(lines) - 1)
    for i, ln in enumerate(lines):
        cv2.putText(out, ln, (10, y0 + i * 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, ln, (10, y0 + i * 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return out


def describe_backend(backend):
    """给界面/自检看的一行说明。"""
    name = getattr(backend, "name", "?")
    if name == "onnx":
        return "ONNX 模型（%s）" % getattr(backend, "model_path", "?")
    return "多信号融合（火色+去肤色+闪烁+增长+烟雾，无需模型）"


# ---------------------------------------------------------------------------
# Backend assembly (verbatim from 13/detector.py)
# ---------------------------------------------------------------------------
# -*- coding: utf-8 -*-



try:
    import numpy as np
except ImportError:
    np = None


class OnnxFireBackend(object):

    name = "onnx"

    def __init__(self, model_path, cfg=None, providers=None):
        if np is None:
            raise RuntimeError("缺少 numpy")
        import onnxruntime  # 延迟 import：没装时由调用方回落

        self.cfg = cfg or load_settings()
        self.model_path = model_path
        self.input_dim = int(self.cfg.get("onnx_input_dim") or 640)
        self.conf = float(self.cfg.get("onnx_conf") or 0.35)
        wanted = str(self.cfg.get("onnx_fire_classes") or "fire,smoke")
        self.class_names = [s.strip().lower() for s in wanted.split(",") if s.strip()]

        so = onnxruntime.SessionOptions()
        so.log_severity_level = 3                    # 静音 ORT 的啰嗦日志
        try:
            self._sess = onnxruntime.InferenceSession(
                model_path, sess_options=so, providers=providers)
        except Exception as exc:                     # noqa: BLE001
            raise RuntimeError("加载 ONNX 模型失败：%s" % str(exc)[:160])
        self._in = self._sess.get_inputs()[0]
        self._input_name = self._in.name
        shape = list(getattr(self._in, "shape", []) or [])
        # 有些导出的 NCHW 里 H/W 是动态的（'height'/'width'），取不到就用设置里的
        for i, tag in ((2, "height"), (3, "width")):
            if i < len(shape) and isinstance(shape[i], int) and shape[i] > 0:
                self.input_dim = int(shape[i])
                break
        self._loaded_ok = True

    # ---------------- 推理 ----------------
    def reset(self):
        """ONNX 后端无时序状态（闪烁/增长由上层不参与），保留接口一致性。"""

    def analyze(self, frame):
        import cv2

        if frame is None or getattr(frame, "ndim", 0) != 3:
            return ok_detection(self.name)
        h0, w0 = frame.shape[:2]
        blob, scale, pad = self._letterbox(frame)
        try:
            outs = self._sess.run(None, {self._input_name: blob})
        except Exception as exc:                     # noqa: BLE001
            return Detection(has_fire=False, score=0.0, backend=self.name,
                                    notes=["推理失败：%s" % str(exc)[:120]])
        boxes, scores, class_ids = self._decode(outs)
        if len(boxes) == 0:
            return ok_detection(self.name)

        keep = self._nms(boxes, scores, iou_thr=0.45)
        regions, best_fire, best_smoke, names = [], 0.0, 0.0, []
        total = float(max(1, h0 * w0))
        for i in keep:
            x1, y1, x2, y2 = boxes[i]
            # 反映射回原图
            x1 = (x1 - pad[0]) / scale
            y1 = (y1 - pad[1]) / scale
            x2 = (x2 - pad[0]) / scale
            y2 = (y2 - pad[1]) / scale
            x1, y1 = max(0, int(round(x1))), max(0, int(round(y1)))
            x2, y2 = min(w0, int(round(x2))), min(h0, int(round(y2)))
            bw, bh = max(1, x2 - x1), max(1, y2 - y1)
            cname = self._class_name(int(class_ids[i]))
            score = float(scores[i])
            names.append("%s %.2f" % (cname, score))
            if "fire" in cname or "flame" in cname or "火" in cname:
                best_fire = max(best_fire, score)
                regions.append((x1, y1, bw, bh, bw * bh))
            elif "smoke" in cname or "烟" in cname:
                best_smoke = max(best_smoke, score)
        area = sum(r[4] for r in regions) / total
        score = max(best_fire, best_smoke * 0.8)
        parts = {}
        if best_fire > 0:
            parts["模型-火"] = round(best_fire, 4)
        if best_smoke > 0:
            parts["模型-烟"] = round(best_smoke * 0.8, 4)
        return Detection(has_fire=score > 0, score=score, parts=parts,
                                fire_ratio=area, smoke_ratio=best_smoke,
                                regions=regions, backend=self.name,
                                notes=["检出：%s" % "、".join(names[:4])] if names else [])

    # ---------------- 预处理 ----------------
    def _letterbox(self, frame):
        import cv2
        h0, w0 = frame.shape[:2]
        dim = self.input_dim
        scale = min(dim / float(w0), dim / float(h0))
        nw, nh = max(1, int(round(w0 * scale))), max(1, int(round(h0 * scale)))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((dim, dim, 3), 114, np.uint8)
        dx, dy = (dim - nw) // 2, (dim - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = resized
        blob = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        return blob[None, ...], scale, (dx, dy)

    # ---------------- 后处理 ----------------
    def _class_name(self, cid):
        if 0 <= cid < len(self.class_names):
            return self.class_names[cid]
        return "class%d" % cid

    def _decode(self, outs):
        """
        把 (1,N,5+C) 或 (1,4+C,N) 统一解成 (boxes_xyxy, scores, class_ids)。
        识别不了就返回空（上层据此如实报"无检出"，不猜）。
        """
        if not outs:
            return [], [], []
        pred = np.asarray(outs[0])
        if pred.ndim == 3:
            pred = pred[0]
        if pred.ndim != 2:
            return [], [], []
        rows, cols = pred.shape
        # 转成 (N, features)：列数少的那一维是 features
        if cols > rows:
            pred = pred.T
        n, feat = pred.shape
        if feat < 6:
            return [], [], []

        if feat == 6:                        # [x1,y1,x2,y2,conf,cls]（已 NMS 的导出）
            boxes = pred[:, :4].astype(np.float32)
            scores = pred[:, 4].astype(np.float32)
            class_ids = pred[:, 5].astype(np.int32)
        else:
            # 判断有没有 obj 通道：YOLOv5/v7 是 5+C，v8/v11 是 4+C
            n_cls = len(self.class_names)
            boxed = pred[:, :4].astype(np.float32)
            rest = pred[:, 4:]
            if rest.shape[1] == n_cls + 1:
                obj = rest[:, 0]
                cls_scores = rest[:, 1:]
                class_ids = np.argmax(cls_scores, axis=1)
                scores = obj * cls_scores[np.arange(len(obj)), class_ids]
            else:
                cls_scores = rest
                class_ids = np.argmax(cls_scores, axis=1)
                scores = cls_scores[np.arange(len(class_ids)), class_ids]
            # cx,cy,w,h -> x1,y1,x2,y2
            cx, cy, bw, bh = boxed[:, 0], boxed[:, 1], boxed[:, 2], boxed[:, 3]
            boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

        keep = scores >= self.conf
        if not np.any(keep):
            return [], [], []
        return boxes[keep], scores[keep], class_ids[keep]

    @staticmethod
    def _nms(boxes, scores, iou_thr=0.45):
        """纯 numpy NMS，避免依赖 torchvision。返回保留的下标列表。"""
        if len(boxes) == 0:
            return []
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(int(i))
            if order.size == 1:
                break
            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])
            inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            union = areas[i] + areas[rest] - inter
            iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
            order = rest[iou <= iou_thr]
        return keep


# ============================================================ 工厂
def build_backend(cfg=None, force=None):
    """
    按设置装配后端。返回 (backend, 说明)。
    **永不抛异常**：ONNX 任何环节失败都回落到传统视觉，并把原因写在说明里。
    """

    cfg = cfg or load_settings()
    mode = (force or cfg.get("backend") or "auto").lower()

    if mode == "vision":
        return VisionFireBackend(cfg), "多信号传统视觉（按设置强制）"

    model = onnx_model_path(cfg)
    if mode == "onnx" and not model:
        b = VisionFireBackend(cfg)
        return b, "设置要求 ONNX，但 models\\ 下没有 fire*.onnx —— 已回落到传统视觉"

    if model:
        try:
            b = OnnxFireBackend(model, cfg)
            return b, "ONNX 模型：%s" % os.path.basename(model)
        except ImportError as exc:
            reason = "未安装 onnxruntime（%s）" % str(exc)[:60]
        except Exception as exc:                     # noqa: BLE001
            reason = str(exc)[:160]
        b = VisionFireBackend(cfg)
        return b, "ONNX 模型不可用（%s）—— 已回落到传统视觉" % reason

    return VisionFireBackend(cfg), "多信号传统视觉（当前没有可用 ONNX 模型）"


def build_detector(cfg=None, on_alarm=None, on_clear=None, force=None):
    """装配带确认闸门的 FireDetector。返回 (detector, 说明)。"""

    cfg = cfg or load_settings()
    backend, why = build_backend(cfg, force=force)
    det = FireDetector(backend=backend, cfg=cfg,
                                 on_alarm=on_alarm, on_clear=on_clear)
    return det, why


def describe_choice(cfg=None):
    """只探测不动手：告诉用户"现在会用哪个后端、为什么"。"""
    cfg = cfg or load_settings()
    model = onnx_model_path(cfg)
    try:
        import onnxruntime  # noqa: F401
        ort = True
    except Exception:
        ort = False
    mode = (cfg.get("backend") or "auto").lower()
    if mode == "vision":
        return {"后端": "多信号传统视觉", "原因": "设置为 vision"}
    if model and ort:
        return {"后端": "ONNX 模型", "模型": model, "原因": "找到模型且 onnxruntime 可用"}
    if model and not ort:
        return {"后端": "多信号传统视觉", "模型": model,
                "原因": "有模型但未安装 onnxruntime，已回落"}
    return {"后端": "多信号传统视觉",
            "原因": "models\\ 下没有 fire*.onnx（可放一个启用模型后端）"}

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
