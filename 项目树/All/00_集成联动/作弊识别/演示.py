# -*- coding: utf-8 -*-
"""考生作弊智能识别 —— 核心逻辑演示。

直接运行：
    python 演示.py
    python 演示.py --trials 200 --seed 2026

演示内容（对应项目书《考生作弊智能识别核心逻辑》）：
  一、判定链路与可量化参数
  二、风险评分框架（权重方向来自项目书表格）
  三、瞬时动作被自动抑制（伸懒腰 / 短暂分心）
  四、持续动作序列构成行为模式并触发告警（旁窥 / 传递 / 手机）
  五、瞬时抑制边界扫描（多短算“瞬时”）
  六、全场景仿真：帧级检测 vs 时空建模
  七、告警的可解释输出（给人工复核用）

【注意】 全部数值来自【合成数据仿真】，用于验证算法逻辑，非真实考场实测。
"""

import argparse
import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheatdetect import CheatDetector, Config, SignalWeights  # noqa: E402
from cheatdetect.patterns import describe  # noqa: E402
from cheatdetect.scoring import CN_NAME, WEIGHT_DIRECTION  # noqa: E402
from cheatdetect.simulate import (  # noqa: E402
    SCENARIOS, duration_sweep, format_duration_sweep, format_trajectory,
    run_simulation,
)


def section(no: str, title: str) -> None:
    print()
    print("=" * 88)
    print("{}  {}".format(no, title))
    print("=" * 88)


def build_cfg() -> Config:
    return Config(fps=10.0)


# ---------------------------------------------------------------
def part1_config(cfg: Config) -> None:
    section("一、", "判定链路与可量化参数")
    print("""
判定链路（不是判断“某个动作本身是否作弊”，而是判断动作序列是否
构成了“获取或传递答案”的行为模式）：

  单帧多模态特征
    → 滑动时间窗口（{win:.1f}s / {wf} 帧）聚合多模态特征
    → 各信号强度 × 有效权重（基础权重 × 持续性增益）加权求和
    → 指数平滑得到风险分数的【连续轨迹】
    → 风险分数【持续】超过阈值（≥{sus:.1f}s / {sf} 帧）才触发最终判定
    → 单次瞬时动作被自动抑制
""".format(win=cfg.window_seconds, wf=cfg.window_frames,
           sus=cfg.min_sustain_seconds, sf=cfg.min_sustain_frames))
    print("关键参数：{}".format(cfg.describe()))
    print("风险阈值 {:.0f}（橙色预警） / {:.0f}（红色预警）".format(
        cfg.risk_threshold, cfg.high_risk_threshold))


def part2_weights(cfg: Config) -> None:
    section("二、", "风险评分框架（权重方向对齐项目书表格）")
    w = SignalWeights()
    print("{:<18} {:<10} {:>8}  {}".format("信号类型", "权重方向", "基础权重", "角色"))
    print("-" * 88)
    roles = {
        "head_deviation": "持续偏离考试区域、角度超阈值",
        "head_variance": "时间窗口内 yaw/pitch 波动大",
        "hand_interaction": "桌面下方手部关键点靠近邻座",
        "prohibited_object": "检测到手机/纸条等直接证据",
        "posture_repetition": "低头-抬头等模式高频出现",
        "expression_aux": "紧张与中性交替（辅助项）",
    }
    for row in w.table():
        print("{:<18} {:<10} {:>8.2f}  {}".format(
            row["cn"], row["direction"], row["weight"], roles[row["signal"]]))
    print("-" * 88)
    print("""权重方向决定“触发速度”，时间维度决定“是否触发”：
  · 违禁物体（极高）/ 手部跨考生交互（高）：持续即触发；
  · 单个中等信号（头部偏转等）：【瞬时】不触发，
    但在整个窗口内【持续】时可达阈值（对应“持续转头面向邻座”）；
  · 单个中等信号只出现一瞬：持续占比低 → 得分被显著压低 → 抑制。
  持续时间既作闸门（gate），又作乘法增益（boost）。""")


def part3_instant(cfg: Config) -> None:
    section("三、", "瞬时动作被自动抑制（伸懒腰 / 短暂分心）")
    for name in ("stretch", "brief_glance"):
        spec = next(s for s in SCENARIOS if s.name == name)
        rng = random.Random(2026)
        frames = spec.generator(rng)
        det = CheatDetector(cfg)
        det.push_many(frames, record=True)
        det.finish()
        s = det.summary()
        print()
        print(format_trajectory(
            spec.generator(random.Random(2026)), cfg, width=36, step=2,
            title="场景：{}（{}）".format(spec.label, spec.note)))
        print("  → 结论：告警事件 {} 个，被抑制的帧级误报 {} 帧。".format(
            s["alarm_events"], s["suppressed_frames"]))
        print("  → 帧级检测会在此场景报警，时空建模将其抑制。")
    print("""
要点：这两个场景在【帧级判据】下都会报警（并因此造成高误报率），
      但风险分数没有“持续”超过阈值，因此不触发最终判定。""")


def part4_sustained(cfg: Config) -> None:
    section("四、", "持续动作序列构成行为模式并触发告警")
    for name, step in (("steady_peek", 2), ("pass_note", 2), ("phone_use", 2)):
        spec = next(s for s in SCENARIOS if s.name == name)
        rng = random.Random(2026)
        frames = spec.generator(rng)
        print()
        print(format_trajectory(
            spec.generator(random.Random(2026)), cfg, width=36, step=step,
            title="场景：{}（{}）".format(spec.label, spec.note)))
        det = CheatDetector(cfg)
        det.push_many(frames, record=False)
        det.finish()
        if det.events:
            ev = det.events[0]
            print("  → 判定：{}，{:.1f}s 起持续可疑，确认于 {:.1f}s，"
                  "峰值风险 {:.1f}。".format(ev.level, ev.onset_time,
                                             ev.confirm_time or 0.0,
                                             ev.peak_risk))
            print("  → 模式：{}".format(describe(ev.peak_pattern)))
    print("""
要点：动作【序列】（持续时间 + 频率 + 上下文）构成模式后才触发，
      且触发时刻（确认）晚于行为起点（onset）—— 这正是“持续超阈”的体现。""")


def part5_boundary(cfg: Config) -> None:
    section("五、", "瞬时抑制边界扫描（多短算“瞬时”）")
    print()
    print(format_duration_sweep(duration_sweep(cfg, trials=40, seed=1000)))


def part6_simulation(cfg: Config, trials: int, seed: int):
    section("六、", "全场景仿真：帧级检测 vs 时空建模")
    print()
    rep = run_simulation(trials_per_scenario=trials, seed=seed, cfg=cfg)
    print(rep.format_table())
    return rep


def part7_explain(cfg: Config) -> None:
    section("七、", "告警的可解释输出（供人工复核）")
    spec = next(s for s in SCENARIOS if s.name == "pass_note")
    frames = spec.generator(random.Random(2026))
    det = CheatDetector(cfg)
    results = det.push_many(frames, record=True)
    det.finish()

    # 取告警确认后的第一帧，展示完整解释
    target = None
    for r in results:
        if r.triggered:
            target = r
            break
    if target is None:
        print("（本场景未触发，无法展示）")
        return

    print("场景：{}（{}）".format(spec.label, spec.note))
    print("时刻：t={:.2f}s  帧号={}  风险分={:.1f}（阈值 {:.0f}）"
          "  级别={}  持续={} 帧".format(
              target.timestamp, target.frame_id, target.risk,
              target.threshold, target.level, target.streak_frames))
    print("行为模式：{}".format(target.pattern))
    print("模式说明：{}".format(target.pattern_desc))
    print()
    print("信号贡献分解（人工复核依据）：")
    print("  {:<18} {:<8} {:>8} {:>10} {:>12}".format(
        "信号", "权重方向", "强度", "持续占比", "贡献分"))
    print("  " + "-" * 62)
    contrib = target.contributions or {}
    inten = target.intensities.as_dict() if target.intensities else {}
    pers = target.persistence or {}
    for key in sorted(contrib, key=lambda k: contrib[k], reverse=True):
        print("  {:<18} {:<8} {:>8.2f} {:>10.2f} {:>12.1f}".format(
            CN_NAME[key], WEIGHT_DIRECTION[key], inten.get(key, 0.0),
            pers.get(key, 0.0), contrib[key]))
    print("  " + "-" * 62)
    print("  合计风险分：{:.1f}".format(target.risk))
    print()
    print("可解释理由：{}".format(target.reason))
    print()
    print("窗口时间维度统计：")
    w = target.window
    if w is not None:
        print("  · 窗口 {} 帧 / {:.1f}s，超阈帧占比 {:.2f}".format(
            w.size, w.duration, w.over_threshold_ratio))
        print("  · 头部姿态方差 {:.4f} rad^2（yaw {:.4f} + pitch {:.4f}）".format(
            w.head_variance, w.head_var_yaw, w.head_var_pitch))
        print("  · 姿态往复 {:.2f} 次/秒，手部交互连续 {} 帧".format(
            w.repetition_rate, w.hand_streak))
        print("  · 违禁物体出现 {} 帧（占比 {:.2f}）".format(
            w.object_frames, w.object_ratio))
    print()
    print("本次会话全部告警事件：")
    for i, ev in enumerate(det.event_dicts(), 1):
        print("  #{}. {}".format(i, ev))


def part8_conclusion(cfg: Config, rep) -> None:
    section("八、", "结论与重要声明")
    bm, em = rep.metrics(True), rep.metrics(False)
    print("""
1. 判定对象是【动作序列构成的行为模式】，不是单个动作本身。
   单个中等信号（如转头）瞬时出现不触发；持续出现或与其他信号叠加才触发。

2. “持续时间”是决定是否触发的关键，本引擎把它实现为两件事：
   ① 闸门：风险分数持续超阈 ≥{sus:.1f}s 才触发最终判定；
   ② 增益：信号在窗口内的持续占比提升其有效权重。
   合成数据下实测：≤0.4s 的瞬时动作被完全抑制，≥0.6s 稳定触发。

3. 时空建模与帧级单帧判断的对比（【合成数据仿真】，{trials} 次/场景）：
   帧级基线：误报率 {bfp:.1f}% · 提示率 {bre:.1f}% · 准确率 {bacc:.1f}%
   时空建模：误报率 {efp:.1f}% · 提示率 {ere:.1f}% · 准确率 {eacc:.1f}%

4. 系统只做“提示与留痕”，最终裁定权在监考/巡考人员 ——
   对“看挂钟”与“看邻座答案”这类画面不可区分的情形，
   引擎会提示，必须由人工复核处置（见第六节边界样本）。

【注意】 重要声明：本演示输出的一切数值均为【合成数据仿真结果】，
   用于验证算法逻辑是否正确工作，【不是】真实考场实测数据，
   不得作为实测精度结论使用。项目书中引用的
   “误报率 15.2%→4.3%、准确率 84.6%→95.1%”为外部引用值，
   与本演示的仿真数值不是同一来源，不应互相替代或混淆。
""".format(sus=cfg.min_sustain_seconds, trials=rep.trials_per_scenario,
           bfp=bm["false_positive_rate"] * 100,
           bre=bm["recall"] * 100, bacc=bm["accuracy"] * 100,
           efp=em["false_positive_rate"] * 100,
           ere=em["recall"] * 100, eacc=em["accuracy"] * 100))


# ---------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="考生作弊智能识别核心逻辑演示")
    ap.add_argument("--trials", type=int, default=100,
                    help="每个场景的仿真试验次数（默认 100）")
    ap.add_argument("--seed", type=int, default=2026, help="随机种子")
    args = ap.parse_args(argv)

    # 输出编码：【不做任何改动】，交给 Python 的默认行为，理由：
    #   · 输出到真实控制台时，Windows 上 Python 通过 WriteConsoleW 直接写
    #     Unicode，中文在 GBK(936) / UTF-8(65001) 控制台下都能正确显示；
    #     若强行改成 utf-8，反而会在 936 控制台变成乱码。
    #   · 输出被重定向（管道/文件）时，Python 使用系统区域编码（简体中文
    #     Windows 即 GBK），与 运行演示.bat 自身的输出保持一致，
    #     重定向得到的日志不会一半 GBK 一半 UTF-8。
    # 如需强制 UTF-8 输出（例如接入其他工具链），在外部设置环境变量
    # PYTHONIOENCODING=utf-8 即可，无需改代码。

    cfg = build_cfg()
    print("=" * 88)
    print("考生作弊智能识别 · 核心逻辑演示")
    print("AI 识别作弊的核心逻辑不是判断“某个动作本身是否作弊”，")
    print("而是判断动作序列是否构成了“获取或传递答案”的行为模式。")
    print("=" * 88)

    part1_config(cfg)
    part2_weights(cfg)
    part3_instant(cfg)
    part4_sustained(cfg)
    part5_boundary(cfg)
    rep = part6_simulation(cfg, args.trials, args.seed)
    part7_explain(cfg)
    part8_conclusion(cfg, rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
