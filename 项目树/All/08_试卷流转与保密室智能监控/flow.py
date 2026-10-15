# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 二、试卷流转环节

逐条实现《试卷流转与保密室智能监控.md》第二节表格：

| 环节         | 实现                                                        |
| 领取/交接    | HandoverFlow：扫码/RFID 电子交接 + 双人签字 + 人脸确认       |
| 押运         | EscortFlow：北斗/GPS 定位 + 车内视频回传 + 路线偏离 ≤30s 告警 |
| 电子铅封     | SealFlow：试卷袋电子封签，异常开启即时告警                   |
| 分发至考场   | DeliveryFlow：分发记录与考场/时间/责任人一一对应             |
| 回收与封存   | RecycleFlow：回收扫码核验数量，差异即时上报                  |

共同约定：
  · 每个环节都往 Bag.history 追加一条（形成完整可追溯链条）
  · 每个环节都把**事件时刻**传给告警中心，用于时延指标核查
  · 记录不完整/数量不符一律告警，不静默放过
"""
import math

import config
import models
from alarm_hub import AlarmHub


# 地球半径（米），用于 haversine
_EARTH_R_M = 6371000.0


def hav_distance_m(lat1, lng1, lat2, lng2):
    """两点球面距离（米）。押运路线偏离判定用。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def point_to_polyline_m(lat, lng, route):
    """点到规划路线（折线）的最短距离（米）。route 为空时返回 None。

    逐段用"点到线段"的近似投影算：把经纬度按该纬度换算成米平面，
    对短线段足够准（押运场景几公里量级，误差可忽略）。
    """
    if not route:
        return None
    if len(route) == 1:
        r = route[0]
        return hav_distance_m(lat, lng, r[0], r[1])
    coslat = math.cos(math.radians(lat))
    px, py = lng * 111320.0 * coslat, lat * 110540.0
    best = None
    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        ax, ay = a[1] * 111320.0 * coslat, a[0] * 110540.0
        bx, by = b[1] * 111320.0 * coslat, b[0] * 110540.0
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        best = d if best is None else min(best, d)
    return best


class FlowBase(object):
    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)

    def _bag(self, bag_id, ensure=True):
        b = self.store.get_bag(bag_id)
        if b is None and ensure:
            b = models.Bag(bag_id, rfid=bag_id)
        return b

    def _touch_bag(self, bag, stage, person, note=""):
        if bag is None:
            return
        bag.current_stage = stage
        bag.history.append({"stage": stage,
                            "stage_text": config.FLOW_STAGES.get(stage, {}).get("name", stage),
                            "ts": models.now_ts(), "person": person, "note": note})
        self.store.save_bag(bag)


# ==================================================================
# 领取 / 交接
# ==================================================================
class HandoverFlow(FlowBase):
    """扫码/RFID 电子交接，双人签字 + 人脸确认。"""

    def handover(self, bag_id, from_person, to_person, persons=None, signatures=None,
                 faces=None, method="scan", quantity=0, room_id="", stage="handover",
                 event_epoch=None, operator="system"):
        """登记一次交接。返回 (Handover, [Alarm,...])。

        文档"双人签字 + 人脸确认"是硬要求，任何一项缺失都告警：
          · 人数 < 2              -> HANDOVER_PERSON_MISS（红）
          · 人脸确认人数 < 参与人 -> HANDOVER_FACE_MISS（红）
          · 签字人数 < 参与人     -> HANDOVER_INCOMPLETE（橙）
        """
        persons = list(persons or [p for p in (from_person, to_person) if p])
        h = models.Handover(bag_id, stage, from_person=from_person, to_person=to_person,
                            persons=persons, signatures=signatures, faces=faces,
                            method=method, quantity=quantity, room_id=room_id)
        if event_epoch is not None:
            h.epoch = float(event_epoch)
            h.ts = models.now_ts()
        problems = h.check()
        self.store.save_handover(h)

        alarms = []
        for code, detail in problems:
            alarms.append(self.hub.raise_alarm(
                code, detail="交接单 %s：%s" % (h.handover_id, detail),
                source_kind="flow", source_id=h.handover_id,
                scope=room_id or bag_id, event_epoch=h.epoch))

        bag = self._bag(bag_id)
        self._touch_bag(bag, stage, operator or from_person,
                        "交接给 %s（%s）" % (to_person or "?", method))
        self.store.log("试卷交接", "%s %s->%s 方法=%s 完整=%s"
                       % (bag_id, from_person or "-", to_person or "-", method,
                          h.complete()), operator)
        return h, alarms

    def pickup(self, bag_id, person, room_id="", persons=None, faces=None,
               method="rfid", event_epoch=None, operator="system"):
        """从保密室领出：本质也是交接（保密室 -> 押运/送卷人）。"""
        return self.handover(bag_id, from_person="保密室", to_person=person,
                             persons=persons or ["保密室", person],
                             faces=faces, method=method,
                             quantity=self._qty(bag_id), room_id=room_id,
                             stage="pickup", event_epoch=event_epoch,
                             operator=operator)

    def _qty(self, bag_id):
        b = self.store.get_bag(bag_id)
        return b.quantity if b else 0

    def completeness_rate(self, limit=1000, room_id=None):
        """交接记录完整率：可追溯（人/时/物/地点）的记录占比。

        room_id 为空时统计全量；给了房间就只统计该房间的交接
        —— 否则拿"别的房间有记录"来回答"这个房间完不完整"是错的。
        """
        rows = self.store.handovers(limit=limit)
        if room_id:
            rows = [h for h in rows if (h.room_id or "") == room_id]
        if not rows:
            return None, 0, 0
        ok = 0
        for h in rows:
            if h.complete():
                ok += 1
        return ok / float(len(rows)), ok, len(rows)

    def records(self, bag_id=None, limit=500):
        return self.store.handovers(bag_id, limit)


# ==================================================================
# 电子铅封
# ==================================================================
class SealFlow(FlowBase):
    """试卷袋电子封签。异常开启即时告警。"""

    def seal(self, bag_id, seal_id, person, room_id="", event_epoch=None):
        bag = self._bag(bag_id)
        bag.seal_id = seal_id
        bag.seal_state = "sealed"
        bag.seal_at = models.now_ts()
        bag.seal_by = person
        if room_id:
            bag.room_id = room_id
        self._touch_bag(bag, "seal", person, "电子封签 %s" % seal_id)
        self.store.log("电子封签", "%s 封签 %s（%s）" % (bag_id, seal_id, person), person)
        return bag

    def report(self, bag_id, state, seal_id="", person="", detail="",
               event_epoch=None):
        """上报封签状态。state='broken' 即异常开启，立即告警。"""
        if state not in config.SEAL_STATES:
            raise ValueError("未知封签状态：%s" % state)
        bag = self._bag(bag_id)
        bag.seal_state = state
        if seal_id:
            bag.seal_id = seal_id
        self._touch_bag(bag, bag.current_stage or "escort", person,
                        "封签状态 -> %s %s" % (state, detail))
        alarm = None
        if state == "broken" and config.SEAL_ALERT_ON_ABNORMAL_OPEN:
            alarm = self.hub.raise_alarm(
                "SEAL_BROKEN",
                detail="试卷袋 %s 电子铅封异常开启%s（封签号 %s）"
                       % (bag_id, ("：" + detail) if detail else "",
                          seal_id or bag.seal_id or "未知"),
                source_kind="flow", source_id=bag_id, scope=bag.room_id,
                event_epoch=event_epoch)
        self.store.log("封签状态", "%s -> %s（%s）" % (bag_id, state, person), person)
        return bag, alarm

    def intact_bags(self):
        return [b for b in self.store.bags() if b.seal_state == "sealed"]

    def broken_bags(self):
        return [b for b in self.store.bags() if b.seal_state == "broken"]


# ==================================================================
# 押运
# ==================================================================
class EscortFlow(FlowBase):
    """车辆北斗/GPS 定位 + 车内视频回传 + 路线偏离告警（≤30 秒）。"""

    def start_task(self, plate, driver="", escort_persons=None, bag_ids=None,
                   route=None, video_url="", operator="system"):
        if not plate:
            raise ValueError("押运车辆车牌不能为空")
        if not escort_persons:
            raise ValueError("押运必须登记押运人员（至少 1 人）")
        t = models.EscortTask(plate, driver=driver, escort_persons=escort_persons,
                              bag_ids=bag_ids, route=route, video_url=video_url)
        self.store.save_escort_task(t)
        for bid in (bag_ids or []):
            bag = self._bag(bid)
            self._touch_bag(bag, "escort", operator or driver,
                            "装车押运（车 %s）" % plate)
        self.store.log("押运开始", "车 %s 驾驶员 %s 试卷袋 %s"
                       % (plate, driver, "、".join(bag_ids or []) or "-"), operator)
        return t

    def report_position(self, task_id, lat, lng, speed_kmh=0.0, source="beidou",
                        event_epoch=None):
        """上报一个轨迹点。

        返回 (EscortPoint, deviation_alarm|None)。
        偏离判定：点到规划路线距离 > ESCORT_DEVIATE_METERS 即告警，
        告警时延按 event_epoch 记（文档要求 ≤30 秒）。
        """
        t = self.store.get_escort_task(task_id)
        if t is None:
            raise ValueError("押运任务不存在：%s" % task_id)
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        p = models.EscortPoint(task_id, lat, lng, speed_kmh=speed_kmh,
                               source=source, epoch=epoch)
        offset = point_to_polyline_m(float(lat), float(lng), t.route)
        p.offset_m = None if offset is None else round(offset, 1)
        p.deviated = bool(offset is not None and offset > config.ESCORT_DEVIATE_METERS)
        self.store.save_escort_point(p)

        alarm = None
        if p.deviated:
            alarm = self.hub.raise_alarm(
                "ESCORT_ROUTE_DEVIATE",
                detail="押运车辆 %s 偏离规划路线 %.0f 米（阈值 %.0f 米），"
                       "当前位置 %.5f,%.5f（定位源 %s）"
                       % (t.plate, offset, config.ESCORT_DEVIATE_METERS, lat, lng,
                          source),
                source_kind="flow", source_id=t.task_id, scope=t.plate,
                event_epoch=epoch)
            if t.status == "running":
                t.status = "abnormal"
                self.store.save_escort_task(t)
        return p, alarm

    def report_video(self, task_id, online, event_epoch=None):
        """车内视频回传状态。中断即红色告警。"""
        t = self.store.get_escort_task(task_id)
        if t is None:
            raise ValueError("押运任务不存在：%s" % task_id)
        t.video_online = bool(online)
        self.store.save_escort_task(t)
        if config.ESCORT_VIDEO_REQUIRED and not online:
            return self.hub.raise_alarm(
                "ESCORT_VIDEO_LOST",
                detail="押运车辆 %s 车内视频回传中断（应保持在线）" % t.plate,
                source_kind="flow", source_id=t.task_id, scope=t.plate,
                event_epoch=event_epoch)
        return None

    def check_stop(self, task_id, event_epoch=None):
        """途中静止过久告警：用轨迹点的速度判断。"""
        t = self.store.get_escort_task(task_id)
        if t is None:
            return None
        pts = self.store.escort_points(task_id)
        if len(pts) < 2:
            return None
        stop_start, last = None, pts[0]
        for p in pts[1:]:
            if p.speed_kmh <= 1.0:
                if stop_start is None:
                    stop_start = last.epoch
            else:
                stop_start = None
            last = p
        if stop_start is None:
            return None
        dur = last.epoch - stop_start
        if dur > config.ESCORT_STOP_MAX_SEC:
            return self.hub.raise_alarm(
                "ESCORT_STOP_TOO_LONG",
                detail="押运车辆 %s 途中静止 %.0f 分钟（超过 %.0f 分钟）"
                       % (t.plate, dur / 60.0, config.ESCORT_STOP_MAX_SEC / 60.0),
                source_kind="flow", source_id=t.task_id, scope=t.plate,
                event_epoch=event_epoch or last.epoch)
        return None

    def finish_task(self, task_id, status="done", operator="system"):
        t = self.store.get_escort_task(task_id)
        if t is None:
            return None
        t.end_ts = models.now_ts()
        t.status = status
        self.store.save_escort_task(t)
        for bid in t.bag_ids:
            bag = self._bag(bid)
            self._touch_bag(bag, "deliver", operator, "押运抵达（车 %s）" % t.plate)
        self.store.log("押运结束", "任务 %s 状态 %s" % (task_id, status), operator)
        return t

    def task_summary(self, task_id):
        t = self.store.get_escort_task(task_id)
        if t is None:
            return None
        pts = self.store.escort_points(task_id)
        dev = [p for p in pts if p.deviated]
        return {
            "task_id": task_id, "plate": t.plate, "status": t.status,
            "points": len(pts), "deviated_points": len(dev),
            "max_offset_m": max([p.offset_m or 0.0 for p in pts], default=0.0),
            "video_online": t.video_online,
            "route_len": len(t.route), "bags": t.bag_ids,
        }

    def tasks(self, status=None):
        return self.store.escort_tasks(status)


# ==================================================================
# 分发至考场
# ==================================================================
class DeliveryFlow(FlowBase):
    """分发记录与考场、时间、责任人一一对应。"""

    def deliver(self, bag_id, room_id, quantity, person, note="",
                event_epoch=None):
        d = models.Delivery(bag_id, room_id, quantity, person, note=note)
        if event_epoch is not None:
            d.epoch = float(event_epoch)
        problems = d.problems()
        self.store.save_delivery(d)
        alarms = []
        for p in problems:
            alarms.append(self.hub.raise_alarm(
                "DELIVER_RECORD_MISS",
                detail="分发记录不完整：%s（袋 %s -> 考场 %s）"
                       % (p, bag_id or "?", room_id or "?"),
                source_kind="flow", source_id=d.delivery_id,
                scope=room_id or bag_id, event_epoch=d.epoch))
        bag = self._bag(bag_id)
        if bag is not None and quantity:
            bag.room_id = room_id
        self._touch_bag(bag, "deliver", person,
                        "分发至考场 %s，%d 份" % (room_id, quantity))
        self.store.log("分发至考场", "%s -> %s %d 份（%s）"
                       % (bag_id, room_id, quantity, person), person)
        return d, alarms

    def records(self, bag_id=None, limit=500):
        return self.store.deliveries(bag_id, limit)

    def covered(self, room_id=None, limit=1000):
        """一一对应核查：每条分发记录都有人/时/考场/数量。"""
        rows = self.store.deliveries(None, limit)
        if room_id:
            rows = [r for r in rows if r.room_id == room_id]
        bad = [r for r in rows if r.problems()]
        return {"total": len(rows), "bad": len(bad),
                "ok": not bad,
                "bad_ids": [r.delivery_id for r in bad]}


# ==================================================================
# 回收与封存
# ==================================================================
class RecycleFlow(FlowBase):
    """回收扫码核验数量，异常差异即时上报；回收后封存。"""

    def recycle(self, bag_id, room_id, issued_qty, returned_qty, person="",
                seal_id="", auto_seal=True, note="", event_epoch=None):
        sealed = bool(auto_seal and seal_id)
        r = models.Recycle(bag_id, room_id, issued_qty, returned_qty, person=person,
                           sealed=sealed, seal_id=seal_id, note=note)
        if event_epoch is not None:
            r.epoch = float(event_epoch)
        problems = r.problems()
        self.store.save_recycle(r)
        alarms = []
        for code, detail in problems:
            alarms.append(self.hub.raise_alarm(
                code, detail="回收核验（袋 %s / 考场 %s）：%s"
                             % (bag_id, room_id, detail),
                source_kind="flow", source_id=r.recycle_id,
                scope=room_id or bag_id, event_epoch=r.epoch))
        bag = self._bag(bag_id)
        self._touch_bag(bag, "recycle", person,
                        "回收 %d/%d 份%s" % (returned_qty, issued_qty,
                                            "，已封存" if sealed else ""))
        if sealed and bag is not None:
            bag.seal_id = seal_id
            bag.seal_state = "sealed"
            bag.seal_at = models.now_ts()
            bag.seal_by = person
            self.store.save_bag(bag)
        self.store.log("回收封存", "%s 回收 %d/%d 差异 %+d"
                       % (bag_id, returned_qty, issued_qty, r.diff), person)
        return r, alarms

    def records(self, bag_id=None, limit=500):
        return self.store.recycles(bag_id, limit)

    def diff_rate(self, limit=1000):
        """回收差异率：差异记录占比（用于报告如实呈现）。"""
        rows = self.store.recycles(None, limit)
        if not rows:
            return None, 0, 0
        bad = sum(1 for r in rows if r.diff != 0)
        return bad / float(len(rows)), bad, len(rows)


# ==================================================================
# 流转总门面
# ==================================================================
class FlowManager(object):
    """试卷流转全环节总入口。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)
        self.handover = HandoverFlow(store, self.hub)
        self.seal = SealFlow(store, self.hub)
        self.escort = EscortFlow(store, self.hub)
        self.delivery = DeliveryFlow(store, self.hub)
        self.recycle = RecycleFlow(store, self.hub)

    # ---------- 试卷袋 ----------
    def register_bag(self, bag_id, rfid="", bundle_id="", subject="", quantity=0,
                     room_id="", operator="system"):
        b = models.Bag(bag_id, rfid=rfid or bag_id, bundle_id=bundle_id,
                       subject=subject, quantity=quantity, room_id=room_id)
        self.store.save_bag(b)
        self.store.log("登记试卷袋", "%s 科目=%s 数量=%d" % (bag_id, subject, quantity),
                       operator)
        return b

    def bags(self, room_id=None):
        return self.store.bags(room_id)

    def bag_trace(self, bag_id):
        """一个袋子的全链条追溯（人/时/物/地点）。"""
        b = self.store.get_bag(bag_id)
        if b is None:
            return None
        return {
            "bag": b,
            "history": b.history,
            "handovers": self.store.handovers(bag_id),
            "deliveries": self.store.deliveries(bag_id),
            "recycles": self.store.recycles(bag_id),
            "seal_state": b.seal_state,
        }

    # ---------- 指标 ----------
    def stage_coverage(self):
        """各环节是否有记录（用于报告"每个环节都有痕迹"）。"""
        out = {}
        for key, meta in config.FLOW_STAGES.items():
            out[key] = {"name": meta["name"], "count": 0}
        out["handover"]["count"] = len(self.store.handovers(limit=10000))
        out["pickup"]["count"] = len([h for h in self.store.handovers(limit=10000)
                                      if h.stage == "pickup"])
        out["deliver"]["count"] = len(self.store.deliveries(limit=10000))
        out["recycle"]["count"] = len(self.store.recycles(limit=10000))
        out["seal"]["count"] = len([b for b in self.store.bags()
                                    if b.seal_state == "sealed"])
        out["escort"]["count"] = len(self.store.escort_tasks())
        return out
