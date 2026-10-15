# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 算法公平性分层测试（第二节第 5 条）

为什么要做：人脸核验这类算法在"戴眼镜/戴口罩/面部有损伤/不同年龄段"上的通过率天然有差异。
如果差异大而没人知道，特殊考生就会在闸机前反复被拒——**这就是技术造成的不公平**。

本模块把"分层测试"变成可执行、可留痕的东西：
  1) 按分层维度（性别/年龄段/眼镜/口罩/遮挡/肤色）录入各分组的样本数与通过数；
  2) 算出组间通过率最大差 max_gap，与可接受范围（默认 5%）比；
  3) 超差必须记录补偿策略（调阈值/补样本重训/该分层优先人工核验），
     并在下一次测试里看差异是否收敛（报告里给"整改前后"）。
"""
import config
import models


def _pct(x):
    return round(float(x) * 100, 2)


def run_fairness(store, payload, operator=""):
    """
    记录一次分层测试并给出结论。返回 (ok, FairnessRun|reason, info)。
    groups 形如 {"gender": {"男": {"total": 200, "passed": 190}, ...}, ...}
    """
    payload = dict(payload or {})
    groups_in = payload.get("groups") or {}
    if not isinstance(groups_in, dict) or not groups_in:
        return False, "缺少分层数据（groups 为空）", {}
    cfg = config.load_settings()
    max_gap_allowed = config.fairness_max_gap(cfg)
    min_samples = config.min_group_samples(cfg)

    dims, groups, total_all = [], [], 0
    small_groups, problems = [], []
    for dim, items in groups_in.items():
        if dim not in config.FAIRNESS_DIMS:
            problems.append("未知分层维度被忽略：%s" % dim)
            continue
        if not isinstance(items, dict) or not items:
            problems.append("分层 %s 没有分组数据" % dim)
            continue
        dims.append(dim)
        rates = []
        for gname, g in (items or {}).items():
            g = g or {}
            try:
                total = int(g.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if total <= 0:
                problems.append("%s/%s 样本数为 0，已跳过" % (dim, gname))
                continue
            if g.get("passed") is not None:
                try:
                    passed = int(g.get("passed") or 0)
                except (TypeError, ValueError):
                    passed = 0
            else:
                try:
                    passed = total - int(g.get("failed") or 0)
                except (TypeError, ValueError):
                    passed = total
            passed = max(0, min(total, passed))
            gr = models.FairnessGroup(run_id="", dim=dim, group_name=str(gname),
                                      total=total, passed=passed,
                                      failed=total - passed,
                                      pass_rate=round(passed / float(total), 4),
                                      small_sample=total < min_samples,
                                      note=str(g.get("note") or ""))
            if gr.small_sample:
                small_groups.append("%s/%s(%d)" % (dim, gname, total))
            groups.append(gr)
            rates.append((gr.pass_rate, str(gname)))
            total_all += total
        if rates:
            best = max(r[0] for r in rates)
            worst = min(rates, key=lambda x: x[0])
            for gr in groups:
                if gr.dim == dim:
                    gr.gap_to_best = round(best - gr.pass_rate, 4)
            _x = worst

    if not groups:
        return False, "分层数据全部无效（样本数为 0 或维度非法）", {}

    # 逐维度算差异，取最差的那个维度作为结论
    worst_dim, worst_group, worst_gap = "", "", 0.0
    dim_rate = {}
    for dim in dims:
        rs = [g for g in groups if g.dim == dim]
        if len(rs) < 2:
            dim_rate[dim] = (rs[0].pass_rate, rs[0].pass_rate, 0.0)
            continue
        hi = max(r.pass_rate for r in rs)
        lo = min(r.pass_rate for r in rs)
        dim_rate[dim] = (lo, hi, round(hi - lo, 4))
        if (hi - lo) > worst_gap:
            worst_gap = round(hi - lo, 4)
            worst_dim = dim
            worst_group = min(rs, key=lambda r: r.pass_rate).group_name

    overall_lo = min(g.pass_rate for g in groups)
    overall_hi = max(g.pass_rate for g in groups)
    passed = worst_gap <= max_gap_allowed + 1e-9

    action = str(payload.get("action") or ("none" if passed else "threshold"))
    if action not in config.FAIRNESS_ADJUST_ACTIONS:
        action = "none" if passed else "threshold"
    action_note = str(payload.get("action_note") or "")
    warn = list(problems)
    if not passed and action == "none":
        warn.append("组间差异超差却选「无需调整」：不合规，请改选补偿策略并写明说明")
    if not passed and not action_note:
        warn.append("差异超差必须写明补偿/整改说明（阈值调整值、重训计划或人工优先策略）")
    if small_groups:
        warn.append("以下分组样本不足 %d，结论仅作参考：%s"
                    % (min_samples, "、".join(small_groups)))
    missing_req = [d for d in config.FAIRNESS_REQUIRED_DIMS if d not in dims]
    if missing_req:
        warn.append("未覆盖必测分层：%s" % "、".join(
            config.DIM_TEXT.get(d, d) for d in missing_req))

    run = models.FairnessRun(
        name=str(payload.get("name") or "分层测试"),
        site=str(payload.get("site") or cfg.get("site") or ""),
        algorithm=str(payload.get("algorithm") or ""),
        threshold=float(payload.get("threshold") or 0.0),
        total=total_all, dims=dims, groups_count=len(groups),
        max_gap=worst_gap, min_rate=round(overall_lo, 4),
        max_rate=round(overall_hi, 4), worst_dim=worst_dim, worst_group=worst_group,
        passed=passed, action=action, action_note=action_note,
        small_groups=small_groups, operator=operator,
        source=str(payload.get("source") or config.MANUAL_SOURCE),
        note=str(payload.get("note") or ""))
    store.save_fairness_run(run)
    for g in groups:
        g.run_id = run.run_id
        store.save_fairness_group(g)

    advice = _advice(worst_dim, worst_group, worst_gap, max_gap_allowed, passed)
    store.log("算法公平性分层测试",
              "样本 %d，组间最大差 %.2f%%（%s/%s），结论=%s"
              % (total_all, _pct(worst_gap),
                 config.DIM_TEXT.get(worst_dim, worst_dim), worst_group,
                 "达标" if passed else "超差需补偿"), operator)
    return True, run, {"advice": advice, "warn": warn, "dim_rate": dim_rate,
                       "max_gap_allowed": max_gap_allowed,
                       "missing_required_dims": missing_req}


def _advice(dim, group, gap, allowed, passed):
    if passed:
        return ("组间通过率最大差 %.2f%% ≤ 可接受范围 %.0f%%，无需补偿；"
                "仍建议保持每考期复测。" % (_pct(gap), allowed * 100))
    return ("%s 的「%s」组通过率明显偏低（组间差 %.2f%% > %.0f%%）。建议按顺序采取："
            "① 调该分层阈值或加补偿策略并复测；② 该分层考生优先走人工核验通道；"
            "③ 补充该分层样本重训模型。整改后必须复测并留痕。"
            % (config.DIM_TEXT.get(dim, dim or "-"), group or "-", _pct(gap),
               allowed * 100))


def coverage(store):
    """必测分层的覆盖情况（上线前要覆盖主要人群分层）。"""
    latest = store.latest_fairness_run()
    dims = list(latest.dims) if latest else []
    missing = [d for d in config.FAIRNESS_REQUIRED_DIMS if d not in dims]
    return {"latest_run": (latest.run_id if latest else ""),
            "latest_ts": (latest.ts if latest else ""),
            "dims": dims, "required": list(config.FAIRNESS_REQUIRED_DIMS),
            "missing": missing, "covered": len(dims) > 0 and not missing,
            "missing_text": [config.DIM_TEXT.get(d, d) for d in missing]}


def trend(store, limit=5):
    """整改前后对比（报告用）：最近几次测试的组间最大差。"""
    runs = store.list_fairness_runs(limit=limit)
    return [{"run_id": r.run_id, "ts": r.ts, "max_gap": r.max_gap,
             "passed": r.passed, "action": r.action,
             "action_text": config.FAIRNESS_ADJUST_TEXT.get(r.action, r.action),
             "worst_dim": r.worst_dim, "worst_group": r.worst_group}
            for r in reversed(runs)]


def fairness_stats(store):
    """指标口径：算法分层测试是否覆盖主要分层、差异是否在可接受范围。"""
    latest = store.latest_fairness_run()
    cov = coverage(store)
    out = {"runs": len(store.list_fairness_runs(limit=1000)),
           "covered": cov["covered"], "missing": cov["missing"],
           "latest": None}
    if latest is not None:
        out["latest"] = {"run_id": latest.run_id, "ts": latest.ts,
                         "total": latest.total, "max_gap": latest.max_gap,
                         "passed": latest.passed, "action": latest.action,
                         "action_note": latest.action_note,
                         "worst_dim": latest.worst_dim,
                         "worst_group": latest.worst_group}
    return out
