# -*- coding: utf-8 -*-
"""数据加密存储单元测试"""
import os
import sqlite3
import tempfile
import unittest

import config
import data_crypto
import models
import storage


class TestDataCrypto(unittest.TestCase):

    def test_field_roundtrip(self):
        plain = "张三 / 2026T001 / A-01"
        cipher = data_crypto.encrypt_field(plain)
        self.assertTrue(cipher.startswith("enc:"))
        self.assertNotEqual(cipher, plain)
        self.assertEqual(data_crypto.decrypt_field(cipher), plain)

    def test_empty_field_passthrough(self):
        self.assertEqual(data_crypto.encrypt_field(""), "")
        self.assertEqual(data_crypto.encrypt_field(None), None)
        self.assertEqual(data_crypto.decrypt_field(""), "")
        self.assertEqual(data_crypto.decrypt_field(None), None)

    def test_plaintext_passthrough(self):
        """兼容旧明文：未加密字段原样返回。"""
        self.assertEqual(data_crypto.decrypt_field("old_plain_text"), "old_plain_text")

    def test_bytes_roundtrip(self):
        data = b"\xff\xd8\xff\xe0 fake image bytes"
        cipher = data_crypto.encrypt_bytes(data)
        self.assertTrue(cipher.startswith("enc:"))
        self.assertEqual(data_crypto.decrypt_bytes(cipher), data)


class TestStoreEncryption(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        from cryptography.fernet import Fernet
        os.environ["EXAM_DATA_KEY"] = Fernet.generate_key().decode("utf-8")

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass
        os.environ.pop("EXAM_DATA_KEY", None)

    def _raw_value(self, table, col, pk_col, pk_val):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT %s FROM %s WHERE %s=?" % (col, table, pk_col),
                               (pk_val,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_device_name_position_encrypted_at_rest(self):
        store = storage.Store(self.db)
        dev = models.Device(code="CAM-01", name="教学楼A-01", room_id="A-01",
                            position="讲台上方 2.5m", url="rtsp://test")
        store.save_device(dev)

        raw_name = self._raw_value("devices", "name", "device_id", dev.device_id)
        raw_pos = self._raw_value("devices", "position", "device_id", dev.device_id)
        self.assertTrue(str(raw_name).startswith("enc:"))
        self.assertTrue(str(raw_pos).startswith("enc:"))

        loaded = store.get_device(device_id=dev.device_id)
        self.assertEqual(loaded.name, "教学楼A-01")
        self.assertEqual(loaded.position, "讲台上方 2.5m")

    def test_workorder_note_encrypted_at_rest(self):
        store = storage.Store(self.db)
        dev = models.Device(code="CAM-02", room_id="A-02")
        store.save_device(dev)
        wo = models.WorkOrder(device=dev, codes=["BLUR"], severity="warn",
                              note="考生举手报告模糊")
        store.save_order(wo)

        raw_note = self._raw_value("workorders", "note", "order_id", wo.order_id)
        raw_review = self._raw_value("workorders", "review_note", "order_id", wo.order_id)
        self.assertTrue(str(raw_note).startswith("enc:"))

        loaded = store.get_order(wo.order_id)
        self.assertEqual(loaded.note, "考生举手报告模糊")
        self.assertEqual(loaded.review_note, raw_review or "")


if __name__ == "__main__":
    unittest.main()
