# -*- coding: utf-8 -*-
"""
报名库唯一数据源（SQLite），人脸端与网页端共用，解决旧版
"GUI 读 candidates.json、网页写 exam.db" 的双数据源问题。

仅依赖标准库 + models/pii/config，网页端也能安全导入（无需 numpy/cv2）。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import data_crypto
import pii
from models import Candidate

SCHEMA_VERSION = "2"

CANDIDATE_COLUMNS = [
    "candidate_id", "name", "id_card_salt", "id_card_hash", "id_card_masked",
    "ticket_no", "seat_no", "reference_photo",
    "consent_status", "consent_at", "retention_until", "updated_at", "updated_by",
]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path=None):
    path = str(db_path or config.DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")   # 允许 GUI 与网页端并发访问
    except sqlite3.Error:
        pass
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def init_schema(db_path=None):
    """建表 + 自动迁移（旧明文 id_card -> salt/hash/掩码）。可重复调用。"""
    conn = connect(db_path)
    try:
        conn.isolation_level = None          # 自动提交，便于迁移后执行 VACUUM
        conn.execute("PRAGMA secure_delete = ON")   # 删除的数据页填零，避免明文残留
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                id_card_salt TEXT NOT NULL DEFAULT '',
                id_card_hash TEXT NOT NULL DEFAULT '',
                id_card_masked TEXT NOT NULL DEFAULT '',
                ticket_no TEXT NOT NULL DEFAULT '',
                seat_no TEXT NOT NULL DEFAULT '',
                reference_photo TEXT NOT NULL DEFAULT '',
                consent_status TEXT NOT NULL DEFAULT '未登记',
                consent_at TEXT NOT NULL DEFAULT '',
                retention_until TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT ''
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS data_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                operator TEXT NOT NULL,
                action TEXT NOT NULL,
                candidate_id TEXT,
                detail TEXT)""")

        cols = _columns(conn, "candidates")
        if "id_card" in cols and "id_card_hash" not in cols:
            _migrate_plaintext_id_card(conn)
            _scrub_plaintext(conn)
        elif "id_card" in cols and "id_card_hash" in cols:
            # 目标结构已具备，仅移除残留明文列
            _drop_legacy_id_card_column(conn)
            _scrub_plaintext(conn)

        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                     (SCHEMA_VERSION,))
    finally:
        conn.close()
    return True


def _scrub_plaintext(conn):
    """
    把已删除明文列的物理残留清掉。
    仅 DROP COLUMN 是不够的：SQLite 会把旧数据留在空闲页里，文件里仍能搜到明文，
    必须 VACUUM 重建文件 + 截断 WAL。这是"身份证号不落明文"能否成立的关键一步。
    """
    try:
        conn.execute("VACUUM")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def _migrate_plaintext_id_card(conn):
    """旧库迁移：把明文身份证号转换为 salt/hash/掩码，并删除明文列。"""
    rows = conn.execute("SELECT * FROM candidates").fetchall()
    migrated = []
    for r in rows:
        plain = (r["id_card"] if "id_card" in r.keys() else "") or ""
        salt, h, masked = (pii.register_id_card(plain) if plain else ("", "", ""))
        migrated.append((
            r["candidate_id"], r["name"] if "name" in r.keys() else "",
            salt, h, masked,
            r["ticket_no"] if "ticket_no" in r.keys() else "",
            r["seat_no"] if "seat_no" in r.keys() else "",
            r["reference_photo"] if "reference_photo" in r.keys() else "",
            "未登记", "", "",
            r["updated_at"] if "updated_at" in r.keys() else _now(),
            r["updated_by"] if "updated_by" in r.keys() else "migration",
        ))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE candidates RENAME TO candidates_old")
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                id_card_salt TEXT NOT NULL DEFAULT '',
                id_card_hash TEXT NOT NULL DEFAULT '',
                id_card_masked TEXT NOT NULL DEFAULT '',
                ticket_no TEXT NOT NULL DEFAULT '',
                seat_no TEXT NOT NULL DEFAULT '',
                reference_photo TEXT NOT NULL DEFAULT '',
                consent_status TEXT NOT NULL DEFAULT '未登记',
                consent_at TEXT NOT NULL DEFAULT '',
                retention_until TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT ''
            )""")
        conn.executemany("""
            INSERT INTO candidates (candidate_id, name, id_card_salt, id_card_hash, id_card_masked,
                ticket_no, seat_no, reference_photo, consent_status, consent_at, retention_until,
                updated_at, updated_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", migrated)
        conn.execute("DROP TABLE candidates_old")
        conn.execute("INSERT INTO data_change_log(ts, operator, action, detail) VALUES(?,?,?,?)",
                     (_now(), "migration", "MIGRATE_ID_CARD",
                      "已将 %d 条明文身份证号迁移为加盐哈希 + 掩码" % len(migrated)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _drop_legacy_id_card_column(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE candidates RENAME TO candidates_old")
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                id_card_salt TEXT NOT NULL DEFAULT '',
                id_card_hash TEXT NOT NULL DEFAULT '',
                id_card_masked TEXT NOT NULL DEFAULT '',
                ticket_no TEXT NOT NULL DEFAULT '',
                seat_no TEXT NOT NULL DEFAULT '',
                reference_photo TEXT NOT NULL DEFAULT '',
                consent_status TEXT NOT NULL DEFAULT '未登记',
                consent_at TEXT NOT NULL DEFAULT '',
                retention_until TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT ''
            )""")
        cols = ", ".join(CANDIDATE_COLUMNS)
        conn.execute("INSERT INTO candidates (%s) SELECT %s FROM candidates_old" % (cols, cols))
        conn.execute("DROP TABLE candidates_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def migrate_legacy_json(json_path=None, operator="migration"):
    """把旧的 data/candidates.json（含明文身份证号）导入 SQLite。返回导入条数。"""
    path = str(json_path or config.LEGACY_JSON_PATH)
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items = []
    for r in raw:
        items.append({
            "candidate_id": r.get("candidate_id", ""),
            "name": r.get("name", ""),
            "ticket_no": r.get("ticket_no", ""),
            "seat_no": r.get("seat_no", ""),
            "reference_photo": r.get("reference_photo", ""),
            "id_card": r.get("id_card", ""),     # 明文，仅用于本次迁移
        })
    n = CandidateStore().upsert_many(items, operator=operator)
    return n


class CandidateStore:
    """报名库访问对象"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        init_schema(self.db_path)

    # ---------- 读 ----------
    def load(self) -> Dict[str, Candidate]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM candidates ORDER BY candidate_id").fetchall()
        finally:
            conn.close()
        out = {}
        for r in rows:
            keys = r.keys()
            out[r["candidate_id"]] = Candidate(
                candidate_id=r["candidate_id"],
                name=data_crypto.decrypt_field(r["name"] or ""),
                ticket_no=data_crypto.decrypt_field(r["ticket_no"] or ""),
                seat_no=data_crypto.decrypt_field(r["seat_no"] or ""),
                reference_photo=data_crypto.decrypt_field(r["reference_photo"] or ""),
                id_card_salt=r["id_card_salt"] or "",
                id_card_hash=r["id_card_hash"] or "",
                id_card_masked=r["id_card_masked"] or "",
                consent_status=r["consent_status"] or "未登记",
                consent_at=r["consent_at"] or "",
                retention_until=r["retention_until"] or "",
                updated_at=r["updated_at"] or "",
                updated_by=r["updated_by"] or "",
            )
        return out

    def count(self) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()["c"]
        finally:
            conn.close()

    # ---------- 写 ----------
    def upsert_many(self, items: List[dict], operator: str = "unknown") -> int:
        """
        事务化 UPSERT（替代旧的"全表 DELETE + INSERT"）：
        - item 含非空 id_card -> 重新计算 salt/hash/掩码；
        - item 的 id_card 为空 -> 保留库中已有身份证信息（未修改）；
        - 逐条校验，非法数据整批回滚，避免出现"删了旧库、新库没写进去"。
        """
        now = _now()
        conn = connect(self.db_path)
        try:
            conn.isolation_level = None          # 手动管理事务，保证整批原子性
            conn.execute("BEGIN IMMEDIATE")
            existing = {r["candidate_id"]: r for r in
                        conn.execute("SELECT candidate_id, id_card_salt, id_card_hash, id_card_masked, "
                                     "consent_status, consent_at, retention_until "
                                     "FROM candidates").fetchall()}
            seen = set()
            n = 0
            for it in items:
                cid = str(it.get("candidate_id", "")).strip()
                if not cid:
                    raise ValueError("存在空的考生编号")
                if cid in seen:
                    raise ValueError("考生编号重复：%s" % cid)
                seen.add(cid)

                plain = (it.get("id_card") or "").strip()
                if plain:
                    ok, msg = pii.validate_id_card(plain)
                    if not ok:
                        raise ValueError("%s：%s" % (cid, msg))
                    salt, h, masked = pii.register_id_card(plain)
                else:
                    old = existing.get(cid)
                    salt = old["id_card_salt"] if old else ""
                    h = old["id_card_hash"] if old else ""
                    masked = old["id_card_masked"] if old else ""

                old = existing.get(cid)
                consent = str(it.get("consent_status") or "").strip()
                if not consent:
                    # 未指定则保留原同意状态，避免编辑其他字段时把同意记录清掉
                    consent = (old["consent_status"] if old else "") or "未登记"
                consent_at = str(it.get("consent_at") or "").strip() or (
                    (old["consent_at"] if old else "") or (now if consent == "已同意" else ""))
                retention = str(it.get("retention_until") or "").strip() or (
                    (old["retention_until"] if old else "") or self._default_retention(now))
                # 敏感字段加密存储（兼容旧明文：encrypt_field 遇到已加密字符串会跳过）
                enc_name = data_crypto.encrypt_field(str(it.get("name", "")).strip())
                enc_ticket = data_crypto.encrypt_field(str(it.get("ticket_no", "")).strip())
                enc_seat = data_crypto.encrypt_field(str(it.get("seat_no", "")).strip())
                enc_photo = data_crypto.encrypt_field(str(it.get("reference_photo", "")).strip())
                conn.execute("""
                    INSERT INTO candidates (candidate_id, name, id_card_salt, id_card_hash,
                        id_card_masked, ticket_no, seat_no, reference_photo,
                        consent_status, consent_at, retention_until, updated_at, updated_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        name=excluded.name,
                        id_card_salt=excluded.id_card_salt,
                        id_card_hash=excluded.id_card_hash,
                        id_card_masked=excluded.id_card_masked,
                        ticket_no=excluded.ticket_no,
                        seat_no=excluded.seat_no,
                        reference_photo=excluded.reference_photo,
                        consent_status=excluded.consent_status,
                        consent_at=excluded.consent_at,
                        retention_until=excluded.retention_until,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by
                """, (
                    cid,
                    enc_name,
                    salt, h, masked,
                    enc_ticket,
                    enc_seat,
                    enc_photo,
                    consent or "未登记",
                    consent_at,
                    retention,
                    now, operator,
                ))
                n += 1
            conn.execute("INSERT INTO data_change_log(ts, operator, action, detail) VALUES(?,?,?,?)",
                         (now, operator, "UPSERT_CANDIDATES", "写入/更新 %d 条考生数据" % n))
            conn.commit()
            return n
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _default_retention(now_iso):
        d = datetime.fromisoformat(now_iso) + timedelta(days=config.RETENTION_DAYS)
        return d.date().isoformat()

    def set_consent(self, candidate_id: str, status: str, operator: str = "unknown") -> bool:
        """登记单独同意状态（合规硬约束：未登记不得采集核验）。"""
        now = _now()
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "UPDATE candidates SET consent_status=?, consent_at=?, updated_at=?, updated_by=? "
                "WHERE candidate_id=?",
                (status, now if status == "已同意" else "", now, operator, candidate_id))
            conn.execute("INSERT INTO data_change_log(ts, operator, action, candidate_id, detail) "
                         "VALUES(?,?,?,?,?)",
                         (now, operator, "SET_CONSENT", candidate_id, "同意状态=%s" % status))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def log_change(self, action, candidate_id=None, detail="", operator="unknown"):
        conn = connect(self.db_path)
        try:
            conn.execute("INSERT INTO data_change_log(ts, operator, action, candidate_id, detail) "
                         "VALUES(?,?,?,?,?)", (_now(), operator, action, candidate_id, detail))
            conn.commit()
        finally:
            conn.close()
