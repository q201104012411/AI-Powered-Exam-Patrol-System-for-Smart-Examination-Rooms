# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 台账（SQLite）

一张库 11 张表，覆盖"人-措施-通道-事件-算法-演练"闭环：

  sites            考点（配置率的分母）
  channels         通道台账（智能/人工/无障碍三类，含净宽、低位终端、语音文字提示）
  profiles         特殊考生无障碍档案（需求、单独同意、证明）
  accommodations   保障措施逐项发放留痕（申请->审批->落实）
  alt_verifies     替代核验记录（人工核验 + 双证比对 + 双人见证，与智能核验同等效力）
  tech_blocks      因技术原因入场受阻事件（指标要求 0 起，必须能追责）
  fairness_runs    算法公平性分层测试（每次上线/换阈值一条）
  fairness_groups  分层结果（维度 x 分组 的通过率与差异）
  drills           演练记录（每考期 ≥1 次）
  audits           哈希链审计（改一条就断链，可自证完整）
  outbox           联动外发队列（03 智能终端不可达时事件不丢）

设计原则：
  1) **绝不因统计口径写坏数据**：所有写入先过一遍字段白名单。
  2) **敏感字段加密 + 盲索引**：candidate_id / 证明编号 / 联系方式加密落库，
     `candidate_key` 存不可逆指纹用于等值查询（见 data_crypto）。
  3) 审计是哈希链（prev_hash -> this_hash），任何"事后解释"都要先解释断链。
"""
import hashlib
import os
import sqlite3
import threading
import time

import config
import data_crypto
import models

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites(
  site_id TEXT PRIMARY KEY, name TEXT UNIQUE, address TEXT, gate_total INTEGER,
  note TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS channels(
  channel_id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, site TEXT, kind TEXT,
  location TEXT, width_mm INTEGER, turn_radius_mm INTEGER, low_counter INTEGER,
  counter_height_mm INTEGER, height_adjustable INTEGER, voice_prompt INTEGER,
  text_prompt INTEGER, vibration INTEGER, sign_service INTEGER, status TEXT,
  devices TEXT, last_check_at TEXT, last_check_epoch REAL, note TEXT,
  created_at TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ix_ch_site ON channels(site, kind);
CREATE INDEX IF NOT EXISTS ix_ch_status ON channels(status);

CREATE TABLE IF NOT EXISTS profiles(
  profile_id TEXT PRIMARY KEY, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, ticket_no TEXT, room_id TEXT, seat_no TEXT, site TEXT,
  needs TEXT, need_detail TEXT, measures TEXT, channel_hint TEXT, cert_no TEXT,
  cert_verified INTEGER, contact TEXT, consent INTEGER, consent_scope TEXT,
  consent_at TEXT, valid_from TEXT, valid_to TEXT, status TEXT, source TEXT,
  operator TEXT, note TEXT, created_at TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_pf_key ON profiles(candidate_key);
CREATE INDEX IF NOT EXISTS ix_pf_site ON profiles(site, status);

CREATE TABLE IF NOT EXISTS accommodations(
  acc_id TEXT PRIMARY KEY, profile_id TEXT, candidate_key TEXT, measure TEXT,
  status TEXT, owner TEXT, place TEXT, provided_at TEXT, provided_epoch REAL,
  operator TEXT, note TEXT, ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_acc_pf ON accommodations(profile_id, measure);
CREATE UNIQUE INDEX IF NOT EXISTS ux_acc ON accommodations(profile_id, measure);
CREATE INDEX IF NOT EXISTS ix_acc_state ON accommodations(status);

CREATE TABLE IF NOT EXISTS alt_verifies(
  av_id TEXT PRIMARY KEY, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, ticket_no TEXT, room_id TEXT, seat_no TEXT, site TEXT,
  mode TEXT, docs TEXT, doc_matched INTEGER, inspector TEXT, witness TEXT,
  needs TEXT, reason TEXT, tech_reason INTEGER, device_code TEXT,
  smart_result TEXT, camera_credibility REAL, outcome TEXT, equivalence INTEGER,
  sla_sec INTEGER, elapsed_sec REAL, overdue INTEGER, alert_id TEXT,
  drill INTEGER, operator TEXT, source TEXT, note TEXT, ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_av_ep ON alt_verifies(epoch);
CREATE INDEX IF NOT EXISTS ix_av_key ON alt_verifies(candidate_key);

CREATE TABLE IF NOT EXISTS tech_blocks(
  block_id TEXT PRIMARY KEY, candidate_id TEXT, candidate_name TEXT,
  candidate_key TEXT, room_id TEXT, seat_no TEXT, site TEXT, symptom TEXT,
  device_code TEXT, detail TEXT, channel_code TEXT, raised_at TEXT,
  raised_epoch REAL, resolved INTEGER, final_result TEXT, resolution TEXT,
  waited_sec REAL, resolved_at TEXT, resolved_epoch REAL, alert_id TEXT,
  escalated INTEGER, operator TEXT, source TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_tb_ep ON tech_blocks(raised_epoch);
CREATE INDEX IF NOT EXISTS ix_tb_open ON tech_blocks(resolved);

CREATE TABLE IF NOT EXISTS fairness_runs(
  run_id TEXT PRIMARY KEY, name TEXT, site TEXT, algorithm TEXT, threshold REAL,
  total INTEGER, dims TEXT, groups_count INTEGER, max_gap REAL, min_rate REAL,
  max_rate REAL, worst_dim TEXT, worst_group TEXT, passed INTEGER, action TEXT,
  action_note TEXT, small_groups TEXT, operator TEXT, source TEXT, note TEXT,
  ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_fr_ep ON fairness_runs(epoch);

CREATE TABLE IF NOT EXISTS fairness_groups(
  group_id TEXT PRIMARY KEY, run_id TEXT, dim TEXT, group_name TEXT, total INTEGER,
  passed INTEGER, failed INTEGER, pass_rate REAL, gap_to_best REAL,
  small_sample INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS ix_fg_run ON fairness_groups(run_id, dim);

CREATE TABLE IF NOT EXISTS drills(
  drill_id TEXT PRIMARY KEY, session TEXT, site TEXT, scenario TEXT, channels TEXT,
  participants INTEGER, planned_min REAL, actual_min REAL, issues TEXT,
  actions TEXT, result TEXT, witness TEXT, operator TEXT, source TEXT, note TEXT,
  ts TEXT, epoch REAL);
CREATE INDEX IF NOT EXISTS ix_dr_session ON drills(session, site);

CREATE TABLE IF NOT EXISTS audits(
  audit_id TEXT PRIMARY KEY, ts TEXT, epoch REAL, action TEXT, detail TEXT,
  operator TEXT, prev_hash TEXT, this_hash TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ep ON audits(epoch);

CREATE TABLE IF NOT EXISTS outbox(
  msg_id TEXT PRIMARY KEY, code TEXT, level TEXT, payload TEXT, created_at TEXT,
  created_epoch REAL, tries INTEGER, sent INTEGER, sent_at TEXT, alert_id TEXT,
  last_error TEXT);
CREATE INDEX IF NOT EXISTS ix_ob_sent ON outbox(sent, created_epoch);
"""

# 每张表允许写入的字段（白名单，防止 dataclass 加字段把 SQL 写坏）
FIELDS = {
    "sites": ("site_id", "name", "address", "gate_total", "note", "created_at"),
    "channels": ("channel_id", "code", "name", "site", "kind", "location", "width_mm",
                 "turn_radius_mm", "low_counter", "counter_height_mm",
                 "height_adjustable", "voice_prompt", "text_prompt", "vibration",
                 "sign_service", "status", "devices", "last_check_at",
                 "last_check_epoch", "note", "created_at", "source"),
    "profiles": ("profile_id", "candidate_id", "candidate_name", "candidate_key",
                 "ticket_no", "room_id", "seat_no", "site", "needs", "need_detail",
                 "measures", "channel_hint", "cert_no", "cert_verified", "contact",
                 "consent", "consent_scope", "consent_at", "valid_from", "valid_to",
                 "status", "source", "operator", "note", "created_at", "epoch"),
    "accommodations": ("acc_id", "profile_id", "candidate_key", "measure", "status",
                       "owner", "place", "provided_at", "provided_epoch", "operator",
                       "note", "ts", "epoch"),
    "alt_verifies": ("av_id", "candidate_id", "candidate_name", "candidate_key",
                     "ticket_no", "room_id", "seat_no", "site", "mode", "docs",
                     "doc_matched", "inspector", "witness", "needs", "reason",
                     "tech_reason", "device_code", "smart_result",
                     "camera_credibility", "outcome", "equivalence", "sla_sec",
                     "elapsed_sec", "overdue", "alert_id", "drill", "operator",
                     "source", "note", "ts", "epoch"),
    "tech_blocks": ("block_id", "candidate_id", "candidate_name", "candidate_key",
                    "room_id", "seat_no", "site", "symptom", "device_code", "detail",
                    "channel_code", "raised_at", "raised_epoch", "resolved",
                    "final_result", "resolution", "waited_sec", "resolved_at",
                    "resolved_epoch", "alert_id", "escalated", "operator", "source",
                    "note"),
    "fairness_runs": ("run_id", "name", "site", "algorithm", "threshold", "total",
                      "dims", "groups_count", "max_gap", "min_rate", "max_rate",
                      "worst_dim", "worst_group", "passed", "action", "action_note",
                      "small_groups", "operator", "source", "note", "ts", "epoch"),
    "fairness_groups": ("group_id", "run_id", "dim", "group_name", "total", "passed",
                        "failed", "pass_rate", "gap_to_best", "small_sample", "note"),
    "drills": ("drill_id", "session", "site", "scenario", "channels", "participants",
               "planned_min", "actual_min", "issues", "actions", "result", "witness",
               "operator", "source", "note", "ts", "epoch"),
    "audits": ("audit_id", "ts", "epoch", "action", "detail", "operator", "prev_hash",
               "this_hash"),
    "outbox": ("msg_id", "code", "level", "payload", "created_at", "created_epoch",
               "tries", "sent", "sent_at", "alert_id", "last_error"),
}

# JSON 列（读的时候要还原成 list/dict）
JSON_COLS = {
    "channels": ("devices",),
    "profiles": ("needs", "measures"),
    "alt_verifies": ("docs", "needs", "smart_result"),
    "fairness_groups": (),
    "fairness_runs": ("dims", "small_groups"),
    "drills": ("channels", "issues"),
    "outbox": ("payload",),
}

# 加密列（落库加密、读出解密；空值原样）
ENC_COLS = {
    "profiles": ("candidate_id", "cert_no", "contact", "need_detail"),
    "alt_verifies": ("candidate_id", "ticket_no"),
    "tech_blocks": ("candidate_id",),
}

# 盲索引列（等值查询走它，因为加密列没法直接比对）
KEY_COL = {
    "profiles": "candidate_key",
    "alt_verifies": "candidate_key",
    "tech_blocks": "candidate_key",
    "accommodations": "candidate_key",
}


class Store(object):
    """SQLite 台账。线程安全（GUI 里后台线程也会写）。"""

    def __init__(self, db_path=None):
        self.path = str(db_path or config.DB_PATH)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    # ---------------- 基础设施 ----------------
    def close(self):
        try:
            with self._lock:
                self.conn.commit()
                self.conn.close()
        except sqlite3.Error:
            pass

    def _migrate(self):
        """老库补列：缺列就 ALTER TABLE 加上（不删不改已有列）。"""
        for table, cols in FIELDS.items():
            try:
                have = {r["name"] for r in
                        self.conn.execute("PRAGMA table_info(%s)" % table)}
            except sqlite3.Error:
                continue
            for c in cols:
                if c not in have:
                    try:
                        self.conn.execute("ALTER TABLE %s ADD COLUMN %s TEXT"
                                          % (table, c))
                    except sqlite3.Error:
                        pass
        self.conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            return list(self.conn.execute(sql, params))

    def _one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _count(self, sql, params=()):
        row = self._one(sql, params)
        return int(list(row)[0]) if row else 0

    # ---------------- 序列化 ----------------
    @staticmethod
    def _dbval(value):
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (list, dict)):
            return models.jdump(value)
        return value

    def _row_to_obj(self, table, row):
        cols = FIELDS[table]
        data = {}
        for c in cols:
            if c not in row.keys():
                continue
            data[c] = row[c]
        for c in JSON_COLS.get(table, ()):            # JSON 列还原
            if c in data:
                default = [] if c in ("needs", "measures", "dims", "channels",
                                      "issues", "small_groups", "devices") else {}
                data[c] = models.jload(data[c], default)
        for c in ENC_COLS.get(table, ()):             # 敏感列解密
            if c in data and data[c] is not None:
                data[c] = data_crypto.decrypt_field(data[c])
        try:
            return self.CLASSES[table](**data)
        except TypeError:
            return None

    def _upsert(self, table, obj):
        data = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        cols = [c for c in FIELDS[table] if c in data]
        values = []
        for c in cols:
            v = data.get(c)
            if c in ENC_COLS.get(table, ()):
                v = data_crypto.encrypt_field(v)
            values.append(self._dbval(v))
        placeholders = ",".join("?" for _ in cols)
        sql = "INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (
            table, ",".join(cols), placeholders)
        self._exec(sql, values)
        return obj

    def _list(self, table, where="", params=(), order="", limit=0):
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + where
        if order:
            sql += " ORDER BY " + order
        if limit:
            sql += " LIMIT %d" % int(limit)
        out = []
        for row in self._query(sql, params):
            obj = self._row_to_obj(table, row)
            if obj is not None:
                out.append(obj)
        return out

    # ---------------- 审计（哈希链） ----------------
    def log(self, action, detail="", operator=""):
        last = self._one("SELECT this_hash FROM audits ORDER BY rowid DESC LIMIT 1")
        prev = (last["this_hash"] if last else "") or ""
        a = models.Audit(audit_id=models.new_id("AU"), action=str(action),
                         detail=str(detail or "")[:500], operator=str(operator or ""),
                         prev_hash=prev)
        raw = "|".join([prev, a.audit_id, a.ts, a.action, a.detail, a.operator])
        a.this_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self._upsert("audits", a)
        return a

    def list_audits(self, limit=200, action=None):
        if action:
            return self._list("audits", "action=?", (action,), "rowid DESC", limit)
        return self._list("audits", "", (), "rowid DESC", limit)

    def verify_audit_chain(self):
        """重算整条链：返回 (是否完整, 说明, 行数)。"""
        rows = self._query("SELECT * FROM audits ORDER BY rowid ASC")
        prev = ""
        for i, r in enumerate(rows, 1):
            raw = "|".join([prev, r["audit_id"], r["ts"], r["action"],
                            r["detail"] or "", r["operator"] or ""])
            want = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if (r["prev_hash"] or "") != prev:
                return False, "第 %d 行 prev_hash 断链" % i, len(rows)
            if (r["this_hash"] or "") != want:
                return False, "第 %d 行内容被改动（哈希不符）" % i, len(rows)
            prev = r["this_hash"] or ""
        return True, "审计链完整（共 %d 行）" % len(rows), len(rows)

    # ---------------- 考点 ----------------
    def save_site(self, site):
        if not site.site_id:
            site.site_id = models.new_id("ST")
        self._upsert("sites", site)
        return site

    def get_site(self, name=None, site_id=None):
        if site_id:
            row = self._one("SELECT * FROM sites WHERE site_id=?", (site_id,))
        else:
            row = self._one("SELECT * FROM sites WHERE name=?", (name,))
        return self._row_to_obj("sites", row) if row else None

    def ensure_site(self, name):
        name = str(name or "").strip()
        if not name:
            return None
        s = self.get_site(name=name)
        if s is None:
            s = self.save_site(models.Site(name=name))
        return s

    def list_sites(self):
        return self._list("sites", "", (), "name ASC")

    def delete_site(self, site_id):
        self._exec("DELETE FROM sites WHERE site_id=?", (site_id,))
        return True

    # ---------------- 通道 ----------------
    def save_channel(self, ch):
        if not ch.channel_id:
            ch.channel_id = models.new_id("CH")
        self._upsert("channels", ch)
        return ch

    def get_channel(self, code=None, channel_id=None):
        if channel_id:
            row = self._one("SELECT * FROM channels WHERE channel_id=?", (channel_id,))
        else:
            row = self._one("SELECT * FROM channels WHERE code=?", (code,))
        return self._row_to_obj("channels", row) if row else None

    def list_channels(self, site=None, kind=None, status=None, limit=0):
        where, params = [], []
        if site:
            where.append("site=?")
            params.append(site)
        if kind:
            where.append("kind=?")
            params.append(kind)
        if status:
            where.append("status=?")
            params.append(status)
        return self._list("channels", " AND ".join(where), tuple(params),
                          "site ASC, kind ASC, code ASC", limit)

    def delete_channel(self, channel_id):
        self._exec("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        return True

    # ---------------- 特殊考生档案 ----------------
    def save_profile(self, p):
        if not p.profile_id:
            p.profile_id = models.new_id("PF")
        p.candidate_key = p.candidate_key or data_crypto.blind_key(p.candidate_id)
        self._upsert("profiles", p)
        return p

    def _pf_row(self, profile_id=None, candidate_id=None):
        if profile_id:
            return self._one("SELECT * FROM profiles WHERE profile_id=?", (profile_id,))
        key = data_crypto.blind_key(candidate_id)
        return self._one("SELECT * FROM profiles WHERE candidate_key=? "
                         "ORDER BY epoch DESC LIMIT 1", (key,))

    def get_profile(self, profile_id=None, candidate_id=None):
        row = self._pf_row(profile_id, candidate_id)
        return self._row_to_obj("profiles", row) if row else None

    def list_profiles(self, limit=200, need=None, site=None, status=None):
        where, params = [], []
        if site:
            where.append("site=?")
            params.append(site)
        if status:
            where.append("status=?")
            params.append(status)
        if need:
            # needs 是 JSON 数组，用 LIKE 兜住（表规模小，够用且不引入 FTS 依赖）
            where.append("needs LIKE ?")
            params.append('%%"%s"%%' % need)
        return self._list("profiles", " AND ".join(where), tuple(params),
                          "epoch DESC", limit)

    def profile_stats(self, since_epoch=None):
        where, params = "", ()
        if since_epoch:
            where, params = "epoch>=?", (float(since_epoch),)
        rows = self._query("SELECT needs, consent, status, epoch FROM profiles"
                           + (" WHERE " + where if where else ""), params)
        by_need, no_consent, active = {}, 0, 0
        for r in rows:
            if not int(r["consent"] or 0):
                no_consent += 1
            if (r["status"] or "active") == "active":
                active += 1
            for n in models.jload(r["needs"], []):
                by_need[n] = by_need.get(n, 0) + 1
        return {"total": len(rows), "active": active, "by_need": by_need,
                "without_consent": no_consent}

    # ---------------- 措施发放 ----------------
    def save_accommodation(self, a):
        if not a.acc_id:
            a.acc_id = models.new_id("AC")
        if not a.candidate_key and a.profile_id:
            pf = self.get_profile(profile_id=a.profile_id)
            if pf:
                a.candidate_key = pf.candidate_key
        self._upsert("accommodations", a)
        return a

    def get_accommodation(self, profile_id, measure):
        row = self._one("SELECT * FROM accommodations WHERE profile_id=? AND measure=?",
                        (profile_id, measure))
        return self._row_to_obj("accommodations", row) if row else None

    def list_accommodations(self, profile_id=None, candidate_id=None, status=None,
                            limit=0):
        where, params = [], []
        if profile_id:
            where.append("profile_id=?")
            params.append(profile_id)
        elif candidate_id:
            where.append("candidate_key=?")
            params.append(data_crypto.blind_key(candidate_id))
        if status:
            where.append("status=?")
            params.append(status)
        return self._list("accommodations", " AND ".join(where), tuple(params),
                          "epoch DESC", limit)

    def acc_stats(self, since_epoch=None):
        where, params = "", ()
        if since_epoch:
            where, params = "epoch>=?", (float(since_epoch),)
        rows = self._query("SELECT measure, status FROM accommodations"
                           + (" WHERE " + where if where else ""), params)
        by_state, pending = {}, 0
        for r in rows:
            by_state[r["status"]] = by_state.get(r["status"], 0) + 1
            if r["status"] in ("requested", "approved"):
                pending += 1
        return {"total": len(rows), "by_state": by_state, "pending": pending,
                "provided": by_state.get("provided", 0)}

    # ---------------- 替代核验 ----------------
    def save_alt_verify(self, av):
        if not av.av_id:
            av.av_id = models.new_id("AV")
        av.candidate_key = av.candidate_key or data_crypto.blind_key(av.candidate_id)
        self._upsert("alt_verifies", av)
        return av

    def get_alt_verify(self, av_id):
        row = self._one("SELECT * FROM alt_verifies WHERE av_id=?", (av_id,))
        return self._row_to_obj("alt_verifies", row) if row else None

    def list_alt_verifies(self, limit=200, since_epoch=None, candidate_id=None,
                          drill=None, outcome=None):
        where, params = [], []
        if since_epoch:
            where.append("epoch>=?")
            params.append(float(since_epoch))
        if candidate_id:
            where.append("candidate_key=?")
            params.append(data_crypto.blind_key(candidate_id))
        if drill is not None:
            where.append("drill=?")
            params.append(1 if drill else 0)
        if outcome:
            where.append("outcome=?")
            params.append(outcome)
        return self._list("alt_verifies", " AND ".join(where), tuple(params),
                          "epoch DESC", limit)

    def alt_verify_stats(self, since_epoch=None):
        where, params = "", ()
        if since_epoch:
            where, params = "epoch>=?", (float(since_epoch),)
        rows = self._query("SELECT outcome, overdue, elapsed_sec, tech_reason, "
                           "equivalence, mode, drill FROM alt_verifies"
                           + (" WHERE " + where if where else ""), params)
        out = {"total": 0, "passed": 0, "failed": 0, "deferred": 0, "overdue": 0,
               "tech_reason": 0, "equivalence_ok": 0, "by_mode": {}, "avg_sec": 0.0,
               "drill": 0}
        total_sec = 0.0
        for r in rows:
            out["total"] += 1
            oc = r["outcome"]
            if oc in out:
                out[oc] += 1
            if int(r["overdue"] or 0):
                out["overdue"] += 1
            if int(r["tech_reason"] or 0):
                out["tech_reason"] += 1
            if int(r["equivalence"] or 0):
                out["equivalence_ok"] += 1
            if int(r["drill"] or 0):
                out["drill"] += 1
            out["by_mode"][r["mode"]] = out["by_mode"].get(r["mode"], 0) + 1
            total_sec += float(r["elapsed_sec"] or 0)
        out["avg_sec"] = round(total_sec / out["total"], 1) if out["total"] else 0.0
        return out

    # ---------------- 因技术原因受阻事件 ----------------
    def save_tech_block(self, b):
        if not b.block_id:
            b.block_id = models.new_id("TB")
        b.candidate_key = b.candidate_key or data_crypto.blind_key(b.candidate_id)
        self._upsert("tech_blocks", b)
        return b

    def get_tech_block(self, block_id):
        row = self._one("SELECT * FROM tech_blocks WHERE block_id=?", (block_id,))
        return self._row_to_obj("tech_blocks", row) if row else None

    def list_tech_blocks(self, limit=200, since_epoch=None, resolved=None,
                         final_result=None):
        where, params = [], []
        if since_epoch:
            where.append("raised_epoch>=?")
            params.append(float(since_epoch))
        if resolved is not None:
            where.append("resolved=?")
            params.append(1 if resolved else 0)
        if final_result:
            where.append("final_result=?")
            params.append(final_result)
        return self._list("tech_blocks", " AND ".join(where), tuple(params),
                          "raised_epoch DESC", limit)

    def tech_block_stats(self, since_epoch=None):
        where, params = "", ()
        if since_epoch:
            where, params = "raised_epoch>=?", (float(since_epoch),)
        rows = self._query("SELECT resolved, final_result, symptom, waited_sec, "
                           "escalated FROM tech_blocks"
                           + (" WHERE " + where if where else ""), params)
        out = {"total": 0, "open": 0, "denied": 0, "entered": 0,
               "entered_manual": 0, "escalated": 0, "by_symptom": {},
               "max_wait_sec": 0.0, "avg_wait_sec": 0.0}
        wait_sum = 0.0
        for r in rows:
            out["total"] += 1
            if not int(r["resolved"] or 0):
                out["open"] += 1
            fr = r["final_result"] or ""
            if fr in ("entered", "entered_manual"):
                out[fr] += 1
            if fr == "denied":
                out["denied"] += 1
            if int(r["escalated"] or 0):
                out["escalated"] += 1
            out["by_symptom"][r["symptom"]] = out["by_symptom"].get(r["symptom"], 0) + 1
            w = float(r["waited_sec"] or 0)
            wait_sum += w
            out["max_wait_sec"] = max(out["max_wait_sec"], w)
        out["avg_wait_sec"] = round(wait_sum / out["total"], 1) if out["total"] else 0.0
        return out

    # ---------------- 公平性分层测试 ----------------
    def save_fairness_run(self, run):
        if not run.run_id:
            run.run_id = models.new_id("FR")
        self._upsert("fairness_runs", run)
        return run

    def get_fairness_run(self, run_id):
        row = self._one("SELECT * FROM fairness_runs WHERE run_id=?", (run_id,))
        return self._row_to_obj("fairness_runs", row) if row else None

    def list_fairness_runs(self, limit=50):
        return self._list("fairness_runs", "", (), "epoch DESC", limit)

    def latest_fairness_run(self):
        runs = self.list_fairness_runs(limit=1)
        return runs[0] if runs else None

    def save_fairness_group(self, g):
        if not g.group_id:
            g.group_id = models.new_id("FG")
        self._upsert("fairness_groups", g)
        return g

    def list_fairness_groups(self, run_id, dim=None):
        where, params = ["run_id=?"], [run_id]
        if dim:
            where.append("dim=?")
            params.append(dim)
        return self._list("fairness_groups", " AND ".join(where), tuple(params),
                          "dim ASC, pass_rate DESC")

    # ---------------- 演练 ----------------
    def save_drill(self, d):
        if not d.drill_id:
            d.drill_id = models.new_id("DL")
        self._upsert("drills", d)
        return d

    def get_drill(self, drill_id):
        row = self._one("SELECT * FROM drills WHERE drill_id=?", (drill_id,))
        return self._row_to_obj("drills", row) if row else None

    def list_drills(self, limit=200, session=None, site=None, since_epoch=None):
        where, params = [], []
        if session:
            where.append("session=?")
            params.append(session)
        if site:
            where.append("site=?")
            params.append(site)
        if since_epoch:
            where.append("epoch>=?")
            params.append(float(since_epoch))
        return self._list("drills", " AND ".join(where), tuple(params),
                          "epoch DESC", limit)

    def drill_stats(self, session=None, site=None, since_epoch=None):
        rows = self.list_drills(limit=10000, session=session, site=site,
                                since_epoch=since_epoch)
        by_result, by_session, sites_done = {}, {}, set()
        for d in rows:
            by_result[d.result] = by_result.get(d.result, 0) + 1
            if d.session:
                by_session[d.session] = by_session.get(d.session, 0) + 1
            if d.site:
                sites_done.add(d.site)
        return {"total": len(rows), "by_result": by_result,
                "by_session": by_session, "sites": sorted(sites_done),
                "pass": by_result.get("pass", 0),
                "partial": by_result.get("partial", 0),
                "fail": by_result.get("fail", 0),
                "with_issues": len([d for d in rows if d.issues])}

    # ---------------- 联动外发队列 ----------------
    def outbox_add(self, code, level, payload):
        it = models.Outbox(msg_id=models.new_id("OB"), code=code, level=level,
                           payload=dict(payload or {}))
        self._upsert("outbox", it)
        return it

    def list_outbox(self, pending_only=True, limit=100):
        if pending_only:
            return self._list("outbox", "sent=0", (), "created_epoch ASC", limit)
        return self._list("outbox", "", (), "created_epoch DESC", limit)

    def mark_outbox(self, msg_id, sent=False, alert_id="", last_error=""):
        self._exec("UPDATE outbox SET tries=tries+1, sent=?, sent_at=?, alert_id=?, "
                   "last_error=? WHERE msg_id=?",
                   (1 if sent else 0, models.now_ts() if sent else "", alert_id,
                    str(last_error or "")[:300], msg_id))
        return True

    def outbox_stats(self):
        total = self._count("SELECT COUNT(*) FROM outbox")
        pending = self._count("SELECT COUNT(*) FROM outbox WHERE sent=0")
        return {"total": total, "pending": pending, "sent": total - pending}

    # ---------------- 汇总 ----------------
    def summary(self, since_epoch=None):
        out = {
            "sites": len(self.list_sites()),
            "channels": self._count("SELECT COUNT(*) FROM channels"),
            "channels_usable": self._count(
                "SELECT COUNT(*) FROM channels WHERE status IN ('ok','degraded')"),
            "profiles": self.profile_stats(since_epoch),
            "accommodations": self.acc_stats(since_epoch),
            "alt_verifies": self.alt_verify_stats(since_epoch),
            "tech_blocks": self.tech_block_stats(since_epoch),
            "drills": self.drill_stats(since_epoch=since_epoch),
            "fairness_runs": self._count("SELECT COUNT(*) FROM fairness_runs"),
            "outbox": self.outbox_stats(),
            "audits": self._count("SELECT COUNT(*) FROM audits"),
        }
        return out

    # ---------------- 维护 ----------------
    def purge_runtime(self, keep_days=30):
        """
        最小必要：清理超过保留期的**运行态**数据（审计不删、通道/考点/档案不删）。
        outbox 只清**已发出**的，待发事件绝不因为过期被删掉（事件不能丢）。
        返回各表删除条数。
        """
        cutoff = time.time() - float(keep_days) * 86400
        deleted = {}
        jobs = (("alt_verifies", "epoch < ?", (cutoff,)),
                ("tech_blocks", "raised_epoch < ? AND resolved = 1", (cutoff,)),
                ("outbox", "created_epoch < ? AND sent = 1", (cutoff,)))
        for table, where, params in jobs:
            try:
                cur = self._exec("DELETE FROM %s WHERE %s" % (table, where), params)
                deleted[table] = cur.rowcount
            except sqlite3.Error:
                deleted[table] = 0
        return deleted

    def export_json(self, obj_list, path):
        import json
        data = [o.to_dict() if hasattr(o, "to_dict") else o for o in obj_list]
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return str(path)


# 表名 -> dataclass（供 _row_to_obj 使用）
Store.CLASSES = {
    "sites": models.Site,
    "channels": models.Channel,
    "profiles": models.Profile,
    "accommodations": models.Accommodation,
    "alt_verifies": models.AltVerify,
    "tech_blocks": models.TechBlock,
    "fairness_runs": models.FairnessRun,
    "fairness_groups": models.FairnessGroup,
    "drills": models.Drill,
    "audits": models.Audit,
    "outbox": models.Outbox,
}
