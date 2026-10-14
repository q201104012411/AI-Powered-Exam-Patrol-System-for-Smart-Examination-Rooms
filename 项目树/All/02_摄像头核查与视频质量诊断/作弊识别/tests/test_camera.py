# -*- coding: utf-8 -*-
"""摄像头/视频接入层单元测试。

设计原则：**不依赖真实摄像头**。用打桩（monkeypatch）替换人体/人脸检测器，
从而确定性地验证几何映射与"不可得信号不参与判定"等逻辑；
真正需要 OpenCV 的用例在缺少 OpenCV 时自动跳过。

运行：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cheatdetect import CheatDetector, Config                # noqa: E402
from cheatdetect import videosource as vs                    # noqa: E402

try:
    from cheatdetect import perception as pc
    HAS_PC = True
except Exception:                                            # pragma: no cover
    pc = None
    HAS_PC = False

NEED_CV = unittest.skipUnless(vs.HAS_OPENCV, "需要 OpenCV")
NEED_PC = unittest.skipUnless(HAS_PC and pc.HAS_CV, "需要 OpenCV + perception")


def frame(h=480, w=640):
    import numpy as np
    return np.zeros((h, w, 3), dtype=np.uint8)


class _FakePerson:
    """打桩人体检测器：固定返回给定框（补上置信度，接口为 5 元组）。"""

    def __init__(self, rects):
        self.rects = list(rects)
        self.backend = "stub"
        self.last_error = ""

    def detect(self, _frame):
        return [(r[0], r[1], r[2], r[3], 0.9) for r in self.rects]


class _FakeFace:
    """打桩人脸检测器：固定返回给定人脸（或 None），接口与真实实现一致（5 元组）。"""

    def __init__(self, face, kind="frontal"):
        self.face = face
        self.kind = kind
        self.ready = True

    def detect(self, _gray, _roi=None):
        if self.face is None:
            return None
        x, y, w, h = self.face
        return (x, y, w, h, self.kind)


# ================================================================
class TestVideoSource(unittest.TestCase):
    """帧源构造与参数校验（不需要真实设备）。"""

    def test_make_source_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            vs.make_source("不存在的类型")

    def test_make_source_file_requires_target(self):
        with self.assertRaises(ValueError):
            vs.make_source(vs.KIND_FILE, "")

    def test_camera_source_kind_and_default_target(self):
        src = vs.make_source(vs.KIND_CAMERA, None)
        self.assertEqual(src.kind, vs.KIND_CAMERA)
        self.assertEqual(src.target, 0)

    def test_stream_url_detected_as_stream(self):
        src = vs.make_source(vs.KIND_STREAM, "rtsp://10.0.0.9:554/Streaming/Channels/101")
        self.assertEqual(src.kind, vs.KIND_STREAM)
        self.assertIn("rtsp", src.describe())

    def test_file_kind_detected(self):
        src = vs.make_source(vs.KIND_FILE, r"C:\videos\room1.mp4")
        self.assertEqual(src.kind, vs.KIND_FILE)

    @NEED_CV
    def test_list_cameras_returns_list(self):
        self.assertIsInstance(vs.list_cameras(3), list)

    @NEED_CV
    def test_open_bad_file_fails_gracefully(self):
        src = vs.make_source(vs.KIND_FILE, r"C:\definitely\not\here.mp4")
        self.assertFalse(src.open())
        src.release()

    @NEED_CV
    def test_read_before_open_is_safe(self):
        src = vs.make_source(vs.KIND_CAMERA, 0)
        ok, f = src.read()
        self.assertFalse(ok)
        self.assertIsNone(f)


class TestUnicodePathHandling(unittest.TestCase):
    """OpenCV 在中文路径下会失败，必须能自动绕开（本机用户名即为中文）。"""

    @NEED_CV
    def test_ascii_staging_dir_is_writable_or_none(self):
        d = vs.ascii_staging_dir()
        if d is not None:
            self.assertTrue(d.isascii(), d)
            self.assertTrue(os.access(d, os.W_OK))

    @NEED_CV
    def test_ensure_ascii_media_handles_missing_file(self):
        path, note = vs.ensure_ascii_media(r"C:\no\such\file.mp4")
        self.assertIsNone(path)
        self.assertTrue(note)

    @NEED_CV
    def test_imread_imwrite_roundtrip_on_ascii_path(self):
        import numpy as np
        img = np.zeros((32, 48, 3), dtype=np.uint8)
        img[:, :, 1] = 200
        tmp = os.path.join(_ROOT, "tests", "_tmp_ascii.png")
        try:
            self.assertTrue(vs.imwrite_any(tmp, img))
            back = vs.imread_any(tmp)
            self.assertIsNotNone(back)
            self.assertEqual(back.shape, img.shape)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# ================================================================
@NEED_PC
class TestPerceptionGeometry(unittest.TestCase):
    """感知层几何映射：用打桩检测器做确定性验证。"""

    def _perceiver(self, person_rects, face):
        p = pc.FramePerceiver(pc.PerceptionConfig(baseline_warmup=1))
        p.person = _FakePerson(person_rects)
        p.face = _FakeFace(face)
        return p

    def test_no_person_returns_none(self):
        p = self._perceiver([], None)
        feats, info = p.process(frame())
        self.assertIsNone(feats)
        self.assertFalse(info.ok)
        self.assertIn("未检出", info.reason)

    def test_centered_face_gives_small_yaw_and_on_paper(self):
        # 人体框中心 x=320；人脸也居中
        p = self._perceiver([(220, 100, 200, 380)], (290, 120, 60, 60))
        for i in range(6):
            feats, info = p.process(frame(), i, i / 10.0)
        self.assertTrue(info.ok)
        self.assertTrue(info.head_known)
        self.assertLess(abs(info.yaw_deg), 5.0)
        self.assertEqual(info.relation, "on_paper")
        self.assertEqual(feats.head.relation, "on_paper")

    def test_face_far_right_gives_toward_neighbor(self):
        # 人脸明显偏向人体框右侧 → 应判为朝向邻座
        p = self._perceiver([(220, 100, 200, 380)], (390, 120, 60, 60))
        for i in range(6):
            feats, info = p.process(frame(), i, i / 10.0)
        self.assertTrue(info.head_known)
        self.assertGreater(info.yaw_deg, 30.0)
        self.assertEqual(info.relation, "toward_neighbor")

    def test_face_far_left_gives_negative_yaw(self):
        p = self._perceiver([(220, 100, 200, 380)], (150, 120, 60, 60))
        for i in range(6):
            feats, info = p.process(frame(), i, i / 10.0)
        self.assertLess(info.yaw_deg, -30.0)
        self.assertIn(info.relation, ("toward_neighbor", "off_area"))

    def test_never_seen_face_is_not_a_head_turn(self):
        """人脸一直检不到（远处/逆光/背对）不得被当作"转头"。

        这是防系统性误报的关键：检测能力边界不等于观测到异常。
        """
        p = self._perceiver([(220, 100, 200, 380)], None)
        for i in range(25):
            feats, info = p.process(frame(), i, i / 10.0)
        self.assertFalse(info.head_known)
        self.assertEqual(info.yaw_deg, 0.0)
        self.assertEqual(info.relation, "unknown")
        self.assertEqual(feats.head.relation, "unknown")

    def test_sudden_face_loss_after_stable_is_a_turn(self):
        """人脸此前稳定可见、突然消失 → 这才算"转头离开"的上下文证据。"""
        p = self._perceiver([(220, 100, 200, 380)], (290, 120, 60, 60))
        for i in range(10):                      # 先稳定检出
            p.process(frame(), i, i / 10.0)
        p.face = _FakeFace(None)                 # 突然检不到
        _feats, info = p.process(frame(), 10, 1.0)
        self.assertTrue(info.head_known)
        self.assertGreater(abs(info.yaw_deg), 20.0)

    def test_unavailable_signals_are_neutral_not_faked(self):
        """摄像头路径不可得的信号必须保持中性，不得编造观测值。"""
        p = self._perceiver([(220, 100, 200, 380)], (290, 120, 60, 60))
        feats, info = p.process(frame(), 0, 0.0)
        self.assertIsNone(feats.hand.cross_distance)   # 跨考生距离不可得
        self.assertFalse(feats.hand.below_desk)
        self.assertEqual(feats.prohibited_objects, [])  # 物体检测不可得
        self.assertFalse(feats.expression_tense)
        self.assertEqual(pc.CAMERA_SIGNAL_AVAIL["hand_interaction"], "unavailable")
        self.assertEqual(pc.CAMERA_SIGNAL_AVAIL["prohibited_object"], "unavailable")

    def test_signal_availability_declaration_is_complete(self):
        from cheatdetect.scoring import SIGNAL_KEYS
        for k in SIGNAL_KEYS:
            self.assertIn(k, pc.CAMERA_SIGNAL_AVAIL)
            self.assertIn(pc.CAMERA_SIGNAL_AVAIL[k],
                          ("measured", "estimate", "unavailable"))

    def test_target_selection_prefers_largest(self):
        p = self._perceiver([(10, 10, 40, 60), (200, 100, 300, 380)], None)
        _f, info = p.process(frame(), 0, 0.0)
        self.assertEqual(info.rect, (200, 100, 300, 380))

    def test_target_rect_is_clamped_inside_frame(self):
        p = self._perceiver([(600, 440, 200, 200)], None)
        _f, info = p.process(frame(480, 640), 0, 0.0)
        x, y, w, h = info.rect
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 640)
        self.assertLessEqual(y + h, 480)


@NEED_PC
class TestPerceptionHelpers(unittest.TestCase):
    """NMS / letterbox 等基础函数。"""

    def test_nms_removes_heavy_overlap(self):
        import numpy as np
        rects = np.array([[0, 0, 100, 100], [2, 2, 100, 100], [300, 300, 50, 50]],
                         dtype=np.float32)
        scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
        keep = pc._nms(rects, scores, 0.45)
        self.assertIn(0, keep)
        self.assertNotIn(1, keep)
        self.assertIn(2, keep)

    def test_nms_handles_empty_input(self):
        import numpy as np
        self.assertEqual(pc._nms(np.zeros((0, 4), dtype=np.float32),
                                 np.zeros((0,), dtype=np.float32)), [])

    def test_letterbox_output_is_square_and_reports_padding(self):
        import numpy as np
        f = np.zeros((480, 640, 3), dtype=np.uint8)
        out, r, dw, dh = pc._letterbox(f, 640)
        self.assertEqual(out.shape, (640, 640, 3))
        self.assertAlmostEqual(r, 1.0, places=5)
        self.assertEqual(dw, 0)
        self.assertEqual(dh, 80)

    def test_iou_math(self):
        self.assertAlmostEqual(pc._iou(0, 0, 10, 10, 0, 0, 10, 10), 1.0)
        self.assertAlmostEqual(pc._iou(0, 0, 10, 10, 20, 20, 10, 10), 0.0)


@NEED_PC
class TestCameraToEngineIntegration(unittest.TestCase):
    """感知输出接入引擎：不可得信号不得参与判分。"""

    def test_unknown_relation_produces_no_head_signal(self):
        from cheatdetect import FrameFeatures, HandState, HeadPose, PostureState
        from cheatdetect.signals import SignalExtractor
        cfg = Config(fps=10.0)
        ex = SignalExtractor(cfg)
        f = FrameFeatures(head=HeadPose(yaw_deg=0.0, pitch_deg=0.0,
                                        relation="unknown"),
                          hand=HandState(), posture=PostureState())
        self.assertEqual(ex.head_deviation(f), 0.0)

    def test_camera_only_signals_can_still_trigger(self):
        """只有头部信号可用时，持续转头仍应能触发告警。"""
        from cheatdetect import FrameFeatures, HandState, HeadPose, PostureState
        cfg = Config(fps=10.0)
        det = CheatDetector(cfg)
        for i in range(40):
            f = FrameFeatures(
                frame_id=i, timestamp=i / 10.0,
                head=HeadPose(yaw_deg=55.0, pitch_deg=0.0,
                              relation="toward_neighbor"),
                hand=HandState(cross_distance=None),
                posture=PostureState())
            det.push(f, record=False)
        det.finish()
        self.assertGreater(det.summary()["alarm_events"], 0)

    def test_camera_missing_signals_cannot_trigger_alone(self):
        """手掌/物体信号不可得时，它们不应凭空产生分数。"""
        from cheatdetect import FrameFeatures, HandState, HeadPose, PostureState
        from cheatdetect.signals import SignalExtractor
        from cheatdetect.scoring import RiskScorer
        cfg = Config(fps=10.0)
        f = FrameFeatures(head=HeadPose(relation="on_paper"),
                          hand=HandState(cross_distance=None),
                          posture=PostureState())
        ex = SignalExtractor(cfg)
        s = ex.extract([f] * 20)
        self.assertEqual(s.intensities.hand_interaction, 0.0)
        self.assertEqual(s.intensities.prohibited_object, 0.0)
        sc = RiskScorer(cfg)
        self.assertLess(sc.instant_risk(s.intensities, s.persistence),
                        cfg.risk_threshold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
