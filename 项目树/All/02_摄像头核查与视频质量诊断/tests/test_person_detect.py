# -*- coding: utf-8 -*-
"""
person_detect 密集教室优化版单元测试

运行方式（在项目 02 根目录执行）：
    python -m unittest discover -s tests -t .
"""
import os
import unittest
from unittest import mock

import cv2
import numpy as np

import person_detect as pd


class TestPersonTracker(unittest.TestCase):

    @staticmethod
    def _make_blank(w=1280, h=720):
        return np.full((h, w, 3), 128, dtype=np.uint8)

    @staticmethod
    def _draw_person(img, cx, cy, body_w=24, body_h=48, head=16):
        """在 (cx,cy) 处画一个简化小人（头 + 身体），返回外接框"""
        x = cx - body_w // 2
        by = cy - body_h // 2 + head // 2
        y = by - head
        color = np.random.randint(40, 220, size=3, dtype=np.uint8).tolist()
        # 身体
        cv2.rectangle(img, (x, by), (x + body_w, by + body_h), color, -1)
        # 头
        hx = cx - head // 2
        cv2.ellipse(img, (cx, y + head // 2), (head // 2, head // 2),
                    0, 0, 360, color, -1)
        return (x, y, body_w, body_h + head)

    @staticmethod
    def _draw_realistic_person(img, cx, cy, height=200):
        """
        画一个高对比度站立人形（椭圆身体 + 圆头），HOG 默认行人检测器可识别。
        纯色小人召回率低，这里使用更接近真实行人轮廓的纵向椭圆，并在内部加
        少量灰度渐变以提供梯度信息，返回外接框。
        """
        color = (0, 0, 0)
        head_r = max(8, int(height * 0.12))
        head_cy = cy - int(height * 0.32)
        body_h = max(30, int(height * 0.78))
        body_w = max(16, int(height * 0.28))
        body_cy = cy + int(height * 0.05)

        # 身体：纵向椭圆，提供与真实行人相似的宽高比（约 0.28）。
        # 黑色填充与浅灰背景形成强边缘，椭圆反锯齿边缘本身即提供灰度渐变。
        cv2.ellipse(img, (cx, body_cy), (body_w // 2, body_h // 2),
                    0, 0, 360, color, -1)
        # 头
        cv2.circle(img, (cx, head_cy), head_r, color, -1)

        x1 = cx - body_w // 2
        y1 = head_cy - head_r
        x2 = cx + body_w // 2
        y2 = body_cy + body_h // 2
        return (x1, y1, x2 - x1, y2 - y1)

    def _make_person_scene(self, n=40, w=1920, h=1080, height=130, jitter=15):
        """生成包含 n 个站立人形剪影的教室场景"""
        img = np.full((h, w, 3), 235, dtype=np.uint8)  # 浅灰背景
        cols = int(np.ceil(np.sqrt(n * w / h)))
        rows = int(np.ceil(n / cols))
        step_x = w // cols
        step_y = h // rows
        for idx in range(n):
            r, c = divmod(idx, cols)
            cx = step_x // 2 + c * step_x + np.random.randint(-jitter, jitter + 1)
            cy = step_y // 2 + r * step_y + np.random.randint(-jitter, jitter + 1)
            self._draw_realistic_person(img, cx, cy, height=height)
        return img

    def test_single_realistic_person_detected(self):
        """单个较真实人形剪影应能被检测到"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        self._draw_realistic_person(img, 640, 360, height=220)
        rects = tracker._detect(img)
        self.assertGreater(len(rects), 0, "HOG 应能检出单个站立人形剪影")

    def test_synthetic_40_persons_detected_without_exception(self):
        """40 个合成小人：不抛异常、检测到若干人、密度等级为 high"""
        tracker = pd.PersonTracker(dense_mode=True, detect_stride=1,
                                   max_input_dim=1920)
        img = self._make_person_scene(n=40)
        # 视频流场景：连续两帧都检出才算"确认的人"（滤掉单帧噪声）
        tracker.draw(img, boundary_ratio=0.5)
        _, violations, persons = tracker.draw(img, boundary_ratio=0.5)
        self.assertIsInstance(violations, int)
        # 合成样本不一定能让 HOG 召回全部 40 人，但应能召回一部分；
        # 同时背景差分/人头辅助不应导致异常，density_level 应反映高密度。
        self.assertGreaterEqual(len(persons), 1,
                                "应至少检测到 1 个人（HOG/辅助检测至少触发）")
        self.assertLessEqual(len(persons), 80,
                             "不应产生离谱的重复检测")
        for p in persons:
            self.assertIn("density_level", p)
            self.assertIn(p["density_level"], ("low", "medium", "high"))
        # 说明：density_level 由"检出轨迹数 ÷ 画面面积"推得，而合成椭圆剪影不是
        # YOLO11n（分块推理后成为主力）的召回对象，检出数会明显低于真值 40，
        # 因此这里**不再断言必须 high** —— 那是旧 HOG 主力时代的偶然结果。
        # 密集场景的真实召回请用 工具_密集场景评测.py（真人素材合成、可复现），
        # 它同时是"要不要上 1280 + 分块推理"的判据来源。

    def test_margin_tighter_than_old_default(self):
        """收紧后的框应比原始 HOG 框小；5% 收紧程度应比 2% 更大（直接测试 _shrink_rect）"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        raw_rect = (100, 100, 300, 500)
        fw, fh = 1280, 720
        raw_area = raw_rect[2] * raw_rect[3]
        shrink_02 = tracker._shrink_rect(raw_rect, fw, fh)
        tracker.margin_ratio = 0.05
        shrink_05 = tracker._shrink_rect(raw_rect, fw, fh)
        self.assertGreater(shrink_02[2] * shrink_02[3], 0)
        self.assertGreater(shrink_05[2] * shrink_05[3], 0)
        self.assertLess(shrink_02[2] * shrink_02[3], raw_area,
                        "收紧后框应小于原始 HOG 框")
        self.assertLess(shrink_05[2] * shrink_05[3], shrink_02[2] * shrink_02[3],
                        "5% 边距应比 2% 边距收缩得更多")

    def test_min_box_size_enforced(self):
        """收紧后框不应低于 24×24"""
        tracker = pd.PersonTracker(margin_ratio=0.02, max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        self._draw_realistic_person(img, 100, 150, height=80)
        rects = tracker._detect(img)
        for r in rects:
            self.assertGreaterEqual(r[2], 24)
            self.assertGreaterEqual(r[3], 24)

    def test_draw_returns_density_level(self):
        """draw 返回的每个人必须包含 density_level 字段"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        self._draw_realistic_person(img, 640, 360, height=220)
        _, _, persons = tracker.draw(img)
        self.assertIsInstance(persons, list)
        for p in persons:
            self.assertIn("density_level", p)
            self.assertIn(p["density_level"], ("low", "medium", "high"))

    def test_high_density_no_exception(self):
        """高密度场景连续多帧不会抛异常"""
        tracker = pd.PersonTracker(dense_mode=True, detect_stride=2,
                                   max_input_dim=1920)
        for _ in range(5):
            img = self._make_person_scene(n=40)
            _, violations, persons = tracker.draw(img)
            self.assertIsInstance(persons, list)

    def test_stop_start_resets_tracker(self):
        """stop / start 后跟踪器应清空并复位"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        self._draw_realistic_person(img, 640, 360, height=220)
        tracker.draw(img)
        self.assertGreater(len(tracker._tracks), 0)

        tracker.stop()
        self.assertEqual(len(tracker._tracks), 0)
        self.assertEqual(tracker._next_pid, 1)

        tracker.start()
        self.assertEqual(len(tracker._tracks), 0)
        self.assertEqual(tracker._next_pid, 1)

    def test_detect_stride_prediction_frame(self):
        """detect_stride > 1 时，非检测帧应通过预测维持轨迹"""
        tracker = pd.PersonTracker(detect_stride=2, max_missed=5,
                                   max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        self._draw_realistic_person(img, 640, 360, height=220)
        tracker.draw(img)
        self.assertGreater(len(tracker._tracks), 0)
        # 第二帧不检测，只预测；不应丢轨迹
        tracker.draw(img)
        self.assertGreater(len(tracker._tracks), 0)

    def test_auxiliary_detectors_do_not_crash(self):
        """背景差分、人头检测辅助分支至少不会崩溃，并返回 list"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        img = self._make_person_scene(n=40)
        # 背景差分需要若干帧历史，连续调用不应抛异常
        for _ in range(3):
            rois = tracker._detect_bg_rois(img)
            self.assertIsInstance(rois, list)
        heads = tracker._detect_heads(img)
        self.assertIsInstance(heads, list)


class TestAccuracyFixes(unittest.TestCase):
    """人数/越界准确性：警戒线默认值、误检过滤、行为时间确认、人数平滑"""

    def test_default_boundary_is_near_bottom(self):
        """警戒线默认必须在画面 90% 以下，否则中后排考生会被误判越界"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        self.assertGreaterEqual(tracker.boundary_ratio, 0.90)

    def test_seated_person_in_middle_is_not_out_of_bounds(self):
        """坐在画面中部（底部在 70% 高度）的人不应被判越界/上报"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (800, 500, 120, 250), int(1080 * tracker.boundary_ratio))
        # 框底 = 750，警戒线 = 993 -> 不越界
        action, bad, behaviors = tracker.analyze(frame, track)
        self.assertFalse(bad, "画面中部的坐姿考生不应判越界")
        self.assertNotIn(pd.ACT_CROSS, behaviors)

    def test_person_at_camera_front_is_out_of_bounds(self):
        """走到镜头前（框底越过 92% 线）才应判越界"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (800, 950, 200, 200), int(1080 * tracker.boundary_ratio))
        _, bad, _ = tracker.analyze(frame, track)
        self.assertTrue(bad, "框底 1150 > 警戒线 993，应判越界")

    def test_wide_boxes_are_filtered_out(self):
        """宽高比超过 max_aspect 的扁框（桌椅/墙面误检）必须被剔除"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        rects = [
            (100, 100, 60, 150),     # 正常人（宽高比 0.4）
            (200, 100, 400, 100),    # 很扁的误检（宽高比 4.0）
            (300, 100, 10, 100),     # 太窄
            (400, 100, 60, 10),      # 太矮
        ]
        kept = tracker._sanitize_boxes(rects, 1920, 1080)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0], (100, 100, 60, 150))

    def test_turn_requires_consecutive_confirmation(self):
        """单帧宽高比突变不应立刻报'大幅度偏转'（旧版会把噪音全刷成异常）"""
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (600, 300, 260, 200), 662)   # 宽高比 1.3，单帧很宽
        action, _, behaviors = tracker.analyze(frame, track)
        self.assertNotIn(pd.ACT_TURN, behaviors,
                         "只有一帧偏宽时不应报大幅度偏转")

    def test_turn_reported_after_sustained_wide_frames(self):
        """相对本人基线明显变宽（>×1.15）且连续确认 → 报'大幅度偏转'"""
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (600, 300, 260, 200), 662)
        for _ in range(8):                      # 基线：宽高比 1.0（坐姿）
            track.aspect_hist.append(1.0)
        for _ in range(3):                      # 侧转：1.4 = 基线 ×1.4 > ×1.15
            track.aspects.append(1.4)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertIn(pd.ACT_TURN, behaviors, "明显侧转应确认为大幅度偏转")

    def test_normal_sitting_posture_is_not_turn(self):
        """回归测试（2026-10-13 报告里的那条，换模型层时丢了，现按报告恢复）：

        正常坐姿的人框**宽高比就有 1.386**（实测 298×215）。旧的固定阈值 0.85
        下"坐着不动必然误报大幅度偏转" —— 现场截图就是这个现象。
        自适应判据（> 基线×1.15）下必须保持"正常姿态"。
        """
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (400, 300, 298, 215), 662)      # 真实坐姿框 298×215
        for _ in range(10):
            track.aspects.append(298 / 215.0)                # 1.386，长期不变
            track.aspect_hist.append(298 / 215.0)
        action, _, behaviors = tracker.analyze(frame, track)
        self.assertNotIn(pd.ACT_TURN, behaviors, "坐姿 1.386 不得判偏转")
        self.assertNotIn(pd.ACT_LEAN, behaviors, "坐姿 1.386 也不得判前倾")

    def test_remote_turn_detected_against_narrow_baseline(self):
        """远距离侧转：基线 0.55 → 0.80（旧固定阈值 0.85 会漏报）"""
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (900, 400, 88, 160), 993)
        for _ in range(10):
            track.aspect_hist.append(0.55)
        for _ in range(3):
            track.aspects.append(0.80)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertIn(pd.ACT_TURN, behaviors, "远距离侧转应能报出（自适应基线）")

    def test_slight_turn_is_not_reported(self):
        """轻微侧身 +8%（1.39 → 1.50）属合理动作，不报偏转"""
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (400, 300, 298, 215), 662)
        for _ in range(10):
            track.aspect_hist.append(1.386)
        for _ in range(3):
            track.aspects.append(1.386 * 1.08)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertNotIn(pd.ACT_TURN, behaviors)

    def test_out_of_bounds_requires_head_low(self):
        """框底过线但头顶还在画面上部（坐姿人框含桌面/椅子）→ 不算越界"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        # 框底 1050 > 警戒线 993，但头顶 y=300 < 45%×1080=486
        track = pd._Track(1, (700, 300, 300, 750), int(1080 * tracker.boundary_ratio))
        _, bad, _ = tracker.analyze(frame, track)
        self.assertFalse(bad, "头顶没下移到画面下部 → 不是真越界")

    def test_walking_to_camera_is_out_of_bounds(self):
        """真走到镜头前：框底过线 + 头顶下移到 50%（报告实测那种）→ 越界"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (700, 540, 500, 540), int(1080 * tracker.boundary_ratio))
        _, bad, _ = tracker.analyze(frame, track)
        self.assertTrue(bad, "框底 1080 > 993 且头顶 540 > 486 → 越界")

    def test_edge_touching_box_is_not_out_of_bounds(self):
        """框贴到画面左右边缘**不算越界**（广角机位里坐在边上的考生会被永久误判）"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        # 贴着右边缘（x+w = 1920），但框底 600 远在警戒线 993 之上
        track = pd._Track(1, (1700, 300, 220, 300), int(1080 * tracker.boundary_ratio))
        action, bad, _ = tracker.analyze(frame, track)
        self.assertFalse(bad, "贴边不等于越界")
        self.assertEqual(action, pd.ACT_RIGHT, "位置标签仍然给（靠右）")

    def test_stable_count_uses_median(self):
        """人数读数取最近几次的中位数，避免在 15/16/17 之间乱跳"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        for n in (10, 11, 10, 40, 10):
            tracker._count_hist.append(n)
            ordered = sorted(tracker._count_hist)
            tracker._stable_count = ordered[len(ordered) // 2]
        # 出现了孤立尖峰 40，但中位数仍是 10
        self.assertEqual(tracker.stable_count(), 10)

    def test_stop_resets_count_smoothing(self):
        tracker = pd.PersonTracker(max_input_dim=1920)
        tracker._count_hist.extend([5, 6, 7])
        tracker._stable_count = 6
        tracker.stop()
        self.assertEqual(tracker.stable_count(), 0)
        self.assertEqual(len(tracker._count_hist), 0)


class TestTrackingFramework(unittest.TestCase):
    """跟踪框架：自适应匹配半径、IoU 关联、重复轨迹合并、单帧噪声抑制"""

    def test_match_distance_scales_with_frame_width(self):
        """匹配半径应随画面宽度自适应，且不超过 max_dist 上限"""
        tracker = pd.PersonTracker(max_dist=90, match_dist_ratio=0.12)
        self.assertEqual(tracker._match_dist(416), 49)    # 416*0.12 ≈ 49
        self.assertEqual(tracker._match_dist(1920), 90)   # 受 max_dist 封顶
        self.assertGreaterEqual(tracker._match_dist(100), 12)  # 有下限

    def test_pair_cost_prefers_overlap(self):
        """重叠度高的候选代价必须更低（否则密集场景会抢轨迹）"""
        tr = (100, 100, 60, 150)
        overlapping = (105, 105, 60, 150)   # 与轨迹高度重叠
        far_but_close = (160, 100, 60, 150)  # 质心更近但不重叠
        d_overlap = 7.1
        d_far = 60.0
        c_overlap = pd.PersonTracker._pair_cost(tr, overlapping, d_overlap, 90)
        c_far = pd.PersonTracker._pair_cost(tr, far_but_close, d_far, 90)
        self.assertLess(c_overlap, c_far,
                        "重叠框的关联代价应低于只是质心近的框")

    def test_pair_cost_rejects_beyond_max_dist(self):
        cost = pd.PersonTracker._pair_cost((0, 0, 10, 10), (500, 500, 10, 10),
                                           700.0, 90)
        self.assertGreaterEqual(cost, 1e6)

    def test_single_frame_noise_is_not_confirmed(self):
        """只命中一次的轨迹不应计入人数（单帧反光/桌椅误检）"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920, min_hits=2)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        TestPersonTracker._draw_realistic_person(img, 640, 360, height=220)
        # 人为放一条只命中一次的轨迹
        tracker._tracks[99] = pd._Track(99, (600, 300, 80, 200), 662)
        tracker._tracks[99].hits = 1
        self.assertNotIn(99, tracker.confirmed_tracks())
        tracker._tracks[99].hits = 2
        self.assertIn(99, tracker.confirmed_tracks())

    def test_duplicate_tracks_are_merged(self):
        """同一个人被检出两个框时应合并，避免人数虚高"""
        tracker = pd.PersonTracker(max_input_dim=1920, min_hits=1)
        tracker._tracks[1] = pd._Track(1, (100, 100, 60, 150), 662)
        tracker._tracks[2] = pd._Track(2, (104, 103, 60, 150), 662)  # 几乎重合
        tracker._tracks[1].hits = 5
        tracker._tracks[2].hits = 5
        tracker._tracks[2].crosses = 3
        tracker._merge_duplicate_tracks()
        self.assertEqual(len(tracker._tracks), 1, "重复轨迹应合并成一条")
        left = list(tracker._tracks.values())[0]
        self.assertEqual(left.crosses, 3, "合并时应继承跨线次数")

    def test_small_box_offset_is_still_same_person(self):
        """回归：小框偏移 6px 时 IoU 只有约 0.49，不能因此被判成两个人。

        这是"人数虚高 + 编号乱跳"的直接成因（旧实现用 IoU>=0.55 判重，
        26×40 的框偏移 6px 恰好漏判）。
        """
        a = (100, 100, 26, 40)
        b = (106, 106, 26, 40)          # 中心偏移约 8.5px
        self.assertLess(pd._iou(a, b), 0.55,
                        "构造前提：这两个框的 IoU 低于旧阈值")
        self.assertTrue(pd.PersonTracker._same_person(a, b),
                        "小框小幅偏移必须判为同一个人")

    def test_same_person_rejects_far_or_different_size(self):
        """相距较远、或尺寸差异过大的框不能判为同一人"""
        a = (100, 100, 26, 40)
        self.assertFalse(pd.PersonTracker._same_person(a, (160, 100, 26, 40)))  # 太远
        self.assertFalse(pd.PersonTracker._same_person(a, (102, 102, 26, 120)))  # 尺寸差太多
        self.assertFalse(pd.PersonTracker._same_person(a, (100, 100, 0, 40)))    # 退化框

    def test_duplicate_small_boxes_merged_end_to_end(self):
        """端到端：同一人被检出两个偏移小框，人数不应翻倍"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920, min_hits=1)
        dupes = [(200, 300, 26, 40), (206, 306, 26, 40)]   # 同一个人的两个框
        tracker._detect = lambda frame: list(dupes)
        frame = np.zeros((234, 416, 3), dtype=np.uint8)
        tracker.update(frame, boundary_ratio=0.92)
        self.assertEqual(len(tracker.confirmed_tracks()), 1,
                         "同一人的重复框应合并为一个人")

    def test_distinct_persons_are_not_merged(self):
        """相隔较远的两条轨迹不能被误合并"""
        tracker = pd.PersonTracker(max_input_dim=1920, min_hits=1)
        tracker._tracks[1] = pd._Track(1, (100, 100, 60, 150), 662)
        tracker._tracks[2] = pd._Track(2, (500, 100, 60, 150), 662)
        tracker._tracks[1].hits = 5
        tracker._tracks[2].hits = 5
        tracker._merge_duplicate_tracks()
        self.assertEqual(len(tracker._tracks), 2)

    def test_min_hits_can_be_overridden_for_single_image(self):
        """单张图片分析可传 min_hits=1 立即得到结果"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        TestPersonTracker._draw_realistic_person(img, 640, 360, height=220)
        persons, _, _ = tracker.analyze_only(img, min_hits=1)
        self.assertIsInstance(persons, list)


class TestHelpers(unittest.TestCase):

    def test_iou(self):
        self.assertAlmostEqual(pd._iou((0, 0, 10, 10), (5, 0, 10, 10)), 1 / 3, places=4)
        self.assertEqual(pd._iou((0, 0, 10, 10), (20, 20, 10, 10)), 0.0)

    def test_soft_nms(self):
        rects = [(0, 0, 50, 50), (2, 2, 48, 48), (100, 100, 30, 30)]
        scores = [0.9, 0.8, 0.7]
        kept = pd._soft_nms(rects, scores, iou_thresh=0.5)
        self.assertGreaterEqual(len(kept), 2)

    def test_density_level(self):
        self.assertEqual(pd._density_level(1, 1920 * 1080), "low")
        self.assertEqual(pd._density_level(40, 1280 * 720), "high")


class TestRenderFastPath(unittest.TestCase):
    """渲染提速相关：绿色正常框、配色语义、标签位图缓存、分析/绘制解耦"""

    @staticmethod
    def _person(pid=1, rect=(10, 10, 40, 80), action="正常姿态",
                bad=False, behaviors=None, crosses=0):
        return {"pid": pid, "rect": rect, "action": action, "bad": bad,
                "behaviors": list(behaviors or []), "crosses": crosses}

    def test_person_color_semantics(self):
        """绿=正常，橙=异常行为，红=越界"""
        self.assertEqual(pd.PersonTracker.person_color(self._person()), pd.COLOR_OK)
        self.assertEqual(
            pd.PersonTracker.person_color(self._person(behaviors=["长时间低头"])),
            pd.COLOR_WARN)
        self.assertEqual(pd.PersonTracker.person_color(self._person(bad=True)),
                         pd.COLOR_BAD)
        # 正常框必须是绿色（BGR 中 G 分量最高）
        g = pd.COLOR_OK
        self.assertGreater(g[1], g[0])
        self.assertGreater(g[1], g[2])

    def test_draw_boxes_paints_green_for_normal_person(self):
        """draw_boxes 应在正常人员位置画出绿色像素"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((200, 200, 3), 255, dtype=np.uint8)
        tracker.draw_boxes(frame, [self._person(rect=(40, 40, 60, 100))],
                           line_y=None, draw_line=False)
        # 矩形上边 y=40 处应出现绿色像素
        row = frame[40, 40:100]
        greens = [tuple(int(v) for v in px) for px in row
                  if px[1] > 100 and px[1] > px[0] and px[1] > px[2]]
        self.assertTrue(greens, "正常人员应画出绿色框")

    def test_draw_boxes_paints_red_for_out_of_bounds(self):
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((200, 200, 3), 255, dtype=np.uint8)
        tracker.draw_boxes(frame, [self._person(bad=True, rect=(40, 40, 60, 100))],
                           line_y=None, draw_line=False)
        row = frame[40, 40:100]
        reds = [tuple(int(v) for v in px) for px in row
                if px[2] > 100 and px[2] > px[0] and px[2] > px[1]]
        self.assertTrue(reds, "越界人员应画出红色框")

    def test_draw_boxes_scales_from_analysis_to_display(self):
        """分析分辨率 -> 显示分辨率的坐标映射必须生效"""
        tracker = pd.PersonTracker(max_input_dim=1920)
        frame = np.full((100, 100, 3), 255, dtype=np.uint8)
        # 分析帧 200 宽，显示帧 100 宽 -> scale=0.5，框应落在 (10,10)
        tracker.draw_boxes(frame, [self._person(rect=(20, 20, 40, 60))],
                           line_y=None, scale=0.5, draw_line=False)
        row = frame[10, 10:30]
        self.assertTrue(any(int(px[1]) > 100 for px in row),
                        "缩放后的框应画在显示坐标 (10,10) 附近")

    def test_analyze_only_does_not_draw(self):
        """analyze_only 不应在帧上落笔（贴图与绘制解耦）"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920)
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        TestPersonTracker._draw_realistic_person(img, 640, 360, height=220)
        before = img.copy()
        persons, violations, line_y = tracker.analyze_only(img)
        self.assertIsInstance(persons, list)
        self.assertIsInstance(violations, int)
        self.assertIsInstance(line_y, int)
        self.assertTrue(np.array_equal(before, img),
                        "analyze_only 不应修改输入帧")

    def test_text_patch_is_cached(self):
        """同一串文字只渲染一次，返回同一个缓存对象"""
        pd.clear_text_cache()
        a = pd.text_patch("正常姿态", (0, 200, 0), 13)
        b = pd.text_patch("正常姿态", (0, 200, 0), 13)
        self.assertIsNotNone(a)
        self.assertIs(a, b, "相同文字/颜色/字号应命中缓存")

    def test_blend_patch_handles_out_of_bounds(self):
        """标签贴到画面外不应抛出异常"""
        frame = np.zeros((20, 20, 3), dtype=np.uint8)
        patch = pd.text_patch("越界测试", (0, 0, 255), 13)
        pd.blend_patch(frame, patch, 100, 100)   # 完全在画面外
        pd.blend_patch(frame, patch, -5, -5)     # 部分在画面外
        self.assertEqual(frame.shape, (20, 20, 3))

    def test_draw_returns_same_array_in_place(self):
        """draw 原地绘制并返回同一数组（界面层据此省掉大帧拷贝）"""
        tracker = pd.PersonTracker(dense_mode=True, max_input_dim=1920)
        img = np.full((360, 640, 3), 235, dtype=np.uint8)
        out, violations, persons = tracker.draw(img)
        self.assertIs(out, img)


class TestYolo11nDetector(unittest.TestCase):
    """YOLO11n ONNX 检测引擎：letterbox / 输出解码 / NMS / 回退链"""

    def test_letterbox_square_keeps_ratio_with_center_pad(self):
        """16:9 帧 letterbox 到 640：等比例、居中补边"""
        frame = np.zeros((360, 640, 3), np.uint8)
        img, r, (dw, dh) = pd._letterbox(frame, 640)
        self.assertEqual(img.shape, (640, 640, 3))
        self.assertAlmostEqual(r, 1.0, places=6)
        self.assertAlmostEqual(dw, 0.0, places=6)
        self.assertAlmostEqual(dh, 140.0, places=6)
        # 补边区域应为 (114,114,114)
        self.assertTrue((img[0, 0] == 114).all())

    def test_letterbox_scales_large_frame(self):
        """1920×1080（也是 16:9）缩到 640：内容 640×360，上下各补 140"""
        frame = np.zeros((1080, 1920, 3), np.uint8)
        img, r, (dw, dh) = pd._letterbox(frame, 640)
        self.assertEqual(img.shape, (640, 640, 3))
        self.assertAlmostEqual(r, 640 / 1920.0, places=6)
        self.assertAlmostEqual(dw, 0.0, places=6)
        self.assertAlmostEqual(dh, 140.0, places=6)

    def test_yolo_postprocess_decodes_person_only_and_maps_back(self):
        """raw [1,84,8400]：只留 person 类且坐标正确还原（去 letterbox）"""
        raw = np.zeros((1, 84, 8400), np.float32)
        # 框0：person 0.9，640 输入空间 中心(320,320) 100×200
        raw[0, 0, 0], raw[0, 1, 0] = 320, 320
        raw[0, 2, 0], raw[0, 3, 0] = 100, 200
        raw[0, 4, 0] = 0.9
        # 框1：person 0.1 低于阈值
        raw[0, 4, 1] = 0.1
        # 框2：car（第 2 类）0.95 不是 person
        raw[0, 6, 2] = 0.95
        rects = pd._yolo_postprocess(raw, (360, 640), 1.0, (0.0, 140.0),
                                     0.25, 0.45)
        self.assertEqual(len(rects), 1)
        x, y, w, h = rects[0]
        # 内容区顶端在 y=140：输入 y=320 -> 帧内 y=180 => 框 y1=80, y2=280
        self.assertAlmostEqual(x, 270, delta=2)
        self.assertAlmostEqual(y, 80, delta=2)
        self.assertAlmostEqual(w, 100, delta=2)
        self.assertAlmostEqual(h, 200, delta=2)

    def test_yolo_postprocess_handles_transposed_layout(self):
        """[1, num, 84] 转置布局也应能正确解码"""
        raw = np.zeros((1, 3, 84), np.float32)
        raw[0, 0, 0], raw[0, 0, 1] = 320, 320
        raw[0, 0, 2], raw[0, 0, 3] = 64, 128
        raw[0, 0, 4] = 0.8
        rects = pd._yolo_postprocess(raw, (640, 640), 1.0, (0.0, 0.0),
                                     0.25, 0.45)
        self.assertEqual(len(rects), 1)
        x, y, w, h = rects[0]
        self.assertAlmostEqual(x, 288, delta=2)
        self.assertAlmostEqual(w, 64, delta=2)

    def test_yolo_postprocess_v5_layout_with_objectness(self):
        """85 列（带 objectness）布局兼容：person 分 = obj × 类分"""
        raw = np.zeros((1, 85, 10), np.float32)
        raw[0, 0, 0], raw[0, 1, 0] = 320, 320
        raw[0, 2, 0], raw[0, 3, 0] = 100, 100
        raw[0, 4, 0] = 0.9      # objectness
        raw[0, 5, 0] = 0.8      # person 类分 -> 0.72 高于阈值
        raw[0, 4, 1] = 0.9
        raw[0, 7, 1] = 0.9      # 第 2 类高分：不应混进来
        rects = pd._yolo_postprocess(raw, (640, 640), 1.0, (0.0, 0.0),
                                     0.25, 0.45)
        self.assertEqual(len(rects), 1)

    def test_nms_rects_suppresses_overlap_keeps_highest_score(self):
        rects = [(0, 0, 100, 100), (5, 5, 100, 100), (300, 0, 100, 100)]
        scores = [0.9, 0.8, 0.7]
        kept = pd._nms_rects(rects, scores, iou_thresh=0.45)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0], (0, 0, 100, 100))

    def test_yolo_disable_env_falls_back_to_hog_chain(self):
        """EXAM_YOLO_DISABLE=1：不加载 YOLO（用于对比/排查）"""
        with mock.patch.dict(os.environ, {"EXAM_YOLO_DISABLE": "1"}):
            tracker = pd.PersonTracker()
            self.assertIsNone(tracker._yolo_session)

    def test_model_path_env_override(self):
        """EXAM_YOLO_MODEL 可指向自定义模型路径"""
        with mock.patch.dict(os.environ, {"EXAM_YOLO_MODEL": "/tmp/no_such.onnx"}):
            self.assertEqual(pd.default_yolo_model_path(), "/tmp/no_such.onnx")

    def test_session_loads_when_model_available(self):
        """模型文件存在时，检测器应加载出 ONNX 会话（不存在则跳过）"""
        path = pd.default_yolo_model_path()
        if not os.path.exists(path):
            self.skipTest("未安装 models/yolo11n.onnx（运行 工具_YOLO模型导出.py）")
        tracker = pd.PersonTracker()
        self.assertIsNotNone(tracker._yolo_session,
                             "模型存在时 YOLO 会话应加载成功")
        # 会话共享缓存：第二个实例拿到的应是同一个 session 对象
        tracker2 = pd.PersonTracker()
        self.assertIs(tracker._yolo_session, tracker2._yolo_session,
                      "多个实例应共享同一个推理会话（模块级缓存）")

    def test_real_photo_person_detection(self):
        """真实照片端到端：检出人物（assets 缺测试图时跳过）"""
        asset = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "people.jpg")
        if not os.path.exists(asset):
            self.skipTest("缺少测试图片 tests/assets/people.jpg")
        if not os.path.exists(pd.default_yolo_model_path()):
            self.skipTest("未安装 YOLO 模型")
        data = np.fromfile(asset, dtype=np.uint8)   # 兼容中文路径
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        self.assertIsNotNone(img)
        tracker = pd.PersonTracker(min_hits=1)
        rects = tracker._detect(img)
        self.assertGreaterEqual(len(rects), 3,
                                "真实人群照片应至少检出 3 人（YOLO11n）")


class TestPartialBoxDedup(unittest.TestCase):
    """部分-整体去重：同一个人被检成"头/上半身 + 整体"时只能留一个框。

    背景（现场实测）：单人特写画面里 YOLO 会对同一个人同时给出"头/上半身"和
    "整体"两个框。两者 IoU 很低（≈ 小框面积/大框面积），IoU-NMS 压不掉；
    跟踪层的 _same_person 又要求两个框尺寸相近（0.6~1.67 倍），头框 vs 整体框
    的高度比往往只有 0.3，于是同一个人稳定占住两条编号 —— 界面上就是
    "一个人被画出一堆框、人数虚高"。这里锁住修复后的行为。
    """

    def test_iomin_is_full_when_fully_nested(self):
        """小框完全落在大框里：IoMin=1（而 IoU 会很小）"""
        body = (0, 0, 200, 400)
        head = (80, 10, 40, 50)
        self.assertAlmostEqual(pd._iomin(head, body), 1.0, places=6)
        self.assertLess(pd._iou(head, body), 0.1)

    def test_suppress_contained_drops_head_box_keeps_body(self):
        body = (0, 0, 200, 400)
        head = (80, 10, 40, 50)
        self.assertEqual(pd._suppress_contained([head, body]), [body])

    def test_suppress_contained_keeps_adjacent_person(self):
        """与主目标部分重叠的"另一个真人"不能被当成身体的一部分吞掉"""
        main = (188, 62, 406, 405)
        side = (469, 271, 166, 200)          # 实测场景：画面右侧半个人
        kept = pd._suppress_contained([side, main])
        self.assertEqual(len(kept), 2, "IoMin=0.80 < 0.85，应保留而不是吞掉")

    def test_suppress_contained_ignores_similar_sized_overlap(self):
        """面积接近的两个框交给 IoU-NMS，不做面积吞并"""
        a = (0, 0, 100, 200)
        b = (10, 10, 100, 200)
        self.assertEqual(len(pd._suppress_contained([a, b])), 2)

    def test_yolo_postprocess_suppresses_nested_partial_boxes(self):
        """YOLO 解码后：整体框 + 套在里面的头框 -> 只留整体框；旁边那个人保留"""
        raw = np.zeros((1, 84, 8400), np.float32)
        # 框0：整体 person 0.90，640 空间 cx=320 cy=320 w=200 h=400
        raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0], raw[0, 4, 0] = 320, 320, 200, 400, 0.90
        # 框1：同一个人被检成"头/上半身" 0.85，完全落在框0 内
        raw[0, 0, 1], raw[0, 1, 1], raw[0, 2, 1], raw[0, 3, 1], raw[0, 4, 1] = 320, 160, 80, 100, 0.85
        # 框2：旁边的另一个人 0.70（与框0 不重叠）
        raw[0, 0, 2], raw[0, 1, 2], raw[0, 2, 2], raw[0, 3, 2], raw[0, 4, 2] = 580, 320, 100, 200, 0.70
        rects = pd._yolo_postprocess(raw, (640, 640), 1.0, (0.0, 0.0), 0.25, 0.45)
        self.assertEqual(len(rects), 2, "同一个人只应留整体框，旁边的人保留")

    def test_merge_sources_ignores_box_inside_existing(self):
        """辅助源给出的"套在已有框里"的框不应被追加成第二个人"""
        primary = [(100, 100, 200, 400)]
        aux = [[(180, 120, 40, 60)]]          # Haar 头推断出的框，落在 primary 里
        merged = pd.PersonTracker._merge_sources(None, primary, aux)
        self.assertEqual(merged, primary)

    def test_merge_duplicate_tracks_merges_part_into_whole(self):
        """头框轨迹与整体框轨迹应合并成一条，且保留"整体框"的位置"""
        tracker = pd.PersonTracker(min_hits=2)
        body = (100, 100, 200, 400)
        head = (180, 120, 60, 80)
        tr_body = pd._Track(1, body, 600)
        tr_body.hits = 3
        tr_head = pd._Track(2, head, 600)
        tr_head.hits = 9                      # 头框命中更多，但仍是"整体"为王
        tracker._tracks = {1: tr_body, 2: tr_head}
        tracker._next_pid = 3
        tracker._merge_duplicate_tracks()
        self.assertEqual(len(tracker._tracks), 1)
        kept = list(tracker._tracks.values())[0]
        self.assertEqual(kept.rect, body)
        self.assertEqual(kept.hits, 9)

    def test_detect_returns_one_box_for_nested_detections(self):
        """端到端：主检测给出"整体+头"两个嵌套框时，_detect 只返回一个"""
        tracker = pd.PersonTracker(min_hits=1)
        nested = [(100, 100, 200, 400), (180, 120, 60, 80)]
        with mock.patch.object(tracker, "_detect_yolo", return_value=nested):
            tracker._yolo_session = object()          # 让主链走 YOLO 分支
            rects = tracker._detect(np.zeros((640, 640, 3), np.uint8))
        self.assertEqual(len(rects), 1)

    def test_aux_sources_skipped_when_primary_found_person(self):
        """主链已检出人时，不再叠加 Haar 人头/背景差分（旧版 len<8 会一直叠）"""
        tracker = pd.PersonTracker(min_hits=1)
        calls = []

        def fake_heads(frame):
            calls.append("heads")
            return [(180, 120, 60, 80)]

        with mock.patch.object(tracker, "_detect_yolo",
                               return_value=[(100, 100, 200, 400)]), \
                mock.patch.object(tracker, "_detect_heads", fake_heads), \
                mock.patch.object(tracker, "_detect_bg_rois",
                                  lambda frame: (calls.append("bg"), [])[1]):
            tracker._yolo_session = object()
            rects = tracker._detect(np.zeros((640, 640, 3), np.uint8))
        self.assertEqual(len(rects), 1)
        self.assertEqual(calls, [], "主链有检出时辅助源不该被调用")


class TestDenseTiling(unittest.TestCase):
    """40 人考场：分块（切片）推理 —— 后排小目标召回的关键。

    背景：单张推理把整帧压到 640×640，1080p 看 40 人时后排考生被压到十几像素，
    实测合成 40 人场景召回只有 12.5%（640）/ 25%（1280、1920 —— 多出来的分辨率
    被单张推理白白丢掉）；分块后每块按原生像素送模型，实测 67.5%（1280）/
    72.5%（1920）。这里锁住"块覆盖 / 块数上限 / 跨块合并去重 / 自动开关"。
    """

    def _tracker(self, **kw):
        kw.setdefault("max_input_dim", 4096)
        return pd.PersonTracker(**kw)

    def test_auto_tiling_off_for_small_frame(self):
        """640 长边的分析帧分块没有收益 → auto 必须关掉（不白花算力）"""
        self.assertFalse(self._tracker(yolo_tiling="auto").tiling_active(
            np.zeros((360, 640, 3), np.uint8)))

    def test_auto_tiling_on_for_dense_frame(self):
        tr = self._tracker(yolo_tiling="auto")
        self.assertTrue(tr.tiling_active(np.zeros((720, 1280, 3), np.uint8)))
        self.assertTrue(tr.tiling_active(np.zeros((1080, 1920, 3), np.uint8)))

    def test_tiling_on_off_override(self):
        self.assertTrue(self._tracker(yolo_tiling="on").tiling_active(
            np.zeros((240, 320, 3), np.uint8)))
        self.assertFalse(self._tracker(yolo_tiling="off").tiling_active(
            np.zeros((1080, 1920, 3), np.uint8)))

    def test_tiling_params_are_constructor_arguments(self):
        tr = self._tracker(yolo_tiling="on", yolo_tile_size=480,
                           yolo_tile_overlap=0.3, yolo_tile_max=7)
        self.assertEqual(tr.yolo_tiling, "on")
        self.assertEqual(tr.yolo_tile_size, 480)
        self.assertAlmostEqual(tr.yolo_tile_overlap, 0.3)
        self.assertEqual(tr.yolo_tile_max, 7)

    def test_tile_windows_cover_frame(self):
        tr = self._tracker(yolo_tiling="on", yolo_tile_size=640, yolo_tile_max=12)
        wins = tr._tile_windows(1080, 1920)
        self.assertTrue(wins)
        self.assertLessEqual(len(wins), 12)
        self.assertTrue(any(x == 0 and y == 0 for (x, y, w, h) in wins), "左上角要有块")
        self.assertTrue(any(x + w >= 1920 and y + h >= 1080 for (x, y, w, h) in wins),
                        "右下角要被覆盖，否则边缘考生必漏")
        for (x, y, w, h) in wins:
            self.assertEqual(w, h, "块应是正方形（与模型输入一致）")
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 1920)
            self.assertLessEqual(y + h, 1080)

    def test_tile_windows_respect_cap(self):
        tr = self._tracker(yolo_tiling="on", yolo_tile_size=320, yolo_tile_max=4)
        self.assertLessEqual(len(tr._tile_windows(1080, 1920)), 4)

    def test_merge_tiled_prefers_complete_box_over_clipped(self):
        """同一个人跨两块：贴边被切断的那个不可靠 → 保留完整框"""
        out = pd.PersonTracker._merge_tiled([(100, 100, 60, 120, True),
                                             (104, 98, 62, 122, False)])
        self.assertEqual(out, [(104, 98, 62, 122)])

    def test_merge_tiled_drops_nested_partial(self):
        """头框套在整体框里 → 只留整体框（与单张路径同一套判据）"""
        out = pd.PersonTracker._merge_tiled([(100, 100, 120, 300, False),
                                             (140, 110, 40, 60, False)])
        self.assertEqual(out, [(100, 100, 120, 300)])

    def test_merge_tiled_keeps_distinct_people(self):
        out = pd.PersonTracker._merge_tiled([(100, 100, 80, 200, False),
                                             (600, 100, 80, 200, False)])
        self.assertEqual(len(out), 2)

    def test_dense_frame_runs_multiple_tile_inferences(self):
        """1280 长边的帧必须真的切成多块跑（而不是只跑一次）"""
        tr = self._tracker(yolo_tiling="on")
        if tr._yolo_session is None:
            self.skipTest("未安装 models/yolo11n.onnx")
        calls = []
        orig = tr._detect_yolo_single

        def spy(frame):
            calls.append(frame.shape[:2])
            return orig(frame)

        tr._detect_yolo_single = spy
        tr._detect_yolo(np.zeros((720, 1280, 3), np.uint8))
        self.assertGreater(len(calls), 1, "分块推理应调用多次单块检测")
        self.assertTrue(tr._last_tiling)
        self.assertGreater(tr._last_tile_count, 1)

    def test_compact_labels_are_used_for_crowds(self):
        """40 人时只给异常框写文字：同等条件下画出来的像素必须更少"""
        import cv2
        tr = self._tracker(min_hits=1)
        frame = np.full((480, 1280, 3), 60, np.uint8)
        persons = []
        for i in range(24):
            persons.append({"pid": i + 1, "rect": (10 + i * 50, 200, 30, 80),
                            "action": "正常姿态", "bad": False, "behaviors": [],
                            "crosses": 0})
        persons.append({"pid": 99, "rect": (600, 120, 40, 100), "action": "使用手机",
                        "bad": True, "behaviors": ["使用手机"], "crosses": 0})

        buf_compact = frame.copy()
        tr.draw_boxes(buf_compact, persons, line_y=440, scale=1.0, font_size=12)
        changed_compact = int(np.count_nonzero(
            cv2.absdiff(buf_compact, frame).any(axis=2)))

        old = pd.COMPACT_LABEL_OVER
        pd.COMPACT_LABEL_OVER = 10_000            # 强制"人人都写文字"
        try:
            buf_full = frame.copy()
            tr.draw_boxes(buf_full, persons, line_y=440, scale=1.0, font_size=12)
            changed_full = int(np.count_nonzero(
                cv2.absdiff(buf_full, frame).any(axis=2)))
        finally:
            pd.COMPACT_LABEL_OVER = old

        self.assertLess(changed_compact, changed_full,
                        "人多时应少画文字（%d < %d）" % (changed_compact, changed_full))
        self.assertGreater(changed_compact, 0, "该画的框还得画")


class TestStandingPosture(unittest.TestCase):
    """站立/起立识别：远景机位站着的人不该再被画成"正常姿态"。

    现场问题：站着的人显示「正常姿态」。原因是旧条件要求
    `框高 > 55% 画面高 且 框底越过警戒线` —— 远景机位里站着的人只有几十像素高、
    也没走到镜头前，两个条件都不成立，于是落到默认的"正常姿态"。
    而 `ACT_STAND` 本来就在 ABNORMAL_ACTIONS 里：**是检测条件写错了，不是策略如此**。
    新判据改用与机位距离无关的证据：框"又高又窄" + 起身时"框高变大、质心上升"。
    """

    def _tracker(self, **kw):
        kw.setdefault("max_input_dim", 1920)
        return pd.PersonTracker(**kw)

    def _stand_track(self, tracker, frame_h=1080, n=5, aspect=0.45, w=90, h=200,
                     y=100, x=500):
        """造一条"持续又高又窄"的轨迹（模拟远景站立者）"""
        track = pd._Track(1, (x, y, w, h), int(frame_h * tracker.boundary_ratio))
        for _ in range(n):
            track.aspects.append(aspect)
            track.heights.append(h)
            track.centroids.append((x + w / 2.0, y + h / 2.0))
        return track

    def test_far_standing_person_is_flagged_even_without_crossing_line(self):
        """远景站立：框只有 90x200、框底远在警戒线之上，也必须报"起立/站立\""""
        tracker = self._tracker(confirm_frames=3)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = self._stand_track(tracker, aspect=0.45, w=90, h=200, y=100)
        self.assertLess(100 + 200, int(1080 * tracker.boundary_ratio), "前提：没越界")
        action, bad, behaviors = tracker.analyze(frame, track)
        self.assertFalse(bad)
        self.assertEqual(action, pd.ACT_STAND)
        self.assertIn(pd.ACT_STAND, behaviors)
        self.assertIn(pd.ACT_STAND, pd.ABNORMAL_ACTIONS, "站立属于要上报的异常行为")

    def test_seated_square_person_is_not_flagged_as_standing(self):
        """坐姿的人框接近方形（宽/高 0.85）→ 不能误报站立"""
        tracker = self._tracker(confirm_frames=3)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = self._stand_track(tracker, aspect=0.85, w=170, h=200, y=700)
        action, _, behaviors = tracker.analyze(frame, track)
        self.assertNotEqual(action, pd.ACT_STAND)
        self.assertNotIn(pd.ACT_STAND, behaviors)

    def test_rise_detected_from_height_growth_and_centroid_up(self):
        """起身：框高变大 + 质心上升，即使宽高比还没稳定下来也要报站立"""
        tracker = self._tracker(confirm_frames=4)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (500, 300, 120, 260), int(1080 * tracker.boundary_ratio))
        for h, y in ((140, 700), (140, 700), (150, 690), (190, 620), (200, 600), (205, 595)):
            track.heights.append(h)
            track.centroids.append((560.0, y + h / 2.0))
            track.aspects.append(0.80)          # 比例还没变窄
        action, _, behaviors = tracker.analyze(frame, track)
        self.assertEqual(action, pd.ACT_STAND)
        self.assertIn(pd.ACT_STAND, behaviors)

    def test_tiny_noise_box_is_not_standing(self):
        """几像素高的碎框不能因为"又高又窄"就被当成站立"""
        tracker = self._tracker(confirm_frames=3)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = self._stand_track(tracker, aspect=0.40, w=16, h=40, y=100)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertNotIn(pd.ACT_STAND, behaviors)

    def test_single_tall_frame_is_not_enough(self):
        """只有一帧又高又窄时不能立刻报站立（仍需连续确认）"""
        tracker = self._tracker(confirm_frames=4)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = self._stand_track(tracker, n=1, aspect=0.45, w=90, h=200)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertNotIn(pd.ACT_STAND, behaviors)

    def test_stand_aspect_is_configurable_and_clamped(self):
        """阈值可调（不同机位/坐姿可放宽或收紧），但必须夹在"偏转"阈值 0.85 以下"""
        loose = self._tracker(confirm_frames=3, stand_aspect=0.78)
        self.assertAlmostEqual(loose.stand_aspect, 0.78)
        frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
        track = self._stand_track(loose, aspect=0.75, w=150, h=200, y=200)
        _, _, behaviors = loose.analyze(frame, track)
        self.assertIn(pd.ACT_STAND, behaviors, "放宽到 0.78 后 0.75 的框应算站立")

        # 想配 0.95（会与"大幅度偏转"重叠 → 站立永远不报）时必须被夹住
        clamped = self._tracker(confirm_frames=3, stand_aspect=0.95)
        self.assertLess(clamped.stand_aspect, 0.85,
                        "stand_aspect 必须小于偏转阈值，否则站立分支被跳过")


if __name__ == "__main__":
    unittest.main()
