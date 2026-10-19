# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 模拟数据、回放测试与演示数据

**如实说明**：本机没有频谱仪，回放测试用的是内置模拟器生成的频谱帧，所有帧都标
`source=sim`；指标只代表"算法在本模拟场景下的表现"，不代表真实设备检出率。
接上真设备后，同样的 `spectrum.ingest_scan` 直接吃真帧，指标才转为真实值。

回放场景覆盖专题文档要求的能力：
  ① 2.4G 数传作弊（突发）        ② UHF 隐形耳机（窄带持续）
  ③ 5.8G 无线图传               ④ 合法 WiFi AP（**不应误报**）
  ⑤ 纯噪声（**不应误报**）       ⑥ 屏蔽开启后的自身信号（**必须剔除**）
  ⑦ 非法频段发射                ⑧ 三站同频（验证定位精度）

每个场景按"信号从某时刻出现 -> 扫描帧 -> 发现 -> 定位"跑一遍，统计：
检出率、误报率、平均/最大发现时延、定位误差。指标不达标时**如实标红**，不粉饰。
"""
import datetime
import hashlib
import math
import random

import config
import features as features_mod
import locate
import models
import schedule
import spectrum
from models import (Approval, Device, JammerState, ReplayItem, ReplayRun,
                    ShieldTest, SpillTest)

REPLAY_LATENCY_SIM_SEC = 1.0        # 模拟设备上报延迟（真设备由帧时间戳决定）
SCENARIO_GAP_SEC = 7200             # 场景之间隔 2 小时，避免异常合并相互干扰

#: 场景定义（详见模块头）
SCENARIOS = (
    {"sid": "S1", "name": "2.4G 数传作弊模块", "band": "ism24",
     "duration": 12.0, "interval": 2.0, "stations": ["MON-A"],
     "signal": {"freq": 2442.0, "bw": 2.0, "power": -62.0, "duty": "burst"},
     "expect_alert": True, "expect_kind": "cheat_burst",
     "desc": "答案数传常用频段，短突发；应在首个扫描周期内告警"},
    {"sid": "S2", "name": "UHF 隐形耳机", "band": "uhf", "step": 0.01,
     "duration": 12.0, "interval": 2.0, "stations": ["MON-A"],
     "signal": {"freq": 433.5, "bw": 0.025, "power": -78.0, "duty": "continuous"},
     "expect_alert": True, "expect_kind": "cheat_narrow",
     "desc": "窄带模拟连续发射，靠特征库识别为隐形耳机"},
    {"sid": "S3", "name": "5.8G 无线图传", "band": "unii3",
     "duration": 12.0, "interval": 2.0, "stations": ["MON-B"],
     "signal": {"freq": 5800.0, "bw": 8.0, "power": -70.0, "duty": "burst"},
     "expect_alert": True, "expect_kind": "cheat_burst",
     "desc": "传答案图像/大流量，宽带突发"},
    {"sid": "S4", "name": "合法 WiFi AP（不应误报）", "band": "ism24",
     "duration": 10.0, "interval": 2.0, "stations": ["MON-C"],
     "signal": {"freq": 2437.0, "bw": 20.0, "power": -50.0, "duty": "continuous"},
     "expect_alert": False,
     "desc": "命中合法 WiFi 特征，只记台账不告警"},
    {"sid": "S5", "name": "纯背景噪声（不应误报）", "band": "ism24",
     "duration": 10.0, "interval": 2.0, "stations": ["MON-A"],
     "signal": None, "expect_alert": False,
     "desc": "没有发射源时应零告警"},
    {"sid": "S6", "name": "屏蔽自身信号（必须剔除）", "band": "ism24",
     "duration": 10.0, "interval": 2.0, "stations": ["MON-A"],
     "signal": {"freq": 2440.0, "bw": 30.0, "power": -90.0, "duty": "continuous"},
     "expect_alert": False, "shield_band": "ism24", "expect_self": True,
     "desc": "屏蔽开启后带内噪声抬高，不能把自己的压制信号当作弊"},
    {"sid": "S7", "name": "非法频段发射", "band": "", "range": [500.0, 540.0],
     "step": 0.02,
     "duration": 10.0, "interval": 2.0, "stations": ["MON-C"],
     "signal": {"freq": 520.0, "bw": 0.05, "power": -80.0, "duty": "continuous"},
     "expect_alert": True, "expect_kind": "illegal_band",
     "desc": "不在任何已知合法频段，判非法发射"},
    {"sid": "S8", "name": "三站同频（定位验证）", "band": "ism24",
     "duration": 10.0, "interval": 2.0, "stations": ["MON-A", "MON-B", "MON-C"],
     "signal": {"freq": 2465.0, "bw": 2.0, "power": -80.0, "duty": "burst"},
     "powers": {"MON-A": -90.97, "MON-B": -90.97, "MON-C": -84.31},
     "expect_alert": True, "expect_kind": "cheat_burst",
     "expect_location": {"x": 40.0, "y": 30.0, "tol": 15.0},
     "desc": "三站场强解算，验证定位误差"},
)


# =============================================================================
# 频谱帧合成
# =============================================================================
def _rng(seed_text):
    h = hashlib.sha1(str(seed_text).encode("utf-8")).hexdigest()
    return random.Random(int(h[:12], 16))


def make_bins(band, freq_range=None, step=None, floor=-100.0, jitter=1.2, rng=None):
    """生成一组背景噪声 bin。"""
    rng = rng or _rng("noise")
    low, high = (freq_range if freq_range else config.band_range(band))
    if not low or not high:
        low, high = (2400.0, 2483.5)
    step = float(step or max(0.05, (high - low) / 400.0))
    bins = []
    f = low
    while f <= high + 1e-9:
        bins.append([round(f, 4), round(float(floor) + rng.gauss(0.0, jitter), 2)])
        f += step
    return bins, step


def inject_peak(bins, freq, power, bw):
    """
    往 bin 里注入一个"抛物线形"发射峰（dB 域）：中心 power，边缘 -12dB。
    真实频谱仪的 RBW 平滑效果类似；这样峰的中心/带宽/信噪比都可预期。
    窄带信号可能落在两个 bin 之间：此时把**最近的 bin**抬到目标功率，
    避免"信号比 RBW 还窄就测不到"的假象（真实频谱仪扫频会扫到）。
    """
    if not bins:
        return bins
    half = max(0.01, float(bw) / 2.0)
    nearest = None
    nearest_off = None
    for item in bins:
        f = float(item[0])
        off = abs(f - float(freq))
        if nearest_off is None or off < nearest_off:
            nearest, nearest_off = item, off
        if off > half:
            continue
        target = float(power) - 12.0 * (off / half) ** 2
        if target > float(item[1]):
            item[1] = round(target, 2)
    if nearest is not None and (nearest_off or 0) > half:
        if float(power) > float(nearest[1]):
            nearest[1] = round(float(power), 2)
    return bins


def demo_frame(station="MON-A", freq=2442.0, bw=2.0, power=-62.0, band="ism24",
               ts=None, source="sim", duty="burst", with_noise=True,
               freq_range=None):
    """造一帧可直接喂给 spectrum.ingest_scan 的模拟频谱（GUI「模拟扫描」用）。"""
    rng = _rng("frame-%s-%s" % (station, int(float(ts or models.now_epoch()))))
    code = band if band is not None else config.band_of(freq)
    bins, step = make_bins(code, freq_range=freq_range, rng=rng)
    if with_noise and power is not None:
        inject_peak(bins, freq, power, bw)
    return {"station_code": station, "band": code or "",
            "ts_epoch": float(ts or models.now_epoch()), "source": source,
            "bins": bins, "noise_floor_dbm": -100.0}


def build_scenario_frames(scn, base_epoch, cfg=None):
    """按场景生成扫描帧列表（按时间升序，每帧含 station/bins/peaks）。"""
    cfg = cfg or config.load_settings()
    frames = []
    duration = float(scn.get("duration") or 10.0)
    interval = float(scn.get("interval") or 2.0)
    steps = max(1, int(round(duration / interval)) + 1)
    signal = scn.get("signal")
    powers = scn.get("powers") or {}
    duty = (signal or {}).get("duty") or "burst"
    for k in range(steps):
        ts = float(base_epoch) + k * interval
        # 真实数传是"短时突发"：按 2/3 帧出现模拟占空比，避免连续发射被误判成 WiFi
        present = True if duty != "burst" else (k % 3 != 2)
        for station in scn.get("stations") or ["MON-A"]:
            power = powers.get(station, (signal or {}).get("power"))
            band = scn.get("band") or ""
            rng = _rng("%s|%s|%d" % (scn["sid"], station, k))
            bins, step = make_bins(band, freq_range=scn.get("range"),
                                   step=scn.get("step"), rng=rng)
            if signal is not None and power is not None and present:
                inject_peak(bins, signal["freq"], power, signal["bw"])
            frames.append({
                "station_code": station, "band": band, "ts_epoch": ts,
                "source": "sim", "bins": bins, "noise_floor_dbm": -100.0,
                "note": scn["sid"],
            })
    frames.sort(key=lambda f: (f["ts_epoch"], f["station_code"]))
    return frames


# =============================================================================
# 内置设备/演示数据
# =============================================================================
def _ensure_device(store, code, name, kind, x=0.0, y=0.0, site="", room_id="",
                   direction=-1.0, sector_width=120.0, bands=None, power_w=1.0,
                   max_power_w=5.0, status="online", proto="sim", note=""):
    dev = store.get_device(code=code)
    if dev is None:
        dev = Device(code=code, name=name, kind=kind, site=site, room_id=room_id,
                     status=status, proto=proto, note=note)
    dev.name = name or dev.name
    dev.kind = kind
    dev.x, dev.y = float(x), float(y)
    dev.located = 1
    dev.site = site or dev.site
    dev.room_id = room_id or dev.room_id
    dev.direction = float(direction)
    dev.sector_width = float(sector_width)
    dev.bands = list(bands or [])
    dev.power_w = float(power_w)
    dev.max_power_w = float(max_power_w)
    dev.status = status
    dev.proto = proto
    dev.note = note or dev.note
    store.save_device(dev)
    return dev


def ensure_stations(store, site="", cfg=None):
    """回放/演示用的三站 + 手持侦测仪 + 两台屏蔽器（已存在则补坐标，不重复建）。"""
    cfg = cfg or config.load_settings()
    site = site or cfg.get("site") or "第一考点"
    plan = config.shield_plan(cfg)
    devs = [
        _ensure_device(store, "MON-A", "监测站 A（大门）", config.DEV_MONITOR,
                       0.0, 0.0, site=site, note="考点平面坐标原点"),
        _ensure_device(store, "MON-B", "监测站 B（东侧）", config.DEV_MONITOR,
                       80.0, 0.0, site=site, note="与 A 站间距 80 米"),
        _ensure_device(store, "MON-C", "监测站 C（北侧）", config.DEV_MONITOR,
                       40.0, 60.0, site=site, note="与 A/B 构成三角"),
        _ensure_device(store, "PROBE-1", "手持侦测仪 1", config.DEV_PROBE,
                       55.0, 35.0, site=site, note="现场逐步逼近用"),
        _ensure_device(store, "JAM-A", "屏蔽器 A（RM-101）", config.DEV_JAMMER,
                       20.0, 20.0, site=site, room_id="RM-101", direction=45.0,
                       sector_width=60.0, bands=plan, power_w=2.0),
        _ensure_device(store, "JAM-B", "屏蔽器 B（RM-102）", config.DEV_JAMMER,
                       60.0, 20.0, site=site, room_id="RM-102", direction=300.0,
                       sector_width=60.0, bands=plan, power_w=1.5),
    ]
    return devs


def _date_offset(days):
    d = datetime.date.today() + datetime.timedelta(days=int(days))
    return d.strftime(models.DATE_FMT)


def seed_demo(store, cfg=None):
    """一键演示数据：设备 + 审批/协同 + 今日场次 + 屏蔽/外溢实测证据。"""
    cfg = cfg or config.load_settings()
    site = cfg.get("site") or "第一考点"
    ensure_stations(store, site=site, cfg=cfg)
    plan = config.shield_plan(cfg)

    # 审批：只在"当前没有有效审批"时补一条演示审批（重复运行演示也能恢复可用状态）
    valid, _why = store.valid_approval(site, plan)
    if valid is None:
        store.save_approval(Approval(
            kind=config.APPR_APPROVAL, no="WX-2026-1019", org="省无线电管理机构",
            site=site, bands=plan, valid_from=_date_offset(-1),
            valid_to=_date_offset(30), doc="批文 WX-2026-1019（原件存考点保密柜）",
            status=config.APPR_APPROVED,
            note="演示数据：仅用于本地演练，不替代真实报备"))
    coord_ok = [a for a in store.list_approvals(kind=config.APPR_COORDINATION,
                                                site=site)
                if a.valid_today(models.today_str())]
    if not coord_ok:
        store.save_approval(Approval(
            kind=config.APPR_COORDINATION, no="YD-2026-1019", org="中国移动/联通/电信",
            site=site, bands=plan, valid_from=_date_offset(-1),
            valid_to=_date_offset(30), doc="运营商协同确认单 YD-2026-1019",
            status=config.APPR_APPROVED, note="演示数据：考前已告知运营商"))

    # 今日演示场次：从现在往前 10 分钟开始，持续 60 分钟（便于现场演示窗口内开机）
    now = models.now_epoch()
    start_epoch = now - 600
    end_epoch = now + 3000
    schedule.add_session(
        store, "演示场次（自动生成）",
        models.date_str(start_epoch), models.hhmm_text(start_epoch),
        models.hhmm_text(end_epoch), subject="演示科目",
        rooms=["RM-101", "RM-102"], operator="演示",
        note="按当前时间生成，用于演示时间表联动；真实使用请改成正式时间表")
    schedule.add_session(store, "次日 语文", _date_offset(1), "09:00", "11:00",
                         subject="语文", rooms=["RM-101", "RM-102"],
                         operator="演示", note="演示数据")

    # 屏蔽效果实测（按频段）
    existing = store.list_shield_tests(limit=1)
    if not existing:
        for i, band in enumerate(plan):
            store.save_shield_test(ShieldTest(
                room_id="RM-101", band=band, before_dbm=-58.0 + i % 3,
                after_dbm=-88.0 - i % 4, attenuation_db=30.0, rate=0.97,
                result="pass", measured_at=models.ts_text(now - 3600 - i * 60),
                ts_epoch=now - 3600 - i * 60, operator="演示",
                note="演示数据：屏蔽前后场强实测"))
    # 外溢实测
    if not store.list_spills(limit=1):
        jam = store.get_device(code="JAM-A")
        for i, (point, dist) in enumerate((("考点东门", 120.0), ("南侧居民区", 180.0),
                                           ("北侧道路", 150.0))):
            store.save_spill(SpillTest(
                jammer_id=jam.device_id if jam else "", point=point,
                distance_m=dist, band="gsm900", measured_dbm=-93.0 - i,
                limit_dbm=models.to_float(cfg.get("spill_limit_dbm"), -85.0),
                result="pass", measured_at=models.ts_text(now - 1800 - i * 60),
                ts_epoch=now - 1800 - i * 60, operator="演示",
                note="演示数据：外围合法通信可用"))
    store.log("载入演示数据", "设备/审批/场次/实测证据（%s）" % site, "演示")
    return {"site": site, "devices": len(store.list_devices()),
            "fields": len(store.list_sessions(limit=500)),
            "approvals": len(store.list_approvals(limit=100)),
            "shield_tests": len(store.list_shield_tests(limit=500)),
            "spills": len(store.list_spills(limit=500)),
            "features": len(store.list_features())}


# =============================================================================
# 回放测试
# =============================================================================
def _detected_this_run(store, ids, stations):
    """只取本次回放真正产生/合并的异常，避免把上一轮同时刻的旧异常算进来。"""
    out = []
    for aid in (ids or ()):
        a = store.get_anomaly(aid)
        if a is not None and a.station_code in stations:
            out.append(a)
    return out


def run_replay(store, operator="", note="", cfg=None):
    """跑全部场景，产出检出率/误报率/时延/定位误差，落库并返回结果 dict。"""
    cfg = cfg or config.load_settings()
    features_mod.ensure_builtin(store)
    ensure_stations(store, site=cfg.get("site") or "", cfg=cfg)
    now = models.now_epoch()
    n = len(SCENARIOS)
    run = ReplayRun(operator=operator, note=note or "内置回放场景",
                    metrics={"latency_sim_sec": REPLAY_LATENCY_SIM_SEC,
                             "source": "sim"})
    store.save_run(run)

    items = []
    hit = miss = false_alarm = 0
    positives = negatives = 0
    latencies = []
    loc_errors = []
    jam = store.get_device(code="JAM-A")
    for idx, scn in enumerate(SCENARIOS):
        base = now - (n - idx) * SCENARIO_GAP_SEC
        prev_state = None
        if scn.get("shield_band") and jam is not None:
            prev_state = store.get_state(jam.device_id)
            store.save_state(JammerState(
                device_id=jam.device_id, is_on=1, since_epoch=base,
                bands=[scn["shield_band"]], power_w=1.0, operator=operator or "replay",
                reason="回放：模拟屏蔽开启（仅测试）"))
        frames = build_scenario_frames(scn, base, cfg)
        self_count = 0
        run_ids = []
        for fr in frames:
            r = spectrum.ingest_scan(store, fr, source="sim", cfg=cfg,
                                     now=fr["ts_epoch"],
                                     detect_epoch=fr["ts_epoch"] + REPLAY_LATENCY_SIM_SEC)
            if not r.get("ok"):
                self_count += 0
            self_count += int(r.get("self_suppressed") or 0)
            run_ids.extend(r.get("new") or [])
            run_ids.extend(r.get("merged") or [])
        detected = _detected_this_run(store, set(run_ids),
                                      set(scn.get("stations") or []))
        kinds = sorted({a.kind for a in detected})
        expect_alert = bool(scn.get("expect_alert"))
        kind_ok = (not scn.get("expect_kind")) or (scn["expect_kind"] in kinds)
        if expect_alert:
            positives += 1
            ok = bool(detected) and kind_ok
            hit += 1 if ok else 0
            miss += 0 if ok else 1
        else:
            negatives += 1
            ok = not detected
            false_alarm += 0 if ok else 1
        latency = min([float(a.latency_sec or 0) for a in detected], default=0.0)
        if expect_alert and detected:
            latencies.append(latency)
        detail = "检出 %d 条：%s" % (len(detected),
                                     "、".join(config.KIND_TEXT.get(k, k) for k in kinds) or "-")
        if scn.get("expect_self"):
            detail += "；自身信号剔除 %d 次" % self_count
            ok = ok and self_count > 0
        if scn.get("expect_location") and detected:
            loc = locate.locate_anomaly(store, detected[0], operator=operator or "replay")
            ex = scn["expect_location"]
            if loc.get("ok"):
                err = math.hypot(float(loc["x"]) - float(ex["x"]),
                                 float(loc["y"]) - float(ex["y"]))
                loc_errors.append(err)
                detail += "；定位 (%.1f, %.1f) ±%.0fm，误差 %.1fm" % (
                    loc["x"], loc["y"], loc["radius_m"], err)
                ok = ok and err <= float(ex.get("tol") or 15.0)
            else:
                detail += "；定位失败：%s" % loc.get("reason")
                ok = False
        item = ReplayItem(run_id=run.run_id, scenario_id=scn["sid"],
                          name=scn["name"], expected="告警" if expect_alert else "不告警",
                          detected="、".join(kinds) or "无", ok=1 if ok else 0,
                          latency_sec=round(latency, 3), detail=detail)
        store.save_item(item)
        items.append(item)
        # 恢复屏蔽状态
        if scn.get("shield_band") and jam is not None:
            if prev_state is not None:
                store.save_state(prev_state)
            else:
                store.delete_state(jam.device_id)

    run.total = len(items)
    run.hit = hit
    run.miss = miss
    run.false_alarm = false_alarm
    run.avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else 0.0
    run.max_latency = round(max(latencies), 3) if latencies else 0.0
    run.detect_rate = round(hit / float(positives), 3) if positives else 0.0
    run.false_rate = round(false_alarm / float(negatives), 3) if negatives else 0.0
    run.metrics = {
        "positives": positives, "negatives": negatives,
        "locate_error_avg_m": round(sum(loc_errors) / len(loc_errors), 2)
        if loc_errors else 0.0,
        "locate_error_max_m": round(max(loc_errors), 2) if loc_errors else 0.0,
        "self_suppressed": sum(1 for it in items if "自身信号剔除" in (it.detail or "")),
        "latency_sim_sec": REPLAY_LATENCY_SIM_SEC,
        "discovery_target_sec": config.TARGET_DISCOVERY_SEC,
        "meets_target": bool(run.detect_rate >= 0.99 and run.false_alarm == 0
                             and run.max_latency <= config.TARGET_DISCOVERY_SEC),
    }
    store.save_run(run)
    store.log("回放测试", "检出率 %.0f%%、误报 %d、平均时延 %.1fs、定位误差 %.1fm"
              % (run.detect_rate * 100, run.false_alarm, run.avg_latency,
                 run.metrics["locate_error_avg_m"]), operator)
    out = run.to_dict()
    out["items"] = [it.to_dict() for it in items]
    out["ok"] = True
    return out


def latest_metrics(store):
    run = store.latest_run()
    if run is None:
        return {"detect_rate": None, "false_alarm": None, "avg_latency": None,
                "note": "尚未跑回放测试"}
    out = run.to_dict()
    out["items"] = [it.to_dict() for it in store.list_items(run.run_id)]
    return out


def describe():
    return {"回放场景": "%d 个（含 3 个负样本：合法 WiFi/纯噪声/自身屏蔽）"
                        % len(SCENARIOS),
            "指标口径": "检出率、误报数、平均/最大发现时延、定位误差",
            "数据来源": "内置模拟器（source=sim），不冒充真实监测"}
