# -*- coding: utf-8 -*-
"""考生敏感数据加密存储单元测试"""
import os
import sqlite3
import tempfile
import unittest

import config
import data_crypto
import store


class TestDataCrypto(unittest.TestCase):

    def test_field_roundtrip(self):
        plain = "张三 / 2026T001 / A-01"
        cipher = data_crypto.encrypt_field(plain)
        self.assertTrue(cipher.startswith("enc:"))
        self.assertNotEqual(cipher, plain)
        self.assertEqual(data_crypto.decrypt_field(cipher), plain)

    def test_empty_and_plaintext_passthrough(self):
        self.assertEqual(data_crypto.encrypt_field(""), "")
        self.assertEqual(data_crypto.decrypt_field("plain"), "plain")

    def test_bytes_roundtrip(self):
        data = b"\xff\xd8\xff\xe0 fake image bytes"
        cipher = data_crypto.encrypt_bytes(data)
        self.assertEqual(data_crypto.decrypt_bytes(cipher), data)


class TestCandidateStoreEncryption(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        # 避免污染默认数据目录的密钥文件
        from cryptography.fernet import Fernet
        os.environ["EXAM_DATA_KEY"] = Fernet.generate_key().decode("utf-8")

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass
        os.environ.pop("EXAM_DATA_KEY", None)

    def _raw_values(self, candidate_id):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT name, ticket_no, seat_no, reference_photo FROM candidates "
                "WHERE candidate_id=?", (candidate_id,)).fetchone()
            return tuple(row) if row else (None, None, None, None)
        finally:
            conn.close()

    def test_candidate_fields_encrypted_at_rest_decrypted_on_load(self):
        st = store.CandidateStore(self.db)
        st.upsert_many([{
            "candidate_id": "C-CRYPTO-01",
            "name": "张三",
            "id_card": "110105199001011234",
            "ticket_no": "2026T001",
            "seat_no": "A-01",
            "reference_photo": "/tmp/photo.jpg",
            "consent_status": "已同意",
        }], operator="test")

        raw_name, raw_ticket, raw_seat, raw_photo = self._raw_values("C-CRYPTO-01")
        for v in (raw_name, raw_ticket, raw_seat, raw_photo):
            self.assertTrue(str(v).startswith("enc:"))

        cands = st.load()
        c = cands["C-CRYPTO-01"]
        self.assertEqual(c.name, "张三")
        self.assertEqual(c.ticket_no, "2026T001")
        self.assertEqual(c.seat_no, "A-01")
        self.assertEqual(c.reference_photo, "/tmp/photo.jpg")

    def test_upsert_preserves_existing_encrypted_id_card(self):
        st = store.CandidateStore(self.db)
        st.upsert_many([{
            "candidate_id": "C-CRYPTO-02",
            "name": "李四",
            "id_card": "110105199202022345",
            "ticket_no": "2026T002",
            "seat_no": "B-02",
        }], operator="test")

        st.upsert_many([{
            "candidate_id": "C-CRYPTO-02",
            "name": "李四-更新",
            "ticket_no": "2026T003",
            # 不提供 id_card，应保留原哈希
        }], operator="test")

        cands = st.load()
        c = cands["C-CRYPTO-02"]
        self.assertEqual(c.name, "李四-更新")
        self.assertEqual(c.ticket_no, "2026T003")
        self.assertTrue(c.id_card_hash and len(c.id_card_hash) > 0)


if __name__ == "__main__":
    unittest.main()
