# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 信号定位

三种手段，按可用数据自动选择（界面/报告里会写明用了哪种、可信度多少）：

  ① 多站场强定位：至少 3 个已知坐标的监测站同时测到同一频率 -> 对数距离模型算距离
     -> 线性化最小二乘 + 高斯-牛顿迭代 -> 估计坐标与误差半径；
  ② 多站测向交会：监测站给的是方位角（定向天线/测向机）-> 两两交会取稳健中心；
  ③ 单站径向：只有一个站时只能给"以站为圆心、半径 d 的圆"，**如实标低可信度**，
     不假装精确定位；现场再用手持侦测仪逐步逼近（probe_hint 给出靠近/远离提示）。

坐标口径：考点平面坐标，单位米；由部署时在「设备」里登记（x, y）。没有坐标的站不参与解算。
"""
import math

import config
import models
from models import Location

#: 对数距离路径损耗模型参数（1.8GHz 室内近似；现场可用实测标定覆盖）
RSSI_REF_DBM = -40.0        # 距发射源 1 米处的参考场强
PATH_LOSS_N = 3.0           # 路径损耗指数（室内有遮挡取 2.7~3.5）
DIST_MIN_M = 3.0
DIST_MAX_M = 800.0
DEFAULT_WINDOW_SEC = 60.0


# =============================================================================
# 距离模型
# =============================================================================
def rssi_to_distance(rssi, ref=RSSI_REF_DBM, n=PATH_LOSS_N):
    """场强 -> 距离（米）：d = 10^((ref - rssi) / (10n))。"""
    try:
        r = float(rssi)
    except (TypeError, ValueError):
        return DIST_MAX_M
    d = 10.0 ** ((float(ref) - r) / (10.0 * float(n)))
    return max(DIST_MIN_M, min(DIST_MAX_M, d))


def distance_to_rssi(distance_m, ref=RSSI_REF_DBM, n=PATH_LOSS_N):
    """距离 -> 场强（米）：rssi = ref - 10n·log10(d)。定位自检用它造数据。"""
    d = max(DIST_MIN_M, float(distance_m))
    return float(ref) - 10.0 * float(n) * math.log10(d)


# =============================================================================
# 数学：2x2 线性方程 / 迭代
# =============================================================================
def _solve2x2(a11, a12, a21, a22, b1, b2):
    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-9:
        return None
    return ((b1 * a22 - b2 * a12) / det, (a11 * b2 - a21 * b1) / det)


def _refine(points, x, y, iterations=4):
    """高斯-牛顿：让 sqrt((x-xi)^2+(y-yi)^2) 逼近测距 d_i。"""
    for _ in range(int(iterations)):
        jtj = [[0.0, 0.0], [0.0, 0.0]]
        jtr = [0.0, 0.0]
        for p in points:
            dx, dy = x - p["x"], y - p["y"]
            r = math.hypot(dx, dy)
            if r < 1e-6:
                continue
            res = r - p["d"]
            w = p["w"]
            jx, jy = dx / r, dy / r
            jtj[0][0] += w * jx * jx
            jtj[0][1] += w * jx * jy
            jtj[1][0] += w * jy * jx
            jtj[1][1] += w * jy * jy
            jtr[0] += w * jx * res
            jtr[1] += w * jy * res
        delta = _solve2x2(jtj[0][0], jtj[0][1], jtj[1][0], jtj[1][1],
                          -jtr[0], -jtr[1])
        if delta is None:
            break
        dx, dy = delta
        x += dx
        y += dy
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            break
    return x, y


def multilaterate(points):
    """
    多站场强最小二乘定位。

    points: [{x, y, d, w?}]（d=由场强换算的距离，w=权重，默认 1/d²）
    返回 {x, y, residual, iterations, ok, method}；点不足/退化时 ok=False。
    """
    pts = []
    for p in points or []:
        try:
            d = float(p["d"])
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if d <= 0:
            continue
        w = p.get("w")
        if w is None:
            w = 1.0 / max(1.0, d * d)
        pts.append({"x": x, "y": y, "d": d, "w": float(w)})
    if len(pts) < 3:
        return {"ok": False, "reason": "少于 3 个站，无法解算", "points": pts}
    # 先取加权质心作为迭代初值（比"最远站"稳）
    sw = sum(p["w"] for p in pts)
    x0 = sum(p["x"] * p["w"] for p in pts) / sw
    y0 = sum(p["y"] * p["w"] for p in pts) / sw

    # 线性化初解：以第一个站为参考
    ref = pts[0]
    a11 = a12 = a21 = a22 = b1 = b2 = 0.0
    for p in pts[1:]:
        ax = 2.0 * (ref["x"] - p["x"])
        ay = 2.0 * (ref["y"] - p["y"])
        c = (p["d"] ** 2 - ref["d"] ** 2
             - p["x"] ** 2 + ref["x"] ** 2
             - p["y"] ** 2 + ref["y"] ** 2)
        w = p["w"]
        a11 += w * ax * ax
        a12 += w * ax * ay
        a21 += w * ay * ax
        a22 += w * ay * ay
        b1 += w * ax * c
        b2 += w * ay * c
    sol = _solve2x2(a11, a12, a21, a22, b1, b2)
    if sol is not None:
        x0, y0 = sol
    x, y = _refine(pts, x0, y0)

    # 残差（米）
    errs = [abs(math.hypot(x - p["x"], y - p["y"]) - p["d"]) for p in pts]
    residual = math.sqrt(sum(e * e for e in errs) / max(1, len(errs)))
    return {"ok": True, "x": x, "y": y, "residual": residual, "n": len(pts),
            "points": pts, "method": "多站场强最小二乘"}


def weighted_centroid(points):
    """加权质心兜底：权重 1/d²（越近的站权重越大）。"""
    sw = 0.0
    sx = sy = 0.0
    for p in points or []:
        d = max(1.0, float(p.get("d") or 1.0))
        w = 1.0 / (d * d)
        sw += w
        sx += float(p["x"]) * w
        sy += float(p["y"]) * w
    if sw <= 0:
        return None
    return sx / sw, sy / sw


# =============================================================================
# 测向交会
# =============================================================================
def bearing_between(x1, y1, x2, y2):
    """从 (x1,y1) 看 (x2,y2) 的方位角（0=正北，顺时针，度）。"""
    dx, dy = float(x2) - float(x1), float(y2) - float(y1)
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _ray_intersection(a, b):
    """两条射线（方位角）交会；平行返回 None。"""
    def rad(deg):
        return math.radians(float(deg))
    dx1, dy1 = math.sin(rad(a["bearing"])), math.cos(rad(a["bearing"]))
    dx2, dy2 = math.sin(rad(b["bearing"])), math.cos(rad(b["bearing"]))
    det = dx1 * (-dy2) - dy1 * (-dx2)
    if abs(det) < 1e-9:
        return None
    rx, ry = float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"])
    t = (rx * (-dy2) - ry * (-dx2)) / det
    return float(a["x"]) + t * dx1, float(a["y"]) + t * dy1


def locate_bearings(bearings):
    """
    多站测向交会：两两求交点，取中位数作为稳健估计（抗一条错误测向）。
    bearings: [{x, y, bearing}]；返回 {ok, x, y, spread, n, method}。
    """
    pts = []
    for b in bearings or []:
        try:
            pts.append({"x": float(b["x"]), "y": float(b["y"]),
                        "bearing": float(b["bearing"]) % 360.0})
        except (KeyError, TypeError, ValueError):
            continue
    if len(pts) < 2:
        return {"ok": False, "reason": "测向点少于 2 个"}
    xs, ys = [], []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            hit = _ray_intersection(pts[i], pts[j])
            if hit is not None:
                xs.append(hit[0])
                ys.append(hit[1])
    if not xs:
        return {"ok": False, "reason": "所有测向线平行，无法交会"}
    xs.sort()
    ys.sort()
    x = xs[len(xs) // 2]
    y = ys[len(ys) // 2]
    spread = max(math.hypot(px - x, py - y) for px, py in zip(xs, ys))
    return {"ok": True, "x": x, "y": y, "spread": spread, "n": len(pts),
            "method": "多站测向交会"}


# =============================================================================
# 从扫描数据里取测量
# =============================================================================
def collect_measurements(store, anomaly, window_sec=DEFAULT_WINDOW_SEC):
    """
    取"其它监测站也测到同一频率"的测量：窗口内、同频 ±容差、功率最高的一条。

    返回 [{station_code, x, y, rssi, bearing?}]，只保留登记了坐标的站。
    """
    tol = float(config.thresholds()["dedup_freq_mhz"])
    # 以异常**首次出现**的时刻为基准取前后窗口，不依赖"当前时间"——
    # 回放测试里的异常是过去时刻产生的，用 now 会把它们排除掉。
    anchor = float(anomaly.first_seen_epoch or anomaly.detected_epoch or
                   models.now_epoch())
    scans = store.list_scans(band=anomaly.band or None,
                             since_epoch=anchor - window_sec, limit=2000)
    best = {}
    for sc in scans:
        if sc.ts_epoch > anchor + window_sec:
            continue
        dev = store.get_device(code=sc.station_code)
        if dev is None or dev.kind == config.DEV_JAMMER:
            continue
        if not dev.located:
            continue
        for pk in (sc.peaks or []):
            try:
                freq = float(pk.get("freq_mhz"))
                power = float(pk.get("power_dbm"))
            except (TypeError, ValueError):
                continue
            if abs(freq - float(anomaly.freq_mhz)) > tol:
                continue
            cur = best.get(sc.station_code)
            if cur is None or power > cur["rssi"]:
                item = {"station_code": dev.code, "x": float(dev.x), "y": float(dev.y),
                        "rssi": power}
                if pk.get("bearing") is not None:
                    item["bearing"] = models.to_float(pk.get("bearing"))
                best[sc.station_code] = item
    return list(best.values())


# =============================================================================
# 主入口
# =============================================================================
def locate_anomaly(store, anomaly, measurements=None, now=None, operator="linkage",
                   persist=True):
    """
    对一条异常做定位（自动选手段），把结果写回异常并留一条 locations 记录。

    返回 {ok, x, y, radius_m, confidence, method, stations, n, residual, note}
    """
    meas = measurements if measurements is not None else collect_measurements(store, anomaly)
    if not meas:
        return {"ok": False, "reason": "没有可用的多点测量（至少需要一个登记坐标的监测站）"}
    # 方向数据优先（测向比场强精确）
    bearings = [m for m in meas if m.get("bearing") is not None]
    if len(bearings) >= 2:
        res = locate_bearings(bearings)
        if res.get("ok"):
            spread = float(res.get("spread") or 0)
            radius = max(12.0, spread)
            conf = min(0.95, 0.55 + 0.1 * len(bearings) - min(0.2, spread / 200.0))
            return _persist(store, anomaly, res["x"], res["y"], radius, conf,
                            res["method"], meas, persist, operator,
                            note="测向交会，散布 ±%.0fm" % spread)
    points = []
    for m in meas:
        d = rssi_to_distance(m["rssi"])
        points.append({"x": float(m["x"]), "y": float(m["y"]), "d": d,
                       "w": 1.0 / max(1.0, d * d), "station_code": m["station_code"],
                       "rssi": m["rssi"]})
    if len(points) >= 3:
        res = multilaterate(points)
        if res.get("ok"):
            residual = float(res["residual"])
            radius = max(10.0, min(300.0, 2.0 * residual + 8.0))
            conf = max(0.15, min(0.95, 0.5 + 0.1 * len(points)
                                 - min(0.35, residual / 100.0)))
            return _persist(store, anomaly, res["x"], res["y"], radius, conf,
                            res["method"], meas, persist, operator,
                            note="残差 %.1fm，参与 %d 站" % (residual, len(points)))
    if len(points) == 2:
        c = weighted_centroid(points)
        d_avg = sum(p["d"] for p in points) / 2.0
        radius = max(15.0, d_avg * 0.5)
        return _persist(store, anomaly, c[0], c[1], radius, 0.35, "两站加权质心",
                        meas, persist, operator, note="仅 2 站，精度有限，建议手持逼近")
    p = points[0]
    return _persist(store, anomaly, p["x"], p["y"], p["d"], 0.2, "单站径向",
                    meas, persist, operator,
                    note="仅 1 站：以站为圆心、半径 %.0fm 的圆，需现场逼近" % p["d"])


def _persist(store, anomaly, x, y, radius, confidence, method, meas, persist,
             operator, note=""):
    stations = [{"station_code": m["station_code"], "x": m["x"], "y": m["y"],
                 "rssi": m["rssi"],
                 "distance_m": round(rssi_to_distance(m["rssi"]), 1)} for m in meas]
    out = {"ok": True, "x": round(float(x), 1), "y": round(float(y), 1),
           "radius_m": round(float(radius), 1),
           "confidence": round(float(confidence), 3), "method": method,
           "stations": stations, "n": len(meas), "note": note}
    if persist:
        store.save_location(Location(anomaly_id=anomaly.anomaly_id, x=out["x"],
                                     y=out["y"], radius_m=out["radius_m"],
                                     confidence=out["confidence"], method=method,
                                     stations=stations, note=note))
        anomaly.located_x = out["x"]
        anomaly.located_y = out["y"]
        anomaly.radius_m = out["radius_m"]
        anomaly.confidence = out["confidence"]
        if note:
            anomaly.detail = (anomaly.detail or "") + " ｜定位：" + note
        store.save_anomaly(anomaly)
        store.log("信号定位", "异常 %s -> (%.1f, %.1f) ±%.0fm（%s，可信度 %.2f）"
                  % (anomaly.anomaly_id, out["x"], out["y"], out["radius_m"],
                     method, out["confidence"]), operator)
    return out


# =============================================================================
# 手持侦测逼近
# =============================================================================
def probe_hint(prev_rssi, rssi, threshold_db=3.0):
    """
    手持侦测仪的逼近提示：场强上升=靠近，下降=远离。

    返回 {delta_db, trend, text}
    """
    delta = round(float(rssi) - float(prev_rssi), 1)
    if delta >= float(threshold_db):
        trend, text = "closer", "靠近了 %.1f dB，沿当前方向继续" % delta
    elif delta <= -float(threshold_db):
        trend, text = "farther", "远离了 %.1f dB，往回走并左右横扫" % abs(delta)
    else:
        trend, text = "hold", "变化 %.1f dB（在阈值内），换方向横扫或贴近墙体" % delta
    return {"delta_db": delta, "trend": trend, "text": text}


def coverage(store, margin_m=None, grid=5):
    """
    监测覆盖自检：考点平面范围（所有登记坐标设备的包围盒）外扩 100 米，
    用 5×5 网格采样，看每个采样点是否落在某个监测站的覆盖半径内。

    没有坐标的站/没有登记任何坐标时**如实判为未覆盖**（部署缺口，不美化）。
    返回 {rate, stations, points, covered, uncovered, margin_m, radius_m}
    """
    margin = float(margin_m if margin_m is not None else config.TARGET_COVERAGE_MARGIN_M)
    radius = float(config.MONITOR_RADIUS_M)
    stations = [d for d in store.list_devices(kind=config.DEV_MONITOR)
                if d.located]
    pts = [(float(d.x), float(d.y)) for d in store.list_devices() if d.located]
    if not stations or not pts:
        return {"rate": 0.0, "stations": len(stations), "points": 0, "covered": 0,
                "uncovered": [], "margin_m": margin, "radius_m": radius,
                "note": "没有登记坐标的监测站，无法评估覆盖"}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    n = max(3, int(grid))
    samples = []
    for i in range(n):
        for j in range(n):
            x = x0 + (x1 - x0) * i / float(n - 1)
            y = y0 + (y1 - y0) * j / float(n - 1)
            samples.append((round(x, 1), round(y, 1)))
    uncovered = []
    for x, y in samples:
        if not any(math.hypot(float(s.x) - x, float(s.y) - y) <= radius
                   for s in stations):
            uncovered.append({"x": x, "y": y})
    covered = len(samples) - len(uncovered)
    return {"rate": round(covered / float(len(samples)), 3),
            "stations": len(stations), "points": len(samples), "covered": covered,
            "uncovered": uncovered[:40], "margin_m": margin, "radius_m": radius,
            "site_box": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
            "note": "覆盖半径 %.0f 米，采样 %d×%d" % (radius, n, n)}


def describe():
    return {"定位手段": "多站场强最小二乘 / 多站测向交会 / 单站径向",
            "距离模型": "RSSI(dBm) = %.0f - 10×%.1f×log10(d)，d=米"
                        % (RSSI_REF_DBM, PATH_LOSS_N),
            "坐标口径": "考点平面坐标（米），设备登记 x/y 后参与解算"}
