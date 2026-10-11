# -*- coding: utf-8 -*-
"""
核心逻辑单元测试（不需要 cv2 / insightface：通过注入假特征提取器验证算法与流程）。

运行：
    python -m unittest discover -s tests -v
    （或）python tests/test_core.py
"""
import json
import os
import shutil
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cv2                                            # noqa: F401
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

import numpy as np                                   # noqa: E402

import pii                                            # noqa: E402
import secure_store                                    # noqa: E402
import accounts                                        # noqa: E402
from audit import AuditLogger, compute_hash            # noqa: E402
from face_engine import FaceError                      # noqa: E402
from face_index import FaceIndex                       # noqa: E402
from models import (                                   # noqa: E402
    RC_AMBIGUOUS, RC_AUTO_PASS, RC_CONSENT_MISSING, RC_DOC_CHECK_CONFIRMED,
    RC_DOC_MISMATCH, RC_ID_MISMATCH, RC_IDENTITY_CONFLICT, RC_IMAGE_ERROR,
    RC_IMAGE_QUALITY_REVIEW, RC_LIVENESS_FAILED, RC_LIVENESS_UNAVAILABLE,
    RC_LOW_SIMILARITY, RC_NO_FACE, RC_NOT_MATCHED, RC_POSE_EXTREME, RC_SEAT_MISMATCH,
    RC_TICKET_MISMATCH, RC_TWIN_SUSPECT, RESULT_FAIL, RESULT_MANUAL, RESULT_PASS,
    Candidate, VerifyRecord,
)
from quality import QualityReport                       # noqa: E402
import liveness                                        # noqa: E402
from store import CandidateStore, init_schema          # noqa: E402
from verifier import FaceVerifier                      # noqa: E402

DEVICE = "GATE-TEST-01"

# 测试临时目录放在工程内（沙箱外临时目录可能不可写/不可删）
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)

# 所有者开关的标记文件重定向到测试临时目录，并复位为**出厂默认**
# （大考模式关闭 / 严格模式开启）。
# 这样无论真实 data\ 目录里的开关被改成什么，测试结果都可复现，
# 也不会因为跑测试而改动真实运行数据。
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
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# 两个"人"的特征向量，余弦相似度约等于 0（正交）
VEC_A = unit(np.eye(512, dtype=np.float32)[0])
VEC_B = unit(np.eye(512, dtype=np.float32)[1])


class FakeEmbedder:
    """把"图像"直接当作特征向量；字符串键用于构造异常与固定人物。"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def __call__(self, img):
        if isinstance(img, str):
            if img == "NOFACE":
                raise FaceError(RC_NO_FACE, "未检测到人脸")
            if img == "BADIMAGE":
                raise FaceError(RC_IMAGE_ERROR, "图像无法解码")
            if img in self.mapping:
                return self.mapping[img]
            raise AssertionError("未定义的假图像：%s" % img)
        return np.asarray(img, dtype=np.float32)


def make_candidate(cid="C001", id_card="330100199001011234", consent="已同意",
                   ticket="2026T001", seat="A-01"):
    salt, h, masked = pii.register_id_card(id_card)
    return Candidate(candidate_id=cid, name="测试考生", ticket_no=ticket, seat_no=seat,
                     id_card_salt=salt, id_card_hash=h, id_card_masked=masked,
                     consent_status=consent)


class TestPII(unittest.TestCase):
    def test_validate_and_mask(self):
        ok, _ = pii.validate_id_card("330100199001011234")
        self.assertTrue(ok)                               # 默认不校验校验位（演示数据）
        self.assertFalse(pii.validate_id_card("123")[0])
        self.assertFalse(pii.validate_id_card("33010019900101123A")[0])
        self.assertEqual(pii.mask_id_card("330100199001011234"), "3301**********1234")

    def test_strict_checksum_mode(self):
        """生产环境开启校验位校验后，编造号码应被拒绝"""
        self.assertFalse(pii.validate_id_card("330100199001011234", strict_checksum=True)[0])
        # 一个校验位正确的号码
        self.assertTrue(pii.validate_id_card("11010519491231002X", strict_checksum=True)[0])

    def test_hash_is_salted(self):
        s1, h1, _ = pii.register_id_card("330100199001011234")
        s2, h2, _ = pii.register_id_card("330100199001011234")
        self.assertNotEqual(h1, h2)          # 同号不同盐 -> 哈希不同，防跨表比对
        # 登记走的是密钥化方案；此处的等值比对必须用对应的校验函数
        self.assertTrue(pii.verify_id_card("330100199001011234", s1, h1))
        # 旧方案（加盐 SHA-256）单独验证，保证历史数据兼容
        self.assertEqual(pii.hash_id_card("330100199001011234", s1),
                         pii.hash_id_card("330100199001011234", s1))

    def test_log_sanitize(self):
        s = pii.sanitize_for_log("考生 330100199001011234 手机 13800138000 异常")
        self.assertNotIn("330100199001011234", s)
        self.assertNotIn("13800138000", s)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir("store")
        self.db = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        cleanup(self.tmp)

    def test_upsert_hashes_id_card_and_keeps_it(self):
        st = CandidateStore(self.db)
        st.upsert_many([{"candidate_id": "C001", "name": "张三",
                         "id_card": "330100199001011234",
                         "ticket_no": "T1", "seat_no": "A-01"}], operator="tester")
        c = st.load()["C001"]
        self.assertTrue(c.id_card_hash and c.id_card_salt)
        self.assertEqual(c.id_card_masked, "3301**********1234")
        self.assertTrue(c.check_id_card("330100199001011234")[0])
        self.assertFalse(c.check_id_card("330100199001011235")[0])

        # 未传明文身份证号时，应保留原哈希（不能被清空）
        st.upsert_many([{"candidate_id": "C001", "name": "张三改", "id_card": "",
                         "ticket_no": "T1", "seat_no": "A-02"}], operator="tester")
        c2 = st.load()["C001"]
        self.assertEqual(c2.name, "张三改")
        self.assertEqual(c2.id_card_hash, c.id_card_hash)
        self.assertEqual(c2.consent_status, "未登记")

    def test_upsert_rolls_back_on_bad_row(self):
        st = CandidateStore(self.db)
        st.upsert_many([{"candidate_id": "C001", "name": "A", "id_card": "330100199001011234"}])
        with self.assertRaises(ValueError):
            st.upsert_many([
                {"candidate_id": "C002", "name": "B", "id_card": "330100199001011234"},
                {"candidate_id": "C003", "name": "C", "id_card": "不是身份证"},
            ])
        self.assertEqual(sorted(st.load()), ["C001"])       # 整批回滚

    def test_duplicate_candidate_id_rejected(self):
        st = CandidateStore(self.db)
        with self.assertRaises(ValueError):
            st.upsert_many([{"candidate_id": "C001", "name": "A"},
                            {"candidate_id": "C001", "name": "B"}])

    def test_migration_removes_plaintext(self):
        """旧库（明文 id_card 列）能被自动迁移，且迁移后文件里不再含明文。"""
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, id_card TEXT NOT NULL, ticket_no TEXT NOT NULL,
            seat_no TEXT NOT NULL, reference_photo TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT)""")
        conn.execute("INSERT INTO candidates (candidate_id,name,id_card,ticket_no,seat_no,"
                     "reference_photo,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?)",
                     ("C009", "老数据", "330100199001011234", "T9", "B-09", "", "2026-01-01", "old"))
        conn.commit()
        conn.close()

        init_schema(self.db)
        c = CandidateStore(self.db).load()["C009"]
        self.assertEqual(c.id_card_masked, "3301**********1234")
        self.assertTrue(c.id_card_hash)
        with open(self.db, "rb") as f:                      # SQLite 文件里不应再有明文
            raw = f.read()
        self.assertNotIn(b"330100199001011234", raw)

    def test_consent(self):
        st = CandidateStore(self.db)
        st.upsert_many([{"candidate_id": "C001", "name": "A"}])
        self.assertEqual(st.load()["C001"].consent_status, "未登记")
        st.set_consent("C001", "已同意", operator="admin")
        self.assertTrue(st.load()["C001"].consent_ok())


class TestFaceIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir("index")
        self.path = os.path.join(self.tmp, "idx.enc")
        self.emb = FakeEmbedder({"C001": VEC_A, "C002": VEC_B})

    def tearDown(self):
        cleanup(self.tmp)

    def test_identify_is_vectorized_and_correct(self):
        idx = FaceIndex(embedder=self.emb, path=self.path, resolve_paths=False)
        cands = {"C001": make_candidate("C001"), "C002": make_candidate("C002", "330100199202022345")}
        cands["C001"].reference_photo = "C001"              # 假图像键
        cands["C002"].reference_photo = "C002"
        stats = idx.build(cands)
        self.assertEqual(stats["built"], 2)
        self.assertEqual(idx.size, 2)

        hits = idx.identify(VEC_A, topk=2)
        self.assertEqual(hits[0][0], "C001")
        self.assertGreater(hits[0][1], 0.99)
        self.assertLess(hits[1][1], 0.01)

    def test_score_same_and_other(self):
        idx = FaceIndex(embedder=self.emb, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        s_same, _ = idx.score("C001", idx.embedding_of("C001"))
        self.assertGreater(s_same, 0.99)
        s_other, _ = idx.score("C001", VEC_B)
        self.assertLess(s_other, 0.01)

    def test_encrypted_persistence_round_trip(self):
        idx = FaceIndex(embedder=self.emb, path=self.path, resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        idx.save()
        with open(self.path, "rb") as f:
            head = f.read(16)
        self.assertTrue(head.startswith((b"DPAPI1\n", b"FERNET1\n", b"PLAIN1\n")),
                        "索引必须以加密头落盘，实际为 %r" % head[:8])

        idx2 = FaceIndex(embedder=self.emb, path=self.path, resolve_paths=False)
        self.assertTrue(idx2.load())
        self.assertEqual(sorted(idx2.ids), ["C001", "C002"])
        self.assertGreater(idx2.identify(VEC_B)[0][1], 0.99)

    def test_missing_embedding_raises(self):
        idx = FaceIndex(embedder=self.emb, path=self.path, resolve_paths=False)
        with self.assertRaises(FaceError):
            idx.score("C999", VEC_A)


class TestVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir("verifier")
        self.logger = AuditLogger(os.path.join(self.tmp, "audit.log"))
        self.emb = FakeEmbedder()
        self.index = FaceIndex(embedder=self.emb, path=os.path.join(self.tmp, "i.enc"), resolve_paths=False)
        self.index._append("C001", VEC_A)
        self.index._append("C002", VEC_B)
        self.c1 = make_candidate("C001")
        self.c2 = make_candidate("C002", "330100199202022345", ticket="2026T002", seat="A-02")
        self.candidates = {"C001": self.c1, "C002": self.c2}
        self.v = FaceVerifier(self.candidates, self.index, DEVICE, self.logger,
                              operator="tester", enforce_consent=True, require_liveness=True)
        self.token = liveness.make_token(DEVICE, source="camera")

    def tearDown(self):
        cleanup(self.tmp)

    def _verify(self, candidate=None, image=None, token="__default__", **kw):
        c = candidate or self.c1
        kwargs = dict(id_card="330100199001011234" if c is self.c1 else "330100199202022345",
                      ticket_no=kw.pop("ticket", c.ticket_no), seat_no=kw.pop("seat", c.seat_no),
                      capture_token=self.token if token == "__default__" else token)
        kwargs.update(kw)
        return self.v.verify_with_retry(c.candidate_id, live_images=[image if image is not None else VEC_A],
                                        **kwargs)

    # ---- 核心回归：旧版的"误识现场"必须被拦下 ----
    def test_other_person_is_not_pass(self):
        rec = self._verify(image=VEC_B)                    # 拿 B 的特征冒充 C001
        self.assertNotEqual(rec.result, RESULT_PASS)
        self.assertIn(rec.reason_code, (RC_NOT_MATCHED, RC_LOW_SIMILARITY, RC_AMBIGUOUS))

    def test_clear_mismatch_is_fail_not_manual(self):
        """画质正常但相似度显著偏低 -> FAIL（判定非本人），而不是含糊地"转人工" """
        rec = self._verify(image=VEC_B)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_NOT_MATCHED)
        self.assertTrue(rec.manual_required)               # 仍需人工介入处置

    def test_same_person_passes(self):
        rec = self._verify(image=VEC_A)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertGreaterEqual(rec.score, 0.99)

    def test_threshold_is_raw_cosine(self):
        """相似度必须是原始余弦：0.36 不能通过（旧版映射后等价于 0.30 就放行）"""
        vec = unit(0.36 * VEC_A + np.sqrt(1 - 0.36 ** 2) * VEC_B)
        rec = self._verify(image=vec)
        self.assertNotEqual(rec.result, RESULT_PASS)
        self.assertAlmostEqual(rec.score, 0.36, places=3)

    # ---- 硬失败不得降级 ----
    def test_ticket_mismatch_is_hard_fail_without_retry(self):
        before = len(self.logger.query())
        rec = self._verify(ticket="WRONG")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_TICKET_MISMATCH)
        self.assertEqual(len(self.logger.query()) - before, 1)   # 不重试

    def test_id_mismatch_is_hard_fail(self):
        rec = self._verify(id_card="110100199001011234")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)

    def test_consent_missing_blocks_verification(self):
        self.c1.consent_status = "未登记"
        rec = self._verify()
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_CONSENT_MISSING)

    def test_no_face(self):
        rec = self._verify(image="NOFACE")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_NO_FACE)

    # ---- 现场性 ----
    def test_strict_mode_rejects_file_sourced_image(self):
        rec = self._verify(token=None)                     # 文件来源没有现场令牌
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)

    def test_expired_token_rejected(self):
        tok = liveness.make_token(DEVICE, source="camera")
        tok["ts"] -= 3600
        rec = self._verify(token=tok)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)

    def test_other_device_token_rejected(self):
        tok = liveness.make_token("GATE-OTHER", source="camera")
        rec = self._verify(token=tok)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)

    # ---- 重试与边界 ----
    def test_retry_writes_every_attempt_and_ends_manual(self):
        """灰区（模糊匹配）会重试，两次都留痕，最终转人工复验"""
        before = len(self.logger.query())
        e3 = np.zeros(512, dtype=np.float32); e3[2] = 1.0
        # 与本人相似 0.50（∈ 灰区 [0.45,0.55)），与另一名考生仅 0.10（避免触发身份混淆）
        gray = unit(0.50 * VEC_A + 0.10 * VEC_B + np.sqrt(1 - 0.25 - 0.01) * e3)
        rec = self.v.verify_with_retry("C001", live_images=[gray, gray],
                                       id_card="330100199001011234", ticket_no="2026T001",
                                       seat_no="A-01", capture_token=self.token)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_AMBIGUOUS)
        self.assertTrue(rec.required_witness)
        self.assertEqual(len(self.logger.query()) - before, 2)

    def test_empty_image_list_does_not_crash(self):
        rec = self.v.verify_with_retry("C001", live_images=[], id_card="x",
                                       ticket_no="y", seat_no="z")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_IMAGE_ERROR)

    def test_unknown_candidate(self):
        rec = self.v.verify_with_retry("C999", live_images=[VEC_A], id_card="x",
                                       ticket_no="y", seat_no="z", capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, "NOT_FOUND")

    def test_1toN_picks_correct_person(self):
        rec = self.v.verify_1toN(VEC_B, capture_token=self.token)
        self.assertEqual(rec.candidate_id, "C002")
        self.assertEqual(rec.result, RESULT_PASS)

    def test_1toN_strict_liveness(self):
        rec = self.v.verify_1toN(VEC_B, capture_token=None)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)


class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir("audit")
        self.path = os.path.join(self.tmp, "audit.log")
        self.logger = AuditLogger(self.path)

    def tearDown(self):
        cleanup(self.tmp)

    def _write(self, n=3, ts=None):
        for i in range(n):
            self.logger.log(VerifyRecord(
                timestamp=ts or datetime.now().isoformat(timespec="seconds"),
                device_id=DEVICE, candidate_id="C001", mode="1:1", result="PASS",
                reason_code="PASS", reason="相似度 0.9"))

    def test_chain_ok_and_tamper_detected(self):
        self._write(3)
        self.assertTrue(self.logger.verify_chain()["ok"])

        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        rec = json.loads(lines[1])
        rec["reason"] = "被篡改的内容"                        # 只改内容，不动哈希
        lines[1] = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        r = AuditLogger(self.path).verify_chain()
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], 2)

    def test_delete_middle_line_detected(self):
        self._write(3)
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines([lines[0], lines[2]])
        r = AuditLogger(self.path).verify_chain()
        self.assertFalse(r["ok"])

    def test_purge_keeps_chain_verifiable(self):
        old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        self._write(2, ts=old)                                # 两条过期
        self._write(1)                                        # 一条有效
        r = self.logger.purge_expired(days=30)
        self.assertEqual(r["removed"], 2)
        self.assertEqual(r["kept"], 1)
        self.assertTrue(os.path.exists(r["backup"]))
        after = AuditLogger(self.path)
        self.assertTrue(after.verify_chain()["ok"])
        self.assertEqual(len(after.query()), 2)                # CHAIN_RESET + 保留的 1 条

    def test_purge_dry_run_does_nothing(self):
        old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        self._write(2, ts=old)
        r = self.logger.purge_expired(days=30, dry_run=True)
        self.assertEqual(r["removed"], 2)
        self.assertIsNone(r["backup"])
        self.assertEqual(len(AuditLogger(self.path).query()), 2)

    def test_hash_helper_stable(self):
        core = {"a": 1, "b": "中文"}
        self.assertEqual(compute_hash("p", core), compute_hash("p", dict(core)))
        self.assertNotEqual(compute_hash("p", core), compute_hash("q", core))


class TestAnomalyHandling(unittest.TestCase):
    """
    极端情况 / 异常状态的处理（本次新增能力）：
      灰区模糊匹配 -> 人工复验；孪生撞脸 / 身份混淆 -> 双人见证复验；
      昏暗等画质问题 -> 增强后重判，仍严重则复验；仰拍等极端姿态 -> 复验。
    """

    def setUp(self):
        self.tmp = tmpdir("anomaly")
        self.logger = AuditLogger(os.path.join(self.tmp, "audit.log"))
        self.c1 = make_candidate("C001")
        self.c2 = make_candidate("C002", "330100199202022345", ticket="2026T002", seat="A-02")
        self.candidates = {"C001": self.c1, "C002": self.c2}
        self.token = liveness.make_token(DEVICE, source="camera")

    def tearDown(self):
        cleanup(self.tmp)

    def _verifier(self, live_vec, quality=None, enhanced=False):
        """构造一个可注入 现场特征 + 画质报告 的核验器"""
        def extractor(img):
            info = {"enhanced": enhanced}
            if quality is not None:
                info["quality"] = quality
            return np.asarray(live_vec, dtype=np.float32), info

        idx = FaceIndex(extractor=extractor, path=os.path.join(self.tmp, "i.enc"),
                        resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        v = FaceVerifier(self.candidates, idx, DEVICE, self.logger, operator="tester",
                         enforce_consent=True, require_liveness=True)
        return v, idx

    @staticmethod
    def _mix(ca, cb, third=0.0):
        """构造一个单位向量：与 A 的余弦为 ca、与 B 的余弦为 cb"""
        rest = max(0.0, 1.0 - ca * ca - cb * cb)
        v = ca * VEC_A + cb * VEC_B
        if rest > 0:
            e3 = np.zeros(512, dtype=np.float32); e3[2] = 1.0
            v = v + np.sqrt(rest) * e3
        return unit(v)

    def _verify(self, v, cid="C001", image=None):
        return v.verify_with_retry(
            cid, live_images=[image if image is not None else VEC_A],
            id_card=(self.c1.id_card_masked and "330100199001011234"),
            ticket_no="2026T001", seat_no="A-01", capture_token=self.token)

    # ---- 灰区（模糊匹配） ----
    def test_gray_zone_triggers_manual_review(self):
        live = self._mix(0.50, 0.10)                 # 本人 0.50 ∈ [0.45, 0.55) 灰区
        v, _ = self._verifier(live)
        rec = self._verify(v, image=live)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_AMBIGUOUS)
        self.assertTrue(rec.gray_zone)
        self.assertTrue(rec.required_witness)

    def test_above_threshold_passes(self):
        live = self._mix(0.70, 0.05)
        v, _ = self._verifier(live)
        rec = self._verify(v, image=live)
        self.assertEqual(rec.result, RESULT_PASS)

    # ---- 双胞胎 / 撞脸 ----
    def test_twin_suspect_triggers_witness_review(self):
        live = self._mix(0.60, 0.55)                 # 本人 0.60，最像的另一人 0.55，差距 0.05
        v, _ = self._verifier(live)
        rec = self._verify(v, image=live)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_TWIN_SUSPECT)
        self.assertEqual(rec.twin_candidate_id, "C002")
        self.assertLess(rec.twin_gap, 0.08)
        self.assertTrue(rec.required_witness)

    def test_identity_conflict_when_other_scores_higher(self):
        live = self._mix(0.60, 0.80)                 # 更像 C002 而不是被核验的 C001
        v, _ = self._verifier(live)
        rec = self._verify(v, image=live)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_IDENTITY_CONFLICT)
        self.assertTrue(rec.required_witness)

    def test_twin_pairs_and_nearest_other(self):
        idx = FaceIndex(embedder=FakeEmbedder({"C001": VEC_A}), path=os.path.join(self.tmp, "i2.enc"),
                        resolve_paths=False)
        near_a = unit(0.9 * VEC_A + np.sqrt(1 - 0.81) * VEC_B)     # 与 A 相似 0.90
        idx._append("C001", VEC_A)
        idx._append("C002", near_a)
        idx._append("C003", VEC_B)
        pairs = idx.twin_pairs(threshold=0.80)
        self.assertEqual(len(pairs), 1)
        self.assertEqual({pairs[0]["a"], pairs[0]["b"]}, {"C001", "C002"})
        self.assertAlmostEqual(pairs[0]["score"], 0.90, places=2)
        who, score = idx.nearest_other("C001")
        self.assertEqual(who, "C002")
        self.assertAlmostEqual(score, 0.90, places=2)

    # ---- 画质（昏暗等） ----
    def test_severe_quality_forces_review_even_with_high_score(self):
        q = QualityReport(brightness=18, sharpness=8, severity="severe",
                          issues=["严重欠曝", "画面模糊"])
        v, _ = self._verifier(VEC_A, quality=q, enhanced=True)
        rec = self._verify(v)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_IMAGE_QUALITY_REVIEW)
        self.assertTrue(rec.enhanced)
        self.assertIn("严重欠曝", rec.quality_issues)

    def test_warn_quality_with_low_score_is_manual_not_fail(self):
        q = QualityReport(brightness=40, sharpness=25, severity="warn", issues=["光线昏暗"])
        live = self._mix(0.20, 0.05)                 # 明显低于灰区
        v, _ = self._verifier(live, quality=q)
        rec = self._verify(v, image=live)
        self.assertEqual(rec.result, RESULT_MANUAL)   # 画质有告警就不下"非本人"的结论
        self.assertEqual(rec.reason_code, RC_LOW_SIMILARITY)

    # ---- 姿态（仰拍等） ----
    def test_extreme_pose_triggers_review(self):
        q = QualityReport(brightness=120, sharpness=200, severity="warn",
                          issues=["仰拍（相机低于人脸）"], pitch=-46.0)
        v, _ = self._verifier(VEC_A, quality=q)
        rec = self._verify(v)
        self.assertEqual(rec.result, RESULT_MANUAL)
        self.assertEqual(rec.reason_code, RC_POSE_EXTREME)
        self.assertTrue(rec.required_witness)
        self.assertLess(rec.pitch, 0)                 # 负值 = 仰拍


@unittest.skipUnless(HAS_CV2, "需要 opencv-python 才能做画质/姿态实测")
class TestQualityModule(unittest.TestCase):
    """真实图像上的画质评估与增强（用合成图，不依赖模型）"""

    def test_dark_image_assessed_and_enhanced(self):
        import cv2
        import quality
        dark = np.full((480, 640, 3), 15, dtype=np.uint8)          # 极暗画面
        dark[100:380, 200:440] = 28                                 # 人脸区域也很暗
        rep = quality.assess(dark, bbox=[200, 100, 440, 380])
        self.assertIn(rep.severity, ("warn", "severe"))
        self.assertLess(rep.brightness, 60)
        self.assertTrue(any("暗" in x or "欠曝" in x for x in rep.issues))

        bright = quality.enhance(dark)
        rep2 = quality.assess(bright, bbox=[200, 100, 440, 380])
        self.assertGreater(rep2.brightness, rep.brightness)         # 增强后确实更亮

    def test_pose_estimate_for_upward_shot(self):
        import quality
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        bbox = [200, 100, 440, 380]
        # 正面：眼睛在上、鼻子居中、嘴在下
        frontal = [(280, 190), (360, 190), (320, 240), (285, 320), (355, 320)]
        y1, p1, r1 = quality._pose_from_kps(frontal, bbox)
        self.assertAlmostEqual(y1, 0.0, delta=8)
        self.assertAlmostEqual(r1, 0.0, delta=8)

        # 仰拍：相机低于人脸 -> 额头被压缩，鼻下比例变大 -> pitch 变负
        upward = [(280, 210), (360, 210), (320, 265), (285, 360), (355, 360)]
        _y2, p2, _r2 = quality._pose_from_kps(upward, bbox)
        self.assertLess(p2, p1)

        # 侧脸：鼻子明显偏向一侧 -> yaw 变大
        side = [(280, 190), (360, 190), (400, 240), (285, 320), (355, 320)]
        y3, _p3, _r3 = quality._pose_from_kps(side, bbox)
        self.assertGreater(abs(y3), 35)


class TestEdge(unittest.TestCase):
    """
    边缘计算层：推理后端自适应、算力降档、批处理、断网待上报队列。
    全部用注入/本地文件验证，不需要真的连网或真的装 Atlas/Jetson。
    """

    def setUp(self):
        self.tmp = tmpdir("edge")

    def tearDown(self):
        cleanup(self.tmp)

    # ---- 推理后端选择 ----
    def test_provider_selection(self):
        import edge
        cpu = edge.detect_providers(force="cpu")
        self.assertEqual(cpu["active"], "CPUExecutionProvider")
        self.assertEqual(cpu["providers"], ["CPUExecutionProvider"])
        self.assertTrue(cpu["forced"])

        auto = edge.detect_providers(force="auto")
        self.assertIn(auto["active"], auto["available"])
        self.assertGreaterEqual(len(auto["providers"]), 1)

    def test_unknown_provider_falls_back_to_cpu_with_note(self):
        import edge
        r = edge.detect_providers(force="CANNExecutionProvider")
        if "CANNExecutionProvider" in r["available"]:
            self.assertEqual(r["active"], "CANNExecutionProvider")
        else:
            self.assertEqual(r["active"], "CPUExecutionProvider")
            self.assertIn("降级", r["note"])

    def test_backend_description_mentions_missing_accelerators(self):
        import edge
        text = edge.describe_backends()
        self.assertIn("推理后端", text)
        self.assertIn("当前生效", text)

    # ---- 档位与算力自适应 ----
    def test_profiles_are_ordered_by_cost(self):
        import edge
        self.assertLess(edge.PROFILES["lite"].det_size, edge.PROFILES["full"].det_size)
        # 增强是安全功能（昏暗时避免误判），过载降档只降分辨率、不关增强
        self.assertTrue(edge.PROFILES["lite"].auto_enhance)
        self.assertTrue(edge.PROFILES["full"].auto_enhance)
        self.assertLessEqual(edge.PROFILES["lite"].max_fps, edge.PROFILES["balanced"].max_fps)

    def test_monitor_degrades_on_high_latency_and_recovers(self):
        import edge
        m = edge.ResourceMonitor(profile="full", target_latency_ms=200, hard_latency_ms=500,
                                 window=5)
        for _ in range(5):
            m.observe(1200)                       # 严重超时
        self.assertEqual(m.profile_name, "balanced")
        self.assertTrue(m.degraded)
        self.assertIn("降档", m.last_change_reason)

        for _ in range(5):
            m.observe(1500)
        self.assertEqual(m.profile_name, "lite")   # 再降到最低档

        for _ in range(5):
            m.observe(20)                          # 恢复
        self.assertIn(m.profile_name, ("balanced", "full"))

    def test_monitor_snapshot_has_decision_fields(self):
        import edge
        m = edge.ResourceMonitor(profile="balanced")
        m.observe(120)
        snap = m.snapshot()
        for k in ("profile", "degraded", "avg_latency_ms", "det_size", "max_fps"):
            self.assertIn(k, snap)
        self.assertEqual(snap["det_size"], edge.PROFILES["balanced"].det_size)

    # ---- 断网降级：待上报队列 ----
    def test_outbox_enqueue_and_keep_on_failure(self):
        import edge
        ob = edge.Outbox(path=os.path.join(self.tmp, "outbox.jsonl"))
        self.assertEqual(ob.pending(), 0)
        ob.enqueue({"candidate_id": "C001", "result": "PASS"})
        ob.enqueue({"candidate_id": "C002", "result": "MANUAL"})
        self.assertEqual(ob.pending(), 2)

        r = ob.flush()                             # 未配置上报地址
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["left"], 2)
        self.assertEqual(ob.pending(), 2)          # 数据不丢

        r2 = ob.flush(endpoint="http://127.0.0.1:9/upload", timeout=1)
        self.assertEqual(r2["sent"], 0)
        self.assertGreaterEqual(ob.pending(), 1)   # 上报失败仍保留

    def test_outbox_records_are_structured_only(self):
        """上报内容只含结构化字段，不含图像/特征（边缘只传结论）"""
        import edge
        ob = edge.Outbox(path=os.path.join(self.tmp, "o.jsonl"))
        rec = VerifyRecord(timestamp="2026-01-01T00:00:00", device_id="GATE-A-01",
                           candidate_id="C001", mode="1:1", result="PASS",
                           reason_code="PASS", score=0.91)
        ob.enqueue(rec.to_dict())
        with open(ob.path, "r", encoding="utf-8") as f:
            line = f.read()
        self.assertIn("candidate_id", line)
        self.assertNotIn("embedding", line)
        self.assertNotIn("image", line)
        self.assertNotIn("base64", line)

    # ---- 批处理 ----
    def test_identify_batch(self):
        emb = FakeEmbedder({"C001": VEC_A, "C002": VEC_B})
        idx = FaceIndex(embedder=emb, path=os.path.join(self.tmp, "b.enc"), resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        res = idx.identify_batch([VEC_A, VEC_B, "NOFACE"], topk=1)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0][0][0], "C001")
        self.assertEqual(res[1][0][0], "C002")
        self.assertEqual(res[2], [])               # 失败位置为空，不中断整批

    def test_extract_batch_isolates_failures(self):
        emb = FakeEmbedder({"C001": VEC_A})
        idx = FaceIndex(embedder=emb, path=os.path.join(self.tmp, "b2.enc"), resolve_paths=False)
        feats = idx.extract_batch([VEC_A, "NOFACE", VEC_B])
        self.assertEqual(len(feats), 3)
        self.assertIsNotNone(feats[0])
        self.assertIsNone(feats[1])
        self.assertIsNotNone(feats[2])


class TestDocumentCheck(unittest.TestCase):
    """
    人脸先过、证件后核：
      人脸通过 -> 自动带出该考生证件信息（掩码）-> 工作人员目视核对 -> 单独写审计记录。
      另外覆盖"显式跳过身份证哈希比对"（仅演示，必须留痕）。
    """

    def setUp(self):
        self.tmp = tmpdir("doccheck")
        self.logger = AuditLogger(os.path.join(self.tmp, "audit.log"))
        self.c1 = make_candidate("C001")
        self.c2 = make_candidate("C002", "330100199202022345", ticket="2026T002", seat="A-02")
        self.candidates = {"C001": self.c1, "C002": self.c2}
        idx = FaceIndex(embedder=FakeEmbedder({"C001": VEC_A, "C002": VEC_B}),
                        path=os.path.join(self.tmp, "i.enc"), resolve_paths=False)
        idx._append("C001", VEC_A)
        idx._append("C002", VEC_B)
        self.index = idx
        self.token = liveness.make_token(DEVICE, source="camera")

    def tearDown(self):
        cleanup(self.tmp)

    def _verifier(self, **kw):
        return FaceVerifier(self.candidates, self.index, DEVICE, self.logger,
                            operator="tester", enforce_consent=True, require_liveness=True, **kw)

    def test_document_info_shows_mask_only(self):
        v = self._verifier()
        text = v.document_info(self.c1)
        self.assertIn(self.c1.id_card_masked, text)          # 带出的是掩码
        self.assertIn("2026T001", text)
        self.assertIn("A-01", text)
        # 绝不能带出明文
        self.assertNotIn("330100199001011234", text)
        self.assertIn("*", text)

    def test_face_pass_then_confirm_document_writes_separate_record(self):
        v = self._verifier()
        before = len(self.logger.query())
        rec = v.verify_with_retry("C001", live_images=[VEC_A], id_card="330100199001011234",
                                  ticket_no="2026T001", seat_no="A-01", capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertTrue(rec.id_card_display)                 # 人脸记录里已带证件展示信息
        self.assertFalse(rec.id_check_skipped)

        doc = v.confirm_document("C001", operator="tester", witness="王老师", ok=True)
        self.assertEqual(doc.result, RESULT_PASS)
        self.assertEqual(doc.reason_code, RC_DOC_CHECK_CONFIRMED)
        self.assertEqual(doc.mode, "DOC-CHECK")
        self.assertTrue(doc.doc_checked)
        self.assertTrue(doc.doc_checked_at)
        self.assertEqual(doc.witness, "王老师")
        self.assertEqual(len(self.logger.query()) - before, 2)   # 人脸 1 条 + 证件核对 1 条

    def test_document_mismatch_is_intercepted(self):
        v = self._verifier()
        doc = v.confirm_document("C001", operator="tester", ok=False, note="照片与本人明显不同")
        self.assertEqual(doc.result, RESULT_FAIL)
        self.assertEqual(doc.reason_code, RC_DOC_MISMATCH)
        self.assertTrue(doc.manual_required)
        self.assertIn("证件", doc.reason)

    def test_face_first_auto_fills_and_doc_check(self):
        """扫脸核验：不再需要手输身份证号；通过后自动带出身份信息并等待人工证件核对"""
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token,
                                  witness="王老师")
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertIn(self.c1.id_card_masked, rec.id_card_display)   # 自动补齐的是掩码
        self.assertFalse(rec.doc_checked)                            # 尚未人工核对
        doc = v.confirm_document("C001", operator="tester", witness="王老师", ok=True)
        self.assertTrue(doc.doc_checked)

    def test_face_first_still_blocks_other_person(self):
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_B], capture_token=self.token)
        self.assertNotEqual(rec.result, RESULT_PASS)

    def test_face_first_requires_liveness_token(self):
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_A], capture_token=None)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)

    def test_face_first_enforces_consent(self):
        self.c1.consent_status = "未登记"
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token)
        self.assertEqual(rec.reason_code, RC_CONSENT_MISSING)

    def test_1toN_pass_carries_document_info(self):
        v = self._verifier()
        rec = v.verify_1toN(VEC_B, capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertEqual(rec.candidate_id, "C002")
        self.assertIn(self.c2.id_card_masked, rec.id_card_display)

    # ---- 自动核验模式：人脸匹配即自动通过，无需人工点击 ----
    def test_auto_confirm_passes_without_click(self):
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token,
                                  auto_confirm=True)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertTrue(rec.doc_checked)                     # 免人工确认即视为已核对
        self.assertIn("自动通过", rec.reason)
        auto = [r for r in self.logger.query() if r.get("reason_code") == RC_AUTO_PASS]
        self.assertEqual(len(auto), 1)                        # 仍单独写 AUTO_PASS 留痕
        self.assertEqual(auto[0].get("witness"), "自动核验")
        self.assertIn(self.c1.id_card_masked, auto[0].get("id_card_display") or "")

    def test_no_auto_confirm_waits_for_manual_click(self):
        """手动模式仍然必须人工点确认（doc_checked 为 False，且不产生 AUTO_PASS）"""
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertFalse(rec.doc_checked)
        auto = [r for r in self.logger.query() if r.get("reason_code") == RC_AUTO_PASS]
        self.assertEqual(len(auto), 0)

    def test_auto_confirm_only_on_pass(self):
        """不通过时绝不能自动放行"""
        v = self._verifier()
        rec = v.verify_face_first("C001", live_images=[VEC_B], capture_token=self.token,
                                  auto_confirm=True)
        self.assertNotEqual(rec.result, RESULT_PASS)
        self.assertFalse(rec.doc_checked)
        auto = [r for r in self.logger.query() if r.get("reason_code") == RC_AUTO_PASS]
        self.assertEqual(len(auto), 0)

    def test_auto_confirm_in_1toN(self):
        v = self._verifier()
        rec = v.verify_1toN(VEC_B, capture_token=self.token, auto_confirm=True)
        self.assertEqual(rec.result, RESULT_PASS)
        self.assertTrue(rec.doc_checked)
        auto = [r for r in self.logger.query() if r.get("reason_code") == RC_AUTO_PASS]
        self.assertEqual(len(auto), 1)


class TestVideoStream(unittest.TestCase):
    """实时视频：FPS 统计、最新帧、优雅降级（用注入的假采集器，无需真摄像头）"""

    @staticmethod
    def _paced_capture(fps=25.0, frames=40):
        import time as _t

        class Cap:
            def __init__(self):
                self.i = 0

            def isOpened(self):
                return True

            def get(self, prop):
                return fps

            def read(self):
                if self.i >= frames:
                    _t.sleep(0.05)
                    return False, None
                self.i += 1
                _t.sleep(1.0 / fps)
                return True, np.zeros((48, 64, 3), dtype=np.uint8)

            def release(self):
                pass
        return Cap()

    def test_fps_matches_source_rate(self):
        import time
        import video
        s = video.VideoStream(capture_factory=lambda idx: self._paced_capture(25.0))
        self.assertTrue(s.start())
        time.sleep(1.2)
        self.assertGreater(s.fps, 15.0)
        self.assertLess(s.fps, 35.0)          # 不能像旧算法那样算出天文数字
        s.stop()
        self.assertFalse(s.running)

    def test_latest_returns_frame_and_seq(self):
        import video
        s = video.VideoStream(capture_factory=lambda idx: self._paced_capture(50.0))
        s.start()
        frame, seq = s.latest()
        self.assertIsNotNone(frame)
        self.assertGreaterEqual(seq, 1)
        self.assertEqual(frame.shape, (48, 64, 3))
        s.stop()

    def test_no_camera_degrades_gracefully(self):
        import video

        class Dead:
            def isOpened(self):
                return False

            def release(self):
                pass
        s = video.VideoStream(capture_factory=lambda idx: Dead())
        self.assertFalse(s.start())           # 不抛异常
        self.assertIn("无法打开摄像头", s.error)
        self.assertEqual(s.fps, 0.0)
        s.stop()

    def test_status_text_mentions_fps(self):
        import time
        import video
        s = video.VideoStream(capture_factory=lambda idx: self._paced_capture(30.0))
        s.start()
        time.sleep(0.4)
        self.assertIn("FPS", s.status_text())
        s.stop()


class TestIdHashHardening(unittest.TestCase):
    """身份证号哈希加固：密钥化 HMAC + 旧数据兼容"""

    def setUp(self):
        self.tmp = tmpdir("idhash")
        import pii
        self.pii = pii
        # 把密钥文件指到临时目录，避免污染真实数据目录
        self._old_keyfile = pii.KEY_FILE
        pii.KEY_FILE = os.path.join(self.tmp, "id_key.enc")
        pii.reset_key_cache()

    def tearDown(self):
        self.pii.KEY_FILE = self._old_keyfile
        self.pii.reset_key_cache()
        cleanup(self.tmp)

    def test_new_records_use_keyed_hmac(self):
        salt, h, masked = self.pii.register_id_card("330100199001011234")
        self.assertTrue(h.startswith(self.pii.HMAC_PREFIX))
        self.assertNotEqual(salt, "")                               # 仍然每人独立盐
        self.assertTrue(self.pii.verify_id_card("330100199001011234", salt, h))
        self.assertFalse(self.pii.verify_id_card("110100199001011234", salt, h))

    def test_hmac_is_still_salted(self):
        """同一号码两次登记：哈希必须不同（盐仍生效，避免跨记录关联）"""
        _s1, h1, _m = self.pii.register_id_card("330100199001011234")
        _s2, h2, _m2 = self.pii.register_id_card("330100199001011234")
        self.assertNotEqual(h1, h2)

    def test_key_file_is_created_separately_from_db(self):
        self.pii.register_id_card("330100199001011234")
        self.assertTrue(os.path.exists(self.pii.KEY_FILE))
        raw = open(self.pii.KEY_FILE, "rb").read()
        self.assertNotIn(b"330100199001011234", raw)                 # 密钥文件里不含号码
        self.assertGreaterEqual(len(raw), 32)

    def test_legacy_salted_records_still_verify(self):
        """旧库里的 salt+sha256 记录必须继续可用（历史数据兼容）"""
        salt = self.pii.new_salt()
        legacy = self.pii.hash_id_card("330100199001011234", salt)
        self.assertFalse(legacy.startswith(self.pii.HMAC_PREFIX))
        self.assertTrue(self.pii.verify_id_card("330100199001011234", salt, legacy))
        self.assertFalse(self.pii.verify_id_card("330100199202022345", salt, legacy))

    def test_key_missing_means_no_bypass(self):
        """密钥方案记录在拿不到密钥时必须判不通过，而不是放行"""
        _, h, _ = self.pii.register_id_card("330100199001011234")
        self.pii.reset_key_cache()
        os.remove(self.pii.KEY_FILE)                                  # 模拟换机器
        self.assertFalse(self.pii.verify_id_card("330100199001011234", "", h))

    def test_scheme_label(self):
        self.assertEqual(self.pii.id_hash_scheme(""), "未登记")
        self.assertIn("HMAC", self.pii.id_hash_scheme("hmac1:abc"))
        self.assertIn("SHA256", self.pii.id_hash_scheme("deadbeef"))


class TestAccountPrivilege(unittest.TestCase):
    """
    账户管理越级防护：只能管理严格低于自己等级的账户。
    这条规则堵住"管理员重置所有者密码 -> 直接夺权"的越级路径。
    """

    def test_can_manage_matrix(self):
        import accounts as acc
        # 所有者：可管管理员与教师，但不能再造一个所有者
        self.assertTrue(acc.can_manage(acc.ROLE_OWNER, acc.ROLE_ADMIN))
        self.assertTrue(acc.can_manage(acc.ROLE_OWNER, acc.ROLE_TEACHER))
        self.assertFalse(acc.can_manage(acc.ROLE_OWNER, acc.ROLE_OWNER))
        # 管理员：只能管教师；不能动所有者，也不能动同级管理员
        self.assertTrue(acc.can_manage(acc.ROLE_ADMIN, acc.ROLE_TEACHER))
        self.assertFalse(acc.can_manage(acc.ROLE_ADMIN, acc.ROLE_ADMIN))
        self.assertFalse(acc.can_manage(acc.ROLE_ADMIN, acc.ROLE_OWNER))
        # 教师：谁都不能管
        self.assertFalse(acc.can_manage(acc.ROLE_TEACHER, acc.ROLE_TEACHER))
        self.assertFalse(acc.can_manage(acc.ROLE_TEACHER, acc.ROLE_ADMIN))

    def test_admin_cannot_create_admin(self):
        """管理员不能新建管理员（避免横向扩散管理员账户）"""
        import accounts as acc
        ok, msg = acc.create_user("should_not_exist", "越级测试", "Passw0rd!", "admin",
                                  by_role=acc.ROLE_ADMIN)
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)

    def test_teacher_cannot_create_anyone(self):
        import accounts as acc
        ok, msg = acc.create_user("should_not_exist2", "越级测试", "Passw0rd!", "teacher",
                                  by_role=acc.ROLE_TEACHER)
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)

    def test_owner_cannot_create_another_owner(self):
        import accounts as acc
        ok, msg = acc.create_user("should_not_exist3", "越级测试", "Passw0rd!", "owner",
                                  by_role=acc.ROLE_OWNER)
        self.assertFalse(ok)

    def test_auto_verify_permission_owner_only_by_default(self):
        import accounts as acc
        self.assertTrue(acc.can_auto_verify(acc.ROLE_OWNER))
        self.assertFalse(acc.can_auto_verify(acc.ROLE_TEACHER))
        # 管理员默认不可用（是否放开由所有者授权文件决定）
        self.assertEqual(acc.can_auto_verify(acc.ROLE_ADMIN), acc.auto_verify_admin_allowed())


class TestOwnerSwitches(unittest.TestCase):
    """
    两个新的所有者开关：
      · 大考模式（major_exam.on，默认**关闭**）：开启后核验必须使用身份证（四要素）；
        关闭时身份证不参与校验，改用准考证号核验（三要素）。
      · 严格模式（strict_mode.on，默认**开启**）：关闭后允许非现场来源（source=file）
        的核验继续，但记录里必须留痕。

    覆盖：标记文件读写与默认值 / 大考模式开与关下的身份要素判定 /
          严格模式关闭时 file 来源核验被放宽且留痕（含不得绕过活体 FAIL）。
    """

    def setUp(self):
        self.tmp = tmpdir("switches")
        self.acc = accounts
        self._orig_flags = (self.acc.MAJOR_EXAM_FLAG, self.acc.STRICT_MODE_FLAG)
        self.acc.MAJOR_EXAM_FLAG = os.path.join(self.tmp, "major_exam.on")
        self.acc.STRICT_MODE_FLAG = os.path.join(self.tmp, "strict_mode.on")

        self.logger = AuditLogger(os.path.join(self.tmp, "audit.log"))
        self.index = FaceIndex(embedder=FakeEmbedder(), path=os.path.join(self.tmp, "i.enc"),
                               resolve_paths=False)
        self.index._append("C001", VEC_A)
        self.index._append("C002", VEC_B)
        self.vec_c = unit(np.eye(512, dtype=np.float32)[2])
        self.index._append("C900", self.vec_c)          # 无身份证考生的特征

        self.c1 = make_candidate("C001")
        self.c2 = make_candidate("C002", "330100199202022345", ticket="2026T002", seat="A-02")
        # 未登记身份证号的考生：常规模式下应当仍能核验（这正是本次改造要解决的痛点）
        self.no_id = Candidate(candidate_id="C900", name="无证考生", ticket_no="2026T900",
                               seat_no="Z-99", consent_status="已同意")
        self.candidates = {"C001": self.c1, "C002": self.c2, "C900": self.no_id}
        self.v = FaceVerifier(self.candidates, self.index, DEVICE, self.logger,
                              operator="tester", enforce_consent=True, require_liveness=True)
        self.token = liveness.make_token(DEVICE, source="camera")
        self.file_token = liveness.make_token(DEVICE, source="file")

    def tearDown(self):
        self.acc.MAJOR_EXAM_FLAG, self.acc.STRICT_MODE_FLAG = self._orig_flags
        liveness.set_checker(None)
        cleanup(self.tmp)

    # ---------------- 标记文件：默认值与读写 ----------------
    def test_defaults_are_major_off_and_strict_on(self):
        """默认：大考模式关闭（无标记文件）、严格模式开启（无标记文件）"""
        self.assertFalse(os.path.exists(self.acc.MAJOR_EXAM_FLAG))
        self.assertFalse(os.path.exists(self.acc.STRICT_MODE_FLAG))
        self.assertFalse(self.acc.major_exam_enabled())
        self.assertTrue(self.acc.strict_mode_enabled())
        # 非大考模式下的文案不得再自称"四要素"、也不得出现"必须身份证"
        self.assertEqual(self.acc.identity_elements(), ["准考证号", "座位号", "姓名"])
        self.assertTrue(self.acc.identity_mode_title().startswith("三要素"))
        self.assertIn("不参与校验", self.acc.identity_mode_detail())

    def test_major_exam_flag_roundtrip(self):
        ok, msg = self.acc.set_major_exam(True)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(self.acc.MAJOR_EXAM_FLAG))
        self.assertTrue(self.acc.major_exam_enabled())
        self.assertEqual(self.acc.identity_elements(),
                         ["身份证号", "准考证号", "座位号", "姓名"])
        self.assertTrue(self.acc.identity_mode_title().startswith("四要素"))

        ok, msg = self.acc.set_major_exam(False)
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.acc.MAJOR_EXAM_FLAG))
        self.assertFalse(self.acc.major_exam_enabled())

    def test_strict_mode_flag_semantics_are_inverted(self):
        """严格模式默认开启，且 strict_mode.on 是"宽松"标记：文件存在 = 已关闭严格模式"""
        self.assertTrue(self.acc.strict_mode_enabled())
        self.assertFalse(self.acc.strict_mode_relaxed())

        ok, msg = self.acc.set_strict_mode(False)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(self.acc.STRICT_MODE_FLAG),
                        "关闭严格模式应写出标记文件")
        self.assertFalse(self.acc.strict_mode_enabled())
        self.assertTrue(self.acc.strict_mode_relaxed())

        ok, msg = self.acc.set_strict_mode(True)
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.acc.STRICT_MODE_FLAG),
                         "恢复严格模式应删除标记文件")
        self.assertTrue(self.acc.strict_mode_enabled())

    # ---------------- 大考模式关闭（默认）：身份证不参与校验 ----------------
    def test_normal_mode_allows_candidate_without_id_card(self):
        """没有身份证的考生，用准考证号即可完成核验"""
        rec = self.v.verify_with_retry("C900", live_images=[self.vec_c], id_card="",
                                       ticket_no="2026T900", seat_no="Z-99",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertEqual(rec.id_mode, "normal")
        self.assertEqual(rec.elements_checked, "准考证号 + 座位号")
        self.assertTrue(rec.id_check_skipped)

    def test_normal_mode_ticket_is_mandatory(self):
        rec = self.v.verify_with_retry("C900", live_images=[self.vec_c], id_card="",
                                       ticket_no="", seat_no="Z-99",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_TICKET_MISMATCH)

    def test_normal_mode_ticket_mismatch_is_blocked(self):
        rec = self.v.verify_with_retry("C900", live_images=[self.vec_c], id_card="",
                                       ticket_no="WRONG-TICKET", seat_no="Z-99",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_TICKET_MISMATCH)

    def test_normal_mode_masked_id_is_treated_as_not_provided(self):
        """界面带出的掩码（含 *）不是明文，不得拿去哈希比对，也不应导致失败"""
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A],
                                       id_card=self.c1.id_card_masked,
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertTrue(rec.id_check_skipped)

    def test_normal_mode_wrong_plaintext_id_still_blocked(self):
        """常规模式下若填写了身份证明文，仍要求一致（避免"填错也放行"）"""
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A],
                                       id_card="110100199001011234",
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)

    def test_seat_mismatch_is_blocked(self):
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A], id_card="",
                                       ticket_no="2026T001", seat_no="A-99",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_SEAT_MISMATCH)

    # ---------------- 大考模式开启：身份证成为必校验项 ----------------
    def test_major_mode_requires_id_card(self):
        self.acc.set_major_exam(True)
        rec = self.v.verify_with_retry("C900", live_images=[self.vec_c], id_card="",
                                       ticket_no="2026T900", seat_no="Z-99",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)
        self.assertIn("大考模式", rec.reason)
        self.assertEqual(rec.id_mode, "major")

    def test_major_mode_rejects_masked_id(self):
        """大考模式下只带出掩码不算提供明文 -> 拦截"""
        self.acc.set_major_exam(True)
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A],
                                       id_card=self.c1.id_card_masked,
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)

    def test_major_mode_with_correct_plaintext_id_passes(self):
        self.acc.set_major_exam(True)
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A],
                                       id_card="330100199001011234",
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertEqual(rec.id_mode, "major")
        self.assertEqual(rec.elements_checked, "身份证号 + 准考证号 + 座位号")
        self.assertFalse(rec.id_check_skipped)

    # ---------------- 严格模式：开启拒绝 / 关闭放宽且留痕 ----------------
    def test_strict_mode_on_rejects_file_source(self):
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A], id_card="",
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.file_token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)
        self.assertFalse(rec.strict_mode_off)

    def test_strict_mode_off_accepts_file_source_and_traces(self):
        """关闭严格模式后 file 来源可继续核验，但记录必须如实标注非现场来源"""
        self.acc.set_strict_mode(False)
        rec = self.v.verify_with_retry("C001", live_images=[VEC_A], id_card="",
                                       ticket_no="2026T001", seat_no="A-01",
                                       capture_token=self.file_token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertTrue(rec.strict_mode_off)
        self.assertEqual(rec.non_live_source, "file")
        self.assertIn("严格模式已关闭", rec.reason)
        self.assertIn("非现场", rec.reason)
        self.assertIn("source=file", rec.reason)
        # 落盘后的审计记录同样带留痕字段（不是只在内存里放宽）
        last = self.logger.query()[-1]
        self.assertTrue(last.get("strict_mode_off"))
        self.assertEqual(last.get("non_live_source"), "file")
        self.assertIn("严格模式已关闭", last.get("reason") or "")

    def test_strict_mode_off_without_any_token_is_traced(self):
        """完全没有现场令牌（如历史调用方传 None）时也要留痕"""
        self.acc.set_strict_mode(False)
        rec = self.v.verify_1to1("C001", VEC_A, id_card="", ticket_no="2026T001",
                                 seat_no="A-01", capture_token=None)
        self.assertTrue(rec.strict_mode_off)
        self.assertIn("非现场", rec.reason)
        self.assertIn("严格模式已关闭", rec.reason)

    def test_strict_mode_off_does_not_bypass_liveness_failure(self):
        """活体模型显式判 FAIL 属硬失败：关闭严格模式也不能放行"""
        self.acc.set_strict_mode(False)
        liveness.set_checker(lambda image, token: liveness.LivenessResult(
            liveness.STATUS_FAIL, "自检：活体模型判失败"))
        rec = self.v.verify_1to1("C001", VEC_A, id_card="", ticket_no="2026T001",
                                 seat_no="A-01", capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_LIVENESS_FAILED)
        self.assertFalse(rec.strict_mode_off)

    def test_config_require_liveness_still_wins(self):
        """部署级 config.REQUIRE_LIVENESS=False 时，严格模式本身就不成立"""
        self.acc.set_strict_mode(True)
        v = FaceVerifier(self.candidates, self.index, DEVICE, self.logger,
                         operator="tester", enforce_consent=True, require_liveness=False)
        self.assertFalse(v.strict_mode_active())
        self.assertTrue(self.acc.strict_mode_enabled(),
                        "所有者开关本身仍是开启的——两种成因不能混为一谈")

    def test_deployment_switch_is_labelled_separately(self):
        """部署级开关导致的放宽，理由里要写"部署级开关"，不能记到所有者头上"""
        self.acc.set_strict_mode(True)
        v = FaceVerifier(self.candidates, self.index, DEVICE, self.logger,
                         operator="tester", enforce_consent=True, require_liveness=False)
        rec = v.verify_1to1("C001", VEC_A, id_card="", ticket_no="2026T001",
                            seat_no="A-01", capture_token=self.file_token)
        self.assertTrue(rec.strict_mode_off)
        self.assertIn("部署级开关", rec.strict_mode_off_reason)
        self.assertIn("部署级开关", rec.reason)

    # ---------------- 扫脸流程（GUI / 网页端使用） ----------------
    def test_face_first_without_elements_keeps_old_flow(self):
        """不传身份要素 -> 保持"只扫脸 + 人工核对证件"的原流程"""
        rec = self.v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertEqual(rec.elements_checked, "准考证号 + 座位号")

    def test_face_first_normal_mode_blocks_bad_ticket(self):
        rec = self.v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token,
                                       id_card="", ticket_no="WRONG", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_TICKET_MISMATCH)

    def test_face_first_major_mode_blocks_missing_id(self):
        self.acc.set_major_exam(True)
        rec = self.v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token,
                                       id_card="", ticket_no="2026T001", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)

    def test_face_first_major_mode_blocks_when_no_elements_supplied_at_all(self):
        """
        大考模式下调用方一个要素都不传也必须拦——否则不传参数就能静默绕过
        "必须使用身份证"（集成接口/无人值守通道正是这种调用方式）。
        """
        self.acc.set_major_exam(True)
        rec = self.v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_ID_MISMATCH)
        self.assertIn("大考模式", rec.reason)

    def test_face_first_normal_mode_without_elements_still_works(self):
        """常规模式下不传要素仍是老流程（只扫脸 + 人工核对证件）"""
        rec = self.v.verify_face_first("C001", live_images=[VEC_A], capture_token=self.token)
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)

    def test_face_first_relaxed_file_source_is_traced(self):
        self.acc.set_strict_mode(False)
        rec = self.v.verify_face_first("C001", live_images=[VEC_A],
                                       capture_token=self.file_token,
                                       id_card="", ticket_no="2026T001", seat_no="A-01")
        self.assertEqual(rec.result, RESULT_PASS, rec.reason)
        self.assertTrue(rec.strict_mode_off)
        self.assertIn("严格模式已关闭", rec.reason)

    def test_1toN_relaxed_file_source_is_traced(self):
        self.acc.set_strict_mode(False)
        rec = self.v.verify_1toN(VEC_B, capture_token=self.file_token)
        self.assertEqual(rec.candidate_id, "C002")
        self.assertTrue(rec.strict_mode_off)
        self.assertEqual(rec.non_live_source, "file")

    def test_1toN_strict_mode_still_rejects_file(self):
        rec = self.v.verify_1toN(VEC_B, capture_token=self.file_token)
        self.assertEqual(rec.result, RESULT_FAIL)
        self.assertEqual(rec.reason_code, RC_LIVENESS_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
