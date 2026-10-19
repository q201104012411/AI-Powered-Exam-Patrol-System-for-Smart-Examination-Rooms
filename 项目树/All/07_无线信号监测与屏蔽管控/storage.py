# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 本地存储（sqlite3，零第三方依赖）

为什么本模块也要一个 Store：屏蔽设备的开关记录、审批/报备、实测证据是**合规证据**，
必须能按时间回溯、能核对与考试时间表是否一致；频谱异常也要能和定位结果、联动工单
对得上。全部落在本目录 data\rf.db，可用 EXAM_RF_HOME 重定向（自检/演练走临时目录）。

留痕：`audits` 表是哈希链（prev_hash -> hash），任何一条被改都会在 verify_chain 暴露。
"""
import dataclasses
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import config
import models
from models import (Anomaly, Approval, Audit, Correlation, Device, Feature,
                    JammerState, Location, ReplayItem, ReplayRun, Scan, Session,
                    ShieldTest, SpillTest, SwitchLog, canonical, new_id, parse_json)


def _dict_json(text, default=None):
    val = parse_json(text, {} if default is None else default)
    return val if isinstance(val, dict) else ({} if default is None else default)


def _list_json(text, default=None):
    val = parse_json(text, [] if default is None else default)
    return val if isinstance(val, list) else ([] if default is None else default)


class Store(object):
    """全部业务的唯一入口（线程安全，一个进程一个连接）。"""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path or config.DB_PATH)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self._migrate()

    # ------------------------------------------------------------------ 基础
    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _exec(self, sql, params=()):
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _migrate(self):
        ddl = (
            """CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY, code TEXT, name TEXT, kind TEXT,
                site TEXT, room_id TEXT, x REAL, y REAL, located INTEGER,
                direction REAL,
                sector_width REAL, bands TEXT, power_w REAL, max_power_w REAL,
                status TEXT, proto TEXT, address TEXT, last_seen TEXT, note TEXT,
                created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS scans (
                scan_id TEXT PRIMARY KEY, station_code TEXT, band TEXT, ts TEXT,
                ts_epoch REAL, noise_floor_dbm REAL, peak_count INTEGER,
                occupancy REAL, source TEXT, peaks TEXT, raw TEXT)""",
            """CREATE TABLE IF NOT EXISTS anomalies (
                anomaly_id TEXT PRIMARY KEY, station_code TEXT, room_id TEXT,
                kind TEXT, band TEXT, freq_mhz REAL, bandwidth_mhz REAL,
                peak_dbm REAL, noise_floor_dbm REAL, snr_db REAL, severity TEXT,
                score REAL, feature_code TEXT, feature_name TEXT, legal INTEGER,
                status TEXT, detected_epoch REAL, first_seen_epoch REAL,
                last_seen_epoch REAL, seen_count INTEGER, latency_sec REAL,
                located_x REAL, located_y REAL, radius_m REAL, confidence REAL,
                detail TEXT, source TEXT, alert_id TEXT, reported_at TEXT,
                review_conclusion TEXT, review_reason TEXT, reviewer TEXT,
                reviewed_at TEXT, created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS jammer_states (
                device_id TEXT PRIMARY KEY, is_on INTEGER, since_epoch REAL,
                bands TEXT, power_w REAL, session_id TEXT, approval_no TEXT,
                operator TEXT, reason TEXT, updated_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS switch_logs (
                log_id TEXT PRIMARY KEY, device_id TEXT, action TEXT, bands TEXT,
                power_w REAL, session_id TEXT, source TEXT, operator TEXT,
                reason TEXT, ts TEXT, ts_epoch REAL, extra TEXT)""",
            """CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, kind TEXT, no TEXT, org TEXT,
                site TEXT, bands TEXT, valid_from TEXT, valid_to TEXT, doc TEXT,
                status TEXT, note TEXT, created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS spill_tests (
                test_id TEXT PRIMARY KEY, jammer_id TEXT, point TEXT,
                distance_m REAL, band TEXT, measured_dbm REAL, limit_dbm REAL,
                result TEXT, measured_at TEXT, ts_epoch REAL, operator TEXT,
                note TEXT)""",
            """CREATE TABLE IF NOT EXISTS shield_tests (
                test_id TEXT PRIMARY KEY, room_id TEXT, band TEXT,
                before_dbm REAL, after_dbm REAL, attenuation_db REAL, rate REAL,
                result TEXT, measured_at TEXT, ts_epoch REAL, operator TEXT,
                note TEXT)""",
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, name TEXT, subject TEXT, date TEXT,
                start TEXT, end TEXT, pre_open_sec INTEGER, post_close_sec INTEGER,
                rooms TEXT, state TEXT, operator TEXT, note TEXT, created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS features (
                feature_code TEXT PRIMARY KEY, name TEXT, low_mhz REAL,
                high_mhz REAL, bandwidth_mhz REAL, modulation TEXT, duty TEXT,
                is_legal INTEGER, base_confidence REAL, enabled INTEGER,
                builtin INTEGER, note TEXT)""",
            """CREATE TABLE IF NOT EXISTS correlations (
                corr_id TEXT PRIMARY KEY, source TEXT, ref_id TEXT,
                anomaly_id TEXT, room_id TEXT, detail TEXT, created_epoch REAL,
                raw TEXT)""",
            """CREATE TABLE IF NOT EXISTS locations (
                loc_id TEXT PRIMARY KEY, anomaly_id TEXT, x REAL, y REAL,
                radius_m REAL, confidence REAL, method TEXT, stations TEXT,
                created_epoch REAL, note TEXT)""",
            """CREATE TABLE IF NOT EXISTS replay_runs (
                run_id TEXT PRIMARY KEY, total INTEGER, hit INTEGER, miss INTEGER,
                false_alarm INTEGER, avg_latency REAL, max_latency REAL,
                detect_rate REAL, false_rate REAL, metrics TEXT, operator TEXT,
                note TEXT, created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS replay_items (
                item_id TEXT PRIMARY KEY, run_id TEXT, scenario_id TEXT,
                name TEXT, expected TEXT, detected TEXT, ok INTEGER,
                latency_sec REAL, detail TEXT)""",
            """CREATE TABLE IF NOT EXISTS audits (
                audit_id TEXT PRIMARY KEY, ts TEXT, ts_epoch REAL, action TEXT,
                detail TEXT, operator TEXT, prev_hash TEXT, hash TEXT)""",
        )
        with self._lock, self._conn:
            for sql in ddl:
                self._conn.execute(sql)
            # 旧库升级：早期版本没有 located 列，这里幂等补齐（不重建、不丢数据）
            self._ensure_column("devices", "located", "INTEGER DEFAULT 0")
            # 常用查询的索引（幂等）
            for sql in ("CREATE INDEX IF NOT EXISTS ix_anom_time "
                        "ON anomalies(detected_epoch DESC)",
                        "CREATE INDEX IF NOT EXISTS ix_anom_status "
                        "ON anomalies(status, severity)",
                        "CREATE INDEX IF NOT EXISTS ix_scan_time "
                        "ON scans(ts_epoch DESC)",
                        "CREATE INDEX IF NOT EXISTS ix_switch_time "
                        "ON switch_logs(ts_epoch DESC)",
                        "CREATE INDEX IF NOT EXISTS ix_audit_time "
                        "ON audits(ts_epoch DESC)"):
                try:
                    self._conn.execute(sql)
                except sqlite3.DatabaseError:
                    pass

    def _ensure_column(self, table, column, decl):
        try:
            cols = [r["name"] for r in self._conn.execute(
                "PRAGMA table_info(%s)" % table).fetchall()]
        except sqlite3.DatabaseError:
            return False
        if column in cols:
            return False
        try:
            self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                               % (table, column, decl))
            return True
        except sqlite3.DatabaseError:
            return False

    # ------------------------------------------------------------ 通用映射
    def _save(self, table, obj):
        cls = type(obj)
        names = [f.name for f in dataclasses.fields(cls)]
        vals = []
        for name in names:
            v = getattr(obj, name)
            if name in cls.JSON_FIELDS:
                v = json.dumps(v if v is not None else ({} if name in
                                                        ("raw", "extra", "metrics")
                                                        else []),
                               ensure_ascii=False)
            vals.append(v)
        sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
            table, ", ".join(names), ", ".join("?" * len(names)))
        with self._lock, self._conn:
            self._conn.execute(sql, vals)
        return obj

    def _obj(self, cls, row):
        if row is None:
            return None
        d = {k: row[k] for k in row.keys()}
        kw = {}
        for f in dataclasses.fields(cls):
            if f.name not in d:
                continue
            v = d[f.name]
            if f.name in cls.JSON_FIELDS:
                if f.name in ("raw", "extra", "metrics"):
                    v = _dict_json(v)
                else:
                    v = _list_json(v)
            kw[f.name] = v
        try:
            return cls(**kw)
        except TypeError:
            return None

    def _list(self, table, cls, where="", params=(), order="rowid DESC", limit=0):
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + where
        if order:
            sql += " ORDER BY " + order
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [self._obj(cls, r) for r in self._query(sql, params)]

    def _count(self, sql, params=()):
        row = self._one(sql, params)
        return int(row[0]) if row and row[0] is not None else 0

    # ================================================================= 设备
    def save_device(self, dev):
        if not dev.device_id:
            dev.device_id = new_id("DEV")
        if not dev.created_at:
            dev.created_at = models.now_ts()
        return self._save("devices", dev)

    def get_device(self, code=None, device_id=None):
        if device_id:
            return self._obj(Device, self._one(
                "SELECT * FROM devices WHERE device_id=?", (device_id,)))
        if code:
            return self._obj(Device, self._one(
                "SELECT * FROM devices WHERE code=?", (code,)))
        return None

    def list_devices(self, kind=None):
        if kind:
            return self._list("devices", Device, "kind=?", (kind,), "code ASC")
        return self._list("devices", Device, order="kind ASC, code ASC")

    def delete_device(self, device_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
            self._conn.execute("DELETE FROM jammer_states WHERE device_id=?", (device_id,))

    def heartbeat(self, code, status="online"):
        dev = self.get_device(code=code)
        if dev is None:
            return None
        dev.status = status
        dev.last_seen = models.now_ts()
        self.save_device(dev)
        return dev

    # ================================================================= 扫描
    def save_scan(self, scan):
        if not scan.scan_id:
            scan.scan_id = new_id("SCN")
        if not scan.ts_epoch:
            scan.ts_epoch = models.now_epoch()
        if not scan.ts:
            scan.ts = models.ts_text(scan.ts_epoch)
        return self._save("scans", scan)

    def get_scan(self, scan_id):
        return self._obj(Scan, self._one("SELECT * FROM scans WHERE scan_id=?",
                                        (scan_id,)))

    def list_scans(self, station_code=None, band=None, since_epoch=None, limit=100):
        where, params = [], []
        if station_code:
            where.append("station_code=?")
            params.append(station_code)
        if band:
            where.append("band=?")
            params.append(band)
        if since_epoch:
            where.append("ts_epoch>=?")
            params.append(float(since_epoch))
        return self._list("scans", Scan, " AND ".join(where), tuple(params),
                          "ts_epoch DESC", int(limit))

    def latest_scan(self, station_code=None, band=None):
        rows = self.list_scans(station_code=station_code, band=band, limit=1)
        return rows[0] if rows else None

    def scan_count(self, since_epoch=None):
        if since_epoch:
            return self._count("SELECT COUNT(*) FROM scans WHERE ts_epoch>=?",
                               (float(since_epoch),))
        return self._count("SELECT COUNT(*) FROM scans")

    def purge_scans(self, keep_days=7):
        cut = models.now_epoch() - float(keep_days) * 86400
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM scans WHERE ts_epoch<?", (cut,))
        return cur.rowcount

    # ================================================================= 异常
    def save_anomaly(self, a):
        if not a.anomaly_id:
            a.anomaly_id = new_id("ANM")
        if not a.detected_epoch:
            a.detected_epoch = models.now_epoch()
        if not a.first_seen_epoch:
            a.first_seen_epoch = a.detected_epoch
        if not a.created_at:
            a.created_at = models.now_ts()
        return self._save("anomalies", a)

    def get_anomaly(self, anomaly_id):
        return self._obj(Anomaly, self._one("SELECT * FROM anomalies WHERE anomaly_id=?",
                                           (anomaly_id,)))

    def list_anomalies(self, status=None, severity=None, kind=None, station_code=None,
                       room_id=None, since_epoch=None, limit=100):
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if severity:
            where.append("severity=?")
            params.append(severity)
        if kind:
            where.append("kind=?")
            params.append(kind)
        if station_code:
            where.append("station_code=?")
            params.append(station_code)
        if room_id:
            where.append("room_id=?")
            params.append(room_id)
        if since_epoch:
            where.append("detected_epoch>=?")
            params.append(float(since_epoch))
        return self._list("anomalies", Anomaly, " AND ".join(where), tuple(params),
                          "detected_epoch DESC", int(limit))

    def find_recent_duplicate(self, station_code, band, freq_mhz, tolerance,
                              window_sec, now=None):
        """
        同站、同频（±tolerance）、时间窗内、仍待处置的异常 -> 复用，避免刷屏。

        时间窗必须**双侧限**（now-window ~ now）：只允许并入"已经发生过"的异常，
        不能并到未来时间戳的异常上（回放/补历史数据时会出现未来样本）。
        """
        now = now or models.now_epoch()
        row = self._one(
            "SELECT * FROM anomalies WHERE station_code=? AND band=? "
            "AND status IN ('open','acked') AND last_seen_epoch>=? "
            "AND last_seen_epoch<=? AND ABS(freq_mhz-?)<=? "
            "ORDER BY detected_epoch DESC LIMIT 1",
            (station_code, band, now - float(window_sec), now + 1.0,
             float(freq_mhz), float(tolerance)))
        return self._obj(Anomaly, row)

    def anomaly_stats(self, since_epoch=None):
        cut = float(since_epoch) if since_epoch else 0.0
        total = self._count("SELECT COUNT(*) FROM anomalies WHERE detected_epoch>=?",
                            (cut,))
        out = {"total": total}
        for sev in ("critical", "warn", "watch"):
            out[sev] = self._count(
                "SELECT COUNT(*) FROM anomalies WHERE detected_epoch>=? AND severity=?",
                (cut, sev))
        for st in config.ANOMALY_STATUSES:
            out[st] = self._count(
                "SELECT COUNT(*) FROM anomalies WHERE detected_epoch>=? AND status=?",
                (cut, st))
        row = self._one(
            "SELECT AVG(latency_sec), MAX(latency_sec), COUNT(*) FROM anomalies "
            "WHERE detected_epoch>=? AND latency_sec>0", (cut,))
        out["avg_latency"] = round(float(row[0] or 0), 3) if row else 0.0
        out["max_latency"] = round(float(row[1] or 0), 3) if row else 0.0
        out["latency_n"] = int(row[2] or 0) if row else 0
        out["open"] = self._count(
            "SELECT COUNT(*) FROM anomalies WHERE status IN ('open','acked')")
        out["reported"] = self._count(
            "SELECT COUNT(*) FROM anomalies WHERE alert_id<>''")
        return out

    def latency_percentile(self, pct=0.95, since_epoch=None):
        cut = float(since_epoch) if since_epoch else 0.0
        rows = self._query(
            "SELECT latency_sec FROM anomalies WHERE detected_epoch>=? "
            "AND latency_sec>0 ORDER BY latency_sec ASC", (cut,))
        vals = [float(r[0]) for r in rows if r[0] is not None]
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, max(0, int(round(pct * (len(vals) - 1)))))
        return round(vals[idx], 3)

    # ============================================================ 屏蔽状态
    def save_state(self, st):
        if not st.updated_at:
            st.updated_at = models.now_ts()
        return self._save("jammer_states", st)

    def get_state(self, device_id):
        return self._obj(JammerState, self._one(
            "SELECT * FROM jammer_states WHERE device_id=?", (device_id,)))

    def delete_state(self, device_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jammer_states WHERE device_id=?",
                               (device_id,))

    def list_states(self):
        return self._list("jammer_states", JammerState, order="updated_at DESC")

    def on_states(self):
        return self._list("jammer_states", JammerState, "is_on=1", (), "updated_at DESC")

    def save_switch(self, log):
        if not log.log_id:
            log.log_id = new_id("SW")
        if not log.ts_epoch:
            log.ts_epoch = models.now_epoch()
        if not log.ts:
            log.ts = models.ts_text(log.ts_epoch)
        return self._save("switch_logs", log)

    def list_switches(self, device_id=None, action=None, since_epoch=None, limit=200):
        where, params = [], []
        if device_id:
            where.append("device_id=?")
            params.append(device_id)
        if action:
            where.append("action=?")
            params.append(action)
        if since_epoch:
            where.append("ts_epoch>=?")
            params.append(float(since_epoch))
        return self._list("switch_logs", SwitchLog, " AND ".join(where), tuple(params),
                          "ts_epoch DESC", int(limit))

    def last_switch(self, device_id, action=None):
        where = ["device_id=?"]
        params = [device_id]
        if action:
            where.append("action=?")
            params.append(action)
        row = self._one("SELECT * FROM switch_logs WHERE " + " AND ".join(where) +
                        " ORDER BY ts_epoch DESC LIMIT 1", tuple(params))
        return self._obj(SwitchLog, row)

    # ============================================================== 审批
    def save_approval(self, ap):
        if not ap.approval_id:
            ap.approval_id = new_id("APR")
        if not ap.created_at:
            ap.created_at = models.now_ts()
        return self._save("approvals", ap)

    def get_approval(self, approval_id):
        return self._obj(Approval, self._one(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)))

    def list_approvals(self, kind=None, site=None, status=None, limit=100):
        where, params = [], []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if site:
            where.append("(site=? OR site='*')")
            params.append(site)
        if status:
            where.append("status=?")
            params.append(status)
        return self._list("approvals", Approval, " AND ".join(where), tuple(params),
                          "valid_to DESC", int(limit))

    def delete_approval(self, approval_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM approvals WHERE approval_id=?",
                               (approval_id,))

    def valid_approval(self, site, bands=None, today=None):
        """当天有效、覆盖该考点（site='*' 视为通用）的审批；返回 (Approval|None, 原因)。"""
        today = today or models.today_str()
        rows = self.list_approvals(kind=config.APPR_APPROVAL, site=site)
        if not rows:
            return None, "未登记设台审批/报备（%s）" % (site or "未指定考点")
        valid = [a for a in rows if a.valid_today(today)]
        if not valid:
            return None, "审批已过期或状态非有效（最近一条：%s %s~%s）" % (
                rows[0].no or "-", rows[0].valid_from or "-", rows[0].valid_to or "-")
        want = set(bands or [])
        for a in valid:
            have = set(a.bands or [])
            if not want or not have or have & want:
                return a, ""
        return None, "审批频段未覆盖本次屏蔽计划（审批：%s）" % (
            "、".join(config.band_name(x) for x in (valid[0].bands or [])) or "-")

    # ========================================================== 外溢/屏蔽实测
    def save_spill(self, t):
        if not t.test_id:
            t.test_id = new_id("SPL")
        if not t.ts_epoch:
            t.ts_epoch = models.now_epoch()
        if not t.measured_at:
            t.measured_at = models.ts_text(t.ts_epoch)
        return self._save("spill_tests", t)

    def list_spills(self, jammer_id=None, since_epoch=None, limit=100):
        where, params = [], []
        if jammer_id:
            where.append("jammer_id=?")
            params.append(jammer_id)
        if since_epoch:
            where.append("ts_epoch>=?")
            params.append(float(since_epoch))
        return self._list("spill_tests", SpillTest, " AND ".join(where), tuple(params),
                          "ts_epoch DESC", int(limit))

    def save_shield_test(self, t):
        if not t.test_id:
            t.test_id = new_id("SHT")
        if not t.ts_epoch:
            t.ts_epoch = models.now_epoch()
        if not t.measured_at:
            t.measured_at = models.ts_text(t.ts_epoch)
        return self._save("shield_tests", t)

    def list_shield_tests(self, room_id=None, since_epoch=None, limit=200):
        where, params = [], []
        if room_id:
            where.append("room_id=?")
            params.append(room_id)
        if since_epoch:
            where.append("ts_epoch>=?")
            params.append(float(since_epoch))
        return self._list("shield_tests", ShieldTest, " AND ".join(where), tuple(params),
                          "ts_epoch DESC", int(limit))

    def shield_rate(self, since_epoch=None):
        rows = self.list_shield_tests(since_epoch=since_epoch, limit=10000)
        if not rows:
            return 0.0, 0
        return round(sum(float(r.rate or 0) for r in rows) / len(rows), 3), len(rows)

    # ============================================================== 场次
    def save_session(self, s):
        if not s.session_id:
            s.session_id = new_id("SES")
        if not s.created_at:
            s.created_at = models.now_ts()
        return self._save("sessions", s)

    def get_session(self, session_id):
        return self._obj(Session, self._one("SELECT * FROM sessions WHERE session_id=?",
                                           (session_id,)))

    def list_sessions(self, date=None, state=None, limit=200):
        where, params = [], []
        if date:
            where.append("date=?")
            params.append(date)
        if state:
            where.append("state=?")
            params.append(state)
        return self._list("sessions", Session, " AND ".join(where), tuple(params),
                          "date DESC, start ASC", int(limit))

    def delete_session(self, session_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    # ========================================================== 特征库
    def save_feature(self, f):
        return self._save("features", f)

    def get_feature(self, code):
        return self._obj(Feature, self._one(
            "SELECT * FROM features WHERE feature_code=?", (code,)))

    def list_features(self, enabled_only=False):
        where = "enabled=1" if enabled_only else ""
        return self._list("features", Feature, where, (), "feature_code ASC")

    def delete_feature(self, code):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM features WHERE feature_code=?", (code,))

    # ========================================================== 联动关联
    def save_correlation(self, c):
        if not c.corr_id:
            c.corr_id = new_id("COR")
        if not c.created_epoch:
            c.created_epoch = models.now_epoch()
        return self._save("correlations", c)

    def list_correlations(self, source=None, anomaly_id=None, limit=100):
        where, params = [], []
        if source:
            where.append("source=?")
            params.append(source)
        if anomaly_id:
            where.append("anomaly_id=?")
            params.append(anomaly_id)
        return self._list("correlations", Correlation, " AND ".join(where),
                          tuple(params), "created_epoch DESC", int(limit))

    # ============================================================== 定位
    def save_location(self, loc):
        if not loc.loc_id:
            loc.loc_id = new_id("LOC")
        if not loc.created_epoch:
            loc.created_epoch = models.now_epoch()
        return self._save("locations", loc)

    def list_locations(self, anomaly_id=None, limit=100):
        where, params = [], []
        if anomaly_id:
            where.append("anomaly_id=?")
            params.append(anomaly_id)
        return self._list("locations", Location, " AND ".join(where), tuple(params),
                          "created_epoch DESC", int(limit))

    # ============================================================ 回放测试
    def save_run(self, run):
        if not run.run_id:
            run.run_id = new_id("RUN")
        if not run.created_at:
            run.created_at = models.now_ts()
        return self._save("replay_runs", run)

    def list_runs(self, limit=50):
        # 按插入顺序（rowid）取最近一次：同一秒内创建的多条回放也能稳定取到最后一条
        return self._list("replay_runs", ReplayRun, order="rowid DESC",
                          limit=int(limit))

    def latest_run(self):
        rows = self.list_runs(limit=1)
        return rows[0] if rows else None

    def save_item(self, item):
        if not item.item_id:
            item.item_id = new_id("ITM")
        return self._save("replay_items", item)

    def list_items(self, run_id, limit=1000):
        return self._list("replay_items", ReplayItem, "run_id=?", (run_id,),
                          "rowid ASC", int(limit))

    # ============================================================== 审计
    def log(self, action, detail="", operator=""):
        """写一条哈希链审计：任何改动都会破坏后续链。"""
        with self._lock:
            row = self._one("SELECT hash FROM audits ORDER BY rowid DESC LIMIT 1")
            prev = (row["hash"] if row else "") or "GENESIS"
            ts = models.now_ts()
            epoch = models.now_epoch()
            body = "%s|%.3f|%s|%s|%s" % (prev, epoch, action, detail, operator)
            h = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self._save("audits", Audit(audit_id=new_id("AUD"), ts=ts, ts_epoch=epoch,
                                       action=action, detail=detail,
                                       operator=operator, prev_hash=prev, hash=h))
            return h

    def list_audits(self, limit=200):
        return self._list("audits", Audit, order="rowid DESC", limit=int(limit))

    def audit_count(self):
        return self._count("SELECT COUNT(*) FROM audits")

    def verify_chain(self):
        rows = self._query("SELECT * FROM audits ORDER BY rowid ASC")
        prev = "GENESIS"
        for i, r in enumerate(rows):
            body = "%s|%.3f|%s|%s|%s" % (prev, float(r["ts_epoch"] or 0), r["action"],
                                          r["detail"], r["operator"])
            h = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if h != r["hash"] or (r["prev_hash"] or "") != prev:
                return False, i + 1, "审计链在第 %d 条断裂（%s）" % (i + 1, r["action"])
            prev = h
        return True, len(rows), "审计链完整（%d 条）" % len(rows)

    # ============================================================== 汇总
    def summary(self, since_epoch=None):
        """给界面/接口/报告共用的一份概览（一次查询，字段名稳定）。"""
        cut = float(since_epoch) if since_epoch else models.now_epoch() - 86400
        devices = self.list_devices()
        by_kind = {k: 0 for k in config.DEV_KINDS}
        for d in devices:
            if d.kind in by_kind:
                by_kind[d.kind] += 1
        a = self.anomaly_stats(since_epoch=cut)
        on = self.on_states()
        rate, n_tests = self.shield_rate()
        return {
            "devices": len(devices),
            "monitors": by_kind.get(config.DEV_MONITOR, 0),
            "probes": by_kind.get(config.DEV_PROBE, 0),
            "jammers": by_kind.get(config.DEV_JAMMER, 0),
            "scans": self.scan_count(since_epoch=cut),
            "anomalies": a["total"],
            "open_anomalies": a["open"],
            "critical": a["critical"],
            "warn": a["warn"],
            "watch": a["watch"],
            "avg_latency": a["avg_latency"],
            "max_latency": a["max_latency"],
            "jammers_on": len(on),
            "shield_rate": rate,
            "shield_tests": n_tests,
            "sessions": self._count("SELECT COUNT(*) FROM sessions"),
            "sessions_today": self._count("SELECT COUNT(*) FROM sessions WHERE date=?",
                                          (models.today_str(),)),
            "switch_logs": self._count("SELECT COUNT(*) FROM switch_logs"),
            "approvals": self._count("SELECT COUNT(*) FROM approvals"),
            "features": self._count("SELECT COUNT(*) FROM features"),
            "correlations": self._count("SELECT COUNT(*) FROM correlations"),
            "replay_runs": self._count("SELECT COUNT(*) FROM replay_runs"),
            "audits": self.audit_count(),
        }

    def purge_runtime(self, keep_days=90):
        """清理过期运行数据（扫描帧/异常/开关记录），证据类（审批/实测）不动。"""
        cut = models.now_epoch() - float(keep_days) * 86400
        with self._lock, self._conn:
            n1 = self._conn.execute("DELETE FROM scans WHERE ts_epoch<?", (cut,)).rowcount
            n2 = self._conn.execute("DELETE FROM anomalies WHERE detected_epoch<?",
                                    (cut,)).rowcount
            n3 = self._conn.execute("DELETE FROM replay_items WHERE rowid IN "
                                    "(SELECT rowid FROM replay_items WHERE run_id IN "
                                    "(SELECT run_id FROM replay_runs WHERE created_at<?))",
                                    (models.ts_text(cut),)).rowcount
        return {"scans": n1, "anomalies": n2, "replay_items": n3}


def export_json(obj_list, path):
    """把一组 dict/对象导出成 JSON（证据导出用）。"""
    data = [o.to_dict() if hasattr(o, "to_dict") else o for o in (obj_list or [])]
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        return True
    except OSError:
        return False


def export_csv(rows, path, columns=None):
    """把一组 dict 导出成 CSV（UTF-8 BOM，Excel 直接双击不乱码）。"""
    import csv
    p = Path(path)
    rows = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in (rows or [])]
    cols = list(columns or (list(rows[0].keys()) if rows else []))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return True
    except OSError:
        return False
