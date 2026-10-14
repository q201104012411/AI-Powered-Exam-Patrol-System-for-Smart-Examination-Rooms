# -*- coding: utf-8 -*-
"""信号强度与持续占比提取。

把多模态观测转成两组量：
  · 强度（intensity，0~1）—— 此刻信号有多强；
  · 持续占比（persistence，0~1）—— 该信号在滑动窗口内出现了多久。

严格对应项目书《风险评分框架》表：

  1) 头部偏转        —— 持续偏离考试区域、角度超阈值（中）
  2) 头部姿态方差    —— 窗口内 yaw/pitch 波动大（中）
  3) 手部跨考生距离  —— 桌面下方手部关键点靠近（高）
  4) 违禁物体        —— 检测到手机/纸条等（极高）
  5) 姿态重复性      —— 低头-抬头等模式高频出现（中）
  6) 面部表情        —— 紧张与中性交替（辅助）

设计要点：本层【只产生强度与持续占比，不产生结论】。是否构成作弊由
时间维度的持续判定给出，避免“单帧说了算”。
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .features import FrameFeatures
from .scoring import (SIGNAL_KEYS, ExtractedSignals, SignalIntensities,
                      empty_persistence)
from .temporal import (count_alternations, head_pose_variance,
                       hand_interaction_streak, object_persistence,
                       repetition_rate)

# 头部朝向相对考试区域的严重度
RELATION_SEVERITY = {
    "on_paper": 0.0,
    "off_area": 0.7,
    "toward_neighbor": 1.0,
    "toward_back": 1.0,
    "unknown": 0.5,
}

# 违禁物品危险度（1.0 = 直接证据）
OBJECT_CRITICALITY = {
    "phone": 1.0, "手机": 1.0, "smartphone": 1.0,
    "note": 1.0, "纸条": 1.0, "小抄": 1.0, "cheat_sheet": 1.0,
    "earpiece": 0.9, "耳机": 0.9, "无线耳机": 0.9, "蓝牙耳机": 0.9,
    "device": 0.85, "电子设备": 0.85, "tablet": 0.85, "平板": 0.85,
    "watch": 0.7, "智能手表": 0.7,
    "book": 0.6, "书籍": 0.6, "资料": 0.6, "textbook": 0.6,
}
DEFAULT_OBJECT_CRITICALITY = 0.8

# 偏离达到“满强度”所需的超出角度（度）
_HEAD_FULL_EXCESS_DEG = 25.0
# 判定“该帧头部偏离成立”的最小强度
_ACTIVE_EPS = 0.05


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _ramp(value: float, lo: float, hi: float) -> float:
    """线性斜坡：value<=lo 记 0，value>=hi 记 1。"""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return _clamp01((value - lo) / (hi - lo))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _object_entry(item) -> Tuple[str, float]:
    """把违禁物体条目规范化为 (名称, 置信度)。"""
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return str(item[0]), float(item[1])
    if isinstance(item, dict):
        return (str(item.get("name", item.get("label", "unknown"))),
                float(item.get("confidence", item.get("conf", 1.0))))
    return str(item), 1.0


def frame_level_cues(frame: FrameFeatures, cfg: Config) -> List[str]:
    """返回该帧成立的【帧级（单帧）检测线索】名列表。

    这就是“帧级检测”的参考判据：任一单帧线索成立即判为可疑，
    不做持续时间、频率或上下文约束。项目书指出这种判法的误报率
    高达 15.2%，因为伸懒腰、短暂分心、调整坐姿都会触发误判。

    本引擎用它与时空建模结果对照，量化“多少帧级误报被抑制”。
    """
    cues: List[str] = []
    h = frame.head
    if (h.relation != "on_paper"
            and (abs(h.yaw_deg) > cfg.head_yaw_threshold_deg
                 or abs(h.pitch_deg) > cfg.head_pitch_threshold_deg)):
        cues.append("头部偏离")
    if frame.hand.below_desk:
        cues.append("手部离桌")
    if frame.prohibited_objects:
        cues.append("违禁物体")
    if abs(frame.posture.tilt_deg) > cfg.posture_tilt_threshold_deg:
        cues.append("坐姿倾斜")
    if frame.posture.leaning_on_desk or frame.posture.left_area:
        cues.append("姿态异常")
    return cues


def frame_level_cue(frame: FrameFeatures, cfg: Config) -> bool:
    """该帧是否存在任一帧级线索。"""
    return bool(frame_level_cues(frame, cfg))


class SignalExtractor:
    """从“当前帧 + 滑动窗口”提取信号强度与持续占比。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ================= 单帧级信号 =================
    # ---------------- 1) 头部偏转 ----------------
    def head_deviation(self, frame: FrameFeatures) -> float:
        """头部偏离考试区域且角度超阈值的强度。

        仅在朝向被判为离开考试区域时计分；正常低头书写
        （relation=on_paper）恒为 0，因此“长时间低头看自己试卷”
        不会产生作弊信号。
        """
        head = frame.head
        sev = RELATION_SEVERITY.get(head.relation, 0.5)
        if sev <= 0.0:
            return 0.0
        yaw_excess = max(0.0, abs(head.yaw_deg) - self.cfg.head_yaw_threshold_deg)
        pitch_excess = max(0.0, abs(head.pitch_deg)
                           - self.cfg.head_pitch_threshold_deg)
        excess = max(yaw_excess, pitch_excess)
        ramp = _clamp01(excess / _HEAD_FULL_EXCESS_DEG)
        return _clamp01(sev * ramp)

    # ---------------- 4) 违禁物体 ----------------
    def prohibited_object(self, frame: FrameFeatures) -> float:
        """检出手机/纸条等违禁物品的强度 —— 直接作弊证据信号。

        取当前帧所有检出物品中的最高危险度；带置信度时按置信度缩放。
        """
        best = 0.0
        for item in frame.prohibited_objects:
            name, conf = _object_entry(item)
            crit = OBJECT_CRITICALITY.get(str(name).lower().strip(),
                                          DEFAULT_OBJECT_CRITICALITY)
            conf_n = (_clamp01(conf / self.cfg.object_full_confidence)
                      if conf <= 1.0 else 1.0)
            best = max(best, crit * conf_n)
        return _clamp01(best)

    # ---------------- 姿态：单帧异常 ----------------
    def posture_abnormal_frame(self, frame: FrameFeatures) -> float:
        """单帧不规范坐姿强度：倾斜超阈 / 趴台面 / 离开台面区域。"""
        p = frame.posture
        tilt = _ramp(abs(p.tilt_deg), self.cfg.posture_tilt_threshold_deg,
                     self.cfg.posture_tilt_full_deg)
        lean = self.cfg.leaning_intensity if p.leaning_on_desk else 0.0
        left = self.cfg.left_area_intensity if p.left_area else 0.0
        return _clamp01(max(tilt, lean, left))

    # ================= 窗口级信号 =================
    # ---------------- 2) 头部姿态方差 ----------------
    def head_variance(self, window: Sequence[FrameFeatures]) -> float:
        """窗口内 yaw/pitch 波动强度。

        项目书要点：与可疑行为高度相关的是姿态的【方差】而非瞬时方向。
        方差达到 `head_variance_threshold` 记强度 1.0。
        """
        if len(window) < 2:
            return 0.0
        _, _, total = head_pose_variance(window)
        return _clamp01(total / self.cfg.head_variance_threshold)

    # ---------------- 3) 手部跨考生交互 ----------------
    def hand_interaction(self, window: Sequence[FrameFeatures]) -> float:
        """桌面下方手部关键点靠近邻座考生的强度。

        需同时满足（项目书口径）：
          · 手部在桌面下方或身体前倾；
          · 与邻座手部归一化距离小于阈值；
          · 【连续保持】 hand_interaction_frames 帧以上（单帧不算交互）。
        书写状态按 hand_write_mitigation 折减，降低正常书写误报。
        """
        if not window:
            return 0.0
        streak = hand_interaction_streak(window, self.cfg)
        if streak < self.cfg.hand_interaction_frames:
            return 0.0
        cur = window[-1]
        d = cur.hand.cross_distance
        if d is None:
            return 0.0
        thr = self.cfg.hand_interaction_distance
        closeness = _clamp01((thr - d) / thr) if thr > 0 else 0.0
        if cur.hand.doing_write:
            closeness *= self.cfg.hand_write_mitigation
        return closeness

    # ---------------- 5) 姿态重复性 ----------------
    def posture_repetition(self, window: Sequence[FrameFeatures]) -> float:
        """姿态重复性与不规范坐姿的合成强度。

        项目书“姿态与持续性异常”维度包含：多动行为（单位时间姿态变换
        次数超阈）、坐姿偏离角度超阈、趴在台面、离开台面区域。
        取“往复强度”与“单帧最差不规范坐姿”的较大者。
        """
        if not window:
            return 0.0
        rep = 0.0
        if len(window) >= 3:
            _, rate = repetition_rate(window, self.cfg.fps,
                                      self.cfg.repetition_min_amplitude)
            rep = (_clamp01(rate / self.cfg.posture_change_rate_threshold)
                   if self.cfg.posture_change_rate_threshold > 0 else 0.0)
        base = max(self.posture_abnormal_frame(f) for f in window)
        return _clamp01(max(rep, base))

    # ---------------- 6) 面部表情（辅助） ----------------
    def expression_aux(self, window: Sequence[FrameFeatures]) -> float:
        """面部表情辅助强度：紧张与中性【交替】变化。

        项目书口径：连续的表情紧张与中性交替变化可被纳入可疑度评分。
        """
        if len(window) < 2:
            return 0.0
        flags = [1 if f.expression_tense else 0 for f in window]
        tense_ratio = sum(flags) / len(flags)
        flips = sum(1 for a, b in zip(flags, flags[1:]) if a != b)
        flip_ratio = flips / max(1, len(flags) - 1)
        return _clamp01(0.5 * tense_ratio + 0.5 * flip_ratio)

    # ================= 持续占比 =================
    def persistence(self, window: Sequence[FrameFeatures]) -> Dict[str, float]:
        """各信号在窗口内的“持续占比”。

        这是项目书表格最后一行“持续时间”的量化形式：同一强度的信号，
        持续出现 2 秒与只出现 1 帧，持续占比相差一个数量级，
        经 multiplicative boost 后得分拉开，从而抑制瞬时误报。
        """
        out = empty_persistence()
        if not window:
            return out
        n = len(window)
        fill = n / self.cfg.window_frames  # 窗口未填满时按比例折减

        # 头部偏转：窗口内偏离成立的帧占比
        dev_active = sum(1 for f in window
                         if self.head_deviation(f) > _ACTIVE_EPS)
        dev_ratio = _clamp01(fill * dev_active / n)
        out["head_deviation"] = dev_ratio

        # 头部方差是窗口级量。其“持续占比”不应简单取窗口填充度 ——
        # 否则一次短暂转头留下的方差残留会在窗口内一直享受满额增益，
        # 造成瞬时动作被误判为持续。这里与头部偏转共享“异常头部行为
        # 在窗口中的占比”，语义为“头部【持续】处于异常波动状态”。
        out["head_variance"] = (dev_ratio
                                if self.head_variance(window) > _ACTIVE_EPS
                                else 0.0)

        # 手部交互：连续成立帧数占窗口比例
        streak = hand_interaction_streak(window, self.cfg)
        out["hand_interaction"] = _clamp01(
            streak / n if streak >= self.cfg.hand_interaction_frames else 0.0)

        # 违禁物体：窗口内出现帧占比
        _, ratio = object_persistence(window)
        out["prohibited_object"] = _clamp01(ratio)

        # 姿态重复性：不规范坐姿帧占比 与 往复强度 的较大者
        abn = sum(1 for f in window
                  if self.posture_abnormal_frame(f) > _ACTIVE_EPS)
        rep_i = 0.0
        if n >= 3:
            _, rate = repetition_rate(window, self.cfg.fps,
                                      self.cfg.repetition_min_amplitude)
            rep_i = (_clamp01(rate / self.cfg.posture_change_rate_threshold)
                     if self.cfg.posture_change_rate_threshold > 0 else 0.0)
        out["posture_repetition"] = _clamp01(
            max(fill * abn / n, rep_i))

        # 面部表情：交替比例本身即“持续变化”的度量
        out["expression_aux"] = self.expression_aux(window)
        return out

    # ================= 汇总 =================
    def extract(self, window: Sequence[FrameFeatures]) -> ExtractedSignals:
        """对窗口末端帧提取全部信号强度与持续占比。"""
        if not window:
            return ExtractedSignals()
        cur = window[-1]
        intensities = SignalIntensities(
            head_deviation=self.head_deviation(cur),
            head_variance=self.head_variance(window),
            hand_interaction=self.hand_interaction(window),
            prohibited_object=self.prohibited_object(cur),
            posture_repetition=self.posture_repetition(window),
            expression_aux=self.expression_aux(window),
        )
        return ExtractedSignals(intensities=intensities,
                                persistence=self.persistence(window))
