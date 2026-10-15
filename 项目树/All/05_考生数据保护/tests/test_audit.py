# -*- coding: utf-8 -*-
"""审计哈希链（第三节第 4 条 / 第六节"操作审计覆盖率"）单元测试

覆盖：
  · 每条审计都带 prev_hash + hash，形成链
  · 篡改任一条记录都能被 verify_chain 定位到
  · 删除中间记录 / 调换顺序 能被发现
  · 审计内容携带操作人、动作、时间，且不含明文敏感值
"""
import unittest

from tests._base import DppTestCase
from dpp import models


class TestAuditChain(DppTestCase):

    def setUp(self):
        super().setUp()
        # 造几条审计
        self.collect_one(name="审计考生", id_card_no="330100200801011234")
        owner = self.login("owner")
        self.life.check_transport("dpp://edge-1/alert")
        self.store.log("自定义动作", "手工打点")

    def _rows(self):
        return self.store.audit_rows(limit=1000)

    def test_chain_genesis_and_links(self):
        rows = list(reversed(self._rows()))     # 按 seq 升序
        prev = "GENESIS"
        for i, r in enumerate(rows, 1):
            self.assertEqual(r["prev_hash"], prev, "第 %d 条 prev_hash 断裂" % i)
            payload = {"ts": r["ts"], "action": r["action"],
                       "detail": r["detail"], "operator": r["operator"]}
            self.assertEqual(models.chain_hash(prev, payload), r["hash"],
                             "第 %d 条 hash 与内容不符" % i)
            prev = r["hash"]

    def test_verify_chain_ok(self):
        ok, msg, n = self.store.verify_chain()
        self.assertTrue(ok, msg)
        self.assertGreater(n, 0)

    def test_tamper_detail_is_detected(self):
        """篡改某条审计的 detail，必须被定位到具体条号。"""
        rows = list(reversed(self._rows()))
        target = rows[0]
        with self.store._lock, self.store._conn() as c:
            c.execute("UPDATE audits SET detail=? WHERE seq=?",
                      ("被篡改的内容", target["seq"]))
        ok, msg, n = self.store.verify_chain()
        self.assertFalse(ok)
        self.assertIn("篡改", msg)
        self.assertEqual(n, 1)

    def test_tamper_action_is_detected(self):
        rows = list(reversed(self._rows()))
        with self.store._lock, self.store._conn() as c:
            c.execute("UPDATE audits SET action=? WHERE seq=?",
                      ("伪造动作", rows[-1]["seq"]))
        ok, msg, _ = self.store.verify_chain()
        self.assertFalse(ok)

    def test_delete_middle_record_detected(self):
        """删掉中间一条会使其后一条的 prev_hash 对不上。"""
        rows = list(reversed(self._rows()))
        self.assertGreaterEqual(len(rows), 3)
        victim = rows[1]
        with self.store._lock, self.store._conn() as c:
            c.execute("DELETE FROM audits WHERE seq=?", (victim["seq"],))
        ok, msg, _ = self.store.verify_chain()
        self.assertFalse(ok)
        self.assertIn("prev_hash", msg)

    def test_insertion_is_detected(self):
        """在链尾插入一条伪造记录也会被发现（hash 对不上）。"""
        rows = list(reversed(self._rows()))
        last = rows[-1]
        with self.store._lock, self.store._conn() as c:
            c.execute("INSERT INTO audits (ts, action, detail, operator, "
                      "prev_hash, hash) VALUES (?,?,?,?,?,?)",
                      (models.now_ts(), "伪造", "无授权插入", "hacker",
                       last["hash"], "deadbeef"))
        ok, msg, _ = self.store.verify_chain()
        self.assertFalse(ok)

    def test_audit_rows_carry_operator_and_time(self):
        for r in self._rows():
            self.assertTrue(r["ts"])
            self.assertTrue(r["operator"] is not None)
            self.assertTrue(r["action"])

    def test_audit_detail_has_no_plaintext_id(self):
        """审计明细里不得出现完整证件号（日志脱敏硬要求）。"""
        for r in self._rows():
            self.assertNotIn("330100200801011234", str(r["detail"]))


class TestAuditCoverage(DppTestCase):

    def test_all_key_actions_covered(self):
        """跑一遍主要动作，验证审计覆盖到验收要求的动作集合。"""
        from dpp import config
        owner = self.login("owner")
        res = self.collect_one(name="覆盖考生", with_image=True)
        self.life.use(owner, res["cand_ref"], "作弊判定", "复核")
        try:
            self.life.use(owner, res["cand_ref"], "情绪分析", "画像")
        except Exception:
            pass
        self.life.reveal_plain(owner, res["cand_ref"], "核对证件")
        sh = self.life.request_share(owner, [res["cand_ref"]],
                                     ["ref", "room_id", "seat_no"])
        self.life.approve_share(owner, sh["req_id"], approve=True, export=True)
        self.life.destroy_by_request(owner, res["cand_ref"], reason="测试销毁")
        self.accounts.create("cov1", "p", config.ROLE_VIEWER, by_role="owner")

        actions = self.store.audit_actions()
        for a in ("登录", "采集", "使用", "使用被拒", "解密查看",
                  "共享申请", "共享审批", "导出", "销毁", "账户"):
            self.assertTrue(actions.get(a), "缺少审计动作：%s" % a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
