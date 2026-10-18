# -*- coding: utf-8 -*-
"""
智能终端操作端 · 本地存储（SQLite）

一张库表管四件事：
  alerts     告警（含复核结果、同步状态）
  reviews    复核留痕（独立成表，便于审计与误报闭环统计）
  audit      哈希链审计日志（告警接入 / 确认 / 复核 / 管控指令 全部留痕）
  commands   统一管控指令队列（远程配置、白名单、远程擦除）

局域网联动另加两张表（**不与本机 audit 链合并**，见各自注释）：
  remote_records  远端设备（01/02）送上来的操作记录 / 核验结论 / 核查结论
  remote_devices  远端设备台账（最后活跃、声明的链头、累计条数）
"""
import hashlib
import json
import sqlite3
import threading
import time

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

    # ---------------- 字段级加密辅助（考生敏感数据） ----------------
    @staticmethod
    def _enc_alert(alert):
        """保存前加密考生敏感字段；返回一个新的 Alert 副本，不修改原对象。"""
        if alert is None:
            return None
        import copy
        a = copy.copy(alert)
        a.candidate_id = data_crypto.encrypt_field(alert.candidate_id)
        a.candidate_name = data_crypto.encrypt_field(alert.candidate_name)
        a.seat_no = data_crypto.encrypt_field(alert.seat_no)
        a.detail = data_crypto.encrypt_field(alert.detail)
        return a

    @staticmethod
    def _dec_alert(alert):
        """读取后解密考生敏感字段。"""
        if alert is None:
            return None
        alert.candidate_id = data_crypto.decrypt_field(alert.candidate_id)
        alert.candidate_name = data_crypto.decrypt_field(alert.candidate_name)
        alert.seat_no = data_crypto.decrypt_field(alert.seat_no)
        alert.detail = data_crypto.decrypt_field(alert.detail)
        return alert

    @staticmethod
    def _enc_review(rec):
        """保存前加密复核敏感字段；返回副本，不修改原对象。"""
        if rec is None:
            return None
        import copy
        r = copy.copy(rec)
        r.reason = data_crypto.encrypt_field(rec.reason)
        if rec.attachments:
            r.attachments = data_crypto.encrypt_field(
                json.dumps(rec.attachments, ensure_ascii=False))
        return r

    @staticmethod
    def _dec_review_row(row):
        """先把原始行里的敏感字段解密，再交给 ReviewRecord.from_row 反序列化。"""
        if row is None:
            return None
        d = dict(row)
        d["reason"] = data_crypto.decrypt_field(d.get("reason") or "")
        att = data_crypto.decrypt_field(d.get("attachments") or "")
        if isinstance(att, str) and att and not att.lstrip().startswith("["):
            att = "[]"
        d["attachments"] = att or "[]"
        return models.ReviewRecord.from_row(d)

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
                applied INTEGER DEFAULT 0, applied_at TEXT, operator TEXT,
                -- 发布命令（03 -> 01/02）的回执：ok/fail + 结果 JSON
                status TEXT, result TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY, v TEXT
            );

            -- ---- 局域网联动：远端设备送上来的记录（与本地审计链**分链**存储）----
            -- 为什么不并进 audit 表：audit 是本机 sha256 哈希链，塞进别人的记录会让
            -- verify_chain() 立刻报"被篡改"。远端记录单独一张表，按 device_id 分组，
            -- 本机只在自己链上写一条"收到 X 设备 N 条"的回执。
            CREATE TABLE IF NOT EXISTS remote_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT, device_name TEXT, kind TEXT, record_id TEXT,
                ts TEXT, action TEXT, detail TEXT, operator TEXT,
                raw TEXT, received_at TEXT, received_epoch REAL, dedupe TEXT,
                UNIQUE(device_id, kind, dedupe)
            );
            CREATE INDEX IF NOT EXISTS idx_remote_dev ON remote_records(device_id, kind);
            CREATE INDEX IF NOT EXISTS idx_remote_epoch ON remote_records(received_epoch);

            -- 远端设备台账：最后活跃时间、声明过的链头、累计条数
            CREATE TABLE IF NOT EXISTS remote_devices (
                device_id TEXT PRIMARY KEY, device_name TEXT, kinds TEXT,
                chain_algo TEXT, chain_head TEXT, chain_count INTEGER,
                chain_verified TEXT, first_seen TEXT, last_seen TEXT,
                records INTEGER DEFAULT 0, chain_changes INTEGER DEFAULT 0
            );

            -- ---- 数据维护留痕（清空操作）----
            -- 为什么单独一张表：清空是"删除数据"的动作，而 audit 是**只追加**的哈希链；
            -- 把"删了多少、删的什么范围、谁删的、为什么删"记在这里，既留了痕，
            -- 又不影响 audit 链的完整性（verify_chain 仍然全绿）。
            CREATE TABLE IF NOT EXISTS purge_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, scope TEXT, mode TEXT, deleted INTEGER,
                detail TEXT, operator TEXT, reason TEXT, audit_hash TEXT
            );
            """)

        # ---- 老库迁移：补 alerts 的清空标记列（mark 模式需要） ----
        # 幂等：列已存在就跳过。不做这一步，mark 模式会因缺列直接报错。
        self._migrate()

    def _migrate(self):
        """给老库补列（幂等）。新库由 CREATE TABLE 直接带列。"""
        want = {
            "alerts": [("purged", "INTEGER DEFAULT 0"),
                       ("purged_at", "TEXT"),
                       ("purged_by", "TEXT"),
                       ("purge_reason", "TEXT")],
            # 老库的 commands 表没有回执列 —— 发布命令功能需要，必须补
            "commands": [("status", "TEXT"),
                         ("result", "TEXT")],
        }
        with self._lock, self._conn() as c:
            for table, cols in want.items():
                have = {r["name"] for r in
                        c.execute("PRAGMA table_info(%s)" % table).fetchall()}
                for name, decl in cols:
                    if name not in have:
                        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))

    # ---------------- 告警 ----------------
    def save_alert(self, alert):
        enc = self._enc_alert(alert)
        row = enc.to_row()
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO alerts (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def get_alert(self, alert_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return self._dec_alert(models.Alert.from_row(r)) if r else None

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
        return [self._dec_alert(models.Alert.from_row(r)) for r in rows]

    def pending_unsynced(self, limit=50):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE synced=0 "
                             "ORDER BY created_epoch ASC LIMIT ?", (int(limit),)).fetchall()
        return [self._dec_alert(models.Alert.from_row(r)) for r in rows]

    def mark_synced(self, alert_id):
        with self._lock, self._conn() as c:
            c.execute("UPDATE alerts SET synced=1 WHERE alert_id=?", (alert_id,))

    # ---------------- 复核 ----------------
    def save_review(self, rec):
        enc = self._enc_review(rec)
        row = enc.to_row()
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reviews (%s) VALUES (%s)" % (cols, ph),
                      list(row.values()))

    def list_reviews(self, limit=500):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM reviews ORDER BY created_at DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [self._dec_review_row(r) for r in rows]

    def get_review(self, review_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if r is None:
            return None
        return self._dec_review_row(r)

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

    # ---------------- 发布命令（03 -> 01/02 局域网，可回执） ----------------
    #: 允许下发的命令白名单（**两侧共同遵守**，防止变成任意代码执行通道）
    COMMAND_TYPES = {
        "ping": "连通性与版本探测",
        "status": "回报运行状态",
        "logs_tail": "回报最近日志（行数可指定，最多 200 行）",
        "rebuild_index": "重建人脸特征索引（仅 01）",
        "selftest": "跑一次本模块自检并回报结果",
    }

    def push_target_command(self, target, kind, params=None, operator="", note=""):
        """下发一条命令给某个目标设备（01/02/…）。返回 cmd_id。

        target 用设备标识（如 PC-A-01）或模块代号（01/02）；
        kind 必须在 COMMAND_TYPES 白名单里，否则拒绝（不写库）。
        """
        kind = (kind or "").strip()
        if kind not in self.COMMAND_TYPES:
            return ""
        payload = {"target": (target or "").strip(), "kind": kind,
                   "params": params or {}, "note": note or ""}
        cmd_id = self.push_command("devcmd", payload, operator or "terminal")
        self.log("下发命令", "目标=%s 类型=%s 参数=%s"
                 % (target, kind, json.dumps(params or {}, ensure_ascii=False)),
                 operator or "terminal")
        return cmd_id

    def commands_for(self, target, limit=20, pending_only=True):
        """某目标待执行的命令（按时间正序）。target 为空表示不过滤。"""
        sql = ("SELECT cmd_id, payload, created_at, operator FROM commands "
               "WHERE kind='devcmd' AND applied=0")
        args = []
        if target:
            # 注意：SQL 的 LIKE 通配符是 %，而 Python 的 % 是格式化符 ——
            # 这里用字符串拼接而非 % 运算，避免 '%"target": "x"' 被当成格式串。
            sql += " AND payload LIKE ?"
            args.append('%' + '"target": "' + target + '"' + '%')
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                pl = json.loads(r["payload"] or "{}")
            except Exception:
                pl = {}
            tgt = (pl.get("target") or "").strip()
            if target and tgt and tgt != target:
                continue                      # LIKE 只是粗筛，这里精确比对
            out.append({"cmd_id": r["cmd_id"], "kind": pl.get("kind", ""),
                        "params": pl.get("params") or {}, "note": pl.get("note", ""),
                        "created_at": r["created_at"], "operator": r["operator"],
                        "target": tgt})
        return out

    def finish_command(self, cmd_id, ok=True, result=None, operator=""):
        """记录命令回执（成功/失败 + 结果摘要），并标记已执行。"""
        with self._lock, self._conn() as c:
            c.execute("UPDATE commands SET applied=1, applied_at=?, status=?, "
                      "result=? WHERE cmd_id=?",
                      (models.now_ts(), "ok" if ok else "fail",
                       json.dumps(result if result is not None else {}, ensure_ascii=False),
                       cmd_id))
        self.log("命令回执", "命令=%s 结果=%s" % (cmd_id, "成功" if ok else "失败"),
                 operator or "remote")

    def list_commands(self, limit=50):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM commands WHERE kind='devcmd' "
                             "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload_obj"] = json.loads(d.get("payload") or "{}")
            except Exception:
                d["payload_obj"] = {}
            try:
                d["result_obj"] = json.loads(d.get("result") or "{}")
            except Exception:
                d["result_obj"] = {}
            out.append(d)
        return out

    def clear_finished_commands(self):
        """清理已完成的命令台账（applied=1）。**不动审计链** —— 下发/回执留痕仍在。"""
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM commands WHERE kind='devcmd' AND applied=1")
            n = cur.rowcount
        self.log("清理命令台账", "已删除 %d 条已完成记录" % n, "terminal")
        return max(0, n)

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

    # ---------------- 局域网联动：远端记录 ----------------
    @staticmethod
    def _norm_remote(kind, rec):
        """
        把上报来的一条记录规整成统一列（认不出来也照存 raw，不丢数据）。
        归一化的目的：01 的操作日志、01 的核验记录、02 的核查结论、02 的工单
        字段名各不相同，而终端上要能**同一张表按时间混排**看。
        """
        if not isinstance(rec, dict):
            rec = {"detail": str(rec)}
        raw = models.canonical(rec)
        ts = str(rec.get("ts") or rec.get("created_at") or rec.get("time")
                 or rec.get("received_at") or "")
        action = str(rec.get("action") or rec.get("code") or rec.get("title")
                     or rec.get("status") or kind)
        detail = str(rec.get("detail") or rec.get("reason") or rec.get("message")
                     or rec.get("note") or "")
        operator = str(rec.get("operator") or rec.get("user") or rec.get("reviewer")
                       or rec.get("assigned_to") or "")
        rid = str(rec.get("record_id") or rec.get("id") or rec.get("alert_id")
                  or rec.get("check_id") or rec.get("order_id") or "")
        dedupe = hashlib.sha1(("%s|%s|%s|%s|%s|%s" % (kind, rid, ts, action, detail,
                                                      raw)).encode("utf-8")).hexdigest()
        return {"record_id": rid, "ts": ts, "action": action, "detail": detail,
                "operator": operator, "raw": raw, "dedupe": dedupe}

    def save_remote_batch(self, device_id, device_name, kind, records, chain=None,
                          operator="远端接入"):
        """
        接入一批远端记录。返回：
          {accepted, duplicated, total, chain_head, chain_changed, first_seen,
           device_records}

        · 幂等：同一条记录（内容相同）重复上报只会算 duplicated，不会写第二行；
        · **不碰本机 audit 链**：远端记录进 remote_records，本机链只写一条回执；
        · 链头变更如实记录（说明对端重建过链/清过日志），本机无法替它对账，
          只把"变更"这件事留痕，不假装校验通过。
        """
        device_id = str(device_id or "").strip() or "unknown"
        kind = str(kind or "audit").strip() or "audit"
        chain = dict(chain or {})
        recs = list(records or [])
        now = models.now_ts()
        epoch = models.now_epoch()
        accepted = duplicated = 0

        with self._lock, self._conn() as c:
            for rec in recs:
                n = self._norm_remote(kind, rec)
                cur = c.execute(
                    "INSERT OR IGNORE INTO remote_records "
                    "(device_id, device_name, kind, record_id, ts, action, detail, "
                    " operator, raw, received_at, received_epoch, dedupe) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, device_name or "", kind, n["record_id"], n["ts"],
                     n["action"], data_crypto.encrypt_field(n["detail"]),
                     n["operator"], data_crypto.encrypt_field(n["raw"]),
                     now, epoch, n["dedupe"]))
                if cur.rowcount:
                    accepted += 1
                else:
                    duplicated += 1

            row = c.execute("SELECT * FROM remote_devices WHERE device_id=?",
                            (device_id,)).fetchone()
            old_head = (row["chain_head"] if row else "") or ""
            new_head = str(chain.get("head") or "")
            # 只在"对端明确声明了链头"且与上次不同、且不是首次登记时算一次变更
            changed = bool(old_head and new_head and old_head != new_head)
            first_seen = bool(row is None)
            kinds = ""
            if row:
                kinds = row["kinds"] or ""
            kinds = "、".join(sorted(set([k for k in kinds.split("、") if k] + [kind])))
            total = (row["records"] if row else 0) + accepted
            changes = ((row["chain_changes"] if row else 0) or 0) + (1 if changed else 0)
            c.execute(
                "INSERT OR REPLACE INTO remote_devices "
                "(device_id, device_name, kinds, chain_algo, chain_head, chain_count, "
                " chain_verified, first_seen, last_seen, records, chain_changes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (device_id, device_name or (row["device_name"] if row else ""), kinds,
                 str(chain.get("algo") or ""), new_head or old_head,
                 int(chain.get("count") or 0), str(chain.get("verified", "")),
                 (row["first_seen"] if row else now), now, total, changes))

        # 本机审计链上的回执：只有"谁在什么时候送来了多少条"，不含远端明细
        self.log("远端记录接入",
                 "%s（%s）kind=%s 收到=%d 重复=%d 声明链头=%s%s"
                 % (device_id, device_name or "-", kind, accepted, duplicated,
                    (new_head[:16] + "…") if len(new_head) > 16 else (new_head or "-"),
                    "（链头与上次不同，已留痕）" if changed else ""),
                 operator)
        return {"accepted": accepted, "duplicated": duplicated, "total": total,
                "chain_head": new_head or old_head, "chain_changed": changed,
                "first_seen": first_seen, "device_records": total,
                "device_id": device_id, "kind": kind}

    def list_remote_records(self, device_id=None, kind=None, limit=200):
        """远端记录（按接收时间倒序）。detail/raw 落库时加密，这里解密后返回。"""
        sql = "SELECT * FROM remote_records WHERE 1=1"
        args = []
        if device_id:
            sql += " AND device_id=?"
            args.append(device_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY received_epoch DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = data_crypto.decrypt_field(d.get("detail"))
            d["raw"] = data_crypto.decrypt_field(d.get("raw"))
            out.append(d)
        return out

    def remote_devices(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM remote_devices "
                             "ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def remote_summary(self):
        with self._lock, self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM remote_records").fetchone()["n"]
            devs = c.execute("SELECT COUNT(*) n FROM remote_devices").fetchone()["n"]
            rows = c.execute("SELECT kind, COUNT(*) n FROM remote_records "
                             "GROUP BY kind").fetchall()
            last = c.execute("SELECT MAX(received_at) t FROM remote_records").fetchone()["t"]
        return {"records": total, "devices": devs, "last_received": last or "",
                "by_kind": {r["kind"]: r["n"] for r in rows}}

    def prune_remote(self, keep=None):
        """远端记录只增不减会撑爆库；超过上限按接收时间裁掉最旧的（本地审计链留痕）。"""
        keep = int(keep or config.LAN_REMOTE_LIMIT)
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM remote_records").fetchone()["n"]
            if n <= keep:
                return 0
            c.execute("DELETE FROM remote_records WHERE id IN ("
                      "SELECT id FROM remote_records ORDER BY received_epoch ASC "
                      "LIMIT ?)", (n - keep,))
        self.log("远端记录裁剪", "上限 %d 条，已裁掉最旧的 %d 条" % (keep, n - keep), "system")
        return n - keep

    # ---------------- 数据维护：清空（追加式留痕，保哈希链） ----------------
    #: 清空范围（scope）与中文名
    PURGE_SCOPES = {
        "remote": "远端记录（01/02 经局域网送上来的数据）",
        "local": "本机告警与复核留痕",
        "queue": "补传队列与本地缓存",
    }

    def purge_data(self, scope, mode="hard", operator="", reason=""):
        """
        清空指定范围的数据。**不删数据库文件**（运行中删 .db 必然被占用锁住，
        实测报 WinError 32），而是在事务里删表内行；审计链与 purge_log 保留。

        scope: remote | local | queue | all
        mode : hard（真删）| mark（只标记，数据仍在，可查不可办）
        reason: 必填（数据维护属敏感操作，要能解释为什么删）
        返回 (ok, 说明, 删除条数明细 dict)
        """
        import time as _t
        if scope not in self.PURGE_SCOPES and scope != "all":
            return False, "未知清空范围：%s" % scope, {}
        if not (reason or "").strip():
            return False, "必须填写清空原因（数据维护要能解释）", {}
        mode = "mark" if str(mode).lower() == "mark" else "hard"
        scopes = list(self.PURGE_SCOPES) if scope == "all" else [scope]
        detail = {}

        with self._lock, self._conn() as c:
            for sc in scopes:
                if mode == "hard":
                    n = 0
                    if sc == "remote":
                        n = c.execute("SELECT COUNT(*) n FROM remote_records").fetchone()["n"]
                        c.execute("DELETE FROM remote_records")
                        # 台账的累计条数是历史事实，不跟着删；但"当前存量"要归零，
                        # 否则界面显示"2 台设备 12 条"而列表是空的，像 bug。
                        c.execute("UPDATE remote_devices SET records=0")
                    elif sc == "local":
                        n = c.execute("SELECT COUNT(*) n FROM alerts").fetchone()["n"]
                        n += c.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
                        c.execute("DELETE FROM alerts")
                        c.execute("DELETE FROM reviews")
                    elif sc == "queue":
                        n = c.execute("SELECT COUNT(*) n FROM alerts "
                                      "WHERE synced=0").fetchone()["n"]
                        # 队列数据 = alerts 里未同步那些；补传走 synced 标记，
                        # 所以"清空队列"是把未同步的标记为已同步（丢弃待补传），
                        # 而不是删掉告警本身——否则等于顺手删了业务数据。
                        c.execute("UPDATE alerts SET synced=1 WHERE synced=0")
                    detail[sc] = n
                else:
                    n = 0
                    if sc == "remote":
                        # remote_records 无状态列，mark 模式改为"超限裁剪到 0"以外的做法：
                        # 这里只能如实说明不可标记，退回硬删 0 条以免误导。
                        detail[sc] = 0
                        continue
                    elif sc == "local":
                        n = c.execute("SELECT COUNT(*) n FROM alerts "
                                      "WHERE purged=0").fetchone()["n"]
                        c.execute("UPDATE alerts SET purged=1, purged_at=?, "
                                  "purged_by=?, purge_reason=? WHERE purged=0",
                                  (models.now_ts(), operator, reason))
                    elif sc == "queue":
                        n = c.execute("SELECT COUNT(*) n FROM alerts "
                                      "WHERE synced=0").fetchone()["n"]
                        c.execute("UPDATE alerts SET synced=1 WHERE synced=0")
                    detail[sc] = n

        total = sum(detail.values())
        desc = "；".join("%s %d 条" % (self.PURGE_SCOPES.get(k, k), v)
                         for k, v in detail.items()) or "无数据"
        h = self.log("数据清空",
                     "范围=%s 模式=%s 明细=%s 原因=%s" % (scope, mode, desc, reason),
                     operator or "system")
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO purge_log (ts, scope, mode, deleted, detail, operator, "
                      "reason, audit_hash) VALUES (?,?,?,?,?,?,?,?)",
                      (models.now_ts(), scope, mode, total, desc, operator or "system",
                       reason, h))
        return True, "已清空：%s（模式 %s，共 %d 条；已写审计链与维护留痕）" % (desc, mode, total), detail

    def purge_rows(self, limit=50):
        """数据维护留痕（最近 N 条）"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM purge_log ORDER BY id DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def vacuum(self):
        """回收空间（清空后调用；运行中安全，但会短暂加锁）"""
        with self._lock, self._conn() as c:
            c.execute("VACUUM")
        return True

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

    def stats(self, hours=24, days=7, recent=30):
        """驾驶舱聚合统计（全部在 SQL 里 GROUP BY，不把明细读进内存）。

        为什么单开一个方法（而不是让界面 list_alerts 后自己数）：
          · 改造前驾驶舱每次刷新都 `list_alerts(limit=2000)` + `audit_rows(100000)`，
            库一大就既慢又**数不准**（超过 2000 条的部分直接不算，KPI 会偏小）；
          · 这里用 COUNT/GROUP BY，返回的就是库里真实的数，与明细条数无关。

        参数：
          hours  趋势图按小时分桶的窗口（含当前小时，旧→新）
          days   趋势图按天分桶的窗口（含今天，旧→新）
          recent "最近告警"表要的明细条数

        返回 dict：
          total/by_level/by_status/reviews/unsynced/overdue/last_hour/audits
          by_code  [(code, n), ...] 全部分类（按数量降序；类型数天然很少，不截断）
          hourly   [(桶起始 epoch, n), ...]（旧→新）
          daily    [(桶起始 epoch, n), ...]（旧→新）
          recent   [models.Alert, ...]（已解密，按时间倒序）
        """
        now = time.time()
        hours = max(1, int(hours))
        days = max(1, int(days))
        # 桶锚点：小时对齐到整点、天对齐到本地 0 点（用本地时间，标签才对得上）
        hour_anchor = float(int(now // 3600) * 3600)
        lt = time.localtime(now)
        try:
            day_anchor = float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                            0, 0, 0, 0, 0, -1)))
        except (OverflowError, ValueError):
            day_anchor = float(int(now // 86400) * 86400)
        hour_since = hour_anchor - (hours - 1) * 3600
        day_since = day_anchor - (days - 1) * 86400

        with self._lock, self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM alerts").fetchone()["n"]
            reviewed = c.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"]
            unsynced = c.execute(
                "SELECT COUNT(*) n FROM alerts WHERE synced=0").fetchone()["n"]
            overdue = c.execute(
                "SELECT COUNT(*) n FROM alerts WHERE status=? AND sla_deadline IS NOT NULL "
                "AND sla_deadline < ?", (models.ST_PENDING, now)).fetchone()["n"]
            last_hour = c.execute(
                "SELECT COUNT(*) n FROM alerts WHERE created_epoch >= ?",
                (now - 3600,)).fetchone()["n"]
            audits = c.execute("SELECT COUNT(*) n FROM audit").fetchone()["n"]
            lv_rows = c.execute("SELECT level, COUNT(*) n FROM alerts "
                                "GROUP BY level").fetchall()
            st_rows = c.execute("SELECT status, COUNT(*) n FROM alerts "
                                "GROUP BY status").fetchall()
            code_rows = c.execute("SELECT code, COUNT(*) n FROM alerts "
                                  "GROUP BY code ORDER BY n DESC, code").fetchall()
            # 分桶用"相对窗口起点"的偏移：分子恒为非负（WHERE 已过滤 >= since），
            # CAST 截断就等于 floor。**不能**用 (anchor - created_epoch) 直接除 ——
            # 那样"上一小时/昨天"的事件会被算进当前桶（整点对齐后必然差一个桶）。
            h_rows = c.execute(
                "SELECT CAST((created_epoch - ?) / 3600 AS INTEGER) i, COUNT(*) n "
                "FROM alerts WHERE created_epoch >= ? GROUP BY i",
                (hour_since, hour_since)).fetchall()
            d_rows = c.execute(
                "SELECT CAST((created_epoch - ?) / 86400 AS INTEGER) i, COUNT(*) n "
                "FROM alerts WHERE created_epoch >= ? GROUP BY i",
                (day_since, day_since)).fetchall()

        hourly = [(int(hour_since + i * 3600), 0) for i in range(hours)]
        for r in h_rows:
            idx = int(r["i"] or 0)
            if 0 <= idx < hours:
                ts, n = hourly[idx]
                hourly[idx] = (ts, n + int(r["n"] or 0))
        daily = [(int(day_since + i * 86400), 0) for i in range(days)]
        for r in d_rows:
            idx = int(r["i"] or 0)
            if 0 <= idx < days:
                ts, n = daily[idx]
                daily[idx] = (ts, n + int(r["n"] or 0))

        return {
            "total": int(total or 0),
            "by_level": {r["level"]: int(r["n"] or 0) for r in lv_rows},
            "by_status": {(r["status"] or ""): int(r["n"] or 0) for r in st_rows},
            "reviews": int(reviewed or 0),
            "unsynced": int(unsynced or 0),
            "overdue": int(overdue or 0),
            "last_hour": int(last_hour or 0),
            "audits": int(audits or 0),
            "by_code": [(r["code"], int(r["n"] or 0)) for r in code_rows],
            "hourly": hourly,
            "daily": daily,
            "recent": self.list_alerts(limit=recent),
        }
