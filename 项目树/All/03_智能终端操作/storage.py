# -*- coding: utf-8 -*-
"""
智能终端操作端 · 本地存储（SQLite）

一张库表管四件事：
  alerts     告警（含复核结果、同步状态）
  reviews    复核留痕（独立成表，便于审计与误报闭环统计）
  audit      哈希链审计日志（告警接入 / 确认 / 复核 / 管控指令 全部留痕）
  commands   统一管控指令队列（远程配置、白名单、远程擦除）
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
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY, code TEXT, level TEXT, title TEXT, detail TEXT,
                room_id TEXT, seat_no TEXT, candidate_id TEXT, candidate_name TEXT,
                device_id TEXT, source TEXT, created_at TEXT, created_epoch REAL,
                status TEXT, acked_by TEXT, acked_at TEXT,
                review_conclusion TEXT, review_reason TEXT, review_by TEXT, review_at TEXT,
                attachments TEXT, synced INTEGER DEFAULT 0, queued_at TEXT,
                false_positive INTEGER DEFAULT 0, sla_deadline REAL, extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_room ON alerts(room_id, status);
            CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_epoch);

            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY, alert_id TEXT, conclusion TEXT, reason TEXT,
                operator TEXT, witness TEXT, attachments TEXT, created_at TEXT,
                closed_loop INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT,
                detail TEXT, operator TEXT, prev_hash TEXT, hash TEXT
            );

            CREATE TABLE IF NOT EXISTS commands (
                cmd_id TEXT PRIMARY KEY, kind TEXT, payload TEXT, created_at TEXT,
                applied INTEGER DEFAULT 0, applied_at TEXT, operator TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY, v TEXT
            );
            """)

    # ---------------- 告警 ----------------
    def save_alert(self, alert):
        row = alert.to_row()
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO alerts (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def get_alert(self, alert_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return models.Alert.from_row(r) if r else None

    def list_alerts(self, room_id=None, level=None, status=None, limit=500):
        sql = "SELECT * FROM alerts WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        if level:
            sql += " AND level=?"
            args.append(level)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [models.Alert.from_row(r) for r in rows]

    def pending_unsynced(self, limit=50):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE synced=0 "
                             "ORDER BY created_epoch ASC LIMIT ?", (int(limit),)).fetchall()
        return [models.Alert.from_row(r) for r in rows]

    def mark_synced(self, alert_id):
        with self._lock, self._conn() as c:
            c.execute("UPDATE alerts SET synced=1 WHERE alert_id=?", (alert_id,))

    # ---------------- 复核 ----------------
    def save_review(self, rec):
        row = rec.to_row()
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reviews (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def list_reviews(self, limit=500):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM reviews ORDER BY created_at DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_review(self, review_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if r is None:
            return None
        import models
        return models.ReviewRecord.from_row(dict(r))

    def false_positive_stats(self):
        """误报闭环统计：按告警码汇总，供算法迭代使用"""
        with self._lock, self._conn() as c:
            rows = c.execute("""
                SELECT a.code AS code, COUNT(*) AS total,
                       SUM(CASE WHEN r.closed_loop=1 THEN 1 ELSE 0 END) AS fp
                FROM reviews r JOIN alerts a ON a.alert_id = r.alert_id
                GROUP BY a.code ORDER BY total DESC""").fetchall()
        out = []
        for r in rows:
            total = r["total"] or 0
            fp = r["fp"] or 0
            out.append({"code": r["code"], "total": total, "false_positive": fp,
                        "fp_rate": round(fp / total, 4) if total else 0.0})
        return out

    # ---------------- 哈希链审计 ----------------
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

    def audit_rows(self, limit=200):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self):
        """校验审计链完整性；返回 (ok, 说明, 校验条数)"""
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

    # ---------------- 统一管控指令 ----------------
    def push_command(self, kind, payload=None, operator="platform"):
        cmd_id = models.new_id("CMD")
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO commands (cmd_id, kind, payload, created_at, operator) "
                      "VALUES (?,?,?,?,?)",
                      (cmd_id, kind, json.dumps(payload or {}, ensure_ascii=False),
                       models.now_ts(), operator))
        return cmd_id

    def pending_commands(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM commands WHERE applied=0 "
                             "ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def mark_command_applied(self, cmd_id):
        with self._lock, self._conn() as c:
            c.execute("UPDATE commands SET applied=1, applied_at=? WHERE cmd_id=?",
                      (models.now_ts(), cmd_id))

    # ---------------- 键值（终端配置快照） ----------------
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

    # ---------------- 统计 ----------------
    def summary(self, room_id=None):
        sql = "SELECT level, status, COUNT(*) n FROM alerts"
        args = []
        if room_id:
            sql += " WHERE room_id=?"
            args.append(room_id)
        sql += " GROUP BY level, status"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
            total = c.execute("SELECT COUNT(*) n FROM alerts" +
                              (" WHERE room_id=?" if room_id else ""), args).fetchone()["n"]
            reviewed = c.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
            unsynced = c.execute("SELECT COUNT(*) n FROM alerts WHERE synced=0").fetchone()["n"]
        by_level = {lv: 0 for lv in config.LEVELS}
        by_status = {}
        for r in rows:
            by_level[r["level"]] = by_level.get(r["level"], 0) + r["n"]
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        return {"total": total, "by_level": by_level, "by_status": by_status,
                "reviews": reviewed, "unsynced": unsynced}
