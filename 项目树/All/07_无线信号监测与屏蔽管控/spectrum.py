# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 频谱分析与异常发现

处理链（每一环都可单独测）：

  ① 归一化   把"频谱仪推来的一行 JSON"变成统一帧（bins / peaks / 单频点都收）
  ② 噪声底   用中位数估计噪声底、MAD 估计噪声抖动（对突发干扰不敏感）
  ③ 峰值检测 高于"噪声底 + 门限"的连续 bin 归并成一个峰，算中心频率/带宽/信噪比
  ④ 占空推断 用时间窗内"出现过该峰的帧数 / 总帧数"推断连续/突发/跳频
  ⑤ 特征匹配 调 features.match：命中作弊特征 -> 告警；命中合法特征 -> 只记台账
  ⑥ 自身屏蔽 屏蔽开启时带内噪声会整体抬高，必须把自己发的信号认出来并剔除，
             否则一开屏蔽就报"宽带异常"（这是真实系统里最常见的自激误报）
  ⑦ 合并告警 同站同频在合并窗内只保留一条，持续出现则升级，不刷屏

发现时延 `latency_sec = 首次出现该信号的帧时间 -> 建立异常的时间`，口径与验收一致（≤10s）。
"""
import statistics

import config
import features as features_mod
import models
from models import Anomaly, Scan


# =============================================================================
# 数学小工具（不引 numpy，保证零依赖）
# =============================================================================
def median(values):
    vals = [float(v) for v in (values or [])]
    if not vals:
        return 0.0
    return float(statistics.median(vals))


def mad_sigma(values, med=None):
    """MAD 稳健标准差：σ ≈ 1.4826 × median(|x - median|)。"""
    vals = [float(v) for v in (values or [])]
    if not vals:
        return 0.0
    m = median(vals) if med is None else float(med)
    return 1.4826 * median([abs(v - m) for v in vals])


def noise_floor(bins):
    """噪声底：优先中位数；bin 太少时退回最小值。返回 (floor, sigma)。"""
    powers = [float(p) for _f, p in (bins or [])]
    if not powers:
        return -100.0, 0.0
    if len(powers) < 5:
        return float(min(powers)), 0.0
    m = median(powers)
    return float(m), mad_sigma(powers, m)


def bin_step(bins):
    freqs = sorted(float(f) for f, _p in (bins or []))
    if len(freqs) < 2:
        return 0.05
    diffs = [b - a for a, b in zip(freqs, freqs[1:]) if (b - a) > 1e-9]
    if not diffs:
        return 0.05
    return float(median(diffs))


def detect_peaks(bins, floor, margin):
    """
    从 (freq, power) 序列里找峰。

    返回 [{freq_mhz, power_dbm, bandwidth_mhz, snr_db, bins}]，按功率从高到低排序。
    只做"高于门限的连续段归并"，不做平滑 —— 频谱仪已经做过 RBW 平滑，
    这里再平滑会把窄带的隐形耳机信号抹掉。
    """
    rows = sorted(((float(f), float(p)) for f, p in (bins or [])), key=lambda x: x[0])
    if not rows:
        return []
    step = bin_step(rows)
    thr = float(floor) + float(margin)
    runs = []
    cur = []
    for f, p in rows:
        if p >= thr:
            cur.append((f, p))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        freqs = [f for f, _p in run]
        powers = [p for _f, p in run]
        maxp = max(powers)
        # 中心频率用"高于噪声底的功率"做加权重心：平顶峰也不会偏到边的第一个 bin，
        # 窄带信号被 RBW 展宽后也能回到真实中心。
        weights = [max(0.01, p - float(floor)) for p in powers]
        sw = sum(weights)
        center = (sum(f * w for f, w in zip(freqs, weights)) / sw) if sw > 0 \
            else freqs[powers.index(maxp)]
        # 占空/带宽用 bin 跨度；单 bin 时给一个 RBW 宽度
        bw = (freqs[-1] - freqs[0]) + step if len(freqs) > 1 else step
        out.append({"freq_mhz": round(center, 6),
                    "power_dbm": round(maxp, 2),
                    "bandwidth_mhz": round(max(step, bw), 6),
                    "snr_db": round(maxp - float(floor), 2),
                    "bins": len(freqs)})
    out.sort(key=lambda p: p["power_dbm"], reverse=True)
    return out


def occupancy(bins, floor, margin):
    """占用率：高于门限的 bin 占比（0~1）。"""
    if not bins:
        return 0.0
    thr = float(floor) + float(margin)
    n = sum(1 for _f, p in bins if float(p) >= thr)
    return round(n / float(len(bins)), 4)


# =============================================================================
# 归一化
# =============================================================================
def normalize_frame(payload, cfg=None):
    """
    把各种设备/人工/模拟输入统一成一帧。

    支持的输入（三选一）：
      {"peaks": [{"freq_mhz":2442,"power_dbm":-62,"bandwidth_mhz":2,"duty":"burst"}]}
      {"bins": [[2400.0,-101.2],[2402.0,-99.8], ...]}
      {"freq_mhz": 433.5, "power_dbm": -78, "bandwidth_mhz": 0.025}
    公共字段：station / station_code、noise_floor_dbm、band、ts_epoch / ts、source。
    返回 (frame_dict, error)。
    """
    cfg = cfg or config.load_settings()
    thresholds = config.thresholds(cfg)
    if not isinstance(payload, dict):
        return None, "输入必须是 JSON 对象"
    station = str(payload.get("station_code") or payload.get("station")
                  or payload.get("device_code") or "").strip()
    if not station:
        return None, "缺少 station_code（监测站编码）"
    ts_epoch = models.to_float(payload.get("ts_epoch"), 0.0)
    if not ts_epoch:
        ts_epoch = models.now_epoch()
    source = str(payload.get("source") or "manual")
    if source in ("sim", "real", "manual"):
        pass
    else:
        source = "manual"

    bins = payload.get("bins") or payload.get("spectrum") or []
    norm_bins = []
    for item in bins:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            norm_bins.append((models.to_float(item[0]), models.to_float(item[1])))
        elif isinstance(item, dict):
            norm_bins.append((models.to_float(item.get("freq_mhz")),
                              models.to_float(item.get("power_dbm"))))
    noise = payload.get("noise_floor_dbm", payload.get("noise_floor"))
    if norm_bins:
        est, _sig = noise_floor(norm_bins)
        noise = models.to_float(noise, est) if noise is not None else est
        peaks = detect_peaks(norm_bins, noise, thresholds["noise_margin_db"])
        occ = occupancy(norm_bins, noise, thresholds["noise_margin_db"])
    else:
        noise = models.to_float(noise, -100.0)
        occ = 0.0
        peaks = []

    raw_peaks = payload.get("peaks") or payload.get("signals") or []
    if raw_peaks:
        peaks = []
        for p in raw_peaks:
            if not isinstance(p, dict):
                continue
            freq = models.to_float(p.get("freq_mhz", p.get("freq")))
            if freq <= 0:
                continue
            bw = p.get("bandwidth_mhz", p.get("bw_mhz", p.get("bandwidth")))
            bw = models.to_float(bw, 0.05) or 0.05
            power = models.to_float(p.get("power_dbm", p.get("power")), noise)
            peaks.append({
                "freq_mhz": freq, "power_dbm": power,
                "bandwidth_mhz": bw,
                "snr_db": round(power - float(noise), 2),
                "duty": str(p.get("duty") or ""),
                "bins": models.to_int(p.get("bins"), 1),
            })
        peaks.sort(key=lambda x: x["power_dbm"], reverse=True)
    elif not norm_bins and payload.get("freq_mhz") is not None:
        freq = models.to_float(payload.get("freq_mhz"))
        power = models.to_float(payload.get("power_dbm"), noise)
        bw = models.to_float(payload.get("bandwidth_mhz"), 0.05) or 0.05
        peaks = [{"freq_mhz": freq, "power_dbm": power, "bandwidth_mhz": bw,
                  "snr_db": round(power - float(noise), 2),
                  "duty": str(payload.get("duty") or ""), "bins": 1}]

    # 频段：显式给了就用（校验存在），否则按每个峰的频率反查
    band = str(payload.get("band") or "").strip()
    if band and band not in config.BAND_INDEX:
        return None, "未知频段：%s" % band
    if not band and peaks:
        band = config.band_of(peaks[0]["freq_mhz"])

    return {"station_code": station, "band": band, "ts_epoch": ts_epoch,
            "noise_floor_dbm": round(float(noise), 2), "peaks": peaks,
            "occupancy": occ, "source": source,
            "room_id": str(payload.get("room_id") or ""),
            "note": str(payload.get("note") or "")}, ""


# =============================================================================
# 占空推断（用时间窗内的出现比例）
# =============================================================================
def infer_duty(store, station_code, band, freq_mhz, ts_epoch, cfg=None,
               explicit=""):
    """
    连续/突发/跳频推断：
      · 设备显式给了 duty 就用它（真设备知道自己的解调方式）
      · 否则看窗口内"出现过该峰的帧数 / 总帧数"：≥0.8 连续，≥0.25 突发，其余跳频
    """
    if explicit in ("burst", "continuous", "hopping"):
        return explicit
    thresholds = config.thresholds(cfg)
    win = float(thresholds["sustain_window_sec"])
    tol = float(thresholds["dedup_freq_mhz"])
    rows = store.list_scans(station_code=station_code, band=band or None,
                            since_epoch=ts_epoch - win, limit=500)
    frames = 0
    seen = 0
    for sc in rows:
        if sc.ts_epoch > ts_epoch + 0.001:
            continue
        frames += 1
        for pk in (sc.peaks or []):
            if abs(models.to_float(pk.get("freq_mhz")) - float(freq_mhz)) <= tol:
                seen += 1
                break
    # 当前帧还没有入库，算上它
    frames += 1
    seen += 1
    ratio = seen / float(max(1, frames))
    if ratio >= 0.8:
        return "continuous"
    if ratio >= 0.25:
        return "burst"
    return "hopping"


# =============================================================================
# 分类
# =============================================================================
def classify(store, peak, band, duty, cfg=None):
    """
    把一个峰判成：合法（跳过）/ 作弊 / 持续 / 宽带 / 非法频段 / 未识别。

    返回 dict：{skip, is_legal, kind, severity, score, confidence, feature_code,
               feature_name, reason}
    """
    thresholds = config.thresholds(cfg)
    freq = models.to_float(peak.get("freq_mhz"))
    bw = models.to_float(peak.get("bandwidth_mhz"), 0.05)
    snr = models.to_float(peak.get("snr_db"))
    m = features_mod.match_peak(store, {"freq_mhz": freq, "bandwidth_mhz": bw,
                                        "duty": duty})
    legal_band = bool(band)
    base = {"skip": False, "is_legal": False, "kind": config.KIND_UNKNOWN,
            "severity": "watch", "score": round(m["score"], 3),
            "confidence": m["confidence"], "feature_code": "",
            "feature_name": "", "reason": m["reason"]}

    if m["matched"]:
        f = m["feature"]
        base["feature_code"] = f.feature_code
        base["feature_name"] = f.name
        if m["is_legal"]:
            base.update({"skip": True, "is_legal": True, "kind": config.KIND_UNKNOWN,
                         "severity": "watch", "confidence": m["confidence"]})
            return base
        # 作弊特征：窄带连续 -> 隐形耳机；其余 -> 数传突发/图传
        narrow = float(f.bandwidth_mhz) <= 0.5
        if narrow and duty == "continuous":
            kind = config.KIND_CHEAT_NARROW
        elif float(f.bandwidth_mhz) >= float(thresholds["wideband_mhz"]):
            kind = config.KIND_WIDEBAND
        else:
            kind = config.KIND_CHEAT_BURST
        severity = "critical" if m["confidence"] >= 0.70 else "warn"
        base.update({"kind": kind, "severity": severity, "is_legal": False})
        return base

    # 未命中特征：按频段合法性/带宽/是否持续决定关注级别
    if not legal_band:
        base.update({"kind": config.KIND_ILLEGAL, "severity": "warn"})
        base["reason"] += "；频率不在已知合法频段内"
    elif bw >= float(thresholds["wideband_mhz"]):
        base.update({"kind": config.KIND_WIDEBAND, "severity": "warn"})
    elif snr < float(thresholds["min_snr_db"]):
        base.update({"kind": config.KIND_UNKNOWN, "severity": "watch"})
    elif duty == "continuous":
        base.update({"kind": config.KIND_SUSTAIN, "severity": "warn"})
    return base


# =============================================================================
# 屏蔽带内自激识别
# =============================================================================
def shield_active_bands(store, site=""):
    """当前有屏蔽器在发射的频段集合（按站点过滤；站点为空则不过滤）。"""
    out = set()
    try:
        import shield
        for st in store.on_states():
            dev = store.get_device(device_id=st.device_id)
            if dev is None:
                continue
            if site and dev.site and dev.site != site:
                continue
            out.update(st.bands or dev.bands or [])
    except Exception:                                            # noqa: BLE001
        return out
    return out


def is_self_signal(band, snr, bw, active_bands, cfg=None):
    """
    屏蔽开启时，带内宽带噪声多半是我们自己发的。判据：
      · 该频段正在屏蔽；且
      · 信噪比没有明显超过"屏蔽抬高量"；或带宽很宽（宽带压制噪声）
    只要命中就按自身信号处理（只记台账，不告警）。
    如果屏蔽开着还能测到**强窄带**信号，那是真的可疑（可能绕射/定向），不豁免。
    """
    if band not in active_bands:
        return False
    thresholds = config.thresholds(cfg)
    lift = float(thresholds["self_suppress_db"])
    wide = float(bw) >= float(thresholds["wideband_mhz"])
    # 屏蔽开启时带内噪声整体抬高：信噪比没有明显超过抬高量的，一律按自身信号处理。
    # （强窄带信号若仍高出很多，不会被豁免 —— 那是真的可疑信号，仍要告警）
    if float(snr) <= float(thresholds["min_snr_db"]) + lift:
        return True
    if wide and float(snr) <= float(thresholds["min_snr_db"]) + 2 * lift:
        return True
    return False


# =============================================================================
# 主入口：吃一帧，产出异常
# =============================================================================
def ingest_scan(store, payload, source=None, cfg=None, now=None, detect_epoch=None):
    """
    处理一帧扫描并落库/落异常。

    `now` 是**这一帧的采样时刻**（模拟回放时是过去的时刻）；
    `detect_epoch` 是**真正处理出告警的时刻**（默认取当前时间）。
    发现时延 = detect_epoch - 该信号首次出现的帧时刻，口径与验收目标（≤10s）一致。

    返回 {ok, scan_id, peaks, new, merged, legal, self_suppressed, latency_max}
    失败返回 {ok: False, reason}
    """
    cfg = cfg or config.load_settings()
    thresholds = config.thresholds(cfg)
    frame, err = normalize_frame(payload, cfg)
    if err:
        return {"ok": False, "reason": err}
    if source:
        frame["source"] = source
    ts_epoch = float(now) if now is not None else frame["ts_epoch"]
    detect_epoch = float(detect_epoch) if detect_epoch is not None else models.now_epoch()
    frame["ts_epoch"] = ts_epoch
    station = store.get_device(code=frame["station_code"])
    site = station.site if station else ""
    room_id = frame["room_id"] or (station.room_id if station else "")
    active = shield_active_bands(store, site=site) if site else shield_active_bands(store)

    scan = Scan(station_code=frame["station_code"], band=frame["band"],
                ts_epoch=ts_epoch, noise_floor_dbm=frame["noise_floor_dbm"],
                peak_count=len(frame["peaks"]), occupancy=frame["occupancy"],
                source=frame["source"], peaks=frame["peaks"], raw=payload)
    store.save_scan(scan)

    new_ids, merged_ids = [], []
    legal = self_suppressed = 0
    latency_max = 0.0
    new_kinds = []
    for peak in frame["peaks"]:
        freq = models.to_float(peak.get("freq_mhz"))
        bw = models.to_float(peak.get("bandwidth_mhz"), 0.05)
        snr = models.to_float(peak.get("snr_db"))
        band = config.band_of(freq) or frame["band"]
        if is_self_signal(band, snr, bw, active, cfg):
            self_suppressed += 1
            continue
        duty = infer_duty(store, frame["station_code"], band, freq, ts_epoch, cfg,
                          explicit=str(peak.get("duty") or ""))
        cls = classify(store, peak, band, duty, cfg)
        if cls["skip"]:
            legal += 1
            continue
        dup = store.find_recent_duplicate(
            frame["station_code"], band, freq,
            thresholds["dedup_freq_mhz"], thresholds["dedup_window_sec"], now=ts_epoch)
        if dup is not None:
            dup.last_seen_epoch = max(dup.last_seen_epoch, ts_epoch)
            dup.seen_count = int(dup.seen_count or 0) + 1
            dup.peak_dbm = max(dup.peak_dbm, models.to_float(peak.get("power_dbm")))
            dup.snr_db = max(dup.snr_db, snr)
            if dup.seen_count >= int(thresholds["sustain_frames"]):
                dup.severity = models.max_severity(dup.severity, "warn")
            note = "；已持续 %d 帧/%.0fs" % (
                dup.seen_count, max(0.0, dup.last_seen_epoch - dup.first_seen_epoch))
            if note.strip("；") not in (dup.detail or ""):
                dup.detail = (dup.detail or "") + note
            store.save_anomaly(dup)
            merged_ids.append(dup.anomaly_id)
            latency_max = max(latency_max, float(dup.latency_sec or 0))
            continue

        latency = max(0.0, detect_epoch - ts_epoch)
        anomaly = Anomaly(
            station_code=frame["station_code"], room_id=room_id,
            kind=cls["kind"], band=band, freq_mhz=freq, bandwidth_mhz=bw,
            peak_dbm=models.to_float(peak.get("power_dbm")),
            noise_floor_dbm=frame["noise_floor_dbm"], snr_db=snr,
            severity=cls["severity"], score=cls["score"],
            feature_code=cls["feature_code"], feature_name=cls["feature_name"],
            legal=1 if cls["is_legal"] else 0, status=config.ST_OPEN,
            detected_epoch=detect_epoch, first_seen_epoch=ts_epoch,
            last_seen_epoch=ts_epoch, seen_count=1, latency_sec=round(latency, 3),
            detail="%s ｜带宽 %.3f MHz ｜峰值 %.1f dBm ｜信噪比 %.1f dB ｜%s"
                   % (config.KIND_TEXT.get(cls["kind"], cls["kind"]), bw,
                      models.to_float(peak.get("power_dbm")), snr, cls["reason"]),
            source=frame["source"], created_at=models.ts_text(detect_epoch))
        store.save_anomaly(anomaly)
        new_ids.append(anomaly.anomaly_id)
        new_kinds.append(cls["kind"])
        latency_max = max(latency_max, latency)

    if new_ids:
        store.log("发现异常", "站 %s 新增 %d 条（%s）" % (
            frame["station_code"], len(new_ids),
            "、".join(config.KIND_TEXT.get(k, k) for k in new_kinds)), "spectrum")
    return {"ok": True, "scan_id": scan.scan_id, "band": frame["band"],
            "peaks": len(frame["peaks"]), "new": new_ids, "merged": merged_ids,
            "legal": legal, "self_suppressed": self_suppressed,
            "latency_max": round(latency_max, 3),
            "noise_floor_dbm": frame["noise_floor_dbm"],
            "occupancy": frame["occupancy"]}


def analyze_peaks(store, peaks, station_code, band="", ts_epoch=None, source="manual"):
    """便利入口：只有峰、没有整帧时走这里（自检/人工录入常用）。"""
    return ingest_scan(store, {"station_code": station_code, "band": band,
                               "ts_epoch": ts_epoch or models.now_epoch(),
                               "peaks": peaks, "source": source})


def describe():
    t = config.thresholds()
    return {"检测门限": "噪声底 + %.1f dB" % t["noise_margin_db"],
            "持续判定": "%d 帧 / %.0f 秒" % (t["sustain_frames"], t["sustain_window_sec"]),
            "同频合并": "±%.2f MHz / %.0f 秒" % (t["dedup_freq_mhz"],
                                                t["dedup_window_sec"]),
            "宽带判定": "≥ %.0f MHz" % t["wideband_mhz"],
            "屏蔽自激": "带内抬高 %.1f dB 后仍超限才告警" % t["self_suppress_db"],
            "发现时延口径": "首帧 -> 异常建立 ≤ %.0f 秒" % config.TARGET_DISCOVERY_SEC}
