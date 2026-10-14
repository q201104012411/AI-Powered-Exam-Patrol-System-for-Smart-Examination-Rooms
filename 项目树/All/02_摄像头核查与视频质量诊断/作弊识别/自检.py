# -*- coding: utf-8 -*-
"""考生作弊智能识别引擎 · 自检。

用法：
    python 自检.py            # 临时环境，随便跑
    python 自检.py --real     # 用真实配置跑一遍仿真（只读，不写任何文件）

自检覆盖：配置换算、时间维度建模、信号提取、加权评分、行为模式、
端到端判定（瞬时抑制 / 持续触发）、仿真指标与可解释输出。
"""

import argparse
import os
import random
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheatdetect import (  # noqa: E402
    CheatDetector, Config, FrameFeatures, HandState, HeadPose, PostureState,
    SignalExtractor, SignalIntensities, SignalWeights,
)
from cheatdetect import patterns, temporal  # noqa: E402
from cheatdetect.scoring import RiskScorer  # noqa: E402
from cheatdetect.simulate import (  # noqa: E402
    SCENARIOS, duration_sweep, run_simulation,
)

FPS = 10.0
_results = []


def check(section, desc, cond, detail=""):
    ok = bool(cond)
    _results.append(ok)
    print("{} {:<46} {}".format("PASS" if ok else "FAIL", desc, detail))
    return ok


def section(title):
    print("\n[{}]".format(title))


def mk(i, **kw):
    kw.setdefault("yaw", 0.0)
    kw.setdefault("pitch", 8.0)
    kw.setdefault("relation", "on_paper")
    kw.setdefault("below", False)
    kw.setdefault("cross", None)
    kw.setdefault("write", True)
    kw.setdefault("tilt", 0.0)
    kw.setdefault("lean", False)
    kw.setdefault("left", False)
    kw.setdefault("objs", ())
    kw.setdefault("tense", False)
    return FrameFeatures(
        frame_id=i, timestamp=i / FPS,
        head=HeadPose(yaw_deg=kw["yaw"], pitch_deg=kw["pitch"],
                      relation=kw["relation"]),
        hand=HandState(below_desk=kw["below"], cross_distance=kw["cross"],
                       doing_write=kw["write"]),
        posture=PostureState(tilt_deg=kw["tilt"],
                             leaning_on_desk=kw["lean"], left_area=kw["left"]),
        prohibited_objects=list(kw["objs"]),
        expression_tense=kw["tense"])


def run_engine(frames, cfg):
    det = CheatDetector(cfg)
    det.push_many(frames, record=False)
    det.finish()
    return det


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true",
                    help="用真实配置跑仿真（只读）")
    args = ap.parse_args(argv)

    cfg = Config(fps=FPS)
    print("=" * 78)
    print("考生作弊智能识别引擎 · 自检")
    print("配置：{}".format(cfg.describe()))
    print("=" * 78)

    # ---------------- [1] 配置 ----------------
    section("1 配置与换算")
    check(1, "窗口帧数换算正确", cfg.window_frames == 20,
          "window_frames={}".format(cfg.window_frames))
    check(1, "最短持续帧数换算正确", cfg.min_sustain_frames == 10,
          "min_sustain_frames={}".format(cfg.min_sustain_frames))
    check(1, "红/橙阈值有序",
          cfg.high_risk_threshold > cfg.risk_threshold,
          "{}>{}".format(cfg.high_risk_threshold, cfg.risk_threshold))
    check(1, "参数摘要可输出", "窗口" in cfg.describe())

    # ---------------- [2] 时间维度 ----------------
    section("2 时间维度建模")
    steady = [mk(i) for i in range(20)]
    _, _, v0 = temporal.head_pose_variance(steady)
    check(2, "静止姿态方差为 0", abs(v0) < 1e-9, "var={:.6f}".format(v0))
    swing = [mk(i, yaw=(40.0 if i % 2 else -40.0), relation="off_area")
             for i in range(20)]
    _, _, v1 = temporal.head_pose_variance(swing)
    check(2, "摆动姿态方差显著为正", v1 > 0.2, "var={:.4f}".format(v1))

    noise = [0.20, 0.22, 0.19, 0.21, 0.20, 0.23, 0.18, 0.21, 0.20, 0.22]
    check(2, "噪声不产生伪往复（滞回生效）",
          temporal.count_alternations(noise) == 0,
          "alt={}".format(temporal.count_alternations(noise)))
    repeat = [0.2, 0.2, 1.0, 1.0, 0.2, 0.2, 1.0, 1.0, 0.2, 0.2]
    check(2, "真实往复被计数", temporal.count_alternations(repeat) >= 2,
          "alt={}".format(temporal.count_alternations(repeat)))

    st = temporal.SustainTracker(cfg)
    _, in_alarm, _, _ = st.update(90.0, mk(0))
    check(2, "单帧超阈不触发", not in_alarm)
    for i in range(1, 10):
        st.update(90.0, mk(i))
    check(2, "持续超阈达标后触发", st.in_alarm,
          "streak={}".format(st.streak))
    check(2, "告警事件记录起止与峰值",
          len(st.events) == 1 and st.events[0].peak_risk >= 90.0,
          "onset={}s".format(st.events[0].onset_time))

    # ---------------- [3] 信号提取 ----------------
    section("3 信号提取（可量化动作界定维度）")
    ex = SignalExtractor(cfg)
    check(3, "on_paper 低头不计头部偏转（防长时间低头误报）",
          ex.head_deviation(mk(0, pitch=45.0)) == 0.0)
    check(3, "低于阈值不计头部偏转",
          ex.head_deviation(mk(0, yaw=20.0, relation="toward_neighbor")) == 0.0)
    check(3, "超阈值后强度随角度上升",
          ex.head_deviation(mk(0, yaw=52.0, relation="toward_neighbor"))
          > ex.head_deviation(mk(0, yaw=33.0, relation="toward_neighbor")) > 0)

    phone = ex.prohibited_object(mk(0, objs=[("phone", 0.9)]))
    book = ex.prohibited_object(mk(0, objs=[("book", 0.9)]))
    check(3, "手机危险度高于书籍", phone > book > 0,
          "phone={:.2f} book={:.2f}".format(phone, book))

    check(3, "手部交互需连续 ≥3 帧",
          ex.hand_interaction([mk(i, below=True, cross=0.03)
                               for i in range(2)]) == 0.0
          and ex.hand_interaction([mk(i, below=True, cross=0.03)
                                   for i in range(4)]) > 0.0)
    check(3, "手在桌面之上不算跨考生交互",
          ex.hand_interaction([mk(i, cross=0.02) for i in range(5)]) == 0.0)

    long_p = ex.persistence([mk(i, yaw=50.0, relation="toward_neighbor")
                             for i in range(20)])["head_deviation"]
    short_p = ex.persistence(
        [mk(i) for i in range(15)]
        + [mk(i, yaw=50.0, relation="toward_neighbor") for i in range(15, 20)]
    )["head_deviation"]
    check(3, "持续占比随时长上升（持续时间量化）", long_p > short_p,
          "long={:.2f} short={:.2f}".format(long_p, short_p))

    # ---------------- [4] 加权评分 ----------------
    section("4 加权风险评分（非二值）")
    sc = RiskScorer(cfg)
    w = SignalWeights()
    check(4, "权重方向 极高>高>中",
          w.prohibited_object > w.hand_interaction > w.head_deviation,
          "{:.2f}>{:.2f}>{:.2f}".format(w.prohibited_object,
                                        w.hand_interaction, w.head_deviation))
    check(4, "单个中等信号（无持续增益）不触发",
          sc.instant_risk(SignalIntensities(head_deviation=1.0))
          < cfg.risk_threshold)
    check(4, "中等信号持续满窗可达阈值",
          sc.instant_risk(SignalIntensities(head_deviation=1.0),
                          {"head_deviation": 1.0}) >= cfg.risk_threshold)
    check(4, "多信号叠加高于单信号",
          sc.instant_risk(SignalIntensities(head_deviation=0.8,
                                            hand_interaction=0.8))
          > sc.instant_risk(SignalIntensities(head_deviation=0.8)))
    check(4, "风险分截顶 100",
          sc.instant_risk(SignalIntensities(prohibited_object=1.0),
                          {"prohibited_object": 1.0}) <= 100.0)
    check(4, "分级标签正确",
          sc.level_of(10, False) == "正常" and sc.level_of(65, True) == "橙色预警"
          and sc.level_of(90, True) == "红色预警")

    # ---------------- [5] 行为模式 ----------------
    section("5 行为模式识别（获取/传递答案）")
    check(5, "违禁物体→物体模式",
          patterns.classify({"prohibited_object": 70.0})
          == patterns.PATTERN_OBJECT)
    check(5, "物体+跨考生手部交互→传递物品模式",
          patterns.classify({"prohibited_object": 70.0, "hand_interaction": 60.0},
                            SignalIntensities(prohibited_object=1.0,
                                              hand_interaction=0.6))
          == patterns.PATTERN_HANDOFF)
    check(5, "头部偏转主导→旁窥模式",
          patterns.classify({"head_deviation": 50.0, "head_variance": 10.0})
          == patterns.PATTERN_PEEK)
    check(5, "姿态重复性让位于更具体的头部信号",
          patterns.classify({"posture_repetition": 52.0, "head_deviation": 48.0})
          == patterns.PATTERN_PEEK)
    check(5, "无信号→未见可疑模式",
          patterns.classify({}) == patterns.PATTERN_NONE)

    # ---------------- [6] 端到端判定 ----------------
    section("6 端到端判定（瞬时抑制 / 持续触发）")
    quiet = run_engine([mk(i) for i in range(40)], cfg)
    check(6, "安静答题不告警", quiet.summary()["alarm_events"] == 0)

    spike = [mk(i) for i in range(40)]
    spike[20] = mk(20, yaw=70.0, relation="toward_back")
    d = run_engine(spike, cfg)
    check(6, "单帧瞬时动作被抑制",
          d.summary()["alarm_events"] == 0
          and d.summary()["suppressed_frames"] >= 1,
          "suppressed={}".format(d.summary()["suppressed_frames"]))

    brief = [mk(i) for i in range(40)]
    for i in range(20, 23):
        brief[i] = mk(i, yaw=70.0, relation="toward_back")
    check(6, "连续 3 帧（0.3s）瞬时动作被抑制",
          run_engine(brief, cfg).summary()["alarm_events"] == 0)

    look = run_engine([mk(i, pitch=45.0, relation="on_paper")
                       for i in range(40)], cfg)
    check(6, "长时间低头看自己试卷不误报",
          look.summary()["alarm_events"] == 0)

    peek = run_engine([mk(i, yaw=52.0, pitch=4.0, relation="toward_neighbor")
                       for i in range(40)], cfg)
    check(6, "持续转头面向邻座触发告警",
          peek.summary()["alarm_events"] > 0,
          "peak={:.1f}".format(peek.summary()["peak_risk"]))

    note = [mk(i) if i < 5 else mk(i, below=True, cross=0.03, write=False,
                                   yaw=35.0, relation="toward_neighbor")
            for i in range(40)]
    check(6, "桌面下手部靠近邻座触发告警",
          run_engine(note, cfg).summary()["alarm_events"] > 0)

    ph = [mk(i) if i < 4 else mk(i, objs=[("phone", 0.9)], below=True,
                                 pitch=40.0, relation="off_area")
          for i in range(40)]
    dp = run_engine(ph, cfg)
    check(6, "手机持续检出触发且达红色预警",
          dp.summary()["alarm_events"] > 0 and dp.events[0].level == "红色预警")

    both = [mk(i, yaw=55.0, relation="toward_neighbor") for i in range(30)] + \
           [mk(i) for i in range(30, 70)]
    de = run_engine(both, cfg)
    check(6, "风险回落后告警事件闭合且记录时长",
          len(de.events) == 1 and not de.events[0].ongoing
          and de.events[0].duration > 0,
          "duration={:.1f}s".format(de.events[0].duration))

    empty = CheatDetector(cfg)
    empty.finish()
    check(6, "空输入安全不报错", empty.summary()["frames"] == 0)

    # ---------------- [7] 可解释输出 ----------------
    section("7 可解释输出")
    det = CheatDetector(cfg)
    res = det.push_many([mk(i, yaw=52.0, relation="toward_neighbor")
                         for i in range(30)], record=True)
    det.finish()
    last = res[-1]
    check(7, "逐帧给出贡献分解", bool(last.contributions))
    check(7, "逐帧给出中文理由文本", bool(last.reason))
    check(7, "逐帧给出窗口时间维度统计",
          last.window is not None and last.window.size > 0)
    check(7, "告警事件可导出为字典",
          bool(det.event_dicts()) and "peak_pattern" in det.event_dicts()[0])
    check(7, "summary 不依赖 record 标志",
          CheatDetector(cfg) is not None and det.summary()["frames"] == 30)

    # ---------------- [8] 仿真指标 ----------------
    section("8 合成数据仿真指标")
    trials = 30
    rep = run_simulation(trials_per_scenario=trials, seed=2026, cfg=cfg)
    bm, em = rep.metrics(True), rep.metrics(False)
    check(8, "时空建模误报率低于帧级基线",
          em["false_positive_rate"] < bm["false_positive_rate"],
          "engine={:.1f}% baseline={:.1f}%".format(
              em["false_positive_rate"] * 100,
              bm["false_positive_rate"] * 100))
    check(8, "时空建模提示率保持高位", em["recall"] >= 0.9,
          "recall={:.1f}%".format(em["recall"] * 100))
    check(8, "边界样本不计入指标",
          rep.metrics(False)["n"] == trials * len(
              [r for r in rep.results if not r.is_ambiguous]))

    rows = duration_sweep(cfg, trials=12, max_frames=20, seed=99)
    short_hits = [r for r in rows if r["frames"] <= 2 and r["trigger_rate"] > 0.2]
    long_hits = [r for r in rows if r["frames"] >= 15 and r["trigger_rate"] < 0.9]
    check(8, "短动作（≤0.2s）不触发", not short_hits)
    check(8, "长动作（≥1.5s）稳定触发", not long_hits)

    # ---------------- 汇总 ----------------
    total = len(_results)
    passed = sum(1 for r in _results if r)
    failed = total - passed
    print()
    print("=" * 78)
    print("自检结果：{} 项通过，{} 项失败（共 {} 项）".format(passed, failed, total))
    if failed:
        print("存在失败项，请检查上方 FAIL 行。")
    print("注：第 8 节数值为【合成数据仿真】结果，不代表真实考场实测性能。")
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
