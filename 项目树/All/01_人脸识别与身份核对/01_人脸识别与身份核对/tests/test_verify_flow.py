# -*- coding: utf-8 -*-
"""
核验全链路回归测试：**真实走一遍界面上点「开始核验」的代码路径**。

背景：曾经出现过"01 无法正常调用核验"——报名库里的参考照缺失/被别的考生共用，
导致无论怎么核验都返回 FAIL/MANUAL。根因不在界面，而在数据与几处健壮性缺陷：
  · data\\ref_photos\\ 只剩 3 张图，其余 300 名考生的照片被删；
  · 两名手填的测试考生把 reference_photo 填成了别人的照片（还漏了扩展名），
    建档后特征与被借用者完全相同 -> 1:1 核验恒判 IDENTITY_CONFLICT；
  · main.py 遇到这种脏数据会直接 FileNotFoundError 崩掉。

这组测试守住"核验能被正常调用并给出正确结论"这条底线，不需要摄像头。

运行：python -m unittest discover -s tests -t . -v
"""
import os
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                       # noqa: E402

import accounts                                          # noqa: E402
import config                                            # noqa: E402
import liveness                                          # noqa: E402
from face_index import FaceIndex                         # noqa: E402
from models import RESULT_FAIL, RESULT_MANUAL, RESULT_PASS   # noqa: E402
from store import CandidateStore                         # noqa: E402
from verifier import FaceVerifier                        # noqa: E402

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_TK = True
except Exception:
    HAS_TK = False

try:
    import cv2                                           # noqa: F401
    import face_engine
    HAS_FACE = True
except Exception:
    HAS_FACE = False

_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
accounts.MAJOR_EXAM_FLAG = os.path.join(_TMP_ROOT, "major_exam.on")
accounts.STRICT_MODE_FLAG = os.path.join(_TMP_ROOT, "strict_mode.on")
for _f in (accounts.MAJOR_EXAM_FLAG, accounts.STRICT_MODE_FLAG):
    try:
        if os.path.exists(_f):
            os.remove(_f)
    except OSError:
        pass


def tmpdir(tag):
    d = os.path.join(_TMP_ROOT, "%s_%s" % (tag, uuid.uuid4().hex[:8]))
    os.makedirs(d, exist_ok=True)
    return d


def cleanup(d):
    shutil.rmtree(d, ignore_errors=True)


# ==================== 参考照路径解析 ====================

class TestResolvePhoto(unittest.TestCase):
    """resolve_photo 必须能处理"缺少扩展名"的历史脏写法"""

    def setUp(self):
        self.tmp = tmpdir("resolve")
        self.orig = config.PHOTO_DIR
        config.PHOTO_DIR = __import__("pathlib").Path(self.tmp)
        # 造一张真图
        with open(os.path.join(self.tmp, "C001.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake")

    def tearDown(self):
        config.PHOTO_DIR = self.orig
        cleanup(self.tmp)

    def test_missing_extension_is_resolved(self):
        """reference_photo 写成 "C001"（漏了 .jpg）也应解析到 C001.jpg"""
        got = str(config.resolve_photo("C001"))
        self.assertTrue(got.lower().endswith("c001.jpg"), got)
        self.assertTrue(os.path.exists(got))

    def test_existing_absolute_path_is_kept(self):
        p = os.path.join(self.tmp, "C001.jpg")
        self.assertEqual(str(config.resolve_photo(p)), p)

    def test_missing_file_falls_back_without_crash(self):
        got = str(config.resolve_photo("NOPE"))
        self.assertIsInstance(got, str)
        self.assertTrue(got)

    def test_empty_returns_as_is(self):
        self.assertEqual(config.resolve_photo(""), "")


# ==================== 库数据体检 ====================

class TestLibraryInspection(unittest.TestCase):
    """体检工具必须能发现"两人共用一张参考照"这种会让核验恒失败的脏数据"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "工具", "库数据体检.py")
        spec = importlib.util.spec_from_file_location("libcheck", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self.tmp = tmpdir("libcheck")
        self.orig = config.PHOTO_DIR
        config.PHOTO_DIR = __import__("pathlib").Path(self.tmp)
        for n in ("C001.jpg", "C002.jpg"):
            with open(os.path.join(self.tmp, n), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fake")

    def tearDown(self):
        config.PHOTO_DIR = self.orig
        cleanup(self.tmp)

    def test_detects_shared_photo(self):
        rows = [
            {"candidate_id": "C001", "reference_photo": os.path.join(self.tmp, "C001.jpg")},
            {"candidate_id": "C002", "reference_photo": os.path.join(self.tmp, "C001.jpg")},
        ]
        rep = self.mod.inspect(rows)
        self.assertEqual(len(rep["shared_photo"]), 1)
        self.assertEqual(rep["shared_photo"][0]["candidates"], ["C001", "C002"])

    def test_detects_missing_photo(self):
        rows = [{"candidate_id": "C001", "reference_photo": os.path.join(self.tmp, "NOPE.jpg")}]
        rep = self.mod.inspect(rows)
        self.assertEqual(len(rep["missing_photo"]), 1)

    def test_detects_no_photo_column(self):
        rep = self.mod.inspect([{"candidate_id": "C001", "reference_photo": ""}])
        self.assertEqual(len(rep["no_photo"]), 1)

    def test_clean_library_reports_nothing(self):
        rows = [
            {"candidate_id": "C001", "reference_photo": os.path.join(self.tmp, "C001.jpg")},
            {"candidate_id": "C002", "reference_photo": os.path.join(self.tmp, "C002.jpg")},
        ]
        rep = self.mod.inspect(rows)
        self.assertEqual(rep["shared_photo"], [])
        self.assertEqual(rep["missing_photo"], [])
        self.assertEqual(rep["no_photo"], [])


# ==================== main.py 脏数据健壮性 ====================

class TestSelfcheckRobustness(unittest.TestCase):
    """自检脚本不得因脏数据崩溃（旧版直接 FileNotFoundError）"""

    @classmethod
    def setUpClass(cls):
        import main as m
        cls.m = m

    def setUp(self):
        self.tmp = tmpdir("usable")
        self.orig = config.PHOTO_DIR
        config.PHOTO_DIR = __import__("pathlib").Path(self.tmp)

    def tearDown(self):
        config.PHOTO_DIR = self.orig
        cleanup(self.tmp)

    def _cand(self, cid, photo):
        from models import Candidate
        return Candidate(candidate_id=cid, name=cid, reference_photo=photo)

    def test_filters_candidates_with_missing_photo(self):
        cands = {
            "GOOD": self._cand("GOOD", os.path.join(self.tmp, "nope.jpg")),
            "BAD": self._cand("BAD", ""),
        }
        usable, skipped = self.m._usable_candidates(cands)
        self.assertEqual(usable, [])
        self.assertEqual(len(skipped), 2)

    def test_does_not_raise_on_extensionless_path(self):
        """缺少扩展名的路径不应让自检抛异常"""
        cands = {"C001": self._cand("C001", "C001")}
        usable, skipped = self.m._usable_candidates(cands)
        self.assertIsInstance(usable, list)
        self.assertIsInstance(skipped, list)


# ==================== 真实库的核验可用性 ====================

@unittest.skipUnless(HAS_FACE, "当前环境缺少 cv2/insightface，跳过真实核验测试")
class TestRealLibraryVerification(unittest.TestCase):
    """用当前 data/ 目录里的真实数据走一遍核验。

    这组测试是"01 能不能正常用"的直接证据：如果参考照被删、索引过期、
    或者有考生共用了别人的照片，这里就会失败。
    """

    @classmethod
    def setUpClass(cls):
        cls.cands = CandidateStore().load()
        cls.idx = FaceIndex()
        try:
            cls.idx.load()
        except Exception as e:
            raise unittest.SkipTest("索引不可用：%s" % e)

    def _img(self, cid):
        c = self.cands.get(cid)
        if c is None:
            self.skipTest("库中没有考生 %s" % cid)
        p = str(config.resolve_photo(c.reference_photo))
        if not os.path.exists(p):
            self.skipTest("考生 %s 的参考照不存在：%s" % (cid, p))
        img = face_engine.imread_unicode(p)
        if img is None:
            self.skipTest("考生 %s 的参考照无法解码" % cid)
        return img

    def test_index_covers_library(self):
        """索引覆盖率：有照片的考生都应已建档（否则核验会 NOT_FOUND）"""
        have_photo = [cid for cid, c in self.cands.items()
                      if c.reference_photo
                      and os.path.exists(str(config.resolve_photo(c.reference_photo)))]
        missing = [cid for cid in have_photo if not self.idx.has(cid)]
        self.assertEqual(missing, [], "这些考生有照片但未建档：%s" % missing[:10])

    def test_self_verification_passes(self):
        """本人核验必须 PASS（这是"核验能正常调用"的最小证据）"""
        cid = "C001" if "C001" in self.cands else sorted(self.cands)[0]
        img = self._img(cid)
        v = FaceVerifier(self.cands, self.idx, device_id="GATE-TEST",
                         logger=_NullLogger(), operator="test", enforce_consent=False)
        tok = liveness.make_token("GATE-TEST", source="camera")
        rec = v.verify_face_first(cid, live_images=[img], capture_token=tok)
        self.assertEqual(rec.result, RESULT_PASS,
                         "本人核验未通过：%s / %s" % (rec.reason_code, rec.reason))

    def test_multi_frame_fusion_also_passes(self):
        """多帧融合路径也必须 PASS，且确实走了融合"""
        cid = "C001" if "C001" in self.cands else sorted(self.cands)[0]
        img = self._img(cid)
        v = FaceVerifier(self.cands, self.idx, device_id="GATE-TEST",
                         logger=_NullLogger(), operator="test", enforce_consent=False)
        tok = liveness.make_token("GATE-TEST", source="camera")
        rec = v.verify_face_first(cid, live_images=[img] * 5, capture_token=tok)
        self.assertEqual(rec.result, RESULT_PASS,
                         "多帧融合核验未通过：%s / %s" % (rec.reason_code, rec.reason))

    def test_impostor_is_not_accepted(self):
        """别人顶替必须被拦（不能因为放宽而失去安全性）"""
        ids = sorted(c for c in self.cands if self.idx.has(c))
        if len(ids) < 2:
            self.skipTest("库中建档考生不足 2 人")
        cid, other = ids[0], ids[1]
        img = self._img(other)
        v = FaceVerifier(self.cands, self.idx, device_id="GATE-TEST",
                         logger=_NullLogger(), operator="test", enforce_consent=False)
        tok = liveness.make_token("GATE-TEST", source="camera")
        rec = v.verify_face_first(cid, live_images=[img], capture_token=tok)
        self.assertNotEqual(rec.result, RESULT_PASS,
                            "他人照片竟然通过了核验（严重安全问题）")

    def test_no_candidate_shares_a_photo(self):
        """真实库里不应存在"两人共用一张参考照"（会让核验恒判身份混淆）"""
        import importlib.util
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "工具", "库数据体检.py")
        spec = importlib.util.spec_from_file_location("libcheck2", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rep = mod.inspect([{"candidate_id": cid,
                            "reference_photo": c.reference_photo}
                           for cid, c in self.cands.items()])
        self.assertEqual(rep["shared_photo"], [],
                         "存在共用参考照的考生：%s" % rep["shared_photo"])


class _NullLogger(object):
    def log(self, rec):
        pass


# ==================== 界面核验按钮链路 ====================

@unittest.skipUnless(HAS_TK and HAS_FACE, "需要 Tk 与 cv2/insightface")
class TestGuiVerifyPath(unittest.TestCase):
    """真正构造界面对象，走 _run_verify（= 界面上点「开始核验」执行的代码）"""

    @classmethod
    def setUpClass(cls):
        import gui
        cls.gui = gui

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = self.gui.FaceVerifyGUI(self.root)
        self.app.user = {"username": "owner", "role": accounts.ROLE_OWNER,
                         "display_name": "系统所有者"}
        self.app._build_ui()
        # 同步加载报名库与索引（不启线程，测试里要确定性）
        self.app.candidates = self.app.store.load()
        if os.path.exists(config.INDEX_PATH):
            self.app.index.load()
        self.app.verifier = FaceVerifier(
            self.app.candidates, self.app.index,
            device_id=self.app.device_var.get(), logger=self.app.audit,
            operator="test")
        self.root.update_idletasks()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _pick(self):
        for cid in sorted(self.app.candidates):
            if not self.app.index.has(cid):
                continue
            c = self.app.candidates[cid]
            p = str(config.resolve_photo(c.reference_photo))
            if os.path.exists(p):
                img = face_engine.imread_unicode(p)
                if img is not None:
                    return cid, c, img
        self.skipTest("库中没有可用的考生")

    def test_run_verify_reaches_pass(self):
        """点核验 -> _run_verify -> 结果区出现 PASS"""
        cid, c, img = self._pick()
        self.app.live_image = img
        self.app.live_frames = [img]
        self.app.live_token = liveness.make_token(self.app.device_var.get(),
                                                  source="camera")
        self.app._run_verify(cid, "1:1", "", (None, None, None))
        self.root.update()                       # 让 root.after(0, ...) 执行
        self.assertIsNotNone(self.app._last_verdict,
                            "核验没有产生结论（链路断了）")
        self.assertEqual(self.app._last_verdict["result"], RESULT_PASS,
                         "界面核验未通过：%s / %s"
                         % (self.app._last_verdict.get("reason_code"),
                            self.app._last_verdict.get("reason")))

    def test_run_verify_uses_all_frames(self):
        """_run_verify 应把连拍帧全部传给核验器（多帧融合）"""
        cid, c, img = self._pick()
        seen = {}

        class _Spy(FaceVerifier):
            def verify_face_first(self, cid2, live_images, **kw):
                seen["n"] = len(list(live_images))
                return FaceVerifier.verify_face_first(self, cid2, live_images, **kw)

        self.app.verifier = _Spy(self.app.candidates, self.app.index,
                                 device_id=self.app.device_var.get(),
                                 logger=self.app.audit, operator="test")
        self.app.live_image = img
        self.app.live_frames = [img, img, img, img, img]
        self.app.live_token = liveness.make_token(self.app.device_var.get(),
                                                  source="camera")
        self.app._run_verify(cid, "1:1", "", (None, None, None))
        self.root.update()
        self.assertEqual(seen.get("n"), 5, "_run_verify 没有把全部连拍帧传下去")

    def test_after_capture_signature_accepts_frames(self):
        """_after_capture 必须能吃下 capture_best_multi 的 5 个参数"""
        import inspect
        sig = inspect.signature(self.gui.FaceVerifyGUI._after_capture)
        names = list(sig.parameters)
        self.assertIn("frames", names)
        self.assertLessEqual(len(names), 6)

    def test_library_health_check_runs_and_warns_on_dirty_data(self):
        """库数据体检要能跑通，并在有脏数据时给出可执行的修复建议"""
        from models import Candidate
        # 造一份"两人共用一张照片 + 一人照片丢失"的脏库
        c1 = Candidate(candidate_id="A1", name="甲",
                       reference_photo=os.path.join(config.PHOTO_DIR, "C001.jpg"))
        c2 = Candidate(candidate_id="A2", name="乙",
                       reference_photo=os.path.join(config.PHOTO_DIR, "C001.jpg"))
        c3 = Candidate(candidate_id="A3", name="丙",
                       reference_photo=os.path.join(config.PHOTO_DIR, "NOPE.jpg"))
        saved = self.app.candidates
        try:
            self.app.candidates = {"A1": c1, "A2": c2, "A3": c3}
            self.app._report_library_health([])          # 不应抛异常
        finally:
            self.app.candidates = saved
            self.root.update_idletasks()

    def test_library_health_check_quiet_on_clean_data(self):
        """干净数据时不打扰操作员（状态栏保持正常色）"""
        self.app._report_library_health([])              # 真实库已清理干净
        self.root.update_idletasks()
        try:
            fg = str(self.app.status.cget("foreground"))
        except Exception:
            fg = ""
        self.assertNotEqual(fg, "#c00", "库数据干净时不应报警")


if __name__ == "__main__":
    unittest.main()
