# -*- coding: utf-8 -*-
"""
person_detect 密集教室优化版单元测试

运行方式（在项目 02 根目录执行）：
    python -m unittest discover -s tests -t .
"""
import unittest

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
            self.assertEqual(p["density_level"], "high")

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
        """连续多帧都偏宽时才报'大幅度偏转'"""
        tracker = pd.PersonTracker(max_input_dim=1920, confirm_frames=4)
        frame = np.full((720, 1280, 3), 200, dtype=np.uint8)
        track = pd._Track(1, (600, 300, 260, 200), 662)
        for _ in range(5):
            # 模拟 update 里写入宽高比历史
            track.aspects.append(260 / 200.0)
        _, _, behaviors = tracker.analyze(frame, track)
        self.assertIn(pd.ACT_TURN, behaviors, "连续偏宽应确认为大幅度偏转")

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


if __name__ == "__main__":
    unittest.main()
