# -*- coding: utf-8 -*-
"""
告警接入与分级展示（《智能终端.md》第二节第 1 条）

职责：
  · 从边缘节点 / 业务平台接入告警（HTTP 拉取，失败不抛异常——断网可用）
  · 分级兜底：平台未给级别时按告警码映射为 橙 / 红
  · 去重：同 考场+座位+告警码 在时间窗内只保留一条
  · 全部接入动作写哈希链审计
"""
import json
import time
import urllib.error
import urllib.request

import config
import models

DEDUP_WINDOW_SEC = 60


class AlertHub(object):
    def __init__(self, store, offline=None):
        self.store = store
        self.offline = offline

    # ---------- 接入 ----------
    def ingest(self, alert, dedup=True):
        """
        接入一条告警：分级 -> 去重 -> 落库 -> 审计（-> 进补传队列）
        返回 (alert, created: bool)
        """
        if alert.level not in config.LEVELS:
            alert.level = config.ALERT_CODE_LEVEL.get(alert.code, config.LEVEL_ORANGE)
        if dedup:
            dup = self._find_dup(alert)
            if dup is not None:
                return dup, False
        self.store.save_alert(alert)
        if self.offline is not None:
            self.offline.enqueue(alert)
        self.store.log("告警接入",
                       "%s %s 考场=%s 座位=%s" % (alert.level, alert.code,
                                                  alert.room_id or "-",
                                                  alert.seat_no or "-"),
                       alert.source or "edge")
        return alert, True

    def ingest_many(self, alerts, dedup=True):
        created, dup = [], 0
        for a in alerts:
            item, is_new = self.ingest(a, dedup=dedup)
            if is_new:
                created.append(item)
            else:
                dup += 1
        return created, dup

    def _find_dup(self, alert):
        now = models.now_epoch()
        for a in self.store.list_alerts(room_id=alert.room_id or None, limit=200):
            if (a.code == alert.code and (a.seat_no or "") == (alert.seat_no or "")
                    and now - a.created_epoch <= DEDUP_WINDOW_SEC
                    and a.status == models.ST_PENDING):
                return a
        return None

    # ---------- 确认（红色预警强制确认） ----------
    def ack(self, alert_id, operator, reason=""):
        a = self.store.get_alert(alert_id)
        if a is None:
            return False, "告警不存在"
        if a.status != models.ST_PENDING:
            return False, "该告警已处置"
        a.status = models.ST_ACKED
        a.acked_by = operator
        a.acked_at = models.now_ts()
        self.store.save_alert(a)
        detail = "%s %s" % (a.code, alert_id)
        if reason:
            detail += " 理由：%s" % reason[:120]
        self.store.log("告警确认", detail, operator)
        return True, "已确认（%s）" % a.level_name()

    # ---------- 优先展示顺序（红在前，其次超时） ----------
    @staticmethod
    def display_order(alerts):
        def key(a):
            return (0 if a.level == config.LEVEL_RED else 1,
                    0 if a.overdue() else 1,
                    -a.created_epoch)
        return sorted(alerts, key=key)

    # ---------- 从边缘节点 / 平台拉取 ----------
    def fetch_from_edge(self, url=None, timeout=None):
        """拉取边缘节点告警；断网/异常时返回 ([], 原因)，并把原因写审计"""
        url = (url or config.EDGE_URL or "").rstrip("/")
        if not url:
            return [], "未配置边缘节点地址"
        try:
            req = urllib.request.Request(url + "/alerts", method="GET")
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT_SEC) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.store.log("边缘拉取失败", str(e)[:160], "edge")
            return [], "边缘节点不可达：%s" % str(e)[:80]
        items = payload.get("alerts", payload) if isinstance(payload, dict) else payload
        out = []
        for it in items or []:
            out.append(models.Alert(code=it.get("code", "UNKNOWN"),
                                    detail=it.get("detail", ""),
                                    room_id=it.get("room_id", ""),
                                    seat_no=it.get("seat_no", ""),
                                    candidate_id=it.get("candidate_id", ""),
                                    candidate_name=it.get("candidate_name", ""),
                                    device_id=it.get("device_id", ""),
                                    level=it.get("level"), source="edge"))
        return out, "拉取 %d 条" % len(out)

    def simulate(self, n=6):
        """本地演示用：造一批覆盖橙/红两级的告警（标注 source=demo，不冒充真实接入）"""
        specs = [
            ("MULTI_FACE", "A-01", "05", "同一画面检测到 2 张人脸"),
            ("LOOK_DOWN", "A-01", "07", "持续低头 45 秒"),
            ("NO_INVIGILATOR", "A-02", "", "监考位无人超过 60 秒"),
            ("PHONE_DETECTED", "A-02", "12", "疑似手机屏幕反光"),
            ("VIDEO_BLUR", "A-03", "", "视频清晰度低于阈值"),
            ("TURN_HEAD", "A-03", "03", "频繁转头 6 次/分钟"),
        ]
        out = []
        for i, (code, room, seat, detail) in enumerate(specs[:n]):
            out.append(models.Alert(code=code, detail=detail, room_id=room, seat_no=seat,
                                    source="demo", device_id="CAM-%s" % room))
        return out


def level_badge(level):
    m = config.LEVEL_META.get(level, {})
    return "%s（提示音=%s 弹窗=%s 强制确认=%s SLA=%ds）" % (
        m.get("name", level), m.get("sound"), m.get("popup"),
        m.get("force_confirm"), m.get("sla_sec", 0))


def sleep(sec):
    time.sleep(sec)
