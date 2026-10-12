# -*- coding: utf-8 -*-
"""渲染性能相关单元测试（不需要真实窗口，只验证纯计算部分）"""
import unittest

import numpy as np

import gui


class TestFitFrame(unittest.TestCase):
    """_fit_frame 必须用 cv2 缩放，且不放大超出面板、不改变宽高比"""

    def test_downscales_1080p_to_panel(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        out = gui.App._fit_frame(frame, 320, 240)
        self.assertLessEqual(out.shape[1], gui.MAX_PANEL_W)
        self.assertLessEqual(out.shape[0], gui.MAX_PANEL_H)
        # 宽高比保持（16:9）
        self.assertAlmostEqual(out.shape[1] / out.shape[0], 1920 / 1080, delta=0.05)

    def test_does_not_upscale_small_frame(self):
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        out = gui.App._fit_frame(frame, 640, 480)
        self.assertIs(out, frame, "小于面板的帧不应被放大")

    def test_handles_degenerate_frame(self):
        frame = np.zeros((0, 0, 3), dtype=np.uint8)
        out = gui.App._fit_frame(frame, 320, 240)
        self.assertEqual(out.shape[0], 0)


class TestRenderInterval(unittest.TestCase):
    """渲染间隔必须随播放路数自适应下调"""

    @staticmethod
    def _app_with(n):
        app = gui.App.__new__(gui.App)          # 不跑 __init__，只测纯逻辑
        app._cam_running = {("u%d" % i): True for i in range(n)}
        return app

    def test_fewer_cameras_render_faster(self):
        few = self._app_with(1)._render_interval()
        mid = self._app_with(4)._render_interval()
        many = self._app_with(8)._render_interval()
        huge = self._app_with(20)._render_interval()
        self.assertLessEqual(few, mid)
        self.assertLessEqual(mid, many)
        self.assertLessEqual(many, huge)
        self.assertGreaterEqual(few, 30, "不应超过 30fps 上限")
        self.assertLessEqual(huge, 400)

    def test_no_cameras_uses_fastest_interval(self):
        self.assertEqual(self._app_with(0)._render_interval(), gui.RENDER_MS_FEW)


class TestAISettings(unittest.TestCase):
    """AI 分析的间隔与输入分辨率必须随模式变化（密集模式更慢但更准）"""

    @staticmethod
    def _app(mode):
        app = gui.App.__new__(gui.App)
        app._ai_settings = {"mode": mode}
        return app

    def test_interval_per_mode(self):
        self.assertLess(self._app("standard")._cam_ai_interval_ms(),
                        self._app("energy")._cam_ai_interval_ms())

    def test_ai_input_dim_per_mode(self):
        self.assertGreaterEqual(self._app("dense")._cam_ai_input_dim(),
                                self._app("energy")._cam_ai_input_dim())

    def test_unknown_mode_falls_back_to_standard(self):
        app = self._app("nonsense")
        self.assertEqual(app._cam_ai_interval_ms(), gui.AI_INTERVAL_MS["standard"])
        self.assertEqual(app._cam_ai_input_dim(), gui.AI_INPUT_DIM["standard"])


class TestDensityLevel(unittest.TestCase):
    """_density_level 既接受帧也接受 (h, w)，便于用分析分辨率算密度"""

    def test_accepts_shape_tuple(self):
        self.assertEqual(gui.App._density_level([{}] * 40, (720, 1280, 3)),
                         gui.DENSITY_HIGH)

    def test_empty_persons_is_low(self):
        self.assertEqual(gui.App._density_level([], (1080, 1920, 3)),
                         gui.DENSITY_LOW)

    def test_none_is_low(self):
        self.assertEqual(gui.App._density_level([{}], None), gui.DENSITY_LOW)


if __name__ == "__main__":
    unittest.main()
