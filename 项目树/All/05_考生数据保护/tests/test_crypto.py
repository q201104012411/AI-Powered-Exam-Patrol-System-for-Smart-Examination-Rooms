# -*- coding: utf-8 -*-
"""加密与不可逆模板（《考生数据保护.md》第三节·工程技术措施）单元测试"""
import os
import unittest

from tests._base import DppTestCase
from dpp import catalog, crypto, storage


class TestCrypto(DppTestCase):

    def test_algorithm_is_national_or_declared(self):
        """必须有一个可用后端；且报告出来的算法名与真实后端一致（不许谎报国密）。"""
        alg = crypto.active_algorithm()
        self.assertIn(alg, (crypto.ALG_SM4, crypto.ALG_AES), "无可用加密后端")
        text = crypto.algorithm_text()
        if alg == crypto.ALG_SM4:
            self.assertIn("SM4", text)
            self.assertIn("国密", text)
        else:
            self.assertIn("AES", text)
            # 非国密时必须如实标注，不能写成"国密"
            self.assertNotIn("国密", text, "后端是 AES 却宣称国密，属谎报")

    def test_roundtrip_text(self):
        plain = "330100200801011234"
        enc = crypto.encrypt_field(plain)
        self.assertTrue(crypto.is_encrypted(enc))
        self.assertNotIn(plain, enc, "密文里不应出现明文")
        self.assertEqual(crypto.decrypt_field(enc), plain)

    def test_roundtrip_bytes_with_chinese(self):
        raw = "张三 · 一中考点 A-01".encode("utf-8")
        enc = crypto.encrypt_bytes(raw)
        self.assertTrue(crypto.is_encrypted(enc))
        self.assertEqual(crypto.decrypt_bytes(enc), raw)

    def test_empty_value_passthrough(self):
        """空值不加密也不报错（允许缺字段，但不允许把空值当密文）。"""
        self.assertEqual(crypto.encrypt_field(""), "")
        self.assertEqual(crypto.encrypt_field(None), None)
        self.assertEqual(crypto.decrypt_field(None), None)

    def test_encrypt_is_idempotent(self):
        """对已加密值再次加密应原样返回，避免"双重加密"导致解密失败。"""
        enc = crypto.encrypt_field("abc")
        self.assertEqual(crypto.encrypt_field(enc), enc)

    def test_decrypt_plaintext_passthrough(self):
        """明文兼容：老数据未加密时也不应崩溃。"""
        self.assertEqual(crypto.decrypt_field("plain-value"), "plain-value")

    def test_ciphertext_differs_each_time(self):
        """同一明文两次加密密文应不同（CBC/GCM 随机 IV），防止等值推断。"""
        a = crypto.encrypt_field("same-value")
        b = crypto.encrypt_field("same-value")
        self.assertNotEqual(a, b)

    def test_tampered_ciphertext_rejected(self):
        """篡改密文必须被完整性校验发现，不能静默解出错误明文。"""
        enc = crypto.encrypt_field("sensitive")
        body = enc[len("enc:"):]
        alg, rest = body.split("|", 1)
        # 反转最后一段（tag/密文）的若干字符
        tail = rest[-8:]
        broken_tail = "".join(
            ("0" if ch != "0" else "1") for ch in tail)
        tampered = "enc:" + alg + "|" + rest[:-8] + broken_tail
        with self.assertRaises(Exception):
            crypto.decrypt_field(tampered)

    def test_irreversible_template_not_reversible(self):
        """特征模板必须不可逆：前缀 tpl1:，且明文不出现在模板里。"""
        tpl = crypto.irreversible_template("feature-vector-王五")
        self.assertTrue(tpl.startswith("tpl1:"))
        self.assertNotIn("王五", tpl)
        self.assertNotIn("feature-vector", tpl)
        # 不可逆 = 没有 decrypt 能还原（结构上就不带 enc: 前缀）
        self.assertFalse(crypto.is_encrypted(tpl))

    def test_irreversible_template_deterministic(self):
        """同一特征稳定得同一模板（比对要用），不同特征不得碰撞。"""
        a1 = crypto.irreversible_template("vec-1")
        a2 = crypto.irreversible_template("vec-1")
        b = crypto.irreversible_template("vec-2")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)

    def test_key_fingerprint_stable(self):
        """主密钥指纹在进程内稳定，且不为空。"""
        crypto.master_key()
        f1 = crypto.key_fingerprint()
        self.assertEqual(f1, crypto.key_fingerprint())
        self.assertNotEqual(f1, "-")

    def test_key_rotated_changes_fingerprint(self):
        """轮换主密钥后指纹必须变化（否则等于没换）。"""
        old = crypto.key_fingerprint()
        crypto.rotate_key()
        crypto.reset_cache()
        new = crypto.key_fingerprint()
        self.assertNotEqual(old, new)


class TestEncryptionAtRest(DppTestCase):
    """落库即密文：直接读原始表，敏感列的存储值不能是明文。"""

    def test_candidate_sensitive_fields_are_ciphertext_in_db(self):
        res = self.collect_one(name="张三", id_card_no="330100200801011234",
                               ticket_no="2026T0001", seat_no="01",
                               room_id="A-01", site="一中考点")
        row = self.store.raw_candidate_row(res["cand_ref"])
        self.assertIsNotNone(row)
        for field in storage.SENSITIVE_CAND_FIELDS:
            val = row[field] if not isinstance(row, dict) else row.get(field)
            if val:
                self.assertTrue(crypto.is_encrypted(val),
                                "字段 %s 未加密落库：%r" % (field, val))
        # 明文不得出现在原始行里
        blob = "".join(str(v) for v in (row.values() if isinstance(row, dict) else row))
        self.assertNotIn("张三", blob)
        self.assertNotIn("330100200801011234", blob)

    def test_read_path_decrypts_transparently(self):
        res = self.collect_one(name="李四", id_card_no="330100200802022345",
                               ticket_no="2026T0002")
        cand = self.store.get_candidate(res["cand_ref"])
        self.assertEqual(cand.name, "李四")
        self.assertEqual(cand.id_card_no, "330100200802022345")

    def test_face_template_stored_irreversible(self):
        res = self.collect_one(name="王五")
        row = self.store.raw_candidate_row(res["cand_ref"])
        tpl = row["face_template"] if not isinstance(row, dict) else row["face_template"]
        self.assertTrue(str(tpl).startswith("tpl1:"),
                        "人脸特征模板必须以不可逆模板存储")

    def test_storage_audit_rate_full(self):
        """逐类别逐字段核对，敏感数据加密率必须 100%。"""
        self.collect_one(name="赵六", with_image=True)
        sa = self.life.storage_audit()
        self.assertGreater(sa["checked"], 0)
        self.assertEqual(sa["missing"], [])
        self.assertEqual(sa["rate"], 1.0)

    def test_catalog_fields_all_covered(self):
        """目录里登记"必须加密"的字段，验收核对时都要被覆盖到。"""
        self.collect_one(name="钱七", with_image=True)
        sa = self.life.storage_audit()
        checked = {(d["category"], d["field"]) for d in sa["details"]}
        for k in catalog.keys():
            for f in catalog.required_encrypted_fields(k):
                self.assertIn((k, f), checked,
                              "类别 %s 字段 %s 未纳入加密核对" % (k, f))


if __name__ == "__main__":
    unittest.main(verbosity=2)
