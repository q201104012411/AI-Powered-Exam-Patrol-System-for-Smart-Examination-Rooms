# -*- coding: utf-8 -*-
r"""
报名库数据体检与清理

为什么需要它：报名库里的"脏数据"会以非常隐蔽的方式让核验失效。真实遇到过两类：

  1. **两名考生共用同一张参考照**（例如手填路径时把 C001 填成了别人的照片）。
     结果两人的特征向量完全相同，1:1 核验时"最像的其他人"就是被核验人自己，
     系统只能判 IDENTITY_CONFLICT（身份混淆）/ TWIN_SUSPECT（疑似撞脸），
     表面现象是"这个人怎么核验都过不了"。
  2. **参考照路径缺少扩展名**（写成 C002 而不是 C002.jpg），或者照片已被删除。
     表现为建档失败 IMAGE_ERROR / 核验直接 NO_FACE。

用法：
  python 工具\库数据体检.py                     # 只体检，列问题，不改数据
  python 工具\库数据体检.py --json              # 输出 JSON，便于脚本处理
  python 工具\库数据体检.py --delete ID1,ID2    # 删除指定考生（会先自动备份数据库）
  python 工具\库数据体检.py --delete ID1 --yes  # 跳过确认（自动化用）

退出码：0 = 没有发现问题；1 = 发现问题（便于 CI/启动脚本判断）
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                                        # noqa: E402
import data_crypto                                   # noqa: E402


def _dec(v):
    try:
        return data_crypto.decrypt_field(v or "")
    except Exception:
        return v or ""


def load_rows(db=None):
    """读出全部考生（敏感字段已解密）"""
    db = str(db or config.DB_PATH)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM candidates ORDER BY candidate_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def inspect(rows=None):
    """体检：返回 {"missing_photo": [...], "shared_photo": [...], "no_photo": [...],
                  "duplicate_pair": [...]}"""
    rows = load_rows() if rows is None else rows
    missing, no_photo = [], []
    by_photo = defaultdict(list)

    for r in rows:
        cid = r.get("candidate_id") or ""
        raw = _dec(r.get("reference_photo"))
        if not raw:
            no_photo.append({"candidate_id": cid, "why": "未填参考照路径"})
            continue
        resolved = str(config.resolve_photo(raw))
        if not os.path.exists(resolved):
            missing.append({"candidate_id": cid, "path": raw,
                            "resolved": resolved, "why": "参考照文件不存在"})
            continue
        # 用"真实文件"归并，能识别 C001 与 C001.jpg 指向同一张图的情况
        try:
            key = str(Path(resolved).resolve()).lower()
        except OSError:
            key = resolved.lower()
        by_photo[key].append(cid)

    shared = []
    for path, cids in sorted(by_photo.items()):
        if len(cids) > 1:
            shared.append({"path": path, "candidates": sorted(cids)})

    return {"total": len(rows), "missing_photo": missing, "no_photo": no_photo,
            "shared_photo": shared}


def delete_candidates(ids, db=None):
    """删除指定考生（先备份数据库）。返回 (删除条数, 备份路径)"""
    db = str(db or config.DB_PATH)
    ids = [str(i).strip() for i in ids if str(i).strip()]
    if not ids:
        return 0, None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = "%s.bak_%s" % (db, stamp)
    shutil.copy2(db, backup)
    conn = sqlite3.connect(db)
    try:
        conn.executemany("DELETE FROM candidates WHERE candidate_id=?", [(i,) for i in ids])
        conn.commit()
    finally:
        conn.close()
    return len(ids), backup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", default="", help="要删除的考生编号，逗号分隔")
    ap.add_argument("--yes", action="store_true", help="跳过删除确认")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出体检结果")
    ap.add_argument("--db", default=None, help="指定数据库路径（默认 config.DB_PATH）")
    args = ap.parse_args()

    report = inspect(load_rows(args.db) if args.db else None)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 74)
        print("报名库数据体检　共 %d 名考生　%s" % (report["total"], config.DB_PATH))
        print("=" * 74)
        if report["shared_photo"]:
            print("\n[严重] %d 组考生共用同一张参考照 —— 会导致核验恒判身份混淆/撞脸："
                  % len(report["shared_photo"]))
            for it in report["shared_photo"]:
                print("   考生 %s  共用 %s" % ("、".join(it["candidates"]), it["path"]))
        if report["missing_photo"]:
            print("\n[问题] %d 名考生的参考照文件不存在 —— 会导致建档失败/核验无人脸："
                  % len(report["missing_photo"]))
            for it in report["missing_photo"][:20]:
                print("   %-18s %s" % (it["candidate_id"], it["path"]))
            if len(report["missing_photo"]) > 20:
                print("   … 其余 %d 条省略" % (len(report["missing_photo"]) - 20))
        if report["no_photo"]:
            print("\n[提示] %d 名考生未填参考照路径：" % len(report["no_photo"]))
            for it in report["no_photo"][:20]:
                print("   %-18s %s" % (it["candidate_id"], it["why"]))
        if not (report["shared_photo"] or report["missing_photo"] or report["no_photo"]):
            print("\n未发现问题：全部考生都有各自独立、且存在的参考照。")
        print("=" * 74)

    problems = bool(report["shared_photo"] or report["missing_photo"])

    if args.delete:
        ids = [s for s in args.delete.split(",") if s.strip()]
        known = {r["candidate_id"] for r in (load_rows(args.db) if args.db else load_rows())}
        unknown = [i for i in ids if i not in known]
        if unknown:
            print("以下编号不存在，已忽略：%s" % "、".join(unknown))
        todo = [i for i in ids if i in known]
        if todo and not args.yes:
            print("\n将删除 %d 名考生：%s" % (len(todo), "、".join(todo)))
            print("（数据库会先自动备份）")
            try:
                ans = input("确认删除？输入 yes 继续：").strip().lower()
            except EOFError:
                ans = ""
            if ans != "yes":
                print("已取消，未改动数据。")
                return 1 if problems else 0
        n, backup = delete_candidates(todo, args.db)
        print("已删除 %d 名考生；备份：%s" % (n, backup))
        print("请重建特征索引：python enroll.py build --force")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
