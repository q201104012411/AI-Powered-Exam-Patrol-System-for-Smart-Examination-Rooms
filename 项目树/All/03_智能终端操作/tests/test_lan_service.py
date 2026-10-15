# -*- coding: utf-8 -*-
"""
03 局域网接入服务单元测试

原则：**不碰真实 data\\ 目录**
  · Store 用临时库（db_path 注入），不走 config.DB_PATH
  · data_crypto 用固定的 EXAM_DATA_KEY，避免在真实数据目录里生成/读取密钥
覆盖：鉴权（回环免令牌 / 跨设备必须令牌 / fail-closed）、接入与幂等、
      参数校验、分链存储（不污染本机 audit 链）、加密落库、链头变更留痕、裁剪。
"""
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import lan_service                                  # noqa: E402
TOKEN_HEADER = lan_service.TOKEN_HEADER
TOKEN = "tok-中文-123"

# 密钥先固定好，再让 data_crypto 用它（不影响真实 data\.data_key）
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except Exception:                                     # noqa: BLE001
    HAS_CRYPTO = False


class _H(dict):
    """模拟 http.server 的 headers（只用到 .get）"""


@unittest.skipUnless(HAS_CRYPTO, "需要 cryptography（字段级加密依赖）")
class TestLanService(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["EXAM_DATA_KEY"] = Fernet.generate_key().decode("utf-8")

    def setUp(self):
        import storage
        self.tmp = tempfile.mkdtemp(prefix="lan_service_test_")
        self.store = storage.Store(db_path=os.path.join(self.tmp, "terminal.db"))
        self.svc = lan_service.LanService(store=self.store, token=TOKEN)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recs(self, n=2, tag="A"):
        return [{"ts": "2026-10-15 09:0%d:00" % i, "action": "核验",
                 "detail": "%s 第 %d 条" % (tag, i), "operator": "owner"}
                for i in range(n)]

    # ---------------- 鉴权 ----------------
    def test_loopback_needs_no_token(self):
        ok, why = self.svc.authorized("127.0.0.1", _H())
        self.assertTrue(ok, why)
        ok, _ = self.svc.authorized("::1", _H({TOKEN_HEADER: "错的"}))
        self.assertTrue(ok, "回环应免令牌")

    def test_lan_requires_token(self):
        ok, why = self.svc.authorized("192.168.1.50", _H())
        self.assertFalse(ok)
        self.assertIn("令牌", why)
        ok, _ = self.svc.authorized("192.168.1.50", _H({TOKEN_HEADER: TOKEN}))
        self.assertTrue(ok, "带对令牌应放行（含非 ASCII 令牌）")
        ok, _ = self.svc.authorized("192.168.1.50", _H({TOKEN_HEADER: "bad"}))
        self.assertFalse(ok)

    def test_fail_closed_when_no_token_configured(self):
        svc = lan_service.LanService(store=self.store, token="")
        ok, why = svc.authorized("10.0.0.9", _H())
        self.assertFalse(ok, "没配令牌时跨设备来源必须拒绝：%s" % why)
        ok, _ = svc.authorized("127.0.0.1", _H())
        self.assertTrue(ok, "本机仍可用")

    def test_bearer_header_also_accepted(self):
        ok, _ = self.svc.authorized("10.0.0.9", _H({"Authorization": "Bearer " + TOKEN}))
        self.assertTrue(ok)

    # ---------------- 接入 ----------------
    def test_health_reports_counts(self):
        h = self.svc.health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["remote_records"], 0)
        self.assertIn("audit_chain_ok", h)
        self.assertEqual(h["token_required"], True)

    def test_records_accepted_then_idempotent(self):
        body = {"device_id": "PC-A", "device_name": "01", "kind": "audit",
                "chain": {"algo": "sha256", "head": "h1", "count": 2},
                "records": self._recs(2)}
        st, out = self.svc.handle_records(body, "192.168.1.50")
        self.assertEqual(st, 200)
        self.assertEqual(out["accepted"], 2)
        self.assertEqual(out["duplicated"], 0)

        st, out2 = self.svc.handle_records(body, "192.168.1.50")
        self.assertEqual(st, 200)
        self.assertEqual(out2["accepted"], 0)
        self.assertEqual(out2["duplicated"], 2)
        self.assertEqual(out2["total"], 2, "重复上报不能把累计条数算大")

    def test_records_need_device_id_and_records(self):
        st, out = self.svc.handle_records({"records": self._recs(1)}, "x")
        self.assertEqual(st, 400)
        self.assertIn("device_id", out["reason"])
        st, out = self.svc.handle_records({"device_id": "PC-A"}, "x")
        self.assertEqual(st, 400)
        self.assertIn("records", out["reason"])

    def test_empty_batch_is_noop(self):
        st, out = self.svc.handle_records({"device_id": "PC-A", "records": []}, "x")
        self.assertEqual(st, 200)
        self.assertEqual(out["accepted"], 0)
        self.assertEqual(self.store.remote_summary()["records"], 0)

    def test_oversize_batch_rejected(self):
        import config
        st, out = self.svc.handle_records(
            {"device_id": "PC-A", "records": self._recs(config.LAN_BATCH_MAX + 1)}, "x")
        self.assertEqual(st, 400)
        self.assertIn("分批", out["reason"])

    def test_unknown_kind_still_stored_with_note(self):
        st, out = self.svc.handle_records(
            {"device_id": "PC-A", "kind": "稀奇古怪", "records": self._recs(1)}, "x")
        self.assertEqual(st, 200)
        self.assertEqual(out["accepted"], 1)
        self.assertIn("未分类", out["note"])

    # ---------------- 分链存储 ----------------
    def test_remote_records_do_not_pollute_local_audit_chain(self):
        self.store.log("本机动作", "告警接入", "owner")
        self.svc.handle_records({"device_id": "PC-A", "records": self._recs(3)}, "x")
        ok, msg, n = self.store.verify_chain()
        self.assertTrue(ok, "本机审计链必须仍然完整：%s" % msg)
        rows = self.store.audit_rows(limit=100)
        actions = [r["action"] for r in rows]
        self.assertIn("远端记录接入", actions, "本机链上要留一条接入回执")
        self.assertTrue(all(a in ("本机动作", "远端记录接入") for a in actions),
                        "远端明细不得写进本机链：%s" % actions)
        self.assertNotIn("核验", actions)

    def test_remote_detail_encrypted_at_rest(self):
        self.svc.handle_records({"device_id": "PC-A", "kind": "oplog",
                                 "records": self._recs(1, tag="机密")}, "x")
        import sqlite3
        c = sqlite3.connect(self.store.db_path)
        try:
            raw_detail, raw_raw = c.execute(
                "SELECT detail, raw FROM remote_records").fetchone()
        finally:
            c.close()
        self.assertTrue(str(raw_detail).startswith("enc:"), "落库必须加密：%s" % raw_detail[:20])
        self.assertTrue(str(raw_raw).startswith("enc:"))
        rows = self.store.list_remote_records(device_id="PC-A")
        self.assertIn("机密", rows[0]["detail"], "读出时必须解密")

    def test_chain_head_change_is_recorded(self):
        self.svc.handle_records({"device_id": "PC-A", "kind": "audit",
                                 "chain": {"head": "h1", "count": 1},
                                 "records": self._recs(1)}, "x")
        devs = {d["device_id"]: d for d in self.store.remote_devices()}
        self.assertEqual(devs["PC-A"]["chain_changes"], 0)
        self.assertEqual(devs["PC-A"]["chain_head"], "h1")

        st, out = self.svc.handle_records({"device_id": "PC-A", "kind": "audit",
                                           "chain": {"head": "h2", "count": 2},
                                           "records": self._recs(1, tag="B")}, "x")
        self.assertTrue(out["chain_changed"], "链头变了要如实报出来")
        devs = {d["device_id"]: d for d in self.store.remote_devices()}
        self.assertEqual(devs["PC-A"]["chain_changes"], 1)
        self.assertEqual(devs["PC-A"]["records"], 2)

    def test_devices_are_isolated(self):
        self.svc.handle_records({"device_id": "PC-A", "records": self._recs(1)}, "x")
        self.svc.handle_records({"device_id": "PC-B", "records": self._recs(1)}, "x")
        s = self.store.remote_summary()
        self.assertEqual(s["devices"], 2)
        self.assertEqual(s["records"], 2)
        self.assertEqual(len(self.store.list_remote_records(device_id="PC-A")), 1)

    def test_list_records_filters(self):
        self.svc.handle_records({"device_id": "PC-A", "kind": "check",
                                 "records": self._recs(1)}, "x")
        self.svc.handle_records({"device_id": "PC-A", "kind": "order",
                                 "records": self._recs(1)}, "x")
        out = self.svc.list_records({"kind": ["check"], "limit": ["10"]})
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(out["records"][0]["kind"], "check")

    def test_prune_remote_keeps_newest(self):
        import time as _t
        for i in range(5):
            self.svc.handle_records({"device_id": "PC-A",
                                     "records": [{"ts": "t%d" % i, "action": "a",
                                                  "detail": str(i)}]}, "x")
            _t.sleep(0.01)
        removed = self.store.prune_remote(keep=2)
        self.assertEqual(removed, 3)
        rows = self.store.list_remote_records(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["detail"], "4", "应保留最新的")
        ok, msg, _n = self.store.verify_chain()
        self.assertTrue(ok, "裁剪也要留痕且不破坏链：%s" % msg)

    # ---------------- 告警 ----------------
    def test_alert_requires_code(self):
        st, out = self.svc.handle_alert({"detail": "没有 code"}, "x")
        self.assertEqual(st, 400)
        self.assertIn("code", out["reason"])

    def test_alert_accepts_wrapped_payload(self):
        st, out = self.svc.handle_alert({"alert": {"detail": "包了一层但没 code"}}, "x")
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
