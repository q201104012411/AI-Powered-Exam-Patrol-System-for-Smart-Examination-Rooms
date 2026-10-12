# -*- coding: utf-8 -*-
"""考生敏感数据加密存储单元测试"""
import os
import sqlite3
import tempfile
import unittest

import data_crypto
import models
import storage


class TestAlertEncryption(unittest.TestCase):

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

    def test_alert_candidate_fields_encrypted_at_rest(self):
        store = storage.Store(self.db)
        a = models.Alert(code="LOOK_DOWN", detail="持续低头 45 秒",
                         room_id="A-01", seat_no="07",
                         candidate_id="T2026007", candidate_name="张三")
        store.save_alert(a)

        raw_name = self._raw_value("alerts", "candidate_name", "alert_id", a.alert_id)
        raw_cid = self._raw_value("alerts", "candidate_id", "alert_id", a.alert_id)
        raw_seat = self._raw_value("alerts", "seat_no", "alert_id", a.alert_id)
        raw_detail = self._raw_value("alerts", "detail", "alert_id", a.alert_id)
        for v in (raw_name, raw_cid, raw_seat, raw_detail):
            self.assertTrue(str(v).startswith("enc:"))

        loaded = store.get_alert(a.alert_id)
        self.assertEqual(loaded.candidate_name, "张三")
        self.assertEqual(loaded.candidate_id, "T2026007")
        self.assertEqual(loaded.seat_no, "07")
        self.assertEqual(loaded.detail, "持续低头 45 秒")

    def test_review_reason_encrypted_at_rest(self):
        store = storage.Store(self.db)
        rec = models.ReviewRecord(alert_id="AL1234567890", conclusion="confirmed",
                                  reason="确有手机反光", operator="patrol1")
        store.save_review(rec)

        raw_reason = self._raw_value("reviews", "reason", "review_id", rec.review_id)
        self.assertTrue(str(raw_reason).startswith("enc:"))

        loaded = store.get_review(rec.review_id)
        self.assertEqual(loaded.reason, "确有手机反光")

    def test_dedup_does_not_mutate_input_alert(self):
        """加密保存不得修改原始 Alert 对象，否则同一批告警二次接入会去重失败。"""
        store = storage.Store(self.db)
        hub_class = __import__("alerts").AlertHub
        hub = hub_class(store)
        items = hub.simulate(6)
        hub.ingest_many(items)
        created2, dup2 = hub.ingest_many(items)
        self.assertEqual(len(created2), 0)
        self.assertEqual(dup2, 6)


if __name__ == "__main__":
    unittest.main()
