# -*- coding: utf-8 -*-
r"""
02 · 检测模型对比实验（把人形/模型差异变成可写进报告的数据）

用途：同一段考场画面，分别用 YOLO11n-ONNX 与 HOG+SVM 跑一遍，输出
      耗时、人数、轨迹连续性、以及（可选）精度指标，用于回答
      "换模型到底值不值" —— 而不是只给结论。

检测链说明（与 person_detect.py 一致，检测链是**自动**选的，不能手动指定族）：
      YOLO11n ONNX（主力，models\yolo11n.onnx）
        -> MobileNet-SSD（本目录存在 prototxt+caffemodel 时兼容启用）
        -> HOG+SVM（兜底）
      注意：YOLO 可用时，"某帧 YOLO 零检出"仍会叠加一次 HOG 兜底（新版行为），
      所以本工具的 YOLO 行如实标注为"零检出帧叠加 HOG 兜底"，不冒充纯 YOLO。

用法（在本目录下执行）：
  # 1) 只看各后端能不能用 + 速度基准（不需要任何素材，随时可跑）
  python 工具_模型对比.py --selftest

  # 2) 用一段真实考场录像对比（最推荐，最能说明问题）
  python 工具_模型对比.py --video data\captures\考场1.mp4 --frames 120

  # 3) 用一批现场抓帧对比（没有录像时用这个）
  python 工具_模型对比.py --images data\captures --frames 60

  # 4) 只测某个后端 / 指定模型文件
  python 工具_模型对比.py --video x.mp4 --detectors yolo --model models\yolo11n.onnx

  # 5) 有标注时直接出精度（标注为 YOLO txt 格式，与图片同名）
  python 工具_模型对比.py --images data\captures --gt data\gt

输出：屏幕表格 + data\reports\模型对比_<时间戳>.md（可直接粘进作品书）

说明：本脚本不下载任何权重。缺哪个模型就报哪个，并给出获取位置。
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import person_detect as pd          # noqa: E402
try:
    import config
    _DEFAULT_REPORT_DIR = str(config.REPORT_DIR)
except Exception:                    # 03 等目录没有 config.REPORT_DIR 时自给自足
    _DEFAULT_REPORT_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "reports")


def _project_tag():
    """自动识别所属项目编号（本工具在 02 与 03 下各有一份，输出要能区分）。"""
    d = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    for tag in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
        if d.startswith(tag):
            return tag
    return d


PROJECT_TAG = _project_tag()


# ---------------------------------------------------------------------
#  检测链探测（新版 person_detect 不再提供 backend_name/backend_info/
#  detect_backends/find_model，这里按它的真实优先级如实判定，不猜）
# ---------------------------------------------------------------------
def yolo_disabled():
    """与 person_detect._yolo_disabled 同口径：EXAM_YOLO_DISABLE=1 时禁用 YOLO"""
    return os.environ.get("EXAM_YOLO_DISABLE", "").strip() in ("1", "true", "True")


def yolo_model_path():
    """当前检测链会用的 YOLO ONNX 路径（环境变量 EXAM_YOLO_MODEL 优先）"""
    env = os.environ.get("EXAM_YOLO_MODEL", "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(pd.__file__)),
                        "models", "yolo11n.onnx")


def onnxruntime_ok():
    """YOLO 推理依赖 onnxruntime；缺了就只剩 HOG 兜底"""
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def ssd_model_paths():
    """MobileNet-SSD 兼容模型的 (prototxt, caffemodel)；缺任一返回 (None, None)"""
    d = os.path.dirname(os.path.abspath(pd.__file__))
    proto = os.path.join(d, "MobileNetSSD_deploy.prototxt")
    caffe = os.path.join(d, "MobileNetSSD_deploy.caffemodel")
    if os.path.exists(proto) and os.path.exists(caffe):
        return proto, caffe
    return None, None


def describe_chain():
    """一行中文说明当前检测链到底会跑什么（自检/报告都用它，不用旧接口）"""
    if yolo_disabled():
        yolo = "YOLO11n 已停用(EXAM_YOLO_DISABLE=1)"
    elif not onnxruntime_ok():
        yolo = "YOLO11n 缺 onnxruntime"
    elif not os.path.exists(yolo_model_path()):
        yolo = "YOLO11n 缺模型(%s)" % os.path.basename(yolo_model_path())
    else:
        yolo = "YOLO11n 就绪(%s)" % os.path.basename(yolo_model_path())
    proto, _ = ssd_model_paths()
    return "；".join([yolo,
                      "MobileNet-SSD %s" % ("就绪" if proto else "缺模型"),
                      "HOG+SVM 兜底可用"])


# ---------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------
def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1, yi1 = max(ax, bx), max(ay, by)
    xi2, yi2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _pct(seq, p):
    if not seq:
        return 0.0
    return float(np.percentile(np.asarray(seq, dtype=float), p))


def _mean(seq):
    return float(np.mean(seq)) if seq else 0.0


def _make_frame(w=1280, h=720, seed=0, persons=40):
    """合成一帧"密集教室"：桌椅线条 + 若干人形剪影。

    注意：这只用于"速度基准"和流程自检，不能用来评价识别准确率——
    合成剪影与真实监控画面差异太大，任何精度结论都必须用真实素材。
    """
    rng = np.random.RandomState(seed)
    img = np.full((h, w, 3), 60, dtype=np.uint8)
    for r in range(8):                       # 课桌横线
        y = int(h * 0.35 + r * (h * 0.55 / 8))
        cv2.line(img, (0, y), (w, y), (110, 110, 110), 2)
    cols = max(1, persons // 5)
    for i in range(persons):
        rr, cc = divmod(i, cols)
        cx = int(w * 0.08 + cc * (w * 0.84 / max(1, cols - 1)))
        cy = int(h * 0.42 + rr * (h * 0.44 / 7))
        bw, bh = int(w * 0.035), int(h * 0.18)
        x0, y0 = cx - bw // 2, cy - bh // 2
        body = (int(rng.randint(60, 200)), int(rng.randint(60, 200)),
                int(rng.randint(60, 200)))
        cv2.ellipse(img, (cx, y0 + int(bh * 0.72)), (bw // 2, int(bh * 0.28)),
                    0, 0, 360, body, -1)
        cv2.circle(img, (cx, y0 + int(bh * 0.18)), max(4, bw // 4),
                   (200, 190, 180), -1)
    return img


def _load_frames(args, limit):
    """按输入源返回 [(名称, frame)] 列表。"""
    out = []
    if args.video:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            print("[对比] 打不开视频：%s" % args.video)
            return out
        idx = 0
        while len(out) < limit:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            if idx % max(1, args.stride) == 0:
                out.append(("帧%d" % idx, frame))
        cap.release()
        return out
    if args.images:
        files = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
            files.extend(glob.glob(os.path.join(args.images, ext)))
            files.extend(glob.glob(os.path.join(args.images, ext.upper())))
        files = sorted(set(files))[:limit]
        for f in files:
            img = cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append((os.path.basename(f), img))
        return out
    # 合成
    for i in range(limit):
        out.append(("合成%d" % i, _make_frame(seed=i, persons=args.synth_persons)))
    return out


def _load_gt(gt_dir, image_name):
    """读取 YOLO txt 标注 -> [(x,y,w,h)] 像素框；找不到返回 None。"""
    if not gt_dir:
        return None
    base = os.path.splitext(image_name)[0]
    path = os.path.join(gt_dir, base + ".txt")
    if not os.path.exists(path):
        return None
    boxes = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                boxes.append([float(v) for v in parts[1:5]])
    except OSError:
        return None
    return boxes


# ---------------------------------------------------------------------
#  单个后端的评测
# ---------------------------------------------------------------------
def run_detector(name, kwargs, frames, args, gt_dir=None, backend="", unavailable=None):
    """跑一个后端，返回指标 dict。

    backend：这一行实际跑的检测链（由 build_detectors 按 person_detect 的
             真实优先级判定，如 "YOLO11n-ONNX"/"HOG+SVM"）。
    unavailable：非 None 表示本次不可用（缺模型/缺依赖），直接标记"未参与"，
             绝不把 HOG 的耗时/人数冒充成 YOLO 的结果。
    """
    if unavailable:
        return {"name": name, "ok": False, "backend": backend or "-",
                "error": unavailable}
    t0 = time.perf_counter()
    try:
        tracker = pd.PersonTracker(**kwargs)
    except Exception as e:
        return {"name": name, "ok": False, "backend": backend or "-",
                "error": "初始化失败：%s" % e}
    init_s = time.perf_counter() - t0

    # 预热（让 DNN 后端完成图优化，避免把首次开销算进均值）
    warm = frames[: min(3, len(frames))]
    for _, f in warm:
        try:
            tracker.analyze_only(f, min_hits=1)
        except Exception as e:
            return {"name": name, "ok": False, "backend": backend,
                    "error": "推理失败：%s" % e}
    tracker.stop()

    lat, counts, id_switch_total, det_total = [], [], 0, 0
    prev_ids = None
    tp = fp = fn = 0
    iou_sum, iou_n = 0.0, 0

    for fname, frame in frames:
        t = time.perf_counter()
        persons, viol, _ = tracker.analyze_only(frame, min_hits=1)
        dt_ms = (time.perf_counter() - t) * 1000.0
        lat.append(dt_ms)
        counts.append(len(persons))
        det_total += len(persons)

        # 轨迹连续性：相邻帧的 pid 集合变化量（越小越稳）
        ids = set(p["pid"] for p in persons)
        if prev_ids is not None:
            id_switch_total += len(ids - prev_ids)
        prev_ids = ids

        # 精度（有标注时）
        gts = _load_gt(gt_dir, fname)
        if gts is not None:
            fh, fw = frame.shape[:2]
            gboxes = [(g[0] * fw - g[2] * fw / 2, g[1] * fh - g[3] * fh / 2,
                       g[2] * fw, g[3] * fh) for g in gts]
            used = set()
            for p in persons:
                best, bi = 0.0, -1
                for i, g in enumerate(gboxes):
                    if i in used:
                        continue
                    v = _iou(p["rect"], g)
                    if v > best:
                        best, bi = v, i
                if best >= 0.5:
                    tp += 1
                    used.add(bi)
                    iou_sum += best
                    iou_n += 1
                else:
                    fp += 1
            fn += max(0, len(gboxes) - len(used))

    med = _mean(lat)
    res = {
        "name": name, "ok": True, "backend": backend,
        "init_ms": init_s * 1000.0,
        "lat_mean": _mean(lat), "lat_med": float(np.median(lat)) if lat else 0.0,
        "lat_p95": _pct(lat, 95), "lat_min": min(lat) if lat else 0.0,
        "count_mean": _mean(counts),
        "count_min": min(counts) if counts else 0,
        "count_max": max(counts) if counts else 0,
        "det_total": det_total,
        "id_churn": id_switch_total,
    }
    if tp or fp or fn:
        prec = tp / float(tp + fp) if (tp + fp) else 0.0
        rec = tp / float(tp + fn) if (tp + fn) else 0.0
        res.update({"tp": tp, "fp": fp, "fn": fn,
                    "precision": prec, "recall": rec,
                    "f1": (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0,
                    "mean_iou": (iou_sum / iou_n) if iou_n else 0.0})
    # 参考：02 默认 AI 分析间隔 500ms（标准模式）下，单路占用
    res["cpu_share"] = min(1.0, res["lat_med"] / 500.0)
    del med
    return res


# ---------------------------------------------------------------------
#  后端可用性自检
# ---------------------------------------------------------------------
def selftest(args):
    print("=" * 78)
    print("%s 检测后端可用性自检" % PROJECT_TAG)
    print("=" * 78)
    print("OpenCV      : %s" % cv2.__version__)
    print("检测链      : %s" % describe_chain())
    print()

    path = yolo_model_path()
    rows = [("HOG+SVM", "可用（OpenCV 自带，永远可用）", "-")]
    if yolo_disabled():
        rows.append(("YOLO11n-ONNX", "已停用（EXAM_YOLO_DISABLE=1）", "-"))
    elif not onnxruntime_ok():
        rows.append(("YOLO11n-ONNX", "缺依赖（pip install onnxruntime）", "-"))
    elif os.path.exists(path):
        rows.append(("YOLO11n-ONNX", "可用（主力检测器）", os.path.basename(path)))
    else:
        rows.append(("YOLO11n-ONNX", "缺模型（运行 工具_YOLO模型导出.py 生成）", "-"))
    proto, _ = ssd_model_paths()
    if proto:
        rows.append(("MobileNet-SSD", "可用（仅在无 YOLO 时启用）",
                     os.path.basename(proto)))
    else:
        rows.append(("MobileNet-SSD", "缺模型（需 .prototxt + .caffemodel）", "-"))

    print("%-16s %-46s %s" % ("后端", "状态", "文件"))
    print("-" * 78)
    for a, b, c in rows:
        print("%-16s %-46s %s" % (a, b, c))
    print()
    print("检测链查找顺序（person_detect 内部，无需在界面上配置）：")
    print("  1) 环境变量 EXAM_YOLO_MODEL 指定的 ONNX")
    print("  2) 本目录 models\\yolo11n.onnx（默认；当前%s）"
          % ("存在" if os.path.exists(path) else "不存在"))
    print("  3) 本目录 MobileNetSSD_deploy.prototxt + .caffemodel（兼容保留）")
    print("  4) 纯 HOG+SVM 兜底")
    print()
    print("获取权重（本脚本不自动下载，请自行确认来源后手动放置）：")
    print("  YOLO11n ONNX : 首选 `python 工具_YOLO模型导出.py`（从官方 .pt 导出）")
    print("                 也可 `python 下载模型.py`（官方 release 下载 + SHA256 校验）")
    print("  MobileNet-SSD: OpenCV 官方 samples/dnn 下的 "
          "MobileNetSSD_deploy.prototxt + .caffemodel")
    print()
    if yolo_disabled() or not onnxruntime_ok() or not os.path.exists(path):
        print("[提示] 当前 YOLO 不可用，对比实验的 YOLO 行会标记为"
              "「未参与」，只跑 HOG。装好模型/依赖后重跑本命令即可。")
    return 0


# ---------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------
def build_detectors(args):
    """构造要对比的后端参数表 -> [(显示名, kwargs, 实际检测链, 不可用原因)]。

    新版 person_detect 没有"指定后端族"的参数（model_kind/prefer_gpu/
    use_onnxruntime 都已移除）：检测链固定按 YOLO11n ONNX -> MobileNet-SSD
    -> HOG 兜底 自动选择。所以这里用"给不给模型"来控制走哪条链：
      · HOG 行    ：把 yolo_model_path 指向一个不存在的文件 -> 强制 HOG 兜底；
      · YOLO 行   ：显式传存在的模型路径（--model / EXAM_YOLO_MODEL 可覆盖）。
    """
    dets = []
    want = [s.strip().lower() for s in (args.detectors or "all").split(",") if s.strip()]
    if "all" in want:
        want = ["hog", "ssd", "yolo"]
    base = dict(boundary_ratio=0.92, max_input_dim=1280, min_hits=1,
                confirm_frames=4, match_dist_ratio=0.12)
    no_model = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "models", "_no_such_model_.onnx")
    proto, _ = ssd_model_paths()

    if "hog" in want:
        if proto:
            reason = ("本目录存在 MobileNet-SSD 模型，检测链会先启用它，"
                      "无法强制纯 HOG（要测纯 HOG 请先把该模型移出本目录）")
        else:
            reason = None
        dets.append(("HOG+SVM（兜底链）",
                     dict(base, dense_mode=True, yolo_model_path=no_model),
                     "HOG+SVM", reason))
    if "ssd" in want:
        dets.append(("MobileNet-SSD",
                     dict(base, yolo_model_path=no_model),
                     "MobileNet-SSD",
                     None if proto else
                     "缺 MobileNet-SSD 模型（需 MobileNetSSD_deploy.prototxt "
                     "+ .caffemodel 放在本目录）"))
    if "yolo" in want:
        path = args.model or yolo_model_path()
        if yolo_disabled():
            reason = "YOLO 已被 EXAM_YOLO_DISABLE=1 停用"
        elif not onnxruntime_ok():
            reason = "未安装 onnxruntime（pip install onnxruntime）"
        elif not os.path.exists(path):
            reason = ("缺模型 %s（可运行 工具_YOLO模型导出.py 生成）"
                      % os.path.basename(path))
        else:
            reason = None
        dets.append(("YOLO11n-ONNX（零检出帧叠加 HOG 兜底）",
                     dict(base, yolo_model_path=path, yolo_conf=args.conf,
                          yolo_input_dim=int(args.input_dim or 640)),
                     "YOLO11n-ONNX", reason))
    return dets


def _resize_to_input_dim(frames, input_dim):
    """按 02 界面真实的 AI 分析长边预缩放，保证各后端在同一起跑线比。

    否则会在 1280px 上比 HOG、在 640px 上比 YOLO，耗时不可比。
    """
    if not input_dim:
        return frames, None
    out = []
    fh, fw = frames[0][1].shape[:2]
    long_edge = max(fh, fw)
    if long_edge <= input_dim:
        return frames, (fw, fh)
    s = input_dim / float(long_edge)
    nw, nh = int(round(fw * s)), int(round(fh * s))
    for name, f in frames:
        out.append((name, cv2.resize(f, (nw, nh))))
    return out, (nw, nh)


def main():
    ap = argparse.ArgumentParser(
        description="%s 检测模型对比实验（YOLO11n-ONNX / MobileNet-SSD / HOG+SVM）"
                    % PROJECT_TAG)
    ap.add_argument("--selftest", action="store_true", help="只做检测链可用性自检")
    ap.add_argument("--video", help="考场录像文件")
    ap.add_argument("--images", help="现场抓帧目录")
    ap.add_argument("--gt", help="YOLO txt 标注目录（与图片同名）")
    ap.add_argument("--frames", type=int, default=60, help="最多评测多少帧")
    ap.add_argument("--stride", type=int, default=1, help="视频抽帧间隔")
    ap.add_argument("--detectors", default="all",
                    help="要对比的后端：all / hog / ssd / yolo（逗号分隔）")
    ap.add_argument("--model", help="YOLO onnx 路径（默认 models\\yolo11n.onnx，"
                                    "也可用环境变量 EXAM_YOLO_MODEL 指定）")
    ap.add_argument("--ssd-model", dest="ssd_model",
                    help="仅用于可用性判定：MobileNet prototxt 路径")
    ap.add_argument("--conf", type=float, default=0.25, help="YOLO 置信度阈值")
    ap.add_argument("--synth-persons", dest="synth_persons", type=int, default=40,
                    help="无素材时合成帧的人数")
    ap.add_argument("--input-dim", dest="input_dim", type=int, default=640,
                    help="AI 分析长边（默认 640，与界面标准/密集/节能三档一致；"
                         "YOLO11n 输入固定 640，长边再大也不会更准）")
    ap.add_argument("--outdir", default=None, help="报告输出目录")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args)

    print("=" * 78)
    print("%s 检测模型对比实验" % PROJECT_TAG)
    print("=" * 78)

    frames = _load_frames(args, args.frames)
    if not frames:
        print("[对比] 没有可用帧。请用 --video / --images 指定素材，"
              "或省略以使用合成帧（仅速度基准）。")
        return 2
    src = ("合成帧（仅速度基准，不可用于精度结论）" if not (args.video or args.images)
           else (args.video or args.images))
    raw_w, raw_h = frames[0][1].shape[1], frames[0][1].shape[0]
    frames, fsz = _resize_to_input_dim(frames, args.input_dim)
    print("素材      : %s" % src)
    print("原始分辨率: %dx%d   ->  AI 分析分辨率 %dx%d（长边 %d，与界面同口径）"
          % (raw_w, raw_h, fsz[0], fsz[1], args.input_dim))
    print("帧数      : %d" % len(frames))
    print("推理后端  : %s" % describe_chain())
    print("精度标注  : %s" % (args.gt if args.gt else "未提供（只出速度/稳定性）"))
    print()

    results = []
    for name, kwargs, backend, reason in build_detectors(args):
        print("跑 %-22s ..." % name, end="", flush=True)
        r = run_detector(name, kwargs, frames, args, gt_dir=args.gt,
                         backend=backend, unavailable=reason)
        results.append(r)
        if r["ok"]:
            print(" 完成（%s，中位 %.1f ms）" % (r["backend"], r["lat_med"]))
        else:
            print(" 跳过（%s）" % r["error"])

    ok = [r for r in results if r.get("ok")]
    print()
    if not ok:
        print("没有任何后端跑成功。先执行 --selftest 看缺什么。")
        return 3

    # ---- 表 1：速度与稳定性 ----
    print("=" * 78)
    print("表1  速度与稳定性（min_hits=1，逐帧 analyze_only）")
    print("=" * 78)
    hdr = "%-22s %-26s %8s %8s %8s %9s %8s %9s"
    print(hdr % ("后端", "实际使用", "耗时中位", "P95", "人数均值",
                 "人数波动", "编号变动", "单路占比"))
    print("-" * 78)
    for r in ok:
        print(hdr % (r["name"], r["backend"],
                     "%.1fms" % r["lat_med"], "%.1fms" % r["lat_p95"],
                     "%.1f" % r["count_mean"],
                     "%d~%d" % (r["count_min"], r["count_max"]),
                     r["id_churn"], "%.0f%%" % (r["cpu_share"] * 100)))
    print("-" * 78)
    print("注：单路占比 = 耗时中位 / 500ms（02 标准模式 AI 分析间隔）。")
    print("    人数波动 = 逐帧检出人数的极差，越小说明检测越稳定。")
    print("    编号变动 = 相邻帧新增 pid 的累计次数，越小说明轨迹越连续。")

    # ---- 表 2：精度 ----
    if any("f1" in r for r in ok):
        print()
        print("=" * 78)
        print("表2  精度（IoU>=0.5 记命中）")
        print("=" * 78)
        hdr2 = "%-22s %8s %8s %8s %8s %8s %6s %6s %6s"
        print(hdr2 % ("后端", "精确率", "召回率", "F1", "平均IoU",
                      "耗时中位", "TP", "FP", "FN"))
        print("-" * 78)
        for r in ok:
            if "f1" not in r:
                continue
            print(hdr2 % (r["name"], "%.3f" % r["precision"],
                          "%.3f" % r["recall"], "%.3f" % r["f1"],
                          "%.3f" % r["mean_iou"], "%.1fms" % r["lat_med"],
                          r["tp"], r["fp"], r["fn"]))
        print("-" * 78)

    # ---- 结论文本 ----
    fastest = min(ok, key=lambda r: r["lat_med"])
    steadiest = min(ok, key=lambda r: (r["count_max"] - r["count_min"], r["id_churn"]))
    print()
    print("=" * 78)
    print("自动结论")
    print("=" * 78)
    print("· 最快：%s（中位 %.1f ms）" % (fastest["name"], fastest["lat_med"]))
    print("· 最稳：%s（人数极差 %d，编号变动 %d）"
          % (steadiest["name"], steadiest["count_max"] - steadiest["count_min"],
             steadiest["id_churn"]))
    if any("f1" in r for r in ok):
        best = max([r for r in ok if "f1" in r], key=lambda r: r["f1"])
        print("· 最准：%s（F1=%.3f，召回=%.3f）"
              % (best["name"], best["f1"], best["recall"]))
    else:
        print("· 精度：未提供标注（--gt），无法给出精确率/召回率。")
        print("  合成帧与真实监控差异过大，本表结论仅限速度与稳定性。")
    missing = [r for r in results if not r.get("ok")]
    for r in missing:
        print("· 未参与：%s —— %s" % (r["name"], r["error"]))

    # ---- 落盘 ----
    outdir = args.outdir or _DEFAULT_REPORT_DIR
    try:
        os.makedirs(outdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(outdir, "模型对比_%s.md" % stamp)
        with open(out, "w", encoding="utf-8") as f:
            f.write("# %s 检测模型对比报告\n\n" % PROJECT_TAG)
            f.write("- 生成时间：%s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            f.write("- 素材：%s\n" % src)
            f.write("- 原始分辨率：%dx%d\n" % (raw_w, raw_h))
            f.write("- AI 分析分辨率：%dx%d（长边 %d）\n"
                    % (fsz[0], fsz[1], args.input_dim))
            f.write("- 帧数：%d\n" % len(frames))
            f.write("- 推理后端：%s\n" % describe_chain())
            f.write("- OpenCV：%s\n\n" % cv2.__version__)
            f.write("## 速度与稳定性\n\n")
            f.write("| 后端 | 实际使用 | 耗时中位 | P95 | 人数均值 | 人数波动 | 编号变动 | 单路占比 |\n")
            f.write("|---|---|---|---|---|---|---|---|\n")
            for r in ok:
                f.write("| %s | %s | %.1fms | %.1fms | %.1f | %d~%d | %d | %.0f%% |\n"
                        % (r["name"], r["backend"], r["lat_med"], r["lat_p95"],
                           r["count_mean"], r["count_min"], r["count_max"],
                           r["id_churn"], r["cpu_share"] * 100))
            if any("f1" in r for r in ok):
                f.write("\n## 精度（IoU>=0.5）\n\n")
                f.write("| 后端 | 精确率 | 召回率 | F1 | 平均IoU | TP | FP | FN |\n")
                f.write("|---|---|---|---|---|---|---|---|\n")
                for r in ok:
                    if "f1" not in r:
                        continue
                    f.write("| %s | %.3f | %.3f | %.3f | %.3f | %d | %d | %d |\n"
                            % (r["name"], r["precision"], r["recall"], r["f1"],
                               r["mean_iou"], r["tp"], r["fp"], r["fn"]))
            f.write("\n## 原始数据\n\n```json\n")
            f.write(json.dumps(results, ensure_ascii=False, indent=2))
            f.write("\n```\n")
        print()
        print("报告已写入：%s" % out)
    except OSError as e:
        print()
        print("[对比] 报告写入失败（不影响上面的结果）：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
