# -*- coding: utf-8 -*-
"""系统配置：阈值、窗口与权重等可量化参数。

所有参数均可按考点、考场模板调整，且每一项都可追溯至
项目书《考生作弊智能识别核心逻辑》中的
“可量化动作界定维度”与“风险评分框架”。
"""

from dataclasses import dataclass


@dataclass
class Config:
    """作弊识别引擎的全局配置。

    Attributes:
        fps: 视频帧率（帧/秒），用于把时间窗口换算为帧数。
        window_seconds: 滑动时间窗口长度（秒）。项目书约定 2 秒。
        risk_threshold: 告警触发的风险分数阈值（0~100）。
            只有风险分数“持续”超过该值才触发最终判定。
        high_risk_threshold: 红色预警阈值（0~100）。
        min_sustain_seconds: 风险分数必须持续超过阈值的最短时长（秒）。
            这是抑制瞬时动作、把误报率从帧级 15.2% 降下来的关键参数。
        smoothing_alpha: 风险轨迹的指数平滑系数（0~1]，越小越平滑。
            平滑使单帧尖峰无法独自冲到阈值，进一步抑制瞬时误报。
        head_yaw_threshold_deg: 头部偏转（yaw）偏离考试区域阈值（度）。
        head_pitch_threshold_deg: 头部俯仰（pitch）偏离阈值（度）。
        head_variance_threshold: 窗口内头部姿态方差阈值（rad^2），
            达到该值记为强度 1.0。
        hand_interaction_distance: 判定“手部跨考生交互”的归一化距离阈值
            （相对画面宽度）。
        hand_interaction_frames: 手部交互需连续保持的最少帧数。
        posture_tilt_threshold_deg: 坐姿倾斜偏离阈值（度）。
        posture_tilt_full_deg: 倾斜强度达到 1.0 的角度。
        posture_change_rate_threshold: 单位时间姿态往复次数阈值（次/秒）。
        object_full_confidence: 违禁物体检测置信度达到该值即记强度 1.0。
        alarm_cooldown_seconds: 同一次告警结束后的静默时长（秒），
            避免风险在阈值附近抖动时反复触发。
    """

    # —— 时间维度 ——
    fps: float = 10.0
    window_seconds: float = 2.0
    risk_threshold: float = 60.0
    high_risk_threshold: float = 80.0
    min_sustain_seconds: float = 1.0
    smoothing_alpha: float = 0.5
    alarm_cooldown_seconds: float = 1.0

    # —— 头部与视线姿态 ——
    head_yaw_threshold_deg: float = 30.0
    head_pitch_threshold_deg: float = 30.0
    head_variance_threshold: float = 0.25  # rad^2

    # —— 手部与肢体交互 ——
    hand_interaction_distance: float = 0.08  # 归一化距离（画面宽度的比例）
    hand_interaction_frames: int = 3
    hand_write_mitigation: float = 0.5       # 书写状态下的抑制系数

    # —— 姿态与持续性异常 ——
    posture_tilt_threshold_deg: float = 25.0
    posture_tilt_full_deg: float = 60.0
    posture_change_rate_threshold: float = 0.6  # 次/秒
    # 往复计数的最小振幅（活动信号单位）—— 带滞回，抑制检测噪声造成的伪往复
    repetition_min_amplitude: float = 0.18
    leaning_intensity: float = 0.5              # 趴台面基准强度
    left_area_intensity: float = 0.6            # 离开台面区域基准强度

    # —— 违禁物体 ——
    object_full_confidence: float = 0.6

    # —— 持续性增益 ——
    # 项目书《风险评分框架》最后一行：“持续时间 —— 上述信号在滑动窗口内
    # 持续出现，决定是否触发”。本引擎把“持续时间”同时实现为两件事：
    #   ① 闸门（gate）：风险分数持续超阈 min_sustain_seconds 才触发；
    #   ② 放大器（boost）：单个信号在窗口内的持续占比会提升其有效权重。
    # 这样“持续转头面向邻座”这类单一中等信号，只要真的【持续】，也能达到
    # 阈值；而同样强度的瞬时动作因持续占比极低被自动抑制。
    persistence_boost: float = 1.0

    @property
    def window_frames(self) -> int:
        """滑动窗口对应的帧数。"""
        return max(1, int(round(self.window_seconds * self.fps)))

    @property
    def min_sustain_frames(self) -> int:
        """风险分数需持续超阈的最小帧数。"""
        return max(1, int(round(self.min_sustain_seconds * self.fps)))

    @property
    def alarm_cooldown_frames(self) -> int:
        """告警结束后的静默帧数。"""
        return max(0, int(round(self.alarm_cooldown_seconds * self.fps)))

    def describe(self) -> str:
        """人类可读的参数摘要（用于报告与自检输出）。"""
        return (
            "窗口 {w:.1f}s/{wf}帧 · 阈值 {t:.0f} · 高阈值 {h:.0f} · "
            "最短持续 {s:.1f}s/{sf}帧 · 平滑 α={a:.2f}"
        ).format(
            w=self.window_seconds, wf=self.window_frames,
            t=self.risk_threshold, h=self.high_risk_threshold,
            s=self.min_sustain_seconds, sf=self.min_sustain_frames,
            a=self.smoothing_alpha,
        )
