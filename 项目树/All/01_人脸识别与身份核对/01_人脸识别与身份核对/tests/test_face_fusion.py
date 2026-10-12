# -*- coding: utf-8 -*-
"""
人脸识别优化：多帧特征融合 + 索引查找/检索性能 单元测试

不需要 cv2 / insightface：通过注入假特征提取器验证算法与流程。

运行：
    python -m unittest discover -s tests -t . -v
"""
import os
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                   # noqa: E402

import config                                          # noqa: E402
import accounts                                        # noqa: E402
from face_index import FaceIndex                       # noqa: E402
from models import RESULT_FAIL, RESULT_MANUAL, RESULT_PASS   # noqa: E402
from quality import QualityReport                      # noqa: E402
from verifier import FaceVerifier                      # noqa: E402

DEVICE = "GATE-FUSE-01"
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)

accounts.MAJOR_EXAM_FLAG = os.path.join(_TMP_ROOT, "major_exam.on")
accounts.STRICT_MODE_FLAG = os.path.join(_TMP_ROOT, "strict_mode.on")
for _flag in (accounts.MAJOR_EXAM_FLAG, accounts.STRICT_MODE_FLAG):
    try:
        if os.path.exists(_flag):
            os.remove(_flag)
    except OSError:
        pass


def tmpdir(tag):
    d = os.path.join(_TMP_ROOT, "%s_%s" % (tag, uuid.uuid4().hex[:8]))
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(d):
    shutil.rmtree(d, ignore_errors=True)


def unit(v):
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# 两个互相正交的身份方向（余弦 = 0）
VEC_A = unit(np.eye(1, 512, 0, dtype=np.float32).ravel())
VEC_B = unit(np.eye(1, 512, 1, dtype=np.float32).ravel())


def mix(ca, cb):
    """构造单位向量：与 A 余弦 = ca，与 B 余弦 = cb"""
    rest = max(0.0, 1.0 - ca * ca - cb * cb)
    v = ca * VEC_A + cb * VEC_B
    if rest > 0:
        e3 = np.zeros(512, dtype=np.float32)
        e3[2] = 1.0
        v = v + np.sqrt(rest) * e3
    return unit(v)


class _SeqExtractor(object):
    """按调用顺序依次返回预设的 (向量, info)，用于模拟"连拍多帧" """

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, img):
        i = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        emb, info = self.frames[i]
        if emb is None:
            raise RuntimeError("模拟提特征失败")
        return np.asarray(emb, dtype=np.float32), dict(info or {})


def good_quality(sharp=900.0, yaw=0.0):
    return QualityReport(brightness=140, contrast=50, sharpness=sharp,
                         face_ratio=0.12, yaw=yaw, pitch=0.0, roll=0.0,
                         severity="ok", issues=[])


# ==================== 索引性能与正确性 ====================

class TestIndexLookup(unittest.TestCase):

    def setUp(self):
        self.tmp = tmpdir("idxperf")
        self.path = os.path.join(self.tmp, "i.enc")

    def tearDown(self):
        cleanup(self.tmp)

    @staticmethod
    def _emb(img):
        return np.asarray(VEC_A, dtype=np.float32)

    def test_row_lookup_is_consistent_after_append_and_remove(self):
        """O(1) 行号映射在增删后必须保持一致"""
        idx = FaceIndex(embedder=self._emb, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        idx._append("C003", VEC_A)
        self.assertEqual(idx._row_of("C001"), 0)
        self.assertEqual(idx._row_of("C002"), 1)
        self.assertEqual(idx._row_of("C003"), 2)
        self.assertTrue(idx.has("C002"))
        self.assertFalse(idx.has("NOPE"))

        idx.remove("C001")
        # 删除后行号整体前移，_row_of 必须跟着更新
        self.assertIsNone(idx._row_of("C001"))
        self.assertEqual(idx._row_of("C002"), 0)
        self.assertEqual(idx._row_of("C003"), 1)

    def test_row_lookup_after_overwrite_append(self):
        """覆盖更新不应改变行号"""
        idx = FaceIndex(embedder=self._emb, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        idx._append("C001", VEC_B)              # 覆盖
        self.assertEqual(idx._row_of("C001"), 0)
        self.assertEqual(idx.size, 2)

    def test_twin_pairs_vectorized_matches_bruteforce(self):
        """向量化撞脸检索的结果必须与暴力双层循环一致"""
        idx = FaceIndex(embedder=self._emb, path=self.path, resolve_paths=False)
        vecs = {
            "C001": VEC_A,
            "C002": VEC_A,                                  # 与 C001 完全相同
            "C003": mix(0.87, 0.10),                        # 与 A 相似 0.87
            "C004": VEC_B,
        }
        for cid, v in vecs.items():
            idx._append(cid, v)

        # 暴力法
        expect = []
        ids = idx.ids
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                s = float(np.dot(vecs[ids[i]], vecs[ids[j]]))
                if s >= 0.80:
                    expect.append((ids[i], ids[j], round(s, 4)))
        expect.sort(key=lambda x: -x[2])

        got = idx.twin_pairs(threshold=0.80)
        self.assertEqual(len(got), len(expect))
        for g, e in zip(got, expect):
            self.assertEqual({g["a"], g["b"]}, {e[0], e[1]})
            self.assertAlmostEqual(g["score"], e[2], places=4)

    def test_twin_pairs_empty_and_single(self):
        idx = FaceIndex(embedder=self._emb, path=self.path, resolve_paths=False)
        self.assertEqual(idx.twin_pairs(), [])
        idx._append("C001", VEC_A)
        self.assertEqual(idx.twin_pairs(), [])


# ==================== 多帧融合 ====================

class TestFrameFusion(unittest.TestCase):

    def setUp(self):
        self.tmp = tmpdir("fuse")
        self.path = os.path.join(self.tmp, "i.enc")

    def tearDown(self):
        cleanup(self.tmp)

    def test_frame_weight_prefers_sharp_over_blurry(self):
        sharp = {"quality": good_quality(sharp=900.0), "det_score": 0.95}
        blurry = {"quality": QualityReport(brightness=140, sharpness=20.0,
                                           severity="warn", issues=["画面模糊"]),
                  "det_score": 0.6}
        self.assertGreater(FaceIndex.frame_weight(sharp),
                           FaceIndex.frame_weight(blurry))

    def test_frame_weight_penalises_severe_and_side_face(self):
        ok = {"quality": good_quality(), "det_score": 0.9}
        severe = {"quality": QualityReport(brightness=15, sharpness=8,
                                           severity="severe", issues=["严重欠曝"]),
                  "det_score": 0.9}
        side = {"quality": good_quality(yaw=config.POSE_MAX_YAW + 25), "det_score": 0.9}
        self.assertLess(FaceIndex.frame_weight(severe), FaceIndex.frame_weight(ok))
        self.assertLess(FaceIndex.frame_weight(side), FaceIndex.frame_weight(ok))

    def test_frame_weight_is_always_positive(self):
        """极端输入也不能得到 0/负权重（否则融合会退化）"""
        for info in ({}, {"quality": None}, {"det_score": 0.0},
                     {"quality": QualityReport(severity="severe"), "det_score": 0.01}):
            self.assertGreater(FaceIndex.frame_weight(info), 0.0)

    def test_fuse_single_vector_is_normalized(self):
        v = np.ones(512, dtype=np.float32)
        out = FaceIndex.fuse([v])
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=5)

    def test_fuse_of_identical_vectors_is_that_vector(self):
        out = FaceIndex.fuse([VEC_A, VEC_A, VEC_A])
        self.assertAlmostEqual(float(np.dot(out, VEC_A)), 1.0, places=5)

    def test_fuse_none_and_empty_returns_none(self):
        self.assertIsNone(FaceIndex.fuse([]))
        self.assertIsNone(FaceIndex.fuse([None, None]))

    def test_fuse_weights_shift_result(self):
        """高权重帧应当把融合结果拉向自己"""
        near_a = mix(0.90, 0.20)
        unweighted = FaceIndex.fuse([VEC_A, near_a])
        weighted = FaceIndex.fuse([VEC_A, near_a], weights=[1.0, 0.05])
        self.assertGreater(float(np.dot(weighted, VEC_A)),
                           float(np.dot(unweighted, VEC_A)))

    def test_score_multi_beats_each_single_noisy_frame(self):
        """关键收益：单帧都低于阈值，融合后应达标。

        模拟真实场景——同一人连拍 3 帧，每帧各带一点噪声。
        """
        thr = config.THRESHOLD_1TO1
        # 三帧各自与本人相似度略低于阈值，且噪声方向互相不同
        scores_each = [thr - 0.03, thr - 0.05, thr - 0.04]
        frames = []
        for i, s in enumerate(scores_each):
            noise = np.zeros(512, dtype=np.float32)
            noise[10 + i] = np.sqrt(max(0.0, 1.0 - s * s))   # 各自独立的噪声方向
            frames.append((unit(s * VEC_A + noise), {"quality": good_quality()}))
        ex = _SeqExtractor(frames)

        idx = FaceIndex(extractor=ex, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        fused_score, info = idx.score_multi("C001", ["f1", "f2", "f3"])

        self.assertEqual(info["fused_frames"], 3)
        self.assertGreaterEqual(fused_score, thr,
                                "多帧融合应把低于阈值的单帧拉回阈值以上")
        self.assertLess(max(info["single_scores"]), thr,
                        "构造前提：每一帧单独都低于阈值")

    def test_score_multi_keeps_other_person_far(self):
        """融合不能让"别人"变像——张冠李戴必须仍然被拒"""
        thr = config.THRESHOLD_1TO1
        frames = [(mix(0.20, 0.10), {"quality": good_quality()}) for _ in range(4)]
        ex = _SeqExtractor(frames)
        idx = FaceIndex(extractor=ex, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        score, info = idx.score_multi("C001", ["a", "b", "c", "d"])
        self.assertLess(score, thr)
        self.assertLess(info["best_other_score"], thr)

    def test_score_multi_missing_candidate_raises(self):
        from face_engine import FaceError
        ex = _SeqExtractor([(VEC_A, {})])
        idx = FaceIndex(extractor=ex, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        with self.assertRaises(FaceError):
            idx.score_multi("NOPE", ["a"])

    def test_score_multi_skips_bad_frames(self):
        """个别帧提特征失败不应中断整批"""
        frames = [(VEC_A, {"quality": good_quality()}),
                  (None, {}),                      # 模拟失败
                  (VEC_A, {"quality": good_quality()})]
        ex = _SeqExtractor(frames)
        idx = FaceIndex(extractor=ex, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        score, info = idx.score_multi("C001", ["a", "b", "c"])
        self.assertEqual(info["fused_frames"], 2)
        self.assertGreater(score, 0.99)


# ==================== 核验器集成：融合只放行、不拒绝 ====================

class TestVerifierFusion(unittest.TestCase):

    def setUp(self):
        self.tmp = tmpdir("vfuse")
        self.path = os.path.join(self.tmp, "i.enc")
        self.logs = []
        self.candidates = {}
        self.token = {"device_id": DEVICE, "source": "camera"}

    def tearDown(self):
        cleanup(self.tmp)

    def _logger(self):
        outer = self

        class _L(object):
            def log(self, rec):
                outer.logs.append(rec)
        return _L()

    def _make_candidate(self, cid):
        from models import Candidate
        c = Candidate(candidate_id=cid, name="测试", ticket_no="2026T001",
                      seat_no="A-01", id_card_salt="s",
                      id_card_hash="h", id_card_masked="1101**********1234",
                      consent_status="已同意")
        self.candidates[cid] = c
        return c

    def _verifier(self, frames):
        ex = _SeqExtractor(frames)
        idx = FaceIndex(extractor=ex, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        self._make_candidate("C001")
        self._make_candidate("C002")
        return FaceVerifier(self.candidates, idx, DEVICE, self._logger(),
                            operator="tester", enforce_consent=True,
                            require_liveness=True)

    def _token(self):
        import liveness
        return liveness.make_token(DEVICE, "camera")

    def test_multi_frame_fusion_passes_marginal_case(self):
        """连拍 3 帧各自略低于阈值，融合后应放行（这是本次优化的核心收益）"""
        thr = config.THRESHOLD_1TO1
        frames = []
        for i, s in enumerate((thr - 0.03, thr - 0.05, thr - 0.04)):
            noise = np.zeros(512, dtype=np.float32)
            noise[10 + i] = np.sqrt(max(0.0, 1.0 - s * s))
            frames.append((unit(s * VEC_A + noise), {"quality": good_quality(),
                                                     "det_score": 0.9}))
        v = self._verifier(frames)
        rec = v.verify_face_first("C001", live_images=["f1", "f2", "f3"],
                                  capture_token=self._token(),
                                  ticket_no="2026T001", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertIn("多帧融合", rec.reason)
        self.assertGreaterEqual(rec.score, thr)

    def test_fusion_never_turns_pass_into_fail(self):
        """融合不达标时应回落到逐帧流程，不能把本来能过的人判失败"""
        thr = config.THRESHOLD_1TO1
        # 第一组：全都不达标；第二组逐帧时能过（这里让每帧都达标来模拟回落）
        frames = [(mix(0.10, 0.05), {"quality": good_quality()}),
                  (VEC_A, {"quality": good_quality()})]
        v = self._verifier(frames)
        rec = v.verify_face_first("C001", live_images=["a", "b"],
                                  capture_token=self._token(),
                                  ticket_no="2026T001", seat_no="A-01")
        # 无论走融合还是逐帧，结论都应是 PASS（有可用帧）
        self.assertEqual(rec.result, RESULT_PASS)

    def test_single_image_behaviour_unchanged(self):
        """只给一帧时不做融合，流程与旧版一致"""
        v = self._verifier([(VEC_A, {"quality": good_quality()})])
        rec = v.verify_face_first("C001", live_images=["only"],
                                  capture_token=self._token(),
                                  ticket_no="2026T001", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertNotIn("多帧融合", rec.reason)

    def test_fusion_rejects_other_person(self):
        """融合绝不能把别人放进来"""
        frames = [(mix(0.20, 0.10), {"quality": good_quality()}) for _ in range(3)]
        v = self._verifier(frames)
        rec = v.verify_face_first("C001", live_images=["a", "b", "c"],
                                  capture_token=self._token(),
                                  ticket_no="2026T001", seat_no="A-01")
        self.assertNotEqual(rec.result, RESULT_PASS)

    def test_fusion_does_not_bypass_liveness(self):
        """没有现场令牌时，融合也不能放行（严格模式）"""
        thr = config.THRESHOLD_1TO1
        frames = [(VEC_A, {"quality": good_quality()}) for _ in range(3)]
        v = self._verifier(frames)
        rec = v.verify_face_first("C001", live_images=["a", "b", "c"],
                                  capture_token=None,
                                  ticket_no="2026T001", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_FAIL)


if __name__ == "__main__":
    unittest.main()
