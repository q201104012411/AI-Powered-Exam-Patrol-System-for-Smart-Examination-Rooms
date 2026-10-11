# -*- coding: utf-8 -*-
"""
断网可用（《智能终端.md》第二节第 3 条）

策略：
  · 告警一旦接入就先进本地队列（synced=0），本机功能完全不依赖网络
  · 网络恢复后按时间顺序批量补传，成功才置 synced=1（失败下次再传，不丢）
  · 超过 OFFLINE_MAX_AGE_HOURS 的陈旧记录只做标记并写审计，不静默丢弃
  · 队列以 SQLite 为准（进程重启不丢），另在 QUEUE_DIR 落一份 NDJSON 便于人工核对
"""
import json
import os
import urllib.error
import urllib.request

import config
import models


class ConnectivityProbe(object):
    """连通性探测：可注入自定义探针，便于测试与离线演示"""

    def __init__(self, probe=None):
        self._probe = probe

    def online(self, url=None):
        if self._probe is not None:
            return bool(self._probe(url))
        url = (url or config.PLATFORM_URL or "").rstrip("/")
        if not url:
            return False
        try:
            req = urllib.request.Request(url + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_SEC) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False


class Outbox(object):
    def __init__(self, store, probe=None, platform_url=None, sender=None):
        self.store = store
        self.probe = probe or ConnectivityProbe()
        self.platform_url = (platform_url or config.PLATFORM_URL or "").rstrip("/")
        self._sender = sender              # 可注入：(url, payload) -> bool

    # ---------- 入队 ----------
    def enqueue(self, alert):
        alert.synced = 0
        alert.queued_at = models.now_ts()
        self.store.save_alert(alert)
        try:
            with open(os.path.join(str(config.QUEUE_DIR), "outbox.ndjson"),
                      "a", encoding="utf-8") as f:
                f.write(models.canonical(alert.to_row()) + "\n")
        except OSError:
            pass
        return True

    # ---------- 发送 ----------
    def _send(self, payload):
        if self._sender is not None:
            return bool(self._sender(self.platform_url, payload))
        if not self.platform_url:
            return False
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.platform_url + "/alerts", data=data,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_SEC) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---------- 补传 ----------
    def flush(self, limit=None, force=False):
        """
        补传：返回 {online, sent, failed, dropped, pending}
        force=True 时跳过连通性判断（用于已确知在线或测试）
        """
        limit = int(limit or config.OFFLINE_MAX_BATCH)
        pending_all = self.store.pending_unsynced(limit=10 ** 6)
        if not pending_all:
            return {"online": True, "sent": 0, "failed": 0, "dropped": 0, "pending": 0}
        if not force and not self.probe.online(self.platform_url):
            self.store.log("补传跳过", "当前离线，待传 %d 条" % len(pending_all), "offline")
            return {"online": False, "sent": 0, "failed": 0, "dropped": 0,
                    "pending": len(pending_all)}

        now = models.now_epoch()
        max_age = config.OFFLINE_MAX_AGE_HOURS * 3600
        sent = failed = dropped = 0
        for a in pending_all[:limit]:
            age = now - (a.created_epoch or now)
            if age > max_age:
                # 陈旧记录：标记已同步并写审计，避免无限重传
                self.store.mark_synced(a.alert_id)
                self.store.log("补传超期丢弃", "%s 已超 %d 小时"
                               % (a.alert_id, config.OFFLINE_MAX_AGE_HOURS), "offline")
                dropped += 1
                continue
            payload = {"alert_id": a.alert_id, "code": a.code, "level": a.level,
                       "room_id": a.room_id, "seat_no": a.seat_no,
                       "candidate_id": a.candidate_id, "created_at": a.created_at,
                       "status": a.status, "review_conclusion": a.review_conclusion,
                       "queued_at": a.queued_at}
            if self._send(payload):
                self.store.mark_synced(a.alert_id)
                sent += 1
            else:
                failed += 1
        self.store.log("补传完成", "成功=%d 失败=%d 超期=%d" % (sent, failed, dropped),
                       "offline")
        return {"online": True, "sent": sent, "failed": failed, "dropped": dropped,
                "pending": len(pending_all) - sent - dropped}

    # ---------- 状态 ----------
    def status(self):
        pending = len(self.store.pending_unsynced(limit=10 ** 6))
        return {"online": self.probe.online(self.platform_url),
                "pending": pending,
                "platform": self.platform_url or "（未配置）",
                "max_age_hours": config.OFFLINE_MAX_AGE_HOURS}
