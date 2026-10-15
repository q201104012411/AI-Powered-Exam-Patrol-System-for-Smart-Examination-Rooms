# -*- coding: utf-8 -*-
r"""
13 AI 火场识别与自动报警 · 集成接口（供 00_集成联动 调用）

ops:
  dispatch_fire  {event_id, level, score, reason, camera_name, camera_url, room_id, ...}
        -> 把一条火警上传到 03 智能终端（**带机位画质可信度降权**）：
           火警是人身安全事件，按 code=FIRE_ALARM（红）/ FIRE_SMOKE（橙）送 03；
           若该机位画质不可信（02 的 credibility < 0.6），03 会把红色降为橙色待复核，
           与 02/03 既有的"防画质误报堆积"约定保持一致。
  fire_selfcheck {}  -> 内部跑一次合成火焰演练，返回**真实检出**的火警事件
  status       {}    -> 概览（后端、阈值、事件统计）

用法：
    python integration_api.py --op fire_selfcheck --args "{}"
    python integration_api.py --op dispatch_fire --args "{...火警字段...}"

为什么带 credibility：单张画面的火焰判定受画质影响很大（糊成一片的镜头、
被遮挡的镜头都可能误判），把画质可信度一并交给 03，让终端按同一套约定降级，
而不是火场模块自己"偷偷决定"要不要报。

**刻意不做**的事：本模块不会"收到火警后再回调 01 人脸核验"之类 ——
火警是人身安全事件，处置路径是"通知人 + 留证据"，与考试违规判定无关。
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

ALL_DIR = os.path.dirname(BASE)

#: 03 会在可信度低于该值时把红色降为橙色（与 03/integration_api.py 一致）
DOWNGRADE_BELOW = 0.6


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _find_sibling(name):
    """找同级的兄弟专题目录（本模块要调 02 的可信度接口，但不 import 它的代码）。"""
    cand = os.path.join(ALL_DIR, name)
    if os.path.isdir(cand):
        return cand
    return ""


DIR_02 = _find_sibling("02_摄像头核查与视频质量诊断")
DIR_03 = _find_sibling("03_智能终端操作")


# ============================================================ 子进程调用
def _project_call(key, op, args, timeout=90):
    """
    用子进程 + JSON 调对端的 integration_api.py。

    为什么必须子进程：02/03/13 各自都有 config.py / models.py，
    同进程 import 必然撞名（与 00_集成联动/linkage.py 同一理由）。
    """
    import subprocess
    home = {"02": os.environ.get("EXAM_CAMERA_HOME"),
            "03": os.environ.get("EXAM_TERMINAL_HOME")}.get(key)
    directory = {"02": DIR_02, "03": DIR_03}.get(key, "")
    if not directory or not os.path.isfile(os.path.join(directory, "integration_api.py")):
        return {"ok": False, "reason": "找不到 %s 的 integration_api.py" % key}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if home:
        os.makedirs(home, exist_ok=True)
        env["EXAM_CAMERA_HOME" if key == "02" else "EXAM_TERMINAL_HOME"] = home

    cmd = [sys.executable, os.path.join(directory, "integration_api.py"),
           "--op", op, "--args", json.dumps(args or {}, ensure_ascii=False)]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "env": env, "cwd": directory}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "%s 调用 %s 超时" % (key, op)}
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    line = ""
    for ln in reversed(out.splitlines()):
        if ln.strip().startswith("{"):
            line = ln.strip()
            break
    if not line:
        return {"ok": False, "reason": "%s %s 无有效输出：%s" % (key, op, (err or out)[:200])}
    try:
        return json.loads(line)
    except ValueError:
        return {"ok": False, "reason": "%s %s 输出无法解析" % (key, op)}


def _f(v, default=0.0):
    """安全转 float（外部传进来的字段可能是字符串）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _camera_credibility(camera_name=""):
    """
    问 02：这个机位的告警可信度是多少。
    02 不在（或没这个机位）时返回 1.0 不降权 —— 画质模块缺席不该让火警被压掉。
    """
    if not DIR_02:
        return 1.0, "02 模块不存在，不做画质降权"
    r = _project_call("02", "credibility", {"device_code": camera_name or ""})
    if not r.get("ok"):
        return 1.0, "02 未就绪（%s），不做画质降权" % str(r.get("reason"))[:40]
    try:
        f = float(r.get("factor", 1.0))
    except (TypeError, ValueError):
        f = 1.0
    return f, str(r.get("reason") or "")


# ============================================================ 上报 03
def op_dispatch_fire(a):
    """把一条火警送到 03 智能终端。"""
    level = str(a.get("level") or "alarm")
    event_type = str(a.get("event_type") or "").strip()
    if event_type not in ("FIRE_ALARM", "FIRE_SMOKE"):
        # 确认火警 -> 红；疑似/烟雾 -> 橙
        event_type = "FIRE_ALARM" if level == "alarm" else "FIRE_SMOKE"

    camera = str(a.get("camera_name") or a.get("camera_url") or "未命名机位")
    camera_key = str(a.get("camera_key") or camera)
    factor, why = _camera_credibility(camera_key)

    detail_bits = ["AI 火场识别"]
    if a.get("event_id"):
        detail_bits.append("事件号 %s" % a["event_id"])
    if a.get("score") is not None:
        detail_bits.append("判定分 %.2f" % _f(a["score"]))
    if a.get("peak_score") is not None:
        detail_bits.append("峰值 %.2f" % _f(a["peak_score"]))
    if a.get("fire_ratio") is not None:
        detail_bits.append("火色占比 %.2f%%" % (_f(a["fire_ratio"]) * 100))
    if a.get("smoke_ratio"):
        detail_bits.append("烟雾占比 %.2f%%" % (_f(a["smoke_ratio"]) * 100))
    if a.get("reason"):
        detail_bits.append("依据：%s" % a["reason"])
    if a.get("footage"):
        detail_bits.append("证据：%s" % os.path.basename(str(a["footage"])))

    args = {
        "code": event_type,
        "room_id": str(a.get("room_id") or ""),
        "seat_no": str(a.get("seat_no") or ""),
        "device_id": camera_key,
        "candidate_name": camera,          # 借用该字段承载"机位/考场"可读名
        "detail": "；".join(detail_bits),
        "credibility": factor,
        "operator": str(a.get("operator") or "13/火场识别"),
        "source": "13_AI火场识别与自动报警",
    }
    r = _project_call("03", "ingest_alert", args)
    out = {"ok": bool(r.get("ok")),
           "alert_id": r.get("alert_id"), "level": r.get("level"),
           "level_name": r.get("level_name"), "created": r.get("created"),
           "downgraded": r.get("downgraded"), "force_confirm": r.get("force_confirm"),
           "credibility": factor, "credibility_reason": why,
           "code": event_type, "camera": camera,
           "reason": r.get("reason") or r.get("_error") or "",
           "source": "13_AI火场识别与自动报警"}
    return out


def op_fire_selfcheck(a):
    """
    内部演练：注入合成火焰，跑 **真实的检测链路**（检测器 -> 确认闸门），
    返回真的被检出的火警事件（不是伪造一条事件）。

    这样上层（00 集成联动 / 一键脚本）拿到的是"火场模块确实报出来的警"，
    再交给 03 处置 —— 整条链都是真的。
    """
    import config
    import detector as detector_mod
    import models
    import video as video_mod

    seconds = float(a.get("seconds") or 4.0)
    camera_name = str(a.get("camera_name") or "演练合成源")
    camera_url = str(a.get("camera_url") or "simulated")

    cfg = config.load_settings()
    store_mod = __import__("storage")
    store = store_mod.EventStore()
    recorder = video_mod.EvidenceRecorder(cfg, store)
    recorder.start()

    caught = {"events": [], "cleared": 0}

    def on_alarm(det, ev):
        caught["events"].append(ev)
        store.save_event(ev)
        store.log("火警触发（联动演练）", "%s 峰值 %.2f" % (ev.event_id, ev.peak_score),
                  "linkage")
        recorder.active_event = ev
        recorder.capture_event(ev, _synth_fire(0.05))

    def on_clear(ev):
        caught["cleared"] += 1
        store.save_event(ev)
        recorder.close_event(ev.event_id, save_clip=True)

    det, why = detector_mod.build_detector(cfg, on_alarm=on_alarm, on_clear=on_clear)
    t0 = time.time()
    frames = 0
    while time.time() - t0 < seconds:
        det.update(_synth_fire(time.time() - t0), camera_name=camera_name,
                   camera_url=camera_url)
        frames += 1
        time.sleep(0.08)

    # 收尾：把进行中的事件标为已解除，并等证据落盘
    if det.active_event is not None:
        ev = det.active_event
        recorder.active_event = ev
        _wait_evidence(recorder, store, ev.event_id)
        ev.clear(force=True)
        store.save_event(ev)
        recorder.close_event(ev.event_id, save_clip=True)
    recorder.stop()

    if not caught["events"]:
        return {"ok": False, "reason": "注入 %d 帧合成火焰，检测器未报警" % frames,
                "frames": frames, "backend": why,
                "source": "13_AI火场识别与自动报警"}

    ev = caught["events"][0]
    row = store.get_event(ev.event_id) or ev
    footage = row.clip or row.marked or row.snapshot or ""
    return {"ok": True, "frames": frames, "backend": why,
            "event_id": ev.event_id, "level": ev.level, "score": row.score,
            "peak_score": row.peak_score, "reason": row.reason,
            "fire_ratio": row.fire_ratio, "smoke_ratio": row.smoke_ratio,
            "camera_name": camera_name, "camera_url": camera_url,
            "snapshot": row.snapshot, "marked": row.marked, "clip": row.clip,
            "footage": footage, "status": row.status,
            "evidence_dir": os.path.join(str(config.EVENT_DIR), ev.event_id),
            "source": "13_AI火场识别与自动报警"}


def op_status(a):
    import config
    import detector as detector_mod
    import storage as storage_mod
    cfg = config.load_settings()
    store = storage_mod.EventStore()
    return {"ok": True, "system": "13_AI火场识别与自动报警",
            "backend": detector_mod.describe_choice(cfg),
            "alarm_threshold": cfg["alarm_threshold"],
            "warning_threshold": cfg["warning_threshold"],
            "confirm_frames": cfg["confirm_frames"],
            "events": store.summary(),
            "data_dir": str(config.DATA_ROOT),
            "camera": "%s（%s）" % (cfg["camera_url"], cfg["camera_name"])}


# ============================================================ 小工具
def _synth_fire(t, h=360, w=480):
    """合成一团会抖动的火（与自检/界面演练同一画法）。"""
    import cv2
    import numpy as np
    img = np.full((h, w, 3), 200, np.uint8)
    cv2.rectangle(img, (30, 40), (w - 30, h - 40), (120, 120, 120), -1)
    cx, cy = w // 2, int(h * 0.62)
    j = int(6 * np.sin(t * 6.0)) + int(4 * np.cos(t * 9.7))
    rw, rh = 50, 85
    pts = np.array([[cx - rw, cy + rh], [cx - rw + 10 + j, cy - rh // 3],
                    [cx, cy - rh - 10 - j], [cx + rw - 6, cy - rh // 4],
                    [cx + rw, cy + rh]], np.int32)
    cv2.fillPoly(img, [pts], (0, 205, 255))
    cv2.circle(img, (cx + j // 2, cy + rh // 3), int(rh * 0.42), (60, 255, 255), -1)
    cv2.circle(img, (cx, cy + rh // 2), int(rh * 0.22), (255, 255, 255), -1)
    return img


def _wait_evidence(recorder, store, event_id, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = recorder.stats()
        row = store.get_event(event_id)
        if st.get("queued") == 0 and st.get("open_clips") == 0 and row and row.snapshot:
            return True
        time.sleep(0.2)
    return False


OPS = {"dispatch_fire": op_dispatch_fire, "fire_selfcheck": op_fire_selfcheck,
       "status": op_status}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.args or "{}")
    except ValueError:
        return emit({"ok": False, "reason": "--args 不是合法 JSON"})
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                       # noqa: BLE001
        import traceback
        return emit({"ok": False,
                     "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
