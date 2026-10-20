# -*- coding: utf-8 -*-
r"""
12 告警上传与终端处置演示 · 自检

分两层：
  · 纯单元层：处置状态机与步骤记录（用假 console，不碰磁盘、不起子进程）
  · 端到端层：真跑 demo_main 的沙箱演示，断言 02/03 的真实落库结果
    （需要 cv2/numpy；缺依赖时自动跳过 e2e，不会让自检整体失败）

用法：
    python tests_selfcheck.py            # 全部
    python tests_selfcheck.py --fast     # 只跑单元层
退出码 0 = 通过。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from disposition import DispositionDesk, DispositionResult  # noqa: E402


# ============================================================ 单元层
class FakeAlert(object):
    def __init__(self, level="orange", status="pending", false_positive=0):
        self.level = level
        self.status = status
        self.false_positive = false_positive
        self.code = "TEST_CODE"

    def needs_force_confirm(self):
        return self.level == "red" and self.status == "pending"


class FakeConsole(object):
    """记录调用顺序的假终端；可注入"复核被拒"来验证错误传递。"""

    def __init__(self, alert=None, deny_review=False):
        self.alert = alert or FakeAlert()
        self.deny_review = deny_review
        self.calls = []

    def get_alert(self, alert_id):
        return self.alert

    def ack(self, alert_id, session, reason=""):
        self.calls.append(("ack", alert_id, reason))
        self.alert.status = "acked"
        return True, "已确认（%s）" % self.alert.level

    def review(self, alert_id, conclusion, reason, session, witness=""):
        self.calls.append(("review", alert_id, conclusion, reason))
        if self.deny_review:
            return False, "红色预警必须先确认，再填写复核结论"
        if not (reason or "").strip():
            return False, "必须填写复核理由（留痕要求）"
        self.alert.status = "reviewed"
        self.alert.false_positive = 1 if conclusion == "false_positive" else 0
        return True, "RV0001"

    def conclusion_text(self, c):
        return {"confirmed": "成立", "rejected": "不成立",
                "false_positive": "误报"}.get(c, c)

    def timeline(self, alert_id):
        return [{"ts": "2026-10-14 20:00:00", "event": "告警接入", "who": "02", "detail": "X"},
                {"ts": "2026-10-14 20:00:05", "event": "复核", "who": "patrol1", "detail": "成立"}]

    def report(self, alert_id, note, session):
        return True, "已上报"


class Session(object):
    def __init__(self, name="patrol1"):
        self.username = name


class TestDispositionMachine(unittest.TestCase):
    def test_red_alert_is_acked_before_review(self):
        """红色预警：确认必须发生在复核之前。"""
        tc = FakeConsole(FakeAlert(level="red"))
        res = DispositionDesk(tc).dispose("AL1", Session(), conclusion="confirmed",
                                          reason="现场已核实")
        self.assertTrue(res.ok, res.as_dict())
        kinds = [c[0] for c in tc.calls]
        self.assertEqual(kinds, ["ack", "review"])
        self.assertEqual(res.level_before, "red")
        self.assertEqual(res.final_status, "reviewed")

    def test_orange_alert_reviews_without_ack(self):
        """橙色预警不需要确认，直接复核。"""
        tc = FakeConsole(FakeAlert(level="orange"))
        res = DispositionDesk(tc).dispose("AL2", Session(), conclusion="confirmed",
                                          reason="已核实")
        self.assertTrue(res.ok, res.as_dict())
        self.assertEqual([c[0] for c in tc.calls], ["review"])

    def test_false_positive_marks_closed_loop(self):
        """误报结论必须进入算法迭代闭环。"""
        tc = FakeConsole(FakeAlert(level="orange"))
        res = DispositionDesk(tc).dispose("AL3", Session(), conclusion="false_positive",
                                          reason="镜片污渍反光，非手机")
        self.assertTrue(res.closed_loop)
        self.assertEqual(res.final_status, "reviewed")

    def test_blank_reason_is_rejected(self):
        """理由为空必须被拒（03 的留痕要求）。"""
        desk = DispositionDesk(FakeConsole(FakeAlert()))
        ok, msg = desk.review("AL4", "confirmed", "   ", Session())
        self.assertFalse(ok)
        self.assertIn("理由", msg)

    def test_bad_conclusion_is_rejected(self):
        desk = DispositionDesk(FakeConsole(FakeAlert()))
        ok, msg = desk.review("AL5", "maybe", "理由齐全", Session())
        self.assertFalse(ok)
        self.assertIn("不合法", msg)

    def test_review_failure_is_reported_not_swallowed(self):
        """03 拒绝复核时，处置结果必须如实标失败，不能伪装成功。"""
        tc = FakeConsole(FakeAlert(level="red"), deny_review=True)
        res = DispositionDesk(tc).dispose("AL6", Session(), conclusion="confirmed",
                                          reason="理由")
        self.assertFalse(res.ok)
        self.assertTrue(any(not s.ok for s in res.steps))

    def test_red_without_auto_ack_stops(self):
        """不允许自动确认时，红色预警直接停住，不去复核。"""
        tc = FakeConsole(FakeAlert(level="red"))
        res = DispositionDesk(tc).dispose("AL7", Session(), conclusion="confirmed",
                                          reason="理由", auto_ack=False)
        self.assertFalse(res.ok)
        self.assertEqual([c[0] for c in tc.calls], [])

    def test_missing_alert_reports_failure(self):
        class NoneConsole(FakeConsole):
            def get_alert(self, alert_id):
                return None

        res = DispositionDesk(NoneConsole()).dispose("AL8", Session(),
                                                     conclusion="confirmed", reason="r")
        self.assertFalse(res.ok)

    def test_result_serialises(self):
        tc = FakeConsole(FakeAlert())
        res = DispositionDesk(tc).dispose("AL9", Session(), conclusion="confirmed",
                                          reason="r")
        blob = json.dumps(res.as_dict(), ensure_ascii=False)
        self.assertIn("AL9", blob)


# ============================================================ 端到端层
def _has_cv2():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_cv2(), "缺 cv2/numpy，跳过端到端演示（先跑「安装环境.bat」）")
class TestEndToEndSandbox(unittest.TestCase):
    """真跑一遍沙箱演示，确认 02 与 03 都真的落了库。"""

    @classmethod
    def setUpClass(cls):
        # 用演示目录下的路径，不用系统 %TEMP%：某些受限/沙箱环境对 %TEMP% 写入会被拒
        cls.sandbox = os.path.join(HERE, "data", "_selftest_e2e")
        shutil.rmtree(cls.sandbox, ignore_errors=True)
        os.makedirs(cls.sandbox, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cls.proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "demo_main.py"),
             "--home", cls.sandbox, "--keep"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, cwd=HERE, timeout=600)
        cls.out = (cls.proc.stdout or b"").decode("utf-8", "replace")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def _show(self):
        return "\n".join(self.out.splitlines()[-40:])

    def test_demo_exit_code_zero(self):
        self.assertEqual(self.proc.returncode, 0,
                         "演示未通过：\n%s" % self._show())

    def test_all_checkpoints_passed(self):
        self.assertIn("全部检查点通过", self.out, self._show())

    def test_no_checkpoint_failed(self):
        self.assertNotIn("[FAIL]", self.out, self._show())

    def test_audit_chain_intact(self):
        self.assertIn("审计链完整", self.out, self._show())

    def test_red_downgraded_to_orange(self):
        self.assertIn("红色已按联动约定降为橙色待复核", self.out, self._show())

    def test_force_confirm_was_enforced(self):
        self.assertIn("被 03 正确挡下", self.out, self._show())

    def test_invigilator_cannot_review(self):
        self.assertIn("监考员", self.out)
        self.assertNotIn("监考员竟然复核成功", self.out, self._show())

    def test_duplicate_upload_deduplicated(self):
        self.assertIn("重复上传被去重", self.out, self._show())

    def test_alerts_landed_in_03_sqlite(self):
        """直接查 03 的库，确认告警真的落盘（不是只在日志里好看）。"""
        import sqlite3
        db = os.path.join(self.sandbox, "03", "terminal.db")
        self.assertTrue(os.path.isfile(db), "03 未生成 terminal.db")
        con = sqlite3.connect(db)
        try:
            n_alerts = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            n_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            n_audit = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(n_alerts, 5, "03 告警落库条数偏少")
        self.assertGreaterEqual(n_reviews, 3, "03 复核留痕条数偏少")
        self.assertGreaterEqual(n_audit, 8, "03 审计条数偏少")

    def test_reviewed_alerts_have_conclusions(self):
        import sqlite3
        db = os.path.join(self.sandbox, "03", "terminal.db")
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT status, review_conclusion FROM alerts").fetchall()
        finally:
            con.close()
        reviewed = [r for r in rows if r[0] == "reviewed"]
        self.assertTrue(reviewed, "没有任何告警走到「已复核」")
        self.assertTrue(all(r[1] for r in reviewed), "有已复核告警没写结论")

    def test_sandbox_is_isolated_from_real_data(self):
        """演示不能碰真实数据目录。"""
        real03 = os.path.join(os.path.dirname(HERE), "03_智能终端操作", "data")
        self.assertFalse(
            os.path.normcase(os.path.abspath(self.sandbox)).startswith(
                os.path.normcase(os.path.abspath(real03)) + os.sep),
            "沙箱目录竟然落在真实数据目录下面")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="只跑单元层（不跑端到端）")
    ns = ap.parse_args()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDispositionMachine))
    if not ns.fast:
        suite.addTests(loader.loadTestsFromTestCase(TestEndToEndSandbox))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
