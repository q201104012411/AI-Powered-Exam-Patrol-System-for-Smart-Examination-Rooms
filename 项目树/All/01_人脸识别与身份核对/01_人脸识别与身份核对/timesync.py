# -*- coding: utf-8 -*-
"""
时钟偏差检查（只报警不阻断）。

审计要求"时间可对账"：如果本机时钟偏差过大，日志时间就失去证据价值。
实现：读取 HTTP 响应的 Date 头（标准库即可），与本地时间比较。
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DEFAULT_URL = "http://www.baidu.com"
WARN_SKEW_SEC = 5


def check_clock_skew(url: str = DEFAULT_URL, timeout: float = 3.0):
    """
    返回 (skew_seconds, detail)。网络不可用时返回 (None, 原因)。
    skew > 0 表示本机比服务器时间快。
    """
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "clock-check"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_hdr = resp.headers.get("Date")
    except Exception as e:
        return None, "无法获取网络时间：%s" % str(e)[:60]

    if not date_hdr:
        return None, "响应中无 Date 头"
    try:
        remote = parsedate_to_datetime(date_hdr)
    except (TypeError, ValueError):
        return None, "Date 头解析失败：%s" % date_hdr
    if remote.tzinfo is None:
        remote = remote.replace(tzinfo=timezone.utc)

    skew = (datetime.now(timezone.utc) - remote).total_seconds()
    if abs(skew) > WARN_SKEW_SEC:
        return skew, "时钟偏差 %.0f 秒，超过告警阈值 %d 秒，请校准系统时间（NTP）" % (skew, WARN_SKEW_SEC)
    return skew, "时钟偏差 %.1f 秒，正常" % skew
