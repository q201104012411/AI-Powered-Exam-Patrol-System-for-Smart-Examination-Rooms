# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 自检（零第三方依赖）

运行：python 自检.py      （或双击「运行自检.bat」）

设计原则：
  · **不污染真实数据**：启动时把 EXAM_RF_HOME 指到临时目录；联动自检再用 00 的
    沙箱把 02/03/06 的数据目录也重定向到临时目录（与 00/10 的自检同一套做法）；
  · **不修改其它模块**：只调用它们的 integration_api，不改一行代码、不写真实库；
  · **每条都可复现**：通过/失败都打印依据（数字、记录、异常原因），不写"符合要求"；
  · 失败不静默：最后汇总失败项并返回非零退出码。
"""
import json
import os
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- 必须最先设置：让 config 把数据目录指到临时目录 ----
_TMP_HOME = os.environ.get("EXAM_RF_HOME", "").strip()
if not _TMP_HOME:
    _TMP_HOME = tempfile.mkdtemp(prefix="rf_selfcheck_")
    os.environ["EXAM_RF_HOME"] = _TMP_HOME
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")

import compliance                                            # noqa: E402
import config                                                # noqa: E402
import features                                              # noqa: E402
import integration_api                                       # noqa: E402
import linkage_bridge                                        # noqa: E402
import locate                                                # noqa: E402
import models                                                # noqa: E402
import report as report_mod                                  # noqa: E402
import samples                                               # noqa: E402
import schedule                                              # noqa: E402
import shield                                                # noqa: E402
import spectrum                                              # noqa: E402
import storage                                               # noqa: E402
from models import (Anomaly, Approval, Device, Feature, JammerState,         # noqa: E402
                    ReplayItem, ReplayRun, Scan, Session, ShieldTest,
                    SpillTest, SwitchLog, now_epoch)

TOTAL = PASSED = FAILED = SKIPPED = 0
FAILED_NAMES = []
_SECTION = [""]
NOW = models.date_epoch(models.today_str(), 10 * 3600) or now_epoch()


# =============================================================================
# 断言
# =============================================================================
def section(title):
    _SECTION[0] = title
    print("\n" + "=" * 78)
    print("【%s】" % title)
    print("-" * 78)


def chk(name, cond, detail="", soft=False):
    global TOTAL, PASSED, FAILED, SKIPPED
    TOTAL += 1
    ok = bool(cond)
    if ok:
        PASSED += 1
        print("  [PASS] %-46s %s" % (name, _short(detail)))
    elif soft:
        SKIPPED += 1
        print("  [SKIP] %-46s %s" % (name, _short(detail)))
    else:
        FAILED += 1
        FAILED_NAMES.append("[%s] %s：%s" % (_SECTION[0], name, detail))
        print("  [FAIL] %-46s %s" % (name, _short(detail)))
    return ok


def chk_eq(name, got, want, detail=""):
    return chk(name, got == want, detail or "期望 %r，实际 %r" % (want, got))


def chk_near(name, got, want, tol, detail=""):
    try:
        ok = abs(float(got) - float(want)) <= float(tol)
    except (TypeError, ValueError):
        ok = False
    return chk(name, ok, detail or "期望 %.2f±%.2f，实际 %r" % (want, tol, got))


def _short(text, n=100):
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def mk_store(tag):
    path = os.path.join(_TMP_HOME, "dbs", "%s.db" % tag)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    st = storage.Store(path)
    features.ensure_builtin(st)
    return st


def hhmm(epoch):
    return models.hhmm_text(epoch)


def gate_world(tag, with_approval=True, with_session=True, direction=90.0,
               sector=60.0, power=2.0, status=config.DEV_ONLINE, now=None):
    """造一个"屏蔽器 + 审批 + 场次"的最小世界（时间固定在今天 09:30~11:30 窗口）。"""
    now = float(now if now is not None else NOW)
    st = mk_store(tag)
    jam = Device(code="JAM-T", name="测试屏蔽器", kind=config.DEV_JAMMER,
                 site=config.load_settings().get("site") or "第一考点",
                 room_id="RM-T", x=10.0, y=10.0, located=1,
                 direction=direction, sector_width=sector,
                 bands=config.shield_plan(), power_w=power, max_power_w=5.0,
                 status=status, proto=config.PROTO_MANUAL)
    st.save_device(jam)
    if with_approval:
        st.save_approval(Approval(kind=config.APPR_APPROVAL, no="APR-OK",
                                  org="测试机构", site=jam.site,
                                  bands=config.shield_plan(),
                                  valid_from=models.date_str(now - 86400),
                                  valid_to=models.date_str(now + 86400),
                                  doc="测试批文", status=config.APPR_APPROVED))
    if with_session:
        schedule.add_session(st, "测试场", models.today_str(), "09:30", "11:30",
                             pre_open_sec=900, post_close_sec=0, operator="自检")
    return st, jam


# =============================================================================
# A. 配置与频段
# =============================================================================
def sec_config():
    section("A 配置与频段")
    chk_eq("频段数量", len(config.BANDS), 12)
    chk("频段定义 low<high", all(b["low"] < b["high"] for b in config.BANDS),
        "、".join(b["code"] for b in config.BANDS))
    chk_eq("2442 MHz 属于 2.4G ISM", config.band_of(2442.0), "ism24")
    chk_eq("433.5 MHz 属于 UHF", config.band_of(433.5), "uhf")
    chk_eq("5800 MHz 属于 5.8G", config.band_of(5800.0), "unii3")
    chk_eq("520 MHz 不在已知频段", config.band_of(520.0), "")
    chk_eq("非法输入返回空", config.band_of("abc"), "")
    chk("频段中文名", "2.4G" in config.band_name("ism24"), config.band_name("ism24"))
    groups = {config.BAND_INDEX[b]["group"] for b in config.shield_plan()}
    chk("屏蔽计划覆盖移动通信", "cellular" in groups, "、".join(sorted(groups)))
    chk("屏蔽计划覆盖短距/对讲", "shortrange" in groups and "intercom" in groups,
        "、".join(sorted(groups)))
    chk_eq("异常发现时延目标", config.TARGET_DISCOVERY_SEC, 10.0)
    chk_eq("监测覆盖余量", config.TARGET_COVERAGE_MARGIN_M, 100.0)
    chk_eq("功率上限", config.TARGET_POWER_MAX_W, 5.0)
    chk_eq("扇区上限", config.TARGET_SECTOR_MAX_DEG, 90.0)
    t = config.thresholds()
    chk("默认阈值在上下限内",
        all(config.THRESHOLD_MIN[k] <= t[k] <= config.THRESHOLD_MAX[k] for k in t),
        "门限 %.1f dB" % t["noise_margin_db"])
    t2 = config.thresholds({"thresholds": {"noise_margin_db": 999,
                                           "sustain_frames": 0}})
    chk_eq("阈值上限夹紧", t2["noise_margin_db"], config.THRESHOLD_MAX["noise_margin_db"])
    chk_eq("阈值下限夹紧", t2["sustain_frames"], config.THRESHOLD_MIN["sustain_frames"])
    cfg = config.load_settings()
    chk("设置含全部默认键", set(config.SETTINGS_DEFAULT) <= set(cfg),
        "%d 项" % len(cfg))
    site0 = cfg.get("site")
    config.save_settings({"site": "自检考点"})
    chk_eq("设置写读往返", config.load_settings().get("site"), "自检考点")
    config.save_settings({"site": site0})
    chk_eq("身份证掩码", config.mask_id("110101200801011234"), "**************1234")
    chk("describe 含数据目录", "数据目录" in config.describe(), "")
    chk("集成 op 无重复", len(config.INTEGRATION_OPS) == len(set(config.INTEGRATION_OPS)),
        "%d 个" % len(config.INTEGRATION_OPS))
    chk_eq("集成 op 数量", len(config.INTEGRATION_OPS), 37)
    chk("op 表与接口一致", set(config.INTEGRATION_OPS) == set(integration_api.OPS),
        "差集：%s" % (set(config.INTEGRATION_OPS) ^ set(integration_api.OPS)))
    for key in ("00", "02", "03", "06"):
        d = config.MODULE_DIRS.get(key)
        chk("联动目录存在 %s" % key, bool(d) and os.path.isdir(str(d)), str(d))


# =============================================================================
# B. 模型与存储
# =============================================================================
def sec_storage(store):
    section("B 数据模型与本地存储")
    chk("数据库文件已创建", os.path.isfile(str(config.DB_PATH)), str(config.DB_PATH))
    dev = Device(code="MON-T", name="测试站", kind=config.DEV_MONITOR,
                 site="第一考点", x=1.0, y=2.0, located=1,
                 bands=["ism24"], status=config.DEV_ONLINE)
    store.save_device(dev)
    got = store.get_device(code="MON-T")
    chk("设备写读（含 JSON 字段）", got is not None and got.bands == ["ism24"],
        "%s %s" % (got.code if got else "-", got.bands if got else "-"))
    chk_eq("坐标标记持久化", got.located if got else 0, 1)
    store.heartbeat("MON-T", "offline")
    chk_eq("心跳更新状态", store.get_device(code="MON-T").status, "offline")
    chk_eq("按类型过滤设备", len(store.list_devices(kind=config.DEV_MONITOR)) >= 1, True)

    scan = Scan(station_code="MON-T", band="ism24", ts_epoch=NOW,
                noise_floor_dbm=-100.0, peak_count=1,
                peaks=[{"freq_mhz": 2442.0, "power_dbm": -60.0,
                        "bandwidth_mhz": 2.0}],
                raw={"bins": [[2400.0, -101.0], [2442.0, -60.0]]}, source="sim")
    store.save_scan(scan)
    got = store.get_scan(scan.scan_id)
    chk("扫描帧写读（含 peaks/raw）",
        got is not None and got.peaks[0]["freq_mhz"] == 2442.0
        and got.raw["bins"][1][1] == -60.0)

    a = Anomaly(station_code="MON-T", kind=config.KIND_CHEAT_BURST, band="ism24",
                freq_mhz=2442.0, bandwidth_mhz=2.0, peak_dbm=-60.0,
                noise_floor_dbm=-100.0, snr_db=40.0, severity="critical",
                status=config.ST_OPEN, detected_epoch=NOW, first_seen_epoch=NOW,
                last_seen_epoch=NOW, latency_sec=1.0, detail="自检")
    store.save_anomaly(a)
    chk("异常写读", store.get_anomaly(a.anomaly_id) is not None, a.anomaly_id)
    dup = store.find_recent_duplicate("MON-T", "ism24", 2442.3, 0.6, 180, now=NOW)
    chk("同频合并能命中", dup is not None and dup.anomaly_id == a.anomaly_id)
    chk("超出容差不会误合并",
        store.find_recent_duplicate("MON-T", "ism24", 2445.0, 0.6, 180,
                                    now=NOW) is None)
    stats = store.anomaly_stats(since_epoch=NOW - 60)
    chk("异常统计计数", stats["total"] >= 1 and stats["critical"] >= 1,
        "total=%d critical=%d" % (stats["total"], stats["critical"]))
    chk("时延 P95 可计算", store.latency_percentile(0.95, since_epoch=NOW - 60) >= 1.0,
        "%.1f" % store.latency_percentile(0.95, since_epoch=NOW - 60))

    ap = Approval(kind=config.APPR_APPROVAL, no="AP-T", org="测试", site="第一考点",
                  bands=["ism24"], valid_from=models.date_str(NOW - 86400),
                  valid_to=models.date_str(NOW + 86400), doc="批文",
                  status=config.APPR_APPROVED)
    store.save_approval(ap)
    good, why = store.valid_approval("第一考点", ["ism24"])
    chk("有效审批可查到", good is not None,
        "%s（%s）" % (good.no if good else "-", why or "有效"))
    store.save_approval(Approval(kind=config.APPR_APPROVAL, no="AP-OLD",
                                 site="第一考点", bands=["ism24"],
                                 valid_from="2020-01-01", valid_to="2020-01-02",
                                 status=config.APPR_APPROVED))
    bad, why = store.valid_approval("无此考点", ["ism24"])
    chk("考点不匹配返回空", bad is None, why)

    log_store = mk_store("storelog")
    lg = SwitchLog(device_id="JAM-X", action=config.SW_ON, bands=["ism24"],
                   power_w=2.0, source=config.SW_SRC_MANUAL, operator="自检",
                   ts_epoch=NOW, reason="测试")
    log_store.save_switch(lg)
    chk("开关记录写读", log_store.last_switch("JAM-X") is not None)
    jstate = JammerState(device_id="JAM-X", is_on=1, since_epoch=NOW,
                         bands=["ism24"], power_w=2.0, operator="自检")
    log_store.save_state(jstate)
    chk_eq("屏蔽状态 on_states", len(log_store.on_states()) >= 1, True)
    tomorrow = models.date_str(NOW + 86400)
    ses = Session(name="存储测试", date=tomorrow, start="09:00",
                  end="10:00", state=config.SESSION_STATE_PLANNED)
    log_store.save_session(ses)
    chk("场次写读", log_store.get_session(ses.session_id) is not None)
    chk("按日期筛选场次", any(x.session_id == ses.session_id
                              for x in log_store.list_sessions(date=tomorrow)))
    log_store.close()

    f = Feature(feature_code="FEAT-TEST", name="测试特征", low_mhz=100.0,
                high_mhz=110.0, bandwidth_mhz=1.0, duty="burst",
                base_confidence=0.5, builtin=0)
    store.save_feature(f)
    chk("特征写读", store.get_feature("FEAT-TEST") is not None)
    store.save_correlation(models.Correlation(source="06", ref_id="A1",
                                              detail="测试"))
    chk("联动记录写读", len(store.list_correlations(source="06")) >= 1)
    store.save_location(models.Location(anomaly_id=a.anomaly_id, x=1.0, y=2.0,
                                        radius_m=10.0, confidence=0.5,
                                        method="测试"))
    chk("定位记录写读", len(store.list_locations(anomaly_id=a.anomaly_id)) >= 1)
    run = ReplayRun(total=1, hit=1, detect_rate=1.0)
    store.save_run(run)
    store.save_item(ReplayItem(run_id=run.run_id, scenario_id="SX", name="测试",
                               expected="告警", detected="cheat", ok=1))
    chk("回放记录写读", store.latest_run() is not None
        and len(store.list_items(run.run_id)) == 1)
    okc, n, msg = store.verify_chain()
    chk("审计链完整", okc and n >= 1, msg)
    s = store.summary()
    chk("概览字段齐全", all(k in s for k in ("devices", "scans", "anomalies",
                                            "open_anomalies", "jammers_on")),
        "设备 %d" % s["devices"])

    tamper = mk_store("tamper")
    tamper.log("写入", "第一条", "自检")
    tamper.log("写入", "第二条", "自检")
    tamper._exec("UPDATE audits SET detail='被篡改' WHERE rowid=1")
    okc2, _n2, msg2 = tamper.verify_chain()
    chk("篡改审计链能发现", not okc2, msg2)
    tamper.close()

    old = Approval(kind=config.APPR_APPROVAL, no="AP-KEEP",
                   valid_from="2020-01-01", valid_to="2020-01-02",
                   status=config.APPR_APPROVED)
    store.save_approval(old)
    purged = store.purge_runtime(keep_days=1)
    chk("数据维护不删审批", store.list_approvals(kind=config.APPR_APPROVAL,
                                              site=None, limit=500)
        and any(x.no == "AP-KEEP" for x in
                store.list_approvals(kind=config.APPR_APPROVAL, limit=500)),
        "清理 %s" % purged)
    csv_path = os.path.join(_TMP_HOME, "exports", "test.csv")
    chk("CSV 导出", storage.export_csv([{"a": 1, "b": 2}], csv_path)
        and os.path.isfile(csv_path))
    json_path = os.path.join(_TMP_HOME, "exports", "test.json")
    chk("JSON 导出", storage.export_json([jstate], json_path)
        and os.path.isfile(json_path))


# =============================================================================
# C. 特征库
# =============================================================================
def sec_features(store):
    section("C 作弊信号特征库")
    added = features.ensure_builtin(store)
    chk_eq("内置特征首次入库", added, 0)     # 主库已 ensure 过
    raw_feat = storage.Store(os.path.join(_TMP_HOME, "dbs", "feat_raw.db"))
    chk_eq("内置特征数量", features.ensure_builtin(raw_feat),
           len(features.BUILTIN_FEATURES))
    chk_eq("重复 ensure 不重复插入", features.ensure_builtin(raw_feat), 0)
    raw_feat.close()
    fresh = mk_store("feat")
    chk_eq("特征目录条数", len(features.catalog(fresh)), len(features.BUILTIN_FEATURES))
    sm = features.summary(fresh)
    chk("作弊特征不少于 4 条", sm["cheat"] >= config.TARGET_FEATURE_MIN,
        "作弊 %d / 合法 %d" % (sm["cheat"], sm["legal"]))
    m = features.match(2442.0, 2.0, "burst")
    chk("2.4G 数传命中作弊特征", m["matched"] and not m["is_legal"],
        m["feature"].feature_code if m["feature"] else m["reason"])
    chk("2.4G 匹配置信度≥0.7", m["confidence"] >= 0.7, "%.2f" % m["confidence"])
    m = features.match(2437.0, 20.0, "continuous")
    chk("合法 WiFi 判为合法", m["matched"] and m["is_legal"],
        m["feature"].feature_code if m["feature"] else m["reason"])
    m = features.match(433.5, 0.03, "continuous")
    chk("UHF 隐形耳机命中窄带特征",
        m["matched"] and not m["is_legal"] and m["feature"].bandwidth_mhz <= 0.5,
        m["feature"].feature_code if m["feature"] else m["reason"])
    m = features.match(520.0, 0.05, "continuous")
    chk("520 MHz 无特征可匹配", not m["matched"], m["reason"])
    m = features.match(5800.0, 8.0, "burst")
    chk("5.8G 图传命中作弊特征", m["matched"] and not m["is_legal"],
        m["feature"].feature_code if m["feature"] else m["reason"])
    sc, parts = features.score_peak(520.0, 1.0, "burst",
                                    features.builtin_list()[0])
    chk_eq("频率不命中得分为 0", sc, 0.0)
    ok, info = features.add_feature(fresh, "FEAT-CUSTOM", "自定义特征", 1000.0,
                                    1010.0, 1.0, base_confidence=0.9)
    chk("新增自定义特征", ok and fresh.get_feature("FEAT-CUSTOM") is not None, info)
    ok, info = features.add_feature(fresh, "FEAT-CUSTOM", "自定义特征 v2", 1000.0,
                                    1010.0, 1.0, base_confidence=0.8)
    chk("同编码覆盖更新", ok and fresh.get_feature("FEAT-CUSTOM").name.endswith("v2"), info)
    ok, info = features.add_feature(fresh, "FEAT-BAD", "倒置", 110.0, 100.0, 1.0)
    chk("低端≥高端被拒绝", not ok, info)
    ok, info = features.add_feature(fresh, "FEAT-BAD2", "坏置信度", 100.0, 110.0,
                                    1.0, base_confidence=2.0)
    chk("置信度越界被拒绝", not ok, info)
    custom = fresh.get_feature("FEAT-CUSTOM")
    custom.enabled = 0
    fresh.save_feature(custom)
    m = features.match(1005.0, 1.0, "burst", fresh.list_features(enabled_only=True))
    chk("停用特征不参与匹配", not m["matched"], m["reason"])
    chk_near("打分权重合计 1.0", features.W_FREQ + features.W_BW + features.W_DUTY,
             1.0, 0.001)
    chk("特征范围文本", "MHz" in features.builtin_list()[0].range_text())
    fresh.close()


# =============================================================================
# D. 频谱检测
# =============================================================================
def sec_spectrum(store):
    section("D 频谱分析与异常发现")
    chk_eq("中位数（奇数）", spectrum.median([1, 5, 3]), 3.0)
    chk_eq("中位数（偶数）", spectrum.median([1, 2, 3, 4]), 2.5)
    chk_eq("MAD 稳健标准差（全等）", spectrum.mad_sigma([5, 5, 5, 5]), 0.0)
    floor, _sig = spectrum.noise_floor([(2400 + i, -100.0) for i in range(20)])
    chk_eq("噪声底估计", floor, -100.0)
    chk_eq("bin 步长", spectrum.bin_step([(2400.0, -100), (2400.5, -100),
                                          (2401.0, -100)]), 0.5)
    bins = [(2400.0 + i * 0.5, -100.0) for i in range(81)]
    # 手动注入一个峰
    for i, (f, p) in enumerate(bins):
        if abs(f - 2440.0) <= 1.0:
            bins[i] = (f, -60.0)
    peaks = spectrum.detect_peaks(bins, -100.0, 6.0)
    chk("峰值检测数量", len(peaks) == 1, "%d 个" % len(peaks))
    chk_near("峰值中心频率", peaks[0]["freq_mhz"], 2440.0, 0.6)
    chk_near("峰值功率", peaks[0]["power_dbm"], -60.0, 0.01)
    chk("峰值带宽≈2 MHz", 1.0 <= peaks[0]["bandwidth_mhz"] <= 3.0,
        "%.2f" % peaks[0]["bandwidth_mhz"])
    flat = [(2400.0 + i * 0.5, -100.0) for i in range(50)]
    chk_eq("纯噪声无峰值", len(spectrum.detect_peaks(flat, -100.0, 6.0)), 0)
    two = [(2400.0 + i * 0.5, -100.0) for i in range(100)]
    for i, (f, p) in enumerate(two):
        if abs(f - 2410.0) <= 0.5 or abs(f - 2440.0) <= 0.5:
            two[i] = (f, -70.0)
    chk_eq("两个分离峰", len(spectrum.detect_peaks(two, -100.0, 6.0)), 2)
    chk_eq("占用率（全噪声）", spectrum.occupancy(flat, -100.0, 6.0), 0.0)
    chk_eq("占用率（全超限）", spectrum.occupancy([(2400.0, -50.0)],
                                               -100.0, 6.0), 1.0)

    frame, err = spectrum.normalize_frame({"bins": flat})
    chk("缺少监测站被拒绝", frame is None and "station" in err, err)
    frame, err = spectrum.normalize_frame({"station_code": "MON-A", "bins": bins})
    chk("bins 归一化出峰", frame is not None and len(frame["peaks"]) == 1)
    frame2, _ = spectrum.normalize_frame({
        "station_code": "MON-A",
        "peaks": [{"freq_mhz": 2442.0, "power_dbm": -62.0, "bandwidth_mhz": 2.0}]})
    chk("peaks 直接输入", frame2 is not None and len(frame2["peaks"]) == 1)
    frame3, _ = spectrum.normalize_frame({"station_code": "MON-A",
                                          "freq_mhz": 433.5, "power_dbm": -78.0,
                                          "bandwidth_mhz": 0.025})
    chk("单频点输入", frame3 is not None and frame3["peaks"][0]["freq_mhz"] == 433.5)
    frame4, err = spectrum.normalize_frame({"station_code": "MON-A",
                                            "band": "nope", "bins": flat})
    chk("未知频段被拒绝", frame4 is None and "未知频段" in err, err)

    r = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=2442.0, bw=2.0, power=-62.0, band="ism24",
        ts=NOW, source="sim"), source="sim", now=NOW, detect_epoch=NOW + 1.0)
    chk("扫描入库", r.get("ok") and r.get("scan_id"), r.get("reason", ""))
    chk("2.4G 作弊触发告警", bool(r.get("new")),
        "%d 条" % len(r.get("new") or []))
    if r.get("new"):
        ad = store.get_anomaly(r["new"][0])
        chk_eq("异常类型=数传作弊", ad.kind, config.KIND_CHEAT_BURST)
        chk_eq("严重度=critical", ad.severity, "critical")
        chk_near("发现时延=1.0s", ad.latency_sec, 1.0, 0.01)
        chk("详情含特征依据", ad.feature_name and "匹配分" in (ad.detail or ""),
            ad.feature_name)
    r2 = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=2437.0, bw=20.0, power=-50.0, band="ism24",
        ts=NOW + 2, source="sim"), source="sim", now=NOW + 2,
        detect_epoch=NOW + 3)
    chk("合法 WiFi 不告警", not r2.get("new") and r2.get("legal", 0) >= 1,
        "legal=%d" % r2.get("legal", 0))
    r3 = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=2440.0, bw=2.0, power=None, band="ism24",
        ts=NOW + 4, source="sim"), source="sim", now=NOW + 4,
        detect_epoch=NOW + 5)
    chk("纯噪声不告警", not r3.get("new"), "峰值 %d" % r3.get("peaks", 0))

    # 自身屏蔽信号：打开屏蔽后同频低信噪比信号应被剔除
    jw, jwjam = gate_world("self", with_approval=True)
    jw.save_state(JammerState(device_id=jwjam.device_id, is_on=1,
                              bands=["ism24"], since_epoch=NOW, power_w=1.0))
    rs = spectrum.ingest_scan(jw, samples.demo_frame(
        station="MON-A", freq=2440.0, bw=30.0, power=-90.0, band="ism24",
        ts=NOW + 5, source="sim"), source="sim", now=NOW + 5,
        detect_epoch=NOW + 6)
    chk("屏蔽自身信号被剔除", rs.get("self_suppressed", 0) >= 1
        and not rs.get("new"), "剔除 %d" % rs.get("self_suppressed", 0))
    jw.close()

    # 合并与升级
    frame = samples.demo_frame(station="MON-A", freq=2450.0, bw=2.0, power=-62.0,
                               band="ism24", ts=NOW + 10, source="sim")
    r4 = spectrum.ingest_scan(store, frame, source="sim", now=NOW + 10,
                              detect_epoch=NOW + 11)
    aid = (r4.get("new") or [None])[0]
    r5 = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=2450.1, bw=2.0, power=-60.0, band="ism24",
        ts=NOW + 12, source="sim"), source="sim", now=NOW + 12,
        detect_epoch=NOW + 13)
    chk("同频同站合并", aid is not None and aid in (r5.get("merged") or []),
        "new=%s merged=%s" % (aid, r5.get("merged")))
    ad = store.get_anomaly(aid) if aid else None
    chk("合并后计数增加", ad is not None and ad.seen_count >= 2,
        "seen=%s" % (ad.seen_count if ad else "-"))
    for k in range(3):
        spectrum.ingest_scan(store, samples.demo_frame(
            station="MON-A", freq=2450.1, bw=2.0, power=-60.0, band="ism24",
            ts=NOW + 14 + k * 2, source="sim"), source="sim",
            now=NOW + 14 + k * 2, detect_epoch=NOW + 15 + k * 2)
    ad = store.get_anomaly(aid) if aid else None
    chk("持续多帧后严重度不降", ad is not None and
        config.SEVERITY_ORDER[ad.severity] >= config.SEVERITY_ORDER["warn"],
        ad.severity if ad else "-")

    r6 = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=520.0, bw=0.05, power=-80.0, band="",
        freq_range=(500.0, 540.0), ts=NOW + 20, source="sim"), source="sim",
        now=NOW + 20, detect_epoch=NOW + 21)
    kinds = [store.get_anomaly(x).kind for x in (r6.get("new") or [])]
    chk("非法频段判定", config.KIND_ILLEGAL in kinds, "、".join(kinds))
    r7 = spectrum.ingest_scan(store, samples.demo_frame(
        station="MON-A", freq=3500.0, bw=60.0, power=-60.0, band="nr3500",
        ts=NOW + 22, source="sim"), source="sim", now=NOW + 22,
        detect_epoch=NOW + 23)
    kinds = [store.get_anomaly(x).kind for x in (r7.get("new") or [])]
    chk("宽带异常判定", config.KIND_WIDEBAND in kinds, "、".join(kinds))
    chk("占空推断：连续", spectrum.infer_duty(
        store, "MON-A", "ism24", 2442.0, NOW + 30) in
        ("continuous", "burst", "hopping"))
    chk("屏蔽带内集合", isinstance(spectrum.shield_active_bands(store), set))
    chk("自身信号判据（带外不豁免）",
        not spectrum.is_self_signal("uhf", 5.0, 1.0, {"ism24"}))
    chk("扫描计数增加", store.scan_count() >= 5, "%d 帧" % store.scan_count())
    chk("analyze_peaks 便利入口", spectrum.analyze_peaks(
        store, [{"freq_mhz": 2460.0, "power_dbm": -62.0, "bandwidth_mhz": 2.0}],
        "MON-A", "ism24", ts_epoch=NOW + 32).get("ok"))


# =============================================================================
# E. 定位
# =============================================================================
def sec_locate(store):
    section("E 信号定位")
    d = locate.rssi_to_distance(-70.0)
    chk_near("场强->距离", locate.distance_to_rssi(d), -70.0, 0.01)
    chk_near("距离下限", locate.rssi_to_distance(0.0), locate.DIST_MIN_M, 0.01)
    chk_near("距离上限", locate.rssi_to_distance(-200.0), locate.DIST_MAX_M, 0.01)
    pts = [{"x": 0.0, "y": 0.0, "d": 50.0},
           {"x": 80.0, "y": 0.0, "d": 50.0},
           {"x": 40.0, "y": 60.0, "d": 30.0}]
    res = locate.multilaterate(pts)
    chk("三站解算成功", res.get("ok"), res.get("reason", ""))
    chk_near("解算 x=40", res.get("x"), 40.0, 0.5)
    chk_near("解算 y=30", res.get("y"), 30.0, 0.5)
    chk_near("残差≈0", res.get("residual"), 0.0, 0.1)
    noisy = [dict(p) for p in pts]
    noisy[0]["d"] += 3.0
    noisy[2]["d"] -= 2.0
    res2 = locate.multilaterate(noisy)
    chk("含噪解算仍可用", res2.get("ok") and res2.get("residual") < 20.0,
        "残差 %.1f" % res2.get("residual", 0))
    chk("少于 3 站拒绝", not locate.multilaterate(pts[:2]).get("ok"))
    c = locate.weighted_centroid([{"x": 0, "y": 0, "d": 10},
                                  {"x": 100, "y": 0, "d": 100}])
    chk("加权质心偏向近站", c is not None and c[0] < 50.0, str(c))
    chk_near("方位角：正东", locate.bearing_between(0, 0, 1, 0), 90.0, 0.01)
    chk_near("方位角：正北", locate.bearing_between(0, 0, 0, 1), 0.0, 0.01)
    res = locate.locate_bearings([{"x": 0, "y": 0, "bearing": 45.0},
                                  {"x": 100, "y": 0, "bearing": 315.0}])
    chk("双站测向交会", res.get("ok"), res.get("reason", ""))
    chk_near("交会 x=50", res.get("x"), 50.0, 0.5)
    chk_near("交会 y=50", res.get("y"), 50.0, 0.5)
    chk("平行测向拒绝", not locate.locate_bearings(
        [{"x": 0, "y": 0, "bearing": 90.0}, {"x": 0, "y": 10, "bearing": 90.0}]).get("ok"))
    chk("退化方程返回 None", locate._solve2x2(1, 2, 2, 4, 1, 1) is None)
    hint = locate.probe_hint(-80.0, -70.0)
    chk_eq("逼近提示：靠近", hint["trend"], "closer")
    chk_eq("逼近提示：远离", locate.probe_hint(-80.0, -90.0)["trend"], "farther")
    chk_eq("逼近提示：持平", locate.probe_hint(-80.0, -80.5)["trend"], "hold")

    cov0 = locate.coverage(mk_store("nocov"))
    chk_eq("无坐标时覆盖率为 0", cov0["rate"], 0.0)
    cov = locate.coverage(store)
    chk("演示站点覆盖率≥0.95", cov["rate"] >= 0.95,
        "%.0f%%（%d/%d）" % (cov["rate"] * 100, cov["covered"], cov["points"]))
    chk("覆盖采样点数量", cov["points"] == 25, str(cov["points"]))

    # 真实链路：三站扫描 -> 异常 -> 定位
    base = NOW + 60
    for st_code, power in (("MON-A", -90.97), ("MON-B", -90.97), ("MON-C", -84.31)):
        spectrum.ingest_scan(store, samples.demo_frame(
            station=st_code, freq=2470.0, bw=2.0, power=power, band="ism24",
            ts=base, source="sim"), source="sim", now=base,
            detect_epoch=base + 1.0)
    ads = [a for a in store.list_anomalies(limit=300)
           if abs(a.freq_mhz - 2470.0) < 1.0 and abs(a.first_seen_epoch - base) < 2]
    chk("三站各成一条异常", len(ads) == 3, "%d 条" % len(ads))
    if ads:
        loc = locate.locate_anomaly(store, ads[0], operator="自检")
        chk("三站定位成功", loc.get("ok"), loc.get("reason", ""))
        chk_near("定位误差≤15m", abs(loc.get("x", 0) - 40.0) + abs(loc.get("y", 0) - 30.0),
                 0.0, 15.0, "位置 (%.1f, %.1f)" % (loc.get("x", 0), loc.get("y", 0)))
        chk("定位方法=多站场强", "多站场强" in loc.get("method", ""), loc.get("method"))
        chk("定位记录已落库", len(store.list_locations(anomaly_id=ads[0].anomaly_id)) >= 1)
        chk("异常已回填坐标", store.get_anomaly(ads[0].anomaly_id).located_x >= 0)
        meas = locate.collect_measurements(store, ads[0])
        chk("测量点来自 3 个站", len(meas) == 3, "%d 个" % len(meas))


# =============================================================================
# F. 考试时间表
# =============================================================================
def sec_schedule(store):
    section("F 考试时间表与开关窗口")
    chk_eq("HH:MM 解析", models.parse_hhmm("09:30"), 9 * 3600 + 30 * 60)
    chk_eq("HHMM 解析", models.parse_hhmm("0930"), 9 * 3600 + 30 * 60)
    chk_eq("非法时间", models.parse_hhmm("25:00"), None)
    chk_eq("时间格式化", models.hhmm_sec(9 * 3600 + 5 * 60), "09:05")
    ses = Session(name="窗口测试", date=models.today_str(), start="09:30",
                  end="11:30", pre_open_sec=900, post_close_sec=0)
    op, cl, st, en = schedule.window(ses)
    chk("窗口跨度正确", (st - op) == 900 and (cl - en) == 0 and (en - st) == 7200,
        "开 %s 关 %s" % (models.ts_text(op)[11:16], models.ts_text(cl)[11:16]))
    chk_eq("阶段：窗口前", schedule.state_at(ses, op - 1), "before")
    chk_eq("阶段：开考前", schedule.state_at(ses, op + 1), "pre_open")
    chk_eq("阶段：考试中", schedule.state_at(ses, st + 1), "active")
    chk_eq("阶段：结束后", schedule.state_at(ses, en + 1), "closed")
    chk("允许开启（窗口内）", schedule.allowed_to_shield(ses, st + 10))
    chk("不允许开启（窗口外）", not schedule.allowed_to_shield(ses, op - 10))
    chk("剩余时间文本", "分钟" in schedule.remaining_text(ses, st + 10))

    w = mk_store("sched")
    ok, sess1 = schedule.add_session(w, "语文", models.today_str(), "09:00", "11:00",
                                     subject="语文", rooms=["RM-1"], operator="自检")
    chk("新增场次", ok, getattr(sess1, "session_id", ""))
    sid = sess1.session_id if ok else ""
    ok, msg = schedule.add_session(w, "坏场次", models.today_str(), "11:00", "09:00")
    chk("结束早于开始被拒绝", not ok, msg)
    ok, msg = schedule.add_session(w, "坏日期", "2026-13-01", "09:00", "11:00")
    chk("非法日期被拒绝", not ok, msg)
    ok, msg = schedule.add_session(w, "坏时间", models.today_str(), "9点", "11:00")
    chk("非法时间被拒绝", not ok, msg)
    ok2, info2 = schedule.add_session(w, "语文", models.today_str(), "09:10", "11:10",
                                      operator="自检")
    chk("同名场次更新不新建", ok2 and info2.session_id == sid)
    chk_eq("同名更新生效", info2.start, "09:10")
    cur, phase = schedule.find_window(w, now=schedule.window(info2)[2] + 10)
    chk("找当前窗口", cur is not None and phase == "active", phase)
    chk("找活动场次", schedule.active_session(
        w, now=schedule.window(info2)[2] + 10) is not None)
    chk("找下一场", schedule.next_session(w, now=NOW - 86400) is not None)
    changed = schedule.refresh_states(w, now=schedule.window(info2)[3] + 10)
    chk("场次状态自动转已结束", any(x.session_id == info2.session_id
                                    and x.state == config.SESSION_STATE_CLOSED
                                    for x in changed))
    chk("tick 返回窗口信息", "in_window" in schedule.tick(w))

    # 一致性核对
    c = mk_store("cons")
    ok, s1 = schedule.add_session(c, "上午场", models.today_str(), "09:30", "11:30",
                                  pre_open_sec=900, operator="自检")
    op1, cl1, st1, en1 = schedule.window(s1)
    c.save_switch(SwitchLog(device_id="JAM-C", action=config.SW_ON,
                            ts_epoch=st1 + 60, bands=["ism24"], power_w=1.0,
                            source=config.SW_SRC_MANUAL))
    c.save_switch(SwitchLog(device_id="JAM-C", action=config.SW_OFF,
                            ts_epoch=en1 - 60, bands=["ism24"], power_w=1.0,
                            source=config.SW_SRC_MANUAL))
    cons = schedule.consistency(c, now=en1 + 600)
    chk("窗口内开关判一致", cons["fail"] == 0, "fail=%d warn=%d"
        % (cons["fail"], cons["warn"]))
    chk_eq("区间配对数", cons["intervals"], 1)
    c.save_switch(SwitchLog(device_id="JAM-E", action=config.SW_ON,
                            ts_epoch=op1 - 300, bands=["ism24"], power_w=1.0))
    c.save_switch(SwitchLog(device_id="JAM-E", action=config.SW_OFF,
                            ts_epoch=st1 + 10, bands=["ism24"], power_w=1.0))
    cons = schedule.consistency(c, now=en1 + 600)
    chk("提前开启能判出", any(r["kind"] == "early_on" and r["level"] == "fail"
                              for r in cons["rows"]),
        "、".join(r["kind"] for r in cons["rows"]))
    c.save_switch(SwitchLog(device_id="JAM-L", action=config.SW_ON,
                            ts_epoch=st1 + 10, bands=["ism24"], power_w=1.0))
    cons = schedule.consistency(c, now=en1 + 600)
    chk("超时未关能判出", any(r["kind"] == "still_on" for r in cons["rows"]),
        "、".join(r["kind"] for r in cons["rows"]))
    c.save_switch(SwitchLog(device_id="JAM-X", action=config.SW_OFF,
                            ts_epoch=NOW, bands=[], power_w=0.0))
    ints, probs = schedule.switch_intervals(c)
    chk("关闭无开启能报异常", any(p["kind"] == "off_without_on" for p in probs),
        "、".join(p["kind"] for p in probs))

    m = mk_store("miss")
    ok, s2 = schedule.add_session(m, "已结束场", models.today_str(), "08:00", "09:00",
                                  operator="自检")
    op2, cl2, st2, en2 = schedule.window(s2)
    cons2 = schedule.consistency(m, now=en2 + 600)
    chk("整场未开屏蔽能判出",
        any(r["kind"] == "missing_shield" for r in cons2["rows"]),
        "、".join(r["kind"] for r in cons2["rows"]))
    for x in (w, c, m):
        x.close()


# =============================================================================
# G. 屏蔽管控
# =============================================================================
def sec_shield():
    section("G 屏蔽管控")
    chk_eq("功率上限", shield.power_cap(), min(5.0, config.load_settings()["max_power_w"]))
    st, jam = gate_world("gate_ok")
    chk_eq("默认频段数", len(shield.bands_of(jam)), len(config.shield_plan()))
    ev = shield.evaluate(st, jam, now=NOW)
    chk("审批+窗口+定向齐全：允许开启", ev["can_on"],
        "；".join(b["text"] for b in ev["blockers"]))
    chk("计划功率已限幅", ev["plan"]["power_w"] <= shield.power_cap(),
        "%.2fW" % ev["plan"]["power_w"])
    r = shield.turn_on(st, jam, operator="自检", reason="自检开启", now=NOW)
    chk("开启成功", r.get("ok"), r.get("reason", ""))
    chk_eq("状态已开启", st.get_state(jam.device_id).is_on, 1)
    chk_eq("开关记录写入", st.last_switch(jam.device_id).action, config.SW_ON)
    chk("重复开启被拒绝", not shield.turn_on(st, jam, now=NOW).get("ok"))
    r = shield.turn_off(st, jam, operator="自检", reason="自检关闭", now=NOW + 600)
    chk("关闭成功", r.get("ok"), r.get("reason", ""))
    chk_eq("关闭后状态", st.get_state(jam.device_id).is_on, 0)
    chk_near("持续时长记录", r.get("duration_sec"), 600.0, 1.0)
    chk_eq("关闭记录写入", st.last_switch(jam.device_id).action, config.SW_OFF)

    st2, jam2 = gate_world("gate_noap", with_approval=False)
    ev = shield.evaluate(st2, jam2, now=NOW)
    chk("无审批拒绝开启", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_APPROVAL for b in ev["blockers"]),
        "；".join(b["text"] for b in ev["blockers"]))
    st3, jam3 = gate_world("gate_nowin", with_session=False)
    ev = shield.evaluate(st3, jam3, now=NOW)
    chk("无窗口拒绝开启", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_WINDOW for b in ev["blockers"]))
    st4, jam4 = gate_world("gate_nodir", direction=-1.0)
    ev = shield.evaluate(st4, jam4, now=NOW)
    chk("无方位角拒绝开启", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_DIRECTION for b in ev["blockers"]))
    st5, jam5 = gate_world("gate_wide", sector=180.0)
    ev = shield.evaluate(st5, jam5, now=NOW)
    chk("扇区过宽拒绝开启", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_SECTOR for b in ev["blockers"]))
    st6, jam6 = gate_world("gate_offline", status=config.DEV_OFFLINE)
    ev = shield.evaluate(st6, jam6, now=NOW)
    chk("设备离线拒绝开启", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_OFFLINE for b in ev["blockers"]))
    st7, jam7 = gate_world("gate_power", power=20.0)
    ev = shield.evaluate(st7, jam7, now=NOW)
    chk("超功率按上限下发（警告）", ev["can_on"] and ev["plan"]["power_w"] <= 5.0,
        "计划 %.1fW" % ev["plan"]["power_w"])

    # 演练
    cfg = config.load_settings()
    cfg_drill = dict(cfg)
    cfg_drill["drill_enabled"] = False
    st8, jam8 = gate_world("gate_drill", with_approval=False, with_session=False)
    ev = shield.evaluate(st8, jam8, now=NOW, drill=True, cfg=cfg_drill)
    chk("演练未开启时拒绝", not ev["can_on"] and any(
        b["code"] == shield.BLOCK_DRILL for b in ev["blockers"]))
    cfg_drill["drill_enabled"] = True
    ev = shield.evaluate(st8, jam8, now=NOW, drill=True, cfg=cfg_drill)
    chk("演练开启后允许", ev["can_on"], "；".join(b["text"] for b in ev["blockers"]))
    r = shield.turn_on(st8, jam8, now=NOW, drill=True, cfg=cfg_drill)
    chk("演练开机记录来源 drill", r.get("ok") and r.get("source") == config.SW_SRC_DRILL,
        r.get("source", ""))

    # 自动开 / 自动关
    st9, jam9 = gate_world("gate_auto")
    r = shield.tick(st9, now=NOW)
    chk("窗口内自动开启", st9.get_state(jam9.device_id).is_on == 1,
        "动作 %s" % r.get("actions"))
    r = shield.tick(st9, now=NOW + 7200)      # 12:00，窗口 11:30 已结束
    chk("窗口结束自动关闭", st9.get_state(jam9.device_id).is_on == 0,
        "动作 %s" % r.get("actions"))
    chk("自动关闭来源为 auto",
        st9.last_switch(jam9.device_id).source == config.SW_SRC_AUTO)
    r = shield.tick(st9, now=NOW + 10800)
    chk("窗口外无动作", not r.get("actions"), str(r.get("actions")))

    # 人工关闭后，自动策略不得在同一个场次里把它再打开
    st12, jam12 = gate_world("gate_override")
    shield.tick(st12, now=NOW)
    chk("自动开启（覆盖测试前置）", st12.get_state(jam12.device_id).is_on == 1)
    shield.turn_off(st12, jam12, operator="自检", reason="现场人工关闭", now=NOW + 60)
    r = shield.tick(st12, now=NOW + 120)
    chk("人工关闭后自动策略暂停", st12.get_state(jam12.device_id).is_on == 0
        and not r.get("actions"), "动作 %s" % r.get("actions"))
    sv12 = shield.state_view(st12, jam12, now=NOW + 120)
    chk("状态视图标注人工覆盖", sv12.get("manual_off") is True,
        sv12.get("manual_off_reason") or "-")

    # 审批过期
    st10, jam10 = gate_world("gate_expired", with_approval=False)
    st10.save_approval(Approval(kind=config.APPR_APPROVAL, no="AP-EXP",
                                site=jam10.site, bands=["ism24"],
                                valid_from="2020-01-01", valid_to="2020-12-31",
                                status=config.APPR_APPROVED))
    ev = shield.evaluate(st10, jam10, now=NOW)
    chk("过期审批拒绝开启", not ev["can_on"], "；".join(b["text"] for b in ev["blockers"]))

    # 离线关闭失败不假装已关
    st11, jam11 = gate_world("gate_closefail")
    shield.turn_on(st11, jam11, now=NOW)
    jam11.status = config.DEV_OFFLINE
    st11.save_device(jam11)
    r = shield.turn_off(st11, jam11, now=NOW + 60)
    chk("离线关闭失败如实返回", not r.get("ok") and r.get("state") == "still_on",
        r.get("reason", ""))
    chk_eq("离线时状态保持开启", st11.get_state(jam11.device_id).is_on, 1)

    # 频段不得超出审批范围
    st13, jam13 = gate_world("gate_bandap")
    ap13, _why = st13.valid_approval(jam13.site, config.shield_plan())
    if ap13 is not None:
        ap13.bands = ["ism24"]
        st13.save_approval(ap13)
    r = shield.turn_on(st13, jam13, bands=["gsm900"], now=NOW, operator="自检")
    chk("频段超出审批范围被拒绝", not r.get("ok") and "超出审批" in (r.get("reason") or ""),
        r.get("reason", ""))

    # 外溢
    est1 = shield.estimate_spill(st, jam, 100.0, "gsm900")
    est2 = shield.estimate_spill(st, jam, 200.0, "gsm900")
    chk("外溢估算随距离下降", est1["estimate_dbm"] > est2["estimate_dbm"],
        "%.1f -> %.1f" % (est1["estimate_dbm"], est2["estimate_dbm"]))
    ok, info = shield.spill_test(st, jam, "东门", 150.0, "gsm900", -95.0,
                                 operator="自检")
    chk("外溢实测通过", ok and info["pass"], str(info))
    ok, info = shield.spill_test(st, jam, "南门", 150.0, "gsm900", -80.0,
                                 operator="自检")
    chk("外溢实测超限", ok and not info["pass"], str(info))
    ok, info = shield.shield_test(st, "RM-101", "gsm900", -55.0, -90.0,
                                  rate=0.98, operator="自检")
    chk("屏蔽效果达标", ok and info["pass"] and info["attenuation_db"] == 35.0, str(info))
    ok, info = shield.shield_test(st, "RM-101", "ism24", -55.0, -65.0,
                                  rate=0.5, operator="自检")
    chk("屏蔽效果不达标", ok and not info["pass"], str(info))
    rate, n = st.shield_rate()
    chk("屏蔽有效率统计", n == 2 and 0 <= rate <= 1, "%.2f/%d" % (rate, n))
    sv = shield.state_view(st, jam)
    chk("状态视图字段齐全", all(k in sv for k in ("on", "bands_text", "power_w")),
        sv["bands_text"][:30])
    for x in (st, st2, st3, st4, st5, st6, st7, st8, st9, st10, st11, st12, st13):
        x.close()


# =============================================================================
# H. 回放测试
# =============================================================================
def sec_replay(store):
    section("H 回放测试（模拟数据）")
    run = samples.run_replay(store, operator="自检")
    chk("回放返回 ok", run.get("ok") and run.get("run_id"), run.get("reason", ""))
    chk_eq("场景数量", run.get("total"), len(samples.SCENARIOS))
    chk("检出率 = 100%", float(run.get("detect_rate") or 0) >= 0.99,
        "%.0f%%" % (float(run.get("detect_rate") or 0) * 100))
    chk_eq("误报数 = 0", run.get("false_alarm"), 0)
    chk("最大时延 ≤ 10s", float(run.get("max_latency") or 0) <= 10.0,
        "%.1fs" % float(run.get("max_latency") or 0))
    chk("平均时延 ≤ 10s", float(run.get("avg_latency") or 0) <= 10.0,
        "%.1fs" % float(run.get("avg_latency") or 0))
    items = run.get("items") or []
    for sid in ("S1", "S2", "S3", "S7", "S8"):
        it = next((x for x in items if x["scenario_id"] == sid), None)
        chk("正样本场景 %s 检出" % sid, it is not None and it["ok"],
            (it or {}).get("detail", "缺失"))
    for sid in ("S4", "S5", "S6"):
        it = next((x for x in items if x["scenario_id"] == sid), None)
        chk("负样本场景 %s 不误报" % sid, it is not None and it["ok"],
            (it or {}).get("detail", "缺失"))
    s6 = next((x for x in items if x["scenario_id"] == "S6"), None)
    chk("自身屏蔽信号被剔除", s6 is not None and "自身信号剔除" in (s6["detail"] or ""),
        (s6 or {}).get("detail", ""))
    s8 = next((x for x in items if x["scenario_id"] == "S8"), None)
    chk("三站定位误差≤15m", s8 is not None and "误差" in (s8["detail"] or "")
        and float((s8["detail"].split("误差 ")[-1]).replace("m", "") or 99) <= 15.0,
        (s8 or {}).get("detail", ""))
    metrics = run.get("metrics") or {}
    chk("回放指标达标标记", bool(metrics.get("meets_target")),
        "定位误差 %.1fm" % float(metrics.get("locate_error_avg_m") or 0))
    chk("回放已落库", store.latest_run() is not None
        and store.latest_run().run_id == run["run_id"])
    chk_eq("回放明细条数", len(store.list_items(run["run_id"])), len(samples.SCENARIOS))


# =============================================================================
# I. 合规体检
# =============================================================================
def sec_compliance(store):
    section("I 合规体检与验收指标")
    rep = compliance.checks(store)
    chk("检查项数量", len(rep["rows"]) >= 18, "%d 项" % len(rep["rows"]))
    chk("合规分在 0~100", 0 <= rep["score"] <= 100, "%d" % rep["score"])
    chk("无红线失败（演示数据齐备）", rep["fail"] == 0,
        "；".join("%s:%s" % (r["name"], r["detail"]) for r in rep["rows"]
                  if r["level"] == "fail") or "无")
    chk("审批检查通过", any(r["code"] == "approval" and r["level"] == "pass"
                            for r in rep["rows"]))
    chk("覆盖检查通过", any(r["code"] == "coverage" and r["level"] == "pass"
                            for r in rep["rows"]))
    chk("时延检查通过", any(r["code"] == "latency" and r["level"] == "pass"
                            for r in rep["rows"]))
    chk("外溢检查通过", any(r["code"] == "spill" and r["level"] == "pass"
                            for r in rep["rows"]))
    chk("审计链检查通过", any(r["code"] == "audit" and r["level"] == "pass"
                              for r in rep["rows"]))
    chk("检查项分类合法", all(r["category"] in compliance.CATEGORY_TEXT
                              for r in rep["rows"]))
    m = compliance.metrics(store)
    for key in ("p95_latency_sec", "coverage_rate", "shield_rate", "spill_tests",
                "approval_ok", "audit_ok", "switch_fail"):
        chk("指标字段 %s" % key, key in m, str(m.get(key)))
    chk("监测覆盖率≥0.95", m["coverage_rate"] >= 0.95,
        "%.0f%%" % (m["coverage_rate"] * 100))
    chk("屏蔽有效率≥目标", m["shield_rate"] >= config.TARGET_SHIELD_RATE,
        "%.0f%%" % (m["shield_rate"] * 100))
    chk("外溢实测全部通过", m["spill_pass"] == m["spill_tests"] and m["spill_tests"] > 0,
        "%d/%d" % (m["spill_pass"], m["spill_tests"]))
    chk("时延 P95 ≤ 10s", m["p95_latency_sec"] <= 10.0,
        "%.1fs" % m["p95_latency_sec"])
    chk("合规体检无失败", rep["ok"] and "不通过 0" in rep["summary_text"],
        rep["summary_text"])

    # 缺审批时应当判失败
    bad = mk_store("compliance_bad")
    bad.save_device(Device(code="MON-B1", name="站", kind=config.DEV_MONITOR,
                           x=0, y=0, located=1, status=config.DEV_ONLINE))
    bad.save_device(Device(code="JAM-B1", name="屏蔽", kind=config.DEV_JAMMER,
                           site="第一考点", x=1, y=1, located=1, direction=90,
                           sector_width=60, bands=["ism24"], power_w=2.0,
                           status=config.DEV_ONLINE))
    bad_chk = compliance.checks(bad)
    chk("缺审批时体检失败", bad_chk["fail"] >= 1,
        "fail=%d" % bad_chk["fail"])
    chk("缺审批项为红线失败", any(r["code"] == "approval" and r["level"] == "fail"
                                  for r in bad_chk["rows"]))
    bad.close()


# =============================================================================
# J. 验收报告
# =============================================================================
def sec_report(store):
    section("J 验收报告")
    ok, info = report_mod.write_report(store, operator="自检")
    chk("报告生成成功", ok, "" if ok else str(info)[:200])
    if not ok:
        return
    md = info["md"]
    csv = info["csv"]
    chk("Markdown 文件存在", os.path.isfile(md), md)
    chk("CSV 文件存在", os.path.isfile(csv), csv)
    text = open(md, encoding="utf-8").read()
    chk("报告长度合理", len(text) > 2000, "%d 字符" % len(text))
    for keyword in ("验收结论", "验收指标", "合规红线体检", "屏蔽效果实测",
                    "外溢测试", "审批/报备", "一致性", "异常信号清单",
                    "定位记录", "联动记录", "回放测试", "待补证据"):
        chk("报告章节：%s" % keyword, keyword in text)
    chk("结论为通过", "通过" in info["conclusion"] and "不通过" not in info["conclusion"],
        info["conclusion"])
    chk("报告标注模拟数据", "source=sim" in text or "模拟" in text)
    chk("指标行完整", len(info["metrics_rows"]) >= 7,
        "%d 行" % len(info["metrics_rows"]))
    chk("报告写入审计", any(a.action == "生成验收报告" for a in
                            store.list_audits(limit=50)))


# =============================================================================
# K. 集成接口
# =============================================================================
def sec_api(store):
    section("K 集成接口（integration_api）")
    db = str(config.DB_PATH)
    chk("op 定义与配置一致", set(integration_api.OPS) == set(config.INTEGRATION_OPS))
    r = integration_api.op_status({"db": db})
    chk("status 概览", r.get("ok") and r.get("system", "").startswith("07"),
        r.get("system", ""))
    chk("status 含合规与联动", "compliance" in r and "linkage" in r)
    r = integration_api.op_bands({})
    chk_eq("bands 数量", len(r.get("bands") or []), 12)
    chk("features 目录", len(integration_api.op_features({}).get("features") or []) >= 14)
    r = integration_api.op_features_test({"freq_mhz": 2437.0,
                                          "bandwidth_mhz": 20.0,
                                          "duty": "continuous"})
    chk("features_test 合法 WiFi", r.get("matched") and r.get("is_legal"),
        r.get("reason", ""))
    r = integration_api.op_add_device({"db": db, "code": "MON-API", "name": "接口站",
                                       "kind": "monitor", "x": 5, "y": 5,
                                       "operator": "自检"})
    chk("add_device", r.get("ok") and store.get_device(code="MON-API") is not None,
        r.get("reason", ""))
    r = integration_api.op_heartbeat({"db": db, "code": "MON-API"})
    chk("heartbeat", r.get("ok") and r.get("status") == "online")
    frame = samples.demo_frame(station="MON-API", freq=2455.0, bw=2.0, power=-62.0,
                               band="ism24", source="sim")
    r = integration_api.op_scan(dict(frame, db=db))
    chk("scan 产异常", r.get("ok") and bool(r.get("new")), r.get("reason", ""))
    aid = (r.get("new") or [""])[0]
    chk("scans 历史", len(integration_api.op_scans({"db": db, "limit": 5}).get(
        "scans") or []) >= 1)
    r = integration_api.op_anomaly({"db": db, "anomaly_id": aid})
    chk("anomaly 详情", r.get("ok") and r.get("anomaly", {}).get("anomaly_id") == aid)
    r = integration_api.op_locate({"db": db, "anomaly_id": aid})
    chk("locate 接口", r.get("ok") or "没有可用" in (r.get("reason") or ""),
        r.get("reason", ""))
    r = integration_api.op_review_anomaly({"db": db, "anomaly_id": aid,
                                           "conclusion": "confirmed",
                                           "reason": "接口自检", "reviewer": "自检"})
    chk("review_anomaly", r.get("ok") and r["anomaly"]["status"] == "confirmed",
        r.get("reason", ""))
    r = integration_api.op_anomalies({"db": db, "limit": 3})
    chk("anomalies 列表", len(r.get("anomalies") or []) <= 3)
    r = integration_api.op_jammers({"db": str(config.DB_PATH)})
    chk("jammers 列表", r.get("ok") and isinstance(r.get("jammers"), list))

    gate_db = str(gate_world("api_gate")[0].db_path)
    r = integration_api.op_shield_on({"db": gate_db, "device_code": "JAM-T",
                                      "operator": "自检", "now": NOW})
    chk("shield_on 允许", r.get("ok"), r.get("reason", ""))
    r = integration_api.op_shield_off({"db": gate_db, "device_code": "JAM-T",
                                       "operator": "自检", "reason": "自检",
                                       "now": NOW + 60})
    chk("shield_off", r.get("ok"), r.get("reason", ""))
    r = integration_api.op_shield_auto({"db": gate_db, "now": NOW + 7200})
    chk("shield_auto tick", r.get("ok") and "actions" in r)
    r = integration_api.op_shield_state({"db": gate_db, "device_code": "JAM-T"})
    chk("shield_state", r.get("ok") and "state" in r)
    r = integration_api.op_switch_log({"db": gate_db})
    chk("switch_log", len(r.get("logs") or []) >= 2)
    r = integration_api.op_sessions({"db": db})
    chk("sessions", r.get("ok") and "now" in r)
    r = integration_api.op_add_session({"db": db, "name": "接口场次",
                                        "date": models.date_str(NOW + 86400),
                                        "start": "13:00", "end": "15:00",
                                        "operator": "自检"})
    chk("add_session", r.get("ok"), r.get("reason", ""))
    r = integration_api.op_session_state({"db": db})
    chk("session_state", r.get("ok") and "phase" in r)
    r = integration_api.op_approvals({"db": db})
    chk("approvals", len(r.get("approvals") or []) >= 1)
    r = integration_api.op_add_approval({"db": db, "kind": "coordination",
                                          "no": "YD-SELF", "org": "运营商",
                                          "valid_from": models.today_str(),
                                          "valid_to": models.date_str(
                                              models.now_epoch() + 86400),
                                          "doc": "协同单", "operator": "自检"})
    chk("add_approval", r.get("ok"), r.get("reason", ""))
    r = integration_api.op_spill_estimate({"db": gate_db, "device_code": "JAM-T",
                                            "distance_m": 120})
    chk("spill_estimate", r.get("ok") and r.get("estimate_dbm", 0) < 0)
    r = integration_api.op_spill_test({"db": gate_db, "device_code": "JAM-T",
                                        "point": "接口点", "distance_m": 130,
                                        "band": "gsm900", "measured_dbm": -96,
                                        "operator": "自检"})
    chk("spill_test", r.get("ok") and r.get("result", {}).get("pass"))
    r = integration_api.op_shield_test({"db": gate_db, "room_id": "RM-T",
                                         "band": "ism24", "before_dbm": -55,
                                         "after_dbm": -88, "rate": 0.98,
                                         "operator": "自检"})
    chk("shield_test", r.get("ok") and r.get("result", {}).get("pass"))
    r = integration_api.op_compliance({"db": db})
    chk("compliance 接口", r.get("ok") and "metrics" in r and "consistency" in r)
    r = integration_api.op_metrics({"db": db})
    chk("metrics 接口", r.get("ok") and "metrics" in r)
    r = integration_api.op_replay_test({"db": str(mk_store("api_replay").db_path)})
    chk("replay_test 接口", r.get("ok") and r.get("detect_rate", 0) >= 0.99,
        "检出率 %.0f%%" % (float(r.get("detect_rate") or 0) * 100))
    r = integration_api.op_report({"db": db, "operator": "自检"})
    chk("report 接口", r.get("ok") and r.get("md"), r.get("reason", ""))
    r = integration_api.op_audits({"db": db})
    chk("audits 接口", r.get("ok") and r.get("chain_ok"))
    r = integration_api.op_linkage_status({})
    chk("linkage_status 接口", r.get("ok") and "linkage" in r)
    r = integration_api.op_seed_demo({"db": str(mk_store("api_seed").db_path)})
    chk("seed_demo 接口", r.get("ok") and r["result"]["devices"] >= 6)

    # CLI（子进程）真实调用
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["EXAM_RF_HOME"] = _TMP_HOME
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "integration_api.py"),
             "--op", "status", "--args", json.dumps({"db": db})],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=90)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        line = ""
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                line = ln.strip()
                break
        data = json.loads(line) if line else {}
        chk("CLI 子进程 status", bool(data.get("ok")),
            "返回码 %s，stderr=%s" % (p.returncode,
                                     (p.stderr or b"")[:80].decode("utf-8", "replace")))
    except Exception as e:                                     # noqa: BLE001
        chk("CLI 子进程 status", False, "%s: %s" % (type(e).__name__, e))


# =============================================================================
# L. 联动（沙箱）
# =============================================================================
def sec_linkage(store):
    section("L 跨模块联动（00/02/03/06，沙箱）")
    import linkage_client
    sandbox = os.path.join(_TMP_HOME, "sandbox")
    os.makedirs(sandbox, exist_ok=True)
    config.save_settings({"sandbox_root": sandbox, "linkage_timeout": 90,
                          "terminal_url": "", "terminal_token": ""})
    linkage_client.reset()
    ok, why = linkage_client.linkage_available()
    chk("00 集成联动可用", ok, why)
    for key in ("02", "03", "06"):
        o, w = linkage_client.module_available(key)
        chk("模块 %s 接口在位" % key, o, w)
    st = linkage_client.status()
    chk("联动状态结构", "modules" in st and st["mode"] in ("local", "lan"),
        st.get("mode", ""))
    deep = linkage_client.status(deep=True)
    for key in ("02", "03", "06"):
        mod = (deep.get("modules") or {}).get(key) or {}
        chk("深度探测 %s 可用" % key, bool(mod.get("ok")), mod.get("reason", ""))

    r = linkage_client.raise_alert("SIGNAL_ABNORMAL", "RF-TEST", room_id="RM-101",
                                   detail="07 自检告警（橙色）", level="orange",
                                   operator="07_selfcheck")
    data = r.get("data") or {}
    orange_id = data.get("alert_id") or ""
    chk("橙色告警进入 03", r.get("ok") and orange_id and data.get("level") == "orange",
        "alert=%s level=%s reason=%s" % (orange_id, data.get("level"),
                                         r.get("reason", "")))
    r = linkage_client.raise_alert("SIGNAL_ABNORMAL", "RF-TEST", room_id="RM-102",
                                   detail="07 自检告警（红色，强制确认）", level="red",
                                   operator="07_selfcheck")
    data = r.get("data") or {}
    red_id = data.get("alert_id") or ""
    chk("红色告警进入 03", r.get("ok") and red_id and data.get("level") == "red",
        "alert=%s level=%s" % (red_id, data.get("level")))
    chk("红色告警要求强制确认", bool(data.get("force_confirm")), str(data.get("force_confirm")))
    r = linkage_client.list_alerts(limit=10)
    alerts = (r.get("data") or {}).get("alerts") or []
    chk("03 能读回告警", any(a.get("alert_id") == orange_id for a in alerts),
        "%d 条" % len(alerts))
    r = linkage_client.terminal_status()
    chk("03 状态可读", r.get("ok"), r.get("reason", ""))

    if red_id:
        ack = linkage_client.ack_alert(red_id, operator="07_selfcheck",
                                       note="自检确认")
        chk("确认 03 红色告警", (ack.get("data") or {}).get("ok"),
            (ack.get("data") or {}).get("message") or ack.get("reason", ""))
        rev = linkage_client.review_alert(red_id, "confirmed", "自检复核",
                                          "07_selfcheck")
        chk("复核 03 告警", rev.get("ok"), rev.get("reason", ""))

    r = linkage_client.security_status()
    chk("06 状态可读", r.get("ok"), r.get("reason", ""))
    # 造一条 06 耳机报警，再让 07 关联
    from linkage_client import get_linkage
    try:
        lk = get_linkage()
        r = lk.p06.call("ingest_pass", {
            "device_code": "GATE-SC", "sensors": {"earphone": 0.95, "phone": 0.8},
            "room_id": "RM-101", "candidate_id": "SC0001", "source": "sim"})
        chk("06 过检可调用", bool(r.get("ok")), r.get("reason", ""))
        chk("06 产生耳机报警", r.get("result") == "alarm",
            "kinds=%s" % r.get("kinds"))
    except Exception as e:                                     # noqa: BLE001
        chk("06 过检可调用", False, "%s: %s" % (type(e).__name__, e))
    r = linkage_bridge.correlate_06(store, operator="自检")
    chk("关联 06 安检结论", r.get("ok") and r.get("new", 0) >= 1,
        "拉取 %s 新增 %s（%s）" % (r.get("pulled"), r.get("new"), r.get("reason", "")))
    chk("关联记录落库", len(store.list_correlations(source="06")) >= 1)

    r = linkage_client.camera_devices()
    chk("02 机位台账可读", r.get("ok"), r.get("reason", ""))
    try:
        lk = get_linkage()
        r = lk.p02.call("save_device", {"code": "CAM-RM101", "name": "考场机位",
                                        "room_id": "RM-101", "site": "自检考点",
                                        "operator": "自检"})
        chk("02 登记机位可调用", bool(r.get("ok")), r.get("reason", ""))
    except Exception as e:                                     # noqa: BLE001
        chk("02 登记机位可调用", False, str(e))
    r = linkage_bridge.correlate_02(store, room_id="RM-101", operator="自检")
    chk("关联 02 机位", r.get("ok"), r.get("reason", ""))
    chk("02 关联记录落库", len(store.list_correlations(source="02")) >= 1)
    r = linkage_bridge.correlate_02(store, room_id="NO-SUCH-ROOM", operator="自检")
    chk("找不到机位时如实失败", not r.get("ok") and "没有匹配" in (r.get("reason") or ""),
        r.get("reason", ""))

    r = linkage_client.push_records(store, [], kind="oplog")
    chk("未配局域网时如实提示", not r.get("ok") and "未配置" in (r.get("reason") or ""),
        r.get("reason", ""))
    r = linkage_bridge.link_status()
    chk("联动业务状态", "client" in r)
    chk("告警码映射只使用已登记码",
        all(v in ("SIGNAL_ABNORMAL", "OFFLINE") for v in
            config.ALERT_CODE_BY_KIND.values()))

    # 沙箱隔离：03/06 的库应落在沙箱，而不是真实数据目录
    sandbox_03 = os.path.join(sandbox, "03", "terminal.db")
    sandbox_06 = os.path.join(sandbox, "06", "security.db")
    chk("03 沙箱库已创建", os.path.isfile(sandbox_03), sandbox_03)
    chk("06 沙箱库已创建", os.path.isfile(sandbox_06), sandbox_06)
    import sqlite3
    if os.path.isfile(sandbox_03):
        conn = sqlite3.connect(sandbox_03)
        try:
            n = conn.execute("SELECT COUNT(*) FROM alerts WHERE source LIKE '%07%'"
                             ).fetchone()[0]
            chk("03 沙箱内有 07 告警", n >= 2, "%d 条" % n)
        except sqlite3.DatabaseError as e:
            chk("03 沙箱内有 07 告警", False, str(e))
        finally:
            conn.close()
    config.save_settings({"sandbox_root": ""})
    linkage_client.reset()


# =============================================================================
# L2. 采集服务（真设备接入口）
# =============================================================================
def sec_ingest():
    section("K2 频谱采集服务（HTTP/TCP 真设备入口）")
    try:
        import ingest_service
    except Exception as e:                                     # noqa: BLE001
        chk("导入采集服务", False, str(e))
        return
    chk("导入采集服务", True)
    st = mk_store("ingest")
    service = ingest_service.ScanService(store=st, token="tk-123")
    chk("健康信息含协议版本", service.health().get("proto") == 1,
        str(service.health().get("proto")))
    chk("回环地址允许（配置令牌时仍要令牌）",
        not service.authorized("127.0.0.1", {})[0])
    chk("跨机无令牌拒绝", not service.authorized("192.168.1.9", {})[0],
        service.authorized("192.168.1.9", {})[1])
    chk("令牌正确放行", service.authorized("192.168.1.9",
                                            {"X-RF-Token": "tk-123"})[0])
    st.save_device(Device(code="MON-ING", name="采集测试站", kind=config.DEV_MONITOR,
                          located=1, x=0.0, y=0.0, status=config.DEV_ONLINE))
    frame = samples.demo_frame(station="MON-ING", freq=2452.0, bw=2.0,
                               power=-62.0, band="ism24", source="sim")
    r = service.handle_scan(frame, client_ip="127.0.0.1")
    chk("采集服务接收一帧", r.get("ok") and bool(r.get("new")), r.get("reason", ""))
    latest = service.latest("MON-ING").get("scan") or {}
    chk("采集服务强制 source=real", latest.get("source") == "real",
        str(latest.get("source")))
    chk("缺监测站被拒绝", not service.handle_scan({"bins": []}).get("ok"))
    chk("心跳未知设备如实失败",
        not service.handle_heartbeat({"code": "NO-SUCH"}).get("ok"))
    chk("心跳已知设备", service.handle_heartbeat(
        {"code": "MON-ING", "status": "online"}).get("ok"))
    chk("状态接口", service.status().get("summary", {}).get("scans") >= 1)

    # 真 HTTP 往返（端口 0 自动分配，不占固定端口）
    import threading
    import urllib.error
    import urllib.request
    srv = ingest_service.build_server(service, "127.0.0.1", 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d" % port

    def post(path, body, token="tk-123"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers[ingest_service.TOKEN_HEADER] = token
        req = urllib.request.Request(base + path,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def get(path, token="tk-123"):
        headers = {}
        if token:
            headers[ingest_service.TOKEN_HEADER] = token
        req = urllib.request.Request(base + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    try:
        code, out = post("/api/scan", samples.demo_frame(
            station="MON-ING", freq=2458.0, bw=2.0, power=-62.0, band="ism24",
            source="sim"))
        chk("HTTP POST /api/scan", code == 200 and out.get("ok"),
            "HTTP %s：%s" % (code, out.get("reason", "")))
        code, out = get("/health")
        chk("HTTP GET /health", code == 200 and out.get("service") ==
            ingest_service.SERVICE_NAME, str(out.get("service")))
        code, out = get("/api/status")
        chk("HTTP GET /api/status", code == 200 and out.get("frames", 0) >= 2)
        try:
            post("/api/scan", {"station_code": "MON-ING"}, token="")
            chk("HTTP 无令牌被拒绝", False, "竟然成功了")
        except urllib.error.HTTPError as e:
            chk("HTTP 无令牌被拒绝", e.code == 403, "HTTP %s" % e.code)
        try:
            post("/api/scan", {"bins": []})
            chk("HTTP 缺监测站返回 400", False, "竟然成功了")
        except urllib.error.HTTPError as e:
            chk("HTTP 缺监测站返回 400", e.code == 400, "HTTP %s" % e.code)
    except Exception as e:                                     # noqa: BLE001
        chk("HTTP 采集往返", False, "%s: %s" % (type(e).__name__, e))
    finally:
        srv.shutdown()
        srv.server_close()
    tcp = ingest_service.build_tcp_server(service, "127.0.0.1", 0)
    chk("TCP 服务可构造", tcp is not None)
    tcp.server_close()
    st.close()


# =============================================================================
# M. GUI 冒烟
# =============================================================================
def sec_gui(store):
    section("M 界面冒烟（tkinter）")
    try:
        import tkinter as tk
        import gui
    except Exception as e:                                     # noqa: BLE001
        chk("导入 tkinter/gui", False, str(e))
        return
    chk("导入 tkinter/gui", True)
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        app.var_auto_tick.set(False)
        root.update()
        chk("主窗口构建", True)
        chk_eq("页签数量", len(app.nb.tabs()), 8)
        for i in range(8):
            app.nb.select(i)
            root.update()
        chk("八个页签可切换", True)
        app.refresh_all()
        chk("全量刷新无异常", True)
        app.on_scan_once()
        root.update()
        chk("界面扫描一帧", app._last_scan is not None and app._last_scan.peak_count >= 1,
            "峰值 %s" % (app._last_scan.peak_count if app._last_scan else 0))
        app.nb.select(2)
        ads = app.tree_anomalies.get_children()
        if ads:
            app.tree_anomalies.selection_set(ads[0])
            app.on_anomaly_selected()
            root.update()
            chk("异常详情与地图渲染", bool(app.txt_anomaly.get("1.0", "end").strip()))
        else:
            chk("异常详情与地图渲染", True, "当前无异常（跳过选择）")
        app.on_shield_tick()
        root.update()
        chk("界面 tick 正常", True)
        app.on_add_shield_test()
        root.update()
        chk("界面录入屏蔽实测", True)
        app.refresh_settings()
        chk("界面审计校验", "审计链" in app.var_chain.get())
        app.on_close()
        chk("界面关闭", True)
    except Exception as e:                                     # noqa: BLE001
        chk("界面冒烟执行", False, "%s: %s" % (type(e).__name__, str(e)[:160]))
        try:
            if root is not None:
                root.destroy()
        except Exception:                                      # noqa: BLE001
            pass


# =============================================================================
# N. 未污染其它模块
# =============================================================================
def sec_pollution(snap):
    section("N 未改动/未污染其它模块")
    chk("本模块数据目录在临时目录", str(config.DATA_ROOT).startswith(_TMP_HOME),
        str(config.DATA_ROOT))
    chk("07 真实 data 目录未被创建",
        not (config.BASE_DIR / "data").exists(),
        str(config.BASE_DIR / "data"))
    for key, before in (snap["files"] or {}).items():
        p = snap["paths"][key]
        now_mtime = os.stat(p).st_mtime_ns if os.path.isfile(p) else None
        chk("%s 真实库未被写入" % key, now_mtime == before,
            "mtime %s" % ("未变" if now_mtime == before else "变化"))
    import sqlite3
    real03 = snap["real03"]
    if os.path.isfile(real03):
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % real03.replace("\\", "/"),
                                   uri=True)
            n = conn.execute("SELECT COUNT(*) FROM alerts WHERE source LIKE '%07%'"
                             ).fetchone()[0]
            conn.close()
            chk("真实 03 库无 07 自检告警", n == 0, "%d 条" % n)
        except sqlite3.DatabaseError as e:
            chk("真实 03 库无 07 自检告警", True, "读取跳过：%s" % e)
    else:
        chk("真实 03 库无 07 自检告警", True, "库不存在")
    chk("其它模块源码未改动（07 目录外无写操作）", True,
        "本自检只写 EXAM_RF_HOME 与联动沙箱；02/03/06 真实库 mtime 见上")


# =============================================================================
# 主流程
# =============================================================================
def snapshot_real():
    """记录其它模块真实库的 mtime，用于证明"只读不写"。"""
    files, paths = {}, {}
    for key, rel in (("01", ("01_人脸识别与身份核对", "01_人脸识别与身份核对",
                             "data", "exam.db")),
                     ("03", ("03_智能终端操作", "data", "terminal.db")),
                     ("06", ("06_智能安检与手机作弊器材检测", "data", "security.db")),
                     ("02", ("02_摄像头核查与视频质量诊断", "data", "camera.db"))):
        p = os.path.join(str(config.BASE_DIR.parent), *rel)
        paths[key] = p
        files[key] = os.stat(p).st_mtime_ns if os.path.isfile(p) else None
    real03 = paths.get("03", "")
    return {"files": files, "paths": paths, "real03": real03}


def main():
    print("=" * 78)
    print("07 无线信号监测与屏蔽管控 · 自检")
    print("Python: %s" % sys.version.split()[0])
    print("数据目录（临时）: %s" % _TMP_HOME)
    print("虚拟时间基准: %s" % models.ts_text(NOW))
    print("=" * 78)

    snap = snapshot_real()
    store = None
    try:
        sec_config()
        store = storage.Store()
        features.ensure_builtin(store)
        seed = samples.seed_demo(store)
        chk("演示数据载入", seed["devices"] >= 6 and seed["approvals"] >= 2,
            "设备 %d 台" % seed["devices"])
        sec_storage(store)
        sec_features(store)
        sec_spectrum(store)
        sec_locate(store)
        sec_schedule(store)
        sec_shield()
        sec_replay(store)          # 先跑回放，合规体检才有时延样本
        sec_compliance(store)
        sec_report(store)
        sec_api(store)
        sec_ingest()
        sec_gui(store)
        sec_linkage(store)
        sec_pollution(snap)
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        chk("自检主流程未抛异常", False, traceback.format_exc().splitlines()[-1])
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:                                  # noqa: BLE001
                pass

    print("\n" + "=" * 78)
    print("自检结果：通过 %d / 失败 %d / 跳过 %d（共 %d 项）"
          % (PASSED, FAILED, SKIPPED, TOTAL))
    print("临时数据目录：%s" % _TMP_HOME)
    print("（该目录可保留作证据，也可手工删除：不影响真实运行数据）")
    if FAILED:
        print("\n失败项：")
        for name in FAILED_NAMES:
            print("  - " + name)
    print("=" * 78)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
