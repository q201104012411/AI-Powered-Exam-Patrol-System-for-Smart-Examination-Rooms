# -*- coding: utf-8 -*-
"""作弊识别引擎单元测试（标准库 unittest，无需第三方依赖）。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_engine.py
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cheatdetect import (  # noqa: E402
    CheatDetector, Config, FrameFeatures, HandState, HeadPose, PostureState,
    SignalExtractor, SignalIntensities, SignalWeights,
)
from cheatdetect import patterns  # noqa: E402
from cheatdetect import temporal  # noqa: E402
from cheatdetect.scoring import RiskScorer  # noqa: E402
from cheatdetect.simulate import (  # noqa: E402
    SCENARIOS, duration_sweep, run_simulation,
)

FPS = 10.0


def mk(i, *, yaw=0.0, pitch=8.0, relation="on_paper", below=False, cross=None,
       write=True, tilt=0.0, lean=False, left=False, objs=(), tense=False):
    """测试用帧构造。"""
    return FrameFeatures(
        frame_id=i, timestamp=i / FPS,
        head=HeadPose(yaw_deg=yaw, pitch_deg=pitch, relation=relation),
        hand=HandState(below_desk=below, cross_distance=cross,
                       doing_write=write),
        posture=PostureState(tilt_deg=tilt, leaning_on_desk=lean,
                             left_area=left),
        prohibited_objects=list(objs), expression_tense=tense)


def run(frames, cfg=None):
    """跑一遍并返回 (detector, results)。"""
    cfg = cfg or Config(fps=FPS)
    det = CheatDetector(cfg)
    res = det.push_many(frames)
    det.finish()
    return det, res


# ============================================================
class TestConfig(unittest.TestCase):
    """配置换算与参数一致性。"""

    def test_window_and_sustain_frames(self):
        cfg = Config(fps=10.0, window_seconds=2.0, min_sustain_seconds=1.0)
        self.assertEqual(cfg.window_frames, 20)
        self.assertEqual(cfg.min_sustain_frames, 10)

    def test_window_frames_min_one(self):
        cfg = Config(fps=1.0, window_seconds=0.1)
        self.assertGreaterEqual(cfg.window_frames, 1)

    def test_describe_contains_key_params(self):
        s = Config().describe()
        self.assertIn("窗口", s)
        self.assertIn("阈值", s)


# ============================================================
class TestTimeDimension(unittest.TestCase):
    """时间维度建模：方差、往复计数、持续判定。"""

    def test_head_variance_zero_for_steady_pose(self):
        frames = [mk(i) for i in range(20)]
        _, _, total = temporal.head_pose_variance(frames)
        self.assertAlmostEqual(total, 0.0, places=6)

    def test_head_variance_positive_for_swinging_head(self):
        frames = [mk(i, yaw=(40.0 if i % 2 else -40.0), relation="off_area")
                  for i in range(20)]
        _, _, total = temporal.head_pose_variance(frames)
        self.assertGreater(total, 0.2)

    def test_alternations_ignore_pure_noise(self):
        """带滞回的往复计数必须滤掉噪声引起的伪往复。"""
        noise = [0.20, 0.22, 0.19, 0.21, 0.20, 0.23, 0.18, 0.21, 0.20, 0.22]
        self.assertEqual(temporal.count_alternations(noise), 0)

    def test_alternations_count_real_repetition(self):
        """真实低头-抬头往复应被计数。"""
        series = [0.2, 0.2, 1.0, 1.0, 0.2, 0.2, 1.0, 1.0, 0.2, 0.2]
        self.assertGreaterEqual(temporal.count_alternations(series), 2)

    def test_hand_streak_requires_persistence(self):
        cfg = Config(fps=FPS)
        frames = [mk(i, below=True, cross=0.03) for i in range(5)]
        self.assertEqual(temporal.hand_interaction_streak(frames, cfg), 5)
        frames2 = [mk(0, below=True, cross=0.03), mk(1, below=False, cross=None)]
        self.assertEqual(temporal.hand_interaction_streak(frames2, cfg), 0)

    def test_sustain_tracker_gates_instantaneous(self):
        """瞬时超阈不得触发；持续超阈才触发。"""
        cfg = Config(fps=10.0, min_sustain_seconds=1.0)
        st = temporal.SustainTracker(cfg)
        f = mk(0)
        # 单帧超阈
        streak, in_alarm, just, _ = st.update(90.0, f)
        self.assertEqual(streak, 1)
        self.assertFalse(in_alarm)
        self.assertFalse(just)
        # 持续 10 帧后触发
        for i in range(1, 10):
            streak, in_alarm, just, _ = st.update(90.0, mk(i))
        self.assertTrue(in_alarm)
        self.assertTrue(just)

    def test_sustain_tracker_resets_on_drop(self):
        cfg = Config(fps=10.0, min_sustain_seconds=0.5)
        st = temporal.SustainTracker(cfg)
        for i in range(4):
            st.update(90.0, mk(i))
        self.assertFalse(st.in_alarm)
        st.update(10.0, mk(5))
        self.assertEqual(st.streak, 0)

    def test_alarm_event_lifecycle(self):
        """告警事件应记录起止时间与峰值。"""
        cfg = Config(fps=10.0, min_sustain_seconds=0.3,
                     alarm_cooldown_seconds=0.0)
        st = temporal.SustainTracker(cfg)
        for i in range(8):
            st.update(85.0, mk(i))
        self.assertEqual(len(st.events), 1)
        ev = st.events[0]
        self.assertTrue(ev.ongoing)
        st.update(5.0, mk(9))
        self.assertFalse(ev.ongoing)
        self.assertAlmostEqual(ev.onset_time, 0.0)
        self.assertAlmostEqual(ev.offset_time, 0.9)
        self.assertGreater(ev.peak_risk, 80.0)


# ============================================================
class TestSignalExtraction(unittest.TestCase):
    """信号强度提取的边界行为。"""

    def setUp(self):
        self.cfg = Config(fps=FPS)
        self.ex = SignalExtractor(self.cfg)

    def test_head_deviation_zero_on_paper(self):
        """低头看自己试卷（on_paper）永不计分 —— 防“长时间低头”误报。"""
        f = mk(0, pitch=45.0, relation="on_paper")
        self.assertEqual(self.ex.head_deviation(f), 0.0)

    def test_head_deviation_grows_with_angle(self):
        small = self.ex.head_deviation(mk(0, yaw=32.0, relation="toward_neighbor"))
        large = self.ex.head_deviation(mk(0, yaw=52.0, relation="toward_neighbor"))
        self.assertGreater(large, small)
        self.assertGreater(small, 0.0)

    def test_head_deviation_below_threshold_is_zero(self):
        f = mk(0, yaw=20.0, relation="toward_neighbor")
        self.assertEqual(self.ex.head_deviation(f), 0.0)

    def test_object_detection_criticality(self):
        """手机应比书籍的危险度更高。"""
        phone = self.ex.prohibited_object(mk(0, objs=[("phone", 0.9)]))
        book = self.ex.prohibited_object(mk(0, objs=[("book", 0.9)]))
        self.assertGreater(phone, book)
        self.assertEqual(self.ex.prohibited_object(mk(0)), 0.0)

    def test_hand_interaction_needs_3_consecutive_frames(self):
        two = [mk(i, below=True, cross=0.03) for i in range(2)]
        self.assertEqual(self.ex.hand_interaction(two), 0.0)
        four = [mk(i, below=True, cross=0.03) for i in range(4)]
        self.assertGreater(self.ex.hand_interaction(four), 0.0)

    def test_hand_interaction_requires_below_desk_or_leaning(self):
        """手在桌面之上时不算跨考生交互。"""
        frames = [mk(i, below=False, cross=0.02, lean=False) for i in range(5)]
        self.assertEqual(self.ex.hand_interaction(frames), 0.0)

    def test_writing_mitigates_hand_interaction(self):
        c = [mk(i, below=True, cross=0.03, write=True) for i in range(5)]
        n = [mk(i, below=True, cross=0.03, write=False) for i in range(5)]
        self.assertLess(self.ex.hand_interaction(c), self.ex.hand_interaction(n))

    def test_persistence_scales_with_duration(self):
        """持续占比应随时长上升 —— “持续时间”的量化。"""
        short = [mk(i, yaw=50.0, relation="toward_neighbor") for i in range(20)]
        p_short = self.ex.persistence(short)["head_deviation"]
        mixed = ([mk(i, pitch=8.0) for i in range(15)]
                 + [mk(i, yaw=50.0, relation="toward_neighbor")
                    for i in range(15, 20)])
        p_mixed = self.ex.persistence(mixed)["head_deviation"]
        self.assertGreater(p_short, p_mixed)


# ============================================================
class TestScoring(unittest.TestCase):
    """加权风险评分：非二值、权重方向、持续性增益。"""

    def setUp(self):
        self.cfg = Config(fps=FPS)
        self.sc = RiskScorer(self.cfg)

    def test_weight_direction_ordering(self):
        """权重方向必须满足：极高 > 高 > 中。"""
        w = SignalWeights()
        self.assertGreater(w.prohibited_object, w.hand_interaction)
        self.assertGreater(w.hand_interaction, w.head_deviation)
        self.assertGreater(w.head_deviation, w.head_variance * 0.0)  # 非负

    def test_single_medium_instant_not_enough(self):
        """单个中等信号在【无持续增益】时不得触发。"""
        s = SignalIntensities(head_deviation=1.0)
        self.assertLess(self.sc.instant_risk(s), self.cfg.risk_threshold)

    def test_sustained_medium_can_trigger(self):
        """中等信号【持续满窗】时可触发（对应“持续转头面向邻座”）。"""
        s = SignalIntensities(head_deviation=1.0)
        pers = {"head_deviation": 1.0}
        self.assertGreaterEqual(self.sc.instant_risk(s, pers),
                                self.cfg.risk_threshold)

    def test_combination_exceeds_single(self):
        one = SignalIntensities(head_deviation=0.8)
        two = SignalIntensities(head_deviation=0.8, hand_interaction=0.8)
        self.assertGreater(self.sc.instant_risk(two), self.sc.instant_risk(one))

    def test_risk_is_clamped_to_100(self):
        s = SignalIntensities(**{k: 1.0 for k in (
            "head_deviation", "head_variance", "hand_interaction",
            "prohibited_object", "posture_repetition", "expression_aux")})
        self.assertLessEqual(self.sc.instant_risk(s, {k: 1.0 for k in (
            "head_deviation", "head_variance", "hand_interaction",
            "prohibited_object", "posture_repetition", "expression_aux")}), 100.0)

    def test_persistence_boost_increases_score(self):
        s = SignalIntensities(head_deviation=0.8)
        low = self.sc.instant_risk(s, {"head_deviation": 0.0})
        high = self.sc.instant_risk(s, {"head_deviation": 1.0})
        self.assertGreater(high, low)

    def test_levels(self):
        self.assertEqual(self.sc.level_of(10.0, False), "正常")
        self.assertEqual(self.sc.level_of(40.0, False), "关注")
        self.assertEqual(self.sc.level_of(65.0, True), "橙色预警")
        self.assertEqual(self.sc.level_of(90.0, True), "红色预警")


# ============================================================
class TestPatterns(unittest.TestCase):
    """行为模式识别。"""

    def test_object_pattern(self):
        c = {"prohibited_object": 70.0, "head_deviation": 5.0}
        self.assertEqual(patterns.classify(c), patterns.PATTERN_OBJECT)

    def test_handoff_pattern(self):
        c = {"hand_interaction": 60.0, "head_deviation": 10.0}
        self.assertEqual(patterns.classify(c), patterns.PATTERN_HANDOFF)

    def test_peek_pattern(self):
        c = {"head_deviation": 50.0, "head_variance": 10.0}
        self.assertEqual(patterns.classify(c), patterns.PATTERN_PEEK)

    def test_no_pattern_for_empty(self):
        self.assertEqual(patterns.classify({}), patterns.PATTERN_NONE)
        self.assertEqual(patterns.classify({"head_deviation": 0.0}),
                         patterns.PATTERN_NONE)

    def test_reason_text_mentions_sustain(self):
        c = {"head_deviation": 50.0}
        txt = patterns.reason_text(patterns.PATTERN_PEEK, c, 80.0, 60.0, True, 12)
        self.assertIn("持续超过阈值", txt)


# ============================================================
class TestDetectorEndToEnd(unittest.TestCase):
    """端到端判定行为 —— 项目书核心要求的直接验证。"""

    def test_quiet_writing_no_alarm(self):
        frames = [mk(i) for i in range(40)]
        det, _ = run(frames)
        self.assertEqual(det.summary()["alarm_events"], 0)

    def test_single_frame_spike_suppressed(self):
        """单帧瞬时动作必须被抑制（项目书核心要求）。"""
        frames = [mk(i) for i in range(40)]
        frames[20] = mk(20, yaw=70.0, relation="toward_back")
        det, _ = run(frames)
        self.assertEqual(det.summary()["alarm_events"], 0)
        self.assertGreaterEqual(det.summary()["suppressed_frames"], 1)

    def test_brief_movement_suppressed(self):
        """连续 3 帧（0.3s）瞬时动作仍应被抑制。"""
        frames = [mk(i) for i in range(40)]
        for i in range(20, 23):
            frames[i] = mk(i, yaw=70.0, relation="toward_back")
        det, _ = run(frames)
        self.assertEqual(det.summary()["alarm_events"], 0)

    def test_sustained_head_turn_triggers(self):
        """持续转头面向邻座应触发（对应“持续转头即可疑信号”）。"""
        frames = [mk(i, yaw=52.0, pitch=4.0, relation="toward_neighbor")
                  for i in range(40)]
        det, _ = run(frames)
        s = det.summary()
        self.assertGreater(s["alarm_events"], 0)
        self.assertGreaterEqual(s["peak_risk"], 60.0)

    def test_sustained_hand_interaction_triggers(self):
        """桌面下手部持续靠近邻座应触发（传递物品模式）。"""
        frames = []
        for i in range(40):
            if i < 5:
                frames.append(mk(i))
            else:
                frames.append(mk(i, below=True, cross=0.03, write=False,
                                 yaw=35.0, relation="toward_neighbor"))
        det, _ = run(frames)
        self.assertGreater(det.summary()["alarm_events"], 0)

    def test_prohibited_object_triggers(self):
        """手机持续检出应触发，且级别达到红色预警。"""
        frames = []
        for i in range(40):
            if i < 4:
                frames.append(mk(i))
            else:
                frames.append(mk(i, objs=[("phone", 0.9)], below=True,
                                 pitch=40.0, relation="off_area"))
        det, _ = run(frames)
        s = det.summary()
        self.assertGreater(s["alarm_events"], 0)
        self.assertEqual(det.events[0].level, "红色预警")

    def test_long_lookdown_at_own_paper_no_alarm(self):
        """长时间低头看自己试卷不得误报。"""
        frames = [mk(i, pitch=45.0, relation="on_paper") for i in range(40)]
        det, _ = run(frames)
        self.assertEqual(det.summary()["alarm_events"], 0)

    def test_alarm_event_closes_when_risk_drops(self):
        """风险回落后事件应闭合，且记录持续时长。"""
        frames = [mk(i, yaw=55.0, relation="toward_neighbor") for i in range(30)]
        frames += [mk(i) for i in range(30, 70)]
        det, _ = run(frames)
        self.assertEqual(len(det.events), 1)
        self.assertFalse(det.events[0].ongoing)
        self.assertGreater(det.events[0].duration, 0.0)

    def test_result_is_explainable(self):
        """每帧结果必须给出贡献分解与理由文本（可解释输出）。"""
        frames = [mk(i, yaw=52.0, relation="toward_neighbor") for i in range(30)]
        det, res = run(frames)
        last = res[-1]
        self.assertTrue(last.contributions)
        self.assertTrue(last.reason)
        self.assertIn(last.pattern, patterns.PATTERN_DESCRIPTION)

    def test_summary_independent_of_record_flag(self):
        """summary 不应依赖是否记录逐帧结果。"""
        frames = [mk(i, yaw=52.0, relation="toward_neighbor") for i in range(40)]
        det = CheatDetector(Config(fps=FPS))
        det.push_many(frames, record=False)
        det.finish()
        self.assertEqual(det.summary()["frames"], 40)
        self.assertGreater(det.summary()["alarm_events"], 0)

    def test_empty_input_safe(self):
        det = CheatDetector(Config(fps=FPS))
        det.finish()
        self.assertEqual(det.summary()["frames"], 0)

    def test_reset_clears_state(self):
        frames = [mk(i, yaw=52.0, relation="toward_neighbor") for i in range(40)]
        det, _ = run(frames)
        self.assertGreater(det.summary()["alarm_events"], 0)
        det.reset()
        self.assertEqual(det.summary()["alarm_events"], 0)
        self.assertEqual(det.summary()["frames"], 0)


# ============================================================
class TestSimulation(unittest.TestCase):
    """仿真框架与指标口径。"""

    def test_scenario_registry_has_both_classes(self):
        truths = {s.truth for s in SCENARIOS}
        self.assertIn("normal", truths)
        self.assertIn("cheat", truths)
        self.assertIn("ambiguous", truths)

    def test_duration_sweep_is_monotonic_ish(self):
        """触发率应随持续时间上升：短动作 0%，长动作 100%。"""
        rows = duration_sweep(trials=10, max_frames=20, seed=7)
        short = [r for r in rows if r["frames"] <= 2]
        long_ = [r for r in rows if r["frames"] >= 15]
        for r in short:
            self.assertLessEqual(r["trigger_rate"], 0.2)
        for r in long_:
            self.assertGreaterEqual(r["trigger_rate"], 0.9)

    def test_simulation_metrics_beat_frame_baseline(self):
        """时空引擎的误报率应显著低于帧级基线。"""
        rep = run_simulation(trials_per_scenario=8, seed=11)
        b = rep.metrics(True)
        e = rep.metrics(False)
        self.assertLess(e["false_positive_rate"], b["false_positive_rate"])
        self.assertGreaterEqual(e["recall"], 0.9)

    def test_ambiguous_excluded_from_metrics(self):
        rep = run_simulation(trials_per_scenario=4, seed=3)
        m = rep.metrics(False)
        amb_trials = sum(r.trials for r in rep.results if r.is_ambiguous)
        self.assertGreater(amb_trials, 0)
        self.assertEqual(m["n"], rep.trials_per_scenario
                         * len([r for r in rep.results if not r.is_ambiguous]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
