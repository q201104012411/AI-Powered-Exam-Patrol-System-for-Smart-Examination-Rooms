# -*- coding: utf-8 -*-
"""合成场景仿真：对比“帧级检测”与“时空建模”的误报与准确率。

【注意】 重要声明（与项目书的诚实性口径一致）：
    本模块输出的一切数值均为【合成数据仿真结果】，用于验证算法逻辑
    是否按设计工作，【不是】真实考场实测数据。项目书中“误报率 15.2%
    → 4.3%、准确率 84.6% → 95.1%”系引用值；本仿真的数值由本代码
    在当前参数下重新计算，两者不应互相替代，也不得作为实测结论。

仿真做什么：
  1) 按“正常行为”与“作弊行为”生成脱敏合成帧序列（含随机抖动）；
  2) 用【帧级基线】（单帧任一线索成立即报警，无时间建模）判定；
  3) 用【时空引擎】（滑动窗口 + 加权风险 + 持续超阈）判定；
  4) 统计误报率、提示率（召回）、准确率，验证：
        · 瞬时动作（伸懒腰、短暂分心、调整坐姿）被自动抑制；
        · 持续动作序列（旁窥、传递、使用手机等）被正确识别。
"""

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .detector import CheatDetector
from .features import FrameFeatures, HandState, HeadPose, PostureState
from .signals import frame_level_cues

FPS = 10.0


# ============================================================
# 帧构造
# ============================================================
def _mk(i: int, fps: float = FPS, *, yaw: float = 0.0, pitch: float = 8.0,
        relation: str = "on_paper", below: bool = False,
        cross: Optional[float] = None, write: bool = True, tilt: float = 0.0,
        lean: bool = False, left: bool = False, objs: Sequence = (),
        tense: bool = False) -> FrameFeatures:
    """构造一帧特征。"""
    return FrameFeatures(
        frame_id=i,
        timestamp=i / fps,
        head=HeadPose(yaw_deg=yaw, pitch_deg=pitch, relation=relation),
        hand=HandState(below_desk=below, cross_distance=cross,
                       doing_write=write),
        posture=PostureState(tilt_deg=tilt, leaning_on_desk=lean,
                             left_area=left),
        prohibited_objects=list(objs),
        expression_tense=tense,
    )


def _j(rng: random.Random, sigma: float = 2.0) -> float:
    """小随机抖动，模拟检测噪声。"""
    return rng.gauss(0.0, sigma)


# ============================================================
# 正常行为场景（真值 = 正常；不应触发最终判定）
# ============================================================
def sc_quiet_writing(rng: random.Random) -> List[FrameFeatures]:
    """安静答题：低头看自己试卷，手在桌面书写。"""
    out = []
    for i in range(30):
        out.append(_mk(i, yaw=_j(rng), pitch=8.0 + _j(rng),
                       relation="on_paper", below=False, write=True))
    return out


def sc_stretch(rng: random.Random) -> List[FrameFeatures]:
    """伸懒腰：3 帧抬头后仰（瞬时动作，应被抑制）。"""
    out = []
    for i in range(30):
        if 10 <= i <= 12:
            out.append(_mk(i, yaw=_j(rng), pitch=-24.0 + _j(rng),
                           relation="off_area", below=True, cross=None,
                           write=False, tilt=20.0 + _j(rng)))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_posture_shift(rng: random.Random) -> List[FrameFeatures]:
    """调整坐姿：4 帧身体倾斜、手抬到桌下（瞬时动作，应被抑制）。"""
    out = []
    for i in range(30):
        if 8 <= i <= 11:
            out.append(_mk(i, yaw=_j(rng), pitch=12.0 + _j(rng), tilt=36.0 + _j(rng),
                           below=True, cross=None, write=False))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_brief_glance(rng: random.Random) -> List[FrameFeatures]:
    """短暂分心：3 帧（0.3s）转头看向邻座方向后立刻回正。"""
    out = []
    for i in range(30):
        if 12 <= i <= 14:
            out.append(_mk(i, yaw=45.0 + _j(rng), pitch=6.0 + _j(rng),
                           relation="toward_neighbor", below=False, write=False))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_long_lookdown(rng: random.Random) -> List[FrameFeatures]:
    """长时间低头：持续低头看【自己的】试卷（relation=on_paper），非作弊。"""
    out = []
    for i in range(30):
        out.append(_mk(i, yaw=_j(rng, 1.5), pitch=34.0 + _j(rng, 1.5),
                       relation="on_paper", below=False, write=True))
    return out


def sc_neck_rest(rng: random.Random) -> List[FrameFeatures]:
    """揉眼/托腮休息：手在桌面、头微侧、无邻座交互。"""
    out = []
    for i in range(30):
        out.append(_mk(i, yaw=6.0 + _j(rng), pitch=14.0 + _j(rng),
                       relation="on_paper", below=False, write=False,
                       tense=True))
    return out


# ============================================================
# 作弊行为场景（真值 = 作弊；应触发最终判定）
# ============================================================
def sc_peek_repeated(rng: random.Random) -> List[FrameFeatures]:
    """反复旁窥：转头看邻座→回正，重复 4 轮（高方差 + 高偏转持续）。"""
    out = []
    total = 40
    for i in range(total):
        phase = i % 9
        if 5 <= i and phase in (3, 4, 5, 6):   # 转头注视段
            out.append(_mk(i, yaw=48.0 + _j(rng), pitch=4.0 + _j(rng),
                           relation="toward_neighbor", below=False,
                           write=False))
        else:                                   # 回正书写段
            out.append(_mk(i, yaw=_j(rng), pitch=10.0 + _j(rng),
                           relation="on_paper", below=False, write=True))
    return out


def sc_steady_peek(rng: random.Random) -> List[FrameFeatures]:
    """持续转头面向邻座：单一中等信号但【持续】 3 秒。"""
    out = []
    for i in range(40):
        if i >= 6:
            out.append(_mk(i, yaw=52.0 + _j(rng, 1.5), pitch=4.0 + _j(rng),
                           relation="toward_neighbor", below=False,
                           write=False, tilt=12.0 + _j(rng)))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_pass_note(rng: random.Random) -> List[FrameFeatures]:
    """传递纸条：桌面下方手部靠近邻座并持续，伴随转头与纸条检出。"""
    out = []
    for i in range(40):
        if i >= 5:
            objs = [("note", 0.85)] if i >= 12 else []
            out.append(_mk(i, yaw=38.0 + _j(rng), pitch=16.0 + _j(rng),
                           relation="toward_neighbor", below=True,
                           cross=0.028 + abs(_j(rng, 0.004)), write=False,
                           tilt=10.0 + _j(rng), objs=objs))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_phone_use(rng: random.Random) -> List[FrameFeatures]:
    """使用手机：手机持续检出，低头看向桌面下方设备。"""
    out = []
    for i in range(40):
        if i >= 4:
            out.append(_mk(i, yaw=8.0 + _j(rng), pitch=42.0 + _j(rng),
                           relation="off_area", below=True, cross=None,
                           write=False, objs=[("phone", 0.9)]))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


def sc_gaze_drift(rng: random.Random) -> List[FrameFeatures]:
    """东张西望：头部大幅左右摆动，窗口内姿态方差显著偏大。"""
    out = []
    for i in range(40):
        sign = 1.0 if (i // 3) % 2 == 0 else -1.0
        out.append(_mk(i, yaw=sign * 42.0 + _j(rng), pitch=6.0 + _j(rng),
                       relation="off_area", below=False, write=False))
    return out


def sc_fidget_multihead(rng: random.Random) -> List[FrameFeatures]:
    """多动 + 频繁低头抬头 + 反复转头（姿态重复性 + 头部偏转）。"""
    out = []
    for i in range(40):
        phase = i % 6
        if phase in (0, 1):
            out.append(_mk(i, yaw=34.0 + _j(rng), pitch=20.0 + _j(rng),
                           relation="toward_neighbor", below=False,
                           write=False, tilt=28.0 + _j(rng)))
        elif phase in (2, 3):
            out.append(_mk(i, yaw=_j(rng), pitch=38.0 + _j(rng),
                           relation="off_area", below=False, write=False,
                           tilt=18.0 + _j(rng)))
        else:
            out.append(_mk(i, yaw=_j(rng), pitch=8.0 + _j(rng), below=False,
                           write=True))
    return out


# ============================================================
# 边界/困难样本（真值不可由画面判定；必须人工复核）
# ============================================================
def sc_amb_clock_glance(rng: random.Random) -> List[FrameFeatures]:
    """反复看向后方（如看挂钟）：运动学上与旁窥高度相似。

    这是本引擎【有意】会提示的情形 —— 项目书口径“视线落点反复指向
    非考试区域时即触发可疑信号”。但“看挂钟”与“看邻座答案”在画面上
    不可区分，因此必须交由监考/巡考员人工复核后处置。
    """
    out = []
    for i in range(40):
        phase = i % 10
        if phase in (2, 3, 4, 5):          # 每次转头持续 0.4s
            out.append(_mk(i, yaw=(-1) ** (i // 10) * 46.0 + _j(rng),
                           pitch=2.0 + _j(rng), relation="toward_back",
                           below=False, write=False))
        else:
            out.append(_mk(i, yaw=_j(rng), pitch=8.0 + _j(rng), below=False,
                           write=True))
    return out


def sc_amb_subthreshold_peek(rng: random.Random) -> List[FrameFeatures]:
    """极短促单次转头（0.4s）：低于实测的可检出持续时间边界。

    这是本引擎的【已知取舍】：为压低误报率设置了“持续超阈”闸门，
    代价是极短促的单次偷看（实测 ≤0.4s）不会触发。方向相反的
    误报/漏报权衡见项目书与本模块的 `duration_sweep`。
    """
    out = []
    for i in range(30):
        if 12 <= i <= 15:                  # 4 帧 = 0.4s
            out.append(_mk(i, yaw=50.0 + _j(rng), pitch=4.0 + _j(rng),
                           relation="toward_neighbor", below=False,
                           write=False))
        else:
            out.append(_mk(i, pitch=8.0 + _j(rng), below=False, write=True))
    return out


# ============================================================
# 场景注册表
# ============================================================
@dataclass
class ScenarioSpec:
    """场景定义。"""

    name: str
    truth: str            # "normal" | "cheat"
    generator: Callable[[random.Random], List[FrameFeatures]]
    label: str            # 中文名
    note: str = ""

    @property
    def is_cheat(self) -> bool:
        return self.truth == "cheat"


SCENARIOS: List[ScenarioSpec] = [
    ScenarioSpec("quiet_writing", "normal", sc_quiet_writing, "安静答题",
                 "低头看自己试卷、手在桌面书写"),
    ScenarioSpec("stretch", "normal", sc_stretch, "伸懒腰",
                 "瞬时抬头后仰 0.3s —— 应被抑制"),
    ScenarioSpec("posture_shift", "normal", sc_posture_shift, "调整坐姿",
                 "瞬时倾斜 0.4s —— 应被抑制"),
    ScenarioSpec("brief_glance", "normal", sc_brief_glance, "短暂分心",
                 "瞬时转头 0.3s —— 应被抑制"),
    ScenarioSpec("long_lookdown", "normal", sc_long_lookdown, "长时间低头",
                 "低头看自己试卷（on_paper）—— 非作弊"),
    ScenarioSpec("neck_rest", "normal", sc_neck_rest, "揉眼/托腮休息",
                 "头微侧、无邻座交互"),
    ScenarioSpec("peek_repeated", "cheat", sc_peek_repeated, "反复旁窥",
                 "转头-回正重复 4 轮，高方差 + 持续偏转"),
    ScenarioSpec("steady_peek", "cheat", sc_steady_peek, "持续转头面向邻座",
                 "单一中等信号但持续 3s"),
    ScenarioSpec("pass_note", "cheat", sc_pass_note, "传递纸条",
                 "桌面下手部靠邻座持续 + 转头 + 纸条检出"),
    ScenarioSpec("phone_use", "cheat", sc_phone_use, "使用手机",
                 "手机持续检出（直接证据）"),
    ScenarioSpec("gaze_drift", "cheat", sc_gaze_drift, "东张西望",
                 "头部大幅摆动，窗口方差大"),
    ScenarioSpec("fidget_multihead", "cheat", sc_fidget_multihead, "多动+反复转头",
                 "姿态重复性 + 头部偏转叠加"),
    # —— 边界样本：真值不可由画面判定，不计入误报/准确率，仅作说明 ——
    ScenarioSpec("amb_clock_glance", "ambiguous", sc_amb_clock_glance,
                 "反复看向后方(如挂钟)",
                 "运动学上与旁窥相似；引擎会提示，需人工复核"),
    ScenarioSpec("amb_subthreshold_peek", "ambiguous", sc_amb_subthreshold_peek,
                 "极短促单次转头(0.4s)",
                 "低于实测可检出边界；引擎按设计不提示，属漏报取舍"),
]


# ============================================================
# 帧级检测基线（无时间建模）
# ============================================================
class FrameLevelBaseline:
    """帧级单帧判定基线 —— 复现“帧级检测误报率高达 15.2%”的成因。

    帧级检测对【每一帧独立】判断：只要该帧有任一线索成立就报警，
    不做持续时间、频率或上下文约束。因此伸懒腰、短暂分心、调整坐姿
    都会触发误判。
    """

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()

    def frame_alarm(self, f: FrameFeatures) -> Tuple[bool, str]:
        """单帧判定：任一线索成立即报警。"""
        cues = frame_level_cues(f, self.cfg)
        return (True, cues[0]) if cues else (False, "")

    def alarm(self, frames: Sequence[FrameFeatures]) -> bool:
        """场景级判定：【任意一帧】报警即判为作弊。"""
        return any(self.frame_alarm(f)[0] for f in frames)

    def first_cue(self, frames: Sequence[FrameFeatures]) -> str:
        for f in frames:
            hit, cue = self.frame_alarm(f)
            if hit:
                return cue
        return ""


# ============================================================
# 仿真与评估
# ============================================================
@dataclass
class ScenarioResult:
    """单场景的仿真统计。"""

    name: str
    label: str
    truth: str
    note: str
    trials: int = 0
    baseline_alarms: int = 0
    engine_alarms: int = 0
    engine_correct_pattern: int = 0
    baseline_cue: str = ""
    mean_peak_risk: float = 0.0
    mean_suppressed_frames: float = 0.0
    example_pattern: str = ""
    example_risk: float = 0.0

    @property
    def is_cheat(self) -> bool:
        return self.truth == "cheat"

    @property
    def is_ambiguous(self) -> bool:
        return self.truth == "ambiguous"

    @property
    def baseline_rate(self) -> float:
        return self.baseline_alarms / self.trials if self.trials else 0.0

    @property
    def engine_rate(self) -> float:
        return self.engine_alarms / self.trials if self.trials else 0.0

    def baseline_correct(self) -> int:
        """帧级基线判定正确的次数（边界样本不计）。"""
        if self.is_cheat:
            return self.baseline_alarms
        return self.trials - self.baseline_alarms

    def engine_correct(self) -> int:
        if self.is_cheat:
            return self.engine_alarms
        return self.trials - self.engine_alarms

    def verdict(self) -> str:
        """引擎在本场景上的判定结论。"""
        if self.is_ambiguous:
            if self.engine_rate >= 0.5:
                return "提示(待复核)"
            return "未提示"
        if self.is_cheat:
            return "命中" if self.engine_rate >= 0.5 else "漏报"
        return "正确" if self.engine_rate <= 0.5 else "误报"


@dataclass
class SimulationReport:
    """整体仿真报告。"""

    trials_per_scenario: int = 0
    seed: int = 0
    results: List[ScenarioResult] = field(default_factory=list)
    cfg_desc: str = ""

    # ---------- 汇总指标 ----------
    def _totals(self, baseline: bool = False) -> Tuple[int, int, int, int]:
        """Returns: (tp, fp, tn, fn)。边界样本不计入。"""
        tp = fp = tn = fn = 0
        for r in self.results:
            if r.is_ambiguous:
                continue
            alarmed = r.baseline_alarms if baseline else r.engine_alarms
            if r.is_cheat:
                tp += alarmed
                fn += r.trials - alarmed
            else:
                fp += alarmed
                tn += r.trials - alarmed
        return tp, fp, tn, fn

    def metrics(self, baseline: bool = False) -> Dict[str, float]:
        """误报率 / 提示率（召回）/ 准确率 / 精确率。"""
        tp, fp, tn, fn = self._totals(baseline)
        n = tp + fp + tn + fn
        n_normal = tn + fp
        n_cheat = tp + fn
        return {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn, "n": n,
            "false_positive_rate": (fp / n_normal) if n_normal else 0.0,
            "recall": (tp / n_cheat) if n_cheat else 0.0,
            "accuracy": ((tp + tn) / n) if n else 0.0,
            "precision": (tp / (tp + fp)) if (tp + fp) else 0.0,
        }

    def summary(self) -> dict:
        return {
            "trials_per_scenario": self.trials_per_scenario,
            "seed": self.seed,
            "scenarios": len(self.results),
            "baseline": self.metrics(True),
            "engine": self.metrics(False),
            "config": self.cfg_desc,
        }

    # ---------- 文本报告 ----------
    def format_table(self) -> str:
        main = [r for r in self.results if not r.is_ambiguous]
        amb = [r for r in self.results if r.is_ambiguous]
        lines = []
        lines.append("=" * 92)
        lines.append("合成场景仿真：帧级检测基线 vs 时空建模引擎"
                     "（合成数据，非实测）")
        lines.append("=" * 92)
        lines.append("{:<20} {:<6} {:>10} {:>10} {:>12} {:>10}".format(
            "场景", "真值", "帧级基线", "时空引擎", "平均峰值风险", "引擎判定"))
        lines.append("-" * 92)
        for r in main:
            truth = "作弊" if r.is_cheat else "正常"
            lines.append("{:<20} {:<6} {:>10} {:>10} {:>12.1f} {:>10}".format(
                r.label, truth,
                "{:.0f}%".format(r.baseline_rate * 100),
                "{:.0f}%".format(r.engine_rate * 100),
                r.mean_peak_risk, r.verdict()))
        lines.append("-" * 92)

        bm, em = self.metrics(True), self.metrics(False)
        lines.append("{:<20} {:<6} {:>10} {:>10}".format("汇总", "", "帧级基线", "时空引擎"))
        lines.append("{:<20} {:<6} {:>9.1f}% {:>9.1f}%".format(
            "误报率", "", bm["false_positive_rate"] * 100,
            em["false_positive_rate"] * 100))
        lines.append("{:<20} {:<6} {:>9.1f}% {:>9.1f}%".format(
            "提示率(召回)", "", bm["recall"] * 100, em["recall"] * 100))
        lines.append("{:<20} {:<6} {:>9.1f}% {:>9.1f}%".format(
            "准确率", "", bm["accuracy"] * 100, em["accuracy"] * 100))
        lines.append("{:<20} {:<6} {:>9.1f}% {:>9.1f}%".format(
            "精确率", "", bm["precision"] * 100, em["precision"] * 100))
        lines.append("-" * 92)

        if amb:
            lines.append("边界样本（真值不可由画面判定，不计入上表指标，"
                         "须人工复核）：")
            for r in amb:
                lines.append("  · {:<18} 引擎提示率 {:>5.0f}%  →  {} ({})".format(
                    r.label, r.engine_rate * 100, r.verdict(), r.note))
            lines.append("-" * 92)

        lines.append("每场景 {} 次随机试验 · 随机种子 {} · {}".format(
            self.trials_per_scenario, self.seed, self.cfg_desc))
        lines.append("注：以上为【合成数据仿真】结果，用于验证算法逻辑，"
                     "不代表真实考场实测性能。")
        lines.append("=" * 92)
        return "\n".join(lines)


def run_simulation(trials_per_scenario: int = 100, seed: int = 2026,
                   cfg: Optional[Config] = None,
                   scenarios: Optional[Sequence[ScenarioSpec]] = None
                   ) -> SimulationReport:
    """按场景批量仿真。

    Args:
        trials_per_scenario: 每个场景的随机试验次数。
        seed: 随机种子（保证可复现）。
        cfg: 引擎配置。
        scenarios: 场景列表，默认全部。

    Returns:
        SimulationReport。
    """
    cfg = cfg or Config(fps=FPS)
    specs = list(scenarios or SCENARIOS)
    baseline = FrameLevelBaseline(cfg)
    results: List[ScenarioResult] = []

    for spec in specs:
        res = ScenarioResult(name=spec.name, label=spec.label,
                             truth=spec.truth, note=spec.note)
        peak_sum = 0.0
        supp_sum = 0.0
        for t in range(trials_per_scenario):
            rng = random.Random(seed * 100003 + hash(spec.name) % 9973 + t)
            frames = spec.generator(rng)

            if baseline.alarm(frames):
                res.baseline_alarms += 1
                if not res.baseline_cue:
                    res.baseline_cue = baseline.first_cue(frames)

            det = CheatDetector(cfg)
            det.push_many(frames, record=False)
            det.finish()
            summary = det.summary()
            peak = summary.get("peak_risk", 0.0)
            peak_sum += peak
            supp_sum += summary.get("suppressed_frames", 0)

            alarmed = summary.get("alarm_events", 0) > 0
            if alarmed:
                res.engine_alarms += 1
            if not res.example_pattern and det.events:
                res.example_pattern = det.events[0].peak_pattern
                res.example_risk = det.events[0].peak_risk

        res.trials = trials_per_scenario
        res.mean_peak_risk = peak_sum / max(1, trials_per_scenario)
        res.mean_suppressed_frames = supp_sum / max(1, trials_per_scenario)
        results.append(res)

    return SimulationReport(trials_per_scenario=trials_per_scenario, seed=seed,
                            results=results, cfg_desc=cfg.describe())


def duration_sweep(cfg: Optional[Config] = None, max_frames: int = 20,
                   trials: int = 40, seed: int = 1000, yaw: float = 50.0,
                   relation: str = "toward_neighbor",
                   lead_in: int = 10, total_frames: int = 40
                   ) -> List[Dict[str, float]]:
    """扫描“单次转头持续时间 → 触发率”，量化瞬时抑制边界。

    这是对项目书“瞬时动作被自动抑制”这一说法的【可复现量化验证】：
    固定转头幅度（默认 yaw=50°、朝向邻座），逐档增加持续时间，
    观察触发率从 0% 跃升到 100% 的位置，即为该强度下的
    最短可检出持续时间。

    Returns:
        每档一行字典：duration_s / frames / trigger_rate / mean_peak_risk。
    """
    cfg = cfg or Config(fps=FPS)
    rows: List[Dict[str, float]] = []
    for k in range(1, max_frames + 1):
        hits = 0
        peak_sum = 0.0
        for t in range(trials):
            rng = random.Random(seed + t * 37 + k)
            frames = []
            for i in range(total_frames):
                if lead_in <= i < lead_in + k:
                    frames.append(_mk(i, yaw=yaw + _j(rng),
                                      pitch=4.0 + _j(rng),
                                      relation=relation, below=False,
                                      write=False))
                else:
                    frames.append(_mk(i, pitch=8.0 + _j(rng), below=False,
                                      write=True))
            det = CheatDetector(cfg)
            det.push_many(frames, record=False)
            det.finish()
            s = det.summary()
            if s["alarm_events"] > 0:
                hits += 1
            peak_sum += s["peak_risk"]
        rows.append({
            "duration_s": k / cfg.fps,
            "frames": k,
            "trigger_rate": hits / trials if trials else 0.0,
            "mean_peak_risk": peak_sum / trials if trials else 0.0,
        })
    return rows


def format_duration_sweep(rows: Sequence[Dict[str, float]],
                          width: int = 34) -> str:
    """把持续时间扫描结果格式化为文本表。"""
    lines = []
    lines.append("瞬时抑制边界扫描（单次转头 yaw=50°、朝向邻座）：")
    lines.append("{:>9} {:>7} {:>10}  {}".format("时长(s)", "帧数", "触发率", "触发率"))
    lines.append("-" * 74)
    boundary = None
    for r in rows:
        filled = int(round(r["trigger_rate"] * width))
        bar = "#" * filled + "." * (width - filled)
        lines.append("{:>9.1f} {:>7d} {:>9.0f}%  {}".format(
            r["duration_s"], r["frames"], r["trigger_rate"] * 100, bar))
        if boundary is None and r["trigger_rate"] >= 0.5:
            boundary = r["duration_s"]
    lines.append("-" * 74)
    if boundary is not None:
        lines.append("最短可检出持续时间（触发率≥50%）：约 {:.1f}s "
                     "—— 更短的动作被自动抑制。".format(boundary))
    else:
        lines.append("扫描范围内未出现稳定触发。")
    lines.append("说明：该边界由“滑动窗口 + 持续超阈闸门 + 轨迹平滑”"
                 "共同决定，是合成数据下的实测值。")
    return "\n".join(lines)


def format_trajectory(frames: Sequence[FrameFeatures],
                      cfg: Optional[Config] = None,
                      width: int = 40, step: int = 1,
                      title: str = "") -> str:
    """打印单场景的风险分数连续轨迹（用于演示瞬时抑制）。

    高亮说明：
      · `*` = 帧级判据会报警，但被时空建模抑制（瞬时动作）；
      · `#` = 处于告警状态（风险持续超阈）；
      · `.` = 正常。

    Args:
        step: 抽稀显示，每 step 帧打印一行（1=全部打印）。
    """
    cfg = cfg or Config(fps=FPS)
    det = CheatDetector(cfg)
    rows = det.push_many(frames, record=True)
    det.finish()

    lines = []
    if title:
        lines.append(title)
    lines.append("风险轨迹（阈值 {:.0f}，窗口 {} 帧/{:.1f}s，最短持续 {} 帧）：".format(
        cfg.risk_threshold, cfg.window_frames, cfg.window_seconds,
        cfg.min_sustain_frames))
    lines.append("  {:>5} {:>6}  {:<{w}} {}   状态".format(
        "t(s)", "风险", "轨迹", "", w=width))
    lines.append("  " + "-" * (width + 34))
    step = max(1, int(step))
    for idx, r in enumerate(rows):
        if idx % step and idx != len(rows) - 1:
            continue
        bar = int(round(r.risk / 100.0 * width))
        if r.triggered:
            mark, state = "#", "告警中"
        elif r.suppressed:
            mark, state = "*", "帧级误报已抑制"
        else:
            mark, state = ".", "正常"
        lines.append("  {:>5.2f} {:>6.1f}  |{:<{w}} {}   {}".format(
            r.timestamp, r.risk, "=" * bar, mark, state, w=width))
    lines.append("  事件：" + ("；".join(
        "#{} {:.1f}s 起(确认于 {:.1f}s) 峰值{:.1f} {} {}".format(
            i + 1, e.onset_time,
            e.confirm_time if e.confirm_time is not None else -1.0,
            e.peak_risk, e.level, e.peak_pattern)
        for i, e in enumerate(det.events)) if det.events else "无告警事件"))
    return "\n".join(lines)
