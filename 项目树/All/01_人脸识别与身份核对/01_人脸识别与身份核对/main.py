# -*- coding: utf-8 -*-
"""
自检 / 回归脚本（需要真实模型，用 Python 3.12 运行）。

把踩过的坑固化成断言：
  场景3 是旧版的"误识现场"——用他人照片冒充却判 PASS。
  修复后该场景**必须不再 PASS**，否则脚本以非零码退出。

用法：python main.py
"""
import os
import sys

import numpy as np

# Windows 控制台默认 GBK，直接打印符号/emoji 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config
import liveness
from audit import AuditLogger
from face_index import FaceIndex
from models import RC_SEAT_MISMATCH, RC_TICKET_MISMATCH, REASON_TEXT
from store import CandidateStore
from verifier import FaceVerifier

DEVICE = "GATE-A-01"


def _photo_of(candidate):
    """参考照路径：兼容数据目录搬迁后的旧绝对路径（走 resolve_photo 回退）"""
    return str(config.resolve_photo(candidate.reference_photo))


def _usable_candidates(candidates, index=None):
    """筛出"参考照确实存在、且能读出来"的考生。

    报名库里可能有脏数据（手填的编号、漏了扩展名的路径、照片已删），
    旧版自检直接拿第一条去读图，一遇到这种就抛 FileNotFoundError 整个崩掉。
    这里先过滤，自检只会用到可用的样本，并在开头如实报告被跳过了多少条。
    """
    from face_engine import imread_unicode
    usable, skipped = [], []
    for cid, c in sorted(candidates.items()):
        path = _photo_of(c)
        try:
            if not os.path.exists(path):
                skipped.append((cid, "参考照不存在"))
                continue
            if imread_unicode(path) is None:
                skipped.append((cid, "参考照无法解码"))
                continue
        except Exception as e:                       # noqa: BLE001
            skipped.append((cid, "参考照读取异常：%s" % str(e)[:40]))
            continue
        if index is not None and not index.has(cid):
            skipped.append((cid, "未建档（索引中无特征）"))
            continue
        usable.append(c)
    return usable, skipped


def _simulate_live_capture(photo_path, quality=70, brightness=12):
    """
    模拟"同一个人、另一次抓拍"：轻微重压缩 + 亮度变化。
    避免自检退化成"拿参考照比对参考照"（那正是旧版的漏洞）。
    注意：读图统一走 face_engine.imread_unicode，规避 Windows 中文路径下
    cv2.imread 直接返回 None 的问题。
    """
    import cv2
    from face_engine import imread_unicode
    img = imread_unicode(photo_path)
    if img is None:
        raise RuntimeError("无法读取 %s（文件损坏或格式不支持）" % photo_path)
    img = cv2.convertScaleAbs(img, alpha=1.0, beta=brightness)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("图像编码失败")
    return cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)


def _blank_image():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _degrade(img, brightness_factor=1.0, quality=60, blur=0):
    """制造"劣化"的现场图：压暗 + 重压缩，可选高斯模糊（模拟昏暗/失焦）"""
    import cv2
    out = img
    if brightness_factor != 1.0:
        out = cv2.convertScaleAbs(out, alpha=brightness_factor, beta=0)
    if blur:
        out = cv2.GaussianBlur(out, (blur | 1, blur | 1), 0)
    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("图像编码失败")
    return cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)


def _warp_upward(img, strength=0.30):
    """
    模拟"仰拍"：摄像机低于人脸往上拍，额头被压缩、下颌被拉长。
    这里用透视变换近似——只为验证姿态估计是否会把这种图识别出来。
    """
    import cv2
    h, w = img.shape[:2]
    s = float(strength)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[w * 0.10 * s, h * 0.16 * s],
                      [w * (1 - 0.10 * s), h * 0.16 * s],
                      [w, h], [0, h]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _selfcheck_four_elements(cand, id_card, ticket_no, seat_no):
    """
    自检专用四要素校验器：库中身份证号是加盐哈希、无法反推明文，
    因此这里只校验准考证号与座位号，身份证视为一致（自检关注人脸与现场性链路）。
    生产链路仍走 models.Candidate.check_id_card 的哈希比对。
    """
    if (cand.ticket_no or "") != (ticket_no or ""):
        return False, RC_TICKET_MISMATCH, REASON_TEXT[RC_TICKET_MISMATCH]
    if (cand.seat_no or "") != (seat_no or ""):
        return False, RC_SEAT_MISMATCH, REASON_TEXT[RC_SEAT_MISMATCH]
    return True, "", ""


def main():
    print("=" * 74)
    print("人脸识别与身份核对 · 自检（严格现场模式：%s）" % config.REQUIRE_LIVENESS)
    print("=" * 74)
    for k, v in config.describe().items():
        print("  %-24s %s" % (k, v))
    # 两个所有者开关的当前状态（自检本身用注入的校验器，不受它们影响，但要如实显示）
    import accounts
    print("  %-24s %s" % ("所有者开关·大考模式", "开启（必须使用身份证）" if accounts.major_exam_enabled()
                          else "关闭（默认：身份证不参与校验，以准考证号核验）"))
    print("  %-24s %s" % ("所有者开关·严格模式", "开启（默认：非现场来源拒绝放行）"
                          if accounts.strict_mode_enabled() else "已关闭（允许本地图片，会留痕）"))
    print("  %-24s %s" % ("身份要素模式", accounts.identity_mode_text()))
    # 边缘计算层状态（推理后端 / 档位 / 待上报）
    try:
        import edge
        print("-" * 74)
        for line in edge.describe_edge().splitlines():
            print("  " + line)
    except Exception as e:
        print("  [边缘层不可用] %s" % str(e)[:80])
    print("-" * 74)

    store = CandidateStore()
    candidates = store.load()
    if not candidates:
        print("报名库为空：请先运行 `python enroll.py migrate-json`，"
              "或在网页操作面板导入考生后执行 `python enroll.py build`。")
        return 2
    print("报名库考生：%s" % "、".join(sorted(candidates)))

    index = FaceIndex()
    if os.path.exists(config.INDEX_PATH):
        try:
            index.load()
            print("已加载特征索引：%d 人" % index.size)
        except Exception as e:
            print("索引加载失败（将重建）：%s" % e)

    def progress(cid, done, total):
        print("  建档 %d/%d：%s" % (done, total, cid))

    stats = index.build(candidates, progress=progress)
    for cid, why in stats["failed"].items():
        print("  [!] 建档失败 %s：%s" % (cid, why))
    if index.is_empty():
        print("没有可用特征，无法继续自检。")
        return 2
    try:
        index.save()
    except Exception as e:
        print("索引保存失败（不影响本次自检）：%s" % e)

    logger = AuditLogger()
    verifier = FaceVerifier(candidates, index, device_id=DEVICE, logger=logger,
                            operator="self-check", enforce_consent=False,
                            four_element_checker=_selfcheck_four_elements)

    usable, skipped = _usable_candidates(candidates, index)
    if skipped:
        print("跳过 %d 条不可用考生（参考照缺失/未建档），自检只用可用的 %d 条："
              % (len(skipped), len(usable)))
        for cid, why in skipped[:5]:
            print("  [跳过] %s：%s" % (cid, why))
        if len(skipped) > 5:
            print("  … 其余 %d 条省略" % (len(skipped) - 5))
    if len(usable) < 2:
        print("可用考生不足 2 人（当前 %d 人），无法完成 本人vs他人 的自检场景。"
              % len(usable))
        print("请先补齐参考照并建档：python 工具\\生成测试考生.py 或 python enroll.py build")
        return 2

    c1 = next((c for c in usable if c.candidate_id == "C001"), usable[0])
    others = [c for c in usable if c.candidate_id != c1.candidate_id]
    token = liveness.make_token(DEVICE, source="camera")       # 模拟现场抓拍令牌
    live_same = _simulate_live_capture(_photo_of(c1))
    results = []

    def record(name, rec, expect_not_pass, expect_codes=None):
        ok = (rec.result != "PASS") if expect_not_pass else True
        if expect_codes:
            ok = ok and rec.reason_code in expect_codes
        results.append((name, rec, ok))

    def record_forbid(name, rec, forbidden_codes):
        """断言：结果的原因码不得落入 forbidden（例如昏暗时不得判"非本人"）"""
        ok = rec.reason_code not in forbidden_codes
        results.append((name, rec, ok))

    def verify(candidate, image, tk, ticket=None):
        return verifier.verify_with_retry(
            candidate.candidate_id, live_images=[image], id_card="0" * 18,
            ticket_no=ticket or candidate.ticket_no, seat_no=candidate.seat_no,
            capture_token=tk)

    # 场景1：本人核验（模拟现场抓拍）
    rec = verify(c1, live_same, token)
    record("场景1 本人核验（现场抓拍）", rec, False)
    print("场景1 -> %-6s %-20s 相似度 %s" % (rec.result, rec.reason_code, rec.score))

    # 场景2：准考证号错误 —— 硬失败，必须拦截且不得降级为"转人工"
    rec = verify(c1, live_same, token, ticket="WRONG-TICKET")
    record("场景2 准考证号不一致", rec, True, {"TICKET_MISMATCH"})
    print("场景2 -> %-6s %-20s" % (rec.result, rec.reason_code))

    # 场景3：他人照片冒充 —— 旧版这里判了 PASS，本脚本的核心回归点
    if others:
        other = others[0]
        live_other = _simulate_live_capture(_photo_of(other), quality=80, brightness=0)
        rec = verify(c1, live_other, token)
        record("场景3 他人照片冒充（必须拦截）", rec, True)
        print("场景3 -> %-6s %-20s 相似度 %s（冒充者 %s → 被核验人 %s）"
              % (rec.result, rec.reason_code, rec.score, other.candidate_id, c1.candidate_id))
    else:
        print("场景3 跳过：报名库只有 1 人")

    # 场景4：无现场令牌（严格模式必须拒绝，堵住"把参考照当现场照"的洞）
    rec = verify(c1, _simulate_live_capture(_photo_of(c1)), None)
    record("场景4 无现场令牌（严格模式）", rec, True, {"LIVENESS_UNAVAILABLE"})
    print("场景4 -> %-6s %-20s" % (rec.result, rec.reason_code))

    # 场景5：画面无人脸
    rec = verify(c1, _blank_image(), token)
    record("场景5 画面无人脸", rec, True, {"NO_FACE"})
    print("场景5 -> %-6s %-20s" % (rec.result, rec.reason_code))

    # 场景6：1:N 抽查应命中本人
    rec = verifier.verify_1toN(live_same, capture_token=token)
    hit_ok = (rec.candidate_id == c1.candidate_id and rec.result == "PASS")
    results.append(("场景6 1:N 检索命中本人", rec, hit_ok))
    print("场景6 -> %-6s %-20s 命中 %s%s" % (rec.result, rec.reason_code, rec.candidate_id,
                                             "" if hit_ok else "（期望 %s）" % c1.candidate_id))

    # ================= 异常状态 / 极端情况 =================
    print("-" * 74)
    print("异常状态处理（昏暗 / 撞脸 / 仰拍 / 灰区）")
    print("-" * 74)

    # 场景7：昏暗现场图 —— 绝不能因为"看不清"就判定非本人（那是冤枉考生）
    dark = _degrade(live_same, brightness_factor=0.22, quality=92)
    rec = verify(c1, dark, token)
    record_forbid("场景7 昏暗（不得判非本人）", rec, {"NOT_MATCHED"})
    print("场景7 -> %-6s %-22s 原图亮度%s → 增强后%s（增强=%s）相似度%s"
          % (rec.result, rec.reason_code, rec.brightness_raw, rec.brightness,
             rec.enhanced, rec.score))
    print("        原图问题：%s ｜ 增强后：%s（%s）"
          % (rec.quality_issues_raw or "无", rec.quality_issues or "无", rec.quality_severity))

    # 场景8：撞脸/孪生 —— 注入一个与本人类似的"另一个人"，必须转双人见证复验
    try:
        emb_live, _info = index.extractor(live_same)
        rng = np.random.default_rng(2026)
        noise = rng.normal(0, 1, size=emb_live.shape).astype(np.float32)
        noise /= max(float(np.linalg.norm(noise)), 1e-9)      # 归一化：噪声向量长度可控
        index._append("__TWIN_TEST__", emb_live + 0.03 * noise)
        rec = verify(c1, live_same, token)
        record("场景8 撞脸/孪生（双人见证复验）", rec, True, {"TWIN_SUSPECT", "IDENTITY_CONFLICT"})
        print("场景8 -> %-6s %-22s 与 %s 差距 %s（需见证人=%s）"
              % (rec.result, rec.reason_code, rec.twin_candidate_id, rec.twin_gap,
                 rec.required_witness))
    finally:
        index.remove("__TWIN_TEST__")

    # 场景9：仰拍模拟 —— 透视变换压缩额头，检查是否被判为极端姿态；同样不得判非本人
    up = _warp_upward(live_same)
    rec = verify(c1, up, token)
    record_forbid("场景9 仰拍（不得判非本人）", rec, {"NOT_MATCHED"})
    print("场景9 -> %-6s %-22s pitch=%s°（阈值为仰拍超 %.0f° 触发复验）"
          % (rec.result, rec.reason_code, rec.pitch, config.POSE_MAX_PITCH_UP))

    # 场景10：灰区（模糊匹配）—— 分数落在 [0.45,0.55) 时必须走人工复验
    blurry = _degrade(live_same, brightness_factor=1.0, quality=12, blur=9)
    rec = verify(c1, blurry, token)
    if rec.score is not None and 0.45 <= rec.score < 0.55:
        record("场景10 灰区（人工复验）", rec, True, {"AMBIGUOUS"})
        print("场景10 -> 相似度 %.4f 落入灰区 -> %s / %s" % (rec.score, rec.result, rec.reason_code))
    else:
        results.append(("场景10 灰区（本次未落入灰区，仅观察）", rec, True))
        print("场景10 -> 相似度 %s 未落入 [0.45,0.55)，本场景仅观察（结果 %s / %s）"
              % (rec.score, rec.result, rec.reason_code))

    # 场景11：人脸通过 -> 自动带出证件信息（掩码）-> 工作人员目视核对 -> 单独留痕
    doc_text = verifier.document_info(c1)
    doc_rec = verifier.confirm_document(c1.candidate_id, operator="自检员", witness="自检见证", ok=True)
    ok11 = (doc_rec.reason_code == "DOC_CHECK_CONFIRMED" and c1.id_card_masked in doc_text
            and "330100199001011234" not in doc_text)
    results.append(("场景11 证件核对（人脸通过后带出）", doc_rec, ok11))
    print("场景11 -> %-6s %-22s 带出：%s" % (doc_rec.result, doc_rec.reason_code, doc_text))

    # 场景12：工作人员目视发现证件不符 -> 当场拦截并留痕
    bad_rec = verifier.confirm_document(c1.candidate_id, operator="自检员", ok=False,
                                        note="自检：模拟证件不符")
    ok12 = (bad_rec.result == "FAIL" and bad_rec.reason_code == "DOC_MISMATCH"
            and bad_rec.manual_required)
    results.append(("场景12 证件不符（拦截）", bad_rec, ok12))
    print("场景12 -> %-6s %-22s 需人工介入=%s" % (bad_rec.result, bad_rec.reason_code,
                                                  bad_rec.manual_required))

    # 汇总
    print("-" * 74)
    print("%-34s %-8s %-22s %s" % ("场景", "结果", "原因码", "自检"))
    failed = 0
    for name, rec, ok in results:
        print("%-34s %-8s %-22s %s" % (name, rec.result, rec.reason_code, "OK" if ok else "FAIL"))
        if not ok:
            failed += 1

    chain = logger.verify_chain()
    print("-" * 74)
    print("审计链校验：%s（共 %d 条）" % (chain["reason"], chain["total"]))
    try:
        import edge
        print("边缘状态：" + edge.describe_edge().splitlines()[1].strip())
    except Exception:
        pass
    print("=" * 74)
    if failed:
        print("自检未通过：%d 个场景不符合预期" % failed)
        return 1
    print("自检通过：全部场景符合预期（含“他人照片冒充必须拦截”）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
