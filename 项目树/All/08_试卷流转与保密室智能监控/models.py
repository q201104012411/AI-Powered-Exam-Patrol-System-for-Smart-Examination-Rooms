# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 数据模型 + 哈希链审计

模型清单（与《试卷流转与保密室智能监控.md》表格逐条对应）：
  保密室侧
    Camera      机位（6 面覆盖登记、双路冗余、录像通道）
    AccessEvent 门禁事件（人脸+刷卡/密码双因子、双人双锁、尾随）
    IntrusionEvent 入侵探测事件（红外双鉴/门窗磁/震动/开柜）
    SafeEvent   试卷柜电子锁事件（开柜记录 + 柜内状态）
    EnvSample   环境监测采样（温湿度/水浸/烟感）
  流转侧
    Bag         试卷袋（RFID/二维码、电子铅封状态、数量）
    Handover    交接记录（双人签字 + 人脸确认 + 扫码/RFID）
    EscortTask  押运任务（车辆、路线、定位、车内视频）
    EscortPoint 押运轨迹点（北斗/GPS）
    Delivery    分发至考场记录
    Recycle     回收与封存记录
  通用
    Alarm       告警（含处置留痕）
    RecordSegment 录像段（连续性/留存/完整性核查）

审计：AuditChain —— 哈希链，任何一条被改动都会在 verify() 暴露。
"""
import hashlib
import json
import time
import uuid

import config


# ---------------- 通用工具 ----------------
def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix="X"):
    return "%s%s" % (prefix, uuid.uuid4().hex[:12])


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------- 哈希链 ----------------
GENESIS = "0" * 64


class AuditChain(object):
    """审计哈希链：prev_hash -> hash，逐条串起来。改任何一条，后续全部对不上。"""

    @staticmethod
    def calc(prev_hash, ts, action, detail, operator):
        raw = "%s|%s|%s|%s|%s" % (prev_hash, ts, action, detail, operator)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def verify(rows):
        """rows: [{"prev_hash","hash","ts","action","detail","operator"}, ...] 按 seq 升序。

        返回 (ok, 首个断点说明)。空链视为合法（还没记录）。
        """
        prev = GENESIS
        for i, r in enumerate(rows):
            if r.get("prev_hash") != prev:
                return False, "第 %d 条 prev_hash 断链（期望 %s…，实际 %s…）" % (
                    i + 1, prev[:10], str(r.get("prev_hash"))[:10])
            want = AuditChain.calc(prev, r.get("ts"), r.get("action"),
                                   r.get("detail"), r.get("operator"))
            if want != r.get("hash"):
                return False, "第 %d 条内容与哈希不符（记录被改动）" % (i + 1)
            prev = want
        return True, "OK（共 %d 条，链头 %s…）" % (len(rows), prev[:10])


# ---------------- 告警状态 ----------------
ST_PENDING = "pending"
ST_ACKED = "acked"
ST_HANDLED = "handled"
ST_CLOSED = "closed"


class Alarm(object):
    """一条告警。level 决定展示方式（橙/红），sla_sec 用于时延核查。"""

    __slots__ = ("alarm_id", "code", "level", "title", "detail", "source_kind",
                 "source_id", "scope", "created_at", "created_epoch", "status",
                 "acked_by", "acked_at", "handled_by", "handled_at", "handle_note",
                 "sla_sec", "latency_sec", "evidence", "extra")

    def __init__(self, code, title="", detail="", source_kind="", source_id="",
                 scope="", level=None, latency_sec=None, evidence=None, extra=None):
        self.alarm_id = new_id("AL")
        self.code = code
        self.level = level or config.ALERT_CODE_LEVEL.get(code, config.LEVEL_ORANGE)
        if self.level not in config.LEVELS:
            self.level = config.LEVEL_ORANGE
        self.title = title or config.ALERT_CODE_TEXT.get(code, code)
        self.detail = detail
        self.source_kind = source_kind          # door / safe / intrusion / env / video / flow
        self.source_id = source_id
        self.scope = scope                      # 保密室编号 或 车牌/考场
        self.created_at = now_ts()
        self.created_epoch = now_epoch()
        self.status = ST_PENDING
        self.acked_by = ""
        self.acked_at = ""
        self.handled_by = ""
        self.handled_at = ""
        self.handle_note = ""
        self.sla_sec = self.meta()["sla_sec"]
        # 事件发生 -> 告警产生 的时延（用于"≤5 秒/≤30 秒"指标核查）
        self.latency_sec = latency_sec
        self.evidence = evidence or ""          # 留证截图相对路径
        self.extra = dict(extra or {})

    def meta(self):
        return config.LEVEL_META[self.level]

    def level_name(self):
        return self.meta()["name"]

    def is_red(self):
        return self.level == config.LEVEL_RED

    def to_row(self):
        return {
            "alarm_id": self.alarm_id, "code": self.code, "level": self.level,
            "title": self.title, "detail": self.detail,
            "source_kind": self.source_kind, "source_id": self.source_id,
            "scope": self.scope, "created_at": self.created_at,
            "created_epoch": self.created_epoch, "status": self.status,
            "acked_by": self.acked_by, "acked_at": self.acked_at,
            "handled_by": self.handled_by, "handled_at": self.handled_at,
            "handle_note": self.handle_note, "sla_sec": self.sla_sec,
            "latency_sec": self.latency_sec, "evidence": self.evidence,
            "extra": json.dumps(self.extra, ensure_ascii=False),
        }

    @staticmethod
    def from_row(row):
        a = Alarm(row["code"], alert_id=None) if False else Alarm(row["code"])
        a.alarm_id = row["alarm_id"]
        for k in ("level", "title", "detail", "source_kind", "source_id", "scope",
                  "created_at", "created_epoch", "status", "acked_by", "acked_at",
                  "handled_by", "handled_at", "handle_note", "sla_sec",
                  "latency_sec", "evidence"):
            if row[k] is not None:
                setattr(a, k, row[k])
        try:
            a.extra = json.loads(row["extra"] or "{}")
        except Exception:
            a.extra = {}
        return a


# ---------------- 保密室：机位 ----------------
class Camera(object):
    """监控机位。face 为 6 面之一；channels 记录冗余录像通道（主/备）。"""

    __slots__ = ("cam_id", "room_id", "name", "face", "url", "angle_deg",
                 "checkpoints", "channels", "baseline_hash", "ledger",
                 "online", "last_seen", "registered_at")

    def __init__(self, cam_id, room_id, name, face, url="", angle_deg=0.0,
                 checkpoints=None, channels=None, ledger=None):
        self.cam_id = cam_id
        self.room_id = room_id
        self.name = name
        self.face = face                     # 必须落在 config.COVER_FACES
        self.url = url
        self.angle_deg = _f(angle_deg)
        self.checkpoints = list(checkpoints or [])
        self.channels = list(channels or [])  # 例如 ["main","backup"]
        self.baseline_hash = ""
        self.ledger = dict(ledger or {})      # 安装位置/参数等台账
        self.online = False
        self.last_seen = ""
        self.registered_at = now_ts()

    def ledger_problems(self):
        """台账必填项核对，缺失即报，用于"逐机位留档验证"。"""
        p = []
        if self.face not in config.COVER_FACES:
            p.append("机位朝向未登记为六面之一（当前：%s）" % (self.face or "空"))
        if not self.room_id:
            p.append("未关联保密室编号")
        if not self.name:
            p.append("机位名称为空")
        if not self.url:
            p.append("未配置取流地址")
        if len(self.channels) < config.REDUNDANT_CHANNELS_REQUIRED:
            p.append("冗余录像通道不足（要求 %d 路，当前 %d 路）"
                     % (config.REDUNDANT_CHANNELS_REQUIRED, len(self.channels)))
        need = config.FACE_CHECKPOINTS.get(self.face, [])
        missing = [c for c in need if c not in self.checkpoints]
        if missing:
            p.append("该朝向必拍要点未覆盖：%s" % "、".join(missing))
        return p

    def to_row(self):
        return {
            "cam_id": self.cam_id, "room_id": self.room_id, "name": self.name,
            "face": self.face, "url": self.url, "angle_deg": self.angle_deg,
            "checkpoints": json.dumps(self.checkpoints, ensure_ascii=False),
            "channels": json.dumps(self.channels, ensure_ascii=False),
            "baseline_hash": self.baseline_hash,
            "ledger": json.dumps(self.ledger, ensure_ascii=False),
            "online": 1 if self.online else 0, "last_seen": self.last_seen,
            "registered_at": self.registered_at,
        }

    @staticmethod
    def from_row(row):
        c = Camera(row["cam_id"], row["room_id"], row["name"], row["face"],
                   url=row["url"] or "", angle_deg=row["angle_deg"] or 0.0,
                   checkpoints=json.loads(row["checkpoints"] or "[]"),
                   channels=json.loads(row["channels"] or "[]"),
                   ledger=json.loads(row["ledger"] or "{}"))
        c.baseline_hash = row["baseline_hash"] or ""
        c.online = bool(row["online"])
        c.last_seen = row["last_seen"] or ""
        c.registered_at = row["registered_at"] or ""
        return c


# ---------------- 保密室：门禁事件 ----------------
class AccessEvent(object):
    """一次门禁动作。

    双因子：factors 里必须同时含 1 个生物/凭证 + 1 个凭证；
    双人双锁：persons 里的授权人数必须 >= DUAL_CUSTODY_REQUIRED。
    """

    __slots__ = ("event_id", "room_id", "person", "factors", "persons",
                 "result", "deny_reason", "ts", "epoch", "extra")

    def __init__(self, room_id, person, factors=None, persons=None,
                 result="deny", deny_reason="", ts=None, epoch=None, extra=None):
        self.event_id = new_id("AC")
        self.room_id = room_id
        self.person = person
        self.factors = list(factors or [])
        self.persons = list(persons or [])
        self.result = result                   # allow / deny
        self.deny_reason = deny_reason
        self.ts = ts or now_ts()
        self.epoch = epoch if epoch is not None else now_epoch()
        self.extra = dict(extra or {})

    def to_row(self):
        return {"event_id": self.event_id, "room_id": self.room_id, "person": self.person,
                "factors": json.dumps(self.factors, ensure_ascii=False),
                "persons": json.dumps(self.persons, ensure_ascii=False),
                "result": self.result, "deny_reason": self.deny_reason,
                "ts": self.ts, "epoch": self.epoch,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        e = AccessEvent(row["room_id"], row["person"],
                        factors=json.loads(row["factors"] or "[]"),
                        persons=json.loads(row["persons"] or "[]"),
                        result=row["result"], deny_reason=row["deny_reason"] or "",
                        ts=row["ts"], epoch=row["epoch"],
                        extra=json.loads(row["extra"] or "{}"))
        e.event_id = row["event_id"]
        return e


# ---------------- 保密室：入侵事件 ----------------
class IntrusionEvent(object):
    """一次入侵探测触发。kind 见 config.INTRUSION_KINDS。"""

    __slots__ = ("event_id", "room_id", "kind", "sensor_id", "zone", "ts", "epoch",
                 "armed", "extra")

    def __init__(self, room_id, kind, sensor_id="", zone="", ts=None, epoch=None,
                 armed=True, extra=None):
        self.event_id = new_id("IN")
        self.room_id = room_id
        self.kind = kind
        self.sensor_id = sensor_id
        self.zone = zone
        self.ts = ts or now_ts()
        self.epoch = epoch if epoch is not None else now_epoch()
        self.armed = armed
        self.extra = dict(extra or {})

    def to_row(self):
        return {"event_id": self.event_id, "room_id": self.room_id, "kind": self.kind,
                "sensor_id": self.sensor_id, "zone": self.zone, "ts": self.ts,
                "epoch": self.epoch, "armed": 1 if self.armed else 0,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        e = IntrusionEvent(row["room_id"], row["kind"], sensor_id=row["sensor_id"] or "",
                           zone=row["zone"] or "", ts=row["ts"], epoch=row["epoch"],
                           armed=bool(row["armed"]),
                           extra=json.loads(row["extra"] or "{}"))
        e.event_id = row["event_id"]
        return e


# ---------------- 保密室：试卷柜事件 ----------------
class SafeEvent(object):
    """试卷柜电子锁动作。柜内状态：paper_present / tamper / humidity。"""

    __slots__ = ("event_id", "room_id", "safe_id", "person", "state", "authorized",
                 "inner", "ts", "epoch", "extra")

    def __init__(self, room_id, safe_id, person="", state="closed", authorized=False,
                 inner=None, ts=None, epoch=None, extra=None):
        self.event_id = new_id("SF")
        self.room_id = room_id
        self.safe_id = safe_id
        self.person = person
        self.state = state
        self.authorized = authorized
        self.inner = dict(inner or {})         # 柜内状态监测
        self.ts = ts or now_ts()
        self.epoch = epoch if epoch is not None else now_epoch()
        self.extra = dict(extra or {})

    def to_row(self):
        return {"event_id": self.event_id, "room_id": self.room_id, "safe_id": self.safe_id,
                "person": self.person, "state": self.state,
                "authorized": 1 if self.authorized else 0,
                "inner": json.dumps(self.inner, ensure_ascii=False),
                "ts": self.ts, "epoch": self.epoch,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        e = SafeEvent(row["room_id"], row["safe_id"], person=row["person"] or "",
                      state=row["state"], authorized=bool(row["authorized"]),
                      inner=json.loads(row["inner"] or "{}"),
                      ts=row["ts"], epoch=row["epoch"],
                      extra=json.loads(row["extra"] or "{}"))
        e.event_id = row["event_id"]
        return e


# ---------------- 保密室：环境采样 ----------------
class EnvSample(object):
    """环境监测采样：temp_c / humidity_pct / water / smoke。"""

    __slots__ = ("sample_id", "room_id", "temp_c", "humidity_pct", "water", "smoke",
                 "ts", "epoch")

    def __init__(self, room_id, temp_c=25.0, humidity_pct=45.0, water=False,
                 smoke=False, ts=None, epoch=None):
        self.sample_id = new_id("EN")
        self.room_id = room_id
        self.temp_c = _f(temp_c, 25.0)
        self.humidity_pct = _f(humidity_pct, 45.0)
        self.water = bool(water)
        self.smoke = bool(smoke)
        self.ts = ts or now_ts()
        self.epoch = epoch if epoch is not None else now_epoch()

    def to_row(self):
        return {"sample_id": self.sample_id, "room_id": self.room_id,
                "temp_c": self.temp_c, "humidity_pct": self.humidity_pct,
                "water": 1 if self.water else 0, "smoke": 1 if self.smoke else 0,
                "ts": self.ts, "epoch": self.epoch}

    @staticmethod
    def from_row(row):
        s = EnvSample(row["room_id"], temp_c=row["temp_c"],
                      humidity_pct=row["humidity_pct"],
                      water=bool(row["water"]), smoke=bool(row["smoke"]),
                      ts=row["ts"], epoch=row["epoch"])
        s.sample_id = row["sample_id"]
        return s

    def violations(self):
        """逐项判断是否越限，返回 [(code, 说明), ...]。"""
        out = []
        t = config.ENV_THRESHOLDS["temp_c"]
        if self.temp_c > t["max"]:
            out.append(("ENV_TEMP_HIGH", "温度 %.1f%s 高于上限 %.1f%s"
                        % (self.temp_c, t["unit"], t["max"], t["unit"])))
        elif self.temp_c < t["min"]:
            out.append(("ENV_TEMP_LOW", "温度 %.1f%s 低于下限 %.1f%s"
                        % (self.temp_c, t["unit"], t["min"], t["unit"])))
        h = config.ENV_THRESHOLDS["humidity_pct"]
        if self.humidity_pct > h["max"]:
            out.append(("ENV_HUMID_HIGH", "湿度 %.1f%s 高于上限 %.1f%s"
                        % (self.humidity_pct, h["unit"], h["max"], h["unit"])))
        elif self.humidity_pct < h["min"]:
            out.append(("ENV_HUMID_LOW", "湿度 %.1f%s 低于下限 %.1f%s"
                        % (self.humidity_pct, h["unit"], h["min"], h["unit"])))
        if self.water:
            out.append(("ENV_WATER", "水浸探测触发"))
        if self.smoke:
            out.append(("ENV_SMOKE", "烟感探测触发"))
        return out


# ---------------- 流转：试卷袋 ----------------
class Bag(object):
    """试卷袋：RFID/二维码标识 + 电子铅封状态 + 数量。"""

    __slots__ = ("bag_id", "rfid", "bundle_id", "subject", "quantity",
                 "seal_id", "seal_state", "seal_at", "seal_by",
                 "room_id", "current_stage", "history", "extra")

    def __init__(self, bag_id, rfid="", bundle_id="", subject="", quantity=0,
                 seal_id="", seal_state="unknown", room_id=""):
        self.bag_id = bag_id
        self.rfid = rfid or bag_id
        self.bundle_id = bundle_id
        self.subject = subject
        self.quantity = int(quantity or 0)
        self.seal_id = seal_id
        self.seal_state = seal_state
        self.seal_at = ""
        self.seal_by = ""
        self.room_id = room_id
        self.current_stage = ""
        self.history = []           # [{stage, ts, person, note}]
        self.extra = {}

    def to_row(self):
        return {"bag_id": self.bag_id, "rfid": self.rfid, "bundle_id": self.bundle_id,
                "subject": self.subject, "quantity": self.quantity,
                "seal_id": self.seal_id, "seal_state": self.seal_state,
                "seal_at": self.seal_at, "seal_by": self.seal_by,
                "room_id": self.room_id, "current_stage": self.current_stage,
                "history": json.dumps(self.history, ensure_ascii=False),
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        b = Bag(row["bag_id"], rfid=row["rfid"] or "", bundle_id=row["bundle_id"] or "",
                subject=row["subject"] or "", quantity=row["quantity"] or 0,
                seal_id=row["seal_id"] or "", seal_state=row["seal_state"] or "unknown",
                room_id=row["room_id"] or "")
        b.seal_at = row["seal_at"] or ""
        b.seal_by = row["seal_by"] or ""
        b.current_stage = row["current_stage"] or ""
        b.history = json.loads(row["history"] or "[]")
        b.extra = json.loads(row["extra"] or "{}")
        return b


# ---------------- 流转：交接 ----------------
class Handover(object):
    """交接记录。双人签字（persons）+ 人脸确认（faces）必须齐备。"""

    __slots__ = ("handover_id", "bag_id", "stage", "from_person", "to_person",
                 "persons", "signatures", "faces", "method", "quantity",
                 "room_id", "ts", "epoch", "problems", "extra")

    def __init__(self, bag_id, stage, from_person="", to_person="", persons=None,
                 signatures=None, faces=None, method="scan", quantity=0,
                 room_id="", extra=None):
        self.handover_id = new_id("HO")
        self.bag_id = bag_id
        self.stage = stage
        self.from_person = from_person
        self.to_person = to_person
        self.persons = list(persons or [])            # 参与人（双人）
        self.signatures = list(signatures or [])      # 电子签字（与 persons 对应）
        self.faces = list(faces or [])                # 人脸确认（与 persons 对应）
        self.method = method                          # scan / rfid
        self.quantity = int(quantity or 0)
        self.room_id = room_id
        self.ts = now_ts()
        self.epoch = now_epoch()
        self.problems = []
        self.extra = dict(extra or {})

    def check(self):
        """完整性核验：文档要求"100% 可追溯（人、时、物、地点）"。返回问题清单。"""
        p = []
        if len(self.persons) < config.HANDOVER_PERSONS_REQUIRED:
            p.append(("HANDOVER_PERSON_MISS",
                      "交接未达双人要求（要求 %d 人，实际 %d 人）"
                      % (config.HANDOVER_PERSONS_REQUIRED, len(self.persons))))
        if len(self.faces) < len(self.persons):
            p.append(("HANDOVER_FACE_MISS",
                      "交接人脸确认不足（%d 人中仅 %d 人完成人脸确认）"
                      % (len(self.persons), len(self.faces))))
        if len(self.signatures) < len(self.persons):
            p.append(("HANDOVER_INCOMPLETE",
                      "交接签字不足（%d 人中仅 %d 人签字）"
                      % (len(self.persons), len(self.signatures))))
        if self.method not in config.HANDOVER_METHODS:
            p.append(("HANDOVER_INCOMPLETE", "交接方式非扫码/RFID（当前：%s）"
                      % (self.method or "空")))
        if not self.bag_id:
            p.append(("HANDOVER_INCOMPLETE", "交接对象（试卷袋）为空"))
        if not self.room_id:
            p.append(("HANDOVER_INCOMPLETE", "交接地点（保密室/考场）为空"))
        self.problems = p
        return p

    def complete(self):
        """"人、时、物、地点"四项齐备才算可追溯。"""
        return not self.check()

    def to_row(self):
        return {"handover_id": self.handover_id, "bag_id": self.bag_id,
                "stage": self.stage, "from_person": self.from_person,
                "to_person": self.to_person,
                "persons": json.dumps(self.persons, ensure_ascii=False),
                "signatures": json.dumps(self.signatures, ensure_ascii=False),
                "faces": json.dumps(self.faces, ensure_ascii=False),
                "method": self.method, "quantity": self.quantity,
                "room_id": self.room_id, "ts": self.ts, "epoch": self.epoch,
                "problems": json.dumps([c for c, _ in self.problems], ensure_ascii=False),
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        h = Handover(row["bag_id"], row["stage"], from_person=row["from_person"] or "",
                     to_person=row["to_person"] or "",
                     persons=json.loads(row["persons"] or "[]"),
                     signatures=json.loads(row["signatures"] or "[]"),
                     faces=json.loads(row["faces"] or "[]"),
                     method=row["method"], quantity=row["quantity"] or 0,
                     room_id=row["room_id"] or "",
                     extra=json.loads(row["extra"] or "{}"))
        h.handover_id = row["handover_id"]
        h.ts = row["ts"]
        h.epoch = row["epoch"]
        h.problems = [(c, "") for c in json.loads(row["problems"] or "[]")]
        return h


# ---------------- 流转：押运 ----------------
class EscortTask(object):
    """押运任务：车辆北斗/GPS 定位 + 车内视频回传 + 路线偏离。"""

    __slots__ = ("task_id", "plate", "driver", "escort_persons", "bag_ids",
                 "route", "video_url", "video_online", "start_ts", "start_epoch",
                 "end_ts", "status", "extra")

    def __init__(self, plate, driver="", escort_persons=None, bag_ids=None,
                 route=None, video_url=""):
        self.task_id = new_id("ET")
        self.plate = plate
        self.driver = driver
        self.escort_persons = list(escort_persons or [])
        self.bag_ids = list(bag_ids or [])
        self.route = list(route or [])      # 规划路线 [(lat, lng), ...]
        self.video_url = video_url
        self.video_online = False
        self.start_ts = now_ts()
        self.start_epoch = now_epoch()
        self.end_ts = ""
        self.status = "running"             # running / done / abnormal
        self.extra = {}

    def to_row(self):
        return {"task_id": self.task_id, "plate": self.plate, "driver": self.driver,
                "escort_persons": json.dumps(self.escort_persons, ensure_ascii=False),
                "bag_ids": json.dumps(self.bag_ids, ensure_ascii=False),
                "route": json.dumps(self.route, ensure_ascii=False),
                "video_url": self.video_url,
                "video_online": 1 if self.video_online else 0,
                "start_ts": self.start_ts, "start_epoch": self.start_epoch,
                "end_ts": self.end_ts, "status": self.status,
                "extra": json.dumps(self.extra, ensure_ascii=False)}

    @staticmethod
    def from_row(row):
        t = EscortTask(row["plate"], driver=row["driver"] or "",
                       escort_persons=json.loads(row["escort_persons"] or "[]"),
                       bag_ids=json.loads(row["bag_ids"] or "[]"),
                       route=json.loads(row["route"] or "[]"),
                       video_url=row["video_url"] or "")
        t.task_id = row["task_id"]
        t.video_online = bool(row["video_online"])
        t.start_ts = row["start_ts"] or ""
        t.start_epoch = row["start_epoch"] or 0.0
        t.end_ts = row["end_ts"] or ""
        t.status = row["status"] or "running"
        t.extra = json.loads(row["extra"] or "{}")
        return t


class EscortPoint(object):
    """押运轨迹点（北斗优先）。"""

    __slots__ = ("point_id", "task_id", "lat", "lng", "speed_kmh", "source",
                 "ts", "epoch", "offset_m", "deviated")

    def __init__(self, task_id, lat, lng, speed_kmh=0.0, source="beidou",
                 ts=None, epoch=None):
        self.point_id = new_id("EP")
        self.task_id = task_id
        self.lat = _f(lat)
        self.lng = _f(lng)
        self.speed_kmh = _f(speed_kmh)
        self.source = source
        self.ts = ts or now_ts()
        self.epoch = epoch if epoch is not None else now_epoch()
        self.offset_m = None
        self.deviated = False

    def to_row(self):
        return {"point_id": self.point_id, "task_id": self.task_id, "lat": self.lat,
                "lng": self.lng, "speed_kmh": self.speed_kmh, "source": self.source,
                "ts": self.ts, "epoch": self.epoch,
                "offset_m": self.offset_m, "deviated": 1 if self.deviated else 0}

    @staticmethod
    def from_row(row):
        p = EscortPoint(row["task_id"], row["lat"], row["lng"],
                        speed_kmh=row["speed_kmh"] or 0.0,
                        source=row["source"] or "beidou",
                        ts=row["ts"], epoch=row["epoch"])
        p.point_id = row["point_id"]
        p.offset_m = row["offset_m"]
        p.deviated = bool(row["deviated"])
        return p


# ---------------- 流转：分发 / 回收 ----------------
class Delivery(object):
    """分发至考场记录（与考场、时间、责任人一一对应）。"""

    __slots__ = ("delivery_id", "bag_id", "room_id", "quantity", "person", "ts",
                 "epoch", "note")

    def __init__(self, bag_id, room_id, quantity, person, note=""):
        self.delivery_id = new_id("DV")
        self.bag_id = bag_id
        self.room_id = room_id
        self.quantity = int(quantity or 0)
        self.person = person
        self.ts = now_ts()
        self.epoch = now_epoch()
        self.note = note

    def problems(self):
        p = []
        if not self.bag_id:
            p.append("试卷袋为空")
        if not self.room_id:
            p.append("考场为空")
        if not self.person:
            p.append("责任人为空")
        if self.quantity <= 0:
            p.append("数量必须为正")
        return p

    def to_row(self):
        return {"delivery_id": self.delivery_id, "bag_id": self.bag_id,
                "room_id": self.room_id, "quantity": self.quantity,
                "person": self.person, "ts": self.ts, "epoch": self.epoch,
                "note": self.note}

    @staticmethod
    def from_row(row):
        d = Delivery(row["bag_id"], row["room_id"], row["quantity"] or 0,
                     row["person"] or "", note=row["note"] or "")
        d.delivery_id = row["delivery_id"]
        d.ts = row["ts"]
        d.epoch = row["epoch"]
        return d


class Recycle(object):
    """回收与封存记录。`

    issued_qty 为该袋发出数量，returned_qty 为回收实测数量，差异即时上报。
    """

    __slots__ = ("recycle_id", "bag_id", "room_id", "issued_qty", "returned_qty",
                 "diff", "person", "sealed", "seal_id", "ts", "epoch", "note")

    def __init__(self, bag_id, room_id, issued_qty, returned_qty, person="",
                 sealed=False, seal_id="", note=""):
        self.recycle_id = new_id("RC")
        self.bag_id = bag_id
        self.room_id = room_id
        self.issued_qty = int(issued_qty or 0)
        self.returned_qty = int(returned_qty or 0)
        self.diff = self.returned_qty - self.issued_qty
        self.person = person
        self.sealed = sealed
        self.seal_id = seal_id
        self.ts = now_ts()
        self.epoch = now_epoch()
        self.note = note

    def problems(self):
        p = []
        if self.diff != 0:
            p.append(("RECYCLE_COUNT_MISMATCH",
                      "回收核验数量差异 %+d（发出 %d，回收 %d）"
                      % (self.diff, self.issued_qty, self.returned_qty)))
        if not self.sealed:
            p.append(("RECYCLE_MISSING", "回收后未完成封存"))
        return p

    def to_row(self):
        return {"recycle_id": self.recycle_id, "bag_id": self.bag_id,
                "room_id": self.room_id, "issued_qty": self.issued_qty,
                "returned_qty": self.returned_qty, "diff": self.diff,
                "person": self.person, "sealed": 1 if self.sealed else 0,
                "seal_id": self.seal_id, "ts": self.ts, "epoch": self.epoch,
                "note": self.note}

    @staticmethod
    def from_row(row):
        r = Recycle(row["bag_id"], row["room_id"], row["issued_qty"] or 0,
                    row["returned_qty"] or 0, person=row["person"] or "",
                    sealed=bool(row["sealed"]), seal_id=row["seal_id"] or "",
                    note=row["note"] or "")
        r.recycle_id = row["recycle_id"]
        r.diff = row["diff"] or 0
        r.ts = row["ts"]
        r.epoch = row["epoch"]
        return r


# ---------------- 录像段 ----------------
class RecordSegment(object):
    """一段录像。连续性由相邻段（start + duration == 下一段 start）判定。"""

    __slots__ = ("seg_id", "cam_id", "channel", "start_epoch", "duration_sec",
                 "playable", "locked", "start_ts", "retain_until")

    def __init__(self, cam_id, start_epoch, duration_sec, channel="main",
                 playable=True, locked=None, start_ts=""):
        self.seg_id = new_id("RS")
        self.cam_id = cam_id
        self.channel = channel
        self.start_epoch = _f(start_epoch)
        self.duration_sec = _f(duration_sec)
        self.playable = bool(playable)
        self.locked = config.RECORD_WORM if locked is None else bool(locked)
        self.start_ts = start_ts or time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.start_epoch))
        self.retain_until = self.start_epoch + config.RECORD_RETAIN_DAYS * 86400

    def end_epoch(self):
        return self.start_epoch + self.duration_sec

    def to_row(self):
        return {"seg_id": self.seg_id, "cam_id": self.cam_id, "channel": self.channel,
                "start_epoch": self.start_epoch, "duration_sec": self.duration_sec,
                "playable": 1 if self.playable else 0,
                "locked": 1 if self.locked else 0,
                "start_ts": self.start_ts, "retain_until": self.retain_until}

    @staticmethod
    def from_row(row):
        s = RecordSegment(row["cam_id"], row["start_epoch"],
                          row["duration_sec"], channel=row["channel"] or "main",
                          playable=bool(row["playable"]),
                          locked=bool(row["locked"]),
                          start_ts=row["start_ts"] or "")
        s.seg_id = row["seg_id"]
        s.retain_until = row["retain_until"] or 0.0
        return s
