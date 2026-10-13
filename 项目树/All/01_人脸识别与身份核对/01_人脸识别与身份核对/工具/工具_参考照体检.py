# -*- coding: utf-8 -*-
r"""
01 · 参考照与识别能力体检

背景：核验"怎么都过不了"最常见的原因根本不是算法，而是数据——
参考照文件丢失/被改名、登记路径不存在、索引没建、照片里检不出人脸。
本工具把这些一次性查清楚，并给出可执行的修复建议。

用法（在本目录下执行）：
  python 工具_参考照体检.py                # 全量体检（推荐，302 人约 3~5 分钟）
  python 工具_参考照体检.py --limit 50     # 只查前 50 人（快速）
  python 工具_参考照体检.py --recognize    # 额外做 1:N 自匹配（验证识别链路是否真的可用）
  python 工具_参考照体检.py --repair       # 尝试修复：把"孤儿照片"认领给缺照片的考生

为什么需要 --repair：
  data/ 被 .gitignore 排除，参考照没有任何版本保护；且历史上多次出现
  "某考生的照片文件被改名/挪走"（例如 C001.jpg 变成了 C003.jpg），
  结果是该考生核验恒失败、而界面上看不出是数据问题。
  --repair 用索引里的特征去比对孤儿照片，确认是同一个人后复制回登记路径
  （复制不删除原件），并自动备份原文件。

体检项：
  1) 登记 vs 实际文件对账（缺文件 / 多文件 / 空登记）
  2) 索引覆盖（有照片但没索引、有索引但没照片）
  3) 逐张人脸检出（insightface 能否检到脸）与画质占比
  4) --recognize：用本人参考照做 1:N 检索，看能否命中自己
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np  # noqa: E402
import config  # noqa: E402
import face_engine  # noqa: E402
from store import CandidateStore  # noqa: E402
from face_index import FaceIndex  # noqa: E402


def repair_missing_photos(cds, idx, missing_file, orphan, photo_dir, min_score, fe):
    """把"孤儿照片"认领给缺照片的考生。

    判据：用考生在索引里的特征向量，与孤儿照片算出的人脸特征比相似度，
    超过 min_score 才认领。只复制、不移动原件，并给已存在的目标做 .bak 备份。
    """
    import shutil
    if not orphan:
        print("  没有孤儿照片可用于修复。")
        print("  请从别处找回照片，或换一张清晰正脸照后在界面点「为该考生建档」。")
        return
    if idx.size == 0:
        print("  索引为空，无法用特征比对认领。请先重建索引。")
        return

    # 预计算孤儿照片的特征
    orphan_embs = {}
    for name in orphan:
        p = None
        for ext in (".jpg", ".jpeg", ".png", ".bmp"):
            cand = os.path.join(photo_dir, name + ext)
            if os.path.exists(cand):
                p = cand
                break
        if p is None:
            continue
        try:
            img = fe.imread_unicode(p)
            if img is None:
                continue
            faces = fe.get_app().get(img)
            if not faces:
                continue
            f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
            orphan_embs[name] = (p, np.asarray(f.normed_embedding, dtype=np.float32))
        except Exception:
            continue

    if not orphan_embs:
        print("  孤儿照片里没有可用的人脸，无法认领。")
        return

    fixed, failed = 0, 0
    for cid, target in missing_file:
        ref = idx.embedding_of(cid)
        if ref is None:
            print("  %-8s 无索引特征，跳过" % cid)
            failed += 1
            continue
        ref = np.asarray(ref, dtype=np.float32)
        best = (None, -1.0)
        for name, (p, emb) in orphan_embs.items():
            s = float(np.dot(emb, ref) /
                      (np.linalg.norm(emb) * np.linalg.norm(ref) + 1e-9))
            if s > best[1]:
                best = (p, s)
        if best[0] is None or best[1] < min_score:
            print("  %-8s 没有足够像的孤儿照片（最高相似度 %.4f < %.2f），跳过"
                  % (cid, max(best[1], 0.0), min_score))
            failed += 1
            continue
        src, score = best
        try:
            if os.path.exists(target):
                bak = target + ".bak"
                if not os.path.exists(bak):
                    shutil.copy2(target, bak)
            shutil.copy2(src, target)
            print("  %-8s 已用 %s 修复（相似度 %.4f）-> %s"
                  % (cid, os.path.basename(src), score, target))
            fixed += 1
        except OSError as e:
            print("  %-8s 修复失败：%s" % (cid, e))
            failed += 1
    print()
    print("  修复成功 %d 人，未修复 %d 人" % (fixed, failed))
    if fixed:
        print("  提示：这些考生的索引特征本来就是按原文件建的，文件补回后即可正常核验；")
        print("        若修复后核验仍异常，请在界面点「为该考生建档」重建索引。")


def main():
    ap = argparse.ArgumentParser(description="01 参考照与识别能力体检")
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 人（0=全部）")
    ap.add_argument("--recognize", action="store_true",
                    help="额外做 1:N 自匹配（较慢，验证识别链路）")
    ap.add_argument("--sample", type=int, default=30, help="1:N 抽样人数")
    ap.add_argument("--repair", action="store_true",
                    help="尝试修复：把孤儿照片认领给缺照片的考生（复制，不删原件）")
    ap.add_argument("--repair-min-score", dest="repair_min", type=float, default=0.65,
                    help="认领孤儿照片所需的最低相似度（默认 0.65，与 1:N 阈值一致）")
    args = ap.parse_args()

    print("=" * 74)
    print("01 · 参考照与识别能力体检")
    print("=" * 74)

    cds = CandidateStore().load()
    idx = FaceIndex()
    loaded = idx.load()
    print("报名库: %d 人 ｜ 索引: %d 人 ｜ 索引加载=%s" % (len(cds), idx.size, loaded))
    print("1:1 阈值=%s ｜ 1:N 阈值=%s"
          % (getattr(config, "THRESHOLD_1TO1", "?"),
             getattr(config, "THRESHOLD_1TON", "?")))
    print("照片目录: %s" % os.path.join("data", "ref_photos"))
    print()

    ids = sorted(cds)
    if args.limit:
        ids = ids[:args.limit]

    # ---------- 1) 登记 vs 文件对账 ----------
    print("-" * 74)
    print("[1] 登记 vs 实际文件对账")
    print("-" * 74)
    missing_file, empty_reg = [], []
    for cid in ids:
        raw = (cds[cid].reference_photo or "").strip()
        if not raw:
            empty_reg.append(cid)
            continue
        p = str(config.resolve_photo(raw))
        if not os.path.exists(p):
            missing_file.append((cid, p))
    print("  登记了但文件不存在 : %d 人" % len(missing_file))
    for cid, p in missing_file[:20]:
        print("      %-8s -> %s" % (cid, p))
    if len(missing_file) > 20:
        print("      ...（共 %d 人）" % len(missing_file))
    print("  完全未登记参考照   : %d 人 %s" % (len(empty_reg), empty_reg[:15]))

    photo_dir = os.path.join("data", "ref_photos")
    files = set()
    if os.path.isdir(photo_dir):
        files = set(os.path.splitext(f)[0] for f in os.listdir(photo_dir)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")))
    registered = set(cds)
    orphan = sorted(files - registered)
    print("  ref_photos 文件数  : %d" % len(files))
    print("  有文件但不在报名库 : %d %s" % (len(orphan), orphan[:15]))

    # ---------- 1.5) 可选：修复缺失的参考照 ----------
    if args.repair and missing_file:
        print()
        print("-" * 74)
        print("[1.5] 尝试修复缺失的参考照（用索引特征认领孤儿照片）")
        print("-" * 74)
        repair_missing_photos(cds, idx, missing_file, orphan, photo_dir,
                              args.repair_min, face_engine)

    # ---------- 2) 索引覆盖 ----------
    print()
    print("-" * 74)
    print("[2] 索引覆盖")
    print("-" * 74)
    no_index = [c for c in ids if not idx.has(c)]
    print("  有考生但索引缺失   : %d 人 %s" % (len(no_index), no_index[:15]))
    if idx.size == 0:
        print("  !! 索引为空：核验不可能通过。请点「为该考生建档」重建索引。")

    # ---------- 3) 逐张人脸检出 ----------
    print()
    print("-" * 74)
    print("[3] 逐张人脸检出（insightface 检测）")
    print("-" * 74)
    app = face_engine.get_app()
    ok, noface, err = 0, [], []
    ratios, t0 = [], time.time()
    for i, cid in enumerate(ids):
        raw = (cds[cid].reference_photo or "").strip()
        if not raw:
            continue
        p = str(config.resolve_photo(raw))
        if not os.path.exists(p):
            continue
        try:
            img = face_engine.imread_unicode(p)
            if img is None:
                err.append((cid, "读图失败"))
                continue
            h, w = img.shape[:2]
            faces = app.get(img)
            if not faces:
                noface.append(cid)
                continue
            f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
            fw = float(f.bbox[2] - f.bbox[0])
            fh = float(f.bbox[3] - f.bbox[1])
            ratios.append((fw * fh) / float(w * h))
            ok += 1
        except Exception as e:
            err.append((cid, "%s: %s" % (type(e).__name__, str(e)[:50])))
        if (i + 1) % 50 == 0:
            print("      ...已检 %d 人" % (i + 1))
    dt = time.time() - t0
    print("  检出人脸           : %d 人" % ok)
    print("  未检出人脸         : %d 人 %s" % (len(noface), noface[:15]))
    print("  读取异常           : %d 人 %s" % (len(err), err[:5]))
    print("  耗时               : %.1fs（%.2fs/人）" % (dt, dt / max(1, ok + len(noface))))
    if ratios:
        r = np.array(ratios)
        small = int((r < 0.06).sum())
        print("  人脸画面占比       : 中位 %.3f ｜ 最小 %.3f" % (np.median(r), r.min()))
        print("  低于 0.06 画质阈值的: %d 人%s" % (small, "（会报'人脸过小'转人工）" if small else ""))
    if noface:
        print("  提示：未检出人脸的参考照应更换为清晰正脸照后重建索引。")

    # ---------- 4) 1:N 自匹配 ----------
    if args.recognize:
        print()
        print("-" * 74)
        print("[4] 1:N 自匹配（用本人参考照检索，应命中本人）")
        print("-" * 74)
        pool = [c for c in ids if idx.has(c) and c not in set(noface)
                and (cds[c].reference_photo or "").strip()
                and os.path.exists(str(config.resolve_photo(cds[c].reference_photo)))]
        if not pool:
            print("  没有可用于测试的考生。")
        else:
            sample = pool[::max(1, len(pool) // args.sample)][:args.sample]
            hit1 = hit5 = 0
            scores, wrong = [], []
            t0 = time.time()
            for cid in sample:
                img = face_engine.imread_unicode(
                    str(config.resolve_photo(cds[cid].reference_photo)))
                res = idx.identify(img, topk=5)
                if not res:
                    continue
                top = [r[0] for r in res]
                if top[0] == cid:
                    hit1 += 1
                    scores.append(float(res[0][1]))
                else:
                    wrong.append((cid, [(r[0], round(float(r[1]), 3)) for r in res[:3]]))
                if cid in top[:5]:
                    hit5 += 1
            n = len(sample)
            print("  Top-1 命中本人     : %d/%d = %.1f%%" % (hit1, n, 100.0 * hit1 / max(1, n)))
            print("  Top-5 命中本人     : %d/%d = %.1f%%" % (hit5, n, 100.0 * hit5 / max(1, n)))
            print("  平均耗时           : %.0f ms/次" % (1000 * (time.time() - t0) / max(1, n)))
            if scores:
                a = np.array(scores)
                print("  本人相似度         : 最低 %.4f ｜ 中位 %.4f"
                      % (a.min(), np.median(a)))
            for cid, top in wrong[:10]:
                print("      Top-1 未命中: %s -> %s" % (cid, top))

    # ---------- 结论 ----------
    print()
    print("=" * 74)
    print("结论与建议")
    print("=" * 74)
    problems = []
    if missing_file:
        problems.append("有 %d 人的参考照文件不存在（核验必然失败）→ 找回照片或换照后重建索引"
                        % len(missing_file))
    if empty_reg:
        problems.append("有 %d 人未登记参考照 → 在台账里补登记" % len(empty_reg))
    if len(no_index):
        problems.append("有 %d 人没有索引 → 点「为该考生建档」" % len(no_index))
    if noface:
        problems.append("有 %d 人的参考照检不出人脸 → 换清晰正脸照后重建索引" % len(noface))
    if orphan:
        problems.append("有 %d 个照片文件不在报名库（多为改名/遗留）→ 确认是否需要还原命名"
                        % len(orphan))
    if ratios and int((np.array(ratios) < 0.06).sum()):
        problems.append("有照片人脸过小 → 会报「人脸过小」转人工，建议换近照")
    if not problems:
        print("  未发现数据问题：参考照、索引、人脸检出均正常。")
        print("  若核验仍不通过，请检查：① 严格模式是否拦掉了选图片；")
        print("  ② 考生的「单独同意」是否已登记；③ 准考证号/座位号是否与报名库一致。")
    else:
        for i, s in enumerate(problems, 1):
            print("  %d) %s" % (i, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
