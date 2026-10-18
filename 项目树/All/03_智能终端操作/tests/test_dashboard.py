# -*- coding: utf-8 -*-
"""
03 驾驶舱 · 单元测试（纯逻辑，不建窗口）

覆盖三块这一版新增/重写的东西：
  1) storage.stats()  —— 驾驶舱的计数口径（全 SQL 聚合，不再"最多数 2000 条"）
  2) dashboard 的纯函数 —— 时间分桶、TOP N、分级/类型序列、路径压缩
  3) theme 的绘图辅助   —— 刻度选择、颜色混合、数字格式

运行：python -m unittest discover -s tests -t .
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="dash_test_")
os.environ["EXAM_TERMINAL_HOME"] = _TMP          # 必须在 import config 之前

import config        # noqa: E402
import dashboard as dash   # noqa: E402
import models        # noqa: E402
import storage       # noqa: E402
import theme as T    # noqa: E402


def _ts(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class ThemeHelperTest(unittest.TestCase):
    def test_nice_ticks_are_integers_for_counts(self):
        for vmax in (1, 2, 3, 7, 12, 97, 1000):
            ticks, top = T.nice_ticks(vmax, 4)
            self.assertGreaterEqual(top, vmax)
            self.assertEqual(ticks[0], 0)
            self.assertEqual(ticks[-1], top)
            for t in ticks:
                self.assertAlmostEqual(t, round(t), places=6)   # 计数轴不出现小数

    def test_nice_ticks_never_repeat_labels(self):
        # 改造前 vmax=1 会给出 0/1/1/0 这种重复标签
        ticks, _top = T.nice_ticks(1, 4)
        self.assertEqual(len(ticks), len(set(ticks)))

    def test_nice_ticks_zero(self):
        ticks, top = T.nice_ticks(0)
        self.assertEqual(top, 1.0)
        self.assertEqual(ticks, [0.0, 1.0])

    def test_blend_endpoints(self):
        self.assertEqual(T.blend("#22d3ee", "#000000", 0), "#22d3ee")
        self.assertEqual(T.blend("#22d3ee", "#000000", 1), "#000000")
        mid = T.blend("#ffffff", "#000000", 0.5)
        self.assertEqual(mid, "#808080")

    def test_fmt_int_and_pct(self):
        self.assertEqual(T.fmt_int(1666), "1,666")
        self.assertEqual(T.fmt_int(0), "0")
        self.assertEqual(T.fmt_pct(0, 0), "—")          # 分母 0 不编 0%
        self.assertEqual(T.fmt_pct(1, 4), "25%")

    def test_fmt_duration(self):
        self.assertEqual(T.fmt_duration(9), "9 秒")
        self.assertEqual(T.fmt_duration(90), "1 分")
        self.assertEqual(T.fmt_duration(7200), "2.0 小时")
        self.assertEqual(T.fmt_duration(-1), "—")

    def test_text_width_cjk_wider(self):
        self.assertGreater(T.text_width("预警"), T.text_width("ab"))


class DashboardHelperTest(unittest.TestCase):
    def _alert(self, ago_sec):
        a = models.Alert(code="LOOK_DOWN")
        a.created_epoch = time.time() - ago_sec
        a.created_at = _ts(a.created_epoch)
        return a

    def _alert_in_bucket(self, buckets_ago, buckets=6, span=3600):
        """把告警放到"整点对齐后倒数第 N 个桶"里（桶内第 60 秒）。

        注意：不能用"now - N 小时"，因为桶是按整点对齐的，
        现在这一小时过了多少分钟会决定它落进哪一个桶 —— 那样写出来的断言会随机失败
        （本用例第一版就是这么挂的，改成按桶起点构造后才是确定的）。
        """
        anchor = int(time.time() // span) * span
        a = models.Alert(code="LOOK_DOWN")
        a.created_epoch = anchor - buckets_ago * span + 60
        a.created_at = _ts(a.created_epoch)
        return a

    def test_time_buckets_puts_now_into_last_bucket(self):
        rows = dash.time_buckets([self._alert(30)], 6, "hour")
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(n for _l, n in rows), 1)
        self.assertEqual(rows[-1][1], 1)

    def test_time_buckets_spans_hours(self):
        rows = dash.time_buckets([self._alert_in_bucket(3)], 6, "hour")
        self.assertEqual(sum(n for _l, n in rows), 1)
        self.assertEqual(rows[-4][1], 1)          # 6 个桶里的倒数第 4 个 = 3 小时前那个

    def test_time_buckets_ignores_out_of_range(self):
        rows = dash.time_buckets([self._alert(3600 * 40)], 6, "hour")
        self.assertEqual(sum(n for _l, n in rows), 0)

    def test_previous_hour_not_counted_in_current(self):
        """边界回归：上一小时最后一秒的告警必须落在【上一小时】桶里。

        这是整点对齐分桶最容易写错的地方（用 anchor-ep 直接除会把上一桶算进当前桶）。
        """
        anchor = int(time.time() // 3600) * 3600
        a = models.Alert(code="LOOK_DOWN")
        a.created_epoch = anchor - 1
        a.created_at = _ts(a.created_epoch)
        rows = dash.time_buckets([a], 6, "hour")
        self.assertEqual(rows[-2][1], 1)
        self.assertEqual(rows[-1][1], 0)

    def test_time_buckets_day_unit(self):
        rows = dash.time_buckets([self._alert_in_bucket(1, 7, 86400)], 7, "day")
        self.assertEqual(len(rows), 7)
        self.assertEqual(sum(n for _l, n in rows), 1)
        self.assertEqual(rows[-2][1], 1)          # 昨天那个桶
        self.assertRegex(rows[-1][0], r"^\d{2}-\d{2}$")

    def test_hour_buckets_is_compat_alias(self):
        self.assertEqual(dash.hour_buckets([], 12), dash.time_buckets([], 12, "hour"))

    def test_type_top_does_not_merge_tail_into_other(self):
        """TOP 榜不合并"其它"：否则尾部几十条会做成一根压过所有真实类型的条。"""
        data = {"by_code": [("LOOK_DOWN", 12), ("TURN_HEAD", 9), ("GAZE_SHIFT", 8),
                            ("VIDEO_BLUR", 7), ("FACE_MISMATCH", 6), ("STAND_UP", 5),
                            ("SLEEPY", 4), ("OFFLINE", 3)]}
        rows = dash.type_series(data, 6)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0][1], 12)
        self.assertNotIn("其它", [r[0] for r in rows])
        # 剩余 2 种 7 条，通过副标题另行说明
        self.assertEqual(dash.type_rest(data, rows), 7)

    def test_type_rest_zero_when_everything_shown(self):
        data = {"by_code": [("LOOK_DOWN", 2)]}
        rows = dash.type_series(data, 6)
        self.assertEqual(dash.type_rest(data, rows), 0)

    def test_level_series_always_two_with_colors(self):
        rows = dash.level_series({"by_level": {config.LEVEL_RED: 3}})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "红色预警")
        self.assertEqual(rows[0][1], 3)
        self.assertEqual(rows[1][1], 0)

    def test_type_series_shows_chinese(self):
        rows = dash.type_series({"by_code": [("FIRE_ALARM", 2), ("LOOK_DOWN", 1)]})
        self.assertEqual(rows[0][0], config.ALERT_CODE_TEXT["FIRE_ALARM"])
        self.assertEqual(rows[0][1], 2)

    def test_type_series_unknown_code_kept_as_is(self):
        rows = dash.type_series({"by_code": [("NOT_REGISTERED", 1)]})
        self.assertEqual(rows[0][0], "NOT_REGISTERED")

    def test_status_text_and_color(self):
        self.assertEqual(dash.status_text(models.ST_PENDING), "待处置")
        self.assertEqual(dash.status_text("weird"), "weird")
        self.assertEqual(dash.status_color(models.ST_REVIEWED), T.OK)

    def test_short_path_keeps_tail(self):
        s = dash.short_path(r"C:\a\b\c\d\e\terminal.db", keep=3)
        self.assertTrue(s.endswith(r"\d\e\terminal.db"))
        self.assertTrue(s.startswith("C:"))

    def test_fmt_ms(self):
        self.assertEqual(dash.fmt_ms(7.4), "7 ms")
        self.assertEqual(dash.fmt_ms(1500), "1.5 s")
        self.assertEqual(dash.fmt_ms(None), "—")


class StoreStatsTest(unittest.TestCase):
    def setUp(self):
        self.store = storage.Store(
            db_path=os.path.join(_TMP, "stats_%s.db" % models.new_id("T")))

    def _add(self, code, ago, level=None, status=None, synced=1):
        a = models.Alert(code=code, level=level, room_id="A-01")
        a.created_epoch = time.time() - ago
        a.created_at = _ts(a.created_epoch)
        a.sla_deadline = a.created_epoch + a.meta()["sla_sec"]
        a.synced = synced
        if status:
            a.status = status
        self.store.save_alert(a)
        return a

    def test_totals_match_sql(self):
        self._add("PHONE_DETECTED", 60)
        self._add("LOOK_DOWN", 120)
        self._add("LOOK_DOWN", 7200)
        st = self.store.stats()
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["by_level"][config.LEVEL_RED], 1)
        self.assertEqual(st["by_level"][config.LEVEL_ORANGE], 2)
        self.assertEqual(st["by_status"]["pending"], 3)

    def test_counts_are_not_truncated_by_a_limit(self):
        # 改造前界面 list_alerts(limit=2000) 后自己数，超过 2000 就少算
        for i in range(120):
            self._add("LOOK_DOWN", 10 + i)
        self.assertEqual(self.store.stats()["total"], 120)

    def test_hourly_and_daily_bucket_lengths(self):
        st = self.store.stats(hours=6, days=3)
        self.assertEqual(len(st["hourly"]), 6)
        self.assertEqual(len(st["daily"]), 3)
        ts = [t for t, _n in st["hourly"]]
        self.assertEqual(ts, sorted(ts))               # 旧 -> 新
        self.assertAlmostEqual(ts[-1], int(time.time() // 3600) * 3600, delta=2)

    def test_last_hour_and_unsynced_and_overdue(self):
        self._add("LOOK_DOWN", 30, synced=0)
        old = self._add("LOOK_DOWN", 3600 * 5)
        old.sla_deadline = time.time() - 3600
        self.store.save_alert(old)
        st = self.store.stats()
        self.assertEqual(st["last_hour"], 1)
        self.assertEqual(st["unsynced"], 1)
        self.assertEqual(st["overdue"], 1)             # 只有那条过期的

    def test_overdue_ignores_non_pending(self):
        a = self._add("LOOK_DOWN", 3600 * 5, status=models.ST_REVIEWED)
        a.sla_deadline = time.time() - 3600
        self.store.save_alert(a)
        self.assertEqual(self.store.stats()["overdue"], 0)

    def test_by_code_sorted_desc(self):
        for _ in range(3):
            self._add("LOOK_DOWN", 60)
        self._add("PHONE_DETECTED", 60)
        rows = dict(self.store.stats()["by_code"])
        self.assertEqual(rows["LOOK_DOWN"], 3)
        self.assertEqual(rows["PHONE_DETECTED"], 1)
        self.assertEqual(self.store.stats()["by_code"][0][0], "LOOK_DOWN")

    def test_recent_is_decrypted_and_limited(self):
        a = models.Alert(code="LOOK_DOWN", room_id="A-01", candidate_name="张三")
        self.store.save_alert(a)
        st = self.store.stats(recent=5)
        self.assertEqual(len(st["recent"]), 1)
        self.assertEqual(st["recent"][0].candidate_name, "张三")

    def test_audits_counts_chain_rows(self):
        before = self.store.stats()["audits"]
        self.store.log("测试", "写一条审计", "unittest")
        self.assertEqual(self.store.stats()["audits"], before + 1)

    def test_hour_bucket_boundary(self):
        """边界回归：上一小时最后一秒 → 上一小时桶（不是当前小时桶）。"""
        anchor = int(time.time() // 3600) * 3600
        a = models.Alert(code="LOOK_DOWN", room_id="A-01")
        a.created_epoch = anchor - 1
        a.created_at = _ts(a.created_epoch)
        a.sla_deadline = a.created_epoch + 300
        self.store.save_alert(a)
        st = self.store.stats(hours=3)
        self.assertEqual(st["hourly"][-2][1], 1)
        self.assertEqual(st["hourly"][-1][1], 0)

    def test_day_bucket_boundary(self):
        lt = time.localtime()
        day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        a = models.Alert(code="LOOK_DOWN", room_id="A-01")
        a.created_epoch = day0 - 1                # 昨天 23:59:59
        a.created_at = _ts(a.created_epoch)
        a.sla_deadline = a.created_epoch + 300
        self.store.save_alert(a)
        st = self.store.stats(days=3)
        self.assertEqual(st["daily"][-2][1], 1)
        self.assertEqual(st["daily"][-1][1], 0)


class Ipv4TargetTest(unittest.TestCase):
    """按 IPv4 下发命令（2026-10-18 新增）：归一化 / 反查设备 / 双向领取 / 目标隔离。"""

    def setUp(self):
        self.store = storage.Store(
            db_path=os.path.join(_TMP, "ip_%s.db" % models.new_id("T")))

    def test_normalize_ipv4(self):
        n = storage.normalize_ipv4
        self.assertEqual(n("192.168.1.20"), "192.168.1.20")
        self.assertEqual(n(" 192.168.1.20:9113 "), "192.168.1.20")
        self.assertEqual(n("10.0.0.1"), "10.0.0.1")
        # 不是 IP 的一律返回空（调用方据此区分"设备标识 vs IP"）
        self.assertEqual(n("PC-A-01"), "")
        self.assertEqual(n("192.168.1.300"), "")
        self.assertEqual(n("192.168.1"), "")
        self.assertEqual(n("192.168.01.2"), "")      # 前导零：不认（避免歧义）
        self.assertEqual(n(""), "")

    def test_device_by_ip_from_batch(self):
        self.assertIsNone(self.store.device_by_ip("192.168.1.20"))
        self.store.save_remote_batch("CAM-A01-1", "A-01 前门", "order",
                                     [{"ts": "2026-10-18 09:00:00",
                                       "action": "巡检", "detail": "ok"}],
                                     client_ip="192.168.1.20")
        dev = self.store.device_by_ip("192.168.1.20")
        self.assertIsNotNone(dev)
        self.assertEqual(dev["device_id"], "CAM-A01-1")
        self.assertEqual(dev["last_ip"], "192.168.1.20")
        self.assertIsNone(self.store.device_by_ip("192.168.1.99"))

    def test_command_pollable_by_id_and_by_ip(self):
        cid = self.store.push_target_command("192.168.1.20", "ping",
                                             target_ip="192.168.1.20")
        self.assertTrue(cid)
        # 对端按设备标识轮询（IP 反查后的 target 也写了进去）
        cid2 = self.store.push_target_command("CAM-A01-1", "status")
        by_id = self.store.commands_for("CAM-A01-1")
        self.assertEqual([c["cmd_id"] for c in by_id], [cid2])
        # 对端按 IP 轮询能领到"按 IP 下发"的那条
        by_ip = self.store.commands_for("CAM-A01-1", ips=["192.168.1.20"])
        self.assertEqual({c["cmd_id"] for c in by_ip}, {cid, cid2})
        self.assertEqual(by_ip[0]["target_ip"], "192.168.1.20")
        # 别的设备领不到
        self.assertEqual(self.store.commands_for("CAM-B02-9", ips=["10.0.0.9"]), [])

    def test_command_by_unknown_ip_still_pollable_by_ip(self):
        cid = self.store.push_target_command("192.168.1.99", "status",
                                             target_ip="192.168.1.99")
        rows = self.store.commands_for("NOT-ME", ips=["192.168.1.99"])
        self.assertEqual([c["cmd_id"] for c in rows], [cid])

    def test_bad_kind_not_written(self):
        self.assertEqual(self.store.push_target_command("192.168.1.20", "rm -rf /"), "")


class ReviewRowsContractTest(unittest.TestCase):
    """回归：review_rows() 必须给界面"字典行"。

    原缺陷：storage.list_reviews() 返回 ReviewRecord 对象，而 terminal_gui.refresh_reviews
    按 `r["created_at"]` 取字段 —— 只要库里有**一条**复核记录，登录时的 refresh_all()
    就抛 TypeError，主界面构建中断。空库永远触发不到，所以长期没暴露。
    """

    def setUp(self):
        self.store = storage.Store(
            db_path=os.path.join(_TMP, "rv_%s.db" % models.new_id("T")))

    def test_review_rows_are_dicts(self):
        import review as review_mod
        a = models.Alert(code="LOOK_DOWN", room_id="A-01")
        self.store.save_alert(a)
        self.store.save_review(models.ReviewRecord(
            alert_id=a.alert_id, conclusion=config.REVIEW_FALSE_POSITIVE,
            reason="误报：反光", operator="patrol1", witness="owner"))
        rows = review_mod.Reviewer(self.store).review_rows(limit=10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsInstance(row, dict)
        # 界面实际用到的每个字段都必须在
        for key in ("created_at", "review_id", "operator", "conclusion",
                    "reason", "closed_loop", "alert_id", "witness"):
            self.assertIn(key, row)
        self.assertEqual(row["reason"], "误报：反光")       # 解密后仍是明文
        self.assertEqual(row["closed_loop"], 1)             # 误报进算法迭代闭环

    def test_review_rows_empty_ok(self):
        import review as review_mod
        self.assertEqual(review_mod.Reviewer(self.store).review_rows(), [])


if __name__ == "__main__":
    unittest.main()
