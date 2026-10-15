# -*- coding: utf-8 -*-
"""
01 人脸识别与身份核对 · 局域网联动上报客户端

场景：本机（设备 A）跑 01，另一台设备（设备 B）跑 03 智能终端，两者在同一局域网。
本脚本把 01 的**操作记录**与**核验记录（哈希链审计）**送到 03 的接入服务
（`03_智能终端操作\\lan_service.py`），并支持断网补传。

职责划分（与 03 的 lan_service、00 的 lan_upload 一起看）：
  · 传输、组批、队列、重试  → `00_集成联动\\lan_upload.py`（只依赖标准库，01/02 共用）
  · 本文件只做"读自己的数据 + 维护水位线"，不重复实现传输
  · 接收端按内容去重，所以重发是安全的（幂等）

上报什么：
  `oplog`   界面操作日志（`data\\logs\\gui_action.log`，写入时已脱敏，这里再脱一次）
  `verify`  核验记录哈希链（`data\\audit.log`，JSON 行，含 hash 链）——
            连同**链头哈希与链路校验结果**一起上报，终端能看到"对端链是否自洽"。

水位线（`data\\lan_push_state.json`）：
  记录"发到第几条"。三条规矩：
    1) 上报失败**不前进**，下次从同一位置重发（接收端按内容去重，不会重复入库）；
    2) 单次只取 `--limit` 条时，水位线只推进**实际发出去的条数**，不跳到文件末尾；
    3) 文件被轮转/清空导致条数变少时，水位线重置为 0 并如实记一条说明 ——
       旧文件（`.1`/`.bak`）里的记录本脚本不再自动上报，它们仍在本地磁盘上，
       需要时可人工补传（`--file`）。

用法：
  set EXAM_TERMINAL_URL=http://192.168.1.20:9113
  set EXAM_TERMINAL_TOKEN=<终端上的令牌>

  python push_oplog.py                     # 补传积压 + 上报新增
  python push_oplog.py --status            # 看水位线/积压/对端健康
  python push_oplog.py --dry-run           # 只看要发什么，不联网
  python push_oplog.py --loop 60           # 常驻，每 60 秒跑一次
  python push_oplog.py --alert FACE_MISMATCH --detail "现场核验不符" --room A-01
  python push_oplog.py --file 其他日志.txt --kind oplog   # 人工补传某个文件
"""
import argparse
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LINKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE)), "00_集成联动")
for _p in (BASE, LINKAGE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                                     # noqa: E402  01 自己的配置
import lan_upload                                 # noqa: E402  00 的通用上传客户端
import pii                                        # noqa: E402  01 自己的脱敏

DEVICE_NAME = "01_人脸识别与身份核对"

STATE_FILE = os.path.join(str(config.DATA_ROOT), "lan_push_state.json")
QUEUE_FILE = os.path.join(str(config.DATA_ROOT), "lan_queue.ndjson")

# 操作日志行格式（action_logger.ActionLogger.log 写的）：
#   [2026-10-15T09:00:01] [owner] 动作 | 详情
ACTION_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*(?:\[(?P<who>[^\]]*)\]\s*)?(?P<rest>.*)$")


def _device_id(explicit=""):
    if explicit:
        return explicit
    env = os.environ.get("EXAM_DEVICE_ID", "").strip()
    if env:
        return env
    return (os.environ.get("COMPUTERNAME") or "PC-01").strip()


def _default_url():
    return os.environ.get("EXAM_TERMINAL_URL", "").strip()


def _default_token():
    return os.environ.get("EXAM_TERMINAL_TOKEN", "").strip()


# ---------------- 水位线 ----------------
def load_state(path=None):
    path = path or STATE_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    path = path or STATE_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except OSError:
        return False


# ---------------- 数据源：操作日志 ----------------
def read_action_log(path=None, start=0, limit=1000):
    """
    读操作日志第 start 行之后的内容。返回 dict：
      {records, start_used, total, notes}
    `start_used` 可能与传入的 start 不同（文件被清空/轮转时回到 0）；
    调用方用 start_used + len(records) 作为新水位线。
    """
    path = path or str(config.ACTION_LOG_PATH)
    notes = []
    if not os.path.exists(path):
        return {"records": [], "start_used": 0, "total": 0,
                "notes": ["操作日志不存在：%s" % path]}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    total = len(lines)
    if start > total:
        notes.append("操作日志行数从 %d 减少到 %d（轮转/清空），水位线已重置；"
                     "旧文件里的记录不再自动上报" % (start, total))
        start = 0
    out = []
    for i, line in enumerate(lines[start:start + max(0, limit)], start=start):
        m = ACTION_LINE_RE.match(line)
        ts = (m.group("ts") if m else "") or ""
        who = (m.group("who") if m else "") or ""
        rest = (m.group("rest") if m else line) or ""
        action, _, detail = rest.partition("|")
        out.append({"record_id": "oplog#%d" % i,
                    "ts": ts.strip(),
                    "action": pii.sanitize_for_log(action.strip()) or "操作",
                    "detail": pii.sanitize_for_log(detail.strip()),
                    "operator": who.strip(),
                    "line": pii.sanitize_for_log(line[:500])})
    return {"records": out, "start_used": start, "total": total, "notes": notes}


# ---------------- 数据源：核验记录（哈希链） ----------------
def read_audit_log(path=None, start=0, limit=1000):
    """
    读核验记录（JSON 行）第 start 条之后的内容。返回 dict：
      {records, start_used, total, chain, notes}

    chain 带链头哈希与本机自校验结果 —— 终端据此知道"对端链是否自洽"，
    但它**不能**替代本机的哈希链校验（终端只有这些记录，验不了对端的完整链）。
    """
    path = path or str(config.AUDIT_LOG_PATH)
    notes = []
    if not os.path.exists(path):
        return {"records": [], "start_used": 0, "total": 0, "chain": {},
                "notes": ["核验记录不存在：%s" % path]}
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                notes.append("核验记录里有一行不是合法 JSON，已跳过")
    total = len(rows)
    if start > total:
        notes.append("核验记录条数从 %d 减少到 %d（到期清理/重建链），水位线已重置"
                     % (start, total))
        start = 0

    head, verified = "", ""
    try:
        from audit import AuditLogger
        logger = AuditLogger()
        head = logger.last_hash() or ""
        ok, msg, _n = logger.verify_chain()
        verified = ("ok：" + msg) if ok else ("异常：" + msg)
    except Exception as e:                            # noqa: BLE001
        verified = "未校验（%s）" % str(e)[:80]

    out = []
    for i, rec in enumerate(rows[start:start + max(0, limit)], start=start):
        rec = dict(rec)
        rec.setdefault("record_id", "audit#%d" % i)
        # 证件类字段一律不进上报体（明文与哈希都不需要离开本机）
        for k in ("id_card", "id_card_display", "id_card_hash", "id_card_salt"):
            rec.pop(k, None)
        rec["reason"] = pii.sanitize_for_log(str(rec.get("reason") or ""))
        rec["action"] = rec.get("action") or ("核验：" + str(rec.get("result") or ""))
        rec["detail"] = rec.get("detail") or ("%s %s %s" % (rec.get("mode") or "",
                                                           rec.get("reason_code") or "",
                                                           rec.get("reason") or "")).strip()
        out.append(rec)
    chain = {"algo": "sha256(prev+canonical)" if head else "",
             "head": head, "count": total, "verified": verified}
    return {"records": out, "start_used": start, "total": total, "chain": chain,
            "notes": notes}


SOURCES = {
    "oplog": {"label": "界面操作日志", "kind": lan_upload.KIND_OPLOG,
              "reader": read_action_log, "default_path": lambda: str(config.ACTION_LOG_PATH)},
    "verify": {"label": "核验记录（哈希链）", "kind": lan_upload.KIND_VERIFY,
               "reader": read_audit_log, "default_path": lambda: str(config.AUDIT_LOG_PATH)},
}


def collect(kinds, state, limit, overrides=None, dry_run=False):
    """
    按数据源收集增量。返回 (batches, new_state, notes)。

    batches 每项：{source, kind, label, records, chain, start, new_watermark}
    new_watermark = start_used + 实际取到的条数（**不跳到文件末尾**）。
    """
    overrides = overrides or {}
    batches, notes = [], []
    new_state = dict(state)
    for name in kinds:
        src = SOURCES[name]
        if name in overrides and overrides[name]:
            got = src["reader"](path=overrides[name], start=0, limit=limit)
            start = 0
        else:
            start = int((state.get("src.%s" % name) or {}).get("lines") or 0)
            got = src["reader"](start=start, limit=limit)
        recs = got["records"]
        notes.extend(got.get("notes") or [])
        new_watermark = got["start_used"] + len(recs)
        batches.append({"source": name, "kind": src["kind"], "label": src["label"],
                        "records": recs, "chain": got.get("chain"),
                        "start": got["start_used"], "new_watermark": new_watermark,
                        "total": got["total"]})
        if not dry_run:
            new_state["src.%s" % name] = {
                "lines": new_watermark, "kind": src["kind"], "file_total": got["total"],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return batches, new_state, notes


def push_alert(url, token, code, detail="", room_id="", seat_no="", device_id="",
               candidate_id="", candidate_name="", credibility=None, timeout=5.0):
    payload = {"code": code, "detail": detail, "room_id": room_id, "seat_no": seat_no,
               "device_id": device_id or "01-HANDHELD", "candidate_id": candidate_id,
               "candidate_name": candidate_name, "source": "lan/01"}
    if credibility is not None:
        payload["credibility"] = credibility
    return lan_upload.post_alert(url, payload, token=token, timeout=timeout)


def run_once(url, token, state_path, kinds, limit, dry_run=False, timeout=5.0,
             device_id="", overrides=None):
    state = load_state(state_path)
    up = lan_upload.RecordUploader(
        url=url, token=token, device_id=_device_id(device_id), device_name=DEVICE_NAME,
        kind=lan_upload.KIND_AUDIT, queue_path=QUEUE_FILE, timeout=timeout)

    summary = {"ok": True, "flushed": None, "sources": [], "notes": [], "dry_run": dry_run}

    if not dry_run and up.pending():
        summary["flushed"] = up.flush_queue()

    batches, new_state, notes = collect(kinds, state, limit, overrides=overrides,
                                        dry_run=dry_run)
    summary["notes"].extend(notes)

    advanced = {}
    for b in batches:
        if not b["records"]:
            summary["sources"].append({"source": b["source"], "kind": b["kind"],
                                       "sent": 0, "note": "无新增",
                                       "file_total": b["total"]})
            advanced[b["source"]] = True     # 无新增时也把水位线对齐到文件末尾
            continue
        if dry_run:
            summary["sources"].append({"source": b["source"], "kind": b["kind"],
                                       "would_send": len(b["records"]),
                                       "from_line": b["start"] + 1,
                                       "to_line": b["new_watermark"]})
            continue
        up.kind = b["kind"]
        r = up.send_all(b["records"], chain=b["chain"])
        entry = {"source": b["source"], "kind": b["kind"], "sent": len(b["records"]),
                 "from_line": b["start"] + 1, "to_line": b["new_watermark"],
                 "accepted": r.get("accepted"), "duplicated": r.get("duplicated"),
                 "queued_batches": r.get("queued_batches"),
                 "failed_batches": r.get("failed_batches"), "ok": r.get("ok")}
        if not r.get("ok"):
            first = next((x for x in (r.get("results") or []) if not x.get("ok")), {})
            entry["reason"] = first.get("reason")
        summary["sources"].append(entry)
        advanced[b["source"]] = bool(r.get("ok"))
        if not r.get("ok"):
            summary["ok"] = False
            summary["notes"].append(
                "%s 上报失败：水位线保持在第 %d 条，已入本地队列（%d 批），下次重传"
                % (b["label"], b["start"], r.get("queued_batches") or 0))

    if not dry_run:
        for b in batches:
            if advanced.get(b["source"]):
                new_state["src.%s" % b["source"]] = {
                    "lines": b["new_watermark"], "kind": b["kind"],
                    "file_total": b["total"],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_state(new_state, state_path)
        summary["watermark"] = {k: v for k, v in new_state.items()
                                if k.startswith("src.")}

    summary["pending_queue"] = up.pending()
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="01 记录上报客户端（局域网联动）")
    ap.add_argument("--url", default=_default_url(),
                    help="03 接入服务地址（默认取 EXAM_TERMINAL_URL）")
    ap.add_argument("--token", default=_default_token(),
                    help="接入令牌（默认取 EXAM_TERMINAL_TOKEN）")
    ap.add_argument("--device-id", default="", help="本机标识（默认取计算机名）")
    ap.add_argument("--only", default="oplog,verify",
                    help="上报哪些源，逗号分隔（oplog / verify）")
    ap.add_argument("--limit", type=int, default=1000,
                    help="每个源单次最多取多少条（默认 1000）")
    ap.add_argument("--state", default=STATE_FILE, help="水位线文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只看要发什么，不联网、不动水位线")
    ap.add_argument("--status", action="store_true", help="显示水位线/积压/对端健康后退出")
    ap.add_argument("--reset-state", action="store_true", help="把水位线清零（下次全量重发）")
    ap.add_argument("--loop", type=int, default=0, help="常驻：每 N 秒跑一次（0=只跑一次）")
    ap.add_argument("--alert", default="", help="改为上报一条业务告警（告警码）")
    ap.add_argument("--detail", default="", help="告警详情")
    ap.add_argument("--room", default="", help="告警考场")
    ap.add_argument("--seat", default="", help="告警座位")
    ap.add_argument("--candidate", default="", help="考生号（可选）")
    ap.add_argument("--file", default="", help="人工补传指定文件（配合 --kind）")
    ap.add_argument("--kind", default="", choices=["", "oplog", "verify"],
                    help="配合 --file：按哪个源解析")
    ns = ap.parse_args(argv)

    if ns.status:
        state = load_state(ns.state)
        up = lan_upload.RecordUploader(url=ns.url, token=ns.token,
                                       device_id=_device_id(ns.device_id),
                                       device_name=DEVICE_NAME, queue_path=QUEUE_FILE)
        out = {"device": _device_id(ns.device_id), "url": ns.url or "（未配置）",
               "state_file": ns.state, "watermark": state,
               "pending_queue": up.pending(), "data_dir": str(config.DATA_ROOT)}
        if ns.url:
            ok, data = up.health()
            out["peer_ok"] = ok
            out["peer"] = data
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if ns.reset_state:
        save_state({}, ns.state)
        print("水位线已清零：%s（下次将全量重发，接收端按内容去重）" % ns.state)

    if ns.alert:
        if not ns.url:
            print("未配置接入地址：请设 EXAM_TERMINAL_URL 或传 --url")
            return 2
        ok, data = push_alert(ns.url, ns.token, ns.alert, detail=ns.detail,
                              room_id=ns.room, seat_no=ns.seat,
                              candidate_id=ns.candidate, device_id=ns.device_id)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    overrides = {}
    if ns.file:
        if not ns.kind:
            print("--file 需要同时指定 --kind（oplog 或 verify）")
            return 2
        overrides[ns.kind] = ns.file
        kinds = [ns.kind]
    else:
        kinds = [k.strip() for k in (ns.only or "").split(",") if k.strip()]
    bad = [k for k in kinds if k not in SOURCES]
    if bad:
        print("未知数据源：%s（可选：%s）" % ("、".join(bad), "、".join(SOURCES)))
        return 2
    if not ns.dry_run and not ns.url:
        print("未配置接入地址：请设 EXAM_TERMINAL_URL，或用 --url 指定 03 的地址")
        print("（只做本地检查可加 --dry-run）")
        return 2

    if ns.loop and ns.loop > 0 and not ns.dry_run:
        print("常驻上报：每 %d 秒一次（Ctrl+C 停止）" % ns.loop)
        while True:
            try:
                s = run_once(ns.url, ns.token, ns.state, kinds, ns.limit,
                             device_id=ns.device_id, overrides=overrides)
                print(json.dumps(s, ensure_ascii=False))
            except KeyboardInterrupt:
                print("已停止。")
                return 0
            except Exception as e:                    # noqa: BLE001
                print("本轮异常（不影响本机业务，下轮重试）：%s" % str(e)[:200])
            time.sleep(ns.loop)

    s = run_once(ns.url, ns.token, ns.state, kinds, ns.limit, dry_run=ns.dry_run,
                 device_id=ns.device_id, overrides=overrides)
    # 机器读的用一行 JSON（脚本/联动层按"最后一行 JSON"解析），人读的用上面 --status 的缩进输出
    print(json.dumps(s, ensure_ascii=False))
    return 0 if (s.get("ok") or ns.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
