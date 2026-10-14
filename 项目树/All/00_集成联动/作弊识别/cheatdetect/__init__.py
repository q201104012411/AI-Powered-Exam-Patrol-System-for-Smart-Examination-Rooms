# -*- coding: utf-8 -*-
"""智能考场 · 考生作弊智能识别核心引擎。

AI 识别作弊的核心逻辑不是判断“某个动作本身是否作弊”，而是
【判断动作序列是否构成了“获取或传递答案”的行为模式】。
单个动作（如转头、低头）可能只是正常疲劳调整，只有结合持续时间、
频率和上下文才能做出有意义的判定。

判定链路：
    单帧多模态特征
      → 滑动时间窗口（默认 2s）聚合多模态特征
      → 各信号强度 × 有效权重（基础权重 × 持续性增益）求和
      → 指数平滑得到风险分数的连续轨迹
      → 风险分数【持续】超过阈值（默认 ≥1s）才触发告警
      → 单次瞬时动作被自动抑制

快速上手::

    from cheatdetect import CheatDetector, FrameFeatures, HeadPose

    det = CheatDetector()
    for i in range(30):
        f = FrameFeatures(frame_id=i, timestamp=i / 10.0,
                          head=HeadPose(yaw_deg=50.0, relation="toward_neighbor"))
        r = det.push(f)
        if r.just_triggered:
            print("触发告警：", r.pattern, r.risk)
    det.finish()
    print(det.summary())
"""

from .config import Config
from .detector import CheatDetector, DetectionResult
from .features import (FrameFeatures, HandState, HeadPose, PostureState,
                       RiskContributions)
from .patterns import (PATTERN_FIDGET, PATTERN_GAZE_DRIFT, PATTERN_HANDOFF,
                       PATTERN_MIXED, PATTERN_NONE, PATTERN_OBJECT,
                       PATTERN_PEEK, classify, describe)
from .scoring import (CN_NAME, SIGNAL_KEYS, WEIGHT_DIRECTION, ExtractedSignals,
                      RiskRecord, RiskScorer, SignalIntensities,
                      SignalWeights)
from .signals import SignalExtractor
from .temporal import (AlarmEvent, SlidingWindow, SustainTracker, WindowStats,
                       count_alternations, head_pose_variance,
                       repetition_rate)

__all__ = [
    "Config",
    "CheatDetector",
    "DetectionResult",
    "FrameFeatures",
    "HeadPose",
    "HandState",
    "PostureState",
    "RiskContributions",
    "SignalWeights",
    "SignalIntensities",
    "ExtractedSignals",
    "RiskScorer",
    "RiskRecord",
    "SignalExtractor",
    "SlidingWindow",
    "SustainTracker",
    "WindowStats",
    "AlarmEvent",
    "SIGNAL_KEYS",
    "WEIGHT_DIRECTION",
    "CN_NAME",
    "classify",
    "describe",
    "head_pose_variance",
    "repetition_rate",
    "count_alternations",
    "PATTERN_NONE",
    "PATTERN_OBJECT",
    "PATTERN_HANDOFF",
    "PATTERN_PEEK",
    "PATTERN_GAZE_DRIFT",
    "PATTERN_FIDGET",
    "PATTERN_MIXED",
]

__version__ = "0.1.0"
