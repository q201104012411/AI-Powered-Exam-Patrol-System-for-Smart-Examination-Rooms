# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 本地存储（SQLite）

表：
  cameras      机位台账（六面覆盖、双路冗余、台账核查）
  access       门禁事件
  intrusion    入侵探测事件
  safes        试卷柜电子锁事件
  env          环境监测采样
  bags         试卷袋（含电子铅封）
  handovers    交接记录
  escort_tasks 押运任务
  escort_pts   押运轨迹点
  deliveries   分发记录
  recycles     回收封存记录
  segments     录像段（连续性/留存）
  alarms       告警（含处置留痕）
  audit        哈希链审计日志
  kv           键值（账户等）
"""
import json
import sqlite3
import threading

import config
import models


class Store(object):
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._lock = threading.RLock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS cameras (
                cam_id TEXT PRIMARY KEY, room_id TEXT, name TEXT, face TEXT,
                url TEXT, angle_deg REAL, checkpoints TEXT, channels TEXT,
                baseline_hash TEXT, ledger TEXT, online INTEGER DEFAULT 0,
                last_seen TEXT, registered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS access (
                event_id TEXT PRIMARY KEY, room_id TEXT, person TEXT, factors TEXT,
                persons TEXT, result TEXT, deny_reason TEXT, ts TEXT, epoch REAL,
                extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_access_epoch ON access(epoch);

            CREATE TABLE IF NOT EXISTS intrusion (
                event_id TEXT PRIMARY KEY, room_id TEXT, kind TEXT, sensor_id TEXT,
                zone TEXT, ts TEXT, epoch REAL, armed INTEGER DEFAULT 1, extra TEXT
            );

            CREATE TABLE IF NOT EXISTS safes (
                event_id TEXT PRIMARY KEY, room_id TEXT, safe_id TEXT, person TEXT,
                state TEXT, authorized INTEGER DEFAULT 0, inner TEXT, ts TEXT,
                epoch REAL, extra TEXT
            );

            CREATE TABLE IF NOT EXISTS env (
                sample_id TEXT PRIMARY KEY, room_id TEXT, temp_c REAL,
                humidity_pct REAL, water INTEGER DEFAULT 0, smoke INTEGER DEFAULT 0,
                ts TEXT, epoch REAL
            );

            CREATE TABLE IF NOT EXISTS bags (
                bag_id TEXT PRIMARY KEY, rfid TEXT, bundle_id TEXT, subject TEXT,
                quantity INTEGER, seal_id TEXT, seal_state TEXT, seal_at TEXT,
                seal_by TEXT, room_id TEXT, current_stage TEXT, history TEXT, extra TEXT
            );

            CREATE TABLE IF NOT EXISTS handovers (
                handover_id TEXT PRIMARY KEY, bag_id TEXT, stage TEXT,
                from_person TEXT, to_person TEXT, persons TEXT, signatures TEXT,
                faces TEXT, method TEXT, quantity INTEGER, room_id TEXT, ts TEXT,
                epoch REAL, problems TEXT, extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_handover_epoch ON handovers(epoch);

            CREATE TABLE IF NOT EXISTS escort_tasks (
                task_id TEXT PRIMARY KEY, plate TEXT, driver TEXT,
                escort_persons TEXT, bag_ids TEXT, route TEXT, video_url TEXT,
                video_online INTEGER DEFAULT 0, start_ts TEXT, start_epoch REAL,
                end_ts TEXT, status TEXT, extra TEXT
            );

            CREATE TABLE IF NOT EXISTS escort_pts (
                point_id TEXT PRIMARY KEY, task_id TEXT, lat REAL, lng REAL,
                speed_kmh REAL, source TEXT, ts TEXT, epoch REAL, offset_m REAL,
                deviated INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_escortpts ON escort_pts(task_id, epoch);

            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY, bag_id TEXT, room_id TEXT,
                quantity INTEGER, person TEXT, ts TEXT, epoch REAL, note TEXT
            );

            CREATE TABLE IF NOT EXISTS recycles (
                recycle_id TEXT PRIMARY KEY, bag_id TEXT, room_id TEXT,
                issued_qty INTEGER, returned_qty INTEGER, diff INTEGER,
                person TEXT, sealed INTEGER DEFAULT 0, seal_id TEXT, ts TEXT,
                epoch REAL, note TEXT
            );

            CREATE TABLE IF NOT EXISTS segments (
                seg_id TEXT PRIMARY KEY, cam_id TEXT, channel TEXT,
                start_epoch REAL, duration_sec REAL, playable INTEGER DEFAULT 1,
                locked INTEGER DEFAULT 1, start_ts TEXT, retain_until REAL
            );
            CREATE INDEX IF NOT EXISTS idx_seg ON segments(cam_id, channel, start_epoch);

            CREATE TABLE IF NOT EXISTS alarms (
                alarm_id TEXT PRIMARY KEY, code TEXT, level TEXT, title TEXT,
                detail TEXT, source_kind TEXT, source_id TEXT, scope TEXT,
                created_at TEXT, created_epoch REAL, status TEXT,
                acked_by TEXT, acked_at TEXT, handled_by TEXT, handled_at TEXT,
                handle_note TEXT, sla_sec REAL, latency_sec REAL, evidence TEXT,
                extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alarms_status ON alarms(status, created_epoch);
            CREATE INDEX IF NOT EXISTS idx_alarms_code ON alarms(code, created_epoch);

            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT,
                detail TEXT, operator TEXT, prev_hash TEXT, hash TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """)

    # ================= 审计哈希链 =================
    def log(self, action, detail="", operator="system"):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else models.GENESIS
            ts = models.now_ts()
            h = models.AuditChain.calc(prev, ts, action, detail, operator)
            c.execute("INSERT INTO audit(ts,action,detail,operator,prev_hash,hash) "
                      "VALUES(?,?,?,?,?,?)", (ts, action, detail, operator, prev, h))
            return h

    def audit_rows(self, limit=500):
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM audit ORDER BY seq ASC LIMIT ?", (int(limit),))]

    def audit_verify(self):
        return models.AuditChain.verify(self.audit_rows(limit=100000))

    # ================= kv =================
    def get_kv(self, key, default=None):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def set_kv(self, key, value):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    # ================= 通用读写小工具 =================
    def _upsert(self, table, pk, obj, cols=None):
        row = obj.to_row()
        keys = cols or list(row.keys())
        cols_sql = ",".join(keys)
        ph = ",".join("?" for _ in keys)
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO %s(%s) VALUES(%s)"
                      % (table, cols_sql, ph), tuple(row[k] for k in keys))

    def _query(self, sql, args=()):
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    # ================= cameras =================
    def save_camera(self, cam):
        self._upsert("cameras", "cam_id", cam)

    def cameras(self, room_id=None):
        if room_id:
            rows = self._query("SELECT * FROM cameras WHERE room_id=? ORDER BY face",
                               (room_id,))
        else:
            rows = self._query("SELECT * FROM cameras ORDER BY room_id, face")
        return [models.Camera.from_row(r) for r in rows]

    def get_camera(self, cam_id):
        rows = self._query("SELECT * FROM cameras WHERE cam_id=?", (cam_id,))
        return models.Camera.from_row(rows[0]) if rows else None

    def delete_camera(self, cam_id):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM cameras WHERE cam_id=?", (cam_id,))

    def rooms(self):
        rows = self._query("SELECT DISTINCT room_id FROM cameras "
                           "WHERE room_id<>'' ORDER BY room_id")
        return [r["room_id"] for r in rows]

    def set_camera_online(self, cam_id, online):
        with self._lock, self._conn() as c:
            c.execute("UPDATE cameras SET online=?, last_seen=? WHERE cam_id=?",
                      (1 if online else 0, models.now_ts() if online else "", cam_id))

    # ================= access / intrusion / safes / env =================
    def save_access(self, e):
        self._upsert("access", "event_id", e)

    def access_events(self, room_id=None, limit=200):
        if room_id:
            rows = self._query("SELECT * FROM access WHERE room_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (room_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM access ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.AccessEvent.from_row(r) for r in rows]

    def save_intrusion(self, e):
        self._upsert("intrusion", "event_id", e)

    def intrusion_events(self, room_id=None, limit=200):
        if room_id:
            rows = self._query("SELECT * FROM intrusion WHERE room_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (room_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM intrusion ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.IntrusionEvent.from_row(r) for r in rows]

    def save_safe_event(self, e):
        self._upsert("safes", "event_id", e)

    def safe_events(self, room_id=None, limit=200):
        if room_id:
            rows = self._query("SELECT * FROM safes WHERE room_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (room_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM safes ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.SafeEvent.from_row(r) for r in rows]

    def save_env(self, s):
        self._upsert("env", "sample_id", s)

    def env_samples(self, room_id=None, limit=200):
        if room_id:
            rows = self._query("SELECT * FROM env WHERE room_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (room_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM env ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.EnvSample.from_row(r) for r in rows]

    # ================= bags / handovers =================
    def save_bag(self, b):
        self._upsert("bags", "bag_id", b)

    def bags(self, room_id=None):
        if room_id:
            rows = self._query("SELECT * FROM bags WHERE room_id=? ORDER BY bag_id",
                               (room_id,))
        else:
            rows = self._query("SELECT * FROM bags ORDER BY bag_id")
        return [models.Bag.from_row(r) for r in rows]

    def get_bag(self, bag_id):
        rows = self._query("SELECT * FROM bags WHERE bag_id=?", (bag_id,))
        return models.Bag.from_row(rows[0]) if rows else None

    def save_handover(self, h):
        h.check()
        self._upsert("handovers", "handover_id", h)

    def handovers(self, bag_id=None, limit=500):
        if bag_id:
            rows = self._query("SELECT * FROM handovers WHERE bag_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (bag_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM handovers ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.Handover.from_row(r) for r in rows]

    # ================= 押运 =================
    def save_escort_task(self, t):
        self._upsert("escort_tasks", "task_id", t)

    def escort_tasks(self, status=None):
        if status:
            rows = self._query("SELECT * FROM escort_tasks WHERE status=? "
                               "ORDER BY start_epoch DESC", (status,))
        else:
            rows = self._query("SELECT * FROM escort_tasks ORDER BY start_epoch DESC")
        return [models.EscortTask.from_row(r) for r in rows]

    def get_escort_task(self, task_id):
        rows = self._query("SELECT * FROM escort_tasks WHERE task_id=?", (task_id,))
        return models.EscortTask.from_row(rows[0]) if rows else None

    def save_escort_point(self, p):
        self._upsert("escort_pts", "point_id", p)

    def escort_points(self, task_id, limit=5000):
        rows = self._query("SELECT * FROM escort_pts WHERE task_id=? "
                           "ORDER BY epoch ASC LIMIT ?", (task_id, int(limit)))
        return [models.EscortPoint.from_row(r) for r in rows]

    # ================= 分发 / 回收 =================
    def save_delivery(self, d):
        self._upsert("deliveries", "delivery_id", d)

    def deliveries(self, bag_id=None, limit=500):
        if bag_id:
            rows = self._query("SELECT * FROM deliveries WHERE bag_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (bag_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM deliveries ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.Delivery.from_row(r) for r in rows]

    def save_recycle(self, r):
        self._upsert("recycles", "recycle_id", r)

    def recycles(self, bag_id=None, limit=500):
        if bag_id:
            rows = self._query("SELECT * FROM recycles WHERE bag_id=? "
                               "ORDER BY epoch DESC LIMIT ?", (bag_id, int(limit)))
        else:
            rows = self._query("SELECT * FROM recycles ORDER BY epoch DESC LIMIT ?",
                               (int(limit),))
        return [models.Recycle.from_row(r) for r in rows]

    # ================= 录像段 =================
    def save_segment(self, s):
        self._upsert("segments", "seg_id", s)

    def segments(self, cam_id=None, channel=None):
        sql, args = "SELECT * FROM segments", []
        cond = []
        if cam_id:
            cond.append("cam_id=?")
            args.append(cam_id)
        if channel:
            cond.append("channel=?")
            args.append(channel)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY cam_id, channel, start_epoch"
        return [models.RecordSegment.from_row(r) for r in self._query(sql, tuple(args))]

    # ================= 告警 =================
    def save_alarm(self, a):
        self._upsert("alarms", "alarm_id", a)

    def alarms(self, status=None, code=None, limit=500):
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if code:
            cond.append("code=?")
            args.append(code)
        sql = "SELECT * FROM alarms"
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY created_epoch DESC LIMIT ?"
        args.append(int(limit))
        return [models.Alarm.from_row(r) for r in self._query(sql, tuple(args))]

    def get_alarm(self, alarm_id):
        rows = self._query("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,))
        return models.Alarm.from_row(rows[0]) if rows else None

    def ack_alarm(self, alarm_id, operator, note=""):
        with self._lock, self._conn() as c:
            c.execute("UPDATE alarms SET status=?, acked_by=?, acked_at=?, "
                      "handle_note=CASE WHEN ?='' THEN handle_note ELSE ? END "
                      "WHERE alarm_id=? AND status=?",
                      (models.ST_ACKED, operator, models.now_ts(), note, note,
                       alarm_id, models.ST_PENDING))
            return c.total_changes

    def handle_alarm(self, alarm_id, operator, note=""):
        with self._lock, self._conn() as c:
            c.execute("UPDATE alarms SET status=?, handled_by=?, handled_at=?, "
                      "handle_note=? WHERE alarm_id=? AND status<>?",
                      (models.ST_HANDLED, operator, models.now_ts(), note,
                       alarm_id, models.ST_HANDLED))
            return c.total_changes

    def open_red_alarms(self):
        return [a for a in self.alarms(status=models.ST_PENDING, limit=500)
                if a.is_red()]
