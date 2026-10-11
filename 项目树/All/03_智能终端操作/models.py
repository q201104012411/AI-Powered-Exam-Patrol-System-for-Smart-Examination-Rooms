# -*- coding: utf-8 -*-
"""智能终端操作端 · 数据模型与审计哈希链"""
import hashlib
import json
import time
import uuid

import config

# ---------------- 告警状态 ----------------
ST_PENDING = "pending"        # 待处置
ST_ACKED = "acked"            # 已确认（红色预警强制确认）
ST_REVIEWED = "reviewed"      # 已复核
ST_CLOSED = "closed"          # 已归档


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix="A"):
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


class Alert(object):
    """
    一条告警。字段与《智能终端.md》要求对应：
      分级展示 -> level / meta()
      复核留痕 -> review_* 字段
      断网可用 -> synced / queued_at
      权限最小化 -> room_id（监考终端只看本考场）
    """

    __slots__ = ("alert_id", "code", "level", "title", "detail", "room_id", "seat_no",
                 "candidate_id", "candidate_name", "device_id", "source", "created_at",
                 "created_epoch", "status", "acked_by", "acked_at", "review_conclusion",
                 "review_reason", "review_by", "review_at", "attachments", "synced",
                 "queued_at", "false_positive", "sla_deadline", "extra")

    def __init__(self, code, title="", detail="", room_id="", seat_no="",
                 candidate_id="", candidate_name="", device_id="", source="edge",
                 level=None, alert_id=None, created_at=None, extra=None):
        self.alert_id = alert_id or new_id("AL")
        self.code = code
        self.level = level or config.ALERT_CODE_LEVEL.get(code, config.LEVEL_ORANGE)
        if self.level not in config.LEVELS:
            self.level = config.LEVEL_ORANGE
        self.title = title or self.level_name()
        self.detail = detail
        self.room_id = room_id
        self.seat_no = seat_no
        self.candidate_id = candidate_id
        self.candidate_name = candidate_name
        self.device_id = device_id
        self.source = source
        self.created_at = created_at or now_ts()
        self.created_epoch = now_epoch()
        self.status = ST_PENDING
        self.acked_by = ""
        self.acked_at = ""
        self.review_conclusion = ""
        self.review_reason = ""
        self.review_by = ""
        self.review_at = ""
        self.attachments = []
        self.synced = 0
        self.queued_at = ""
        self.false_positive = 0
        self.sla_deadline = self.created_epoch + self.meta()["sla_sec"]
        self.extra = dict(extra or {})

    # ---- 分级展示相关 ----
    def meta(self):
        return config.LEVEL_META[self.level]

    def level_name(self):
        return self.meta()["name"]

    def needs_force_confirm(self):
        """红色预警必须强制确认"""
        return bool(self.meta()["force_confirm"]) and self.status == ST_PENDING

    def overdue(self):
        """是否已超出处置时限（SLA）"""
        return self.status == ST_PENDING and now_epoch() > self.sla_deadline

    # ---- 序列化 ----
    def to_row(self):
        return {
            "alert_id": self.alert_id, "code": self.code, "level": self.level,
            "title": self.title, "detail": self.detail, "room_id": self.room_id,
            "seat_no": self.seat_no, "candidate_id": self.candidate_id,
            "candidate_name": self.candidate_name, "device_id": self.device_id,
            "source": self.source, "created_at": self.created_at,
            "created_epoch": self.created_epoch, "status": self.status,
            "acked_by": self.acked_by, "acked_at": self.acked_at,
            "review_conclusion": self.review_conclusion, "review_reason": self.review_reason,
            "review_by": self.review_by, "review_at": self.review_at,
            "attachments": json.dumps(self.attachments, ensure_ascii=False),
            "synced": self.synced, "queued_at": self.queued_at,
            "false_positive": self.false_positive,
            "sla_deadline": self.sla_deadline,
            "extra": json.dumps(self.extra, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        a = cls(code=d["code"], title=d["title"], detail=d.get("detail") or "",
                room_id=d.get("room_id") or "", seat_no=d.get("seat_no") or "",
                candidate_id=d.get("candidate_id") or "",
                candidate_name=d.get("candidate_name") or "",
                device_id=d.get("device_id") or "", source=d.get("source") or "edge",
                level=d.get("level"), alert_id=d.get("alert_id"),
                created_at=d.get("created_at"))
        for k in ("status", "acked_by", "acked_at", "review_conclusion", "review_reason",
                  "review_by", "review_at", "queued_at"):
            setattr(a, k, d.get(k) or "")
        a.created_epoch = d.get("created_epoch") or a.created_epoch
        a.synced = int(d.get("synced") or 0)
        a.false_positive = int(d.get("false_positive") or 0)
        a.sla_deadline = d.get("sla_deadline") or a.sla_deadline
        try:
            a.attachments = json.loads(d.get("attachments") or "[]")
        except (ValueError, TypeError):
            a.attachments = []
        try:
            a.extra = json.loads(d.get("extra") or "{}")
        except (ValueError, TypeError):
            a.extra = {}
        return a

    def display(self, masked=True):
        """展示用文本；默认脱敏（权限最小化要求）"""
        who = self.candidate_name or "—"
        cid = self.candidate_id or "—"
        if masked and self.candidate_id:
            cid = _mask_id(self.candidate_id)
        return "[%s] %s ｜ %s ｜ 考场 %s 座位 %s ｜ 考生 %s(%s)" % (
            self.level_name(), self.code, self.created_at,
            self.room_id or "—", self.seat_no or "—", who, cid)


def _mask_id(v):
    """与 01 项目一致的掩码口径：18 位身份证 -> 3301**********1234"""
    v = str(v)
    if len(v) <= 4:
        return "*" * len(v)
    if len(v) <= 8:
        return v[:1] + "*" * (len(v) - 2) + v[-1]
    return v[:4] + "*" * (len(v) - 8) + v[-4:]


class ReviewRecord(object):
    """复核留痕：结论 + 理由 + 附件（《智能终端.md》第二节第 2 条）"""

    __slots__ = ("review_id", "alert_id", "conclusion", "reason", "operator", "witness",
                 "attachments", "created_at", "closed_loop")

    def __init__(self, alert_id, conclusion, reason="", operator="", witness="",
                 attachments=None, review_id=None, created_at=None):
        if conclusion not in config.REVIEW_CONCLUSIONS:
            raise ValueError("复核结论不合法：%s" % conclusion)
        self.review_id = review_id or new_id("RV")
        self.alert_id = alert_id
        self.conclusion = conclusion
        self.reason = reason
        self.operator = operator
        self.witness = witness
        self.attachments = list(attachments or [])
        self.created_at = created_at or now_ts()
        # 误报进入算法迭代闭环
        self.closed_loop = 1 if conclusion == config.REVIEW_FALSE_POSITIVE else 0

    def to_row(self):
        return {
            "review_id": self.review_id, "alert_id": self.alert_id,
            "conclusion": self.conclusion, "reason": self.reason,
            "operator": self.operator, "witness": self.witness,
            "attachments": json.dumps(self.attachments, ensure_ascii=False),
            "created_at": self.created_at, "closed_loop": self.closed_loop,
        }

    @classmethod
    def from_row(cls, row):
        import json
        return cls(
            alert_id=row.get("alert_id", ""),
            conclusion=row.get("conclusion", config.REVIEW_CONFIRMED),
            reason=row.get("reason", ""),
            operator=row.get("operator", ""),
            witness=row.get("witness", ""),
            attachments=json.loads(row.get("attachments", "[]") or "[]"),
            review_id=row.get("review_id"),
            created_at=row.get("created_at"),
        )


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_hash, payload):
    """与 01 一致的哈希链：sha256(prev + 规范化 JSON)"""
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()
