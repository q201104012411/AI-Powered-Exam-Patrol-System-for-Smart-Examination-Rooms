# -*- coding: utf-8 -*-
"""
考场时钟终端（《智能终端.md》第一节"考场时钟终端"、第三节"标准时间"）

能力：
  · 标准时间获取：优先平台授时接口，退化到本机时间（并标注来源）
  · 偏差检测：与标准时间偏差超过 CLOCK_MAX_DRIFT_SEC 即产生 CLOCK_DRIFT 告警
  · 指令播报：按时刻表到点触发播报（到点判定可注入 now，便于测试）
  · 播报留痕：每次播报写审计，便于考务追溯
"""
import json
import time
import urllib.error
import urllib.request

import config
import models

BROADCAST_KEY = "clock.broadcasts"


class StandardClock(object):
    def __init__(self, store, hub=None, time_provider=None, platform_url=None):
        self.store = store
        self.hub = hub
        self._provider = time_provider      # 可注入：() -> (epoch, source)
        self.platform_url = (platform_url if platform_url is not None
                             else config.PLATFORM_URL).rstrip("/")
        self._fired = set()

    # ---------------- 标准时间 ----------------
    def standard_time(self):
        """返回 (epoch, source)；source 用于如实标注时间来源"""
        if self._provider is not None:
            e, s = self._provider()
            return float(e), s
        if self.platform_url:
            try:
                req = urllib.request.Request(self.platform_url + "/time", method="GET")
                with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_SEC) as r:
                    data = json.loads(r.read().decode("utf-8"))
                return float(data["epoch"]), "平台授时"
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass
        return time.time(), "本机时间（未接平台授时）"

    def drift(self):
        """返回 {drift_sec, source, display, ok}"""
        std, source = self.standard_time()
        local = time.time()
        d = local - std
        return {"drift_sec": round(d, 3), "source": source,
                "display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(std)),
                "ok": abs(d) <= config.CLOCK_MAX_DRIFT_SEC}

    def check_and_alert(self, device_id="CLOCK-01"):
        """偏差超限则产生告警（橙级）"""
        st = self.drift()
        if st["ok"] or self.hub is None:
            return None
        a = models.Alert(code="CLOCK_DRIFT",
                         detail="时钟偏差 %.1f 秒（来源：%s）" % (st["drift_sec"], st["source"]),
                         device_id=device_id, source="clock")
        alert, created = self.hub.ingest(a)
        return alert if created else None

    # ---------------- 指令播报 ----------------
    def broadcasts(self):
        b = self.store.get_kv(BROADCAST_KEY)
        if b is None:
            b = [{"at": t, "text": s} for t, s in config.DEFAULT_BROADCASTS]
            self.store.set_kv(BROADCAST_KEY, b)
        return b

    def set_broadcasts(self, items, operator="owner"):
        norm = [{"at": str(i.get("at", "")).strip(), "text": str(i.get("text", "")).strip()}
                for i in items if i.get("at") and i.get("text")]
        self.store.set_kv(BROADCAST_KEY, norm)
        self.store.log("播报时刻表更新", "%d 条" % len(norm), operator)
        return norm

    def due_broadcasts(self, now=None):
        """
        返回"当前这一分钟应播报"的条目（同一时刻只播一次）。
        now 可注入，便于离线测试。
        """
        now = now if now is not None else time.time()
        hhmm = time.strftime("%H:%M", time.localtime(now))
        out = []
        for b in self.broadcasts():
            key = "%s|%s" % (hhmm, b["at"])
            if b["at"] == hhmm and key not in self._fired:
                self._fired.add(key)
                out.append(b)
        return out

    def fire_due(self, now=None, device_id="CLOCK-01"):
        """播报到点条目，写审计；返回播报列表（实际发声由终端界面负责）"""
        items = self.due_broadcasts(now=now)
        for it in items:
            self.store.log("考试指令播报", "%s %s" % (it["at"], it["text"]), device_id)
        return items

    def next_broadcast(self, now=None):
        """下一条待播报（用于界面提示）"""
        now = now if now is not None else time.time()
        cur = time.strftime("%H:%M", time.localtime(now))
        later = [b for b in sorted(self.broadcasts(), key=lambda x: x["at"]) if b["at"] > cur]
        return later[0] if later else (sorted(self.broadcasts(), key=lambda x: x["at"])[0]
                                       if self.broadcasts() else None)

    def display(self, now=None):
        st = self.drift()
        nxt = self.next_broadcast(now=now)
        return {
            "standard_time": st["display"],
            "source": st["source"],
            "drift_sec": st["drift_sec"],
            "drift_ok": st["ok"],
            "next": ("%s %s" % (nxt["at"], nxt["text"])) if nxt else "—",
        }
