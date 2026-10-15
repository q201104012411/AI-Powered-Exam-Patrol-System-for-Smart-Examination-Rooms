# -*- coding: utf-8 -*-
"""
02 摄像头核查与视频质量诊断 · 集成接口（供 00_集成联动 调用）

ops:
  credibility  {device_code}             -> {factor, reason}   给告警做可信度降权
  inspect      {device_id, image}        -> {ok, defects, ...} 单机位画质+视野诊断
  devices      {}                        -> 机位清单
  status       {}                        -> 概览
用法：python integration_api.py --op credibility --args "{\"device_code\":\"CAM-A01-1\"}"
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _stack():
    import config                                     # noqa: F401
    import storage
    import baseline as baseline_mod
    import patrol as patrol_mod
    store = storage.Store()
    bs = baseline_mod.BaselineStore(store)
    runner = patrol_mod.PatrolRunner(store, baseline_store=bs)
    return store, bs, runner


def op_credibility(a):
    store, _bs, runner = _stack()
    code = a.get("device_code") or ""
    factor, why = runner.credibility_factor(code)
    dev = store.get_device(code=code)
    ck = store.latest_check(dev.device_id) if dev else None
    return {"ok": True, "device_code": code, "factor": factor, "reason": why,
            "defects": [x.code for x in (ck.defects if ck else [])],
            "checked_at": ck.ts if ck else "",
            "source": "02_摄像头核查与视频质量诊断"}


def op_inspect(a):
    import config
    import diagnose
    import models
    store, bs, runner = _stack()
    dev = store.get_device(device_id=a.get("device_id")) if a.get("device_id") else \
        store.get_device(code=a.get("device_code") or "")
    if dev is None:
        return {"ok": False, "reason": "机位不存在"}
    img = diagnose.imread_unicode(a.get("image") or "")
    if img is None:
        return {"ok": False, "reason": "画面无法解码"}
    rec, base_gray, base_metrics = bs.get(dev)
    defects, metrics = runner.dg.diagnose(img, baseline_gray=base_gray,
                                         baseline_metrics=base_metrics)
    ck = models.CheckResult(dev, config.POLICY_UPLOAD, online=True, defects=defects,
                            metrics=metrics, operator=a.get("operator") or "linkage",
                            note="集成联动调用")
    store.save_check(ck)
    return {"ok": True, "device_code": dev.code, "result_ok": ck.ok,
            "defects": [x.to_dict() for x in defects],
            "baseline_used": base_gray is not None,
            "factor": runner.credibility_factor(dev.code)[0],
            "source": "02_摄像头核查与视频质量诊断"}


def op_devices(a):
    store, _bs, _r = _stack()
    return {"ok": True, "devices": [{"device_id": d.device_id, "code": d.code,
                                     "room_id": d.room_id, "position": d.position,
                                     "has_baseline": bool(store.get_baseline(d.device_id))}
                                    for d in store.list_devices()]}


def op_save_device(a):
    import models
    store, _bs, _r = _stack()
    code = (a.get("code") or "").strip()
    if not code:
        return {"ok": False, "reason": "设备编码必填"}
    old = store.get_device(code=code)
    dev = models.Device(code=code, name=a.get("name") or code,
                        room_id=a.get("room_id") or "",
                        site=a.get("site") or "", position=a.get("position") or "",
                        url=a.get("url") or "", fov_deg=a.get("fov_deg") or 90,
                        pan_deg=a.get("pan_deg") or 0, tilt_deg=a.get("tilt_deg") or 0,
                        has_audio=bool(a.get("has_audio")),
                        session=a.get("session") or "",
                        device_id=(old.device_id if old else None))
    if old:
        dev.last_heartbeat = old.last_heartbeat
        dev.baseline_at = old.baseline_at
    store.save_device(dev)
    store.heartbeat(dev.device_id)
    store.log("集成联动登记机位", code, a.get("operator") or "linkage")
    return {"ok": True, "device_id": dev.device_id, "code": code}


def op_baseline(a):
    store, bs, _r = _stack()
    dev = store.get_device(code=a.get("device_code") or "")
    if dev is None:
        return {"ok": False, "reason": "机位不存在"}
    ok, res = bs.capture(dev, image_path=a.get("image") or "",
                         operator=a.get("operator") or "linkage", note="集成联动采集")
    if not ok:
        return {"ok": False, "reason": str(res)[:200]}
    return {"ok": True, "path": res["path"], "sharpness": res["sharpness"]}


def op_status(a):
    import config
    store, _bs, runner = _stack()
    return {"ok": True, "system": "02_摄像头核查与视频质量诊断",
            "devices": len(store.list_devices()), "rooms": store.rooms(),
            "online_rate": store.online_rate()["rate"],
            "orders": store.workorder_summary(), "data_dir": str(config.DATA_ROOT)}


# =====================================================================
#  账户管理（供 03 智能终端远程管理本模块的网页/界面登录账号）
#  —— 与 01 的口径一致：本接口只做越级防护（by=调用方角色），
#     真正的权限判定与留痕在 03 侧完成（两道闸）。
# =====================================================================
_ACCT_NOTE = ("账户管理属敏感操作：03 侧需 owner 权限 + 写审计链后才可调用本接口；"
              "本接口只做越级防护，不替代 03 的权限判定。")


def op_account_list(a):
    import web_accounts
    store = _stack()[0]
    acc = web_accounts.Accounts(store)
    acc.ensure()
    return {"ok": True, "accounts": acc.list_accounts(),
            "roles": web_accounts.ROLE_TEXT, "note": _ACCT_NOTE,
            "source": "02_摄像头核查与视频质量诊断"}


def op_account_create(a):
    import web_accounts
    store = _stack()[0]
    acc = web_accounts.Accounts(store)
    acc.ensure()
    ok, msg = acc.create(a.get("username") or "", a.get("password") or "",
                         a.get("role") or web_accounts.ROLE_ADMIN,
                         name=a.get("display_name") or a.get("name") or "",
                         by=a.get("by_role") or web_accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "02_摄像头核查与视频质量诊断"}


def op_account_reset(a):
    import web_accounts
    store = _stack()[0]
    acc = web_accounts.Accounts(store)
    acc.ensure()
    ok, msg = acc.reset_password(a.get("username") or "", a.get("password") or "",
                                 by=a.get("by_role") or web_accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "02_摄像头核查与视频质量诊断"}


def op_account_delete(a):
    import web_accounts
    store = _stack()[0]
    acc = web_accounts.Accounts(store)
    acc.ensure()
    ok, msg = acc.delete(a.get("username") or "",
                         by=a.get("by_role") or web_accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "02_摄像头核查与视频质量诊断"}


OPS = {"credibility": op_credibility, "inspect": op_inspect,
       "devices": op_devices, "save_device": op_save_device,
       "baseline": op_baseline, "status": op_status,
       "account_list": op_account_list, "account_create": op_account_create,
       "account_reset": op_account_reset, "account_delete": op_account_delete}


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
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
