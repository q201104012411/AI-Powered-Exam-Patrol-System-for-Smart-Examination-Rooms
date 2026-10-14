# -*- coding: utf-8 -*-
"""加权风险评分 —— 非二值判断，且把“持续时间”纳入权重。

项目书要求：判定逻辑采用【加权风险分数】而非二值判断。本模块实现：

    有效权重_i = 基础权重_i × (1 + 持续性增益 × 持续占比_i)
    原始分     = Σ (有效权重_i × 强度_i)
    风险分     = min(100, 100 × 原始分)

基础权重严格对应项目书《风险评分框架》表的“权重方向”：
    头部偏转（中）/ 头部姿态方差（中）/ 手部跨考生距离（高）/
    违禁物体（极高）/ 姿态重复性（中），面部表情为辅助项。

“持续时间”实现为【乘法增益】：同一强度的信号，持续出现 2 秒与只出现
1 帧，得分完全不同 —— 这就是“瞬时动作被自动抑制”的量化机制之一
（另一重机制是 detector 中的持续超阈闸门与轨迹平滑）。
"""

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .config import Config

# 信号键（顺序与项目书表格一致）
SIGNAL_KEYS = (
    "head_deviation",
    "head_variance",
    "hand_interaction",
    "prohibited_object",
    "posture_repetition",
    "expression_aux",
)

# 权重方向标签（项目书口径）
WEIGHT_DIRECTION = {
    "head_deviation": "中",
    "head_variance": "中",
    "hand_interaction": "高",
    "prohibited_object": "极高",
    "posture_repetition": "中",
    "expression_aux": "辅助",
}

# 中文名
CN_NAME = {
    "head_deviation": "头部偏转",
    "head_variance": "头部姿态方差",
    "hand_interaction": "手部跨考生距离",
    "prohibited_object": "违禁物体",
    "posture_repetition": "姿态重复性",
    "expression_aux": "面部表情",
}


@dataclass
class SignalWeights:
    """各信号基础权重，按项目书“权重方向”标定。

    标定说明（阈值 60，持续性增益 1.0）：

      · 违禁物体（极高 0.70）：窗口内持续出现即独立触发，属直接证据；
      · 手部跨考生交互（高 0.62）：持续即触发；
      · 头部偏转（中 0.40）：满强度且【在整个窗口持续】时，有效权重
        0.40×(1+1.0)=0.80 → 80 分，可独立触发（对应“持续转头面向邻座”）；
        但只出现一瞬（持续占比 0.15）时有效权重仅 0.46 → 约 37 分，不触发；
      · 头部姿态方差（中 0.28）：满强度持续仅 56 分，【不单独触发】，
        需与头部偏转或姿态异常叠加 —— 对应“高方差意味着不安或分心”，
        单独看不足以认定作弊；
      · 姿态重复性（中 0.26）：满强度持续 52 分，同样需叠加。

    于是形成清晰的判定语义：
        单个【瞬时】动作 → 不触发（持续占比低）；
        单个【中等】信号长期持续 → 可达阈值（对应“持续转头面向邻座”）；
        头部方差 / 姿态重复等中等信号 → 必须叠加成模式才触发；
        高 / 极高信号（跨考生手部交互、违禁物体）→ 持续即触发。
    """

    head_deviation: float = 0.40      # 中
    head_variance: float = 0.28       # 中
    hand_interaction: float = 0.62    # 高
    prohibited_object: float = 0.70   # 极高
    posture_repetition: float = 0.26  # 中
    expression_aux: float = 0.06      # 辅助

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)

    def direction(self, key: str) -> str:
        """该信号的权重方向标签。"""
        return WEIGHT_DIRECTION.get(key, "—")

    @property
    def maximum(self) -> float:
        """所有信号基础权重之和。"""
        return sum(self.as_dict().values())

    def table(self) -> List[dict]:
        """权重表（供报告与项目书对照）。"""
        d = self.as_dict()
        return [
            {
                "signal": key,
                "cn": CN_NAME[key],
                "direction": WEIGHT_DIRECTION[key],
                "weight": d[key],
            }
            for key in SIGNAL_KEYS
        ]


@dataclass
class SignalIntensities:
    """单帧各信号的归一化强度（0~1）。"""

    head_deviation: float = 0.0
    head_variance: float = 0.0
    hand_interaction: float = 0.0
    prohibited_object: float = 0.0
    posture_repetition: float = 0.0
    expression_aux: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)

    def dominant(self) -> str:
        """当前最强信号键名。"""
        items = self.as_dict()
        return max(items, key=lambda k: items[k]) if items else ""

    def active_keys(self, eps: float = 1e-6) -> List[str]:
        return [k for k, v in self.as_dict().items() if v > eps]


def empty_persistence() -> Dict[str, float]:
    """全零持续占比。"""
    return {k: 0.0 for k in SIGNAL_KEYS}


@dataclass
class ExtractedSignals:
    """一次提取的结果：强度 + 窗口内持续占比。"""

    intensities: SignalIntensities = field(default_factory=SignalIntensities)
    persistence: Dict[str, float] = field(default_factory=empty_persistence)

    def as_dict(self) -> dict:
        return {
            "intensities": self.intensities.as_dict(),
            "persistence": {k: round(v, 3) for k, v in self.persistence.items()},
        }


@dataclass
class RiskRecord:
    """一帧的风险评分记录（可解释输出）。"""

    frame_id: int = 0
    timestamp: float = 0.0
    raw_score: float = 0.0
    risk: float = 0.0            # 平滑后的风险分数（阈值比较用）
    instant_risk: float = 0.0    # 未平滑的瞬时风险分数
    threshold: float = 60.0
    over_threshold: bool = False
    contributions: Optional[Dict[str, float]] = None
    intensities: Optional[SignalIntensities] = None
    persistence: Optional[Dict[str, float]] = None
    pattern: str = ""
    level: str = "正常"
    streak_frames: int = 0
    reason: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        if self.intensities is not None:
            d["intensities"] = self.intensities.as_dict()
        return d


class RiskScorer:
    """加权风险评分器。

    评分是【加权求和后线性映射并截顶 100】，理由：
      · 权重按项目书“权重方向”直接标定，分数含义直观（有效权重×100 即贡献分）；
      · 线性可加保证“模式语义”——单个低/中权重信号不触发，多信号叠加才触发；
      · 截顶在 100，避免多项极高信号产生无意义的巨大数值。

    同时输出每个信号的【贡献分】，供人工复核时直接看到“这一帧为什么可疑”。
    """

    def __init__(self, cfg: Config,
                 weights: Optional[SignalWeights] = None,
                 persistence_boost: Optional[float] = None):
        self.cfg = cfg
        self.weights = weights or SignalWeights()
        self.persistence_boost = (cfg.persistence_boost
                                  if persistence_boost is None
                                  else persistence_boost)

    # ---------------- 权重 ----------------
    def effective_weights(self,
                          persistence: Optional[Dict[str, float]] = None
                          ) -> Dict[str, float]:
        """把“持续时间”并入基础权重，得到有效权重。"""
        base = self.weights.as_dict()
        if not persistence:
            return dict(base)
        out = {}
        for k, w in base.items():
            p = float(persistence.get(k, 0.0))
            p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
            out[k] = w * (1.0 + self.persistence_boost * p)
        return out

    # ---------------- 评分 ----------------
    def contributions(self, s: SignalIntensities,
                      persistence: Optional[Dict[str, float]] = None
                      ) -> Dict[str, float]:
        """各信号贡献分（0~100 量纲）。"""
        eff = self.effective_weights(persistence)
        inten = s.as_dict()
        return {k: eff[k] * inten[k] * 100.0 for k in SIGNAL_KEYS}

    def raw_score(self, s: SignalIntensities,
                  persistence: Optional[Dict[str, float]] = None) -> float:
        """原始加权分（未截顶）。"""
        eff = self.effective_weights(persistence)
        inten = s.as_dict()
        return sum(eff[k] * inten[k] for k in SIGNAL_KEYS)

    def instant_risk(self, s: SignalIntensities,
                     persistence: Optional[Dict[str, float]] = None) -> float:
        """瞬时风险分（0~100，截顶）。"""
        return min(100.0, self.raw_score(s, persistence) * 100.0)

    def score(self, s: SignalIntensities,
              persistence: Optional[Dict[str, float]] = None) -> tuple:
        """Returns: (instant_risk, contributions, dominant_key)"""
        return (self.instant_risk(s, persistence),
                self.contributions(s, persistence),
                s.dominant())

    # ---------------- 分级 ----------------
    def level_of(self, risk: float, over_threshold: bool) -> str:
        """风险分级标签（对齐项目书告警级别）。"""
        if not over_threshold:
            return "关注" if risk >= self.cfg.risk_threshold * 0.6 else "正常"
        if risk >= self.cfg.high_risk_threshold:
            return "红色预警"
        return "橙色预警"
