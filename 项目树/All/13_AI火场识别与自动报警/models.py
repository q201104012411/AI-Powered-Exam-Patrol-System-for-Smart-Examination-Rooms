# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 数据模型

两件事：
  Detection —— **一帧**的判定结果（检测器每帧吐一个，不进库）
  FireEvent —— **一次火警事件**（进库、留证据、可确认/解除）

设计原则：Detection 里必须带 `parts`（各信号贡献分解）与 `regions`（火色区域框），
         这样报警时能说清"凭什么判的"，复核的人不用猜。
"""
import json
import time
import uuid

import config

# ---------------- 事件状态 ----------------
ST_FIRING = "firing"          # 正在报警（火还在/刚发现）
ST_ACKED = "acked"            # 人工已确认（值班人员已响应）
ST_CLEARED = "cleared"        # 已解除（连续多帧无火）
ST_FALSE = "false_alarm"      # 误报（人工标记，用于后续调参）

STATUSES = (ST_FIRING, ST_ACKED, ST_CLEARED, ST_FALSE)
STATUS_TEXT = {
    ST_FIRING: "报警中",
    ST_ACKED: "已确认",
    ST_CLEARED: "已解除",
    ST_FALSE: "误报",
}


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix="FE"):
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


# ============================================================ 单帧判定
class Detection(object):
    """一帧的检测结果。score 0~1；level 见 config.LEVELS。"""

    __slots__ = ("has_fire", "score", "level", "parts", "fire_ratio", "smoke_ratio",
                 "regions", "smoke_regions", "sharpness", "backend", "notes",
                 "created_epoch")

    def __init__(self, has_fire=False, score=0.0, level=None, parts=None,
                 fire_ratio=0.0, smoke_ratio=0.0, regions=None, smoke_regions=None,
                 sharpness=0.0, backend="vision", notes=None):
        self.has_fire = bool(has_fire)
        self.score = float(score)
        # 级别一律按 config 的阈值算，调用方只在明确要覆盖时才传 level
        if level not in config.LEVELS:
            level = config.level_for_score(self.score)
        self.level = level
        self.parts = dict(parts or {})          # {"火色":0.3,"闪烁":0.2,...}
        self.fire_ratio = float(fire_ratio)
        self.smoke_ratio = float(smoke_ratio)
        self.regions = list(regions or [])      # [(x,y,w,h,area_px), ...]
        self.smoke_regions = list(smoke_regions or [])
        self.sharpness = float(sharpness)
        self.backend = backend
        self.notes = list(notes or [])
        self.created_epoch = now_epoch()

    # ---- 展示 ----
    def level_name(self):
        if self.level not in config.LEVELS:
            return "正常"
        return config.LEVEL_META[self.level]["name"]

    def reason_text(self):
        """中文理由：把贡献最大的两三个信号说出来（给值班人员看的）。"""
        if not self.has_fire or not self.parts:
            return "无火情特征"
        items = sorted(self.parts.items(), key=lambda kv: -kv[1])
        top = ["%s %.2f" % (k, v) for k, v in items if v > 0][:3]
        return "、".join(top) if top else "综合判定"

    def to_dict(self):
        return {"has_fire": self.has_fire, "score": round(self.score, 4),
                "level": self.level, "level_name": self.level_name(),
                "reason": self.reason_text(),
                "parts": {k: round(v, 4) for k, v in self.parts.items()},
                "fire_ratio": round(self.fire_ratio, 5),
                "smoke_ratio": round(self.smoke_ratio, 5),
                "regions": [list(r) for r in self.regions],
                "smoke_regions": [list(r) for r in self.smoke_regions],
                "sharpness": round(self.sharpness, 2),
                "backend": self.backend, "notes": list(self.notes)}

    def __repr__(self):
        return "Detection(fire=%s score=%.3f level=%s fire_ratio=%.4f)" % (
            self.has_fire, self.score, self.level, self.fire_ratio)


def ok_detection(backend="vision"):
    """正常帧（无火）的快捷构造。"""
    return Detection(has_fire=False, score=0.0, level=None, backend=backend)


# ============================================================ 火警事件
class FireEvent(object):
    """一次火警事件（从"报警中"到"已解除/误报"的全过程留痕）。"""

    __slots__ = ("event_id", "camera_name", "camera_url", "level", "score",
                 "created_at", "created_epoch", "peak_score", "peak_epoch",
                 "frames_hit", "reason", "parts", "fire_ratio", "smoke_ratio",
                 "regions", "snapshot", "marked", "clip", "status", "acked_by",
                 "acked_at", "acked_note", "cleared_at", "duration_sec")

    def __init__(self, camera_name="", camera_url="", level=config.LEVEL_ALARM,
                 score=0.0, reason="", parts=None, fire_ratio=0.0, smoke_ratio=0.0,
                 regions=None, event_id=None, created_at=None):
        self.event_id = event_id or new_id("FE")
        self.camera_name = camera_name
        self.camera_url = str(camera_url)
        self.level = level if level in config.LEVELS else config.LEVEL_ALARM
        self.score = float(score)
        self.created_at = created_at or now_ts()
        self.created_epoch = now_epoch()
        self.peak_score = float(score)
        self.peak_epoch = self.created_epoch
        self.frames_hit = 1
        self.reason = reason
        self.parts = dict(parts or {})
        self.fire_ratio = float(fire_ratio)
        self.smoke_ratio = float(smoke_ratio)
        self.regions = list(regions or [])
        self.snapshot = ""          # 抓拍图（原始帧）
        self.marked = ""            # 标注图（画了框）
        self.clip = ""              # 短视频（告警前后）
        self.status = ST_FIRING
        self.acked_by = ""
        self.acked_at = ""
        self.acked_note = ""
        self.cleared_at = ""
        self.duration_sec = 0.0

    # ---- 状态迁移 ----
    def observe(self, det):
        """同一次火情里再来一帧：更新峰值与命中帧数。"""
        self.frames_hit += 1
        if det.score > self.peak_score:
            self.peak_score = float(det.score)
            self.peak_epoch = now_epoch()
        if det.level == config.LEVEL_ALARM and self.level != config.LEVEL_ALARM:
            self.level = config.LEVEL_ALARM      # 升级：疑似 -> 确认
        self.fire_ratio = max(self.fire_ratio, float(det.fire_ratio))
        self.smoke_ratio = max(self.smoke_ratio, float(det.smoke_ratio))
        if len(det.regions) > len(self.regions):
            self.regions = list(det.regions)

    def ack(self, by="值班员", note=""):
        """值班人员确认收到（响应留痕；火还可能继续烧）。"""
        self.status = ST_ACKED
        self.acked_by = by or "值班员"
        self.acked_at = now_ts()
        self.acked_note = note or ""
        return self

    def mark_false(self, by="值班员", note=""):
        """人工判定为误报（留存下来供后续调参，是降低误报的主要依据）。"""
        self.status = ST_FALSE
        self.acked_by = by or "值班员"
        self.acked_at = now_ts()
        self.acked_note = note or "人工判定为误报"
        return self

    def clear(self, force=False):
        """连续多帧无火后解除；误报事件不因"没火了"被改成已解除。"""
        if self.status == ST_FALSE:
            return self
        if self.status in (ST_FIRING, ST_ACKED) or force:
            self.status = ST_CLEARED
            self.cleared_at = now_ts()
            self.duration_sec = round(max(0.0, now_epoch() - self.created_epoch), 1)
        return self

    # ---- 展示 ----
    def level_name(self):
        return config.LEVEL_META.get(self.level, {}).get("name", self.level)

    def status_text(self):
        return STATUS_TEXT.get(self.status, self.status)

    def title(self):
        return "%s · %s" % (self.level_name(), self.camera_name or "未命名机位")

    def to_row(self):
        return {
            "event_id": self.event_id, "camera_name": self.camera_name,
            "camera_url": self.camera_url, "level": self.level,
            "score": self.score, "created_at": self.created_at,
            "created_epoch": self.created_epoch, "peak_score": self.peak_score,
            "peak_epoch": self.peak_epoch, "frames_hit": self.frames_hit,
            "reason": self.reason,
            "parts": json.dumps(self.parts, ensure_ascii=False),
            "fire_ratio": self.fire_ratio, "smoke_ratio": self.smoke_ratio,
            "regions": json.dumps(self.regions, ensure_ascii=False),
            "snapshot": self.snapshot, "marked": self.marked, "clip": self.clip,
            "status": self.status, "acked_by": self.acked_by,
            "acked_at": self.acked_at, "acked_note": self.acked_note,
            "cleared_at": self.cleared_at, "duration_sec": self.duration_sec,
        }

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        e = cls(camera_name=d.get("camera_name") or "",
                camera_url=d.get("camera_url") or "",
                level=d.get("level"), score=d.get("score") or 0.0,
                reason=d.get("reason") or "", fire_ratio=d.get("fire_ratio") or 0.0,
                smoke_ratio=d.get("smoke_ratio") or 0.0,
                event_id=d.get("event_id"), created_at=d.get("created_at"))
        e.created_epoch = d.get("created_epoch") or e.created_epoch
        e.peak_score = d.get("peak_score") or e.score
        e.peak_epoch = d.get("peak_epoch") or e.created_epoch
        e.frames_hit = int(d.get("frames_hit") or 1)
        e.snapshot = d.get("snapshot") or ""
        e.marked = d.get("marked") or ""
        e.clip = d.get("clip") or ""
        e.status = d.get("status") or ST_FIRING
        e.acked_by = d.get("acked_by") or ""
        e.acked_at = d.get("acked_at") or ""
        e.acked_note = d.get("acked_note") or ""
        e.cleared_at = d.get("cleared_at") or ""
        e.duration_sec = d.get("duration_sec") or 0.0
        try:
            e.parts = json.loads(d.get("parts") or "{}")
            if not isinstance(e.parts, dict):
                e.parts = {}
        except (ValueError, TypeError):
            e.parts = {}
        try:
            e.regions = json.loads(d.get("regions") or "[]")
            if not isinstance(e.regions, list):
                e.regions = []
        except (ValueError, TypeError):
            e.regions = []
        return e

    def to_dict(self):
        return {"event_id": self.event_id, "camera_name": self.camera_name,
                "camera_url": self.camera_url, "level": self.level,
                "level_name": self.level_name(), "score": round(self.score, 4),
                "peak_score": round(self.peak_score, 4), "frames_hit": self.frames_hit,
                "created_at": self.created_at, "reason": self.reason,
                "fire_ratio": round(self.fire_ratio, 5),
                "smoke_ratio": round(self.smoke_ratio, 5),
                "regions": [list(r) for r in self.regions],
                "snapshot": self.snapshot, "marked": self.marked, "clip": self.clip,
                "status": self.status, "status_text": self.status_text(),
                "acked_by": self.acked_by, "acked_at": self.acked_at,
                "acked_note": self.acked_note, "cleared_at": self.cleared_at,
                "duration_sec": self.duration_sec}

    def __repr__(self):
        return "FireEvent(%s %s score=%.3f %s)" % (
            self.event_id, self.level, self.score, self.status)
