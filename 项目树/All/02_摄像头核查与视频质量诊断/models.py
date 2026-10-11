# -*- coding: utf-8 -*-
"""摄像头核查与视频质量诊断 · 数据模型与审计哈希链"""
import hashlib
import json
import time
import uuid

import config


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix="C"):
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


class Device(object):
    """设备台账（核查内容表最后一行：设备编码、考场对应、安装位置、角度参数）"""

    __slots__ = ("device_id", "code", "name", "room_id", "site", "position", "url",
                 "resolution", "fov_deg", "pan_deg", "tilt_deg", "baseline_at",
                 "last_heartbeat", "enabled", "has_audio", "expected_segments",
                 "session", "extra")

    def __init__(self, code, room_id="", name="", site="", position="", url="",
                 resolution="1920x1080", fov_deg=90.0, pan_deg=0.0, tilt_deg=0.0,
                 has_audio=False, session="", device_id=None, extra=None):
        self.device_id = device_id or new_id("DEV")
        self.code = code
        self.name = name or code
        self.room_id = room_id
        self.site = site
        self.position = position
        self.url = url
        self.resolution = resolution
        self.fov_deg = float(fov_deg)
        self.pan_deg = float(pan_deg)
        self.tilt_deg = float(tilt_deg)
        self.baseline_at = ""
        self.last_heartbeat = 0.0
        self.enabled = 1
        self.has_audio = 1 if has_audio else 0
        self.expected_segments = None
        self.session = session
        self.extra = dict(extra or {})

    def ledger_problems(self):
        """台账核对：编码/考场/角度参数是否齐全"""
        problems = []
        if not self.code:
            problems.append("缺少设备编码")
        if not self.room_id:
            problems.append("未绑定考场")
        if self.fov_deg <= 0:
            problems.append("缺少/非法视场角")
        return problems

    def to_row(self):
        return {"device_id": self.device_id, "code": self.code, "name": self.name,
                "room_id": self.room_id, "site": self.site, "position": self.position,
                "url": self.url, "resolution": self.resolution,
                "fov_deg": self.fov_deg, "pan_deg": self.pan_deg, "tilt_deg": self.tilt_deg,
                "baseline_at": self.baseline_at, "last_heartbeat": self.last_heartbeat,
                "enabled": self.enabled, "has_audio": self.has_audio,
                "session": self.session,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        try:
            extra = json.loads(d.get("extra") or "{}")
        except (ValueError, TypeError):
            extra = {}
        dev = cls(code=d["code"], room_id=d.get("room_id") or "", name=d.get("name") or "",
                  site=d.get("site") or "", position=d.get("position") or "",
                  url=d.get("url") or "", resolution=d.get("resolution") or "1920x1080",
                  fov_deg=d.get("fov_deg") or 90.0, pan_deg=d.get("pan_deg") or 0.0,
                  tilt_deg=d.get("tilt_deg") or 0.0,
                  has_audio=bool(d.get("has_audio")), session=d.get("session") or "",
                  device_id=d.get("device_id"), extra=extra)
        dev.baseline_at = d.get("baseline_at") or ""
        dev.last_heartbeat = float(d.get("last_heartbeat") or 0)
        dev.enabled = int(d.get("enabled") or 0)
        return dev


class Defect(object):
    """一条缺陷：码 + 严重级 + 实测指标 + 说明"""

    __slots__ = ("code", "severity", "metrics", "detail")

    def __init__(self, code, metrics=None, detail="", severity=None):
        self.code = code
        self.severity = severity or config.defect_sev(code)
        self.metrics = dict(metrics or {})
        self.detail = detail or config.defect_name(code)

    def to_dict(self):
        return {"code": self.code, "name": config.defect_name(self.code),
                "severity": self.severity, "detail": self.detail,
                "metrics": {k: _round(v) for k, v in self.metrics.items()}}

    def __repr__(self):
        return "<Defect %s %s>" % (self.code, self.severity)


def _round(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return v


class CheckResult(object):
    """一次机位核查的结果"""

    __slots__ = ("check_id", "device_id", "device_code", "room_id", "policy", "ts",
                 "epoch", "online", "defects", "metrics", "frame_path", "baseline_path",
                 "operator", "note")

    def __init__(self, device, policy, online=True, defects=None, metrics=None,
                 frame_path="", baseline_path="", operator="", check_id=None,
                 note=""):
        self.check_id = check_id or new_id("CK")
        self.device_id = device.device_id
        self.device_code = device.code
        self.room_id = device.room_id
        self.policy = policy
        self.ts = now_ts()
        self.epoch = now_epoch()
        self.online = 1 if online else 0
        self.defects = list(defects or [])
        self.metrics = dict(metrics or {})
        self.frame_path = frame_path
        self.baseline_path = baseline_path
        self.operator = operator
        self.note = note

    @property
    def ok(self):
        return self.online == 1 and not any(d.severity == config.SEV_FAIL for d in self.defects)

    def fail_codes(self):
        return [d.code for d in self.defects if d.severity == config.SEV_FAIL]

    def warn_codes(self):
        return [d.code for d in self.defects if d.severity == config.SEV_WARN]

    def to_row(self):
        return {"check_id": self.check_id, "device_id": self.device_id,
                "device_code": self.device_code, "room_id": self.room_id,
                "policy": self.policy, "ts": self.ts, "epoch": self.epoch,
                "online": self.online,
                "defects": json.dumps([d.to_dict() for d in self.defects], ensure_ascii=False),
                "metrics": json.dumps({k: _round(v) for k, v in self.metrics.items()},
                                      ensure_ascii=False),
                "frame_path": self.frame_path, "baseline_path": self.baseline_path,
                "operator": self.operator, "note": self.note,
                "ok": 1 if self.ok else 0}

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        obj = cls.__new__(cls)
        obj.check_id = d["check_id"]
        obj.device_id = d["device_id"]
        obj.device_code = d["device_code"]
        obj.room_id = d.get("room_id") or ""
        obj.policy = d.get("policy") or ""
        obj.ts = d.get("ts") or ""
        obj.epoch = float(d.get("epoch") or 0)
        obj.online = int(d.get("online") or 0)
        try:
            raw = json.loads(d.get("defects") or "[]")
        except (ValueError, TypeError):
            raw = []
        obj.defects = [Defect(x.get("code"), x.get("metrics"), x.get("detail"),
                              x.get("severity")) for x in raw]
        try:
            obj.metrics = json.loads(d.get("metrics") or "{}")
        except (ValueError, TypeError):
            obj.metrics = {}
        obj.frame_path = d.get("frame_path") or ""
        obj.baseline_path = d.get("baseline_path") or ""
        obj.operator = d.get("operator") or ""
        obj.note = d.get("note") or ""
        return obj

    def display(self):
        if not self.online:
            head = "离线/取流失败"
        elif self.defects:
            head = "、".join("%s(%s)" % (config.defect_name(d.code), d.severity)
                            for d in self.defects)
        else:
            head = "正常"
        return "[%s] %s %s 考场=%s 机位=%s" % (self.ts, self.policy,
                                              head, self.room_id or "-", self.device_code)


class WorkOrder(object):
    """整改工单：发现—派单—整改—复核 闭环（第四节第 3 条）"""

    __slots__ = ("order_id", "device_id", "device_code", "room_id", "codes", "severity",
                 "status", "created_at", "assigned_to", "dispatched_at", "rectified_at",
                 "reviewed_at", "before_path", "after_path", "note", "review_note",
                 "reviewer", "operator")

    def __init__(self, device, codes, severity, note="", operator="system",
                 order_id=None):
        self.order_id = order_id or new_id("WO")
        self.device_id = device.device_id
        self.device_code = device.code
        self.room_id = device.room_id
        self.codes = list(codes)
        self.severity = severity
        self.status = "open"           # open -> dispatched -> rectified -> closed
        self.created_at = now_ts()
        self.assigned_to = ""
        self.dispatched_at = ""
        self.rectified_at = ""
        self.reviewed_at = ""
        self.before_path = ""
        self.after_path = ""
        self.note = note
        self.review_note = ""
        self.reviewer = ""
        self.operator = operator

    def to_row(self):
        return {"order_id": self.order_id, "device_id": self.device_id,
                "device_code": self.device_code, "room_id": self.room_id,
                "codes": json.dumps(self.codes, ensure_ascii=False),
                "severity": self.severity, "status": self.status,
                "created_at": self.created_at, "assigned_to": self.assigned_to,
                "dispatched_at": self.dispatched_at, "rectified_at": self.rectified_at,
                "reviewed_at": self.reviewed_at, "before_path": self.before_path,
                "after_path": self.after_path, "note": self.note,
                "review_note": self.review_note, "reviewer": self.reviewer,
                "operator": self.operator}

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        obj = cls.__new__(cls)
        for k in ("order_id", "device_id", "device_code", "room_id", "severity",
                  "status", "created_at", "assigned_to", "dispatched_at",
                  "rectified_at", "reviewed_at", "before_path", "after_path",
                  "note", "review_note", "reviewer", "operator"):
            setattr(obj, k, d.get(k) or "")
        try:
            obj.codes = json.loads(d.get("codes") or "[]")
        except (ValueError, TypeError):
            obj.codes = []
        return obj


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_hash, payload):
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()
