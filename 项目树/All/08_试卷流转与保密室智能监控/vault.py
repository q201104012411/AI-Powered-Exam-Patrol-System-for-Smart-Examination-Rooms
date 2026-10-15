# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 一、保密室智能监控

逐条实现《试卷流转与保密室智能监控.md》第一节表格：

| 监控项         | 实现方式                                                 |
| 无死角视频     | CameraManager：六面覆盖登记 + 基准图 + 双路冗余 + 覆盖率 |
| 门禁管控       | AccessControl：人脸+刷卡/密码双因子 + 双人双锁 + 尾随     |
| 入侵探测       | IntrusionMonitor：红外双鉴/门窗磁/震动/开柜 + 布防时段    |
| 试卷柜电子锁   | SafeMonitor：电子锁 + 开柜记录 + 柜内状态监测            |
| 环境监测       | EnvMonitor：温湿度/水浸/烟感，异常自动告警                |
| 值守替代       | PatrolAssistant：巡考机器人定时巡查 + 非接触值守          |
| 录像留存       | RecordGuard：留存期/WORM/连续性/可回放                    |

所有"事件"都走 _emit()：先落库，再由 AlarmHub 产生告警，
并把**事件时刻**传给告警中心以便算时延（≤5 秒指标）。
"""
import os
import time

import cv2
import numpy as np

import config
import models
from alarm_hub import AlarmHub


# ==================================================================
# 机位管理（无死角视频 + 实时画面 + 抓帧留证）
# ==================================================================
class CameraManager(object):
    """机位台账 + 取流。界面用它拿实时画面，告警中心用它抓帧留证。

    取流走 camera_stream.FrameReader（后台读帧 + 看门狗 + 自动重连，
    永不阻塞界面线程）。本模块只做"多路机位注册与轮询"。
    """

    def __init__(self, store, log=None):
        self.store = store
        self._log = log or (lambda m: None)
        self._readers = {}
        self._snapshot_dir = config.EVIDENCE_DIR

    # ---------- 台账 ----------
    def register(self, cam_id, room_id, name, face, url="", angle_deg=0.0,
                 checkpoints=None, channels=None, ledger=None, operator="system"):
        if face not in config.COVER_FACES:
            raise ValueError("机位朝向必须是六面之一：%s"
                             % "、".join(config.FACE_TEXT[f] for f in config.COVER_FACES))
        cam = models.Camera(cam_id, room_id, name, face, url=url, angle_deg=angle_deg,
                            checkpoints=checkpoints, channels=channels, ledger=ledger)
        self.store.save_camera(cam)
        self.store.log("登记机位", "%s %s %s" % (cam_id, name, config.FACE_TEXT[face]),
                       operator)
        return cam

    def remove(self, cam_id, operator="system"):
        self.stop(cam_id)
        self.store.delete_camera(cam_id)
        self.store.log("删除机位", cam_id, operator)

    def all(self, room_id=None):
        return self.store.cameras(room_id)

    # ---------- 六面覆盖率 ----------
    def coverage(self, room_id):
        """返回 (覆盖率, 明细)。覆盖率 = 在线且六面都有登记 的面数 / 6。

        文档要求"100%，无盲区（逐机位留档验证）"，所以：
          · 每一面都必须有登记机位
          · 该机位还必须配置了取流地址（否则是"画在纸上的覆盖"）
          · 覆盖率不达标要单独报 COVER_RATE_LOW
        """
        cams = self.store.cameras(room_id)
        detail = {}
        for f in config.COVER_FACES:
            face_cams = [c for c in cams if c.face == f]
            configured = [c for c in face_cams if c.url]
            detail[f] = {
                "face": f, "face_text": config.FACE_TEXT[f],
                "total": len(face_cams),
                "configured": len(configured),
                "missing_checkpoints": self._missing_checkpoints(face_cams),
                "ledger_problems": [p for c in face_cams for p in c.ledger_problems()],
                "cams": [c.cam_id for c in face_cams],
            }
        covered = sum(1 for f in config.COVER_FACES if detail[f]["configured"] > 0)
        rate = covered / float(len(config.COVER_FACES))
        return rate, detail

    @staticmethod
    def _missing_checkpoints(face_cams):
        if not face_cams:
            return list(config.FACE_CHECKPOINTS.get("", []))
        face = face_cams[0].face
        need = config.FACE_CHECKPOINTS.get(face, [])
        have = set()
        for c in face_cams:
            have.update(c.checkpoints)
        return [n for n in need if n not in have]

    def blind_spots(self, room_id):
        """返回该保密室所有未覆盖/未配置的朝向（盲区）。"""
        _, detail = self.coverage(room_id)
        return [config.FACE_TEXT[f] for f in config.COVER_FACES
                if detail[f]["configured"] == 0]

    # ---------- 取流 ----------
    def start(self, cam_id, width=0, height=0, fps=0):
        cam = self.store.get_camera(cam_id)
        if cam is None:
            return False, "机位不存在"
        if not cam.url:
            return False, "未配置取流地址"
        if cam_id in self._readers:
            r = self._readers[cam_id]
            if r.running:
                return True, "已在取流"
            r.stop()
        import camera_stream
        r = camera_stream.FrameReader(cam.url, width=width, height=height, fps=fps,
                                      label="%s(%s)" % (cam.name, cam_id))
        r.start()
        self._readers[cam_id] = r
        self.store.set_camera_online(cam_id, True)
        self.store.log("开始取流", "%s %s" % (cam_id, cam.url), "camera_manager")
        return True, "已开始取流"

    def stop(self, cam_id):
        r = self._readers.pop(cam_id, None)
        if r is not None:
            r.stop()
        self.store.set_camera_online(cam_id, False)
        return True

    def stop_all(self):
        for cid in list(self._readers.keys()):
            self.stop(cid)

    def running_ids(self):
        return [cid for cid, r in self._readers.items() if r.running]

    def snapshot_state(self, cam_id):
        """非阻塞地拿一路的最新状态：{"state","frame","fps","status","reconnects"}"""
        r = self._readers.get(cam_id)
        if r is None:
            return {"state": "stopped", "frame": None, "fps": 0.0,
                    "status": "未开始取流", "reconnects": 0, "seq": 0}
        return r.snapshot()

    def probe_local(self, max_index=None):
        """探测本机摄像头可用序号（0/1/2…），用于界面"连接摄像头"。"""
        max_index = max_index or config.LOCAL_CAM_INDEX_MAX
        found = []
        for i in range(max_index):
            cap = None
            try:
                cap = cv2.VideoCapture(i)
                if cap is not None and cap.isOpened():
                    ok = False
                    try:
                        ok, frame = cap.read()
                        ok = bool(ok) and frame is not None
                    except Exception:
                        ok = False
                    if ok:
                        found.append(i)
            except Exception:
                pass
            finally:
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
        return found

    # ---------- 抓帧留证 ----------
    def snapshot(self, cam_id, reason="alarm", frame=None):
        """把一路当前画面存成留证图片，返回相对路径（失败返回空串）。"""
        if frame is None:
            st = self.snapshot_state(cam_id)
            frame = st.get("frame")
        if frame is None:
            cam = self.store.get_camera(cam_id)
            if cam is None or not cam.url:
                return ""
            # 还没开始取流：临时打开抓一帧（带超时，不卡界面）
            import camera_stream
            cap, info = camera_stream.open_capture(cam.url, timeout=4.0)
            if cap is None:
                return ""
            try:
                ok, frame = cap.read()
                if not ok:
                    frame = None
            finally:
                camera_stream.release_async(cap, "抓帧留证")
        if frame is None:
            return ""
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            name = "%s_%s_%s.jpg" % (
                time.strftime("%Y%m%d_%H%M%S"), cam_id,
                "".join(ch for ch in str(reason) if ch.isalnum() or ch == "_")[:40])
            path = self._snapshot_dir / name
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return ""
            path.write_bytes(buf.tobytes())
            rel = os.path.relpath(str(path), str(config.DATA_ROOT))
            return rel
        except Exception:
            return ""


# ==================================================================
# 门禁管控
# ==================================================================
class AccessControl(object):
    """人脸 + 刷卡/密码 双因子；双人双锁；尾随检测；开柜/开门告警 ≤5 秒。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)

    def _emit(self, code, detail, room_id, source_id="", event_epoch=None, level=None):
        return self.hub.raise_alarm(code, detail=detail, source_kind="door",
                                    source_id=source_id or room_id, scope=room_id,
                                    level=level, event_epoch=event_epoch)

    # ---------- 因子校验 ----------
    @staticmethod
    def _has_biometric(factors):
        return any(f in config.FACTOR_BIOMETRIC for f in factors)

    @staticmethod
    def _has_credential(factors):
        return any(f in config.FACTOR_CREDENTIAL for f in factors)

    def factor_ok(self, factors):
        """双因子：必须同时含 1 个生物/人脸 + 1 个凭证。"""
        return self._has_biometric(factors) and self._has_credential(factors)

    # ---------- 开门请求 ----------
    def request_open(self, room_id, person, factors, persons=None, tailgating=False,
                     event_epoch=None):
        """一次门禁开门请求。返回 (允许?, AccessEvent)。

        判定顺序：双因子 -> 双人双锁 -> （可选）尾随
        任何一步不过：拒绝 + 立即告警（时延按 event_epoch 算）。
        """
        factors = list(factors or [])
        persons = list(persons or [person])
        epoch = event_epoch if event_epoch is not None else models.now_epoch()

        if not self.factor_ok(factors):
            ev = models.AccessEvent(room_id, person, factors=factors, persons=persons,
                                    result="deny",
                                    deny_reason="双因子校验未通过（需人脸 + 刷卡/密码）",
                                    epoch=epoch)
            self.store.save_access(ev)
            self._emit("ACCESS_DUAL_FAIL",
                       "人员 %s 门禁双因子校验失败（因子：%s）"
                       % (person, "、".join(factors) or "无"),
                       room_id, source_id=ev.event_id, event_epoch=epoch)
            return False, ev

        if len(persons) < config.DUAL_CUSTODY_REQUIRED:
            ev = models.AccessEvent(room_id, person, factors=factors, persons=persons,
                                    result="deny",
                                    deny_reason="双人双锁未满足（到场 %d 人，需 %d 人）"
                                                % (len(persons),
                                                   config.DUAL_CUSTODY_REQUIRED),
                                    epoch=epoch)
            self.store.save_access(ev)
            self._emit("ACCESS_DUAL_FAIL",
                       "保密室双人双锁未满足：%s 独自申请开门（到场 %d 人）"
                       % (person, len(persons)),
                       room_id, source_id=ev.event_id, event_epoch=epoch)
            return False, ev

        ev = models.AccessEvent(room_id, person, factors=factors, persons=persons,
                                result="allow", epoch=epoch)
        self.store.save_access(ev)
        self._emit("DOOR_OPEN_NORMAL",
                   "授权开门：%s（双人：%s）"
                   % (person, "、".join(persons)),
                   room_id, source_id=ev.event_id, event_epoch=epoch)

        if tailgating:
            self._emit("ACCESS_TAILGATING",
                       "检测到疑似尾随进入（授权 %d 人，画面内人数超限）" % len(persons),
                       room_id, source_id=ev.event_id, event_epoch=epoch)
        return True, ev

    # ---------- 开门告警（未授权/暴力开启） ----------
    def report_abnormal_open(self, room_id, person="", detail="", event_epoch=None):
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        ev = models.AccessEvent(room_id, person, factors=[], persons=[],
                                result="deny", deny_reason=detail or "未授权开门",
                                epoch=epoch)
        self.store.save_access(ev)
        return self._emit("DOOR_OPEN_ABNORMAL",
                          detail or "保密室门被未授权开启（无有效双因子记录）",
                          room_id, source_id=ev.event_id, event_epoch=epoch)

    def events(self, room_id=None, limit=200):
        return self.store.access_events(room_id, limit)


# ==================================================================
# 入侵探测
# ==================================================================
class IntrusionMonitor(object):
    """红外双鉴 / 门窗磁 / 震动 / 开柜。布防时段决定是否强告警。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)
        self._armed = {}          # room_id -> bool（人工强制布防/撤防覆盖）

    # ---------- 布防时段 ----------
    @staticmethod
    def in_armed_window(epoch=None):
        lt = time.localtime(epoch if epoch is not None else time.time())
        h = lt.tm_hour
        for h1, m1, h2, m2 in config.ARMED_WINDOWS:
            start, end = h1 * 60 + m1, h2 * 60 + m2
            cur = h * 60 + lt.tm_min
            if start <= end:
                if start <= cur < end:
                    return True
            else:                                  # 跨零点（如 18:00-24:00 与 00:00-08:00 已拆开，这里兜底跨段）
                if cur >= start or cur < end:
                    return True
        return False

    def is_armed(self, room_id):
        if room_id in self._armed:
            return self._armed[room_id]
        return self.in_armed_window()

    def set_armed(self, room_id, armed, operator="system"):
        self._armed[room_id] = bool(armed)
        self.store.log("布防" if armed else "撤防", room_id, operator)
        return self._armed[room_id]

    def armed_rooms(self):
        return dict(self._armed)

    # ---------- 触发 ----------
    SCENARIO = {
        "ir_dual":    ("INTRUSION_IR", "红外双鉴"),
        "door_mag":   ("INTRUSION_DOOR_MAG", "门窗磁"),
        "vibration":  ("INTRUSION_VIBRATION", "震动"),
        "safe_open":  ("SAFE_OPEN_ABNORMAL", "开柜检测"),
    }

    def trigger(self, room_id, kind, sensor_id="", zone="", event_epoch=None,
                armed=None):
        """一次入侵探测触发。返回 (IntrusionEvent, Alarm|None)。"""
        if kind not in config.INTRUSION_KINDS:
            raise ValueError("未知入侵探测类型：%s" % kind)
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        if armed is None:
            armed = self.is_armed(room_id)
        ev = models.IntrusionEvent(room_id, kind, sensor_id=sensor_id, zone=zone,
                                   epoch=epoch, armed=armed)
        self.store.save_intrusion(ev)

        code, text = self.SCENARIO[kind]
        alarm = None
        if armed:
            # 强布防时段：按码的兜底级别（多为红色）立即告警
            alarm = self.hub.raise_alarm(
                code,
                detail="%s触发：%s%s（%s）" % (
                    text, config.INTRUSION_KINDS[kind],
                    ("，防区 " + zone) if zone else "",
                    "布防中" if armed else "已撤防"),
                source_kind="intrusion", source_id=ev.event_id, scope=room_id,
                event_epoch=epoch)
        else:
            # 撤防时段：只留痕，不产生红色告警（但开柜检测无论如何都报）
            if kind == "safe_open":
                alarm = self.hub.raise_alarm(
                    code, detail="撤防时段检测到开柜动作：%s" % (zone or sensor_id or "-"),
                    source_kind="intrusion", source_id=ev.event_id, scope=room_id,
                    event_epoch=epoch)
        return ev, alarm

    def events(self, room_id=None, limit=200):
        return self.store.intrusion_events(room_id, limit)


# ==================================================================
# 试卷柜电子锁
# ==================================================================
class SafeMonitor(object):
    """电子锁 + 开柜记录 + 柜内状态监测（paper_present/tamper/humidity）。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)

    def open_request(self, room_id, safe_id, person, authorized, inner=None,
                     event_epoch=None):
        """开柜动作。authorized=False 即"未授权开柜"，红色告警。"""
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        ev = models.SafeEvent(room_id, safe_id, person=person, state="open",
                              authorized=authorized, inner=inner, epoch=epoch)
        self.store.save_safe_event(ev)
        alarms = []
        if authorized:
            alarms.append(self.hub.raise_alarm(
                "SAFE_OPEN_NORMAL",
                detail="授权开柜：%s 开启试卷柜 %s" % (person, safe_id),
                source_kind="safe", source_id=ev.event_id, scope=room_id,
                event_epoch=epoch))
        else:
            alarms.append(self.hub.raise_alarm(
                "SAFE_OPEN_ABNORMAL",
                detail="未授权开柜：试卷柜 %s 被开启（操作人登记：%s）"
                       % (safe_id, person or "未知"),
                source_kind="safe", source_id=ev.event_id, scope=room_id,
                event_epoch=epoch))
        # 柜内状态监测
        for a in self.check_inner(room_id, safe_id, inner or {}, event_epoch=epoch):
            alarms.append(a)
        return ev, alarms

    def report_state(self, room_id, safe_id, state, person="", inner=None,
                     event_epoch=None):
        """上报柜门状态（关/开/虚掩）。虚掩要提醒。"""
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        if state not in config.SAFE_STATES:
            raise ValueError("未知柜门状态：%s" % state)
        ev = models.SafeEvent(room_id, safe_id, person=person, state=state,
                              authorized=True, inner=inner, epoch=epoch)
        self.store.save_safe_event(ev)
        alarms = []
        if state == "ajar":
            alarms.append(self.hub.raise_alarm(
                "SAFE_AJAR", detail="试卷柜 %s 门虚掩未关严" % safe_id,
                source_kind="safe", source_id=ev.event_id, scope=room_id,
                event_epoch=epoch))
        for a in self.check_inner(room_id, safe_id, inner or {}, event_epoch=epoch):
            alarms.append(a)
        return ev, alarms

    def check_inner(self, room_id, safe_id, inner, event_epoch=None):
        """柜内状态监测：纸张缺失 / 被撬 / 柜内湿度异常。"""
        out = []
        if inner.get("paper_present") is False:
            out.append(self.hub.raise_alarm(
                "SAFE_OPEN_ABNORMAL",
                detail="试卷柜 %s 柜内未检测到试卷（paper_present=False）" % safe_id,
                source_kind="safe", source_id=safe_id, scope=room_id,
                event_epoch=event_epoch))
        if inner.get("tamper") is True:
            out.append(self.hub.raise_alarm(
                "SAFE_OPEN_ABNORMAL",
                detail="试卷柜 %s 检测到撬动/拆解（tamper=True）" % safe_id,
                source_kind="safe", source_id=safe_id, scope=room_id,
                event_epoch=event_epoch))
        h = inner.get("humidity")
        if h is not None:
            try:
                hv = float(h)
                lim = config.ENV_THRESHOLDS["humidity_pct"]
                if hv > lim["max"] or hv < lim["min"]:
                    out.append(self.hub.raise_alarm(
                        "ENV_HUMID_HIGH" if hv > lim["max"] else "ENV_HUMID_LOW",
                        detail="试卷柜 %s 柜内湿度 %.1f%% 越限（%.0f%%~%.0f%%）"
                               % (safe_id, hv, lim["min"], lim["max"]),
                        source_kind="safe", source_id=safe_id, scope=room_id,
                        event_epoch=event_epoch))
            except (TypeError, ValueError):
                pass
        return out

    def last_state(self, room_id, safe_id):
        for e in self.store.safe_events(room_id, limit=200):
            if e.safe_id == safe_id:
                return e
        return None

    def events(self, room_id=None, limit=200):
        return self.store.safe_events(room_id, limit)


# ==================================================================
# 环境监测
# ==================================================================
class EnvMonitor(object):
    """温湿度 / 水浸 / 烟感。异常自动告警。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)

    def sample(self, room_id, temp_c=25.0, humidity_pct=45.0, water=False,
               smoke=False, event_epoch=None):
        """采一次样：落库 + 逐项越限判定 + 告警。"""
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        s = models.EnvSample(room_id, temp_c=temp_c, humidity_pct=humidity_pct,
                             water=water, smoke=smoke, epoch=epoch)
        self.store.save_env(s)
        alarms = []
        for code, detail in s.violations():
            alarms.append(self.hub.raise_alarm(
                code, detail=detail, source_kind="env", source_id=s.sample_id,
                scope=room_id, event_epoch=epoch))
        return s, alarms

    def latest(self, room_id):
        rows = self.store.env_samples(room_id, limit=1)
        return rows[0] if rows else None

    def samples(self, room_id=None, limit=200):
        return self.store.env_samples(room_id, limit)


# ==================================================================
# 值守替代（巡考机器人定时巡查 + 非接触值守）
# ==================================================================
class PatrolAssistant(object):
    """巡考机器人定时巡查 + 非接触值守。

    文档"值守替代：巡考机器人定时巡查 + 非接触值守（见专题 04）"。
    本模块只做**本专题需要的部分**：把每次巡查/值守轮次落成记录，
    并在"该到未到"时告警（值守缺位），以及提供"非接触值守"的
    画面巡检结论（复用 CameraManager 的抓帧）。
    """

    def __init__(self, store, cameras=None, hub=None):
        self.store = store
        self.cameras = cameras
        self.hub = hub or AlarmHub(store, cameras)
        self._rounds = {}          # room_id -> [{"ts","epoch","by","result"}]

    def patrol_round(self, room_id, by="patrol_robot", result="ok", note="",
                     snapshot_cam=None, event_epoch=None):
        epoch = event_epoch if event_epoch is not None else models.now_epoch()
        rec = {"ts": models.now_ts(), "epoch": epoch, "by": by,
               "result": result, "note": note}
        if snapshot_cam and self.cameras is not None:
            rec["evidence"] = self.cameras.snapshot(snapshot_cam, reason="patrol")
        self._rounds.setdefault(room_id, []).append(rec)
        self.store.log("机器人巡查", "%s 结果=%s %s" % (room_id, result, note), by)
        return rec

    def rounds(self, room_id):
        return list(self._rounds.get(room_id, []))

    def last_round_epoch(self, room_id):
        rs = self._rounds.get(room_id)
        return rs[-1]["epoch"] if rs else None

    def check_due(self, room_id, interval_sec=1800, event_epoch=None):
        """该到未到就报值守缺位。文档要求"巡更完成率 ≥99%"（见专题 04），
        本专题只需回答"值守有没有断"。"""
        last = self.last_round_epoch(room_id)
        now = event_epoch if event_epoch is not None else models.now_epoch()
        if last is None or (now - last) > interval_sec:
            return self.hub.raise_alarm(
                "FLOW_ABNORMAL",
                detail="保密室 %s 超过 %.0f 分钟无机器人巡查记录（疑似值守缺位）"
                       % (room_id, interval_sec / 60.0),
                source_kind="video", source_id=room_id, scope=room_id,
                event_epoch=now)
        return None


# ==================================================================
# 录像留存与完整性
# ==================================================================
class RecordGuard(object):
    """录像留存（≥6 个月，WORM 不可删改）+ 连续性/可回放核查。"""

    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub or AlarmHub(store)

    # ---------- 段登记 ----------
    def add_segment(self, cam_id, start_epoch, duration_sec, channel="main",
                    playable=True):
        seg = models.RecordSegment(cam_id, start_epoch, duration_sec,
                                   channel=channel, playable=playable)
        self.store.save_segment(seg)
        return seg

    def channels(self, cam_id):
        return sorted({s.channel for s in self.store.segments(cam_id=cam_id)})

    def redundancy_ok(self, cam_id):
        """双路冗余：该机位必须登记 >= 2 路通道，且每路都有段。"""
        chs = [c for c in self.channels(cam_id) if self.store.segments(cam_id, c)]
        return len(chs) >= config.REDUNDANT_CHANNELS_REQUIRED, chs

    # ---------- 连续性核查 ----------
    def continuity(self, cam_id, channel="main"):
        """逐段核查连续性。返回 {"ok":bool,"gaps":[...],"segments":n,"playable":bool}。

        判据：
          · 段必须可回放（playable）
          · 相邻段之间 start_prev + duration_prev 与 start_next 的缝隙
            不得超过 RECORD_GAP_TOLERANCE_SEC
          · 段必须按时间升序且不重叠
        """
        segs = self.store.segments(cam_id, channel)
        segs.sort(key=lambda s: s.start_epoch)
        gaps, unplayable = [], []
        for s in segs:
            if not s.playable:
                unplayable.append({"seg_id": s.seg_id, "start_ts": s.start_ts,
                                   "why": "段不可回放"})
        for a, b in zip(segs, segs[1:]):
            gap = b.start_epoch - a.end_epoch()
            if gap > config.RECORD_GAP_TOLERANCE_SEC:
                gaps.append({"after": a.start_ts, "before": b.start_ts,
                             "gap_sec": round(gap, 2), "channel": channel})
            elif gap < -config.RECORD_GAP_TOLERANCE_SEC:
                gaps.append({"after": a.start_ts, "before": b.start_ts,
                             "gap_sec": round(gap, 2), "channel": channel,
                             "why": "段重叠"})
        ok = not gaps and not unplayable and bool(segs)
        return {"ok": ok, "gaps": gaps, "unplayable": unplayable,
                "segments": len(segs), "channel": channel}

    def check_all(self, room_id=None, event_epoch=None):
        """全机位连续性核查，发现问题即告警。返回缺陷清单。"""
        problems = []
        cams = self.store.cameras(room_id)
        for cam in cams:
            chs = self.channels(cam.cam_id) or ["main"]
            for ch in chs:
                r = self.continuity(cam.cam_id, ch)
                if not r["ok"]:
                    problems.append({"cam_id": cam.cam_id, "channel": ch,
                                     "gaps": r["gaps"],
                                     "unplayable": r["unplayable"]})
                    self.hub.raise_alarm(
                        "VIDEO_RECORD_BREAK",
                        detail="机位 %s 通道 %s 录像不连续：缺口 %d 段、不可回放 %d 段"
                               % (cam.cam_id, ch, len(r["gaps"]),
                                  len(r["unplayable"])),
                        source_kind="video", source_id="%s/%s" % (cam.cam_id, ch),
                        scope=cam.room_id, event_epoch=event_epoch)
            ok, chs_present = self.redundancy_ok(cam.cam_id)
            if not ok:
                problems.append({"cam_id": cam.cam_id, "channel": "redundancy",
                                 "why": "双路冗余录像缺失（当前 %d 路）"
                                        % len(chs_present)})
        return problems

    # ---------- 留存期与 WORM ----------
    def retention_report(self, now_epoch=None, cam_id=None):
        """留存核查：留存期内必须连续可回放、且不可删改（locked/WORM）。"""
        now = now_epoch if now_epoch is not None else time.time()
        out = {"retain_days": config.RECORD_RETAIN_DAYS, "worm": config.RECORD_WORM,
               "within_retention": 0, "unlocked": 0, "expired": 0, "total": 0,
               "problem_segs": []}
        for s in self.store.segments(cam_id=cam_id):
            out["total"] += 1
            within = (now - s.start_epoch) <= config.RECORD_RETAIN_DAYS * 86400
            if within:
                out["within_retention"] += 1
                if config.RECORD_WORM and not s.locked:
                    out["unlocked"] += 1
                    out["problem_segs"].append(
                        {"seg_id": s.seg_id, "cam_id": s.cam_id,
                         "why": "留存期内可删改（未加锁）"})
            else:
                out["expired"] += 1
        return out


# ==================================================================
# 汇总门面
# ==================================================================
class Vault(object):
    """保密室智能监控总入口：把上述模块串起来，供界面/网页/自检调用。"""

    def __init__(self, store, hub=None, cameras=None):
        self.store = store
        self.cameras = cameras or CameraManager(store)
        self.hub = hub or AlarmHub(store, self.cameras)
        self.access = AccessControl(store, self.hub)
        self.intrusion = IntrusionMonitor(store, self.hub)
        self.safes = SafeMonitor(store, self.hub)
        self.env = EnvMonitor(store, self.hub)
        self.patrol = PatrolAssistant(store, self.cameras, self.hub)
        self.records = RecordGuard(store, self.hub)

    # ---------- 七项能力体检 ----------
    def self_check(self, room_id):
        """逐项检查该保密室七项监控能力配置是否到位，返回清单。"""
        items = []
        rate, detail = self.cameras.coverage(room_id)
        items.append({
            "key": "coverage", "name": "无死角视频（六面覆盖）",
            "ok": rate >= config.COVER_RATE_REQUIRED,
            "detail": "覆盖率 %.0f%%（要求 100%%）；缺口朝向：%s"
                      % (rate * 100, "、".join(self.cameras.blind_spots(room_id)) or "无"),
        })
        cams = self.store.cameras(room_id)
        redundant = [c.cam_id for c in cams
                     if len(c.channels) < config.REDUNDANT_CHANNELS_REQUIRED]
        items.append({
            "key": "redundant", "name": "双路冗余录像",
            "ok": not redundant,
            "detail": "全部机位已配双路" if not redundant
                      else "以下机位冗余不足：%s" % "、".join(redundant),
        })
        ledger_bad = [(c.cam_id, c.ledger_problems()) for c in cams
                      if c.ledger_problems()]
        items.append({
            "key": "ledger", "name": "机位台账留档",
            "ok": not ledger_bad,
            "detail": "台账齐备" if not ledger_bad
                      else "%d 个机位台账有缺项" % len(ledger_bad),
        })
        acc = self.store.access_events(room_id, limit=1)
        items.append({
            "key": "access", "name": "门禁双因子 + 双人双锁",
            "ok": True,
            "detail": "已启用（要求双因子 %d 项 + 双人到场 %d 人）；最近事件：%s"
                      % (config.ACCESS_FACTORS_REQUIRED,
                         config.DUAL_CUSTODY_REQUIRED,
                         acc[0].ts if acc else "暂无"),
        })
        intr = self.store.intrusion_events(room_id, limit=1)
        items.append({
            "key": "intrusion", "name": "入侵探测",
            "ok": True,
            "detail": "已启用 %s；当前%s；最近事件：%s"
                      % ("、".join(config.INTRUSION_KINDS.values()),
                         "布防中" if self.intrusion.is_armed(room_id) else "已撤防",
                         intr[0].ts if intr else "暂无"),
        })
        last_env = self.env.latest(room_id)
        items.append({
            "key": "env", "name": "环境监测",
            "ok": last_env is not None,
            "detail": ("最近采样 %.1f℃ / %.1f%%（%s）"
                       % (last_env.temp_c, last_env.humidity_pct, last_env.ts))
                      if last_env else "尚无采样数据（需接入传感器）",
        })
        ret = self.records.retention_report()
        items.append({
            "key": "record", "name": "录像留存（≥%d 天，不可删改）"
                                     % config.RECORD_RETAIN_DAYS,
            "ok": ret["total"] > 0 and ret["unlocked"] == 0,
            "detail": "共 %d 段，留存期内 %d 段，未加锁 %d 段"
                      % (ret["total"], ret["within_retention"], ret["unlocked"]),
        })
        return items
