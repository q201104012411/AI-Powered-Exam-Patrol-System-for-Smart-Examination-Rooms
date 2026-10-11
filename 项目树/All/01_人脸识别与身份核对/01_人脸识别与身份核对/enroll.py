# -*- coding: utf-8 -*-
"""
建档与运维命令行工具。

用法：
    python enroll.py build [--force] [--purge-photos] [--yes]   # 提取特征并（可选）删除原图
    python enroll.py status                                     # 查看索引与合规状态
    python enroll.py check-paths [--suggest]                    # 运行路径体检
    python enroll.py check-photos [--faces]                     # 参考照体检
    python enroll.py migrate-json                               # 旧 JSON 报名库迁移到 SQLite
    python enroll.py purge-expired [--days 30] [--dry-run]      # 按保留期限清理核验记录
    python enroll.py purge-templates [--days 30] [--yes]        # 清理到期考生的特征与照片
    python enroll.py check-chain                                # 校验审计日志哈希链
    python enroll.py clock                                       # 时钟偏差自检
"""
import argparse
import os
import sys

try:                                    # Windows 控制台默认 GBK，避免中文输出报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config
import secure_store
from audit import AuditLogger
from face_index import FaceIndex
from store import CandidateStore, migrate_legacy_json


def cmd_build(args):
    store = CandidateStore()
    candidates = store.load()
    if not candidates:
        print("报名库为空。可先用 `python enroll.py migrate-json` 导入旧数据，"
              "或在网页操作面板中导入考生。")
        return 2

    index = FaceIndex()
    loaded = False
    if not args.force and os.path.exists(config.INDEX_PATH):
        try:
            loaded = index.load()
            print("已加载现有索引：%d 人（加密后端 %s）" % (index.size, secure_store.backend_name()))
        except Exception as e:
            print("索引加载失败，将重建：%s" % e)
            index = FaceIndex()

    def progress(cid, done, total):
        print("  建档 %d/%d：%s" % (done, total, cid))

    stats = index.build(candidates, force=args.force, progress=progress)
    print("建档完成：新增 %d，跳过 %d，失败 %d" % (stats["built"], stats["skipped"], len(stats["failed"])))
    for cid, why in stats["failed"].items():
        print("  [!] %s：%s" % (cid, why))

    if index.size:
        path = index.save()
        print("索引已加密保存：%s（%.1f KB，后端 %s）"
              % (path, os.path.getsize(path) / 1024.0, secure_store.backend_name()))

    if args.purge_photos:
        if not args.yes:
            ans = input("即将删除已建档考生的原始参考照（合规要求：不保留原始人脸图像）。确认？[y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("已取消删除原图。")
                return 0
        removed, failed = 0, []
        for cid, c in candidates.items():
            if not index.has(cid) or not c.reference_photo:
                continue
            p = c.reference_photo
            try:
                if os.path.exists(p):
                    os.remove(p)
                removed += 1
            except OSError as e:
                failed.append("%s: %s" % (p, e))
        store.log_change("PURGE_PHOTOS", detail="删除原始参考照 %d 张" % removed, operator="enroll")
        print("已删除原始参考照 %d 张（失败 %d）" % (removed, len(failed)))
        for f in failed:
            print("  [!]", f)
    return 0


def cmd_status(args):
    store = CandidateStore()
    candidates = store.load()
    index = FaceIndex()
    has_index = False
    if os.path.exists(config.INDEX_PATH):
        try:
            has_index = index.load()
        except Exception as e:
            print("索引读取失败：%s" % e)
    print("=" * 56)
    for k, v in config.describe().items():
        print("  %-22s %s" % (k, v))
    print("=" * 56)
    print("  报名库考生数              %d" % len(candidates))
    print("  特征索引                 %s" % ("已加载 %d 人" % index.size if has_index else "不存在"))
    missing = [cid for cid in candidates if not index.has(cid)]
    print("  尚未建档                 %d %s" % (len(missing), ("（如 " + "、".join(missing[:5]) + "）") if missing else ""))
    no_consent = [c.candidate_id for c in candidates.values() if not c.consent_ok()]
    print("  未登记单独同意           %d %s" % (len(no_consent), ("（如 " + "、".join(no_consent[:5]) + "）") if no_consent else ""))
    with_photo = [c.candidate_id for c in candidates.values() if c.reference_photo and os.path.exists(c.reference_photo)]
    print("  仍保留原始参考照         %d（合规上建议执行 enroll.py build --purge-photos）" % len(with_photo))
    logger = AuditLogger()
    print("  审计链校验               %s" % logger.verify_chain()["reason"])
    return 0


def cmd_migrate_json(args):
    n = migrate_legacy_json()
    print("已从旧 JSON 报名库迁移 %d 条记录到 SQLite（身份证号已转为加盐哈希 + 掩码）" % n)
    if n:
        print("建议确认无误后删除 data/candidates.json，避免明文残留。")
    return 0


def cmd_purge_expired(args):
    logger = AuditLogger()
    r = logger.purge_expired(days=args.days, dry_run=args.dry_run)
    print("审计记录清理：扫描 %d，删除 %d，保留 %d，损坏行 %d%s"
          % (r["scanned"], r["removed"], r["kept"], r.get("corrupted", 0),
             "（演练模式，未实际删除）" if args.dry_run else ""))
    if r.get("backup"):
        print("原日志已备份：%s" % r["backup"])
    return 0


def cmd_purge_templates(args):
    """按保留期限清理到期考生的特征与照片（生物特征最小必要）"""
    import datetime as _dt
    store = CandidateStore()
    candidates = store.load()
    today = _dt.date.today().isoformat()
    expired = [c for c in candidates.values() if c.retention_until and c.retention_until < today]
    if not expired:
        print("没有到期需要清理的考生特征。")
        return 0
    print("到期考生 %d 人：" % len(expired))
    for c in expired:
        print("  %s（保留至 %s）" % (c.candidate_id, c.retention_until))
    if not args.yes:
        ans = input("确认删除这些考生的特征与照片？[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0
    index = FaceIndex()
    if os.path.exists(config.INDEX_PATH):
        index.load()
        for c in expired:
            index.remove(c.candidate_id)
            if c.reference_photo and os.path.exists(c.reference_photo):
                try:
                    os.remove(c.reference_photo)
                except OSError:
                    pass
        index.save()
    store.log_change("PURGE_TEMPLATES", detail="清理到期特征 %d 人" % len(expired), operator="enroll")
    print("已清理 %d 人的特征模板。" % len(expired))
    return 0


def cmd_check_chain(args):
    r = AuditLogger().verify_chain()
    print("审计链校验：%s（共 %d 条，断点 %s）" % (r["reason"], r["total"], r["broken_at"]))
    return 0 if r["ok"] else 1


def cmd_migrate_data(args):
    """把旧布局的运行数据收拢到当前数据根目录（切换 EXAM_HOME / exam_home.txt 后执行一次）"""
    r = config.migrate_legacy_layout(dry_run=args.dry_run)
    print("数据根目录：%s" % r["数据根目录"])
    if args.dry_run:
        print("演练模式，计划迁移 %d 项：" % len(r["计划"]))
        for x in r["计划"]:
            print("  %s" % x)
        return 0
    print("已迁移 %d 项" % r["moved"])
    for x in r["明细"]:
        print("  %s" % x)
    for x in r["失败"]:
        print("  [!] 失败：%s" % x)
    if r.get("路径改写"):
        print("已改写报名库中 %d 条参考照路径" % r["路径改写"])
    for x in r.get("路径失效", []):
        print("  [!] 参考照仍找不到：%s" % x)
    print("建议随后执行：python enroll.py status && python main.py")
    return 0 if not r["失败"] else 1


def cmd_check_paths(args):
    """运行路径体检：中文/空格/超长路径是非 ASCII 环境最常见的事故源"""
    import pathcheck
    r = pathcheck.print_report()
    if getattr(args, "suggest", False):
        s = pathcheck.ascii_root_suggestion()
        print("\n短路径方案（可选）：")
        print("  推荐根目录：%s" % s["推荐根目录"])
        for k, v in s["迁移后结构"].items():
            print("    %-12s %s" % (k, v))
        print("  迁移要点：")
        for x in s["迁移要点"]:
            print("    - %s" % x)
    return 0            # 路径问题只提示，不阻断流程


def cmd_check_photos(args):
    """参考照体检：损坏/无人脸/多人的照片会在建档时失败，提前发现更省事"""
    from face_engine import imread_unicode
    store = CandidateStore()
    candidates = store.load()
    if not candidates:
        print("报名库为空。")
        return 2
    bad, ok = [], 0
    for cid, c in sorted(candidates.items()):
        p = c.reference_photo
        if not p:
            bad.append((cid, "未登记参考照"))
            continue
        path = str(config.resolve_photo(p))
        if not os.path.exists(path):
            bad.append((cid, "文件不存在：%s" % path))
            continue
        img = imread_unicode(path)
        if img is None:
            bad.append((cid, "无法解码（文件可能损坏）"))
            continue
        h, w = img.shape[:2]
        if min(h, w) < 100:
            bad.append((cid, "分辨率过低：%dx%d" % (w, h)))
            continue

        note = ""
        if args.faces:
            try:
                from face_engine import detect
                faces = detect(img)
                if not faces:
                    bad.append((cid, "未检测到人脸"))
                    continue
                if len(faces) > 1:
                    bad.append((cid, "检测到 %d 张人脸（建档取最大脸，建议换单人照）" % len(faces)))
                    continue
                note = "，人脸 %.0f×%.0f" % (faces[0].bbox[2] - faces[0].bbox[0],
                                             faces[0].bbox[3] - faces[0].bbox[1])
            except Exception as e:
                bad.append((cid, "人脸检测失败：%s" % str(e)[:60]))
                continue

        print("  [OK] %s  %dx%d%s" % (cid, w, h, note))
        # JPEG 完整性：没有 EOI 标记说明文件被截断（本项目的 C001.jpg 就是这种情况）
        if path.lower().endswith((".jpg", ".jpeg")):
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, os.path.getsize(path) - 2))
                    if f.read(2) != b"\xff\xd9":
                        print("       [!] 缺少 JPEG 结束标记（文件疑似被截断）")
            except OSError:
                pass
        ok += 1

    print("-" * 56)
    print("参考照体检：可用 %d，异常 %d" % (ok, len(bad)))
    for cid, why in bad:
        print("  [!] %s：%s" % (cid, why))
    return 0 if not bad else 1


def cmd_edge_status(args):
    """边缘计算层状态：推理后端、档位、实测延迟、待上报队列"""
    import edge
    print("=" * 60)
    print(edge.describe_edge())
    print("=" * 60)
    print("档位说明：full=全量增强/640px；balanced=480px且限帧；lite=320px且关闭增强（过载时自动降档）")
    return 0


def cmd_flush_outbox(args):
    """把边缘待上报的核验记录推送到云端（断网期间缓存的那些）"""
    import edge
    ob = edge.Outbox()
    print("待上报：%d 条" % ob.pending())
    r = ob.flush(endpoint=args.url or None)
    print("已上报 %d 条，剩余 %d 条%s"
          % (r["sent"], r["left"], ("（%s）" % r["error"]) if r["error"] else ""))
    if r["left"] and not (args.url or ob.endpoint):
        print("提示：用 --url 指定云端接收地址，或设置环境变量 EDGE_UPLOAD_URL")
    return 0 if not r["error"] else 1


PLAIN_EXT = (".log", ".json", ".jsonl", ".txt", ".csv", ".bak", ".v1bak", ".md")


def scan_plaintext(roots):
    """
    扫描文本类文件中的明文身份证号（15/18 位）。
    返回 [{"path","hits":[...],"lines":n}]
    """
    import re
    pat = re.compile(r"(?<!\d)(\d{17}[\dXx]|\d{15})(?!\d)")
    found = []
    seen = set()                       # 扫描根目录会重叠，按真实路径去重
    for root in roots:
        if not root or not os.path.exists(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() not in PLAIN_EXT:
                    continue
                p = os.path.join(dirpath, name)
                if name.endswith(".premask.bak"):
                    continue          # 脱敏前留的备份，属于有意保留
                real = os.path.realpath(p)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                hits = pat.findall(text)
                if hits:
                    found.append({"path": p, "hits": sorted(set(hits)),
                                  "count": len(hits)})
    return found


def cmd_scrub_plaintext(args):
    """
    清除历史遗留的明文身份证号（日志 / 备份 JSON / 导出文件等）。

    背景：旧版把身份证号明文写在 candidates.json，且日志里会带出以身份证号命名的照片路径。
    新版已改为哈希存储 + 写日志前脱敏，但历史文件里的明文仍在磁盘上。
    """
    from pii import sanitize_for_log
    roots = [str(config.DATA_ROOT), os.path.join(str(config.BASE_DIR), "备份"),
             str(config.BASE_DIR)]
    found = scan_plaintext(roots)
    print("=" * 66)
    print("明文身份证号扫描（数据目录 / 备份目录 / 工程目录）")
    print("=" * 66)
    if not found:
        print("[OK] 未发现明文身份证号。")
        return 0
    total = 0
    for item in found:
        total += item["count"]
        print("[!] %s" % os.path.relpath(item["path"], str(config.BASE_DIR)))
        print("    命中 %d 处：%s" % (item["count"], "、".join(item["hits"])[:120]))
    print("-" * 66)
    print("合计 %d 个文件、%d 处明文。" % (len(found), total))

    if not args.apply:
        print("当前为**预演**模式，未修改任何文件。确认无误后加 --apply 执行脱敏。")
        return 0

    changed = 0
    for item in found:
        p = item["path"]
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            new = sanitize_for_log(text)
            if new == text:
                print("[跳过] %s（脱敏后无变化）" % os.path.relpath(p, str(config.BASE_DIR)))
                continue
            if not args.no_backup:
                bak = p + ".premask.bak"
                if not os.path.exists(bak):
                    with open(bak, "w", encoding="utf-8") as f:
                        f.write(text)
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            changed += 1
            print("[已脱敏] %s" % os.path.relpath(p, str(config.BASE_DIR)))
        except OSError as e:
            print("[失败] %s：%s" % (p, e))
    print("-" * 66)
    print("已脱敏 %d 个文件（原文件备份为 *.premask.bak）" % changed)
    left = scan_plaintext(roots)
    print("复查：%s" % ("仍发现 %d 个文件含明文，请人工检查" % len(left) if left else "已无明文身份证号"))
    return 0 if not left else 1


def cmd_check_twins(args):
    """撞脸/双胞胎体检：提前找出长相极其相似的考生，核验时改用人工复验流程"""
    store = CandidateStore()
    candidates = store.load()
    index = FaceIndex()
    if not os.path.exists(config.INDEX_PATH):
        print("特征索引不存在，请先执行：python enroll.py build")
        return 2
    index.load()
    threshold = config.TWIN_ENROLL_WARN if args.threshold is None else float(args.threshold)
    pairs = index.twin_pairs(threshold=threshold)
    print("库内特征：%d 人；相似度阈值：%.2f" % (index.size, threshold))
    if not pairs:
        print("未发现撞脸/孪生风险考生。")
        return 0
    print("发现 %d 对相似考生（这些考生核验时会自动转人工复验）：" % len(pairs))
    for p in pairs:
        print("  %s ↔ %s  相似度 %.4f" % (p["a"], p["b"], p["score"]))
    print("建议：为其安排不同的核验通道，或在考前登记辅助证件信息，核验时双人见证。")
    return 0


def cmd_clock(args):
    import timesync
    skew, detail = timesync.check_clock_skew()
    print("时钟自检：%s" % detail)
    return 0 if (skew is None or abs(skew) <= timesync.WARN_SKEW_SEC) else 1


def main():
    ap = argparse.ArgumentParser(description="人脸核验 · 建档与运维工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="为考生提取特征并加密保存索引")
    b.add_argument("--force", action="store_true", help="全部重新建档")
    b.add_argument("--purge-photos", action="store_true", help="建档后删除原始参考照（建议开启）")
    b.add_argument("--yes", action="store_true", help="跳过删除确认")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("status", help="查看索引与合规状态")
    s.set_defaults(func=cmd_status)

    m = sub.add_parser("migrate-json", help="旧 JSON 报名库迁移到 SQLite")
    m.set_defaults(func=cmd_migrate_json)

    md = sub.add_parser("migrate-data", help="把旧布局运行数据收拢到数据根目录")
    md.add_argument("--dry-run", action="store_true", help="只显示计划，不实际移动")
    md.set_defaults(func=cmd_migrate_data)

    p = sub.add_parser("purge-expired", help="按保留期限清理核验记录")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_purge_expired)

    t = sub.add_parser("purge-templates", help="清理到期考生的特征与照片")
    t.add_argument("--days", type=int, default=None)
    t.add_argument("--yes", action="store_true")
    t.set_defaults(func=cmd_purge_templates)

    c = sub.add_parser("check-chain", help="校验审计日志哈希链")
    c.set_defaults(func=cmd_check_chain)

    cp = sub.add_parser("check-paths", help="运行路径体检（中文/空格/超长路径）")
    cp.add_argument("--suggest", action="store_true", help="同时给出短路径迁移方案")
    cp.set_defaults(func=cmd_check_paths)

    ph = sub.add_parser("check-photos", help="参考照体检（损坏/无人脸/多人）")
    ph.add_argument("--faces", action="store_true", help="额外做人脸检测（较慢）")
    ph.set_defaults(func=cmd_check_photos)

    tw = sub.add_parser("check-twins", help="撞脸/双胞胎体检（库内两两相似度）")
    tw.add_argument("--threshold", type=float, default=None,
                    help="告警阈值，默认取 config.TWIN_ENROLL_WARN")
    tw.set_defaults(func=cmd_check_twins)

    sp = sub.add_parser("scrub-plaintext", help="扫描并清除历史遗留的明文身份证号")
    sp.add_argument("--apply", action="store_true", help="真正执行脱敏（默认仅预演）")
    sp.add_argument("--no-backup", action="store_true", help="不留 *.premask.bak 备份")
    sp.set_defaults(func=cmd_scrub_plaintext)

    es = sub.add_parser("edge-status", help="边缘计算层状态（推理后端/档位/延迟/待上报）")
    es.set_defaults(func=cmd_edge_status)

    fo = sub.add_parser("flush-outbox", help="上报边缘缓存的核验记录")
    fo.add_argument("--url", default=None, help="云端接收地址（默认取 EDGE_UPLOAD_URL）")
    fo.set_defaults(func=cmd_flush_outbox)

    k = sub.add_parser("clock", help="时钟偏差自检")
    k.set_defaults(func=cmd_clock)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
