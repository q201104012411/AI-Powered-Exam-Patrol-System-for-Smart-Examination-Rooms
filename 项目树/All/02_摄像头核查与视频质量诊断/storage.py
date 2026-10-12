# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 本地存储（SQLite）

devices     设备台账
checks      逐机位核查记录（含缺陷与实测指标）
workorders  整改工单（闭环）
baselines   基准图登记（逐机位）
audit       哈希链审计（巡检/工单/台账变更全部留痕）
kv          键值（巡检计划、会话信息等）
"""
import json
import sqlite3
import threading

import config
import data_crypto
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
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, room_id TEXT,
                site TEXT, position TEXT, url TEXT, resolution TEXT,
                fov_deg REAL, pan_deg REAL, tilt_deg REAL, baseline_at TEXT,
                last_heartbeat REAL DEFAULT 0, enabled INTEGER DEFAULT 1,
                has_audio INTEGER DEFAULT 0, session TEXT, extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dev_room ON devices(room_id);

            CREATE TABLE IF NOT EXISTS checks (
                check_id TEXT PRIMARY KEY, device_id TEXT, device_code TEXT, room_id TEXT,
                policy TEXT, ts TEXT, epoch REAL, online INTEGER, defects TEXT,
                metrics TEXT, frame_path TEXT, baseline_path TEXT, operator TEXT,
                note TEXT, ok INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ck_dev ON checks(device_id, epoch);
            CREATE INDEX IF NOT EXISTS idx_ck_policy ON checks(policy, epoch);

            CREATE TABLE IF NOT EXISTS workorders (
                order_id TEXT PRIMARY KEY, device_id TEXT, device_code TEXT, room_id TEXT,
                codes TEXT, severity TEXT, status TEXT, created_at TEXT,
                assigned_to TEXT, dispatched_at TEXT, rectified_at TEXT,
                reviewed_at TEXT, before_path TEXT, after_path TEXT, note TEXT,
                review_note TEXT, reviewer TEXT, operator TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wo_status ON workorders(status, created_at);

            CREATE TABLE IF NOT EXISTS baselines (
                device_id TEXT PRIMARY KEY, code TEXT, path TEXT, captured_at TEXT,
                pan_deg REAL, tilt_deg REAL, sharpness REAL, coverage REAL,
                operator TEXT, note TEXT
            );

            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT,
                detail TEXT, operator TEXT, prev_hash TEXT, hash TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """)

    # ---------------- 设备台账 ----------------
    def save_device(self, dev):
        row = dev.to_row()
        # 敏感字段加密存储（name/position 可能含考场具体位置信息）
        row["name"] = data_crypto.encrypt_field(row.get("name") or "")
        row["position"] = data_crypto.encrypt_field(row.get("position") or "")
        with self._lock, self._conn() as c:
            existing = c.execute("SELECT device_id FROM devices WHERE device_id=?",
                                 (dev.device_id,)).fetchone()
            if existing:
                sets = ",".join("%s=?" % k for k in row if k != "device_id")
                vals = [v for k, v in row.items() if k != "device_id"] + [dev.device_id]
                c.execute("UPDATE devices SET %s WHERE device_id=?" % sets, vals)
            else:
                cols = ",".join(row.keys())
                ph = ",".join("?" * len(row))
                c.execute("INSERT INTO devices (%s) VALUES (%s)" % (cols, ph),
                          list(row.values()))

    def _dec_device(self, r):
        if r is None:
            return None
        d = dict(r)
        d["name"] = data_crypto.decrypt_field(d.get("name") or "")
        d["position"] = data_crypto.decrypt_field(d.get("position") or "")
        return models.Device.from_row(d)

    def get_device(self, device_id=None, code=None):
        sql, arg = ("SELECT * FROM devices WHERE device_id=?", device_id) if device_id \
            else ("SELECT * FROM devices WHERE code=?", code)
        with self._lock, self._conn() as c:
            r = c.execute(sql, (arg,)).fetchone()
        return self._dec_device(r)

    def list_devices(self, room_id=None, site=None, session=None, enabled_only=True):
        sql = "SELECT * FROM devices WHERE 1=1"
        args = []
        if enabled_only:
            sql += " AND enabled=1"
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if site:
            sql += " AND site=?"
            args.append(site)
        if session:
            sql += " AND session=?"
            args.append(session)
        sql += " ORDER BY room_id, code"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._dec_device(r) for r in rows]

    def rooms(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT DISTINCT room_id FROM devices WHERE room_id<>'' "
                             "ORDER BY room_id").fetchall()
        return [r["room_id"] for r in rows]

    def delete_device(self, device_id):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
            c.execute("DELETE FROM baselines WHERE device_id=?", (device_id,))

    def heartbeat(self, device_id, epoch=None):
        with self._lock, self._conn() as c:
            c.execute("UPDATE devices SET last_heartbeat=? WHERE device_id=?",
                      (float(epoch if epoch is not None else models.now_epoch()), device_id))

    def set_baseline_at(self, device_id, ts):
        """
        只更新基准图时间。
        注意：不要在这里整行 save_device——调用方手上的设备对象可能是旧快照，
        整行覆盖会把刚写入的 last_heartbeat 抹回 0（曾导致"刚心跳过却被判离线"）。
        """
        with self._lock, self._conn() as c:
            c.execute("UPDATE devices SET baseline_at=? WHERE device_id=?",
                      (ts, device_id))

    # ---------------- 核查记录 ----------------
    def save_check(self, ck):
        row = ck.to_row()
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO checks (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def list_checks(self, policy=None, room_id=None, device_id=None, since_epoch=None,
                    only_bad=False, limit=1000):
        sql = "SELECT * FROM checks WHERE 1=1"
        args = []
        for k, v in (("policy", policy), ("room_id", room_id), ("device_id", device_id)):
            if v:
                sql += " AND %s=?" % k
                args.append(v)
        if since_epoch:
            sql += " AND epoch>=?"
            args.append(float(since_epoch))
        if only_bad:
            sql += " AND ok=0"
        sql += " ORDER BY epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [models.CheckResult.from_row(r) for r in rows]

    def latest_check(self, device_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM checks WHERE device_id=? "
                          "ORDER BY epoch DESC LIMIT 1", (device_id,)).fetchone()
        return models.CheckResult.from_row(r) if r else None

    def online_rate(self, since_epoch=None, policy=None):
        """在线率（考试期间关键指标）"""
        sql = "SELECT COUNT(*) n, SUM(online) ok FROM checks WHERE 1=1"
        args = []
        if since_epoch:
            sql += " AND epoch>=?"
            args.append(float(since_epoch))
        if policy:
            sql += " AND policy=?"
            args.append(policy)
        with self._lock, self._conn() as c:
            r = c.execute(sql, args).fetchone()
        n = r["n"] or 0
        return {"samples": n, "online": r["ok"] or 0,
                "rate": round((r["ok"] or 0) / n, 4) if n else None}

    def defect_stats(self, since_epoch=None):
        """按缺陷码汇总（用于报告与整改统计）"""
        rows = self.list_checks(since_epoch=since_epoch, limit=10 ** 6)
        agg = {}
        for ck in rows:
            for d in ck.defects:
                it = agg.setdefault(d.code, {"code": d.code, "name": config.defect_name(d.code),
                                             "severity": d.severity, "count": 0,
                                             "devices": set()})
                it["count"] += 1
                it["devices"].add(ck.device_code)
        out = []
        for it in agg.values():
            it["devices"] = sorted(it["devices"])
            it["device_count"] = len(it["devices"])
            out.append(it)
        out.sort(key=lambda x: -x["count"])
        return out

    # ---------------- 工单 ----------------
    def save_order(self, wo):
        row = wo.to_row()
        # 敏感/自由文本字段加密
        row["note"] = data_crypto.encrypt_field(row.get("note") or "")
        row["review_note"] = data_crypto.encrypt_field(row.get("review_note") or "")
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO workorders (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def _dec_order(self, r):
        if r is None:
            return None
        d = dict(r)
        d["note"] = data_crypto.decrypt_field(d.get("note") or "")
        d["review_note"] = data_crypto.decrypt_field(d.get("review_note") or "")
        return models.WorkOrder.from_row(d)

    def get_order(self, order_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM workorders WHERE order_id=?",
                          (order_id,)).fetchone()
        return self._dec_order(r)

    def list_orders(self, status=None, room_id=None, limit=500):
        sql = "SELECT * FROM workorders WHERE 1=1"
        args = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._dec_order(r) for r in rows]

    def open_order_for(self, device_id, codes):
        """同设备同缺陷是否已有未关闭工单（避免重复派单）"""
        want = set(codes)
        for wo in self.list_orders(limit=200):
            if wo.device_id == device_id and wo.status != "closed" \
                    and want & set(wo.codes):
                return wo
        return None

    def workorder_summary(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM workorders "
                             "GROUP BY status").fetchall()
        out = {r["status"]: r["n"] for r in rows}
        total = sum(out.values())
        closed = out.get("closed", 0)
        return {"by_status": out, "total": total, "closed": closed,
                "close_rate": round(closed / total, 4) if total else None}

    # ---------------- 基准图 ----------------
    def save_baseline(self, rec):
        cols = ",".join(rec.keys())
        ph = ",".join("?" * len(rec))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO baselines (%s) VALUES (%s)" % (cols, ph),
                      list(rec.values()))

    def get_baseline(self, device_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM baselines WHERE device_id=?",
                          (device_id,)).fetchone()
        return dict(r) if r else None

    def list_baselines(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM baselines ORDER BY code").fetchall()
        return [dict(r) for r in rows]

    # ---------------- 审计链 ----------------
    def log(self, action, detail="", operator=""):
        with self._lock, self._conn() as c:
            last = c.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "GENESIS"
            ts = models.now_ts()
            payload = {"ts": ts, "action": action, "detail": detail, "operator": operator}
            h = models.chain_hash(prev, payload)
            c.execute("INSERT INTO audit (ts, action, detail, operator, prev_hash, hash) "
                      "VALUES (?,?,?,?,?,?)", (ts, action, detail, operator, prev, h))
        return h

    def audit_rows(self, limit=300):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY seq ASC").fetchall()
        prev = "GENESIS"
        for i, r in enumerate(rows, 1):
            payload = {"ts": r["ts"], "action": r["action"], "detail": r["detail"],
                       "operator": r["operator"]}
            if r["prev_hash"] != prev:
                return False, "第 %d 条 prev_hash 不匹配" % i, i
            if models.chain_hash(prev, payload) != r["hash"]:
                return False, "第 %d 条 hash 被篡改" % i, i
            prev = r["hash"]
        return True, "审计链完整（%d 条）" % len(rows), len(rows)

    # ---------------- 键值 ----------------
    def set_kv(self, k, v):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)",
                      (k, json.dumps(v, ensure_ascii=False)))

    def get_kv(self, k, default=None):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except (ValueError, TypeError):
            return default
