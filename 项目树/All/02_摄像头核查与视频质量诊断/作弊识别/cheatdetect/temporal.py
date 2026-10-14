# -*- coding: utf-8 -*-
"""时间维度建模：滑动窗口聚合、方差、往复次数与持续判定。

本模块是“帧级误报 15.2% → 时空建模后 4.3%”这一改善的【实现所在】：
帧级检测只看某一帧，本模块改为看“一段窗口内是否持续在做”。
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .config import Config
from .features import FrameFeatures


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pvariance(values: Sequence[float]) -> float:
    """总体方差。"""
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return sum((v - mu) ** 2 for v in values) / len(values)


@dataclass
class WindowStats:
    """滑动窗口统计量 —— 时间维度的可观测量。

    Attr:
        size: 窗口内帧数。
        duration: 窗口覆盖时长（秒）。
        head_var_yaw / head_var_pitch: 窗口内 yaw/pitch 方差（rad^2）。
        head_variance: 二者之和，即项目书中的“头部姿态方差”。
        alternations: 窗口内活动信号穿过均值的次数。
        repetition_rate: 往复频率（次/秒），对应“姿态重复性”。
        hand_streak: 截至当前帧，手部跨考生条件连续成立的帧数。
        object_frames: 窗口内检出违禁物体的帧数。
        object_ratio: 违禁物体出现帧占窗口比例。
        over_threshold_ratio: 窗口内风险分数超阈帧占比（“持续时间”的量化）。
    """

    size: int = 0
    duration: float = 0.0
    head_var_yaw: float = 0.0
    head_var_pitch: float = 0.0
    head_variance: float = 0.0
    alternations: int = 0
    repetition_rate: float = 0.0
    hand_streak: int = 0
    object_frames: int = 0
    object_ratio: float = 0.0
    over_threshold_ratio: float = 0.0


def head_pose_variance(frames: Sequence[FrameFeatures]) -> tuple:
    """计算窗口内头部姿态方差。

    项目书要点：与注意力涣散、可疑行为高度相关的不是瞬时朝向，
    而是姿态的【方差】——高方差通常意味着不安或分心。

    Returns:
        (var_yaw, var_pitch, var_sum)，单位 rad^2。
    """
    if len(frames) < 2:
        return 0.0, 0.0, 0.0
    yaws = [math.radians(f.head.yaw_deg) for f in frames]
    pitch = [math.radians(f.head.pitch_deg) for f in frames]
    vy, vp = _pvariance(yaws), _pvariance(pitch)
    return vy, vp, vy + vp


def activity_series(frames: Sequence[FrameFeatures]) -> List[float]:
    """构造“活动强度”序列，用于统计往复次数。

    活动强度融合三类可观测动作：
      · 头部偏航/俯仰偏离（转头、低头-抬头）
      · 手部是否在桌面下方（抬手-放手）
      · 坐姿倾斜（起落、扭动）
    """
    series = []
    for f in frames:
        act = 0.0
        act += min(1.0, abs(f.head.yaw_deg) / 45.0)
        act += min(1.0, abs(f.head.pitch_deg) / 45.0)
        act += 1.0 if f.hand.below_desk else 0.0
        act += min(1.0, abs(f.posture.tilt_deg) / 45.0)
        series.append(act)
    return series


def _smooth(series: Sequence[float], k: int = 3) -> List[float]:
    """滑动平均，抑制高频噪声。"""
    n = len(series)
    if k <= 1 or n < k:
        return list(series)
    half = k // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(series[lo:hi]) / (hi - lo))
    return out


def count_alternations(series: Sequence[float],
                       min_amplitude: float = 0.18,
                       smooth_k: int = 3) -> int:
    """统计序列的往复（交替）次数 —— 带滞回与最小幅度约束。

    【为什么必须带滞回】：检测噪声会让活动信号在其均值附近频繁抖动，
    若直接按“穿越均值”计数，安静答题时的随机抖动也会被算成高频往复，
    从而把“姿态重复性”误判为可疑。因此这里要求幅度超过
    `min_amplitude` 才算一次真实的状态翻转（施密特触发）。

    真实动作（低头与抬头、抬手与放手）的振幅远大于噪声，
    滞回不会削弱它们，只滤掉噪声引起的伪往复。
    """
    n = len(series)
    if n < 3:
        return 0
    s = _smooth(series, smooth_k)
    lo, hi = min(s), max(s)
    if hi - lo < 1e-6:
        return 0
    mu = _mean(s)
    delta = min(min_amplitude, (hi - lo) / 2.0 - 1e-9)
    if delta <= 0:
        delta = (hi - lo) * 0.25
    state = 0          # 0=未定，1=高位，-1=低位
    flips = 0
    for v in s:
        if v > mu + delta:
            if state == -1:
                flips += 1
            state = 1
        elif v < mu - delta:
            if state == 1:
                flips += 1
            state = -1
    return flips


def repetition_rate(frames: Sequence[FrameFeatures], fps: float,
                    min_amplitude: float = 0.18) -> tuple:
    """窗口内姿态往复频率（次/秒）。

    Returns:
        (alternations, rate)，rate = (状态翻转次数 / 2) / 窗口时长。
    """
    if len(frames) < 3 or fps <= 0:
        return 0, 0.0
    alt = count_alternations(activity_series(frames), min_amplitude)
    duration = len(frames) / fps
    cycles = alt / 2.0
    return alt, (cycles / duration if duration > 0 else 0.0)


def hand_interaction_streak(frames: Sequence[FrameFeatures], cfg: Config) -> int:
    """截至窗口末端，手部跨考生条件“连续成立”的帧数。

    单帧手部靠近不算交互；只有连续保持 `hand_interaction_frames`
    帧以上，才认为构成了跨考生手部交互。
    """
    streak = 0
    for f in reversed(frames):
        d = f.hand.cross_distance
        ok = d is not None and d < cfg.hand_interaction_distance
        if not ok:
            break
        if not (f.hand.below_desk or f.posture.leaning_on_desk):
            break
        streak += 1
    return streak


def object_persistence(frames: Sequence[FrameFeatures]) -> tuple:
    """窗口内违禁物体的出现帧数与占比。"""
    n = sum(1 for f in frames if f.prohibited_objects)
    return n, (n / len(frames) if frames else 0.0)


class SlidingWindow:
    """定长滑动时间窗口。

    以“帧”为步长向前滑动，始终保留最近 `window_seconds` 秒的多模态特征，
    供窗口级特征（方差、重复性、持续性）计算使用。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._buf: List[FrameFeatures] = []
        self._max = cfg.window_frames

    def push(self, frame: FrameFeatures) -> List[FrameFeatures]:
        """推入一帧并返回推入后的窗口内容（副本）。"""
        self._buf.append(frame)
        if len(self._buf) > self._max:
            del self._buf[0: len(self._buf) - self._max]
        return list(self._buf)

    @property
    def frames(self) -> List[FrameFeatures]:
        return list(self._buf)

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def is_full(self) -> bool:
        return len(self._buf) >= self._max

    def duration(self) -> float:
        return len(self._buf) / self.cfg.fps if self.cfg.fps > 0 else 0.0

    def clear(self) -> None:
        self._buf.clear()

    def stats(self, over_threshold_ratio: float = 0.0) -> WindowStats:
        """汇总当前窗口的时间维度统计量。"""
        frames = self._buf
        vy, vp, vs = head_pose_variance(frames)
        alt, rate = repetition_rate(frames, self.cfg.fps)
        obj_n, obj_r = object_persistence(frames)
        return WindowStats(
            size=len(frames),
            duration=self.duration(),
            head_var_yaw=vy,
            head_var_pitch=vp,
            head_variance=vs,
            alternations=alt,
            repetition_rate=rate,
            hand_streak=hand_interaction_streak(frames, self.cfg),
            object_frames=obj_n,
            object_ratio=obj_r,
            over_threshold_ratio=over_threshold_ratio,
        )


@dataclass
class AlarmEvent:
    """一次完整的告警事件（从风险首次超阈到回落）。

    Attr:
        onset_frame / onset_time: 【风险分数首次超过阈值】的时刻。
            即可疑行为在证据上的起点（早于“确认触发”时刻）。
        confirm_frame / confirm_time: 持续超阈达标、正式触发告警的时刻。
        offset_frame / offset_time: 风险回落到阈值以下的时刻。
        peak_risk / peak_pattern: 事件期间的峰值风险与其对应模式。
        level: 事件达到的最高级别。
        frames_held: 事件持续的帧数。
    """

    onset_frame: int = 0
    onset_time: float = 0.0
    confirm_frame: Optional[int] = None
    confirm_time: Optional[float] = None
    offset_frame: Optional[int] = None
    offset_time: Optional[float] = None
    peak_risk: float = 0.0
    peak_pattern: str = ""
    level: str = "橙色预警"
    frames_held: int = 0

    @property
    def duration(self) -> float:
        if self.offset_time is None:
            return 0.0
        return max(0.0, self.offset_time - self.onset_time)

    @property
    def ongoing(self) -> bool:
        return self.offset_time is None

    def as_dict(self) -> dict:
        return {
            "onset_frame": self.onset_frame,
            "onset_time": round(self.onset_time, 3),
            "confirm_frame": self.confirm_frame,
            "confirm_time": (round(self.confirm_time, 3)
                             if self.confirm_time is not None else None),
            "offset_frame": self.offset_frame,
            "offset_time": (round(self.offset_time, 3)
                            if self.offset_time is not None else None),
            "duration": round(self.duration, 3),
            "peak_risk": round(self.peak_risk, 2),
            "peak_pattern": self.peak_pattern,
            "level": self.level,
            "frames_held": self.frames_held,
            "ongoing": self.ongoing,
        }


class SustainTracker:
    """“持续时间”判定器 —— 决定是否触发最终判定。

    规则（项目书最终判定口径）：
      风险分数在滑动窗口内【持续】超过阈值，才被标记为可疑作弊行为；
      单次瞬时动作不触发最终判定。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.streak = 0
        self.in_alarm = False
        self.cooldown = 0
        self.events: List[AlarmEvent] = []
        self._current: Optional[AlarmEvent] = None
        self._peak_pattern = ""
        self._peak_level = "橙色预警"
        # 当前连续超阈区间的起点（用于把事件起点记为行为起点）
        self._run_start: Optional[tuple] = None

    def update(self, risk: float, frame: FrameFeatures,
               pattern: str = "", level: str = "橙色预警") -> tuple:
        """推进一步持续判定。

        Args:
            risk: 当前（平滑后）风险分数。
            frame: 当前帧（取 frame_id / timestamp）。
            pattern: 当前主导行为模式标签。
            level: 当前级别标签。

        Returns:
            (streak, in_alarm, just_triggered, just_released)
        """
        over = risk >= self.cfg.risk_threshold

        if over:
            if self.streak == 0:
                # 记录本段连续超阈的起点
                self._run_start = (frame.frame_id, frame.timestamp)
            self.streak += 1
        else:
            self.streak = 0
            self._run_start = None
            if self.cooldown > 0:
                self.cooldown -= 1

        just_triggered = False
        just_released = False

        if self.in_alarm:
            # 事件进行中：刷新峰值
            if self._current is not None:
                self._current.frames_held += 1
                if risk > self._current.peak_risk:
                    self._current.peak_risk = risk
                    self._current.peak_pattern = pattern
                if risk >= self.cfg.high_risk_threshold:
                    self._current.level = "红色预警"
                    self._peak_level = "红色预警"
            if not over:
                # 风险回落，事件闭合
                self._close(frame)
                self.in_alarm = False
                self.cooldown = self.cfg.alarm_cooldown_frames
                just_released = True
        else:
            if self.streak >= self.cfg.min_sustain_frames and self.cooldown == 0:
                # 持续超阈达标 → 触发
                self.in_alarm = True
                just_triggered = True
                self._peak_pattern = pattern
                self._peak_level = ("红色预警"
                                    if risk >= self.cfg.high_risk_threshold
                                    else level)
                onset_frame, onset_time = (self._run_start
                                           or (frame.frame_id, frame.timestamp))
                self._current = AlarmEvent(
                    onset_frame=onset_frame,
                    onset_time=onset_time,
                    confirm_frame=frame.frame_id,
                    confirm_time=frame.timestamp,
                    peak_risk=risk,
                    peak_pattern=pattern,
                    level=self._peak_level,
                    frames_held=0,
                )
                self.events.append(self._current)

        return self.streak, self.in_alarm, just_triggered, just_released

    def _close(self, frame: FrameFeatures) -> None:
        if self._current is not None:
            self._current.offset_frame = frame.frame_id
            self._current.offset_time = frame.timestamp
            self._current = None

    def close_all(self, frame: Optional[FrameFeatures] = None) -> None:
        """流结束时闭合仍在进行的事件。"""
        if self._current is not None:
            if frame is not None:
                self._current.offset_frame = frame.frame_id
                self._current.offset_time = frame.timestamp
            self._current = None
        self.in_alarm = False
        self._run_start = None

    @property
    def event_count(self) -> int:
        return len(self.events)
