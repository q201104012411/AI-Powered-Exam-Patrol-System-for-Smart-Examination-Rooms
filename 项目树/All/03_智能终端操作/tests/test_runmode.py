# -*- coding: utf-8 -*-
"""运行模式（考试 / 非考试·教学）单元测试。

只测**行为差异**与**留痕**，不建窗口：
  默认值 / 非法值回落 / 切换写审计 / 教学模式红色可直接复核 / 考试模式必须先确认。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="runmode_test_")
os.environ["EXAM_TERMINAL_HOME"] = _TMP

import access      # noqa: E402
import alerts      # noqa: E402
import config      # noqa: E402
import models      # noqa: E402
import review      # noqa: E402
import runmode     # noqa: E402
import storage     # noqa: E402


class RunModeTest(unittest.TestCase):
    def setUp(self):
        self.store = storage.Store(
            db_path=os.path.join(_TMP, "mode_%s.db" % models.new_id("T")))
        self.acc = access.AccountStore(self.store)
        self.acc.ensure_seed()
        self.session = self.acc.authenticate("owner", "EDY_POWERQ")
        self.hub = alerts.AlertHub(self.store)
        self.reviewer = review.Reviewer(self.store)

    def test_default_is_exam(self):
        self.assertEqual(runmode.get(self.store), config.RUN_MODE_EXAM)
        self.assertTrue(runmode.is_exam(self.store))

    def test_bad_value_falls_back_to_exam(self):
        self.store.set_kv(runmode.KEY, "whatever")
        self.assertTrue(runmode.is_exam(self.store))     # 宁严不松

    def test_switch_writes_audit_once(self):
        before = len(self.store.audit_rows(limit=100000))
        ok, _msg = runmode.set(self.store, config.RUN_MODE_TEACH, "owner", "教学")
        self.assertTrue(ok)
        self.assertTrue(runmode.is_teach(self.store))
        self.assertEqual(len(self.store.audit_rows(limit=100000)), before + 1)
        # 重复切同一模式：不重复写
        ok2, _m2 = runmode.set(self.store, config.RUN_MODE_TEACH, "owner")
        self.assertTrue(ok2)
        self.assertEqual(len(self.store.audit_rows(limit=100000)), before + 1)

    def test_invalid_mode_rejected(self):
        ok, msg = runmode.set(self.store, "exam_mode", "owner")
        self.assertFalse(ok)
        self.assertIn("不认识", msg)

    def test_teach_mode_allows_review_without_ack(self):
        runmode.set(self.store, config.RUN_MODE_TEACH, "owner")
        a = models.Alert(code="PHONE_DETECTED", room_id="A-01")
        self.hub.ingest(a, dedup=False)
        self.assertEqual((a.extra or {}).get("run_mode"), config.RUN_MODE_TEACH)
        ok, res = self.reviewer.review(a.alert_id, config.REVIEW_CONFIRMED,
                                       "教学复核", self.session)
        self.assertTrue(ok, res)

    def test_exam_mode_requires_ack_first(self):
        runmode.set(self.store, config.RUN_MODE_EXAM, "owner")
        a = models.Alert(code="MULTI_FACE", room_id="A-01")
        self.hub.ingest(a, dedup=False)
        self.assertNotIn("run_mode", a.extra or {})
        ok, res = self.reviewer.review(a.alert_id, config.REVIEW_CONFIRMED,
                                       "考试复核", self.session)
        self.assertFalse(ok)
        self.assertIn("确认", res)
        self.hub.ack(a.alert_id, "owner", reason="确认")
        ok2, res2 = self.reviewer.review(a.alert_id, config.REVIEW_CONFIRMED,
                                         "确认后复核", self.session)
        self.assertTrue(ok2, res2)

    def test_effects_and_labels(self):
        for m in config.RUN_MODES:
            self.assertGreaterEqual(len(runmode.effects(m)), 3)
            self.assertTrue(runmode.label(m))
        self.assertIn("教学", runmode.label(config.RUN_MODE_TEACH))


if __name__ == "__main__":
    unittest.main()
