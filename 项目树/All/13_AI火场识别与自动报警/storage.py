# -*- coding: utf-8 -*-
r"""
火警事件存储（SQLite）

一张表 + 一份审计日志：
    events  火警事件（含峰值、证据路径、确认/解除留痕）
    audit   追加式操作日志（谁在什么时候确认/标记误报/清理，不可改历史）

为什么不用哈希链：本模块是**值班设备**，不是取证系统。审计只需"可回溯、不可篡改历史"，
     为此 audit 表只允许 INSERT（不提供任何 UPDATE/DELETE 接口）。
     需要哈希链审计的是 03_智能终端操作 那套告警复核体系。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import config
import models

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    camera_name   TEXT,
    camera_url    TEXT,
    level         TEXT,
    score         REAL,
    created_at    TEXT,
    created_epoch REAL,
    peak_score    REAL,
    peak_epoch    REAL,
    frames_hit    INTEGER,
    reason        TEXT,
    parts         TEXT,
    fire_ratio    REAL,
    smoke_ratio   REAL,
    regions       TEXT,
    snapshot      TEXT,
    marked        TEXT,
    clip          TEXT,
    status        TEXT,
    acked_by      TEXT,
    acked_at      TEXT,
    acked_note    TEXT,
    cleared_at    TEXT,
    duration_sec  REAL
);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(created_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS audit (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    action   TEXT,
    detail   TEXT,
    operator TEXT
);
"""


class EventStore(object):
    """火警事件与审计日志。线程安全（GUI 线程与采集线程都会写）。"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._lock = threading.RLock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return c

    def _init(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)

    # ---------------- 事件 ----------------
    #: 证据字段：只允许"从空变有"，不允许被"空"覆盖。
    #: 为什么：证据由另一个线程写盘后回填，而主线程会整行保存事件。
    #: 如果整行保存时它手上的对象还没有路径，就会把已经写好的路径抹成空
    #: （实测踩过：截图文件明明在磁盘上，库里却是空）。
    EVIDENCE_COLS = ("snapshot", "marked", "clip")

    def save_event(self, ev):
        """
        插入或更新一条事件（同 event_id 覆盖）。
        **证据路径受保护**：本次值为空而库里已有值时，保留库里的（见 EVIDENCE_COLS）。
        """
        if ev is None:
            return False
        row = ev.to_row()
        with self._lock, self._conn() as c:
            old = c.execute("SELECT snapshot, marked, clip FROM events WHERE event_id=?",
                            (row["event_id"],)).fetchone()
            if old is not None:
                for col in self.EVIDENCE_COLS:
                    if not row.get(col) and old[col]:
                        row[col] = old[col]      # 保护已有证据，不被空值覆盖
                        setattr(ev, col, old[col])   # 顺手回填内存对象，避免它一直是"旧的空值"
            cols = ",".join(row.keys())
            ph = ",".join("?" * len(row))
            c.execute("INSERT OR REPLACE INTO events (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))
        return True

    def get_event(self, event_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM events WHERE event_id=?",
                          (event_id,)).fetchone()
        return models.FireEvent.from_row(r) if r else None

    def list_events(self, limit=200, status=None, level=None, since_epoch=None):
        sql = "SELECT * FROM events WHERE 1=1"
        args = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if level:
            sql += " AND level=?"
            args.append(level)
        if since_epoch:
            sql += " AND created_epoch>=?"
            args.append(float(since_epoch))
        sql += " ORDER BY created_epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [models.FireEvent.from_row(r) for r in rows]

    def active_events(self, limit=20):
        """还在报警/已确认但未解除的事件（界面要一直提示）。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE status IN (?,?) "
                "ORDER BY created_epoch DESC LIMIT ?",
                (models.ST_FIRING, models.ST_ACKED, int(limit))).fetchall()
        return [models.FireEvent.from_row(r) for r in rows]

    def set_evidence(self, event_id, snapshot=None, marked=None, clip=None):
        """
        只更新证据路径字段。

        为什么必须有这个"窄写入"接口：证据是**另一个线程**写盘后回填的，
        而主线程（检测回调/演练收尾）会整行 save_event。两边都用整行写入就会互相覆盖 ——
        实测出现过"截图文件明明已落盘，库里却还是空路径"。
        这里改成只 UPDATE 路径列，竞态窗口就没了。
        """
        sets, args = [], []
        for col, val in (("snapshot", snapshot), ("marked", marked), ("clip", clip)):
            if val:
                sets.append("%s=?" % col)
                args.append(val)
        if not sets:
            return False
        args.append(event_id)
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE events SET %s WHERE event_id=?" % ",".join(sets),
                            args)
            return cur.rowcount > 0

    def ack_event(self, event_id, by="值班员", note=""):
        ev = self.get_event(event_id)
        if ev is None:
            return False, "事件不存在"
        if ev.status == models.ST_CLEARED:
            return False, "该事件已解除，无需确认"
        ev.ack(by=by, note=note)
        self.save_event(ev)
        self.log("火警确认", "%s %s" % (event_id, ev.level_name()), by)
        return True, "已确认（%s）" % ev.acked_at

    def mark_false(self, event_id, by="值班员", note=""):
        ev = self.get_event(event_id)
        if ev is None:
            return False, "事件不存在"
        ev.mark_false(by=by, note=note)
        self.save_event(ev)
        self.log("标记误报", "%s %s" % (event_id, note or "人工判定"), by)
        return True, "已标记误报（可用于后续调参）"

    # ---------------- 统计 ----------------
    def summary(self):
        out = {"total": 0, "alarm": 0, "warning": 0, "firing": 0, "acked": 0,
               "cleared": 0, "false_alarm": 0, "last_epoch": 0.0}
        with self._lock, self._conn() as c:
            for r in c.execute("SELECT level, status, COUNT(*) n, MAX(created_epoch) last "
                               "FROM events GROUP BY level, status"):
                n = int(r["n"])
                out["total"] += n
                if r["level"] in out:
                    out[r["level"]] += n
                if r["status"] in out:
                    out[r["status"]] += n
                out["last_epoch"] = max(out["last_epoch"], r["last"] or 0.0)
        return out

    def count(self):
        with self._lock, self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # ---------------- 审计（只增不改） ----------------
    def log(self, action, detail="", operator="system"):
        try:
            with self._lock, self._conn() as c:
                c.execute("INSERT INTO audit (ts, action, detail, operator) VALUES (?,?,?,?)",
                          (models.now_ts(), action, detail, operator))
        except sqlite3.Error:
            pass

    def audit_rows(self, limit=200):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 清理 ----------------
    def purge_old(self, keep_days=None, delete_files=True):
        """
        按保留天数清理旧事件与证据文件。keep_days<=0 表示不清理。
        返回 (删除事件数, 删除文件数, 释放字节)。
        """
        days = int(config.load_settings()["keep_days"] if keep_days is None else keep_days)
        if days <= 0:
            return 0, 0, 0
        cutoff = time.time() - days * 86400.0
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT event_id, snapshot, marked, clip FROM events "
                             "WHERE created_epoch < ?", (cutoff,)).fetchall()
        n_files = 0
        freed = 0
        for r in rows:
            if delete_files:
                for p in (r["snapshot"], r["marked"], r["clip"]):
                    if not p:
                        continue
                    try:
                        if os.path.isfile(p):
                            freed += os.path.getsize(p)
                            os.remove(p)
                            n_files += 1
                    except OSError:
                        pass
                # 事件子目录空了就删掉
                for p in (r["snapshot"], r["marked"], r["clip"]):
                    if not p:
                        continue
                    d = os.path.dirname(p)
                    try:
                        if os.path.isdir(d) and not os.listdir(d):
                            os.rmdir(d)
                    except OSError:
                        pass
        if rows:
            with self._lock, self._conn() as c:
                c.executemany("DELETE FROM events WHERE event_id=?",
                              [(r["event_id"],) for r in rows])
            self.log("清理旧事件", "删除 %d 条（保留 %d 天），文件 %d 个，释放 %.1f MB"
                     % (len(rows), days, n_files, freed / 1048576.0))
        return len(rows), n_files, freed


def event_dir(event_id):
    """某个事件专属的证据目录（抓拍/标注图/录像都放这里，便于整体拷贝）。"""
    d = os.path.join(str(config.EVENT_DIR), str(event_id))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d
