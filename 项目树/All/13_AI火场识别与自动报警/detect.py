# -*- coding: utf-8 -*-
r"""
火/烟检测核心：多信号融合的传统视觉算法

为什么不是"跑个模型就完事"
    公开可用的火焰检测深度学习权重没法随代码一起交付（体积/授权/版本都不确定），
    所以默认后端是**可解释、可标定、零额外依赖**的多信号融合；
    如果本目录 models\ 放了 fire*.onnx，detector.py 会自动切到 ONNX 后端，
    本文件的视觉后端仍作为兜底与交叉验证。

五个信号（每个都单独可调，出问题时能定位到是哪一个在误报）
    1) 火色      HSV 红/橙/黄 高饱和高亮度区域
    2) 去肤色    YCbCr 肤色掩膜外扩后扣除 —— 人脸/手臂在 HSV 里也偏红黄，
                 不排掉的话"人脸一动就报警"
    3) 闪烁      火苗在抖：区域内亮度与面积的帧间抖动（静态红布/红灯没有）
    4) 增长      火在蔓延：区域面积随时间上升（灯光恒定、红布静止都没有）
    5) 烟雾      灰白 + 低饱和 + 极低边缘密度，且比基线明显增多（早期征兆）

安全取向：**宁可提示，不可漏报，但也不许乱报**。
    单帧永远不算数 —— 必须连续 confirm_frames 帧命中才出告警（见 FireDetector）。
"""
from __future__ import annotations

import math
import time

try:
    import cv2
except ImportError:                       # 允许无 OpenCV 时 import（自检会如实报缺依赖）
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

import config
import models

#: 判定用的固定权重（改这些等于改算法本身；现场只调 config 里的阈值）
W_COLOR_MAX = 0.50        # 火色（面积 × 鲜艳度）
W_SAT_MAX = 0.15          # 饱和度
W_VAL_MAX = 0.15          # 亮度
W_FLICKER_MAX = 0.25      # 闪烁
W_GROWTH_MAX = 0.15       # 增长
W_SMOKE_MAX = 0.30        # 只有烟没有火时的上限（早期提示）

#: 掩膜最小像素数（小图防止"3 个像素也算火"）
_MIN_MASK_PX = 4


class FireSignals(object):
    """一帧的中间量，便于界面显示"当前火色占比/闪烁强度"等调试信息。"""

    __slots__ = ("fire_ratio", "fire_px", "smoke_ratio", "smoke_px",
                 "vivid_ratio", "mean_v", "mean_s", "flicker", "growth",
                 "edge_density", "regions", "smoke_regions", "sharpness")

    def __init__(self):
        self.fire_ratio = 0.0
        self.fire_px = 0
        self.smoke_ratio = 0.0
        self.smoke_px = 0
        self.vivid_ratio = 0.0
        self.mean_v = 0.0
        self.mean_s = 0.0
        self.flicker = 0.0
        self.growth = 0.0
        self.edge_density = 0.0
        self.regions = []
        self.smoke_regions = []
        self.sharpness = 0.0


class VisionFireBackend(object):
    """多信号融合后端。analyze(frame) -> models.Detection"""

    name = "vision"

    def __init__(self, cfg=None):
        self.cfg = cfg or config.load_settings()
        self.reset()

    # ---------------- 状态 ----------------
    def reset(self):
        """清空时序历史（换摄像头/重启监控时调用）。"""
        self._hist_v = []          # 火区平均亮度的历史
        self._hist_area = []       # 火色面积占比的历史
        self._hist_smoke = []      # 烟雾面积占比的历史
        self._hist_edge = []       # 边缘密度历史
        self._frames = 0

    @property
    def cfg_now(self):
        return self.cfg

    # ---------------- 主入口 ----------------
    def analyze(self, frame):
        cfg = self.cfg
        if frame is None or np is None or cv2 is None:
            return models.ok_detection(self.name)
        if getattr(frame, "ndim", 0) != 3 or frame.shape[0] < 16 or frame.shape[1] < 16:
            return models.ok_detection(self.name)

        sig = self._measure(frame, cfg)
        score, parts, notes = self._score(sig, cfg)
        det = models.Detection(has_fire=score >= float(cfg["warning_threshold"]),
                               score=score, parts=parts,
                               fire_ratio=sig.fire_ratio, smoke_ratio=sig.smoke_ratio,
                               regions=sig.regions, smoke_regions=sig.smoke_regions,
                               sharpness=sig.sharpness, backend=self.name, notes=notes)
        self._push_history(sig, cfg)
        return det

    # ---------------- 测量 ----------------
    def _measure(self, frame, cfg):
        sig = FireSignals()
        h, w = frame.shape[:2]
        total = float(max(1, h * w))

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        # ---- 1) 火色掩膜（红/橙/黄 两段色相 + 高饱和高亮） ----
        s_min, v_min = int(cfg["hsv_s_min"]), int(cfg["hsv_v_min"])
        low = cv2.inRange(hsv, (0, s_min, v_min), (int(cfg["hsv_h_low_max"]), 255, 255))
        high = cv2.inRange(hsv, (int(cfg["hsv_h_red_min"]), s_min, v_min), (179, 255, 255))
        fire = cv2.bitwise_or(low, high)

        # ---- 2) 去肤色：YCbCr 上的人脸/手臂也落在红黄区间 ----
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)          # 通道序 Y, Cr, Cb
        Y, Cr, Cb = ycrcb[:, :, 0], ycrcb[:, :, 1], ycrcb[:, :, 2]
        skin = cv2.inRange(ycrcb,
                           (int(cfg["skin_y_min"]), int(cfg["skin_cr_min"]),
                            int(cfg["skin_cb_min"])),
                           (int(cfg["skin_y_max"]), int(cfg["skin_cr_max"]),
                            int(cfg["skin_cb_max"])))
        k = int(cfg["skin_dilate_px"])
        if k >= 3:
            kk = k if k % 2 == 1 else k + 1
            skin = cv2.dilate(skin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
        fire = cv2.bitwise_and(fire, cv2.bitwise_not(skin))

        # ---- 形态学：开运算去椒盐，闭运算把火苗连成片 ----
        mk = int(cfg["morph_kernel"])
        if mk >= 3:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
            fire = cv2.morphologyEx(fire, cv2.MORPH_OPEN, kernel)
            fire = cv2.morphologyEx(fire, cv2.MORPH_CLOSE, kernel)

        fire_px = int(cv2.countNonZero(fire))
        sig.fire_px = fire_px
        sig.fire_ratio = fire_px / total

        # 选出足够大的连通块（小块不算火）
        min_px = max(int(cfg["min_fire_px"]), int(_MIN_MASK_PX))
        regions, keep_mask = self._regions_of(fire, min_px, int(cfg["max_regions"]))
        sig.regions = regions
        if keep_mask is not None:
            use = keep_mask
        else:
            use = fire
        used_px = int(cv2.countNonZero(use)) if use is not None else 0
        sig.fire_ratio = used_px / total

        # 鲜艳度：保留区域里的平均饱和度与亮度（归一化 0~1）
        if used_px > 0:
            sig.mean_s = float(cv2.mean(S, mask=use)[0]) / 255.0
            sig.mean_v = float(cv2.mean(V, mask=use)[0]) / 255.0
            sat_need = max(1.0, float(s_min))
            val_need = max(1.0, float(v_min))
            sig.vivid_ratio = max(0.0, min(1.0, ((sig.mean_s * 255.0) / sat_need - 1.0) / 0.35)) * \
                              max(0.0, min(1.0, ((sig.mean_v * 255.0) / val_need - 1.0) / 0.30))

        # ---- 3) 清晰度（糊成一片时任何判据都不可靠，如实记录） ----
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sig.sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # ---- 5) 烟雾：灰白 + 低饱和 + 极低边缘密度 ----
        if cfg["smoke_enable"]:
            smoke = cv2.inRange(hsv, (0, 0, int(cfg["smoke_v_min"])),
                                (179, int(cfg["smoke_s_max"]), int(cfg["smoke_v_max"])))
            if mk >= 3:
                smoke = cv2.morphologyEx(smoke, cv2.MORPH_OPEN, kernel)
            smoke_px = int(cv2.countNonZero(smoke))
            sig.smoke_px = smoke_px
            sig.smoke_ratio = smoke_px / total
            edges = cv2.Canny(gray, 60, 160)
            sig.edge_density = float(cv2.countNonZero(edges)) / total
            sregions, _ = self._regions_of(smoke, max(min_px, int(total * 0.01)), 4)
            sig.smoke_regions = sregions

        # ---- 3)+4) 时序：闪烁与增长 ----
        sig.flicker, sig.growth = self._temporal(sig, cfg)
        return sig

    @staticmethod
    def _regions_of(mask, min_px, max_regions):
        """返回 ([(x,y,w,h,area_px)...], 仅含合格块的掩膜)"""
        if mask is None or cv2 is None:
            return [], None
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        keep = []
        for c in contours:
            a = int(cv2.contourArea(c))
            if a >= min_px:
                keep.append((a, cv2.boundingRect(c)))
        keep.sort(key=lambda t: -t[0])
        keep = keep[:max(1, max_regions)]
        if not keep:
            return [], None
        out = [(int(x), int(y), int(w), int(h), int(a)) for a, (x, y, w, h) in keep]
        clean = np.zeros_like(mask) if np is not None else None
        for _a, (x, y, w, h) in keep:
            clean[y:y + h, x:x + w] = 255      # 用外接矩形近似，够用于面积与标注
        clean = cv2.bitwise_and(clean, mask)   # 与原始掩膜求交，避免矩形把背景算进去
        return out, clean

    def _temporal(self, sig, cfg):
        """闪烁强度（0~1）与增长率（-1~1）。历史不足时返回 0（不猜）。"""
        win = int(cfg["flicker_window"])
        v_now = sig.mean_v * 255.0
        a_now = sig.fire_ratio

        # 火区平均亮度的帧间抖动：火苗抖，红布不抖
        flicker = 0.0
        if len(self._hist_v) >= 4:
            recent = self._hist_v[-(win - 1):] + [v_now]
            mean = sum(recent) / len(recent)
            var = sum((x - mean) ** 2 for x in recent) / len(recent)
            std = math.sqrt(max(0.0, var))
            base = max(1.0, float(cfg["flicker_v_min"]))
            flicker = max(0.0, min(1.0, (std / base) * 0.6))
            # 面积也要在抖（只亮度抖可能是自动曝光在动）
            areas = self._hist_area[-(win - 1):] + [a_now]
            if areas and max(areas) > 0:
                am = sum(areas) / len(areas)
                avar = sum((x - am) ** 2 for x in areas) / len(areas)
                astd = math.sqrt(max(0.0, avar))
                rel = astd / max(1e-6, am)
                need = max(1e-6, float(cfg["flicker_area_min"]))
                flicker = min(1.0, flicker + max(0.0, min(1.0, rel / need)) * 0.4)

        # 面积增长：最小二乘斜率相对均值
        growth = 0.0
        if len(self._hist_area) >= 4:
            areas = self._hist_area[-(win - 1):] + [a_now]
            n = len(areas)
            xs = list(range(n))
            mx = sum(xs) / n
            my = sum(areas) / n
            den = sum((x - mx) ** 2 for x in xs)
            if den > 0:
                slope = sum((xs[i] - mx) * (areas[i] - my) for i in range(n)) / den
                growth = slope / max(1e-6, my)
                growth = max(-1.0, min(1.0, growth * 4.0))   # 放大到可用区间
        return flicker, growth

    def _push_history(self, sig, cfg):
        win = int(cfg["flicker_window"])
        self._hist_v.append(sig.mean_v * 255.0)
        self._hist_area.append(sig.fire_ratio)
        self._hist_smoke.append(sig.smoke_ratio)
        self._hist_edge.append(sig.edge_density)
        cutoff = max(win * 3, 24)
        for h in (self._hist_v, self._hist_area, self._hist_smoke, self._hist_edge):
            if len(h) > cutoff:
                del h[:len(h) - cutoff]
        self._frames += 1

    # ---------------- 打分 ----------------
    def _score(self, sig, cfg):
        """
        返回 (score 0~1, parts 各信号贡献, notes 说明)。

        关键设计：**火色面积本身不足以定案**。
        一块静止的红布/红灯在 HSV 上与火几乎不可分，所以当区域性特征没有
        "闪烁 / 增长 / 高饱和高亮"任何一条支撑时，火色项要被打折 ——
        这就是静态红布进不了火警线、而真火能进的原因（见 tests/test_detect.py）。
        """
        parts = {}
        notes = []
        cfg_low = float(cfg["min_fire_ratio"])
        hot = sig.fire_ratio >= cfg_low

        # ---- 火色 ----
        color = 0.0
        temporal = 0.0
        oversize = False
        if hot:
            area_f = min(1.0, 0.35 + 0.65 * min(1.0, sig.fire_ratio / max(1e-6, cfg_low * 8.0)))
            vivid = 0.5 + 0.5 * sig.vivid_ratio       # 不够艳也不至于 0 分，但也不给满分
            color = W_COLOR_MAX * area_f * vivid
            oversize = sig.fire_ratio > float(cfg["max_fire_ratio"])

            # ---- 区域性证据：闪烁 / 增长 / 高饱和高亮 ----
            flicker_ev = min(1.0, sig.flicker / 0.45)
            growth_ev = max(0.0, min(1.0, sig.growth / 0.12))
            intense = 1.0 if (sig.mean_v >= 0.86 and sig.mean_s >= 0.60) else 0.0
            temporal = max(flicker_ev, growth_ev, intense)

            # 火色占比过大（多半是整片偏红：红灯直射、红色幕布糊满画面）：
            # 把面积权重压到"占比达标"那一档，不让"面积大"本身变成高分的理由。
            if oversize:
                color = min(color, W_COLOR_MAX * min(1.0, 0.35 + 0.65 * 1.0) * vivid * 0.30)
                notes.append("火色占比 %.0f%% 超出常规（多半是整片偏红），"
                             "按最高「疑似」上报，需人工确认" % (sig.fire_ratio * 100))

            # 没有闪烁/增长/突出亮度支撑时打折；且整体封顶在疑似线附近，
            # 保证静止红色物体最多只到"疑似"，不会进"确认火警"。
            if temporal < 0.2:
                color *= 0.30 if temporal <= 0.0 else 0.55
                notes.append("火色区域无闪烁、无增长、亮度/饱和度也不算突出："
                             "按静止红色物体处理（不计火警）")
            elif temporal < 0.35:
                color *= 0.55 + 0.45 * temporal
                notes.append("区域性证据偏弱（闪烁/增长不足），已打折")
        parts["火色"] = color

        # ---- 饱和度 / 亮度（仅在确有火色区域时计分） ----
        parts["饱和度"] = (W_SAT_MAX * max(0.0, min(1.0, sig.mean_s))) if hot else 0.0
        parts["亮度"] = (W_VAL_MAX * max(0.0, min(1.0, sig.mean_v))) if hot else 0.0

        # ---- 闪烁 / 增长 ----
        parts["闪烁"] = (W_FLICKER_MAX * min(1.0, flicker_ev)) if hot else 0.0
        if hot and sig.growth < float(cfg["growth_min"]):
            # 面积在明显收缩：可能是镜头前飘过的红色物体，扣一点分
            parts["增长"] = -W_GROWTH_MAX * 0.5
            notes.append("火色区域明显收缩（增长 %.2f）" % sig.growth)
        else:
            parts["增长"] = (W_GROWTH_MAX * growth_ev) if hot else 0.0

        # ---- 烟雾（早期征兆；只有烟没有火时**最多只到疑似**，不单独定火警） ----
        smoke = 0.0
        if cfg["smoke_enable"] and sig.smoke_ratio >= float(cfg["smoke_min_ratio"]):
            base, trend, enough = self._smoke_evidence(sig, cfg)
            rise = sig.smoke_ratio - base
            rising = max(sig.smoke_ratio - max(self._hist_smoke[-8:] or [0.0]), 0.0)
            # 必须"比平静基线高且仍在上升"，否则就是把灰墙/白墙当烟
            if enough and sig.edge_density <= float(cfg["smoke_edge_max"]) and \
                    rise > float(cfg["smoke_rise_ratio"]) and \
                    trend + rising > float(cfg["smoke_rise_ratio"]):
                area_f = min(1.0, sig.smoke_ratio / max(1e-6, float(cfg["smoke_min_ratio"]) * 3.0))
                smoke = W_SMOKE_MAX * (0.5 + 0.5 * area_f)
                notes.append("烟雾较基线上升 %.1f 个百分点且仍在增加" % (rise * 100))
        parts["烟雾"] = smoke

        fire_score = sum(v for k, v in parts.items() if k != "烟雾" and v > 0)
        fire_score = max(0.0, min(1.0, fire_score))
        smoke_score = max(0.0, min(1.0, smoke))

        if fire_score > 0:
            score = min(1.0, fire_score + smoke_score * 0.25)   # 有火又有烟：互相印证
        elif smoke_score > 0:
            # 只有烟雾：封顶在"疑似"线附近，绝不让它单独触发确认火警
            cap = max(float(cfg["warning_threshold"]), 0.45)
            score = min(smoke_score, cap)
        else:
            score = 0.0

        # ---- 最终封顶：没有闪烁/增长支撑的"静态火色"只允许到疑似级**以下** ----
        # 这条是"红布/红灯不报警"的最后一道闸。
        # 注意上限取 0.8×疑似阈值：如果正好卡在疑似线上，静止红物会连续命中、
        # 照样把确认闸门喂满（实测 0.400 触发了 4 帧确认）—— 留出余量才真的挡得住。
        # 真火必然有闪烁或增长（火苗本就不稳定），所以不会被它挡掉。
        if hot and temporal < 0.25:
            max_static = float(cfg["warning_threshold"]) * 0.8 * (1.0 + 0.5 * temporal / 0.25)
            if score > max_static:
                notes.append("无时序证据（闪烁/增长不足），判定上限压到疑似线以下 %.2f"
                             % max_static)
                score = max_static

        if sig.fire_px and sig.fire_px < int(cfg["min_fire_px"]) and not hot:
            notes.append("火色像素少于阈值，未计入")
        return max(0.0, min(1.0, score)), parts, notes

    def _smoke_evidence(self, sig, cfg):
        """
        烟雾基线、趋势与"够不够判"。

        返回 (baseline, trend, enough)：
          baseline 取历史里**最平静**那一档的中位数（烟雾会累积，不能用上一帧当基线，
                   否则烟起来后 rise≈0，永远不报 —— 这是早期版本的真实缺陷）；
          trend    最近几帧的上升量（正=在起烟，负=在散）；
          enough   历史帧数够不够（不够时**一律不判烟雾**，宁可晚一帧也不要误报）。
        """
        hist = self._hist_smoke
        win = max(4, int(cfg["flicker_window"]))
        if len(hist) < win:
            return 0.0, 0.0, False
        past = hist[:-1] or [0.0]
        quiet = sorted(past)[:max(1, len(past) // 10)]
        base = quiet[len(quiet) // 2]
        recent = past[-win:]
        trend = sig.smoke_ratio - (sum(recent) / len(recent))
        return base, trend, True


# ============================================================ 确认闸门
class FireDetector(object):
    """
    带"确认/解除"闸门的检测器：后端每帧给分，本类负责**去抖动**。

    单帧高分不算火警 —— 闪光灯、车灯扫过、红色衣服飘过都会造成单帧高分。
    必须连续 confirm_frames 帧 ≥ 阈值才出告警；已报警后要连续 clear_frames 帧
    无火才判解除（避免火还在就反复"报警-解除"刷屏）。
    """

    def __init__(self, backend=None, cfg=None, on_alarm=None, on_clear=None):
        self.cfg = cfg or config.load_settings()
        self.backend = backend or VisionFireBackend(self.cfg)
        self.on_alarm = on_alarm            # 回调(det, event)：确认火警时
        self.on_clear = on_clear            # 回调(event)：解除时
        self.reset()

    def reset(self):
        self._hit = 0                        # 连续命中帧数
        self._miss = 0                       # 连续未命中帧数
        self._active = None                  # 当前进行中的事件
        self.last_detection = models.ok_detection(getattr(self.backend, "name", "vision"))
        self._last_alarm_epoch = 0.0         # 上次建事件的时刻（冷却用）
        if hasattr(self.backend, "reset"):
            self.backend.reset()

    # ---------------- 查询 ----------------
    @property
    def active_event(self):
        return self._active

    @property
    def hit_streak(self):
        return self._hit

    def status_text(self):
        if self._active is not None:
            return "%s（%s，峰值 %.2f）" % (
                self._active.level_name(), self._active.status_text(),
                self._active.peak_score)
        if self._hit:
            return "疑似特征累计 %d/%d 帧" % (self._hit, int(self.cfg["confirm_frames"]))
        return "监控中，未发现火情"

    # ---------------- 主入口 ----------------
    def update(self, frame, camera_name="", camera_url=""):
        """
        喂一帧，返回本次的 Detection；需要建/解除事件时同步触发回调。
        本方法不做任何 IO（截图/录像由调用方在回调里做），所以可以在采集线程里直接调。
        """
        cfg = self.cfg
        det = self.backend.analyze(frame)
        self.last_detection = det
        need = int(cfg["confirm_frames"])
        clear_need = int(cfg["clear_frames"])

        if det.level is not None:            # 达到疑似线或火警线
            self._hit += 1
            self._miss = 0
        else:
            self._miss += 1
            if self._hit:
                self._hit = 0                # 中间断了就重新计数（防"隔帧凑数"）

        # ---- 确认：连续命中达标 ----
        if self._hit >= need:
            if self._active is None:
                now = models.now_epoch()
                cooldown = float(cfg["event_cooldown_sec"])
                if now - self._last_alarm_epoch >= cooldown:
                    ev = self._open_event(det, camera_name, camera_url)
                    self._active = ev
                    self._last_alarm_epoch = now
                    if self.on_alarm:
                        self.on_alarm(det, ev)
            else:
                self._active.observe(det)

        # ---- 解除：连续无火达标 ----
        if self._active is not None and self._miss >= clear_need:
            ev = self._active
            ev.clear()
            self._active = None
            self._hit = 0
            if self.on_clear:
                self.on_clear(ev)

        return det

    def _open_event(self, det, camera_name, camera_url):
        return models.FireEvent(camera_name=camera_name, camera_url=camera_url,
                                level=det.level or config.LEVEL_ALARM,
                                score=det.score, reason=det.reason_text(),
                                parts=det.parts, fire_ratio=det.fire_ratio,
                                smoke_ratio=det.smoke_ratio, regions=det.regions)

    # ---------------- 人工介入 ----------------
    def force_clear(self):
        """人工把当前事件判为已解除（例如确认无火）。"""
        if self._active is None:
            return None
        ev = self._active
        ev.clear(force=True)
        self._active = None
        self._hit = 0
        if self.on_clear:
            self.on_clear(ev)
        return ev


# ============================================================ 可视化
def draw_overlay(frame, det, cfg=None, extra_lines=None):
    """
    在帧上画检测结果（界面预览与事件标注图共用同一套画法，避免"看到的"和"存的"不一致）。
    返回新帧（不修改入参）。
    """
    if frame is None or cv2 is None or np is None:
        return frame
    out = frame.copy()
    cfg = cfg or config.load_settings()
    h, w = out.shape[:2]
    level = det.level if det is not None else None
    color = (0, 140, 255)
    if level == config.LEVEL_ALARM:
        color = (0, 0, 255)
    elif level == config.LEVEL_WARNING:
        color = (0, 165, 255)

    # 火色区域
    for r in (det.regions if det else []) or []:
        x, y, bw, bh = r[0], r[1], r[2], r[3]
        cv2.rectangle(out, (x, y), (x + bw, y + bh), color, 2)
    # 烟雾区域
    for r in (det.smoke_regions if det else []) or []:
        x, y, bw, bh = r[0], r[1], r[2], r[3]
        cv2.rectangle(out, (x, y), (x + bw, y + bh), (180, 180, 180), 1)

    if det is not None and det.has_fire:
        text = "%s %.2f" % (det.level_name(), det.score)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(out, (6, 6), (16 + tw, 16 + th), (0, 0, 0), -1)
        cv2.putText(out, text, (10, 12 + th), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    color, 2, cv2.LINE_AA)

    lines = list(extra_lines or [])
    if det is not None:
        lines.append("火色 %.2f%%  烟 %.2f%%  闪烁 %.2f  增长 %+.2f"
                     % (det.fire_ratio * 100, det.smoke_ratio * 100,
                        det.parts.get("闪烁", 0.0) / max(1e-6, W_FLICKER_MAX),
                        (det.parts.get("增长", 0.0)) / max(1e-6, W_GROWTH_MAX)))
    y0 = h - 10 - 18 * max(0, len(lines) - 1)
    for i, ln in enumerate(lines):
        cv2.putText(out, ln, (10, y0 + i * 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, ln, (10, y0 + i * 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return out


def describe_backend(backend):
    """给界面/自检看的一行说明。"""
    name = getattr(backend, "name", "?")
    if name == "onnx":
        return "ONNX 模型（%s）" % getattr(backend, "model_path", "?")
    return "多信号融合（火色+去肤色+闪烁+增长+烟雾，无需模型）"
