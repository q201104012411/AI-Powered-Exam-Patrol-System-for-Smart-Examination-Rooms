# -*- coding: utf-8 -*-
r"""
考生数据保护 · 数据模型

所有涉及考生敏感信息的模型都遵守两条铁律：
  1. to_row() 只输出**已加密**的字段（加密在这里或 storage 里做，二选一但只做一次）；
  2. from_row() 之后由 storage 统一解密。
这样"库里是密文、内存里是明文"这件事只有一个出入口，验收时好核对。
"""
import hashlib
import json
import time
import uuid


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix="X"):
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_hash, payload):
    """与 01/02/03 一致的哈希链：sha256(prev + 规范化 JSON)"""
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 考生主体
class Candidate(object):
    """
    考生记录（对应分类分级里的"身份报名信息"+"人脸特征模板"+"人脸原始图像"）。

    明文只在内存里；落库时 name / id_card_no / ticket_no / seat_no / face_image_path
    全部字段级加密，face_template 存**不可逆摘要**。
    """

    __slots__ = ("cand_ref", "name", "id_card_no", "ticket_no", "seat_no", "room_id",
                 "site", "face_template", "face_image_path", "captured_epoch",
                 "retention_days", "legal_hold", "legal_hold_reason", "consent_id",
                 "created_at", "extra")

    def __init__(self, name, id_card_no="", ticket_no="", seat_no="", room_id="",
                 site="", face_template="", face_image_path="", cand_ref=None,
                 captured_epoch=None, retention_days=None, consent_id="",
                 extra=None, created_at=None):
        self.cand_ref = cand_ref or new_id("CD")     # 去标识化引用号（库内主键）
        self.name = name
        self.id_card_no = id_card_no
        self.ticket_no = ticket_no
        self.seat_no = seat_no
        self.room_id = room_id
        self.site = site
        self.face_template = face_template           # 不可逆摘要（tpl1:...）
        self.face_image_path = face_image_path       # 加密后的原始图像文件路径
        self.captured_epoch = float(captured_epoch or now_epoch())
        self.retention_days = retention_days
        self.legal_hold = 0
        self.legal_hold_reason = ""
        self.consent_id = consent_id
        self.created_at = created_at or now_ts()
        self.extra = dict(extra or {})

    @property
    def destroy_at_epoch(self):
        """到期销毁时刻；retention_days 为 None 表示沿用既有规定、不由本系统销毁。"""
        if self.retention_days is None:
            return None
        return self.captured_epoch + float(self.retention_days) * 86400.0

    def is_expired(self, now=None):
        if self.legal_hold:
            return False
        t = self.destroy_at_epoch
        return t is not None and (now or now_epoch()) >= t

    def display_deid(self):
        """去标识化展示（默认视图）"""
        from dpp import desensitize
        return desensitize.deidentify({"ref": self.cand_ref, "room_id": self.room_id,
                                       "seat_no": self.seat_no})

    def __repr__(self):
        return "<Candidate %s %s>" % (self.cand_ref, self.room_id)


# ---------------------------------------------------------------- 单独同意
class Consent(object):
    """
    单独同意记录（采集的前置条件）。
    scope 支持 "face"（人脸识别）等；withdrawn 表示已撤回，撤回后必须走替代通道。
    """

    __slots__ = ("consent_id", "cand_ref", "name", "scope", "granted_epoch",
                 "granted_at", "method", "operator", "withdrawn", "withdrawn_at",
                 "withdrawn_reason", "evidence_path", "extra")

    def __init__(self, cand_ref, name="", scope="face", method="考前告知书签署",
                 operator="", evidence_path="", consent_id=None, extra=None,
                 granted_at=None):
        self.consent_id = consent_id or new_id("CS")
        self.cand_ref = cand_ref
        self.name = name
        self.scope = scope
        self.method = method                 # 签署方式（纸质/电子），属可核查证据
        self.operator = operator
        self.evidence_path = evidence_path   # 同意书扫描件路径（加密留档）
        self.granted_epoch = now_epoch()
        self.granted_at = granted_at or now_ts()
        self.withdrawn = 0
        self.withdrawn_at = ""
        self.withdrawn_reason = ""
        self.extra = dict(extra or {})

    def to_row(self):
        return {"consent_id": self.consent_id, "cand_ref": self.cand_ref,
                "name": self.name, "scope": self.scope, "method": self.method,
                "operator": self.operator, "evidence_path": self.evidence_path,
                "granted_epoch": self.granted_epoch, "granted_at": self.granted_at,
                "withdrawn": self.withdrawn, "withdrawn_at": self.withdrawn_at,
                "withdrawn_reason": self.withdrawn_reason,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @classmethod
    def from_row(cls, r):
        d = dict(r)
        obj = cls(cand_ref=d.get("cand_ref") or "", name=d.get("name") or "",
                  scope=d.get("scope") or "face", method=d.get("method") or "",
                  operator=d.get("operator") or "",
                  evidence_path=d.get("evidence_path") or "",
                  consent_id=d.get("consent_id"), granted_at=d.get("granted_at"))
        obj.granted_epoch = float(d.get("granted_epoch") or 0)
        obj.withdrawn = int(d.get("withdrawn") or 0)
        obj.withdrawn_at = d.get("withdrawn_at") or ""
        obj.withdrawn_reason = d.get("withdrawn_reason") or ""
        try:
            obj.extra = json.loads(d.get("extra") or "{}")
        except (ValueError, TypeError):
            obj.extra = {}
        return obj


# ---------------------------------------------------------------- 目的限定使用
PURPOSE_ALLOWED = ("作弊判定", "身份核验")
PURPOSE_FORBIDDEN = ("情绪分析", "行为画像", "商业用途", "跨境传输")


class UseRecord(object):
    """一次数据使用（目的限定：仅作弊判定/身份核验，其余一律拒绝）。"""

    __slots__ = ("use_id", "cand_ref", "purpose", "action", "operator",
                 "terminal", "allowed", "deny_reason", "ts", "epoch")

    def __init__(self, cand_ref, purpose, action, operator="", terminal="",
                 allowed=True, deny_reason=""):
        self.use_id = new_id("US")
        self.cand_ref = cand_ref
        self.purpose = purpose
        self.action = action
        self.operator = operator
        self.terminal = terminal
        self.allowed = 1 if allowed else 0
        self.deny_reason = deny_reason
        self.ts = now_ts()
        self.epoch = now_epoch()


# ---------------------------------------------------------------- 解密查看
class RevealRecord(object):
    """授权解密查看留痕（人脸图像 / 明文证件号）"""

    __slots__ = ("reveal_id", "cand_ref", "target", "reason", "operator",
                 "terminal", "watermark", "ts", "epoch", "approved_by")

    def __init__(self, cand_ref, target, reason, operator="", terminal="",
                 watermark="", approved_by=""):
        self.reveal_id = new_id("RV")
        self.cand_ref = cand_ref
        self.target = target                  # face_image / id_card_no / all
        self.reason = reason
        self.operator = operator
        self.terminal = terminal
        self.watermark = watermark
        self.approved_by = approved_by
        self.ts = now_ts()
        self.epoch = now_epoch()


# ---------------------------------------------------------------- 共享/导出
class ShareRequest(object):
    """跨层级共享/导出：仅传必要字段，导出需审批。"""

    __slots__ = ("req_id", "cand_refs", "fields", "direction", "purpose",
                 "reason", "status", "created_at", "created_epoch", "applicant",
                 "approver", "approved_at", "approved_epoch", "reject_reason",
                 "export_path", "row_count")

    def __init__(self, cand_refs, fields, direction="up", purpose="作弊判定",
                 reason="", applicant=""):
        self.req_id = new_id("SH")
        self.cand_refs = list(cand_refs or [])
        self.fields = list(fields or [])
        self.direction = direction
        self.purpose = purpose
        self.reason = reason
        self.status = "pending"               # pending -> approved / rejected
        self.created_at = now_ts()
        self.created_epoch = now_epoch()
        self.applicant = applicant
        self.approver = ""
        self.approved_at = ""
        self.approved_epoch = 0.0
        self.reject_reason = ""
        self.export_path = ""
        self.row_count = 0


# ---------------------------------------------------------------- 销毁
class DestroyRecord(object):
    """
    销毁记录（不可篡改：内容参与哈希链）。
    mode: auto（到期自动）/ manual（按申请）/ force（强制，需双人确认）
    """

    __slots__ = ("destroy_id", "cand_ref", "category", "mode", "reason",
                 "operator", "witness", "created_epoch", "covered", "ts")

    def __init__(self, cand_ref, category, mode, reason="", operator="",
                 witness="", covered=None):
        self.destroy_id = new_id("DS")
        self.cand_ref = cand_ref
        self.category = category
        self.mode = mode
        self.reason = reason
        self.operator = operator
        self.witness = witness                # 强制销毁的双人确认见证人
        self.created_epoch = now_epoch()
        self.covered = dict(covered or {})    # {"fields": [...], "files": [...]}
        self.ts = now_ts()


# ---------------------------------------------------------------- 考生权利申请
class RightsRequest(object):
    """
    考生权利响应申请：知情/查阅/复制/更正/删除/撤回同意。
    时限按 config.RIGHTS_SLA_DAYS 计算，超期要在报告里标红。
    """

    __slots__ = ("req_id", "cand_ref", "right", "detail", "status",
                 "created_at", "created_epoch", "due_epoch", "handler",
                 "handled_at", "handled_epoch", "result", "reason", "channel",
                 "alt_channel_used")

    def __init__(self, cand_ref, right, detail="", channel="窗口受理",
                 alt_channel_used=False, handler=""):
        self.req_id = new_id("RQ")
        self.cand_ref = cand_ref
        self.right = right
        self.detail = detail
        self.status = "open"                  # open -> done / rejected
        self.created_at = now_ts()
        self.created_epoch = now_epoch()
        self.due_epoch = 0.0
        self.handler = handler
        self.handled_at = ""
        self.handled_epoch = 0.0
        self.result = ""
        self.reason = ""
        self.channel = channel
        self.alt_channel_used = 1 if alt_channel_used else 0

    def is_overdue(self, now=None):
        if self.status != "open" or not self.due_epoch:
            return False
        return (now or now_epoch()) > self.due_epoch

    def remaining_days(self, now=None):
        if not self.due_epoch:
            return None
        left = self.due_epoch - (now or now_epoch())
        return round(left / 86400.0, 2)


# ---------------------------------------------------------------- 替代通道
class AlternateChannel(object):
    """
    非人脸替代核验通道（合规硬要求：个性化智能环节必须保留人工/非人脸路径）。
    available=1 且 last_drill_at 非空 才算"可用且有演练记录"。
    """

    __slots__ = ("channel_id", "site", "name", "kind", "available",
                 "last_drill_at", "drill_passed", "note")

    def __init__(self, site, name="人工核验通道", kind="manual", channel_id=None,
                 available=1, note=""):
        self.channel_id = channel_id or new_id("AC")
        self.site = site
        self.name = name
        self.kind = kind                      # manual（人工）/ document（证件核验）
        self.available = int(available)
        self.last_drill_at = ""
        self.drill_passed = 0
        self.note = note


# ---------------------------------------------------------------- 管理措施
class ManagementMeasure(object):
    """管理措施台账：责任人 / DPIA / 权限审批 / 应急响应 / 培训"""

    __slots__ = ("measure_id", "kind", "title", "owner", "owner_role", "status",
                 "due_at", "done_at", "evidence", "detail")

    def __init__(self, kind, title, owner="", owner_role="", status="open",
                 due_at="", evidence="", detail="", measure_id=None):
        self.measure_id = measure_id or new_id("MG")
        self.kind = kind
        self.title = title
        self.owner = owner
        self.owner_role = owner_role
        self.status = status                  # open / done / overdue
        self.due_at = due_at
        self.done_at = ""
        self.evidence = evidence
        self.detail = detail


class Incident(object):
    """泄露/安全事件：2 小时内上报，留存处置记录。"""

    __slots__ = ("incident_id", "level", "title", "detail", "found_epoch",
                 "reported_epoch", "handler", "status", "disposal",
                 "disposal_epoch")

    def __init__(self, level, title, detail="", handler=""):
        self.incident_id = new_id("IN")
        self.level = level
        self.title = title
        self.detail = detail
        self.found_epoch = now_epoch()
        self.reported_epoch = 0.0
        self.handler = handler
        self.status = "found"                 # found -> reported -> closed
        self.disposal = ""
        self.disposal_epoch = 0.0

    def report_delay_hours(self):
        if not self.reported_epoch:
            return None
        return round((self.reported_epoch - self.found_epoch) / 3600.0, 3)
