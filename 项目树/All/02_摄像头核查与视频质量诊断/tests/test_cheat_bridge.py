# -*- coding: utf-8 -*-
"""作弊识别结合层测试（离线、确定性，不需要摄像头）。

锁住的是**结合口径**，不是引擎内部（引擎自带 75 项测试在 作弊识别\\tests 里）：
  · 本项目人框/人脸/VLM 物体 → FrameFeatures 的映射对不对
  · "瞬时动作被抑制、持续模式才告警"这个关键行为有没有被改坏
  · 送帧频率与窗口/持续帧数的换算（填错会永远不告警）
  · 不可得信号是否如实标注（不假装有手部/物体模型）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cheat_bridge as cb          # noqa: E402


def person(pid=1, rect=(500, 200, 180, 300), action="正常姿态", behaviors=(),
           bad=False, face=None, phone=False):
    return {"pid": pid, "rect": rect, "action": action, "behaviors": list(behaviors),
            "bad": bad, "crosses": 0, "face": face, "phone": phone}


class TestCheatBridge(unittest.TestCase):

    def setUp(self):
        if not cb.CheatWatch(feed_hz=2.0).ok:
            self.skipTest("作弊识别引擎不可用（作弊识别\\ 目录缺失或依赖不全）")

    # ---------------- 基本可用性 ----------------
    def test_describe_and_signals_are_honest(self):
        w = cb.CheatWatch(feed_hz=2.0)
        self.assertIn("送帧 2.0 帧/秒", w.describe())
        notes = w.signal_notes()
        # 摄像头路径下这三项引擎自己声明"不可得"，结合层必须原样透出
        self.assertIn("hand_interaction=不可得", notes)
        self.assertIn("prohibited_object=不可得", notes)
        self.assertIn("head_variance=实测", notes)

    def test_feed_hz_drives_window_and_sustain_frames(self):
        """窗口 2s / 最短持续 1s 按帧数换算：送 2 帧/秒就该是 4 帧与 2 帧"""
        w = cb.CheatWatch(feed_hz=2.0)
        self.assertEqual(w.cfg.window_frames, 4)
        self.assertEqual(w.cfg.min_sustain_frames, 2)
        w10 = cb.CheatWatch(feed_hz=10.0)
        self.assertEqual(w10.cfg.window_frames, 20)
        self.assertEqual(w10.cfg.min_sustain_frames, 10)

    def test_push_without_perception_says_why(self):
        """默认不启用引擎自带感知（复用项目检测结果），走 push() 要如实说明"""
        w = cb.CheatWatch(feed_hz=2.0)
        r = w.push(None)
        self.assertFalse(r["ok"])
        self.assertIn("感知", r["reason"])

    def test_no_person_reports_reason_without_fake_frame(self):
        """本帧没有人 → 不喂假帧，只如实报原因"""
        w = cb.CheatWatch(feed_hz=2.0)
        r = w.push_persons([], frame_shape=(720, 1280))
        self.assertFalse(r["ok"])
        self.assertIn("没有可用的人框", r["reason"])

    # ---------------- 目标选择 ----------------
    def test_target_prefers_suspicious_person(self):
        w = cb.CheatWatch(feed_hz=2.0)
        big_quiet = person(pid=1, rect=(100, 100, 400, 600))
        small_suspect = person(pid=7, rect=(600, 200, 120, 200),
                               behaviors=["大幅度偏转"], bad=True)
        self.assertEqual(w._pick_target_person([big_quiet, small_suspect])["pid"], 7)

    def test_target_is_largest_when_all_normal(self):
        w = cb.CheatWatch(feed_hz=2.0)
        a = person(pid=1, rect=(100, 100, 100, 100))
        b = person(pid=2, rect=(400, 100, 300, 400))
        self.assertEqual(w._pick_target_person([a, b])["pid"], 2)

    # ---------------- 映射口径 ----------------
    def test_yaw_from_face_offset_and_phone_from_vlm(self):
        """人脸偏到框右侧 → yaw 为正（relation 判为朝向邻座）；VLM 判出手机 → 物体信号"""
        w = cb.CheatWatch(feed_hz=2.0)
        # 框 500..680，框心 590；人脸中心放到 660（偏右）
        p = person(rect=(500, 200, 180, 300), face=(640, 215, 40, 40))
        r = w.push_persons([p], frame_shape=(720, 1280), timestamp=0.0,
                           vlm_objects=[{"label": "手机", "confidence": 0.8}])
        self.assertTrue(r["ok"])
        self.assertGreater(r["yaw_deg"], 0)
        self.assertEqual(r["objects"], ["phone"])
        self.assertEqual(r["source"], "project")

    def test_phone_flag_on_person_also_counts(self):
        w = cb.CheatWatch(feed_hz=2.0)
        r = w.push_persons([person(phone=True)], frame_shape=(720, 1280))
        self.assertIn("phone", r["objects"])

    def test_standing_marks_left_area(self):
        """起立/站立 → 离开台面区域信号（引擎的 left_area）"""
        w = cb.CheatWatch(feed_hz=2.0)
        r = w.push_persons([person(action="起立/站立", behaviors=["起立/站立"])],
                           frame_shape=(720, 1280))
        self.assertTrue(r["left_area"])

    def test_leaning_marks_posture(self):
        w = cb.CheatWatch(feed_hz=2.0)
        r = w.push_persons([person(action="前倾/趴桌", behaviors=["前倾/趴桌"])],
                           frame_shape=(720, 1280))
        self.assertTrue(r["leaning"])

    # ---------------- 关键行为：抑制 vs 告警 ----------------
    def test_quiet_writing_never_alarms(self):
        w = cb.CheatWatch(feed_hz=2.0)
        for i in range(20):
            r = w.push_persons([person()], frame_shape=(720, 1280), timestamp=i * 0.5)
        self.assertFalse(r["triggered"])
        self.assertEqual(w.alarms, [])
        self.assertIn("正常", w.status_text())

    def test_single_instant_action_is_suppressed(self):
        """单帧瞬时动作（只一帧异常，其余正常）不得告警 —— 现场最容易误报的形态"""
        w = cb.CheatWatch(feed_hz=2.0)
        for i in range(16):
            spike = (i == 8)
            p = person(rect=(500, 200, 240 if spike else 180, 300),
                       action="大幅度偏转" if spike else "正常姿态",
                       behaviors=["大幅度偏转"] if spike else [],
                       face=(640, 215, 40, 40))
            w.push_persons([p], frame_shape=(720, 1280), timestamp=i * 0.5)
        self.assertEqual(w.alarms, [], "瞬时动作不应触发告警")
        self.assertLess(w.last["risk"], w.last["threshold"])

    def test_sustained_pattern_alarms(self):
        """持续侧头 + 手机（VLM 判出）→ 应告警，且模式指向违禁物品"""
        w = cb.CheatWatch(feed_hz=2.0)
        for i in range(16):
            p = person(rect=(500, 200, 240, 300), action="大幅度偏转",
                       behaviors=["大幅度偏转"], face=(640, 215, 40, 40),
                       phone=(i > 2))
            r = w.push_persons([p], frame_shape=(720, 1280), timestamp=i * 0.5,
                               vlm_objects=([{"label": "手机"}] if i > 2 else None))
        self.assertTrue(r["triggered"], "持续模式应触发告警")
        self.assertGreaterEqual(r["risk"], r["threshold"])
        self.assertEqual(len(w.alarms), 1)
        self.assertIn("手机", w.last["pattern_desc"] + w.last["reason_text"])

    def test_status_and_badge_text(self):
        w = cb.CheatWatch(feed_hz=2.0)
        self.assertIn("待分析", w.status_text())
        for i in range(14):
            w.push_persons([person(rect=(500, 200, 240, 300), action="大幅度偏转",
                                   behaviors=["大幅度偏转"], phone=True)],
                           frame_shape=(720, 1280), timestamp=i * 0.5)
        self.assertIn("作弊风险", w.status_text())
        self.assertTrue(w.badge_text())          # 告警中必须有角标

    def test_summary_and_reset(self):
        w = cb.CheatWatch(feed_hz=2.0)
        for i in range(14):
            w.push_persons([person(behaviors=["大幅度偏转"], phone=True, bad=True)],
                           frame_shape=(720, 1280), timestamp=i * 0.5)
        s = w.summary()
        self.assertTrue(s["ok"])
        self.assertEqual(s["feed_hz"], 2.0)
        self.assertGreaterEqual(s["alarms"], 1)
        w.reset()
        self.assertEqual(w.alarms, [])
        self.assertIsNone(w.last)
        self.assertIn("待分析", w.status_text())


if __name__ == "__main__":
    unittest.main()
