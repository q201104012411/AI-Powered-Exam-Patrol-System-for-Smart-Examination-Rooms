# -*- coding: utf-8 -*-
"""
10 普通考场+特殊考场应用 · 台账（SQLite）

一张库 10 张表，覆盖"考场-模板-自检-告警-分区-指标"闭环：

  rooms        考场（含套用的模板、实际点位、能力缺口、降级档位）
  checks       准入自检记录（含逐机位画质结论与整合可信度系数）
  sessions     考试场次（考前/考试中/已结束）
  alerts       分级告警路由与处置（抑制、分流、推送对象、SLA）
  behaviors    行为清单（44 种普通 + 机考/口试附加；含考场适用性）
  kpi_runs     验收指标测算（只认实测证据，来源如实标注）
  partitions   数据分域（隔离/机考/涉密强制独立分区）
  grants       分区独立授权
  privacy      合规留痕（DPIA/演练/授权/销毁/封存）
  audits       哈希链审计

设计原则：
  1) 字段白名单：dataclass 加字段不会写坏 SQL。
  2) 敏感字段（考生编号/座位）落库前掩码，明细只留分区文件。
  3) 审计是哈希链（prev_hash -> this_hash），改一条即断链，可自证完整。
"""
import json
import os
import sqlite3
import threading
import time

import config
import models

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms(
  room_id TEXT PRIMARY KEY, name TEXT, room_type TEXT, site TEXT, building TEXT,
  floor TEXT, seats INTEGER, status TEXT, session_name TEXT, session_state TEXT,
  apply_by TEXT, apply_reason TEXT, applied_at TEXT,
  template_applied INTEGER, template_at TEXT, min_config TEXT, degrade_used INTEGER,
  points TEXT, points_missing TEXT, caps_present TEXT, caps_missing TEXT,
  caps_weak TEXT, enabled_behaviors TEXT, alert_policy TEXT, invigilators TEXT,
  invigilators_required INTEGER, remote_invigilator TEXT, ledger_ready INTEGER,
  ledger_sample TEXT, points_verified INTEGER, calibration_at TEXT,
  clock_drift_sec REAL, data_domain TEXT, offline_ready INTEGER, fast_enable INTEGER,
  doctor_link INTEGER, network_log INTEGER, independent_mic INTEGER,
  dpia_done INTEGER,
  drill_done INTEGER, dedup_verified INTEGER, screen_audit INTEGER,
  audio_sample_rate INTEGER, alt_channel INTEGER, last_check_at TEXT,
  last_check_ok INTEGER, armed_at TEXT, closed_at TEXT, retention_days INTEGER,
  note TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_rooms_type ON rooms(room_type, status);

CREATE TABLE IF NOT EXISTS checks(
  check_id TEXT PRIMARY KEY, room_id TEXT, room_name TEXT, room_type TEXT,
  phase TEXT, operator TEXT, ok INTEGER, blocked INTEGER, defects TEXT,
  point_checks TEXT, factor REAL, passed TEXT, min_config TEXT, note TEXT,
  ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_checks_room ON checks(room_id, epoch);

CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, room_id TEXT, room_name TEXT, room_type TEXT,
  name TEXT, subject TEXT, starts_at TEXT, ends_at TEXT, state TEXT,
  planned_seats INTEGER, actual_seats INTEGER, operator TEXT, note TEXT,
  created_at TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_sessions_room ON sessions(room_id, epoch);

CREATE TABLE IF NOT EXISTS alerts(
  event_id TEXT PRIMARY KEY, room_id TEXT, room_name TEXT, room_type TEXT,
  session_id TEXT, behavior TEXT, behavior_name TEXT, category TEXT,
  alert_code TEXT, level TEXT, confidence REAL, device_code TEXT, zone TEXT,
  seat_no TEXT, candidate_id TEXT, detail TEXT, delivered INTEGER,
  suppressed INTEGER, suppress_reason TEXT, pushed_to TEXT, response_text TEXT,
  sla_sec INTEGER, source TEXT, ts TEXT, epoch REAL, external TEXT);
CREATE INDEX IF NOT EXISTS ix_alerts_room ON alerts(room_id, epoch);
CREATE INDEX IF NOT EXISTS ix_alerts_sup ON alerts(suppressed, epoch);

CREATE TABLE IF NOT EXISTS behaviors(
  code TEXT PRIMARY KEY, name TEXT, category TEXT, level TEXT, signals TEXT,
  enabled INTEGER, rooms_off TEXT);

CREATE TABLE IF NOT EXISTS kpi_runs(
  run_id TEXT PRIMARY KEY, room_id TEXT, room_name TEXT, room_type TEXT,
  session_id TEXT, operator TEXT, data_source TEXT, observe_total INTEGER,
  observe_hit INTEGER, observe_correct INTEGER, observe_false INTEGER,
  response_sec REAL, camera_online_rate REAL, coverage_rate REAL,
  dedup_expected INTEGER, dedup_counted INTEGER, clock_drift_sec REAL,
  invigilator_onpost INTEGER, invigilator_total INTEGER, alt_channel INTEGER,
  alt_channel_drill INTEGER, domain_isolation INTEGER, offline_ready INTEGER,
  min_config INTEGER, metrics TEXT, notes TEXT, ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_kpi_room ON kpi_runs(room_id, epoch);

CREATE TABLE IF NOT EXISTS partitions(
  partition_id TEXT PRIMARY KEY, domain TEXT, room_id TEXT, room_name TEXT,
  state TEXT, isolated INTEGER, encrypted INTEGER, path TEXT, access_roles TEXT,
  retention_days INTEGER, destroy_at TEXT, destroyed_at TEXT,
  destroy_operator TEXT, created_at TEXT, note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_parts ON partitions(domain, room_id);

CREATE TABLE IF NOT EXISTS grants(
  grant_id TEXT PRIMARY KEY, domain TEXT, role TEXT, operator TEXT,
  granted_by TEXT, revoked INTEGER, reason TEXT, ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_grants_domain ON grants(domain, role);

CREATE TABLE IF NOT EXISTS privacy(
  record_id TEXT PRIMARY KEY, action TEXT, room_id TEXT, room_name TEXT,
  domain TEXT, operator TEXT, witness TEXT, result TEXT, detail TEXT,
  ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_privacy_room ON privacy(room_id, epoch);

CREATE TABLE IF NOT EXISTS audits(
  audit_id TEXT PRIMARY KEY, ts TEXT, epoch REAL, action TEXT, detail TEXT,
  operator TEXT, room_id TEXT, prev_hash TEXT, this_hash TEXT);
CREATE INDEX IF NOT EXISTS ix_audits_epoch ON audits(epoch);
"""

FIELDS = {
    "rooms": ("room_id", "name", "room_type", "site", "building", "floor", "seats",
              "status", "session_name", "session_state", "apply_by", "apply_reason",
              "applied_at", "template_applied", "template_at", "min_config",
              "degrade_used", "points", "points_missing", "caps_present",
              "caps_missing", "caps_weak", "enabled_behaviors", "alert_policy",
              "invigilators", "invigilators_required", "remote_invigilator",
              "ledger_ready", "ledger_sample", "points_verified", "calibration_at",
              "clock_drift_sec", "data_domain", "offline_ready", "fast_enable",
              "doctor_link", "network_log", "independent_mic",
              "dpia_done", "drill_done", "dedup_verified", "screen_audit",
              "audio_sample_rate", "alt_channel", "last_check_at", "last_check_ok",
              "armed_at", "closed_at", "retention_days", "note", "created_at",
              "updated_at"),
    "checks": ("check_id", "room_id", "room_name", "room_type", "phase", "operator",
               "ok", "blocked", "defects", "point_checks", "factor", "passed",
               "min_config", "note", "ts", "epoch"),
    "sessions": ("session_id", "room_id", "room_name", "room_type", "name", "subject",
                 "starts_at", "ends_at", "state", "planned_seats", "actual_seats",
                 "operator", "note", "created_at", "epoch"),
    "alerts": ("event_id", "room_id", "room_name", "room_type", "session_id",
               "behavior", "behavior_name", "category", "alert_code", "level",
               "confidence", "device_code", "zone", "seat_no", "candidate_id",
               "detail", "delivered", "suppressed", "suppress_reason", "pushed_to",
               "response_text", "sla_sec", "source", "ts", "epoch", "external"),
    "behaviors": ("code", "name", "category", "level", "signals", "enabled",
                  "rooms_off"),
    "kpi_runs": ("run_id", "room_id", "room_name", "room_type", "session_id",
                 "operator", "data_source", "observe_total", "observe_hit",
                 "observe_correct", "observe_false", "response_sec",
                 "camera_online_rate", "coverage_rate", "dedup_expected",
                 "dedup_counted", "clock_drift_sec", "invigilator_onpost",
                 "invigilator_total", "alt_channel", "alt_channel_drill",
                 "domain_isolation", "offline_ready", "min_config", "metrics",
                 "notes", "ts", "epoch"),
    "partitions": ("partition_id", "domain", "room_id", "room_name", "state",
                   "isolated", "encrypted", "path", "access_roles",
                   "retention_days", "destroy_at", "destroyed_at",
                   "destroy_operator", "created_at", "note"),
    "grants": ("grant_id", "domain", "role", "operator", "granted_by", "revoked",
               "reason", "ts", "epoch"),
    "privacy": ("record_id", "action", "room_id", "room_name", "domain", "operator",
                "witness", "result", "detail", "ts", "epoch"),
    "audits": ("audit_id", "ts", "epoch", "action", "detail", "operator", "room_id",
               "prev_hash", "this_hash"),
}

# 序列化时转 JSON 的字段（其余按标量写）
JSON_FIELDS = frozenset({
    "points", "points_missing", "caps_present", "caps_missing", "caps_weak",
    "enabled_behaviors", "alert_policy", "invigilators", "remote_invigilator",
    "ledger_sample", "defects", "point_checks",
    "passed", "pushed_to", "external", "signals", "rooms_off", "access_roles",
    "metrics", "notes",
})

AUDIT_TABLES = ("rooms", "checks", "sessions", "alerts", "behaviors", "kpi_runs",
                "partitions", "grants", "privacy", "audits")


class Store(object):
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init()
        self.ensure_behaviors()

    # ---------------- 连接 ----------------
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._lock:
            c = self._conn()
            try:
                c.executescript(SCHEMA)
                c.commit()
            finally:
                c.close()

    def close(self):
        pass

    # ---------------- 通用写入 ----------------
    @staticmethod
    def _dump(table, obj, cols):
        d = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        row = {}
        for k in cols:
            v = d.get(k)
            if k in JSON_FIELDS:
                row[k] = models.jdump(v if v is not None else ({} if k in
                                                               ("alert_policy",
                                                                "external",
                                                                "metrics") else []))
            elif isinstance(v, bool):
                row[k] = 1 if v else 0
            elif k == "candidate_id" and table == "alerts" and v:
                # 告警台账不落明文考生编号（明细留分区文件）
                row[k] = config.mask_id(v)
            else:
                row[k] = v
        return row

    def _upsert(self, table, obj, key=None):
        cols = FIELDS[table]
        row = self._dump(table, obj, cols)
        key = key or cols[0]
        names = list(row.keys())
        placeholders = ",".join("?" for _ in names)
        updates = ",".join("%s=excluded.%s" % (n, n) for n in names if n != key)
        sql = ("INSERT INTO %s(%s) VALUES(%s) ON CONFLICT(%s) DO UPDATE SET %s"
               % (table, ",".join(names), placeholders, key, updates))
        with self._lock:
            c = self._conn()
            try:
                c.execute(sql, [row[n] for n in names])
                c.commit()
            finally:
                c.close()
        return row

    def _query(self, sql, args=()):
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute(sql, args)
                return [dict(r) for r in cur.fetchall()]
            finally:
                c.close()

    def _one(self, sql, args=()):
        rows = self._query(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql, args=()):
        with self._lock:
            c = self._conn()
            try:
                c.execute(sql, args)
                c.commit()
            finally:
                c.close()

    # =====================================================================
    # 考场
    # =====================================================================
    def save_room(self, room):
        if not room.room_id:
            room.room_id = models.new_id("RM")
        if not room.created_at:
            room.created_at = models.now_text()
        room.updated_at = models.now_text()
        self._upsert("rooms", room, "room_id")
        return room

    def get_room(self, room_id):
        row = self._one("SELECT * FROM rooms WHERE room_id=?", (room_id,))
        return models.Room.from_row(row) if row else None

    def get_room_by_name(self, name):
        row = self._one("SELECT * FROM rooms WHERE name=?", (name,))
        return models.Room.from_row(row) if row else None

    def list_rooms(self, room_type=None, status=None, special=None, site=None,
                   limit=500):
        sql = "SELECT * FROM rooms WHERE 1=1"
        args = []
        if room_type:
            sql += " AND room_type=?"
            args.append(room_type)
        if status:
            sql += " AND status=?"
            args.append(status)
        if special is not None:
            types = config.SPECIAL_TYPES if special else (config.TYPE_STANDARD,)
            sql += " AND room_type IN (%s)" % ",".join("?" for _ in types)
            args.extend(types)
        if site:
            sql += " AND site=?"
            args.append(site)
        sql += " ORDER BY room_type, name LIMIT ?"
        args.append(int(limit))
        return [models.Room.from_row(r) for r in self._query(sql, args)]

    def delete_room(self, room_id):
        self._exec("DELETE FROM rooms WHERE room_id=?", (room_id,))

    def room_type_stats(self):
        rows = self._query("SELECT room_type, COUNT(*) n,"
                           " SUM(CASE WHEN status IN ('armed','degraded') THEN 1 ELSE 0 END) arm"
                           " FROM rooms GROUP BY room_type")
        out = {}
        for r in rows:
            out[r["room_type"]] = {"total": int(r["n"] or 0),
                                   "armed": int(r["arm"] or 0)}
        return out

    def armed_rooms(self):
        return [models.Room.from_row(r) for r in self._query(
            "SELECT * FROM rooms WHERE status IN ('armed','degraded')"
            " ORDER BY name")]

    # =====================================================================
    # 准入自检
    # =====================================================================
    def save_check(self, run):
        if not run.check_id:
            run.check_id = models.new_id("CK")
        if not run.ts:
            run.ts = models.now_text()
        if not run.epoch:
            run.epoch = models.now_epoch()
        run.defects = [d.to_dict() if hasattr(d, "to_dict") else d
                       for d in (run.defects or [])]
        run.point_checks = [p.to_dict() if hasattr(p, "to_dict") else p
                            for p in (run.point_checks or [])]
        self._upsert("checks", run, "check_id")
        return run

    def latest_check(self, room_id):
        row = self._one("SELECT * FROM checks WHERE room_id=?"
                        " ORDER BY epoch DESC, rowid DESC LIMIT 1", (room_id,))
        return models.CheckRun.from_row(row) if row else None

    def list_checks(self, room_id=None, limit=50):
        if room_id:
            rows = self._query("SELECT * FROM checks WHERE room_id=?"
                               " ORDER BY epoch DESC LIMIT ?", (room_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM checks ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.CheckRun.from_row(r) for r in rows]

    # =====================================================================
    # 场次
    # =====================================================================
    def save_session(self, s):
        if not s.session_id:
            s.session_id = models.new_id("SE")
        if not s.created_at:
            s.created_at = models.now_text()
        if not s.epoch:
            s.epoch = models.now_epoch()
        self._upsert("sessions", s, "session_id")
        return s

    def get_session(self, session_id):
        row = self._one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return models.Session.from_row(row) if row else None

    def latest_session(self, room_id):
        row = self._one("SELECT * FROM sessions WHERE room_id=?"
                        " ORDER BY epoch DESC, rowid DESC LIMIT 1", (room_id,))
        return models.Session.from_row(row) if row else None

    def list_sessions(self, room_id=None, state=None, limit=100):
        sql = "SELECT * FROM sessions WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if state:
            sql += " AND state=?"
            args.append(state)
        sql += " ORDER BY epoch DESC LIMIT ?"
        args.append(int(limit))
        return [models.Session.from_row(r) for r in self._query(sql, args)]

    # =====================================================================
    # 告警
    # =====================================================================
    def save_alert(self, ev):
        if not ev.event_id:
            ev.event_id = models.new_id("AL")
        if not ev.ts:
            ev.ts = models.now_text()
        if not ev.epoch:
            ev.epoch = models.now_epoch()
        self._upsert("alerts", ev, "event_id")
        return ev

    def get_alert(self, event_id):
        row = self._one("SELECT * FROM alerts WHERE event_id=?", (event_id,))
        return models.AlertEvent.from_row(row) if row else None

    def list_alerts(self, room_id=None, level=None, suppressed=None, limit=100):
        sql = "SELECT * FROM alerts WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if level:
            sql += " AND level=?"
            args.append(level)
        if suppressed is not None:
            sql += " AND suppressed=?"
            args.append(1 if suppressed else 0)
        sql += " ORDER BY epoch DESC LIMIT ?"
        args.append(int(limit))
        return [models.AlertEvent.from_row(r) for r in self._query(sql, args)]

    def recent_same_behavior(self, room_id, behavior, within_sec):
        since = models.now_epoch() - float(within_sec)
        rows = self._query("SELECT * FROM alerts WHERE room_id=? AND behavior=?"
                           " AND suppressed=0 AND epoch>=? ORDER BY epoch DESC",
                           (room_id, behavior, since))
        return [models.AlertEvent.from_row(r) for r in rows]

    def alert_stats(self, room_id=None):
        sql = ("SELECT COUNT(*) total,"
               " SUM(CASE WHEN level='red' THEN 1 ELSE 0 END) red,"
               " SUM(CASE WHEN level='orange' THEN 1 ELSE 0 END) orange,"
               " SUM(CASE WHEN suppressed=1 THEN 1 ELSE 0 END) suppressed,"
               " SUM(CASE WHEN delivered=1 THEN 1 ELSE 0 END) delivered"
               " FROM alerts WHERE 1=1")
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        r = self._one(sql, args) or {}
        return {k: int(r.get(k) or 0) for k in
                ("total", "red", "orange", "suppressed", "delivered")}

    def behavior_stats(self, limit=20):
        rows = self._query(
            "SELECT behavior, behavior_name, level, COUNT(*) n FROM alerts"
            " GROUP BY behavior, behavior_name, level ORDER BY n DESC LIMIT ?",
            (int(limit),))
        return [{"behavior": r["behavior"], "name": r["behavior_name"],
                 "level": r["level"], "count": int(r["n"] or 0)} for r in rows]

    # =====================================================================
    # 行为清单
    # =====================================================================
    def save_behavior(self, b):
        self._upsert("behaviors", b, "code")
        return b

    def ensure_behaviors(self):
        """把 config 里的行为清单灌进库（幂等，不覆盖人工开关）。"""
        existing = {r["code"] for r in self._query("SELECT code FROM behaviors")}
        added = 0
        for t in list(config.BEHAVIORS) + list(config.EXTRA_BEHAVIORS):
            if t[0] in existing:
                continue
            self.save_behavior(models.BehaviorDef.from_tuple(t))
            added += 1
        return added

    def get_behavior(self, code):
        row = self._one("SELECT * FROM behaviors WHERE code=?", (code,))
        return models.BehaviorDef.from_row(row) if row else None

    def list_behaviors(self, category=None, enabled=None, room_type=None):
        sql = "SELECT * FROM behaviors WHERE 1=1"
        args = []
        if category:
            sql += " AND category=?"
            args.append(category)
        if enabled is not None:
            sql += " AND enabled=?"
            args.append(1 if enabled else 0)
        sql += " ORDER BY category, code"
        out = [models.BehaviorDef.from_row(r) for r in self._query(sql, args)]
        if room_type:
            out = [b for b in out if room_type not in b.rooms_off]
        return out

    def set_behavior(self, code, enabled, rooms_off=None):
        b = self.get_behavior(code)
        if b is None:
            return None
        b.enabled = 1 if enabled else 0
        if rooms_off is not None:
            b.rooms_off = list(rooms_off)
        self.save_behavior(b)
        return b

    # =====================================================================
    # KPI
    # =====================================================================
    def save_kpi(self, run):
        if not run.run_id:
            run.run_id = models.new_id("KP")
        if not run.ts:
            run.ts = models.now_text()
        if not run.epoch:
            run.epoch = models.now_epoch()
        self._upsert("kpi_runs", run, "run_id")
        return run

    def list_kpi(self, room_id=None, data_source=None, limit=50):
        sql = "SELECT * FROM kpi_runs WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if data_source:
            sql += " AND data_source=?"
            args.append(data_source)
        sql += " ORDER BY epoch DESC LIMIT ?"
        args.append(int(limit))
        return [models.KpiRun.from_row(r) for r in self._query(sql, args)]

    def latest_kpi(self, room_id, data_source=None):
        sql = "SELECT * FROM kpi_runs WHERE room_id=?"
        args = [room_id]
        if data_source:
            sql += " AND data_source=?"
            args.append(data_source)
        sql += " ORDER BY epoch DESC, rowid DESC LIMIT 1"
        row = self._one(sql, args)
        return models.KpiRun.from_row(row) if row else None

    # =====================================================================
    # 数据分域
    # =====================================================================
    def save_partition(self, p):
        if not p.partition_id:
            p.partition_id = models.new_id("PT")
        if not p.created_at:
            p.created_at = models.now_text()
        self._upsert("partitions", p, "partition_id")
        return p

    def get_partition(self, domain, room_id):
        row = self._one("SELECT * FROM partitions WHERE domain=? AND room_id=?",
                        (domain, room_id))
        return models.Partition.from_row(row) if row else None

    def list_partitions(self, domain=None, room_id=None):
        sql = "SELECT * FROM partitions WHERE 1=1"
        args = []
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        sql += " ORDER BY domain, room_name"
        return [models.Partition.from_row(r) for r in self._query(sql, args)]

    def partition_stats(self):
        rows = self._query("SELECT domain, COUNT(*) n,"
                           " SUM(CASE WHEN state='active' THEN 1 ELSE 0 END) act,"
                           " SUM(CASE WHEN isolated=1 THEN 1 ELSE 0 END) iso"
                           " FROM partitions GROUP BY domain")
        return {r["domain"]: {"total": int(r["n"] or 0), "active": int(r["act"] or 0),
                              "isolated": int(r["iso"] or 0)} for r in rows}

    # =====================================================================
    # 授权
    # =====================================================================
    def save_grant(self, g):
        if not g.grant_id:
            g.grant_id = models.new_id("GR")
        if not g.ts:
            g.ts = models.now_text()
        if not g.epoch:
            g.epoch = models.now_epoch()
        self._upsert("grants", g, "grant_id")
        return g

    def list_grants(self, domain=None, active_only=True):
        sql = "SELECT * FROM grants WHERE 1=1"
        args = []
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        if active_only:
            sql += " AND revoked=0"
        sql += " ORDER BY ts DESC"
        return [models.Grant.from_row(r) for r in self._query(sql, args)]

    def revoke_grant(self, grant_id):
        self._exec("UPDATE grants SET revoked=1 WHERE grant_id=?", (grant_id,))
        return True

    def role_authorized(self, domain, role):
        """该角色对该域是否已被独立授权（默认拒绝）。"""
        if not domain or not role:
            return False
        row = self._one("SELECT COUNT(*) n FROM grants WHERE domain=? AND role=?"
                        " AND revoked=0", (domain, role))
        return bool(row and int(row["n"]) > 0)

    # =====================================================================
    # 合规留痕
    # =====================================================================
    def save_privacy(self, rec):
        if not rec.record_id:
            rec.record_id = models.new_id("PV")
        if not rec.ts:
            rec.ts = models.now_text()
        if not rec.epoch:
            rec.epoch = models.now_epoch()
        self._upsert("privacy", rec, "record_id")
        return rec

    def list_privacy(self, room_id=None, action=None, limit=100):
        sql = "SELECT * FROM privacy WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if action:
            sql += " AND action=?"
            args.append(action)
        sql += " ORDER BY epoch DESC LIMIT ?"
        args.append(int(limit))
        return [models.PrivacyRecord.from_row(r) for r in self._query(sql, args)]

    def has_privacy(self, room_id, action):
        row = self._one("SELECT COUNT(*) n FROM privacy WHERE room_id=? AND action=?",
                        (room_id, action))
        return bool(row and int(row["n"]) > 0)

    def privacy_stats(self):
        rows = self._query("SELECT action, COUNT(*) n FROM privacy GROUP BY action")
        return {r["action"]: int(r["n"] or 0) for r in rows}

    # =====================================================================
    # 审计（哈希链）
    # =====================================================================
    def log(self, action, detail="", operator="", room_id=""):
        last = self._one("SELECT this_hash FROM audits ORDER BY rowid DESC LIMIT 1")
        prev = (last or {}).get("this_hash") or ""
        rec = models.AuditLog(audit_id=models.new_id("AD"), ts=models.now_text(),
                              epoch=models.now_epoch(), action=action,
                              detail=str(detail)[:1000], operator=operator,
                              room_id=room_id, prev_hash=prev)
        rec.this_hash = rec.compute_hash()
        self._upsert("audits", rec, "audit_id")
        return rec

    def list_audits(self, limit=100):
        rows = self._query("SELECT * FROM audits ORDER BY rowid DESC LIMIT ?",
                           (int(limit),))
        return [models.AuditLog.from_row(r) for r in rows]

    def verify_chain(self):
        """校验哈希链；返回 (ok, 条数, 说明)。"""
        rows = self._query("SELECT * FROM audits ORDER BY rowid ASC")
        prev = ""
        for i, r in enumerate(rows, 1):
            rec = models.AuditLog.from_row(r)
            if rec.prev_hash != prev:
                return False, i, "第 %d 条 prev_hash 与上一条 this_hash 不一致" % i
            if rec.compute_hash() != rec.this_hash:
                return False, i, "第 %d 条内容被改动（哈希不符）" % i
            prev = rec.this_hash
        return True, len(rows), "哈希链完整（%d 条）" % len(rows)

    def purge_runtime(self, keep_days=0):
        """清理运行期数据，**不删审计、不删分区配置**。"""
        cutoff = models.now_epoch() - float(keep_days) * 86400.0
        n = 0
        for table in ("alerts", "kpi_runs", "checks", "privacy"):
            rows = self._query("SELECT COUNT(*) c FROM %s WHERE epoch < ?" % table,
                               (cutoff,))
            n += int((rows[0] or {}).get("c") or 0)
            self._exec("DELETE FROM %s WHERE epoch < ?" % table, (cutoff,))
        return n

    # =====================================================================
    # 概览
    # =====================================================================
    def summary(self):
        rooms = self._one("SELECT COUNT(*) c,"
                          " SUM(CASE WHEN room_type='%s' THEN 1 ELSE 0 END) std"
                          " FROM rooms" % config.TYPE_STANDARD) or {}
        special = self._one(
            "SELECT COUNT(*) c FROM rooms WHERE room_type IN (%s)"
            % ",".join("?" for _ in config.SPECIAL_TYPES), config.SPECIAL_TYPES) or {}
        armed = self._one("SELECT COUNT(*) c FROM rooms WHERE status IN"
                          " ('armed','degraded')") or {}
        kpi = self._one("SELECT COUNT(*) c FROM kpi_runs") or {}
        return {
            "rooms": int(rooms.get("c") or 0),
            "standard_rooms": int(rooms.get("std") or 0),
            "special_rooms": int(special.get("c") or 0),
            "armed": int(armed.get("c") or 0),
            "alerts": self.alert_stats(),
            "kpi_runs": int(kpi.get("c") or 0),
            "partitions": self.partition_stats(),
            "checks": int((self._one("SELECT COUNT(*) c FROM checks") or {}).get("c") or 0),
            "sessions": int((self._one("SELECT COUNT(*) c FROM sessions") or {}).get("c") or 0),
        }

    def export_json(self, path):
        t0 = time.time()
        data = {"exported_at": models.now_text(), "tables": {}}
        for t in FIELDS:
            data["tables"][t] = self._query("SELECT * FROM %s" % t)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"path": path, "ms": int((time.time() - t0) * 1000),
                "tables": len(data["tables"]),
                "rows": sum(len(v) for v in data["tables"].values())}
