# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 台账（SQLite）

一张库 13 张表，覆盖"人-物-时"闭环：
  devices       安检设备（安检门/手持探测器/储物柜/鞋底仪）
  passes        过检记录（每次通过安检门都是留痕）
  alarms        报警（由过检记录派生，进复检队列）
  reinspects    复检与处置结论
  lockers/cells 智能储物柜与柜格
  locker_logs   存取留痕（手机丢失纠纷靠它追溯）
  specials      特殊考生人工安检通道记录（双人见证）
  flows         安检-核验-签到三点位串联记录
  samples       样本库（含无害物与植入医疗器械，用于算误报率）
  test_runs/items 验收测试运行与逐样本结论
  audits        哈希链审计

设计原则：
  1) **绝不因统计口径写坏数据**：所有写入先过一遍字段白名单。
  2) **统计只认结论**：误报率来自复检结论，检出率来自样本测试（有 ground truth），
     不拿"报警数/过检数"冒充检出率。
  3) 审计是哈希链（prev_hash -> this_hash），改一条就会断链，可自证完整。
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

import config
import data_crypto
import models

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  device_id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, kind TEXT, site TEXT,
  channel_no TEXT, room_id TEXT, proto TEXT, address TEXT, sensitivity REAL,
  enabled INTEGER, status TEXT, last_heartbeat TEXT, note TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS passes(
  event_id TEXT PRIMARY KEY, device_code TEXT, channel_no TEXT, room_id TEXT,
  candidate_id TEXT, candidate_name TEXT, candidate_key TEXT, seat_no TEXT,
  ts TEXT, epoch REAL,
  result TEXT, kinds TEXT, levels TEXT, scores TEXT, duration_ms INTEGER,
  operator TEXT, source TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_passes_epoch ON passes(epoch);
CREATE INDEX IF NOT EXISTS ix_passes_dev ON passes(device_code, epoch);
CREATE INDEX IF NOT EXISTS ix_passes_ckey ON passes(candidate_key);

CREATE TABLE IF NOT EXISTS alarms(
  alarm_id TEXT PRIMARY KEY, event_id TEXT, device_code TEXT, channel_no TEXT,
  room_id TEXT, kind TEXT, kinds TEXT, level TEXT, detail TEXT,
  candidate_id TEXT, candidate_name TEXT, candidate_key TEXT, status TEXT,
  created_at TEXT, created_epoch REAL, closed_at TEXT, reported INTEGER,
  reported_at TEXT, report_reason TEXT);
CREATE INDEX IF NOT EXISTS ix_alarms_st ON alarms(status, created_epoch);

CREATE TABLE IF NOT EXISTS reinspects(
  reinspect_id TEXT PRIMARY KEY, alarm_id TEXT, event_id TEXT, operator TEXT,
  witness TEXT, method TEXT, handheld_code TEXT, result TEXT, found_kind TEXT,
  found_detail TEXT, storage_cell TEXT, started_epoch REAL, finished_epoch REAL,
  started_at TEXT, finished_at TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_rein_alarm ON reinspects(alarm_id);

CREATE TABLE IF NOT EXISTS lockers(
  locker_id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, site TEXT,
  cells_total INTEGER, address TEXT, proto TEXT, enabled INTEGER, status TEXT,
  note TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS cells(
  cell_id TEXT PRIMARY KEY, locker_id TEXT, locker_code TEXT, cell_no TEXT,
  state TEXT, candidate_id TEXT, candidate_name TEXT, candidate_key TEXT,
  ticket_no TEXT, seat_no TEXT, device_desc TEXT, event_id TEXT, stored_at TEXT,
  stored_epoch REAL, taken_at TEXT, taken_epoch REAL, method TEXT,
  overdue_notified INTEGER, note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cells ON cells(locker_id, cell_no);
CREATE INDEX IF NOT EXISTS ix_cells_state ON cells(state);
CREATE INDEX IF NOT EXISTS ix_cells_ckey ON cells(candidate_key);

CREATE TABLE IF NOT EXISTS locker_logs(
  log_id TEXT PRIMARY KEY, locker_code TEXT, cell_no TEXT, action TEXT,
  method TEXT, operator TEXT, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, device_desc TEXT, ts TEXT, epoch REAL, note TEXT);
CREATE INDEX IF NOT EXISTS ix_lockerlog_cand ON locker_logs(candidate_key, epoch);

CREATE TABLE IF NOT EXISTS specials(
  record_id TEXT PRIMARY KEY, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, ticket_no TEXT, room_id TEXT, seat_no TEXT, reason TEXT,
  inspector TEXT, witness TEXT, method TEXT, result TEXT, detail TEXT,
  decision TEXT, ts TEXT, epoch REAL, drill INTEGER);
CREATE INDEX IF NOT EXISTS ix_special_ep ON specials(epoch);

CREATE TABLE IF NOT EXISTS flows(
  flow_id TEXT PRIMARY KEY, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, ticket_no TEXT, room_id TEXT, seat_no TEXT,
  security_at TEXT, security_epoch REAL, verify_at TEXT, verify_epoch REAL,
  signin_at TEXT, signin_epoch REAL, security_event TEXT, reentries INTEGER,
  note TEXT);
CREATE INDEX IF NOT EXISTS ix_flows_cand ON flows(candidate_key);

CREATE TABLE IF NOT EXISTS samples(
  sample_id TEXT PRIMARY KEY, name TEXT, kind TEXT, expect_alarm INTEGER,
  scores TEXT, note TEXT, builtin INTEGER);

CREATE TABLE IF NOT EXISTS test_runs(
  run_id TEXT PRIMARY KEY, mode TEXT, site TEXT, operator TEXT, started_at TEXT,
  finished_at TEXT, note TEXT, total INTEGER, hit INTEGER, miss INTEGER,
  false_alarm INTEGER, correct_clear INTEGER, metrics TEXT);
CREATE INDEX IF NOT EXISTS ix_runs_start ON test_runs(started_at);

CREATE TABLE IF NOT EXISTS test_items(
  item_id TEXT PRIMARY KEY, run_id TEXT, sample_id TEXT, sample_name TEXT,
  kind TEXT, expect_alarm INTEGER, got_alarm INTEGER, got_kinds TEXT,
  level TEXT, verdict TEXT, duration_ms INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS ix_items_run ON test_items(run_id);

CREATE TABLE IF NOT EXISTS audits(
  audit_id TEXT PRIMARY KEY, ts TEXT, epoch REAL, action TEXT, detail TEXT,
  operator TEXT, prev_hash TEXT, this_hash TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ep ON audits(epoch);
"""

# 每张表允许写入的字段（白名单，防止 dataclass 加字段把 SQL 写坏）
FIELDS = {
    "devices": ("device_id", "code", "name", "kind", "site", "channel_no", "room_id",
                "proto", "address", "sensitivity", "enabled", "status",
                "last_heartbeat", "note", "created_at"),
    "passes": ("event_id", "device_code", "channel_no", "room_id", "candidate_id",
               "candidate_name", "candidate_key", "seat_no", "ts", "epoch", "result",
               "kinds", "levels", "scores", "duration_ms", "operator", "source", "note"),
    "alarms": ("alarm_id", "event_id", "device_code", "channel_no", "room_id", "kind",
               "kinds", "level", "detail", "candidate_id", "candidate_name",
               "candidate_key", "status", "created_at", "created_epoch", "closed_at",
               "reported", "reported_at", "report_reason"),
    "reinspects": ("reinspect_id", "alarm_id", "event_id", "operator", "witness",
                   "method", "handheld_code", "result", "found_kind", "found_detail",
                   "storage_cell", "started_epoch", "finished_epoch", "started_at",
                   "finished_at", "note"),
    "lockers": ("locker_id", "code", "name", "site", "cells_total", "address", "proto",
                "enabled", "status", "note", "created_at"),
    "cells": ("cell_id", "locker_id", "locker_code", "cell_no", "state", "candidate_id",
              "candidate_name", "candidate_key", "ticket_no", "seat_no", "device_desc",
              "event_id", "stored_at", "stored_epoch", "taken_at", "taken_epoch",
              "method", "overdue_notified", "note"),
    "locker_logs": ("log_id", "locker_code", "cell_no", "action", "method", "operator",
                    "candidate_id", "candidate_name", "candidate_key", "device_desc",
                    "ts", "epoch", "note"),
    "specials": ("record_id", "candidate_id", "candidate_name", "candidate_key",
                 "ticket_no", "room_id", "seat_no", "reason", "inspector", "witness",
                 "method", "result", "detail", "decision", "ts", "epoch", "drill"),
    "flows": ("flow_id", "candidate_id", "candidate_name", "candidate_key", "ticket_no",
              "room_id", "seat_no", "security_at", "security_epoch", "verify_at",
              "verify_epoch", "signin_at", "signin_epoch", "security_event",
              "reentries", "note"),
    "samples": ("sample_id", "name", "kind", "expect_alarm", "scores", "note", "builtin"),
    "test_runs": ("run_id", "mode", "site", "operator", "started_at", "finished_at",
                  "note", "total", "hit", "miss", "false_alarm", "correct_clear", "metrics"),
    "test_items": ("item_id", "run_id", "sample_id", "sample_name", "kind",
                   "expect_alarm", "got_alarm", "got_kinds", "level", "verdict",
                   "duration_ms", "note"),
    "audits": ("audit_id", "ts", "epoch", "action", "detail", "operator",
               "prev_hash", "this_hash"),
}

# 需要**字段级加密**落库的敏感字段（考生标识；身份核验关联就靠这些字段）
SECRET_FIELDS = {
    "passes": ("candidate_id", "candidate_name"),
    "alarms": ("candidate_id", "candidate_name"),
    "cells": ("candidate_id", "candidate_name", "ticket_no", "seat_no"),
    "locker_logs": ("candidate_id", "candidate_name"),
    "specials": ("candidate_id", "candidate_name", "ticket_no", "seat_no"),
    "flows": ("candidate_id", "candidate_name", "ticket_no", "seat_no"),
}

# 带盲索引的表：加密后没法用 SQL 等值查考生，改查不可逆指纹列 candidate_key
BLIND_INDEX_TABLES = ("passes", "alarms", "cells", "locker_logs", "specials", "flows")

# 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
MIGRATE_COLUMNS = {
    "passes": (("candidate_key", "TEXT"),),
    "alarms": (("candidate_key", "TEXT"),),
    "cells": (("candidate_key", "TEXT"),),
    "locker_logs": (("candidate_key", "TEXT"),),
    "specials": (("candidate_key", "TEXT"),),
    "flows": (("candidate_key", "TEXT"),),
}

# JSON 字段（出库时自动反序列化，入库时自动序列化）
JSON_FIELDS = {
    "passes": ("kinds", "levels", "scores"),
    "alarms": ("kinds",),
    "samples": ("scores",),
    "test_runs": ("metrics",),
    "test_items": ("got_kinds",),
}


class Store(object):
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """给老库补新列（本模块升级过表结构，不能让老 data 目录打不开）。"""
        for table, cols in MIGRATE_COLUMNS.items():
            try:
                have = {r[1] for r in self._conn.execute("PRAGMA table_info(%s)" % table)}
            except sqlite3.Error:
                continue
            for name, decl in cols:
                if name in have:
                    continue
                try:
                    self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                       % (table, name, decl))
                except sqlite3.Error:
                    pass

    # ---------------- 基础 ----------------
    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _jsonify(table, row):
        row = {k: v for k, v in row.items() if k in FIELDS[table]}
        # 盲索引必须**先用明文**算（加密后密文每次不同，拿密文算指纹就查不回来了）
        if table in BLIND_INDEX_TABLES:
            row["candidate_key"] = data_crypto.blind_key(row.get("candidate_id") or "")
        # 敏感字段加密落库（未装 cryptography 或设置里关掉时原样写入，并如实提示）
        for k in SECRET_FIELDS.get(table, ()):
            if k in row:
                row[k] = data_crypto.encrypt_field(row[k])
        for k in JSON_FIELDS.get(table, ()):
            if k in row and not isinstance(row[k], str):
                row[k] = models.jdump(row[k])
        for k, v in list(row.items()):
            if isinstance(v, bool):
                row[k] = 1 if v else 0
        return row

    @staticmethod
    def _load(table, row):
        """行 -> dataclass（JSON 字段反序列化，敏感字段解密，布尔字段还原）。"""
        data = dict(row)
        for k in SECRET_FIELDS.get(table, ()):
            if data.get(k):
                data[k] = data_crypto.decrypt_field(data[k])
        for k in JSON_FIELDS.get(table, ()):
            if k in data:
                data[k] = models.jload(data[k], {} if k in ("scores", "metrics") else [])
        for k in ("enabled", "reported", "overdue_notified", "drill", "builtin",
                  "expect_alarm", "got_alarm"):
            if k in data and data[k] is not None:
                data[k] = bool(data[k])
        cls = {"devices": models.Device, "passes": models.PassEvent,
               "alarms": models.Alarm, "reinspects": models.Reinspect,
               "lockers": models.Locker, "cells": models.Cell,
               "locker_logs": models.LockerLog, "specials": models.SpecialRecord,
               "flows": models.FlowRecord, "samples": models.Sample,
               "test_runs": models.TestRun, "test_items": models.TestItem,
               "audits": models.Audit}[table]
        names = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in data.items() if k in names})

    def _upsert(self, table, obj):
        row = self._jsonify(table, obj.to_dict())
        cols = [c for c in FIELDS[table] if c in row]
        sql = ("INSERT OR REPLACE INTO %s(%s) VALUES(%s)"
               % (table, ",".join(cols), ",".join("?" * len(cols))))
        self._exec(sql, tuple(row[c] for c in cols))

    def _list(self, table, where="", params=(), order="", limit=0):
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + where
        if order:
            sql += " ORDER BY " + order
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [self._load(table, r) for r in self._query(sql, params)]

    def _count(self, sql, params=()):
        row = self._one(sql, params)
        return int(list(row.values())[0]) if row else 0

    # ---------------- 审计（哈希链） ----------------
    def log(self, action, detail="", operator=""):
        prev = self._one("SELECT this_hash FROM audits ORDER BY epoch DESC, rowid DESC LIMIT 1")
        prev_hash = (prev or {}).get("this_hash", "") or "GENESIS"
        ts = models.now_ts()
        raw = "%s|%s|%s|%s|%s" % (ts, action, detail, operator, prev_hash)
        this_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        a = models.Audit(audit_id=models.new_id("A"), ts=ts, epoch=models.now_epoch(),
                         action=action, detail=str(detail)[:500], operator=operator,
                         prev_hash=prev_hash, this_hash=this_hash)
        self._upsert("audits", a)
        try:
            with open(str(config.SECURITY_LOG), "a", encoding="utf-8") as f:
                f.write("%s [%s] %s (%s)\n" % (ts, action, detail, operator or "-"))
        except OSError:
            pass
        return a

    def list_audits(self, limit=200):
        return self._list("audits", order="epoch DESC, rowid DESC", limit=limit)

    def verify_audit_chain(self):
        """校验哈希链：返回 (ok, 断链位置, 说明)。"""
        rows = self._query("SELECT * FROM audits ORDER BY epoch ASC, rowid ASC")
        prev = "GENESIS"
        for i, r in enumerate(rows):
            raw = "%s|%s|%s|%s|%s" % (r["ts"], r["action"], r["detail"], r["operator"],
                                      r["prev_hash"])
            calc = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            if r["prev_hash"] != prev:
                return False, i + 1, "第 %d 条 prev_hash 与上一条 this_hash 不一致" % (i + 1)
            if calc != r["this_hash"]:
                return False, i + 1, "第 %d 条内容被改动（哈希不匹配）" % (i + 1)
            prev = r["this_hash"]
        return True, len(rows), "哈希链完整（共 %d 条）" % len(rows)

    # ---------------- 设备 ----------------
    def save_device(self, dev):
        if not dev.device_id:
            dev.device_id = models.new_id("D")
        self._upsert("devices", dev)
        return dev

    def get_device(self, code=None, device_id=None):
        if device_id:
            r = self._one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        else:
            r = self._one("SELECT * FROM devices WHERE code=?", (code or "",))
        return self._load("devices", r) if r else None

    def list_devices(self, kind=None, enabled_only=False):
        where, params = [], []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if enabled_only:
            where.append("enabled=1")
        return self._list("devices", " AND ".join(where), tuple(params), "kind, channel_no, code")

    def delete_device(self, device_id):
        self._exec("DELETE FROM devices WHERE device_id=?", (device_id,))
        return True

    def heartbeat(self, code, status="online"):
        self._exec("UPDATE devices SET status=?, last_heartbeat=? WHERE code=?",
                   (status, models.now_ts(), code))

    def device_online_rate(self):
        rows = self._list("devices")
        if not rows:
            return {"total": 0, "online": 0, "rate": 0.0}
        on = sum(1 for d in rows if d.status == "online")
        return {"total": len(rows), "online": on, "rate": on / float(len(rows))}

    # ---------------- 过检记录 ----------------
    def save_pass(self, ev):
        if not ev.event_id:
            ev.event_id = models.new_id("P")
        self._upsert("passes", ev)
        return ev

    def list_passes(self, limit=200, device_code=None, room_id=None, result=None,
                    since_epoch=None, source=None):
        where, params = [], []
        if device_code:
            where.append("device_code=?")
            params.append(device_code)
        if room_id:
            where.append("room_id=?")
            params.append(room_id)
        if result:
            where.append("result=?")
            params.append(result)
        if source:
            where.append("source=?")
            params.append(source)
        if since_epoch:
            where.append("epoch>=?")
            params.append(float(since_epoch))
        return self._list("passes", " AND ".join(where), tuple(params),
                          "epoch DESC, rowid DESC", limit)

    def pass_count(self, since_epoch=None, device_code=None, result=None, source=None):
        sql = "SELECT COUNT(*) FROM passes WHERE 1=1"
        params = []
        if since_epoch:
            sql += " AND epoch>=?"
            params.append(float(since_epoch))
        if device_code:
            sql += " AND device_code=?"
            params.append(device_code)
        if result:
            sql += " AND result=?"
            params.append(result)
        if source:
            sql += " AND source=?"
            params.append(source)
        return self._count(sql, tuple(params))

    # ---------------- 报警 ----------------
    def save_alarm(self, a):
        if not a.alarm_id:
            a.alarm_id = models.new_id("AL")
        self._upsert("alarms", a)
        return a

    def get_alarm(self, alarm_id):
        r = self._one("SELECT * FROM alarms WHERE alarm_id=?", (str(alarm_id or ""),))
        return self._load("alarms", r) if r else None

    def list_alarms(self, status=None, level=None, limit=200, room_id=None,
                    pending_only=False):
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if pending_only:
            where.append("status IN ('pending','reinspecting')")
        if level:
            where.append("level=?")
            params.append(level)
        if room_id:
            where.append("room_id=?")
            params.append(room_id)
        return self._list("alarms", " AND ".join(where), tuple(params),
                          "created_epoch DESC, rowid DESC", limit)

    def alarm_for_event(self, event_id):
        r = self._one("SELECT * FROM alarms WHERE event_id=? ORDER BY created_epoch DESC LIMIT 1",
                      (event_id,))
        return self._load("alarms", r) if r else None

    def set_alarm_status(self, alarm_id, status, closed=False):
        if closed:
            self._exec("UPDATE alarms SET status=?, closed_at=? WHERE alarm_id=?",
                       (status, models.now_ts(), alarm_id))
        else:
            self._exec("UPDATE alarms SET status=? WHERE alarm_id=?", (status, alarm_id))

    def mark_alarm_reported(self, alarm_id, reason=""):
        self._exec("UPDATE alarms SET reported=1, reported_at=?, report_reason=?"
                   " WHERE alarm_id=?", (models.now_ts(), str(reason)[:300], alarm_id))

    def alarm_stats(self, since_epoch=None):
        where = " WHERE created_epoch>=?" if since_epoch else ""
        params = (float(since_epoch),) if since_epoch else ()
        total = self._count("SELECT COUNT(*) FROM alarms" + where, params)
        red = self._count("SELECT COUNT(*) FROM alarms" + where +
                          (" AND" if where else " WHERE") + " level='red'", params)
        pending = self._count("SELECT COUNT(*) FROM alarms" + where +
                              (" AND" if where else " WHERE") +
                              " status IN ('pending','reinspecting')", params)
        return {"total": total, "red": red, "pending": pending}

    # ---------------- 复检 ----------------
    def save_reinspect(self, r):
        if not r.reinspect_id:
            r.reinspect_id = models.new_id("R")
        self._upsert("reinspects", r)
        return r

    def get_reinspect(self, alarm_id):
        row = self._one("SELECT * FROM reinspects WHERE alarm_id=?"
                        " ORDER BY finished_epoch DESC, rowid DESC LIMIT 1", (alarm_id,))
        return self._load("reinspects", row) if row else None

    def list_reinspects(self, limit=200):
        return self._list("reinspects", order="started_epoch DESC, rowid DESC", limit=limit)

    def reinspect_stats(self, since_epoch=None):
        """复检口径：误报率 = 误报 / 已处置报警；处置率、平均耗时。"""
        where, params = ["finished_epoch>0"], []
        if since_epoch:
            where.append("started_epoch>=?")
            params.append(float(since_epoch))
        rows = self._list("reinspects", " AND ".join(where), tuple(params))
        done = len(rows)
        fp = sum(1 for r in rows if r.result == "false_positive")
        found = sum(1 for r in rows if r.result == "detain")
        manual = sum(1 for r in rows if r.result == "manual_channel")
        costs = [r.cost_sec() for r in rows]
        return {"done": done, "false_positive": fp, "detained": found,
                "to_manual_channel": manual,
                "false_rate": (fp / float(done)) if done else 0.0,
                "avg_cost_sec": (sum(costs) / len(costs)) if costs else 0.0,
                "max_cost_sec": max(costs) if costs else 0.0}

    # ---------------- 储物柜 ----------------
    def save_locker(self, lk):
        if not lk.locker_id:
            lk.locker_id = models.new_id("L")
        self._upsert("lockers", lk)
        return lk

    def get_locker(self, code=None, locker_id=None):
        if locker_id:
            r = self._one("SELECT * FROM lockers WHERE locker_id=?", (locker_id,))
        else:
            r = self._one("SELECT * FROM lockers WHERE code=?", (code or "",))
        return self._load("lockers", r) if r else None

    def list_lockers(self):
        return self._list("lockers", order="code")

    def delete_locker(self, locker_id):
        self._exec("DELETE FROM cells WHERE locker_id=?", (locker_id,))
        self._exec("DELETE FROM lockers WHERE locker_id=?", (locker_id,))

    def ensure_cells(self, locker, prefix=None):
        """按柜体容量补齐柜格（已存在的不动），返回新建数量。"""
        prefix = prefix or (locker.code[:1] or "A")
        have = {c.cell_no for c in self.list_cells(locker_id=locker.locker_id)}
        n = 0
        for i in range(1, int(locker.cells_total or 0) + 1):
            no = "%s-%02d" % (prefix, i)
            if no in have:
                continue
            self._upsert("cells", models.Cell(cell_id=models.new_id("C"),
                                              locker_id=locker.locker_id,
                                              locker_code=locker.code, cell_no=no))
            n += 1
        return n

    def save_cell(self, cell):
        if not cell.cell_id:
            cell.cell_id = models.new_id("C")
        self._upsert("cells", cell)
        return cell

    def get_cell(self, locker_code, cell_no):
        r = self._one("SELECT * FROM cells WHERE locker_code=? AND cell_no=?",
                      (locker_code or "", cell_no or ""))
        return self._load("cells", r) if r else None

    def list_cells(self, locker_id=None, locker_code=None, state=None):
        where, params = [], []
        if locker_id:
            where.append("locker_id=?")
            params.append(locker_id)
        if locker_code:
            where.append("locker_code=?")
            params.append(locker_code)
        if state:
            where.append("state=?")
            params.append(state)
        return self._list("cells", " AND ".join(where), tuple(params), "cell_no")

    def find_cells_by_candidate(self, candidate_id):
        """按考生查在存柜格（走盲索引列：candidate_id 是密文，没法直接等值查）。"""
        return self._list("cells", "candidate_key=? AND state='occupied'",
                          (data_crypto.blind_key(candidate_id),), "stored_epoch DESC")

    def cell_stats(self):
        rows = self._list("cells")
        out = {"total": len(rows), "free": 0, "occupied": 0, "locked": 0,
               "fault": 0, "overdue": 0}
        for c in rows:
            if c.state in out:
                out[c.state] += 1
            if c.overdue():
                out["overdue"] += 1
        out["usage_rate"] = (out["occupied"] / float(out["total"])) if out["total"] else 0.0
        return out

    def log_locker(self, log):
        if not log.log_id:
            log.log_id = models.new_id("LL")
        self._upsert("locker_logs", log)
        return log

    def list_locker_logs(self, limit=200, locker_code=None, candidate_id=None,
                         cell_no=None, action=None):
        where, params = [], []
        for col, val in (("locker_code", locker_code), ("cell_no", cell_no),
                         ("action", action)):
            if val:
                where.append("%s=?" % col)
                params.append(val)
        if candidate_id:
            # 走盲索引（candidate_id 列是密文）
            where.append("candidate_key=?")
            params.append(data_crypto.blind_key(candidate_id))
        return self._list("locker_logs", " AND ".join(where), tuple(params),
                          "epoch DESC, rowid DESC", limit)

    # ---------------- 特殊考生 ----------------
    def save_special(self, rec):
        if not rec.record_id:
            rec.record_id = models.new_id("S")
        self._upsert("specials", rec)
        return rec

    def list_specials(self, limit=200, reason=None, drill=None, since_epoch=None):
        where, params = [], []
        if reason:
            where.append("reason=?")
            params.append(reason)
        if drill is not None:
            where.append("drill=?")
            params.append(1 if drill else 0)
        if since_epoch:
            where.append("epoch>=?")
            params.append(float(since_epoch))
        return self._list("specials", " AND ".join(where), tuple(params),
                          "epoch DESC, rowid DESC", limit)

    def special_stats(self, since_epoch=None):
        rows = self._list("specials")
        if since_epoch:
            rows = [r for r in rows if r.epoch >= float(since_epoch)]
        by_reason = {}
        for r in rows:
            by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
        return {"total": len(rows), "drill": sum(1 for r in rows if r.drill),
                "real": sum(1 for r in rows if not r.drill),
                "by_reason": by_reason,
                "double_witness": sum(1 for r in rows if r.witness)}

    # ---------------- 入场流程 ----------------
    def get_flow(self, candidate_id, room_id=""):
        key = data_crypto.blind_key(candidate_id)
        if room_id:
            r = self._one("SELECT * FROM flows WHERE candidate_key=? AND room_id=?"
                          " ORDER BY rowid DESC LIMIT 1", (key, room_id))
        else:
            r = self._one("SELECT * FROM flows WHERE candidate_key=?"
                          " ORDER BY rowid DESC LIMIT 1", (key,))
        return self._load("flows", r) if r else None

    def save_flow(self, f):
        if not f.flow_id:
            f.flow_id = models.new_id("F")
        self._upsert("flows", f)
        return f

    def list_flows(self, limit=200, since_epoch=None, incomplete_only=False):
        where, params = [], []
        if since_epoch:
            where.append("security_epoch>=?")
            params.append(float(since_epoch))
        if incomplete_only:
            where.append("(signin_epoch IS NULL OR signin_epoch=0)")
        return self._list("flows", " AND ".join(where), tuple(params),
                          "security_epoch DESC, rowid DESC", limit)

    def flow_stats(self, since_epoch=None, window_sec=None):
        rows = self._list("flows")
        if since_epoch:
            rows = [r for r in rows if r.security_epoch >= float(since_epoch)]
        done = [r for r in rows if r.signin_epoch and r.security_epoch]
        costs = [r.total_sec() for r in done]
        stage_costs = {"security": [], "verify": []}
        for r in done:
            for k, v in r.stage_costs().items():
                stage_costs.setdefault(k, []).append(v)
        avg = {}
        for k, vals in stage_costs.items():
            avg[k] = (sum(vals) / len(vals)) if vals else 0.0
        bottleneck = ""
        if avg:
            bottleneck = max(avg.items(), key=lambda kv: kv[1])[0]
        return {"tracked": len(rows), "completed": len(done),
                "avg_total_sec": (sum(costs) / len(costs)) if costs else 0.0,
                "max_total_sec": max(costs) if costs else 0.0,
                "avg_stage_sec": avg, "bottleneck": bottleneck}

    # ---------------- 样本与验收测试 ----------------
    def save_sample(self, s):
        if not s.sample_id:
            s.sample_id = models.new_id("SP")
        self._upsert("samples", s)
        return s

    def list_samples(self):
        return self._list("samples", order="builtin DESC, kind, name")

    def get_sample(self, sample_id):
        r = self._one("SELECT * FROM samples WHERE sample_id=?", (sample_id,))
        return self._load("samples", r) if r else None

    def delete_sample(self, sample_id):
        self._exec("DELETE FROM samples WHERE sample_id=?", (sample_id,))

    def save_test_run(self, run):
        if not run.run_id:
            run.run_id = models.new_id("T")
        self._upsert("test_runs", run)
        return run

    def list_test_runs(self, limit=50, mode=None):
        where = "mode=?" if mode else ""
        params = (mode,) if mode else ()
        return self._list("test_runs", where, params, "started_at DESC, rowid DESC", limit)

    def latest_test_run(self, mode=None):
        runs = self.list_test_runs(limit=1, mode=mode)
        return runs[0] if runs else None

    def save_test_item(self, item):
        if not item.item_id:
            item.item_id = models.new_id("TI")
        self._upsert("test_items", item)
        return item

    def list_test_items(self, run_id, limit=1000):
        return self._list("test_items", "run_id=?", (run_id,), "rowid", limit)

    # ---------------- 统计总览 ----------------
    def throughput_ppm(self, window_sec=None, device_code=None, since_epoch=None):
        """通过效率（人/分钟）：窗口内过检数 / 分钟数。"""
        window_sec = float(window_sec or config.THROUGHPUT_WINDOW_SEC)
        since = since_epoch or (time.time() - window_sec)
        n = self.pass_count(since_epoch=since, device_code=device_code)
        return n / (window_sec / 60.0)

    def hourly_histogram(self, since_epoch=None):
        """按小时统计过检与报警（用于流量曲线与高峰期疏导）。"""
        since = float(since_epoch or (time.time() - 6 * 3600))
        rows = self._query("SELECT strftime('%H', ts) AS hh, COUNT(*) AS n"
                           " FROM passes WHERE epoch>=? GROUP BY hh ORDER BY hh", (since,))
        alarms = self._query("SELECT strftime('%H', created_at) AS hh, COUNT(*) AS n"
                             " FROM alarms WHERE created_epoch>=? GROUP BY hh ORDER BY hh",
                             (since,))
        amap = {r["hh"]: r["n"] for r in alarms}
        return [{"hour": r["hh"], "passes": r["n"], "alarms": amap.get(r["hh"], 0)}
                for r in rows]

    def summary(self, since_epoch=None):
        since = since_epoch or (time.time() - 24 * 3600)
        passes = self.pass_count(since_epoch=since)
        last_run = self.latest_test_run()
        return {
            "passes": passes,
            "alarms": self.alarm_stats(since),
            "reinspects": self.reinspect_stats(since),
            "throughput_ppm": self.throughput_ppm(since_epoch=since),
            "devices": self.device_online_rate(),
            "cells": self.cell_stats(),
            "specials": self.special_stats(since),
            "flows": self.flow_stats(since),
            "test_run": (last_run.to_dict() if last_run else {}),
            "data_dir": str(config.DATA_ROOT),
        }

    def purge_runtime(self, keep_days=30):
        """
        清理运行期数据（合规：安检图像/记录不长期保留）。
        keep_days 之前的过检记录、已处置报警、开柜日志会被删除，审计保留。
        """
        cut = time.time() - float(keep_days) * 86400
        n1 = self._exec("DELETE FROM passes WHERE epoch<?", (cut,)).rowcount
        n2 = self._exec("DELETE FROM alarms WHERE created_epoch<? AND status='closed'",
                        (cut,)).rowcount
        n3 = self._exec("DELETE FROM locker_logs WHERE epoch<?", (cut,)).rowcount
        self.log("清理运行期数据", "删除过检 %d、已处置报警 %d、开柜日志 %d（保留 %d 天）"
                 % (n1, n2, n3, keep_days), "system")
        return {"passes": n1, "alarms": n2, "locker_logs": n3}


def export_json(obj_list, path):
    """把 dataclass 列表导出成 JSON 文件（样本/柜体清单用）。"""
    try:
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump([o.to_dict() if hasattr(o, "to_dict") else o for o in obj_list],
                      f, ensure_ascii=False, indent=2)
        return True, str(path)
    except OSError as e:
        return False, str(e)
