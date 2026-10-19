# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 作弊信号特征库

为什么需要特征库：频谱上"有信号"不等于"有作弊"。同一个 2.4 GHz 峰可能是合法 WiFi AP、
蓝牙鼠标，也可能是数传作弊模块；只看场强会天天误报。特征库把**频率范围、占用带宽、
调制方式、占空特征**四件事放在一起比对，把"异常"分成：

    作弊特征（命中非法特征） -> 红/橙告警，送 03 处置
    合法特征（WiFi/基站/对讲）-> 只记台账，不告警
    无匹配（未知发射）       -> 低级别"关注"，靠多帧持续/定位进一步判

匹配是**可解释的打分**：频率命中 45%、带宽相似 30%、占空特征 25%，再乘特征基准置信度。
不搞黑盒，界面/报告里能看到"为什么判成这个"。
"""
import config
from models import Feature

#: 内置特征（feature_code, 名称, 低 MHz, 高 MHz, 带宽 MHz, 调制, 占空,
#:            是否合法, 基准置信度, 说明）
BUILTIN_FEATURES = (
    ("FEAT-CHEAT-ISM-BURST", "2.4G 数传作弊模块（突发）", 2400.0, 2483.5, 2.0,
     "GFSK/DSSS", "burst", 0, 0.86, "答案数传常用；短突发、间隔发射"),
    ("FEAT-CHEAT-ISM-HOP", "2.4G 跳频作弊（改装蓝牙/耳机）", 2400.0, 2483.5, 1.0,
     "FHSS", "hopping", 0, 0.80, "跳频图案规律、单次驻留时间短"),
    ("FEAT-CHEAT-58G", "5.8G 无线图传/数传", 5725.0, 5850.0, 8.0,
     "OFDM", "burst", 0, 0.82, "传答案图像或大流量数据"),
    ("FEAT-CHEAT-UHF-EAR", "UHF 隐形耳机接收", 400.0, 470.0, 0.025,
     "FM", "continuous", 0, 0.76, "窄带模拟，长时间连续发射"),
    ("FEAT-CHEAT-VHF-EAR", "VHF 隐形耳机", 136.0, 174.0, 0.025,
     "FM", "continuous", 0, 0.72, "窄带模拟，隐蔽性强"),
    ("FEAT-CHEAT-GSM", "GSM 数传模块（短信/数据）", 890.0, 960.0, 0.2,
     "GMSK", "burst", 0, 0.70, "2G 模块发答案；考场内不应有终端发射"),
    ("FEAT-CHEAT-LTE", "4G 数传终端（上行）", 1710.0, 2170.0, 10.0,
     "OFDMA", "burst", 0, 0.68, "4G 数据终端上行发射"),
    ("FEAT-LEGAL-WIFI24", "合法 WiFi AP（2.4G）", 2400.0, 2483.5, 20.0,
     "OFDM", "continuous", 1, 0.18, "20/40M 连续占用，多为考场外 AP"),
    ("FEAT-LEGAL-WIFI5", "合法 WiFi AP（5G）", 5150.0, 5850.0, 40.0,
     "OFDM", "continuous", 1, 0.15, "5G WiFi 连续占用"),
    ("FEAT-LEGAL-BT", "蓝牙设备（考场属禁带物品）", 2400.0, 2483.5, 1.0,
     "GFSK", "hopping", 1, 0.25, "频谱形态与作弊耳机相近，是否作弊由 06 安检/现场确认"),
    ("FEAT-LEGAL-INTERCOM", "考务对讲", 400.0, 470.0, 0.0125,
     "FM", "continuous", 1, 0.20, "窄带对讲；若本考点考务对讲在此段，屏蔽计划应排除"),
    ("FEAT-LEGAL-GSM-DL", "公众移动通信基站（GSM 下行）", 935.0, 960.0, 0.2,
     "GMSK", "continuous", 1, 0.18, "运营商基站，屏蔽时须控制外溢"),
    ("FEAT-LEGAL-LTE-DL", "公众移动通信基站（LTE 下行）", 1805.0, 1880.0, 20.0,
     "OFDMA", "continuous", 1, 0.16, "运营商基站"),
    ("FEAT-LEGAL-NR", "公众移动通信基站（5G NR）", 2515.0, 2675.0, 100.0,
     "OFDMA", "continuous", 1, 0.15, "5G 基站，宽带连续"),
)

DUTY_TEXT = {"burst": "突发", "continuous": "连续", "hopping": "跳频"}
DUTY_SCORE = {
    ("burst", "burst"): 1.0, ("continuous", "continuous"): 1.0,
    ("hopping", "hopping"): 1.0, ("burst", "continuous"): 0.35,
    ("continuous", "burst"): 0.35, ("hopping", "burst"): 0.6,
    ("burst", "hopping"): 0.6, ("continuous", "hopping"): 0.3,
    ("hopping", "continuous"): 0.3,
}

W_FREQ = 0.45
W_BW = 0.30
W_DUTY = 0.25
EDGE_TOL_MHZ = 1.0


# =============================================================================
# 内置特征入库
# =============================================================================
def ensure_builtin(store):
    """把内置特征补进库（已存在的**不覆盖**，用户改过的名称/置信度不会被冲掉）。"""
    added = 0
    for row in BUILTIN_FEATURES:
        code = row[0]
        if store.get_feature(code) is not None:
            continue
        store.save_feature(Feature(
            feature_code=code, name=row[1], low_mhz=row[2], high_mhz=row[3],
            bandwidth_mhz=row[4], modulation=row[5], duty=row[6],
            is_legal=int(row[7]), base_confidence=float(row[8]), enabled=1,
            builtin=1, note=row[9]))
        added += 1
    return added


def catalog(store=None):
    rows = store.list_features() if store is not None else builtin_list()
    return [dict(r.to_dict(), duty_text=DUTY_TEXT.get(r.duty, r.duty),
                 legal_text="合法" if r.is_legal else "作弊/可疑",
                 range_text=r.range_text()) for r in rows]


def builtin_list():
    return [Feature(feature_code=r[0], name=r[1], low_mhz=r[2], high_mhz=r[3],
                    bandwidth_mhz=r[4], modulation=r[5], duty=r[6],
                    is_legal=int(r[7]), base_confidence=float(r[8]), enabled=1,
                    builtin=1, note=r[9]) for r in BUILTIN_FEATURES]


# =============================================================================
# 新增/修改特征
# =============================================================================
def add_feature(store, code, name, low_mhz, high_mhz, bandwidth_mhz,
                modulation="", duty="burst", is_legal=False,
                base_confidence=0.7, note="", operator="linkage"):
    code = str(code or "").strip().upper()
    name = str(name or "").strip()
    if not code or not name:
        return False, "特征编码与名称必填"
    try:
        lo, hi = float(low_mhz), float(high_mhz)
        bw = float(bandwidth_mhz)
        conf = float(base_confidence)
    except (TypeError, ValueError):
        return False, "频率/带宽/置信度必须是数字"
    if lo >= hi:
        return False, "低端频率必须小于高端频率"
    if bw <= 0 or bw > (hi - lo + 50.0):
        return False, "带宽不合理（0 < 带宽 ≤ 频段宽度）"
    if not (0.0 <= conf <= 1.0):
        return False, "置信度必须在 0~1 之间"
    if duty not in DUTY_TEXT:
        return False, "占空特征必须是 burst/continuous/hopping 之一"
    old = store.get_feature(code)
    f = Feature(feature_code=code, name=name, low_mhz=lo, high_mhz=hi,
                bandwidth_mhz=bw, modulation=str(modulation or ""), duty=duty,
                is_legal=1 if is_legal else 0, base_confidence=conf, enabled=1,
                builtin=0, note=str(note or ""))
    store.save_feature(f)
    store.log("特征库更新", "%s %s（%s，%s）" % (
        code, name, f.range_text(), "合法" if is_legal else "作弊/可疑"),
        operator)
    return True, ("已更新" if old else "已新增") + "：%s" % code


# =============================================================================
# 匹配
# =============================================================================
def _bw_score(measured_bw, feature_bw):
    """
    带宽相似度（0~1）：按**相对差**衰减，1.0=完全一致。

    不用 log2 比值：2.4G 数传 2 MHz 与 WiFi 20 MHz 的 log 比只有 3.3，仍能拿到
    0.45 分，会把"窄带作弊"错配成"合法 WiFi"。相对差更贴近工程判断：
    差 10 倍就是 0.1 分，差 5% 才是 0.95 分。
    """
    try:
        m = max(0.005, float(measured_bw))
        f = max(0.005, float(feature_bw))
    except (TypeError, ValueError):
        return 0.0
    diff = abs(m - f) / max(m, f)
    return max(0.0, 1.0 - diff)


def _freq_score(freq, low, high):
    if low <= freq <= high:
        return 1.0
    edge = min(abs(freq - low), abs(freq - high))
    if edge <= EDGE_TOL_MHZ:
        return max(0.0, 0.5 - edge / (2.0 * EDGE_TOL_MHZ))
    return 0.0


def score_peak(freq_mhz, bandwidth_mhz, duty, feature):
    """单个特征与单个峰的匹配得分（0~1，可解释）。"""
    fs = _freq_score(float(freq_mhz), float(feature.low_mhz), float(feature.high_mhz))
    if fs <= 0:
        return 0.0, {"freq": 0.0, "bw": 0.0, "duty": 0.0}
    bws = _bw_score(bandwidth_mhz, feature.bandwidth_mhz)
    ds = DUTY_SCORE.get((str(duty or "burst"), str(feature.duty or "burst")), 0.5)
    total = W_FREQ * fs + W_BW * bws + W_DUTY * ds
    return total, {"freq": round(fs, 3), "bw": round(bws, 3), "duty": round(ds, 3)}


def match(freq_mhz, bandwidth_mhz=0.1, duty="burst", features=None):
    """
    给一个信号峰找最像的特征。

    返回 dict：
      matched: bool           是否有可接受的特征（score ≥ 0.45 且频率命中）
      feature: Feature|None
      score / confidence / is_legal / reason / parts
    没有匹配时 confidence=0，is_legal=False，reason 说明原因（供界面展示）。
    """
    feats = list(features if features is not None else builtin_list())
    best = None
    best_score = 0.0
    best_parts = {}
    for f in feats:
        if not int(getattr(f, "enabled", 1) or 0):
            continue
        s, parts = score_peak(freq_mhz, bandwidth_mhz, duty, f)
        if s > best_score:
            best, best_score, best_parts = f, s, parts
    if best is None or best_score < 0.45:
        return {"matched": False, "feature": None, "score": round(best_score, 3),
                "confidence": 0.0, "is_legal": False,
                "reason": "未命中特征库（频率 %.3f MHz，带宽 %.3f MHz）"
                          % (float(freq_mhz), float(bandwidth_mhz)),
                "parts": best_parts}
    # 置信度 = 基准置信度 × (0.55 + 0.45 × 匹配分)
    conf = round(float(best.base_confidence) * (0.55 + 0.45 * best_score), 3)
    return {"matched": True, "feature": best, "score": round(best_score, 3),
            "confidence": conf, "is_legal": bool(best.is_legal),
            "reason": "%s（匹配分 %.2f：频率 %.2f/带宽 %.2f/占空 %.2f）"
                      % (best.name, best_score, best_parts.get("freq", 0),
                         best_parts.get("bw", 0), best_parts.get("duty", 0)),
            "parts": best_parts}


def match_peak(store, peak):
    """便利函数：按库内特征匹配一个峰 dict（freq_mhz/bandwidth_mhz/duty）。"""
    feats = store.list_features(enabled_only=True)
    if not feats:
        feats = builtin_list()
    return match(peak.get("freq_mhz") or 0, peak.get("bandwidth_mhz") or 0.05,
                 peak.get("duty") or "burst", feats)


def summary(store=None):
    rows = store.list_features() if store is not None else builtin_list()
    total = len(rows)
    legal = sum(1 for r in rows if r.is_legal)
    return {"total": total, "cheat": total - legal, "legal": legal,
            "builtin": sum(1 for r in rows if r.builtin),
            "custom": sum(1 for r in rows if not r.builtin)}


def describe():
    s = summary()
    return {"特征库": "%d 条（作弊/可疑 %d、合法 %d；内置 %d、自定义 %d）"
                      % (s["total"], s["cheat"], s["legal"], s["builtin"],
                         s["custom"]),
            "打分权重": "频率 %.0f%% / 带宽 %.0f%% / 占空 %.0f%%"
                        % (W_FREQ * 100, W_BW * 100, W_DUTY * 100)}
