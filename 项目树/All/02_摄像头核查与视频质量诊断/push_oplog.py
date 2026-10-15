# -*- coding: utf-8 -*-
"""
02 摄像头核查与视频质量诊断 · 局域网联动上报客户端

场景：本机（设备 A）跑 02，另一台设备（设备 B）跑 03 智能终端，两者在同一局域网。
本脚本把 02 的**审计留痕**、**机位核查结论**、**整改工单**送到 03 的接入服务
（`03_智能终端操作\\lan_service.py`），并支持断网补传。

上报什么（三个数据源，各自独立水位线）：
  `audit`  巡检/台账/工单的哈希链审计（`audit` 表，带 seq）
  `check`  机位核查结论（`checks` 表：画质缺陷、指标、在线与否、操作人）
  `order`  整改工单（`workorders` 表：发现→派单→整改→复核，第 4 节第 3 条闭环）

只读原则（重要）：
  本脚本用 `sqlite3.connect("file:...?mode=ro", uri=True)` **只读**打开 02 的库，
  自己实现链校验（复用 02 的 `models.chain_hash`），不 new `storage.Store()`
  —— 因为 `Store()` 会执行 `PRAGMA journal_mode=WAL`，那对数据库是**持久改动**。
  上报客户端不应该改动被上报系统的数据。

水位线（`<数据目录>\\lan_push_state.json`）：
  `audit` 用单调递增的 `seq`；`check`/`order` 用 SQLite 的隐式 `rowid`。
  上报失败水位线**不前进**，下次重发；接收端按内容去重，不会重复入库。

用法：
  set EXAM_TERMINAL_URL=http://192.168.1.20:9113
  set EXAM_TERMINAL_TOKEN=<终端上的令牌>

  python push_oplog.py                     # 补传积压 + 上报新增
  python push_oplog.py --status
  python push_oplog.py --dry-run
  python push_oplog.py --loop 120          # 常驻（巡检场景建议）
  python push_oplog.py --alert VIDEO_BLUR --detail "CAM-A01-1 画面模糊" --room A-01
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LINKAGE_DIR = os.path.join(os.path.dirname(BASE), "00_集成联动")
for _p in (BASE, LINKAGE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                                     # noqa: E402  02 自己的配置
import models                                     # noqa: E402  02 自己的模型（chain_hash）
import lan_upload                                 # noqa: E402  00 的通用上传客户端

DEVICE_NAME = "02_摄像头核查与视频质量诊断"

DB_PATH = str(config.DB_PATH)
STATE_FILE = os.path.join(str(config.DATA_ROOT), "lan_push_state.json")
QUEUE_FILE = os.path.join(str(config.DATA_ROOT), "lan_queue.ndjson")


def _device_id(explicit=""):
    if explicit:
        return explicit
    env = os.environ.get("EXAM_DEVICE_ID", "").strip()
    if env:
        return env
    return (os.environ.get("COMPUTERNAME") or "PC-02").strip()


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


def open_ro(db_path=None):
    """只读打开 02 的库（读不到就抛，调用方如实报错，不假装成功）"""
    path = str(db_path or DB_PATH)
    if not os.path.exists(path):
        raise IOError("数据库不存在：%s" % path)
    c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True,
                        timeout=15)
    c.row_factory = sqlite3.Row
    return c


def verify_chain_ro(conn):
    """
    只读复刻 02 `storage.Store.verify_chain()` 的判定（复用 02 的 chain_hash）。
    返回 (ok, 说明, 条数, 链头哈希)。
    """
    rows = conn.execute("SELECT * FROM audit ORDER BY seq ASC").fetchall()
    prev = "GENESIS"
    head = ""
    for i, r in enumerate(rows, 1):
        payload = {"ts": r["ts"], "action": r["action"], "detail": r["detail"],
                   "operator": r["operator"]}
        if r["prev_hash"] != prev:
            return False, "第 %d 条 prev_hash 不匹配" % i, i, head
        if models.chain_hash(prev, payload) != r["hash"]:
            return False, "第 %d 条 hash 被篡改" % i, i, head
        prev = r["hash"]
        head = r["hash"]
    return True, "审计链完整（%d 条）" % len(rows), len(rows), head


# ---------------- 数据源 ----------------
def read_audit(conn, start=0, limit=1000):
    rows = conn.execute("SELECT * FROM audit WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                        (int(start), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({"record_id": "audit#%s" % d.get("seq"), "seq": d.get("seq"),
                    "ts": d.get("ts"), "action": d.get("action"),
                    "detail": d.get("detail"), "operator": d.get("operator"),
                    "prev_hash": d.get("prev_hash"), "hash": d.get("hash")})
    return out


def read_checks(conn, start=0, limit=1000):
    rows = conn.execute("SELECT rowid AS _rid, * FROM checks WHERE rowid > ? "
                        "ORDER BY rowid ASC LIMIT ?",
                        (int(start), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        defects = []
        try:
            raw = json.loads(d.get("defects") or "[]")
            defects = [{"code": x.get("code"), "severity": x.get("severity"),
                        "detail": x.get("detail")} for x in raw if isinstance(x, dict)]
        except (ValueError, TypeError):
            pass
        out.append({"record_id": "check#%s" % d.get("_rid"), "rowid": d.get("_rid"),
                    "check_id": d.get("check_id"), "ts": d.get("ts"),
                    "action": "机位核查", "device_code": d.get("device_code"),
                    "room_id": d.get("room_id"), "policy": d.get("policy"),
                    "online": d.get("online"), "ok": d.get("ok"),
                    "defects": defects, "operator": d.get("operator"),
                    "detail": ("%s 考场=%s 机位=%s %s"
                               % (d.get("policy") or "", d.get("room_id") or "-",
                                  d.get("device_code") or "-",
                                  ("缺陷：" + "、".join(x["code"] for x in defects))
                                  if defects else "无缺陷")).strip()})
    return out


def read_orders(conn, start=0, limit=1000):
    rows = conn.execute("SELECT rowid AS _rid, * FROM workorders WHERE rowid > ? "
                        "ORDER BY rowid ASC LIMIT ?",
                        (int(start), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            codes = json.loads(d.get("codes") or "[]")
        except (ValueError, TypeError):
            codes = []
        out.append({"record_id": "order#%s" % d.get("_rid"), "rowid": d.get("_rid"),
                    "order_id": d.get("order_id"), "ts": d.get("created_at"),
                    "action": "整改工单", "device_code": d.get("device_code"),
                    "room_id": d.get("room_id"), "codes": codes,
                    "status": d.get("status"), "severity": d.get("severity"),
                    "operator": d.get("operator") or d.get("assigned_to"),
                    "detail": ("工单 %s 状态=%s 严重级=%s 缺陷=%s"
                               % (d.get("order_id") or "-", d.get("status") or "-",
                                  d.get("severity") or "-",
                                  "、".join(str(c) for c in codes) or "-"))})
    return out


SOURCES = {
    "audit": {"label": "审计留痕（哈希链）", "kind": lan_upload.KIND_AUDIT,
              "reader": read_audit, "table": "audit"},
    "check": {"label": "机位核查结论", "kind": lan_upload.KIND_CHECK,
              "reader": read_checks, "table": "checks"},
    "order": {"label": "整改工单", "kind": lan_upload.KIND_ORDER,
              "reader": read_orders, "table": "workorders"},
}


def collect(kinds, state, limit, conn=None, dry_run=False):
    """收集增量。返回 (batches, new_state, notes)"""
    batches, notes = [], []
    new_state = dict(state)
    own = conn is None
    if own:
        conn = open_ro()
    try:
        for name in kinds:
            src = SOURCES[name]
            start = int((state.get("src.%s" % name) or {}).get("lines") or 0)
            try:
                recs = src["reader"](conn, start=start, limit=limit)
            except sqlite3.Error as e:
                notes.append("%s 读取失败：%s" % (src["label"], e))
                recs = []
            # 用最后一条的 seq/rowid 作为新水位线（比"条数"稳：删过行也不会错位）
            if recs:
                last = recs[-1]
                new_wm = int(last.get("seq") or last.get("rowid") or
                             (start + len(recs)))
            else:
                new_wm = start
            chain = None
            batches.append({"source": name, "kind": src["kind"], "label": src["label"],
                            "records": recs, "chain": chain, "start": start,
                            "new_watermark": new_wm})
            if not dry_run:
                new_state["src.%s" % name] = {
                    "lines": new_wm, "kind": src["kind"],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    finally:
        if own:
            conn.close()
    return batches, new_state, notes


def audit_chain_info(conn=None):
    """链头与自校验结果：跟着 audit 批次一起报给终端"""
    own = conn is None
    if own:
        try:
            conn = open_ro()
        except IOError as e:
            return {"algo": "", "head": "", "count": 0, "verified": "不可用：%s" % e}
    try:
        ok, msg, n, head = verify_chain_ro(conn)
        return {"algo": "sha256(prev+canonical)", "head": head, "count": n,
                "verified": ("ok：" + msg) if ok else ("异常：" + msg)}
    except sqlite3.Error as e:
        return {"algo": "", "head": "", "count": 0, "verified": "读取失败：%s" % e}
    finally:
        if own:
            conn.close()


def push_alert(url, token, code, detail="", room_id="", seat_no="", device_id="",
               credibility=None, timeout=5.0):
    payload = {"code": code, "detail": detail, "room_id": room_id, "seat_no": seat_no,
               "device_id": device_id or "CAM-UNKNOWN", "source": "lan/02"}
    if credibility is not None:
        payload["credibility"] = credibility
    return lan_upload.post_alert(url, payload, token=token, timeout=timeout)


def run_once(url, token, state_path, kinds, limit, dry_run=False, timeout=5.0,
             device_id=""):
    state = load_state(state_path)
    up = lan_upload.RecordUploader(
        url=url, token=token, device_id=_device_id(device_id), device_name=DEVICE_NAME,
        kind=lan_upload.KIND_AUDIT, queue_path=QUEUE_FILE, timeout=timeout)

    summary = {"ok": True, "flushed": None, "sources": [], "notes": [], "dry_run": dry_run}
    if not dry_run and up.pending():
        summary["flushed"] = up.flush_queue()

    try:
        conn = open_ro()
    except IOError as e:
        summary["ok"] = False
        summary["notes"].append(str(e))
        summary["pending_queue"] = up.pending()
        return summary

    try:
        batches, new_state, notes = collect(kinds, state, limit, conn=conn,
                                            dry_run=dry_run)
        chain = audit_chain_info(conn) if "audit" in kinds else None
        summary["notes"].extend(notes)
        advanced = {}
        for b in batches:
            if b["source"] == "audit":
                b["chain"] = chain
            if not b["records"]:
                summary["sources"].append({"source": b["source"], "kind": b["kind"],
                                           "sent": 0, "note": "无新增"})
                advanced[b["source"]] = True
                continue
            if dry_run:
                summary["sources"].append({"source": b["source"], "kind": b["kind"],
                                           "would_send": len(b["records"]),
                                           "from": b["start"] + 1,
                                           "to": b["new_watermark"]})
                continue
            up.kind = b["kind"]
            r = up.send_all(b["records"], chain=b["chain"])
            entry = {"source": b["source"], "kind": b["kind"], "sent": len(b["records"]),
                     "from": b["start"] + 1, "to": b["new_watermark"],
                     "accepted": r.get("accepted"), "duplicated": r.get("duplicated"),
                     "queued_batches": r.get("queued_batches"),
                     "failed_batches": r.get("failed_batches"), "ok": r.get("ok")}
            if not r.get("ok"):
                first = next((x for x in (r.get("results") or []) if not x.get("ok")), {})
                entry["reason"] = first.get("reason")
                summary["ok"] = False
                summary["notes"].append(
                    "%s 上报失败：水位线保持在第 %d 条，已入本地队列，下次重传"
                    % (b["label"], b["start"]))
            summary["sources"].append(entry)
            advanced[b["source"]] = bool(r.get("ok"))

        if not dry_run:
            for b in batches:
                if advanced.get(b["source"]):
                    new_state["src.%s" % b["source"]] = {
                        "lines": b["new_watermark"], "kind": b["kind"],
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_state(new_state, state_path)
            summary["watermark"] = {k: v for k, v in new_state.items()
                                    if k.startswith("src.")}
    finally:
        conn.close()

    summary["pending_queue"] = up.pending()
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="02 记录上报客户端（局域网联动）")
    ap.add_argument("--url", default=os.environ.get("EXAM_TERMINAL_URL", "").strip(),
                    help="03 接入服务地址（默认取 EXAM_TERMINAL_URL）")
    ap.add_argument("--token", default=os.environ.get("EXAM_TERMINAL_TOKEN", "").strip(),
                    help="接入令牌（默认取 EXAM_TERMINAL_TOKEN）")
    ap.add_argument("--device-id", default="", help="本机标识（默认取计算机名）")
    ap.add_argument("--only", default="audit,check,order",
                    help="上报哪些源，逗号分隔（audit / check / order）")
    ap.add_argument("--limit", type=int, default=1000, help="每个源单次最多取多少条")
    ap.add_argument("--state", default=STATE_FILE, help="水位线文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只看要发什么，不联网、不动水位线")
    ap.add_argument("--status", action="store_true", help="显示水位线/积压/对端健康后退出")
    ap.add_argument("--reset-state", action="store_true", help="水位线清零（下次全量重发）")
    ap.add_argument("--loop", type=int, default=0, help="常驻：每 N 秒跑一次（0=只跑一次）")
    ap.add_argument("--alert", default="", help="改为上报一条业务告警（告警码）")
    ap.add_argument("--detail", default="", help="告警详情")
    ap.add_argument("--room", default="", help="告警考场")
    ap.add_argument("--seat", default="", help="告警座位")
    ap.add_argument("--credibility", type=float, default=None,
                    help="该机位的画质可信度（0~1，交给终端决定是否降级）")
    ns = ap.parse_args(argv)

    if ns.status:
        state = load_state(ns.state)
        up = lan_upload.RecordUploader(url=ns.url, token=ns.token,
                                       device_id=_device_id(ns.device_id),
                                       device_name=DEVICE_NAME, queue_path=QUEUE_FILE)
        out = {"device": _device_id(ns.device_id), "url": ns.url or "（未配置）",
               "db": DB_PATH, "state_file": ns.state, "watermark": state,
               "pending_queue": up.pending()}
        try:
            conn = open_ro()
            ok, msg, n, head = verify_chain_ro(conn)
            conn.close()
            out["local_chain"] = {"ok": ok, "message": msg, "rows": n,
                                  "head": (head[:16] + "…") if len(head) > 16 else head}
        except (IOError, sqlite3.Error) as e:
            out["local_chain"] = {"ok": False, "message": str(e)}
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
                              device_id=ns.device_id, credibility=ns.credibility)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if ok else 1

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
                             device_id=ns.device_id)
                print(json.dumps(s, ensure_ascii=False))
            except KeyboardInterrupt:
                print("已停止。")
                return 0
            except Exception as e:                    # noqa: BLE001
                print("本轮异常（不影响本机业务，下轮重试）：%s" % str(e)[:200])
            time.sleep(ns.loop)

    s = run_once(ns.url, ns.token, ns.state, kinds, ns.limit, dry_run=ns.dry_run,
                 device_id=ns.device_id)
    # 机器读的用一行 JSON（脚本/联动层按"最后一行 JSON"解析），人读的用上面 --status 的缩进输出
    print(json.dumps(s, ensure_ascii=False))
    return 0 if (s.get("ok") or ns.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
