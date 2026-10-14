# -*- coding: utf-8 -*-
"""作弊识别引擎主入口 —— 串起“特征 → 窗口 → 评分 → 持续判定”。

对外只暴露一个 `CheatDetector`：逐帧 `push`，返回当帧的
`DetectionResult`（风险分数、贡献分解、行为模式、是否触发告警）。

判定链路（与项目书一致）：
    单帧多模态特征
      → 滑动时间窗口（默认 2s）聚合多模态特征
      → 计算各信号强度与加权风险分
      → 指数平滑得到风险分数的连续轨迹
      → 风险分数【持续】超过阈值（默认 ≥1s）才触发最终判定
      → 单次瞬时动作被自动抑制
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .config import Config
from .features import FrameFeatures
from .patterns import PATTERN_NONE, classify, describe, reason_text
from .scoring import RiskRecord, RiskScorer, SignalIntensities, SignalWeights
from .signals import SignalExtractor, frame_level_cue
from .temporal import SlidingWindow, SustainTracker, WindowStats


@dataclass
class DetectionResult:
    """单帧检测结果。"""

    frame_id: int = 0
    timestamp: float = 0.0
    risk: float = 0.0                 # 平滑后风险分数（阈值比较用）
    instant_risk: float = 0.0         # 瞬时风险分数
    threshold: float = 60.0
    over_threshold: bool = False
    triggered: bool = False           # 当前是否处于告警状态
    just_triggered: bool = False      # 本帧是否为告警起始
    released: bool = False            # 本帧是否告警解除
    level: str = "正常"
    pattern: str = PATTERN_NONE
    pattern_desc: str = ""
    reason: str = ""
    streak_frames: int = 0
    sustain_frames_needed: int = 0
    contributions: Dict[str, float] = field(default_factory=dict)
    intensities: Optional[SignalIntensities] = None
    persistence: Dict[str, float] = field(default_factory=dict)
    window: Optional[WindowStats] = None
    suppressed: bool = False          # 帧级判据会报警、但被时间维度抑制

    def as_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "timestamp": round(self.timestamp, 3),
            "risk": round(self.risk, 2),
            "instant_risk": round(self.instant_risk, 2),
            "threshold": self.threshold,
            "over_threshold": self.over_threshold,
            "triggered": self.triggered,
            "just_triggered": self.just_triggered,
            "released": self.released,
            "level": self.level,
            "pattern": self.pattern,
            "pattern_desc": self.pattern_desc,
            "reason": self.reason,
            "streak_frames": self.streak_frames,
            "sustain_frames_needed": self.sustain_frames_needed,
            "suppressed": self.suppressed,
            "contributions": {k: round(v, 2)
                              for k, v in (self.contributions or {}).items()},
            "intensities": (self.intensities.as_dict()
                            if self.intensities is not None else {}),
            "persistence": {k: round(v, 3)
                            for k, v in (self.persistence or {}).items()},
            "window": (self.window.__dict__ if self.window is not None else {}),
        }


class CheatDetector:
    """考生作弊识别引擎。

    Example:
        >>> det = CheatDetector()
        >>> for frame in frames:
        ...     result = det.push(frame)
        ...     if result.just_triggered:
        ...         print(result.frame_id, result.pattern, result.risk)
    """

    def __init__(self, cfg: Optional[Config] = None,
                 weights: Optional[SignalWeights] = None):
        self.cfg = cfg or Config()
        self.weights = weights or SignalWeights()
        self.scorer = RiskScorer(self.cfg, self.weights)
        self.extractor = SignalExtractor(self.cfg)
        self.window = SlidingWindow(self.cfg)
        self.sustain = SustainTracker(self.cfg)

        self._smoothed: Optional[float] = None
        self._recent_over: deque = deque(maxlen=self.cfg.window_frames)
        self._last_frame: Optional[FrameFeatures] = None
        self.results: List[DetectionResult] = []

        # 与 record 无关的会话级计数（summary 用）
        self._frames_seen = 0
        self._peak_risk = 0.0
        self._alarm_frames = 0
        self._suppressed_frames = 0
        self._last_timestamp = 0.0

    # ---------------- 主流程 ----------------
    def push(self, frame: FrameFeatures, record: bool = True) -> DetectionResult:
        """推入一帧并返回检测结果。"""
        window = self.window.push(frame)
        self._last_frame = frame

        # 1) 多模态信号强度 + 窗口内持续占比
        extracted = self.extractor.extract(window)
        intensities = extracted.intensities
        persistence = extracted.persistence

        # 2) 加权风险评分（非二值；“持续时间”作为乘法增益并入有效权重）
        instant, contributions, _ = self.scorer.score(intensities, persistence)

        # 3) 风险分数的连续轨迹（指数平滑）
        if self._smoothed is None:
            self._smoothed = instant
        else:
            a = self.cfg.smoothing_alpha
            self._smoothed = a * instant + (1.0 - a) * self._smoothed
        risk = self._smoothed
        over = risk >= self.cfg.risk_threshold

        # 4) 行为模式（由贡献分解解读）
        pattern = classify(contributions, intensities)

        # 5) 持续时间判定 —— 决定是否触发
        level = self.scorer.level_of(risk, over)
        streak, in_alarm, just_triggered, released = self.sustain.update(
            risk, frame, pattern, level)

        # 6) 窗口内超阈占比（“持续时间”的量化形式）
        self._recent_over.append(1 if over else 0)
        over_ratio = (sum(self._recent_over) / len(self._recent_over)
                      if self._recent_over else 0.0)
        stats = self.window.stats(over_threshold_ratio=over_ratio)

        # 7) 瞬时抑制标记：该帧在【帧级判据】下会被报警，
        #    但时空建模尚未（或未再）触发最终判定 —— 即“被抑制的帧级误报”。
        suppressed = (not in_alarm) and frame_level_cue(frame, self.cfg)

        result = DetectionResult(
            frame_id=frame.frame_id,
            timestamp=frame.timestamp,
            risk=risk,
            instant_risk=instant,
            threshold=self.cfg.risk_threshold,
            over_threshold=over,
            triggered=in_alarm,
            just_triggered=just_triggered,
            released=released,
            level=level if over else ("关注" if risk >= self.cfg.risk_threshold * 0.6
                                      else "正常"),
            pattern=pattern,
            pattern_desc=describe(pattern),
            reason=reason_text(pattern, contributions, risk,
                               self.cfg.risk_threshold, in_alarm, streak),
            streak_frames=streak,
            sustain_frames_needed=self.cfg.min_sustain_frames,
            contributions=contributions,
            intensities=intensities,
            persistence=persistence,
            window=stats,
            suppressed=suppressed,
        )
        if record:
            self.results.append(result)

        # 会话级计数（不依赖 record）
        self._frames_seen += 1
        self._last_timestamp = frame.timestamp
        self._peak_risk = max(self._peak_risk, risk)
        if in_alarm:
            self._alarm_frames += 1
        if suppressed:
            self._suppressed_frames += 1
        return result

    def push_many(self, frames: Iterable[FrameFeatures],
                  record: bool = True) -> List[DetectionResult]:
        """推入一批帧。"""
        return [self.push(f, record=record) for f in frames]

    def finish(self) -> None:
        """流结束：闭合仍在进行中的告警事件。"""
        self.sustain.close_all(self._last_frame)

    # ---------------- 查询 ----------------
    @property
    def events(self) -> list:
        """本次会话的所有告警事件。"""
        return self.sustain.events

    def event_dicts(self) -> List[dict]:
        return [e.as_dict() for e in self.sustain.events]

    def alarms(self) -> List[DetectionResult]:
        """所有处于告警状态（或告警起始）的帧结果。"""
        return [r for r in self.results if r.triggered or r.just_triggered]

    def summary(self) -> dict:
        """会话级汇总（与是否记录逐帧结果无关）。"""
        return {
            "frames": self._frames_seen,
            "duration": round(self._last_timestamp, 3),
            "alarm_events": len(self.sustain.events),
            "peak_risk": round(self._peak_risk, 2),
            "alarm_frames": self._alarm_frames,
            "suppressed_frames": self._suppressed_frames,
            "threshold": self.cfg.risk_threshold,
            "config": self.cfg.describe(),
        }

    def reset(self) -> None:
        """复位引擎，便于复用。"""
        self.window.clear()
        self.sustain = SustainTracker(self.cfg)
        self._smoothed = None
        self._recent_over.clear()
        self._last_frame = None
        self.results = []
        self._frames_seen = 0
        self._peak_risk = 0.0
        self._alarm_frames = 0
        self._suppressed_frames = 0
        self._last_timestamp = 0.0
