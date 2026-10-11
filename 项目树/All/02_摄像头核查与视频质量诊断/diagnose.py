# -*- coding: utf-8 -*-
"""
视频质量诊断核心（《摄像头核查与视频质量诊断.md》第四节第 2 条：多算法融合）

全部是**无参考**评价 + **基准图比对**，不依赖任何深度学习模型，
因此在纯 CPU 上也能秒级跑完一个机位。

图像质量（无参考）：
  BLUR          拉普拉斯方差（在统一 640 宽上计算，跨分辨率可比）
  UNDEREXPOSED  平均亮度 / 暗像素占比
  OVEREXPOSED   平均亮度 / 亮像素占比
  LOW_CONTRAST  亮度标准差
  COLOR_CAST    通道均值最大偏离（灰度世界假设偏离）
  STRIPES       行/列投影的周期分量占比（FFT 峰值/总能量）
  SNOW          中值滤波残差标准差（噪声估计）
  MOSAIC        8×8 块边界梯度 / 块内梯度
  FROZEN        连续帧间平均绝对差 ≈ 0

视野完整性（基准图比对 + 遮挡检测）：
  OCCLUDED      大面积低方差区域占比 + 边缘密度相对基准下降
  VIEW_OFFSET   相位相关求平移 -> 按水平视场角换算角度
  COVERAGE_LOW  有效纹理覆盖率低于下限（疑盲区）
"""
import os

import cv2
import numpy as np

import config


# ---------------- 中文路径安全读写（与 01 项目同一坑） ----------------
def imread_unicode(path):
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error):
        return None


def imwrite_unicode(path, img):
    ext = os.path.splitext(str(path))[1] or ".jpg"
    try:
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(str(path))
        return True
    except (OSError, ValueError, cv2.error):
        return False


def to_gray(img):
    if img is None:
        return None
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def work_size(gray, width=None):
    """统一到工作宽度，保证指标跨分辨率可比"""
    width = int(width or config.QUALITY["work_width"])
    h, w = gray.shape[:2]
    if w == width:
        return gray
    scale = width / float(w)
    return cv2.resize(gray, (width, max(1, int(round(h * scale)))),
                      interpolation=cv2.INTER_AREA)


# ---------------- 无参考指标 ----------------
def sharpness(gray):
    """拉普拉斯方差：越小越糊"""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure(gray):
    m = float(gray.mean())
    return {"mean": m,
            "dark_ratio": float((gray < 16).mean()),
            "bright_ratio": float((gray > 240).mean())}


def contrast(gray):
    return float(gray.std())


def color_cast(bgr):
    """通道均值最大偏离：灰度世界假设下应接近相等"""
    if bgr.ndim == 2:
        return 0.0
    means = [float(bgr[:, :, i].mean()) for i in range(3)]
    center = sum(means) / 3.0
    return max(abs(m - center) for m in means)


def _periodic_ratio(profile):
    """一维投影的周期分量占比（0~1），用于条纹检测"""
    p = np.asarray(profile, dtype=np.float32)
    if p.size < 32:
        return 0.0
    p = p - p.mean()
    if float(np.abs(p).max()) < 1e-6:
        return 0.0
    win = np.hanning(p.size).astype(np.float32)
    spec = np.abs(np.fft.rfft(p * win))
    if spec.size < 8:
        return 0.0
    total = float(spec[1:].sum())
    if total <= 1e-9:
        return 0.0
    lo = max(2, int(spec.size * 0.03))          # 排除直流与缓慢趋势
    peak = float(spec[lo:].max()) if spec.size > lo else 0.0
    return peak / total


def stripe_ratio(gray):
    return max(_periodic_ratio(gray.mean(axis=1)), _periodic_ratio(gray.mean(axis=0)))


def noise_level(gray):
    """中值滤波残差标准差：噪声估计（对边缘不敏感）"""
    resid = gray.astype(np.float32) - cv2.medianBlur(gray, 3).astype(np.float32)
    return float(resid.std())


def blockiness(gray):
    """
    块效应检测：块边界处的梯度应显著高于整体。

    关键点：**行、列两个方向的块相位必须一致**才算真块效应。
    压缩/马赛克产生的块在行列方向对齐；自然纹理即使某个方向偶然出现
    强相位，另一个方向也对不上，因此不会被误报（这是本指标的主要误报来源）。
    返回 1.0 表示无块效应，越大越明显。
    """
    g = gray.astype(np.float32)
    profs = [np.abs(np.diff(g, axis=1)).mean(axis=0),     # 列边界剖面
             np.abs(np.diff(g, axis=0)).mean(axis=1)]     # 行边界剖面
    best = 1.0
    for B in (8, 10, 16):
        if min(p.size for p in profs) < B * 6:
            continue
        stats = []
        ok = True
        for prof in profs:
            idx = np.arange(prof.size)
            means = np.array([prof[idx % B == p].mean() for p in range(B)])
            med = float(np.median(means))
            if med <= 1e-6:
                ok = False
                break
            stats.append(means / med)
        if not ok:
            continue
        # 同一相位下行、列都显著高出各自中位数，才算真块效应
        best = max(best, float(np.minimum(stats[0], stats[1]).max()))
    return best


def local_std(gray, win=9):
    g = gray.astype(np.float32)
    mean = cv2.blur(g, (win, win))
    sq = cv2.blur(g * g, (win, win))
    return np.sqrt(np.clip(sq - mean * mean, 0, None))


def local_uniform_ratio(gray, win=9, std_max=6.0):
    """大面积低方差区域占比（遮挡/喷涂会让画面变成一片纯色）"""
    return float((local_std(gray, win) < std_max).mean())


def texture_loss_ratio(cur_gray, base_gray, win=9, base_std_min=12.0, std_max=6.0):
    """
    纹理丢失率：只看**基准图里有纹理**的位置，现在是否变成了平坦区。
    这比"全局低方差占比"更能区分"遮挡"与"整体过暗/低对比"。
    返回 (ratio, 有效掩码占比)；基准本身太素时返回 (None, ratio)
    """
    if cur_gray.shape != base_gray.shape:
        return None, 0.0
    bstd = local_std(base_gray, win)
    cstd = local_std(cur_gray, win)
    mask = bstd >= base_std_min
    frac = float(mask.mean())
    if frac < 0.05:
        return None, frac
    return float((cstd[mask] < std_max).mean()), frac


def edge_density(gray):
    """边缘密度（0~1）：Sobel 梯度均值归一化"""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return float(mag.mean() / 255.0)


def frame_delta(prev_gray, gray):
    """帧间平均绝对差（0~255）"""
    if prev_gray is None or prev_gray.shape != gray.shape:
        return None
    return float(cv2.absdiff(prev_gray, gray).mean())


def translation_offset(cur_gray, base_gray):
    """
    相位相关求平移：返回 (dx, dy, response, angle_deg)
    response 太低说明两图差异过大（光照/内容变化），此时不做偏移判定，避免误报。
    """
    if base_gray is None or base_gray.shape != cur_gray.shape:
        return None
    a = np.float32(base_gray)
    b = np.float32(cur_gray)
    try:
        (dx, dy), resp = cv2.phaseCorrelate(a, b)
    except cv2.error:
        return None
    mag = float(np.hypot(dx, dy))
    width = float(cur_gray.shape[1])
    angle = mag / width * float(config.BASELINE["hfov_deg"])
    return {"dx": float(dx), "dy": float(dy), "shift_px": mag,
            "response": float(resp), "angle_deg": angle}


# ---------------- 诊断器 ----------------
class Diagnoser(object):
    def __init__(self, quality=None, baseline=None, frozen=None):
        self.q = dict(config.QUALITY)
        self.q.update(quality or {})
        self.b = dict(config.BASELINE)
        self.b.update(baseline or {})
        self.f = dict(config.FROZEN)
        self.f.update(frozen or {})

    # ---- 单帧指标 ----
    def frame_metrics(self, img, prev_gray=None):
        gray = to_gray(img)
        if gray is None:
            return None, None            # 调用方统一按二元组解包
        g = work_size(gray, self.q["work_width"])
        exp = exposure(g)
        m = {"sharpness": sharpness(g), "contrast": contrast(g),
             "mean_luma": exp["mean"], "dark_ratio": exp["dark_ratio"],
             "bright_ratio": exp["bright_ratio"], "color_cast": color_cast(img),
             "stripe_ratio": stripe_ratio(g), "noise": noise_level(g),
             "uniform_ratio": local_uniform_ratio(g),
             "edge_density": edge_density(g)}
        d = frame_delta(prev_gray, g)
        if d is not None:
            m["frame_delta"] = d
        # 块效应必须用**原始分辨率**算：块边界是像素栅格对齐的，
        # 一旦缩放到工作宽度，块边界会被插值抹掉（实测马赛克指标从 4.08 掉到 1.18）
        m["blockiness"] = blockiness(gray)
        return m, g

    # ---- 图像质量诊断（含多算法融合降误判） ----
    def quality_defects(self, img, metrics=None, baseline_metrics=None):
        if metrics is None:
            metrics, _ = self.frame_metrics(img)
        if metrics is None:
            return [DefectFactory.make("STREAM_FAIL", {}, "画面无法解码")]
        q = self.q
        bm = baseline_metrics or {}

        def rel(key, gain_key, floor):
            """有基准图时取"相对基准的倍数"与绝对下限的较大者"""
            base = bm.get(key)
            if base:
                return max(floor, float(base) * q[gain_key])
            return floor

        expo_bad = (metrics["mean_luma"] < q["dark_mean"]
                    or metrics["dark_ratio"] > q["dark_ratio"]
                    or metrics["mean_luma"] > q["bright_mean"]
                    or metrics["bright_ratio"] > q["bright_ratio"])

        # 这里只负责"列出所有命中项"，抑制与取舍统一交给 _fuse()（多算法融合）
        out = []
        if metrics["sharpness"] < q["blur_min"]:
            out.append(DefectFactory.make("BLUR", metrics,
                                          "清晰度 %.1f < %.1f"
                                          % (metrics["sharpness"], q["blur_min"])))
        if metrics["mean_luma"] < q["dark_mean"] or metrics["dark_ratio"] > q["dark_ratio"]:
            out.append(DefectFactory.make("UNDEREXPOSED", metrics,
                                          "平均亮度 %.1f，暗像素占比 %.2f"
                                          % (metrics["mean_luma"], metrics["dark_ratio"])))
        if metrics["mean_luma"] > q["bright_mean"] or \
                metrics["bright_ratio"] > q["bright_ratio"]:
            out.append(DefectFactory.make("OVEREXPOSED", metrics,
                                          "平均亮度 %.1f，亮像素占比 %.2f"
                                          % (metrics["mean_luma"], metrics["bright_ratio"])))
        if metrics["contrast"] < q["contrast_min"]:
            out.append(DefectFactory.make("LOW_CONTRAST", metrics,
                                          "亮度标准差 %.1f < %.1f"
                                          % (metrics["contrast"], q["contrast_min"])))
        if metrics["color_cast"] > q["color_cast_max"]:
            out.append(DefectFactory.make("COLOR_CAST", metrics,
                                          "通道均值偏离 %.1f > %.1f"
                                          % (metrics["color_cast"], q["color_cast_max"])))
        stripe_thr = rel("stripe_ratio", "stripe_gain", q["stripe_max"])
        if metrics["stripe_ratio"] > stripe_thr:
            out.append(DefectFactory.make("STRIPES", metrics,
                                          "周期分量占比 %.3f > %.3f"
                                          % (metrics["stripe_ratio"], stripe_thr)))
        noise_thr = rel("noise", "noise_gain", q["noise_max"])
        if metrics["noise"] > noise_thr:
            out.append(DefectFactory.make("SNOW", metrics,
                                          "噪声残差 %.2f > %.2f"
                                          % (metrics["noise"], noise_thr)))
        block_thr = rel("blockiness", "blockiness_gain", q["blockiness_max"])
        if metrics["blockiness"] > block_thr:
            out.append(DefectFactory.make("MOSAIC", metrics,
                                          "块边界梯度比 %.2f > %.2f"
                                          % (metrics["blockiness"], block_thr)))
        return out

    def frozen_defect(self, deltas):
        """连续多帧几乎无变化 -> 冻结帧"""
        vals = [d for d in deltas if d is not None]
        if len(vals) < self.f["frames"]:
            return None
        recent = vals[-(self.f["frames"]):]
        if all(v < self.f["diff_max"] for v in recent):
            return DefectFactory.make("FROZEN", {"frame_delta": max(recent)},
                                      "连续 %d 帧无变化（最大帧差 %.2f）"
                                      % (len(recent), max(recent)))
        return None

    # ---- 视野完整性（基准图比对） ----
    def vision_defects(self, img, baseline_gray, baseline_metrics=None,
                       quality_codes=None):
        gray = to_gray(img)
        if gray is None or baseline_gray is None:
            return [], {}
        g = work_size(gray, self.q["work_width"])
        base = work_size(baseline_gray, self.q["work_width"])
        if base.shape != g.shape:
            base = cv2.resize(base, (g.shape[1], g.shape[0]), interpolation=cv2.INTER_AREA)
        bm = baseline_metrics or {}
        qc = set(quality_codes or ())
        out, info = [], {}

        cur_uni = local_uniform_ratio(g)
        cur_edge = edge_density(g)
        base_edge = float(bm.get("edge_density") or 0.0)
        base_uni = float(bm.get("uniform_ratio") or 0.0)
        edge_drop = (1.0 - cur_edge / base_edge) if base_edge > 1e-6 else 0.0
        info.update({"uniform_ratio": cur_uni, "edge_density": cur_edge,
                     "baseline_edge_density": base_edge, "edge_drop": edge_drop})

        # 遮挡：只看"基准里有纹理的位置现在是否变平"（比全局低方差更抗光照变化）
        loss, cover = texture_loss_ratio(g, base)
        info["texture_loss"] = loss
        info["baseline_texture_cover"] = cover
        if loss is not None:
            if loss > self.b["occlude_uniform_ratio"] and \
                    edge_drop > self.b["occlude_edge_drop"]:
                out.append(DefectFactory.make(
                    "OCCLUDED", info,
                    "基准有纹理区域中 %.0f%% 已丢失纹理，边缘密度下降 %.0f%%"
                    % (loss * 100, edge_drop * 100)))

        # 视角偏移：相位相关换算角度
        off = translation_offset(g, base)
        if off:
            info.update(off)
            if off["response"] >= self.b["phase_min_response"]:
                if off["shift_px"] > self.b["offset_max_px"] and \
                        off["angle_deg"] > self.b["angle_max_deg"]:
                    out.append(DefectFactory.make(
                        "VIEW_OFFSET", info,
                        "平移 %.1f px（≈%.1f°）超过阈值 %.1f px / %.1f°"
                        % (off["shift_px"], off["angle_deg"],
                           self.b["offset_max_px"], self.b["angle_max_deg"])))

        # 覆盖完整性：有效纹理覆盖率偏低（疑盲区）
        if cur_uni > self.b["coverage_min"] and \
                cur_edge < self.b["coverage_min"] * max(base_edge, 1e-6):
            out.append(DefectFactory.make("COVERAGE_LOW", info,
                                          "有效纹理覆盖率偏低（边缘密度 %.4f，基准 %.4f）"
                                          % (cur_edge, base_edge)))
        return out, info

    # ---- 多算法融合：抑制同因多报与互相干扰，降低误判（第四节第 2 条） ----
    def _fuse(self, defects):
        codes = {d.code for d in defects}
        by = {d.code: d for d in defects}
        drop = set()
        # 过暗/过曝时清晰度、对比度、块效应、纹理都不可靠，只报曝光问题
        if {"UNDEREXPOSED", "OVEREXPOSED"} & codes:
            drop |= {"BLUR", "LOW_CONTRAST", "MOSAIC", "OCCLUDED", "COVERAGE_LOW"}
        # 遮挡边界容易被当成块效应
        if "OCCLUDED" in codes:
            drop |= {"MOSAIC"}
        # 条纹会抬高噪声估计
        if "STRIPES" in codes:
            drop |= {"SNOW"}
        # 模糊与低对比同时命中：对比度下降多为模糊的结果，保留更能定位问题的那个
        if "BLUR" in codes and "LOW_CONTRAST" in codes:
            c = by["BLUR"].metrics.get("contrast", 0)
            if c >= self.q["contrast_min"] * 0.8:
                drop.add("LOW_CONTRAST")
            else:
                drop.add("BLUR")
        # 画面几乎纯色（极低对比）时，块效应/遮挡/盲区都无从判断
        if "LOW_CONTRAST" in codes:
            c = by["LOW_CONTRAST"].metrics.get("contrast", 99)
            if c < self.q["contrast_min"] * 0.5:
                drop |= {"MOSAIC", "OCCLUDED", "COVERAGE_LOW"}
        return [d for d in defects if d.code not in drop]

    # ---- 总入口 ----
    def diagnose(self, img, baseline_gray=None, baseline_metrics=None, prev_deltas=None):
        """
        返回 (defects, metrics)
        未提供基准图时只做无参考质量诊断（并在指标里如实标注 baseline_missing）
        """
        metrics, g = self.frame_metrics(img)
        if metrics is None:
            return [DefectFactory.make("STREAM_FAIL", {}, "画面无法解码")], {}
        defects = self.quality_defects(img, metrics, baseline_metrics=baseline_metrics)
        if baseline_gray is not None:
            vis, info = self.vision_defects(img, baseline_gray, baseline_metrics)
            defects.extend(vis)
            metrics.update({k: v for k, v in info.items()})
            metrics["baseline_metrics"] = baseline_metrics or {}
        else:
            metrics["baseline_missing"] = 1
        if prev_deltas:
            fz = self.frozen_defect(prev_deltas)
            if fz:
                defects.append(fz)
        return self._fuse(defects), metrics


class DefectFactory(object):
    """避免循环 import：这里延迟取 models.Defect"""

    @staticmethod
    def make(code, metrics=None, detail=""):
        import models
        return models.Defect(code, metrics, detail)


# ---------------- 基准图指标（部署时采集一次） ----------------
def baseline_metrics(img, diagnoser=None):
    d = diagnoser or Diagnoser()
    m, g = d.frame_metrics(img)
    if m is None:
        return None, None
    return m, g
