# -*- coding: utf-8 -*-
r"""
完整性保护用例：哈希（SM3）+ 非对称签名（SM2）+ 加密数据只读不可改。

覆盖用户的核心要求：
  "任何人都不能动加密的数据（加密由哈希 + 非对称算法组成），
   最多只能进行删除或上传新的数据。"

用例分四组：
  A. 算法与密钥（签名后端可用、算法如实标注、签名验签真实有效）
  B. 摘要与规范化（数值归一化不导致假失败、字段顺序不影响摘要）
  C. 登记即签名 + 全量验签（追加合法、改库必被检出）
  D. 只读不可改（save_candidate 拒绝覆盖 / update_candidate_meta 白名单 /
     数据库触发器兜底 / 删除依旧是允许操作）
"""
import sqlite3

from tests._base import DppTestCase
from dpp import config, integrity, models, storage


class TestIntegrityAlgorithm(DppTestCase):
    """A. 算法与密钥"""

    def test_backend_available(self):
        self.assertTrue(integrity.available(),
                        "既没有 gmssl 也没有 cryptography：完整性保护无法落地")

    def test_algorithm_is_honest(self):
        """算法名必须与实际后端一致，绝不把 SHA-256 冒充 SM3。"""
        h = integrity.hash_algorithm()
        s = integrity.signature_algorithm()
        self.assertIn(h, ("sm3", "sha256"))
        self.assertIn(s, ("sm2", "rsa2048"))
        text = integrity.integrity_algorithm_text()
        if h == "sm3":
            self.assertIn("SM3", text)
        else:
            self.assertIn("SHA-256", text)
        if s == "sm2":
            self.assertIn("SM2", text)
        else:
            self.assertIn("RSA", text)

    def test_selftest_passes(self):
        ok, msg = integrity.selftest()
        self.assertTrue(ok, msg)

    def test_signature_rejects_tamper(self):
        payload = {"rec": "C001", "name": "张三"}
        sig = integrity.sign(payload)
        self.assertTrue(integrity.verify(payload, sig))
        self.assertFalse(integrity.verify({"rec": "C001", "name": "李四"}, sig),
                         "改了一个字竟然还能验签通过")

    def test_private_key_not_plaintext_on_disk(self):
        integrity.sign_key()
        p = integrity._sign_key_path()
        self.assertTrue(p.exists(), "签名私钥未落盘")
        blob = p.read_bytes()
        priv, _ = integrity.sign_key()
        self.assertNotIn(priv, blob, "私钥以明文形式写入了磁盘（应被安全存储包裹）")


class TestCanonical(DppTestCase):
    """B. 摘要与规范化"""

    def test_field_order_does_not_matter(self):
        a = integrity.canonical_record([("b", 2), ("a", 1)])
        b = integrity.canonical_record([("a", 1), ("b", 2)])
        self.assertEqual(a, b)

    def test_int_float_normalization(self):
        """SQLite 读回的是 float，Python 传的是 int，摘要必须一致。"""
        a = integrity.canonical_record([("retention_days", 30)])
        b = integrity.canonical_record([("retention_days", 30.0)])
        self.assertEqual(a, b)
        self.assertEqual(integrity.digest(a), integrity.digest(b))

    def test_non_integral_float_kept(self):
        self.assertNotEqual(
            integrity.canonical_record([("x", 1.5)]),
            integrity.canonical_record([("x", 1)]))


class TestSignOnWrite(DppTestCase):
    """C. 登记即签名 + 全量验签"""

    def test_collect_writes_signature(self):
        res = self.collect_one()
        cand_ref = res["cand_ref"]
        row = self.store.get_signature("candidates:%s" % cand_ref)
        self.assertIsNotNone(row, "登记后没有落完整性签名")
        self.assertEqual(row["kind"], "candidate")
        self.assertTrue(row["signature"])
        self.assertTrue(row["digest"])

    def test_consent_writes_signature(self):
        cs = self.make_consent()
        row = self.store.get_signature("consents:%s" % cs.consent_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "consent")

    def test_verify_all_clean(self):
        self.collect_one(ticket_no="T0001", seat_no="01")
        self.collect_one(name="考生二", id_card_no="330100200801015678",
                         ticket_no="T0002", seat_no="02")
        va = self.store.verify_all_records()
        self.assertEqual(va["bad_count"], 0, "全新登记的记录竟然验签失败：%s" % va)
        self.assertGreaterEqual(va["ok"], 2)

    def test_direct_db_tamper_detected(self):
        """绕过业务层直接改库中的密文：必须被全量验签检出。"""
        res = self.collect_one()
        ref = res["cand_ref"]
        self.assertEqual(self.store.verify_all_records()["bad_count"], 0)

        # 攻击者直接 UPDATE（先卸掉触发器，模拟最强攻击者）
        con = sqlite3.connect(config.DB_PATH)
        try:
            con.execute("DROP TRIGGER IF EXISTS trg_cand_no_update")
            con.execute("UPDATE candidates SET name=? WHERE cand_ref=?",
                        ("eBogusCipherText==", ref))
            con.commit()
        finally:
            con.close()

        va = self.store.verify_all_records()
        self.assertEqual(va["bad_count"], 1,
                         "直接改库竟未被检出：%s" % va)
        self.assertIn("candidates:%s" % ref, va["bad"])

    def test_list_survives_corrupted_row(self):
        """一条记录密文坏了，不能让整表查询崩掉（应降级为占位文本）。"""
        res = self.collect_one(ticket_no="T9001", seat_no="09")
        ref = res["cand_ref"]
        con = sqlite3.connect(config.DB_PATH)
        try:
            con.execute("DROP TRIGGER IF EXISTS trg_cand_no_update")
            con.execute("UPDATE candidates SET name=? WHERE cand_ref=?",
                        ("@@not-a-cipher@@", ref))
            con.commit()
        finally:
            con.close()
        rows = self.store.list_candidates()
        self.assertEqual(len(rows), 1)
        self.assertIn("解密失败", rows[0].name)


class TestReadOnly(DppTestCase):
    """D. 只读不可改（加密数据只能删 / 增）"""

    def test_save_candidate_rejects_existing(self):
        res = self.collect_one()
        ref = res["cand_ref"]
        old = self.store.get_candidate(ref)
        # 拿同 ref 再存一遍 = 试图修改
        with self.assertRaises(storage.ReadOnlyViolation):
            self.store.save_candidate(old)

    def test_update_meta_whitelist(self):
        res = self.collect_one()
        ref = res["cand_ref"]
        # 允许：法律保留
        self.store.update_candidate_meta(ref, legal_hold=1,
                                         legal_hold_reason="涉诉")
        rec = self.store.get_candidate(ref)
        self.assertEqual(int(rec.legal_hold), 1)
        # 拒绝：改名（加密载荷）
        with self.assertRaises(storage.ReadOnlyViolation):
            self.store.update_candidate_meta(ref, name="改名试试")
        # 拒绝：改证件号
        with self.assertRaises(storage.ReadOnlyViolation):
            self.store.update_candidate_meta(ref, id_card_no="x")

    def test_db_trigger_blocks_direct_update(self):
        """触发器在，直接改加密字段会被数据库层 ABORT。"""
        res = self.collect_one()
        ref = res["cand_ref"]
        con = sqlite3.connect(config.DB_PATH)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE candidates SET name=? WHERE cand_ref=?",
                            ("X", ref))
        finally:
            con.close()

    def test_delete_is_allowed(self):
        res = self.collect_one()
        ref = res["cand_ref"]
        self.store.delete_candidate(ref)
        self.assertIsNone(self.store.get_candidate(ref))
        # 删除后验签应报"已删除"（属允许操作，不计入 bad）
        va = self.store.verify_all_records()
        self.assertEqual(va["bad_count"], 0)
        self.assertGreaterEqual(va["deleted"], 1)

    def test_correct_is_delete_plus_insert(self):
        """更正权的落地 = 删旧 + 增新，原 ref 消失、新 ref 出现。"""
        res = self.collect_one()
        old_ref = res["cand_ref"]
        owner = self.login("owner")
        out = self.rights.submit(owner, old_ref, "correct",
                                 detail="考生申请更正考场座位号",
                                 channel="现场受理", identity_verified=True)
        rid = out["req_id"]
        out2 = self.rights.handle(owner, rid, corrected={"seat_no": "88"})
        self.assertEqual(out2["status"], "done")
        self.assertIsNone(self.store.get_candidate(old_ref),
                          "更正走'删除+新增'，原记录应已删除")
        new_ref = out2.get("new_cand_ref")
        self.assertTrue(new_ref, "更正应产生新的 cand_ref")
        self.assertEqual(self.store.get_candidate(new_ref).seat_no, "88")
        # 新记录同样带完整性签名，且全量验签不因更正而报错
        self.assertIsNotNone(self.store.get_signature("candidates:%s" % new_ref))
        self.assertEqual(self.store.verify_all_records()["bad_count"], 0)


class TestIntegrationApiFormat(DppTestCase):
    """E. 集成接口：JSON 结构与只读语义"""

    def test_integration_api_ops_shape(self):
        import json
        import subprocess
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["EXAM_DPP_HOME"] = self.tmp
        # 先把两条记录灌进沙箱
        self.collect_one(ticket_no="TL001", seat_no="01")
        p = subprocess.run(
            [sys.executable, os.path.join(root, "integration_api.py"),
             "--op", "verify_data", "--args", "{}"],
            capture_output=True, env=env, cwd=root, timeout=120)
        last = [ln for ln in p.stdout.decode("utf-8", "replace").splitlines()
                if ln.strip().startswith("{")][-1]
        data = json.loads(last)
        self.assertTrue(data["ok"])
        self.assertEqual(data["bad_count"], 0)
        self.assertIsInstance(data["ok"], bool)

    def test_integration_api_readonly_probe(self):
        import json
        import subprocess
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["EXAM_DPP_HOME"] = self.tmp
        self.collect_one(ticket_no="TL002", seat_no="02")
        p = subprocess.run(
            [sys.executable, os.path.join(root, "integration_api.py"),
             "--op", "readonly_probe", "--args", "{}"],
            capture_output=True, env=env, cwd=root, timeout=120)
        last = [ln for ln in p.stdout.decode("utf-8", "replace").splitlines()
                if ln.strip().startswith("{")][-1]
        data = json.loads(last)
        self.assertTrue(data["ok"])
        self.assertTrue(data["blocked"], "只读探针没能拦住修改：%s" % data)

    def test_integration_api_status_reports_algorithms(self):
        import json
        import subprocess
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["EXAM_DPP_HOME"] = self.tmp
        p = subprocess.run(
            [sys.executable, os.path.join(root, "integration_api.py"),
             "--op", "status", "--args", "{}"],
            capture_output=True, env=env, cwd=root, timeout=120)
        last = [ln for ln in p.stdout.decode("utf-8", "replace").splitlines()
                if ln.strip().startswith("{")][-1]
        data = json.loads(last)
        self.assertTrue(data["ok"])
        self.assertTrue(data["readonly"])
        self.assertTrue(data["integrity_algorithm"])
        self.assertTrue(data["encrypt_algorithm"])

    def test_integration_api_foreign_operator_falls_back_to_owner(self):
        """联动侧传业务标识（如 linkage）而非账户名时，应回退 owner 而非报错。"""
        import json
        import subprocess
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["EXAM_DPP_HOME"] = self.tmp
        p = subprocess.run(
            [sys.executable, os.path.join(root, "integration_api.py"),
             "--op", "protect",
             "--args", json.dumps({"name": "联动考生", "ticket_no": "TLX01",
                                   "seat_no": "05", "room_id": "A-09",
                                   "operator": "linkage"}, ensure_ascii=False)],
            capture_output=True, env=env, cwd=root, timeout=120)
        last = [ln for ln in p.stdout.decode("utf-8", "replace").splitlines()
                if ln.strip().startswith("{")][-1]
        data = json.loads(last)
        self.assertTrue(data.get("ok"), "联动 protect 失败：%s" % data)
        self.assertTrue(data.get("signed"))
