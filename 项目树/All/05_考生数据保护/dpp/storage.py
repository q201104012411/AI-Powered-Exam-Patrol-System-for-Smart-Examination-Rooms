# -*- coding: utf-8 -*-
r"""
考生数据保护 · 本地存储（SQLite）

表结构（全部哈希链留痕）：
  candidates     考生记录（敏感字段**全部字段级加密**；特征模板存不可逆摘要）
  consents       单独同意记录（采集的前置条件）
  uses           目的限定使用记录（含被拒绝的越范围使用）
  reveals        授权解密查看留痕（含动态水印文本）
  shares         跨层级共享/导出申请与审批
  destroys       销毁记录（到期自动 / 按申请 / 强制）
  rights         考生权利响应申请
  channels       非人脸替代通道与演练记录
  measures       管理措施台账（DPO/DPIA/权限审批/应急/培训）
  incidents      安全事件与上报时延
  audits         哈希链审计（登录/查询/导出/删除/解密/共享/销毁… 全覆盖）
  kv             键值（账户、会话密钥等）

铁律：candidates 表里 name / id_card_no / ticket_no / seat_no / face_image_path
      入库前一律 encrypt_field，读取时统一 decrypt；只有这一处出入口。

只读不可改（append-only）铁律：
      已落库的加密数据**任何人不得就地修改**，系统只提供两条路径：
        · INSERT —— 上传/登记新的考生数据（追加）
        · DELETE —— 删除整条记录（到期自动销毁 / 权利删除 / 强制销毁）
      注意 candidates 表用 **INSERT（唯一键冲突即报错）**，不用 INSERT OR REPLACE，
      否则"改一条考生"会被伪装成"又插了一条"。同时：
        · 业务层：save_candidate 前先查是否已存在，已存在 -> 抛 ReadOnlyViolation；
        · 数据库层：BEFORE UPDATE 触发器直接 RAISE(ABORT)，连绕过业务直接改库也挡住；
                   BEFORE DELETE 触发器放行（删除是允许的），但 DELETE 前后由业务层留痕。
      每条加密记录还会写一行 signatures（SM3 摘要 + SM2 签名），
      任何改库行为都会让 verify_record() 验签失败。
"""
import json
import os
import sqlite3
import threading

from dpp import config
from dpp import crypto
from dpp import desensitize
from dpp import integrity

SENSITIVE_CAND_FIELDS = ("name", "id_card_no", "ticket_no", "seat_no", "face_image_path")


class ReadOnlyViolation(Exception):
    """
    试图修改已加密数据时抛出。

    本系统的设计是"任何加密数据不可修改"：
    你只能**删除**（DELETE）或**追加新数据**（INSERT）。
    这不是"提醒"，而是硬拒绝 —— 抛异常 + 留审计 + 数据库触发器兜底。
    """
    pass


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

    # ---------------- 建表 ----------------
    def _init(self):
        with self._lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS candidates (
                cand_ref TEXT PRIMARY KEY, name TEXT, id_card_no TEXT, ticket_no TEXT,
                seat_no TEXT, room_id TEXT, site TEXT, face_template TEXT,
                face_image_path TEXT, captured_epoch REAL, retention_days REAL,
                legal_hold INTEGER DEFAULT 0, legal_hold_reason TEXT,
                consent_id TEXT, created_at TEXT, extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cand_room ON candidates(room_id);
            CREATE INDEX IF NOT EXISTS idx_cand_expire ON candidates(captured_epoch);

            CREATE TABLE IF NOT EXISTS consents (
                consent_id TEXT PRIMARY KEY, cand_ref TEXT, name TEXT, scope TEXT,
                method TEXT, operator TEXT, evidence_path TEXT, granted_epoch REAL,
                granted_at TEXT, withdrawn INTEGER DEFAULT 0, withdrawn_at TEXT,
                withdrawn_reason TEXT, extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_consent_ref ON consents(cand_ref, scope);

            CREATE TABLE IF NOT EXISTS uses (
                use_id TEXT PRIMARY KEY, cand_ref TEXT, purpose TEXT, action TEXT,
                operator TEXT, terminal TEXT, allowed INTEGER, deny_reason TEXT,
                ts TEXT, epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_use_purpose ON uses(purpose, epoch);

            CREATE TABLE IF NOT EXISTS reveals (
                reveal_id TEXT PRIMARY KEY, cand_ref TEXT, target TEXT, reason TEXT,
                operator TEXT, terminal TEXT, watermark TEXT, ts TEXT, epoch REAL,
                approved_by TEXT
            );

            CREATE TABLE IF NOT EXISTS shares (
                req_id TEXT PRIMARY KEY, cand_refs TEXT, fields TEXT, direction TEXT,
                purpose TEXT, reason TEXT, status TEXT, created_at TEXT,
                created_epoch REAL, applicant TEXT, approver TEXT, approved_at TEXT,
                approved_epoch REAL, reject_reason TEXT, export_path TEXT,
                row_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS destroys (
                destroy_id TEXT PRIMARY KEY, cand_ref TEXT, category TEXT, mode TEXT,
                reason TEXT, operator TEXT, witness TEXT, created_epoch REAL,
                covered TEXT, ts TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_destroy_mode ON destroys(mode, created_epoch);

            CREATE TABLE IF NOT EXISTS rights (
                req_id TEXT PRIMARY KEY, cand_ref TEXT, right_kind TEXT, detail TEXT,
                status TEXT, created_at TEXT, created_epoch REAL, due_epoch REAL,
                handler TEXT, handled_at TEXT, handled_epoch REAL, result TEXT,
                reason TEXT, channel TEXT, alt_channel_used INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY, site TEXT, name TEXT, kind TEXT,
                available INTEGER, last_drill_at TEXT, drill_passed INTEGER, note TEXT
            );

            CREATE TABLE IF NOT EXISTS measures (
                measure_id TEXT PRIMARY KEY, kind TEXT, title TEXT, owner TEXT,
                owner_role TEXT, status TEXT, due_at TEXT, done_at TEXT,
                evidence TEXT, detail TEXT
            );

            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY, level TEXT, title TEXT, detail TEXT,
                found_epoch REAL, reported_epoch REAL, handler TEXT, status TEXT,
                disposal TEXT, disposal_epoch REAL
            );

            CREATE TABLE IF NOT EXISTS audits (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT,
                detail TEXT, operator TEXT, prev_hash TEXT, hash TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

            -- 完整性签名台账：每条加密记录一行（SM3 摘要 + SM2/RSA 签名）
            -- record_ref 形如 "candidates:<cand_ref>" / "consents:<consent_id>"
            --           / "vault:<文件名>"
            CREATE TABLE IF NOT EXISTS signatures (
                record_ref TEXT PRIMARY KEY, kind TEXT, digest TEXT, signature TEXT,
                hash_alg TEXT, sig_alg TEXT, key_fp TEXT, signed_epoch REAL, signed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sig_kind ON signatures(kind);

            -- 只读不可改：数据库层兜底（连绕过业务直接 UPDATE 也被挡）
            -- 已存在的旧库没有触发器，CREATE TRIGGER IF NOT EXISTS 会补上。
            --
            -- 力度是**字段级**：涉及加密载荷/身份/生物特征的列一旦被改动就 ABORT；
            -- 只有 legal_hold（法律保留标记）、consent_id（同意绑定）这类
            -- "非加密的运行状态"允许在受控路径下更新。
            CREATE TRIGGER IF NOT EXISTS trg_cand_no_update
              BEFORE UPDATE ON candidates
              WHEN (
                   OLD.name             IS NOT NEW.name
                OR OLD.id_card_no        IS NOT NEW.id_card_no
                OR OLD.ticket_no         IS NOT NEW.ticket_no
                OR OLD.seat_no           IS NOT NEW.seat_no
                OR OLD.room_id           IS NOT NEW.room_id
                OR OLD.site              IS NOT NEW.site
                OR OLD.face_template     IS NOT NEW.face_template
                OR OLD.face_image_path   IS NOT NEW.face_image_path
                OR OLD.captured_epoch    IS NOT NEW.captured_epoch
                OR OLD.retention_days    IS NOT NEW.retention_days
                OR OLD.created_at        IS NOT NEW.created_at
              )
              BEGIN
                SELECT RAISE(ABORT,
                  '只读不可改：考生加密数据禁止修改（只能 DELETE 删除或 INSERT 新增）');
              END;

            -- 同意记录同理：加密载荷字段不许改
            CREATE TRIGGER IF NOT EXISTS trg_consent_no_update
              BEFORE UPDATE ON consents
              WHEN (
                   OLD.cand_ref          IS NOT NEW.cand_ref
                OR OLD.name              IS NOT NEW.name
                OR OLD.scope             IS NOT NEW.scope
                OR OLD.method            IS NOT NEW.method
                OR OLD.evidence_path     IS NOT NEW.evidence_path
                OR OLD.granted_epoch     IS NOT NEW.granted_epoch
              )
              BEGIN
                SELECT RAISE(ABORT,
                  '只读不可改：同意记录载荷禁止修改（只能 DELETE 或新增）');
              END;

            -- 签名台账自身也不许改（防止"改完数据顺手改签名"）
            CREATE TRIGGER IF NOT EXISTS trg_sig_no_update
              BEFORE UPDATE ON signatures
              WHEN (
                   OLD.record_ref IS NOT NEW.record_ref
                OR OLD.kind       IS NOT NEW.kind
                OR OLD.digest     IS NOT NEW.digest
                OR OLD.signature  IS NOT NEW.signature
                OR OLD.hash_alg   IS NOT NEW.hash_alg
                OR OLD.sig_alg    IS NOT NEW.sig_alg
              )
              BEGIN
                SELECT RAISE(ABORT, '只读不可改：签名台账禁止修改');
              END;
            """)

    # ---------------- 考生记录 ----------------
    # 不可修改的"加密载荷/身份/生物特征"列（改了就是篡改）
    IM_MUTABLE_CAND_FIELDS = ("name", "id_card_no", "ticket_no", "seat_no", "room_id",
                              "site", "face_template", "face_image_path",
                              "captured_epoch", "retention_days", "created_at")

    def candidate_exists(self, cand_ref):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT 1 FROM candidates WHERE cand_ref=? LIMIT 1",
                          (cand_ref,)).fetchone()
        return r is not None

    def save_candidate(self, cand, allow_overwrite=False):
        """
        登记（追加）一名考生。**已存在则拒绝**——加密数据不可就地改。

        如果你确实要"更正"考生信息，合规路径是：
          ① 删除原记录（走销毁/更正流程，留痕），② 再登记一条新的；
        绝不允许把原记录原地改写。allow_overwrite 仅供极少数内部迁移场景，
        且必须显式传 True（默认 False，即默认拒绝）。
        """
        if not allow_overwrite and self.candidate_exists(cand.cand_ref):
            self.log("登记被拒", "试图修改已存在的考生记录 %s（加密数据只读，"
                                 "只能删除或新增）" % cand.cand_ref)
            raise ReadOnlyViolation(
                "拒绝修改已加密的考生数据：%s 已存在。"
                "合规做法是先删除（DELETE）再新增，或直接以新 cand_ref 追加。"
                % cand.cand_ref)
        row = self._candidate_row(cand)
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        if allow_overwrite:
            sql = "INSERT OR REPLACE INTO candidates (%s) VALUES (%s)" % (cols, ph)
        else:
            # 用 INSERT（不是 REPLACE）：并发下唯一键冲突会直接报错，不会静默改写
            sql = "INSERT INTO candidates (%s) VALUES (%s)" % (cols, ph)
        with self._lock, self._conn() as c:
            c.execute(sql, list(row.values()))
        self.sign_record("candidates:%s" % cand.cand_ref, "candidate", row)
        return cand.cand_ref

    def _candidate_row(self, cand):
        return {
            "cand_ref": cand.cand_ref,
            "name": crypto.encrypt_field(cand.name),
            "id_card_no": crypto.encrypt_field(cand.id_card_no),
            "ticket_no": crypto.encrypt_field(cand.ticket_no),
            "seat_no": crypto.encrypt_field(cand.seat_no),
            "room_id": cand.room_id,                     # 考场号非直接标识，明文便于检索
            "site": cand.site,
            "face_template": cand.face_template,          # 已是不可逆摘要
            "face_image_path": crypto.encrypt_field(cand.face_image_path),
            "captured_epoch": cand.captured_epoch,
            "retention_days": cand.retention_days,
            "legal_hold": cand.legal_hold,
            "legal_hold_reason": cand.legal_hold_reason,
            "consent_id": cand.consent_id,
            "created_at": cand.created_at,
            "extra": json.dumps(cand.extra, ensure_ascii=False),
        }

    def update_candidate_meta(self, cand_ref, **fields):
        """
        **受控**更新考生的"非加密运行状态"字段（目前仅 legal_hold /
        legal_hold_reason / consent_id）。

        这是唯一允许触碰已存在考生行的路径，且：
          · 白名单外的字段直接拒绝（尤其加密载荷字段）；
          · 加密载荷字段由数据库触发器再兜一层，改了就 ABORT；
          · 每次更新都留审计。
        """
        allowed = {"legal_hold", "legal_hold_reason", "consent_id"}
        bad = [k for k in fields if k not in allowed]
        if bad:
            raise ReadOnlyViolation(
                "这些字段属于不可修改的加密数据，禁止更新：%s（仅允许改 %s）"
                % ("、".join(bad), "、".join(sorted(allowed))))
        if not fields:
            return True
        sets = ",".join("%s=?" % k for k in fields)
        args = list(fields.values()) + [cand_ref]
        with self._lock, self._conn() as c:
            c.execute("UPDATE candidates SET %s WHERE cand_ref=?" % sets, args)
        # 元数据更新后重签（内容变更属允许范围，但仍要保证签名与当前行一致）
        raw = self.raw_candidate_row(cand_ref)
        if raw is not None:
            self.sign_record("candidates:%s" % cand_ref, "candidate", raw)
        self.log("元数据更新", "%s 更新 %s" % (cand_ref, ",".join(fields.keys())))
        return True

    # ---------------- 完整性签名台账 ----------------
    def sign_record(self, record_ref, kind, payload):
        """
        为一条加密记录写签名（SM3 摘要 + SM2/RSA 签名）。
        payload 是入库后的**原始行**（含密文），保证签名绑定的是真正落库的内容。
        """
        if not integrity.available():
            return None
        text = integrity.canonical_record(
            [(k, "" if v is None else v) for k, v in payload.items()])
        dg = integrity.digest(text)
        sig = integrity.sign(text)
        from dpp import models
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO signatures (record_ref, kind, digest, "
                      "signature, hash_alg, sig_alg, key_fp, signed_epoch, signed_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (record_ref, kind, dg, sig,
                       integrity.hash_algorithm(), integrity.signature_algorithm(),
                       integrity.key_fingerprint(), models.now_epoch(), models.now_ts()))
        return dg

    def get_signature(self, record_ref):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM signatures WHERE record_ref=?",
                          (record_ref,)).fetchone()
        return dict(r) if r else None

    def list_signatures(self, kind=None, limit=5000):
        sql = "SELECT * FROM signatures WHERE 1=1"
        args = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY signed_epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def verify_record(self, record_ref):
        """
        验证单条记录是否被篡改。返回 (ok, 说明, detail)。
          ok=True  —— 摘要一致且验签通过（内容未被改动）
          ok=False —— 记录不存在签名 / 内容被改过 / 验签失败
        """
        sig_row = self.get_signature(record_ref)
        if sig_row is None:
            return False, "无签名记录（该记录未登记完整性签名）", {}
        kind = sig_row["kind"]
        if kind == "candidate":
            cand_ref = record_ref.split(":", 1)[1]
            row = self.raw_candidate_row(cand_ref)
            if row is None:
                return False, "记录已被删除（删除属允许操作，非篡改）", {"deleted": True}
            payload = {k: ("" if row.get(k) is None else row.get(k)) for k in row}
        elif kind == "consent":
            consent_id = record_ref.split(":", 1)[1]
            row = self.raw_consent_row(consent_id)
            if row is None:
                return False, "记录已被删除（删除属允许操作，非篡改）", {"deleted": True}
            payload = {k: ("" if row.get(k) is None else row.get(k)) for k in row}
        else:
            return True, "该类型记录不做内容级验签（仅登记签名）", {}
        text = integrity.canonical_record(list(payload.items()))
        dg = integrity.digest(text)
        digest_ok = (dg == sig_row["digest"])
        sig_ok = integrity.verify(text, sig_row["signature"])
        ok = digest_ok and sig_ok
        detail = {"digest_ok": digest_ok, "signature_ok": sig_ok,
                  "hash_alg": sig_row["hash_alg"], "sig_alg": sig_row["sig_alg"]}
        if ok:
            return True, "完整：摘要一致且验签通过", detail
        return False, "**被篡改**：摘要或签名与登记值不符", detail

    def verify_all_records(self, kinds=("candidate", "consent")):
        """
        全量核对完整性。返回 {total, ok, bad, deleted, missing_sig, details}
        bad 就是"被改过"的记录清单（这一项必须为 0 才算通过）。
        """
        total, ok_n, bad, deleted, missing = 0, 0, [], 0, 0
        details = []
        for sig_row in self.list_signatures(limit=10 ** 6):
            if sig_row["kind"] not in kinds:
                continue
            ref = sig_row["record_ref"]
            o, msg, d = self.verify_record(ref)
            total += 1
            if d.get("deleted"):
                deleted += 1
                continue
            if o:
                ok_n += 1
            else:
                bad.append(ref)
                details.append({"record_ref": ref, "reason": msg})
        return {"total": total, "ok": ok_n, "bad": bad, "bad_count": len(bad),
                "deleted": deleted, "details": details,
                "algorithm": integrity.integrity_algorithm_text()}

    def _dec_candidate(self, r):
        if r is None:
            return None
        from dpp import models
        d = dict(r)
        corrupted = []
        for f in SENSITIVE_CAND_FIELDS:
            raw = d.get(f) or ""
            if raw and not crypto.is_encrypted(raw):
                # 敏感列本应是密文；出现明文说明这行被人动过（或迁移出错）。
                # 同样降级处理，绝不把可疑明文当正常值返回。
                d[f] = "〈解密失败：密文异常〉"
                corrupted.append(f)
                continue
            try:
                d[f] = crypto.decrypt_field(raw)
            except Exception:                                # noqa: BLE001
                # 密文被篡改/密钥不匹配：**不能让整个列表查询崩掉**，
                # 否则连"发现异常"的机会都没有。这里降级为占位符并记录，
                # 真实结论由 verify_all_records() 的验签给出。
                d[f] = "〈解密失败：密文异常〉"
                corrupted.append(f)
        cand = models.Candidate(
            name=d.get("name") or "", id_card_no=d.get("id_card_no") or "",
            ticket_no=d.get("ticket_no") or "", seat_no=d.get("seat_no") or "",
            room_id=d.get("room_id") or "", site=d.get("site") or "",
            face_template=d.get("face_template") or "",
            face_image_path=d.get("face_image_path") or "",
            cand_ref=d.get("cand_ref"),
            captured_epoch=d.get("captured_epoch"),
            retention_days=d.get("retention_days"),
            consent_id=d.get("consent_id") or "",
            created_at=d.get("created_at"))
        cand.legal_hold = int(d.get("legal_hold") or 0)
        cand.legal_hold_reason = d.get("legal_hold_reason") or ""
        try:
            cand.extra = json.loads(d.get("extra") or "{}")
        except (ValueError, TypeError):
            cand.extra = {}
        if corrupted:
            cand.extra = dict(cand.extra or {})
            cand.extra["_corrupted_fields"] = corrupted
        return cand

    def get_candidate(self, cand_ref):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM candidates WHERE cand_ref=?",
                          (cand_ref,)).fetchone()
        return self._dec_candidate(r)

    def list_candidates(self, room_id=None, limit=1000):
        sql = "SELECT * FROM candidates WHERE 1=1"
        args = []
        if room_id:
            sql += " AND room_id=?"
            args.append(room_id)
        sql += " ORDER BY room_id, cand_ref LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._dec_candidate(r) for r in rows]

    def raw_candidate_row(self, cand_ref):
        """取原始行（密文态），验收"加密率 100%"时逐列核对用。"""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM candidates WHERE cand_ref=?",
                          (cand_ref,)).fetchone()
        return dict(r) if r else None

    def delete_candidate(self, cand_ref):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM candidates WHERE cand_ref=?", (cand_ref,))
            c.execute("DELETE FROM consents WHERE cand_ref=?", (cand_ref,))

    def expired_candidates(self, now=None, grace_days=None):
        """取到期应销毁的考生记录（有法律保留必要的不在其中）。"""
        import time
        now = now or time.time()
        grace = config.DESTROY["grace_days"] if grace_days is None else grace_days
        out = []
        for cand in self.list_candidates(limit=10 ** 6):
            if cand.legal_hold or cand.retention_days is None:
                continue
            t = cand.destroy_at_epoch + float(grace) * 86400.0
            if now >= t:
                out.append(cand)
        return out

    # ---------------- 同意 ----------------
    def save_consent(self, cs, allow_overwrite=False):
        """
        写入同意记录。已存在（同一 consent_id）则拒绝就地修改；
        "撤回同意"不走这里改原记录，而是追加一条 withdrawn 状态的新记录
        （见 rights.py），保持 append-only。
        """
        if not allow_overwrite and self.consent_exists(cs.consent_id):
            self.log("同意被拒", "试图修改已存在的同意记录 %s（只读，只能删或新增）"
                               % cs.consent_id)
            raise ReadOnlyViolation(
                "拒绝修改已存在的同意记录：%s（只能删除或追加新记录）" % cs.consent_id)
        row = cs.to_row()
        row["name"] = crypto.encrypt_field(row.get("name") or "")
        row["evidence_path"] = crypto.encrypt_field(row.get("evidence_path") or "")
        row["withdrawn_reason"] = crypto.encrypt_field(row.get("withdrawn_reason") or "")
        cols = ",".join(row.keys())
        ph = ",".join("?" * len(row))
        sql = ("INSERT OR REPLACE INTO consents (%s) VALUES (%s)" % (cols, ph)
               if allow_overwrite else
               "INSERT INTO consents (%s) VALUES (%s)" % (cols, ph))
        with self._lock, self._conn() as c:
            c.execute(sql, list(row.values()))
        self.sign_record("consents:%s" % cs.consent_id, "consent", row)
        return cs.consent_id

    def consent_exists(self, consent_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT 1 FROM consents WHERE consent_id=? LIMIT 1",
                          (consent_id,)).fetchone()
        return r is not None

    def bind_consent_subject(self, consent_id, cand_ref):
        """
        **受控**把同意记录绑定到考生（同意先于采集建立时，cand_ref 是占位空值）。

        只允许在"原值为空"时补填一次，且绑定后不可再改 —— 这样既满足
        "同意必须能反查同一考生"，又不破坏 append-only。
        """
        cur = self.raw_consent_row(consent_id)
        if cur is None:
            return False
        old = cur.get("cand_ref") or ""
        if old and old != cand_ref:
            self.log("同意被拒", "禁止改写已绑定的同意主体 %s：%s -> %s"
                               % (consent_id, old, cand_ref))
            raise ReadOnlyViolation(
                "同意记录 %s 已绑定考生 %s，禁止改写为 %s（只能新增同意记录）"
                % (consent_id, old, cand_ref))
        if old == cand_ref:
            return True
        with self._lock, self._conn() as c:
            c.execute("UPDATE consents SET cand_ref=? WHERE consent_id=?",
                      (cand_ref, consent_id))
        raw = self.raw_consent_row(consent_id)
        if raw is not None:
            self.sign_record("consents:%s" % consent_id, "consent", raw)
        self.log("同意绑定", "%s 绑定考生 %s（原为空占位）" % (consent_id, cand_ref))
        return True

    def withdraw_consent(self, consent_id, reason=""):
        """**受控**撤回同意：只允许 withdrawn 0->1 的单向切换，且不可回滚。"""
        cur = self.raw_consent_row(consent_id)
        if cur is None:
            return False
        if int(cur.get("withdrawn") or 0) == 1:
            return True
        from dpp import models
        with self._lock, self._conn() as c:
            c.execute("UPDATE consents SET withdrawn=1, withdrawn_at=?, "
                      "withdrawn_reason=? WHERE consent_id=?",
                      (models.now_ts(),
                       crypto.encrypt_field(reason or "考生行使撤回同意权"),
                       consent_id))
        raw = self.raw_consent_row(consent_id)
        if raw is not None:
            self.sign_record("consents:%s" % consent_id, "consent", raw)
        self.log("撤回同意", "%s 已撤回（不可回滚）" % consent_id)
        return True

    def raw_consent_row(self, consent_id):
        """取原始同意行（密文态），完整性核对用。"""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM consents WHERE consent_id=?",
                          (consent_id,)).fetchone()
        return dict(r) if r else None

    def _dec_consent(self, r):
        if r is None:
            return None
        from dpp import models
        d = dict(r)
        for f in ("name", "evidence_path", "withdrawn_reason"):
            raw = d.get(f) or ""
            if raw and not crypto.is_encrypted(raw):
                d[f] = "〈解密失败：密文异常〉"      # 敏感列出现明文 = 被动过
                continue
            try:
                d[f] = crypto.decrypt_field(raw)
            except Exception:                                # noqa: BLE001
                d[f] = "〈解密失败：密文异常〉"
        return models.Consent.from_row(d)

    def valid_consent(self, cand_ref, scope="face"):
        """取该考生该用途下**未撤回**的同意记录；没有则返回 None。"""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM consents WHERE cand_ref=? AND scope=? "
                          "AND withdrawn=0 ORDER BY granted_epoch DESC LIMIT 1",
                          (cand_ref, scope)).fetchone()
        return self._dec_consent(r)

    def list_consents(self, limit=1000):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM consents ORDER BY granted_epoch DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [self._dec_consent(r) for r in rows]

    # ---------------- 使用记录 ----------------
    def save_use(self, u):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO uses (use_id, cand_ref, purpose, action, operator, "
                      "terminal, allowed, deny_reason, ts, epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (u.use_id, u.cand_ref, u.purpose, u.action, u.operator,
                       u.terminal, u.allowed, u.deny_reason, u.ts, u.epoch))

    def list_uses(self, limit=2000):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM uses ORDER BY epoch DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 解密查看 ----------------
    def save_reveal(self, rv):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO reveals (reveal_id, cand_ref, target, reason, "
                      "operator, terminal, watermark, ts, epoch, approved_by) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rv.reveal_id, rv.cand_ref, rv.target, rv.reason, rv.operator,
                       rv.terminal, rv.watermark, rv.ts, rv.epoch, rv.approved_by))

    def list_reveals(self, limit=500):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM reveals ORDER BY epoch DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 共享/导出 ----------------
    def save_share(self, sh):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO shares (req_id, cand_refs, fields, "
                      "direction, purpose, reason, status, created_at, created_epoch, "
                      "applicant, approver, approved_at, approved_epoch, reject_reason, "
                      "export_path, row_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sh.req_id, json.dumps(sh.cand_refs, ensure_ascii=False),
                       json.dumps(sh.fields, ensure_ascii=False), sh.direction,
                       sh.purpose, sh.reason, sh.status, sh.created_at,
                       sh.created_epoch, sh.applicant, sh.approver, sh.approved_at,
                       sh.approved_epoch, sh.reject_reason, sh.export_path,
                       sh.row_count))

    def _dec_share(self, r):
        d = dict(r)
        for f in ("cand_refs", "fields"):
            try:
                d[f] = json.loads(d.get(f) or "[]")
            except (ValueError, TypeError):
                d[f] = []
        return d

    def get_share(self, req_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM shares WHERE req_id=?", (req_id,)).fetchone()
        return self._dec_share(r) if r else None

    def list_shares(self, limit=500):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM shares ORDER BY created_epoch DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [self._dec_share(r) for r in rows]

    # ---------------- 销毁 ----------------
    def save_destroy(self, d):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO destroys (destroy_id, cand_ref, category, mode, "
                      "reason, operator, witness, created_epoch, covered, ts) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (d.destroy_id, d.cand_ref, d.category, d.mode, d.reason,
                       d.operator, d.witness, d.created_epoch,
                       json.dumps(d.covered, ensure_ascii=False), d.ts))

    def list_destroys(self, mode=None, limit=2000):
        sql = "SELECT * FROM destroys WHERE 1=1"
        args = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        sql += " ORDER BY created_epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 权利申请 ----------------
    def save_rights(self, rq):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO rights (req_id, cand_ref, right_kind, "
                      "detail, status, created_at, created_epoch, due_epoch, handler, "
                      "handled_at, handled_epoch, result, reason, channel, "
                      "alt_channel_used) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rq.req_id, rq.cand_ref, rq.right, rq.detail, rq.status,
                       rq.created_at, rq.created_epoch, rq.due_epoch, rq.handler,
                       rq.handled_at, rq.handled_epoch, rq.result, rq.reason,
                       rq.channel, rq.alt_channel_used))

    def _dec_rights(self, r):
        from dpp import models
        d = dict(r)
        obj = models.RightsRequest(d.get("cand_ref") or "", d.get("right_kind") or "",
                                   detail=d.get("detail") or "",
                                   channel=d.get("channel") or "")
        obj.req_id = d.get("req_id")
        obj.status = d.get("status") or "open"
        obj.created_at = d.get("created_at") or ""
        obj.created_epoch = float(d.get("created_epoch") or 0)
        obj.due_epoch = float(d.get("due_epoch") or 0)
        obj.handler = d.get("handler") or ""
        obj.handled_at = d.get("handled_at") or ""
        obj.handled_epoch = float(d.get("handled_epoch") or 0)
        obj.result = d.get("result") or ""
        obj.reason = d.get("reason") or ""
        obj.alt_channel_used = int(d.get("alt_channel_used") or 0)
        return obj

    def get_rights(self, req_id):
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM rights WHERE req_id=?", (req_id,)).fetchone()
        return self._dec_rights(r) if r else None

    def list_rights(self, status=None, limit=1000):
        sql = "SELECT * FROM rights WHERE 1=1"
        args = []
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_epoch DESC LIMIT ?"
        args.append(int(limit))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._dec_rights(r) for r in rows]

    # ---------------- 替代通道 ----------------
    def save_channel(self, ch):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO channels (channel_id, site, name, kind, "
                      "available, last_drill_at, drill_passed, note) VALUES (?,?,?,?,?,?,?,?)",
                      (ch.channel_id, ch.site, ch.name, ch.kind, ch.available,
                       ch.last_drill_at, ch.drill_passed, ch.note))

    def list_channels(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM channels ORDER BY site").fetchall()
        return [dict(r) for r in rows]

    # ---------------- 管理措施 / 事件 ----------------
    def save_measure(self, m):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO measures (measure_id, kind, title, owner, "
                      "owner_role, status, due_at, done_at, evidence, detail) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (m.measure_id, m.kind, m.title, m.owner, m.owner_role,
                       m.status, m.due_at, m.done_at, m.evidence, m.detail))

    def list_measures(self, kind=None):
        sql = "SELECT * FROM measures"
        args = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY kind, title"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def save_incident(self, inc):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO incidents (incident_id, level, title, "
                      "detail, found_epoch, reported_epoch, handler, status, disposal, "
                      "disposal_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (inc.incident_id, inc.level, inc.title, inc.detail,
                       inc.found_epoch, inc.reported_epoch, inc.handler, inc.status,
                       inc.disposal, inc.disposal_epoch))

    def list_incidents(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM incidents ORDER BY found_epoch DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------------- 审计哈希链 ----------------
    def log(self, action, detail="", operator=""):
        if config.LOG_SANITIZE:
            detail = desensitize.sanitize_for_log(detail)
        with self._lock, self._conn() as c:
            last = c.execute("SELECT hash FROM audits ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else "GENESIS"
            from dpp import models
            ts = models.now_ts()
            payload = {"ts": ts, "action": action, "detail": detail, "operator": operator}
            h = models.chain_hash(prev, payload)
            c.execute("INSERT INTO audits (ts, action, detail, operator, prev_hash, hash) "
                      "VALUES (?,?,?,?,?,?)", (ts, action, detail, operator, prev, h))
        return h

    def audit_rows(self, limit=300):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audits ORDER BY seq DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def verify_chain(self):
        """校验审计链完整性；返回 (ok, 说明, 条数)"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM audits ORDER BY seq ASC").fetchall()
        from dpp import models
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

    def audit_actions(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT action, COUNT(*) n FROM audits GROUP BY action").fetchall()
        return {r["action"]: r["n"] for r in rows}

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

    # ---------------- 统计 ----------------
    def summary(self):
        with self._lock, self._conn() as c:
            n_cand = c.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
            n_consent = c.execute("SELECT COUNT(*) n FROM consents").fetchone()["n"]
            n_withdrawn = c.execute("SELECT COUNT(*) n FROM consents "
                                    "WHERE withdrawn=1").fetchone()["n"]
            n_destroy = c.execute("SELECT COUNT(*) n FROM destroys").fetchone()["n"]
            n_auto = c.execute("SELECT COUNT(*) n FROM destroys WHERE mode='auto'"
                               ).fetchone()["n"]
            n_reveal = c.execute("SELECT COUNT(*) n FROM reveals").fetchone()["n"]
            n_share = c.execute("SELECT COUNT(*) n FROM shares").fetchone()["n"]
            n_open_rights = c.execute("SELECT COUNT(*) n FROM rights WHERE status='open'"
                                      ).fetchone()["n"]
            n_audit = c.execute("SELECT COUNT(*) n FROM audits").fetchone()["n"]
            n_hold = c.execute("SELECT COUNT(*) n FROM candidates WHERE legal_hold=1"
                               ).fetchone()["n"]
        return {"考生数": n_cand, "同意记录": n_consent, "已撤回同意": n_withdrawn,
                "销毁总数": n_destroy, "其中自动销毁": n_auto,
                "解密查看次数": n_reveal, "共享申请": n_share,
                "待办权利申请": n_open_rights, "审计条数": n_audit,
                "法律保留中": n_hold}
