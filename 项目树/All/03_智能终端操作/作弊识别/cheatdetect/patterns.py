# -*- coding: utf-8 -*-
"""行为模式识别 —— “获取或传递答案”的行为模式判定。

项目书的核心命题：判定对象不是“某个动作本身是否作弊”，
而是【动作序列是否构成了“获取或传递答案”的行为模式】。

本模块把加权风险贡献解读为可读的行为模式标签，供人工复核使用。
模式判定只负责“是什么模式”，不负责“是否告警”——是否告警由
时间维度的持续判定决定。
"""

from typing import Dict, Optional

# 行为模式标签（对齐项目书“考生个体行为”典型行为）
PATTERN_NONE = "未见可疑模式"
PATTERN_OBJECT = "疑似使用/持有违禁物品"
PATTERN_HANDOFF = "疑似传递物品（跨考生手部交互）"
PATTERN_PEEK = "疑似旁窥（转头获取答案）"
PATTERN_GAZE_DRIFT = "疑似东张西望/注意力显著涣散"
PATTERN_FIDGET = "疑似多动与坐姿异常"
PATTERN_MIXED = "疑似复合作弊行为模式"

# 模式说明（用于告警文本与项目书对照）
PATTERN_DESCRIPTION: Dict[str, str] = {
    PATTERN_OBJECT: "检测到手机/纸条等违禁物品，属直接证据信号。",
    PATTERN_HANDOFF: "桌面下方手部关键点与邻座考生显著靠近并持续，"
                     "伴随身体朝向对方 —— 符合传递物品模式。",
    PATTERN_PEEK: "头部持续偏离试卷/屏幕并朝向邻座或后方，角度超阈值 —— "
                  "符合获取答案的旁窥模式。",
    PATTERN_GAZE_DRIFT: "窗口内头部姿态方差显著偏大，视线落点反复指向"
                        "非考试区域 —— 符合注意力涣散与可疑张望模式。",
    PATTERN_FIDGET: "低头-抬头、抬手-放手等重复性动作高频出现，"
                    "坐姿持续偏离 —— 符合多动与不规范姿态模式。",
    PATTERN_MIXED: "多项信号同时成立并持续超过阈值，构成复合可疑行为模式。",
    PATTERN_NONE: "当前窗口内各信号均未构成持续可疑模式。",
}


def classify(contributions: Dict[str, float],
             intensities=None,
             threshold: float = 0.0) -> str:
    """依据加权贡献判定主导行为模式。

    Args:
        contributions: 各信号贡献分（有效权重×强度×100）。
        intensities: 可选，SignalIntensities。用于判别“物体是否正在被递出”
            以及区分“姿态重复”与更具体的头部/手部模式。
        threshold: 贡献分有效下限，低于该值的信号不参与模式判定。

    Returns:
        行为模式标签。

    判别顺序体现【特异性优先】：
        违禁物体（+ 跨考生手部交互 → 传递物品）> 手部交互 > 头部偏转 >
        头部姿态方差 > 姿态重复性。
    “姿态重复性”的活动信号本身含头部运动，因此重复转头会同时抬高它，
    所以它被当作【最不特异】的信号：只有当更具体的头部/手部信号都不显著时，
    才把模式归为“多动与坐姿异常”。
    """
    if not contributions:
        return PATTERN_NONE

    active = {k: v for k, v in contributions.items() if v > max(threshold, 1e-6)}
    if not active:
        return PATTERN_NONE

    inten = intensities.as_dict() if intensities is not None else {}

    # —— 违禁物体：直接证据，优先成标签 ——
    obj = contributions.get("prohibited_object", 0.0)
    if obj > 0.0:
        # 物体在场且跨考生手部交互同时成立 → 正在递出/接收，属“传递物品”。
        # 这里比较【强度】而非贡献分：贡献分在高权重下会饱和，
        # 0.5×物体贡献 的门槛会随饱和而失真。
        hand_i = inten.get("hand_interaction", 0.0)
        if hand_i >= 0.4:
            return PATTERN_HANDOFF
        return PATTERN_OBJECT

    dominant = max(active, key=lambda k: active[k])

    # —— 特异性优先：姿态重复性让位于更具体的头部/手部模式 ——
    if dominant == "posture_repetition":
        specifics = ("hand_interaction", "head_deviation", "head_variance")
        cand = [k for k in specifics if active.get(k, 0.0) > 0.0]
        if cand:
            best = max(cand, key=lambda k: active[k])
            if active[best] >= 0.8 * active[dominant]:
                dominant = best

    # 主信号占比过低 → 判为复合模式
    total = sum(active.values())
    if total > 0 and active[dominant] / total < 0.40 and len(active) >= 3:
        return PATTERN_MIXED

    if dominant == "hand_interaction":
        return PATTERN_HANDOFF
    if dominant == "head_deviation":
        return PATTERN_PEEK
    if dominant == "head_variance":
        return PATTERN_GAZE_DRIFT
    if dominant == "posture_repetition":
        return PATTERN_FIDGET
    if dominant == "expression_aux":
        # 表情只是辅助，不足以独立成模式
        return PATTERN_NONE
    return PATTERN_MIXED


def describe(pattern: str) -> str:
    """返回模式的可读说明。"""
    return PATTERN_DESCRIPTION.get(pattern, "")


def reason_text(pattern: str, contributions: Dict[str, float],
                risk: float, threshold: float,
                sustained: bool, streak_frames: int = 0) -> str:
    """生成可解释的判定理由文本（供巡考员复核）。"""
    if not contributions:
        return "无有效信号。"
    top = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)[:3]
    parts = []
    for key, val in top:
        if val <= 0.5:
            continue
        parts.append("{}({:.1f}分)".format(_CN_NAME.get(key, key), val))
    detail = "、".join(parts) if parts else "无明显信号"
    if sustained:
        return ("{}：{}；风险分 {:.1f} 持续超过阈值 {:.0f} 已达 {} 帧。"
                .format(pattern, detail, risk, threshold, streak_frames))
    return ("{}：{}；风险分 {:.1f} 未持续超过阈值 {:.0f}，不触发最终判定。"
            .format(pattern if pattern != PATTERN_NONE else "未见可疑模式",
                    detail, risk, threshold))


_CN_NAME = {
    "head_deviation": "头部偏转",
    "head_variance": "头部姿态方差",
    "hand_interaction": "手部跨考生距离",
    "prohibited_object": "违禁物体",
    "posture_repetition": "姿态重复性",
    "expression_aux": "面部表情",
}


def cn_name(key: str) -> str:
    """信号键的中文名。"""
    return _CN_NAME.get(key, key)
