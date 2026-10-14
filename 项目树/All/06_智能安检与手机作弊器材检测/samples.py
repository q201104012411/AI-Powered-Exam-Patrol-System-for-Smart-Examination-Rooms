# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 样本库与验收测试

**检出率/误报率怎么来的，这里说清楚**（验收最容易含糊的地方）：

  检出率 = 应报警且报了 / 应报警的样本数       ← 必须有 ground truth（样本）
  误报率 = 无害样本却报警 / 无害样本数         ← 必须有"确定无害"的样本

所以本模块维护一个样本库（内置 20 条，可增删、可导入导出）：
  · 作弊样本：手机（开机/关机/鞋内）、无线耳机（入耳/充电仓/挂脖）、
              金属接收器、强磁器材、智能手表
  · 无害样本：腰带扣、钥匙串、金属镜架、硬币拉链、纯布衣考生
  · 特殊样本：心脏起搏器（**设备会响，但不是作弊**，须转人工安检通道）

两种测试模式，**分开记账、不混用**：
  replay  判据回放：把样本的感应特征喂给判据引擎，验的是**算法与阈值**
  field   现场实测：真设备上跑真实样本，人工录入"报没报"，验的是**整机**
  报告里会写清哪一份是哪一种；回放结果**不能**充作整机实测证据。

另外：模拟器（gate.SimGate）用的感应特征就从样本库取，保证演示与测试口径一致。
"""
import time

import config
import gate
import models

# 内置样本：(编号, 名称, 类别, 期望报警, 六路感应强度, 说明)
BUILTIN = [
    # ---------------- 作弊样本：手机 ----------------
    ("SP-B01", "手机-开机放口袋", config.KIND_MOBILE, True,
     {"metal": 0.48, "magnetic": 0.12, "phone": 0.86, "earphone": 0.05,
      "millimeter": 0.30, "shoe": 0.02},
     "最主流情形：手机特征明显，单路即可报警（橙色）"),
    ("SP-B02", "手机-关机藏内袋", config.KIND_MOBILE, True,
     {"metal": 0.55, "magnetic": 0.18, "phone": 0.72, "earphone": 0.02,
      "millimeter": 0.66, "shoe": 0.02},
     "关机仍保留电池/金属特征，手机+毫米波双判据 -> 红色"),
    ("SP-B03", "手机-藏鞋内", config.KIND_SHOE, True,
     {"metal": 0.30, "magnetic": 0.10, "phone": 0.34, "earphone": 0.02,
      "millimeter": 0.28, "shoe": 0.71},
     "靠鞋底检测仪检出；不配鞋底仪就会漏 —— 报告里会指出这个前提"),
    ("SP-B04", "手机-绑小腿(藏匿)", config.KIND_MOBILE, True,
     {"metal": 0.42, "magnetic": 0.14, "phone": 0.63, "earphone": 0.03,
      "millimeter": 0.74, "shoe": 0.10},
     "贴身藏匿：毫米波+手机特征双判据"),
    # ---------------- 作弊样本：无线耳机 ----------------
    ("SP-B05", "无线耳机-入耳式(开机)", config.KIND_EARPHONE, True,
     {"metal": 0.10, "magnetic": 0.05, "phone": 0.08, "earphone": 0.68,
      "millimeter": 0.12, "shoe": 0.01},
     "信号弱，靠耳机特征单路报警（橙色）-> 复检确认，压误报"),
    ("SP-B06", "无线耳机-带充电仓", config.KIND_EARPHONE, True,
     {"metal": 0.66, "magnetic": 0.22, "phone": 0.10, "earphone": 0.61,
      "millimeter": 0.15, "shoe": 0.01},
     "充电仓有电池和金属，耳机+金属双判据 -> 红色"),
    ("SP-B07", "无线耳机-挂脖(关机)", config.KIND_EARPHONE, True,
     {"metal": 0.22, "magnetic": 0.08, "phone": 0.06, "earphone": 0.55,
      "millimeter": 0.10, "shoe": 0.01},
     "刚过阈值：考验阈值调校，调钝就会漏"),
    # ---------------- 作弊样本：金属/强磁/智能设备 ----------------
    ("SP-B08", "金属作弊接收器(线圈+电池)", config.KIND_METAL, True,
     {"metal": 0.88, "magnetic": 0.34, "phone": 0.20, "earphone": 0.08,
      "millimeter": 0.25, "shoe": 0.03},
     "金属感应强，单路即报警"),
    ("SP-B09", "强磁作弊器材(磁吸)", config.KIND_MAGNETIC, True,
     {"metal": 0.70, "magnetic": 0.91, "phone": 0.12, "earphone": 0.05,
      "millimeter": 0.20, "shoe": 0.02},
     "强磁+金属双判据 -> 红色"),
    ("SP-B10", "智能手表(联网款)", config.KIND_METAL, True,
     {"metal": 0.64, "magnetic": 0.12, "phone": 0.58, "earphone": 0.14,
      "millimeter": 0.28, "shoe": 0.02},
     "考场同样禁入；手机特征+金属双判据"),
    ("SP-B11", "无线作弊模块(2.4G)", config.KIND_EARPHONE, True,
     {"metal": 0.40, "magnetic": 0.16, "phone": 0.24, "earphone": 0.79,
      "millimeter": 0.22, "shoe": 0.02},
     "2.4G 辐射特征明显"),
    # ---------------- 无害样本（用来算误报率） ----------------
    ("SP-G01", "腰带金属扣", "harmless", False,
     {"metal": 0.58, "magnetic": 0.30, "phone": 0.04, "earphone": 0.02,
      "millimeter": 0.08, "shoe": 0.01},
     "最常见误报来源：金属扣 0.58 低于阈值 0.62，应放行"),
    ("SP-G02", "钥匙串", "harmless", False,
     {"metal": 0.55, "magnetic": 0.10, "phone": 0.03, "earphone": 0.01,
      "millimeter": 0.06, "shoe": 0.01},
     "钥匙串应为放行；若阈值调到 0.55 以下就会误报"),
    ("SP-G03", "金属镜架眼镜", "harmless", False,
     {"metal": 0.30, "magnetic": 0.04, "phone": 0.02, "earphone": 0.01,
      "millimeter": 0.05, "shoe": 0.00},
     "含量低，不应报警"),
    ("SP-G04", "硬币+拉链", "harmless", False,
     {"metal": 0.52, "magnetic": 0.06, "phone": 0.02, "earphone": 0.01,
      "millimeter": 0.07, "shoe": 0.01},
     "小金属件，不应报警"),
    ("SP-G05", "纯布衣考生(无金属)", "harmless", False,
     {"metal": 0.06, "magnetic": 0.02, "phone": 0.01, "earphone": 0.00,
      "millimeter": 0.02, "shoe": 0.00},
     "空手通过，必须一路放行"),
    ("SP-G06", "皮带扣+工牌夹", "harmless", False,
     {"metal": 0.60, "magnetic": 0.18, "phone": 0.03, "earphone": 0.02,
      "millimeter": 0.06, "shoe": 0.01},
     "临界样本：0.60 逼近阈值，用来暴露「阈值过严」的误报风险"),
    ("SP-G07", "孕妇(主动申报，无金属)", "harmless", False,
     {"metal": 0.08, "magnetic": 0.02, "phone": 0.01, "earphone": 0.00,
      "millimeter": 0.03, "shoe": 0.00},
     "按政策主动走人工安检通道，不进门（本样本验「不误报」）"),
    # ---------------- 特殊样本：植入医疗器械 ----------------
    ("SP-S01", "心脏起搏器(植入医疗器械)", config.KIND_IMPLANT, True,
     {"metal": 0.79, "magnetic": 0.66, "phone": 0.06, "earphone": 0.03,
      "millimeter": 0.18, "shoe": 0.01},
     "**设备必须响**（金属感应），但这不是作弊：须转人工安检通道、"
     "双人见证，并提示考生不要过安检门（植入器械安全风险）"),
    ("SP-S02", "金属骨钉/人工关节", config.KIND_IMPLANT, True,
     {"metal": 0.72, "magnetic": 0.22, "phone": 0.05, "earphone": 0.02,
      "millimeter": 0.14, "shoe": 0.02},
     "植入物报警 -> 人工通道；同时提示考生提前申报"),
]

CHEAT_KINDS = (config.KIND_MOBILE, config.KIND_EARPHONE, config.KIND_METAL,
               config.KIND_MAGNETIC, config.KIND_SHOE)


def builtin_samples():
    """内置样本（Sample 对象列表）。"""
    out = []
    for sid, name, kind, expect, scores, note in BUILTIN:
        out.append(models.Sample(sample_id=sid, name=name, kind=kind,
                                 expect_alarm=bool(expect), scores=dict(scores),
                                 note=note, builtin=True))
    return out


def ensure_builtin(store):
    """把内置样本补齐（已存在的不动，用户改过的也不覆盖）。返回新建数量。"""
    have = {s.sample_id for s in store.list_samples()}
    n = 0
    for s in builtin_samples():
        if s.sample_id in have:
            continue
        store.save_sample(s)
        n += 1
    return n


def sim_profiles():
    """模拟器用的感应特征：作弊/特殊样本 -> 报警画像；无害样本 -> 正常画像。"""
    out = []
    for s in builtin_samples():
        out.append({"name": s.name,
                    "kind": "" if s.kind == "harmless" else s.kind,
                    "scores": dict(s.scores)})
    return out


def verdict_of(expect_alarm, got_alarm):
    if expect_alarm and got_alarm:
        return "hit"
    if expect_alarm and not got_alarm:
        return "miss"
    if (not expect_alarm) and got_alarm:
        return "false_alarm"
    return "correct_clear"


def _kind_bucket(kind):
    if kind in CHEAT_KINDS:
        return "cheat"
    if kind == config.KIND_IMPLANT:
        return "implant"
    return "harmless"


def summarize_items(items):
    """
    逐样本结论 -> 指标。**手机检出率、耳机检出率、误报率分开算**，
    整体作弊检出率另给一个，避免用一个大数糊过去。
    """
    def blank():
        return {"total": 0, "hit": 0, "miss": 0, "false_alarm": 0, "correct_clear": 0}

    by_kind = {}
    buckets = {"cheat": blank(), "harmless": blank(), "implant": blank()}
    for it in items:
        k = it.kind or "harmless"
        b = by_kind.setdefault(k, blank())
        b["total"] += 1
        b[it.verdict if it.verdict in b else "correct_clear"] += 1
        bk = _kind_bucket(k)
        buckets[bk]["total"] += 1
        buckets[bk][it.verdict if it.verdict in buckets[bk] else "correct_clear"] += 1

    def rate(b, key="hit"):
        base = b["hit"] + b["miss"] if key == "hit" else (b["total"] or 1)
        return (b[key] / float(base)) if base else 0.0

    phone = by_kind.get(config.KIND_MOBILE, blank())
    earphone = by_kind.get(config.KIND_EARPHONE, blank())
    cheat = buckets["cheat"]
    harmless = buckets["harmless"]
    implant = buckets["implant"]
    false_rate = (harmless["false_alarm"] / float(harmless["total"])) if harmless["total"] else 0.0
    metrics = {
        "phone_total": phone["total"], "phone_rate": rate(phone, "hit"),
        "earphone_total": earphone["total"], "earphone_rate": rate(earphone, "hit"),
        "cheat_total": cheat["total"], "cheat_rate": rate(cheat, "hit"),
        "harmless_total": harmless["total"], "false_rate": false_rate,
        "implant_total": implant["total"],
        "implant_alarmed": implant["hit"],
        "implant_channel_rate": rate(implant, "hit"),
        "by_kind": by_kind,
        "targets": {"phone": config.TARGET_PHONE_RATE,
                    "earphone": config.TARGET_EARPHONE_RATE,
                    "false_rate": config.TARGET_FALSE_RATE},
    }
    metrics["pass"] = {
        "phone": (metrics["phone_rate"] >= config.TARGET_PHONE_RATE
                  if phone["total"] else None),
        "earphone": (metrics["earphone_rate"] >= config.TARGET_EARPHONE_RATE
                     if earphone["total"] else None),
        "false_rate": (false_rate <= config.TARGET_FALSE_RATE
                       if harmless["total"] else None),
    }
    return metrics


def run_replay(store, operator="", site="", samples_list=None, note="判据回放"):
    """
    判据回放：把样本感应特征喂给判据引擎，验算法与阈值。
    返回 (TestRun, items)
    """
    samples_list = samples_list or store.list_samples() or builtin_samples()
    run = models.TestRun(mode="replay", site=site or config.load_settings().get("site", ""),
                         operator=operator, note=note)
    store.save_test_run(run)
    items = []
    for s in samples_list:
        t0 = time.perf_counter()
        res = gate.decide(s.scores)
        cost_ms = int((time.perf_counter() - t0) * 1000)
        got = res["result"] == "alarm"
        it = models.TestItem(run_id=run.run_id, sample_id=s.sample_id,
                            sample_name=s.name, kind=s.kind,
                            expect_alarm=bool(s.expect_alarm), got_alarm=got,
                            got_kinds=res["kinds"], level=res["level"],
                            verdict=verdict_of(bool(s.expect_alarm), got),
                            duration_ms=max(1, cost_ms), note=s.note)
        store.save_test_item(it)
        items.append(it)
    return finish_run(store, run, items)


def create_field_run(store, operator="", site="", note="现场实测"):
    """开一次现场实测（真设备 + 真实样本）。"""
    run = models.TestRun(mode="field", site=site or config.load_settings().get("site", ""),
                         operator=operator, note=note)
    store.save_test_run(run)
    store.log("开始现场实测", "%s 操作人=%s" % (run.run_id, operator or "-"), operator or "system")
    return run


def record_field_item(store, run_id, sample_id=None, sample_name="", kind=config.KIND_MOBILE,
                      expect_alarm=True, got_alarm=False, got_kinds=None, note=""):
    """录入一条现场实测结果（人看着真设备，报没报就是报没报）。"""
    s = store.get_sample(sample_id) if sample_id else None
    if s is not None:
        sample_name = sample_name or s.name
        kind = s.kind or kind
    it = models.TestItem(run_id=run_id, sample_id=sample_id or "",
                         sample_name=sample_name or "现场样本", kind=kind,
                         expect_alarm=bool(expect_alarm), got_alarm=bool(got_alarm),
                         got_kinds=list(got_kinds or []),
                         level=(models.LEVEL_RED if got_alarm else ""),
                         verdict=verdict_of(bool(expect_alarm), bool(got_alarm)),
                         duration_ms=0, note=note)
    store.save_test_item(it)
    return it


def finish_run(store, run, items=None):
    """汇总一次测试运行（逐样本结论 -> 指标），保存并返回 (run, items)。"""
    items = items if items is not None else store.list_test_items(run.run_id)
    run.total = len(items)
    run.hit = sum(1 for i in items if i.verdict == "hit")
    run.miss = sum(1 for i in items if i.verdict == "miss")
    run.false_alarm = sum(1 for i in items if i.verdict == "false_alarm")
    run.correct_clear = sum(1 for i in items if i.verdict == "correct_clear")
    run.finished_at = models.now_ts()
    run.metrics = summarize_items(items)
    store.save_test_run(run)
    store.log("测试完成", "%s（%s）%d 样本，检出 %d / 漏检 %d / 误报 %d"
              % (run.run_id, "判据回放" if run.mode == "replay" else "现场实测",
                 run.total, run.hit, run.miss, run.false_alarm), run.operator or "system")
    return run, items


def latest_metrics(store, mode=None):
    run = store.latest_test_run(mode=mode)
    if not run:
        return {}
    m = dict(run.metrics or {})
    m["run_id"] = run.run_id
    m["mode"] = run.mode
    m["finished_at"] = run.finished_at
    return m


def compare_modes(store, operator=""):
    """
    阈值模式对比（**不落库**，只算给你看）：
      turbo(放宽) / standard(标准) / strict(从严)
    同一批样本在三种模式下跑判据，直接看出"漏检与误报的取舍"，
    这是阈值调校依据，也是验收时说明"为什么定这个阈值"的证据。
    """
    out = {}
    for mode in ("turbo", "standard", "strict"):
        items = []
        for s in (store.list_samples() or builtin_samples()):
            res = gate.decide(s.scores, gate_mode=mode)
            got = res["result"] == "alarm"
            items.append(models.TestItem(
                sample_id=s.sample_id, sample_name=s.name, kind=s.kind,
                expect_alarm=bool(s.expect_alarm), got_alarm=got,
                got_kinds=res["kinds"], level=res["level"],
                verdict=verdict_of(bool(s.expect_alarm), got)))
        out[mode] = summarize_items(items)
    return out


def export_samples(store, path=None):
    import storage
    path = path or (config.SAMPLE_DIR / "样本清单.json")
    return storage.export_json(store.list_samples(), path)


def import_samples(store, path):
    """从 JSON 导入样本（按 sample_id 覆盖）。返回 (ok, 说明)

    注意：已存在的样本保留原来的 builtin 标记——内置样本是"出厂口径"，
    导入不该把它变成用户样本（否则现场实测按 builtin 筛选样本会筛不到）。
    """
    data = models.jload(open(str(path), encoding="utf-8").read(), [])
    if not isinstance(data, list):
        return False, "文件不是样本数组"
    n = 0
    for row in data:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        sid = row.get("sample_id") or models.new_id("SP")
        old = store.get_sample(sid)
        s = models.Sample(sample_id=sid, name=row["name"],
                          kind=row.get("kind") or "harmless",
                          expect_alarm=bool(row.get("expect_alarm")),
                          scores=gate.normalize_scores(row.get("scores") or {}),
                          note=row.get("note") or "",
                          builtin=(old.builtin if old else False))
        store.save_sample(s)
        n += 1
    store.log("导入样本", "%d 条（%s）" % (n, path), "system")
    return True, "导入 %d 条样本" % n
