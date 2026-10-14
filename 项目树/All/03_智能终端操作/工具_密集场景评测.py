# -*- coding: utf-8 -*-
r"""
密集考场（40 人）检测评测 —— 把"后排看不看得见"变成可复现的数字

为什么需要：考场监控不是看一个人。一个 1080p 机位看 40 个考生时，
后排考生只有几十像素高：若把整帧压到 640 长边再送模型，学生会被压到
十几像素 —— 必然大量漏检（这正是"摄像头不能只看一个人"要解决的问题）。

本工具用**真实人物素材**按考场排布合成 40 人场景（8 列 × 5 排，近大远小、
桌线遮挡、随机翻转与亮度扰动），再用不同"分析分辨率 / 是否分块推理"跑一遍，
按与真实粘贴位置的 IoU 匹配，算出：

    召回率 recall（40 人里检出几个）、精确率 precision（检出的框有几个对）、
    框数、单帧耗时 —— 用来判断某项优化到底值不值。

如实说明：
  · 合成场景的**人物外观来自真实照片**（tests\assets\people.jpg 与 01 的抓拍），
    但排布是合成的，因此**只用于横向对比不同算法配置**，
    不能当"真实考场准确率"对外宣称；真实指标必须用考场现场样本。
  · 未提供 --image 时一律用合成场景；给了 --image 就只跑真实照片（无法算召回，
    只输出框数与画框图，供人工核对）。

用法：
  python 工具_密集场景评测.py                      # 默认：40 人 × 3 种分辨率对比
  python 工具_密集场景评测.py --persons 40 --res 640,1280,1920
  python 工具_密集场景评测.py --tiling on           # 打开分块推理对比
  python 工具_密集场景评测.py --image data\captures\考场1.jpg
输出：屏幕表格 + data\reports\密集场景评测_<时间戳>.md 与各配置的画框图。
"""
import argparse
import glob
import inspect
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cv2                              # noqa: E402
import numpy as np                      # noqa: E402

import person_detect as pd              # noqa: E402


# ---------------------------------------------------------------------
#  真实人物素材
# ---------------------------------------------------------------------
def _load(path):
    data = np.fromfile(str(path), dtype=np.uint8)      # 兼容中文路径
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _person_crops(max_crops=16):
    """用检测器自己在真实照片上找"人"，把人裁出来当素材（避免手写坐标）。

    确定性：源文件排序后取固定前几个、裁片排序去重后截断 —— 否则 01 目录里
    不断新增的抓拍会让每次评测的场景都不一样，前后数字没法比。
    """
    crops = []
    sources = [HERE / "tests" / "assets" / "people.jpg"]
    sources += sorted(Path(p) for p in glob.glob(
        str(HERE.parent / "01_人脸识别与身份核对" / "*" / "data" / "captures" / "*.jpg")))[:2]
    tr = pd.PersonTracker(max_input_dim=1920, min_hits=1)
    for src in sources:
        if not Path(src).exists():
            continue
        img = _load(src)
        if img is None:
            continue
        h, w = img.shape[:2]
        scale = 1920.0 / max(h, w) if max(h, w) < 1920 else 1.0
        big = cv2.resize(img, (int(w * scale), int(h * scale))) if scale > 1 else img
        for (x, y, bw, bh) in tr._detect(big):
            px, py = int(bw * 0.06), int(bh * 0.04)
            x0 = max(0, x - px)
            y0 = max(0, y - py)
            x1 = min(big.shape[1], x + bw + px)
            y1 = min(big.shape[0], y + bh + py)
            if (x1 - x0) >= 24 and (y1 - y0) >= 40:
                crops.append(big[y0:y1, x0:x1].copy())
    crops.sort(key=lambda c: (-c.shape[0], -c.shape[1]))    # 稳定顺序
    return crops[:max_crops]


def _composite(crops, persons, canvas=(1080, 1920), rows=5, cols=8, seed=7):
    """把真人裁片按"近大远小"排成考场，返回 (图, 真实框列表)"""
    rng = np.random.RandomState(seed)
    ch, cw = canvas
    img = np.full((ch, cw, 3), 70, dtype=np.uint8)
    # 背景：墙 + 桌线
    cv2.rectangle(img, (0, 0), (cw, int(ch * 0.18)), (96, 96, 100), -1)
    for r in range(rows):
        y = int(ch * (0.20 + r * (0.78 / rows)))
        cv2.line(img, (0, y), (cw, y), (118, 118, 118), 2)
    gts = []
    n = 0
    for r in range(rows):
        # 近大远小：最后一行最大
        depth = (r + 1) / float(rows)                 # 0.2(远) → 1.0(近)
        target_h = int(34 + depth * 118)              # 34~152 px 高
        y_center = int(ch * (0.24 + r * (0.72 / rows)))
        for c in range(cols):
            if n >= persons:
                break
            crop = crops[rng.randint(0, len(crops))] if crops else None
            if crop is None:
                break
            h, w = crop.shape[:2]
            s = target_h / float(h)
            tw, th = max(8, int(w * s)), max(12, int(h * s))
            patch = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
            if rng.rand() < 0.5:
                patch = cv2.flip(patch, 1)
            gain = 0.85 + 0.3 * rng.rand()            # 亮度扰动
            patch = np.clip(patch.astype(np.float32) * gain, 0, 255).astype(np.uint8)
            if r >= 2:                                # 后几排：桌线遮挡下半身
                cut = int(th * (0.25 + 0.2 * (rows - r) / rows))
                patch[th - cut:, :] = (118, 118, 118)
            x_center = int(cw * (0.06 + c * (0.88 / max(1, cols - 1))))
            x0 = max(0, min(cw - tw, x_center - tw // 2 + rng.randint(-4, 5)))
            y0 = max(0, min(ch - th, y_center - th // 2))
            img[y0:y0 + th, x0:x0 + tw] = patch
            gts.append((x0, y0, tw, th))
            n += 1
        if n >= persons:
            break
    return img, gts


# ---------------------------------------------------------------------
#  评测
# ---------------------------------------------------------------------
def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1, yi1 = max(ax, bx), max(ay, by)
    xi2, yi2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    union = aw * ah + bw * bh - inter
    return inter / float(union) if union > 0 else 0.0


def _match(dets, gts, iou_thresh=0.4):
    """贪婪匹配 → (匹配数, 每个 GT 是否命中, 命中用的 IoU 列表)"""
    used = set()
    hit = [False] * len(gts)
    ious = []
    for d in dets:
        best, bi = 0.0, -1
        for i, g in enumerate(gts):
            if i in used:
                continue
            v = _iou(d, g)
            if v > best:
                best, bi = v, i
        if best >= iou_thresh and bi >= 0:
            used.add(bi)
            hit[bi] = True
            ious.append(best)
    return len(used), hit, ious


def _resize_long_edge(img, long_edge):
    h, w = img.shape[:2]
    if max(h, w) <= long_edge:
        return img, 1.0
    s = long_edge / float(max(h, w))
    return cv2.resize(img, (max(2, int(w * s)), max(2, int(h * s))),
                      interpolation=cv2.INTER_AREA), s


def _tracker_kwargs(args, tiling):
    kw = dict(max_input_dim=4096, min_hits=1, dense_mode=True)
    try:
        params = inspect.signature(pd.PersonTracker.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "yolo_tiling" in params:                    # 老版本没有分块推理，自动忽略
        kw["yolo_tiling"] = ("on" if tiling == "on" else
                             ("off" if tiling == "off" else "auto"))
        if args.tile_size:
            kw["yolo_tile_size"] = args.tile_size
        if args.tile_max:
            kw["yolo_tile_max"] = args.tile_max
    if args.conf is not None and "yolo_conf" in params:
        kw["yolo_conf"] = args.conf
    if args.contain is not None and "contain_thresh" in params:
        kw["contain_thresh"] = args.contain
    return kw


def evaluate(frames, gts, args, long_edge, tiling):
    """在给定配置下跑一遍 → 指标 dict"""
    tr = pd.PersonTracker(**_tracker_kwargs(args, tiling))
    dets_all = []
    lat = []
    for frame in frames:
        small, scale = _resize_long_edge(frame, long_edge)
        t0 = time.perf_counter()
        dets = tr._detect(small)
        lat.append((time.perf_counter() - t0) * 1000.0)
        # 映射回原图坐标，便于与 GT 比
        dets_all.append([tuple(int(round(v / scale)) for v in d) for d in dets])
    d = dets_all[0]
    matched, hit, ious = _match(d, gts)
    return {"long_edge": long_edge, "tiling": tiling, "det": len(d),
            "matched": matched, "recall": matched / float(len(gts)) if gts else 0.0,
            "precision": matched / float(len(d)) if d else 0.0,
            "ms": float(np.median(lat)), "missed": [g for g, h in zip(gts, hit) if not h],
            "dets": d}


def draw(frame, dets, gts, note):
    out = frame.copy()
    for (x, y, w, h) in dets:
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 200, 0), 2)
    for (x, y, w, h) in (gts or []):
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 165, 255), 1)
    cv2.putText(out, note, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)
    return out


def main():
    ap = argparse.ArgumentParser(description="密集考场（40 人）检测评测")
    ap.add_argument("--persons", type=int, default=40, help="合成场景里的人数")
    ap.add_argument("--rows", type=int, default=5, help="排数")
    ap.add_argument("--cols", type=int, default=8, help="每排人数")
    ap.add_argument("--canvas", default="1920x1080", help="画面尺寸 WxH")
    ap.add_argument("--res", default="640,1280,1920",
                    help="要对比的 AI 分析长边（逗号分隔）")
    ap.add_argument("--tiling", default="auto", choices=("auto", "on", "off"),
                    help="分块推理开关（auto=按画面尺寸自动决定）")
    ap.add_argument("--tile-size", dest="tile_size", type=int, default=None,
                    help="分块边长（像素，默认用引擎的 640）")
    ap.add_argument("--tile-max", dest="tile_max", type=int, default=None,
                    help="单帧最多几块（默认 12）")
    ap.add_argument("--conf", type=float, default=None,
                    help="YOLO 置信度阈值（默认 0.25）")
    ap.add_argument("--contain", type=float, default=None,
                    help="最终「部分-整体」去重阈值（默认 0.85；越大越保守，密集场景建议 0.92 以上）")
    ap.add_argument("--image", help="用一张真实照片代替合成场景（只出框数与画框图）")
    ap.add_argument("--frames", type=int, default=1, help="重复跑几帧取耗时中位")
    ap.add_argument("--outdir", default=None, help="报告目录（默认 data\\reports）")
    args = ap.parse_args()

    cw, ch = (int(v) for v in args.canvas.lower().split("x"))
    print("=" * 82)
    print("密集考场检测评测")
    print("=" * 82)

    if args.image:
        frame = _load(args.image)
        if frame is None:
            print("[失败] 读不到图片：%s" % args.image)
            return 2
        gts = None
        print("素材：真实照片 %s（%dx%d）—— 无法算召回，只输出检出数与画框图"
              % (args.image, frame.shape[1], frame.shape[0]))
    else:
        crops = _person_crops()
        if not crops:
            print("[失败] 没找到可用的真实人物素材（tests\\assets\\people.jpg 缺失？）")
            return 2
        frame, gts = _composite(crops, args.persons, (ch, cw), args.rows, args.cols)
        print("素材：合成考场 %dx%d ｜ 真人裁片 %d 个 ｜ 排布 %d 排 × %d 人 = %d 人"
              % (cw, ch, len(crops), args.rows, args.cols, len(gts)))
        print("      近大远小：最远一排约 34px 高，最近一排约 152px 高（模拟 1080p 看 40 人）")
        print("      注：外观取自真实抓拍，排布为合成，仅用于横向对比，不等同真实考场准确率")

    res_list = [int(v) for v in str(args.res).split(",") if v.strip()]
    rows = []
    for res in res_list:
        frames = [frame] * max(1, args.frames)
        r = evaluate(frames, gts or [], args, res, args.tiling)
        rows.append(r)
        print()
        print("---- 分析长边 %4d ｜ 分块推理 %-4s ----" % (res, r["tiling"]))
        if gts:
            print("  召回 %2d/%2d = %5.1f%%   精确 %5.1f%%   检出框 %3d   单帧 %6.1f ms"
                  % (r["matched"], len(gts), r["recall"] * 100, r["precision"] * 100,
                     r["det"], r["ms"]))
        else:
            print("  检出框 %3d   单帧 %6.1f ms" % (r["det"], r["ms"]))
        if gts and r["missed"]:
            print("  漏检位置示例（原图坐标）：%s%s"
                  % (r["missed"][:5], " …" if len(r["missed"]) > 5 else ""))

    outdir = Path(args.outdir) if args.outdir else (HERE / "data" / "reports")
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        md = outdir / ("密集场景评测_%s.md" % stamp)
        lines = ["# 密集考场检测评测报告", "",
                 "- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "- 场景：%s" % ("真实照片 %s" % args.image if args.image else
                                "合成考场 %d 人（%d×%d，真人裁片 %d 个）"
                                % (args.persons, cw, ch, len(_person_crops()))),
                 "- 匹配判据：与真实位置 IoU ≥ 0.4", "",
                 "| 分析长边 | 分块推理 | 检出框 | 召回 | 精确 | 单帧耗时 |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            lines.append("| %d | %s | %d | %s | %s | %.1f ms |"
                         % (r["long_edge"], r["tiling"], r["det"],
                            ("%d/%d = %.1f%%" % (r["matched"], len(gts), r["recall"] * 100))
                            if gts else "—",
                            ("%.1f%%" % (r["precision"] * 100)) if gts else "—", r["ms"]))
        lines += ["", "> 合成场景只用于横向对比优化效果；真实考场准确率必须用现场样本。"]
        md.write_text("\n".join(lines), encoding="utf-8")
        for r in rows:
            small, sc = _resize_long_edge(frame, r["long_edge"])
            dets_small = [tuple(int(v * sc) for v in d) for d in r["dets"]]
            gts_small = [tuple(int(v * sc) for v in g) for g in (gts or [])]
            note = "res=%d tiling=%s det=%d%s" % (
                r["long_edge"], r["tiling"], r["det"],
                (" recall=%.0f%%" % (r["recall"] * 100)) if gts else "")
            img = draw(small, dets_small, gts_small, note)
            cv2.imencode(".jpg", img)[1].tofile(
                str(outdir / ("密集场景评测_%s_res%d_%s.jpg"
                              % (stamp, r["long_edge"], r["tiling"]))))
        print()
        print("报告：%s" % md)
        print("画框图：%s\\密集场景评测_%s_res*_*.jpg" % (outdir, stamp))
    except OSError as e:
        print("（写报告失败，不影响上面结果：%s）" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
