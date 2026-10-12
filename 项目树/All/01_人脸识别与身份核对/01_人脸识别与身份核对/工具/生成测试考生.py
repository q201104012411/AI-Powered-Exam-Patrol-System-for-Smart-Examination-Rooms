# -*- coding: utf-8 -*-
r"""
生成测试考生（合成人像 + 名单入库）

用途：为「01 人脸识别与身份核对」批量造测试数据，方便演示/压测列表与检索。
注意：**这些是程序合成的人像图，不是真人照片**，图上带 "SYNTHETIC" 水印；
     绝不会与真实考生混淆。正式使用前请用 --clean 清掉。

用法：
  python 工具\生成测试考生.py --probe 5          # 先探一下合成脸能被检测到的比例（不入库）
  python 工具\生成测试考生.py --count 300        # 生成 300 张 + 入库 + 建档
  python 工具\生成测试考生.py --count 300 --no-enroll   # 只生成图与名单，不建档
  python 工具\生成测试考生.py --clean            # 删除本工具生成的全部测试考生与照片

命名规则：data\ref_photos\<考生编号>.jpg，考生编号 = <前缀><4位序号>（默认 T0001…）
"""
import argparse
import os
import random
import sys
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import config                                        # noqa: E402

SURNAMES = ("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
            "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
GIVEN = ("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英建华文博子轩浩然梓涵欣怡诗涵"
         "雨泽思远晨曦嘉怡若曦宇轩子墨语嫣一诺婉清雨桐志强建国春梅晓东海燕国强立新玉兰桂芳")
CONSENT = "已同意"
# 6 位行政区划码（前 6 位）+ 8 位出生日期 + 3 位顺序码 = 17 位，再加 1 位校验位
REGIONS = ("330106", "330102", "110105", "310104", "440305", "510107", "420106", "320106",
           "350203", "610113", "370102", "500106")

_ID_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_C = "10X98765432"


def make_id_card(rng):
    """生成校验位合法的 18 位身份证号（GB 11643-1999）"""
    y = rng.randint(1988, 2007)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    seq = rng.randint(0, 999)
    body = "%s%04d%02d%02d%03d" % (rng.choice(REGIONS), y, m, d, seq)
    assert len(body) == 17, len(body)
    s = sum(int(body[i]) * _ID_W[i] for i in range(17))
    return body + _ID_C[s % 11]


def draw_face(rng, w=480, h=640):
    """
    画一张合成人像。按"证件照"构图：正面、大头、居中。
    每个身份在脸型/五官/肤色/发型上有随机差异，便于索引区分不同身份。
    """
    # 背景（证件照常见浅蓝/浅灰）
    bg = (rng.randint(190, 235), rng.randint(200, 240), rng.randint(215, 250))
    img = np.full((h, w, 3), bg, np.uint8)
    # 轻微渐晕，避免纯色背景
    yy = np.linspace(-1, 1, h, dtype=np.float32)[:, None, None]
    img = np.clip(img.astype(np.float32) * (0.92 + 0.08 * (1 - np.abs(yy))), 0, 255).astype(np.uint8)

    cx, cy = w // 2, int(h * 0.46)
    fw = int(w * rng.uniform(0.30, 0.36))          # 脸半宽
    fh = int(fw * rng.uniform(1.22, 1.36))         # 脸半高
    skin = (rng.randint(150, 200), rng.randint(175, 220), rng.randint(200, 240))

    # 脖子与肩
    cv2.rectangle(img, (cx - fw // 2, cy + fh - 10), (cx + fw // 2, cy + fh + 60),
                  tuple(int(c * 0.92) for c in skin), -1)
    cv2.ellipse(img, (cx, cy + fh + 130), (int(fw * 1.9), int(fh * 0.95)), 0, 180, 360,
                (rng.randint(50, 110), rng.randint(55, 115), rng.randint(60, 120)), -1)

    # 头发（外轮廓，比脸略大）
    hair_col = (rng.randint(20, 70), rng.randint(20, 70), rng.randint(20, 75))
    cv2.ellipse(img, (cx, cy - int(fh * 0.14)), (int(fw * 1.14), int(fh * 1.06)), 0, 0, 360,
                hair_col, -1)
    # 脸
    cv2.ellipse(img, (cx, cy), (fw, fh), 0, 0, 360, skin, -1)
    # 下颌收窄（更像人脸轮廓）
    cv2.ellipse(img, (cx, cy + int(fh * 0.30)), (int(fw * 0.86), int(fh * 0.76)), 0, 0, 360,
                skin, -1)

    # 眉
    brow_y = cy - int(fh * 0.34)
    eye_dx = int(fw * rng.uniform(0.42, 0.50))
    for sgn in (-1, 1):
        cv2.ellipse(img, (cx + sgn * eye_dx, brow_y), (int(fw * 0.26), int(fh * 0.045)),
                    sgn * rng.randint(-6, 6), 180, 360, hair_col, max(3, fw // 16))

    # 眼
    eye_r = int(fw * rng.uniform(0.15, 0.19))
    eye_y = cy - int(fh * 0.18)
    for sgn in (-1, 1):
        ex = cx + sgn * eye_dx
        cv2.ellipse(img, (ex, eye_y), (eye_r, int(eye_r * 0.66)), 0, 0, 360, (245, 245, 245), -1)
        iris = rng.choice([(70, 55, 40), (60, 80, 90), (45, 60, 75), (55, 70, 65)])
        cv2.circle(img, (ex, eye_y), int(eye_r * 0.46), iris, -1)
        cv2.circle(img, (ex, eye_y), int(eye_r * 0.20), (18, 18, 18), -1)
        cv2.ellipse(img, (ex, eye_y - int(eye_r * 0.62)), (eye_r, int(eye_r * 0.34)), 0,
                    180, 360, (hair_col[0] // 2, hair_col[1] // 2, hair_col[2] // 2),
                    max(2, fw // 22))

    # 鼻
    nose_len = int(fh * rng.uniform(0.24, 0.32))
    cv2.line(img, (cx, eye_y + int(fh * 0.06)), (cx - int(fw * 0.10), eye_y + nose_len),
             tuple(int(c * 0.86) for c in skin), max(2, fw // 26))
    cv2.line(img, (cx, eye_y + int(fh * 0.06)), (cx + int(fw * 0.10), eye_y + nose_len),
             tuple(int(c * 0.86) for c in skin), max(2, fw // 26))
    cv2.ellipse(img, (cx, eye_y + nose_len), (int(fw * 0.20), int(fh * 0.05)), 0, 0, 360,
                tuple(int(c * 0.80) for c in skin), -1)

    # 嘴
    mouth_y = eye_y + int(fh * 0.46)
    mw = int(fw * rng.uniform(0.32, 0.44))
    lip = (rng.randint(90, 150), rng.randint(110, 165), rng.randint(150, 200))
    cv2.ellipse(img, (cx, mouth_y), (mw, int(fh * 0.075)), 0, 0, 360, lip, -1)
    cv2.line(img, (cx - mw, mouth_y), (cx + mw, mouth_y), (60, 60, 70), max(2, fw // 30))

    # 柔化，去掉生硬边缘（更像照片）
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(1.0, 1.9))
    # 光照不均 + 传感器噪声 + JPEG 压缩
    grad = np.linspace(rng.uniform(0.90, 0.97), rng.uniform(1.02, 1.10), w)[None, :, None]
    img = np.clip(img.astype(np.float32) * grad, 0, 255)
    img = np.clip(img + np.random.normal(0, rng.uniform(1.5, 4.0), img.shape), 0, 255)
    img = img.astype(np.uint8)

    # 明确标注：这是合成测试图（避免被误当真人照片）
    cv2.putText(img, "SYNTHETIC TEST IMAGE", (10, h - 14), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(img, "SYNTHETIC TEST IMAGE", (10, h - 14), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (40, 40, 160), 1, cv2.LINE_AA)
    return img


def gen_name(rng):
    s = rng.choice(SURNAMES)
    n = rng.randint(1, 2)
    return s + "".join(rng.choice(GIVEN) for _ in range(n))


_IDX_W = {3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9, 11: 10, 12: 11, 13: 12, 14: 13,
          15: 14, 16: 15, 17: 16, 18: 17}


def build_items(rng, count, prefix, start, room_prefix="B"):
    items = []
    for i in range(count):
        cid = "%s%04d" % (prefix, start + i)
        seat = "%s-%02d" % (room_prefix, i % 30 + 1)
        items.append({"candidate_id": cid, "name": gen_name(rng),
                      "id_card": make_id_card(rng),
                      "ticket_no": "2026%s" % cid,
                      "seat_no": seat, "consent_status": CONSENT,
                      "reference_photo": ""})
    return items


def photo_path(cid):
    return str(Path(config.PHOTO_DIR) / ("%s.jpg" % cid))


def cmd_probe(args):
    """只探合成脸的检测率，不入库"""
    import face_engine
    rng = random.Random(args.seed)
    ok, dets = 0, []
    for i in range(args.probe):
        img = draw_face(rng)
        try:
            info = face_engine.extract(img)
            ok += 1
            d = (info.get("det_score") if isinstance(info, dict) else None)
            dets.append("检测到人脸%s" % (("　置信度 %.3f" % d) if d else ""))
        except Exception as e:                       # noqa: BLE001
            dets.append("未检测到人脸（%s）" % type(e).__name__)
    for i, d in enumerate(dets, 1):
        print("  第 %d 张：%s" % (i, d))
    print("\n合成脸检测率：%d/%d = %.0f%%" % (ok, args.probe, 100.0 * ok / max(1, args.probe)))
    if ok == 0:
        print("结论：合成图不会被 InsightFace 检出（它面向真人脸训练）。")
        print("     仍可正常用作名单/界面演示数据；人脸比对需真实照片。")
    return 0


def cmd_clean(args):
    import sqlite3
    import config as cfg
    n = 0
    conn = sqlite3.connect(str(cfg.DB_PATH))
    try:
        rows = conn.execute("SELECT candidate_id FROM candidates WHERE candidate_id LIKE ?",
                            (args.prefix + "%",)).fetchall()
        ids = [r[0] for r in rows]
        for cid in ids:
            p = photo_path(cid)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        conn.execute("DELETE FROM candidates WHERE candidate_id LIKE ?", (args.prefix + "%",))
        conn.commit()
        n = len(ids)
    finally:
        conn.close()
    # 同步从特征索引里摘掉
    try:
        from face_index import FaceIndex
        idx = FaceIndex()
        idx.load()
        for cid in ids:
            try:
                idx.remove(cid)
            except Exception:                        # noqa: BLE001
                pass
        idx.save()
    except Exception as e:                           # noqa: BLE001
        print("  特征索引同步跳过：%s" % str(e)[:120])
    print("已清理测试考生 %d 个（编号前缀 %s），照片与索引已同步" % (n, args.prefix))
    print("如需彻底重建索引：python enroll.py build")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--prefix", default="T")
    ap.add_argument("--seed", type=int, default=20261011)
    ap.add_argument("--probe", type=int, default=0, help="只生成 N 张探检测率，不入库")
    ap.add_argument("--no-enroll", action="store_true", help="不入库/不建档，只生成图与名单")
    ap.add_argument("--clean", action="store_true", help="删除本工具生成的测试考生")
    args = ap.parse_args()

    if args.clean:
        return cmd_clean(args)
    if args.probe:
        return cmd_probe(args)

    rng = random.Random(args.seed)
    os.makedirs(str(config.PHOTO_DIR), exist_ok=True)
    items = build_items(rng, args.count, args.prefix, args.start)
    print("生成 %d 张合成人像 -> %s" % (len(items), config.PHOTO_DIR))
    from face_engine import imwrite_unicode
    for i, it in enumerate(items, 1):
        img = draw_face(rng)
        p = photo_path(it["candidate_id"])
        if not imwrite_unicode(p, img, quality=88, encrypt=True):
            print("  写入失败：%s" % p)
            continue
        it["reference_photo"] = p
        if i % 50 == 0 or i == len(items):
            print("  已生成 %d/%d" % (i, len(items)))
    with open(str(Path(config.DATA_ROOT) / "测试考生名单.csv"), "w",
              encoding="utf-8-sig") as f:
        f.write("考生编号,姓名,身份证号,准考证号,座位号,同意状态\n")
        for it in items:
            f.write("%s,%s,%s,%s,%s,%s\n" % (it["candidate_id"], it["name"], it["id_card"],
                                             it["ticket_no"], it["seat_no"],
                                             it["consent_status"]))
    print("名单已导出：%s" % (Path(config.DATA_ROOT) / "测试考生名单.csv"))

    if args.no_enroll:
        print("（--no-enroll）未入库。要入库可执行：python enroll.py import 名单.csv 或用界面名单导入")
        return 0

    from store import CandidateStore
    st = CandidateStore()
    n = st.upsert_many(items, operator="gen-test-data")
    print("已写入报名库：%d 条（当前共 %d 人）" % (n, st.count()))

    print("开始建档（提取人脸特征）…")
    try:
        from face_index import FaceIndex
        idx = FaceIndex()
        report = idx.build(st.load(), force=True)
        idx.save()
        print("建档结果：%s" % report)
        if isinstance(report, dict):
            failed = report.get("failed") or report.get("skipped") or []
            if failed:
                print("提示：%d 人未能建档（合成图不含真实人脸，InsightFace 检出不到属正常）。"
                      % len(failed))
                print("     这些人仍可用于名单/界面/权限演示，但无法参与人脸比对。")
    except Exception as e:                      # noqa: BLE001
        print("建档未完成：%s" % str(e)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
