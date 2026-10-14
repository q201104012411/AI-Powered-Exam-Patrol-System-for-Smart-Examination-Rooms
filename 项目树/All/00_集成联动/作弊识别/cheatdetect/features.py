"""帧级特征定义与风险贡献。

这一层只负责把“某一帧”的多模态观测转成结构化特征与对应的
风险贡献，本身不做是否为作弊的判断 —— 判断留给时间维度的
滑动窗口聚合完成。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HeadPose:
    """头部姿态观测。

    Attr:
        yaw_deg: 偏航角（度），正值表示偏离试卷/屏幕方向。
        pitch_deg: 俯仰角（度），正值表示低头。
        relation: 头部方向相对考试区域的语义标签。
    """

    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    relation: str = "on_paper"  # on_paper / off_area / toward_neighbor / toward_back


@dataclass
class HandState:
    """手部关键点状态观测。

    Attr:
        below_desk: 手部是否位于桌面下方。
        cross_distance: 与相邻考生手部关键点的归一化最近距离；
            None 表示本帧无可用跨考生手部观测。
        doing_write: 手部是否处于书写状态（可作为负向缓解信号）。
    """

    below_desk: bool = False
    cross_distance: Optional[float] = None
    doing_write: bool = False


@dataclass
class PostureState:
    """坐姿与持续性观测。

    Attr:
        tilt_deg: 人体倾斜偏离角度（度）。
        leaning_on_desk: 是否趴在台面上。
        left_area: 是否离开台面区域。
    """

    tilt_deg: float = 0.0
    leaning_on_desk: bool = False
    left_area: bool = False


@dataclass
class RiskContributions:
    """单帧各信号的风险贡献（0~100 量纲的加权前原始贡献）。

    具体加权在 RiskScorer 中执行；此处仅保留来源以便解释。
    """

    head_deviation: float = 0.0
    head_variance_placeholder: float = 0.0  # 方差需窗口聚合，见 temporal 模块
    hand_interaction: float = 0.0
    prohibited_object: float = 0.0
    posture_repetition: float = 0.0
    duration_placeholder: float = 0.0  # 持续时间由窗口层判定


@dataclass
class FrameFeatures:
    """单帧多模态特征聚合。

    Attr:
        frame_id: 帧序号。
        timestamp: 相对视频起点的时间（秒）。
        head: 头部姿态。
        hand: 手部状态。
        posture: 坐姿状态。
        prohibited_objects: 检测到的违禁物体列表（如 ["phone", "note"]）。
        expression_tense: 表情是否判定为紧张（用于可疑度辅助）。
        raw: 任意附加的原始观测（用于扩展）。
    """

    frame_id: int = 0
    timestamp: float = 0.0
    head: HeadPose = field(default_factory=HeadPose)
    hand: HandState = field(default_factory=HandState)
    posture: PostureState = field(default_factory=PostureState)
    prohibited_objects: list = field(default_factory=list)
    expression_tense: bool = False
