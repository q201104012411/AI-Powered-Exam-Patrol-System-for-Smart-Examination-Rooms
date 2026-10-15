# -*- coding: utf-8 -*-
"""
00 集成联动 · 局域网联动上传客户端（01 / 02 / 03 共用）

为什么要这个模块：
  `linkage.py` 的 Linkage 是**同一台机器**上的子进程 + JSON 调用。
  两台设备在同一局域网时（设备 A 跑 01/02，设备 B 跑 03 智能终端），
  由本模块把记录 POST 到 03 的接入服务（`03_智能终端操作\\lan_service.py`）。

为什么放在 00 而不是各项目里：
  01/02/03 各自都有同名的 `config.py` / `models.py`，同进程 import 必然撞名。
  本模块**只依赖标准库**，不 import 任何项目的 config，参数全部显式传入；
  01/02 各自的 `push_oplog.py` 只负责"从自己的数据源取记录"，传输逻辑只有这一份。
  （与 `01\\linkage_bridge.py` 把 00 目录插进 sys.path 再 import 是同一个套路。）

协议（与 lan_service.py 一一对应，双方都要改时一起改）：
  GET  /health
       连通性探测；未配置令牌时**仅本机可访问**。
  POST /api/records
       headers: X-Exam-Token / Content-Type: application/json
       body: {"device_id","device_name","kind","chain":{...},"records":[{...}]}
       resp: {"ok","accepted","duplicated","total","chain_head"}
  POST /api/ingest_alert
       上报一条业务告警（接收端按 03 的规则分级、降权、去重）

可靠性（与 01 的 `edge.Outbox`、03 的 `offline.Outbox` 同一套思路）：
  · 上报失败**不丢数据**：整批写进本地队列（NDJSON，一行一个批次）
  · 下次先发积压队列，成功才从队列里删掉那一行
  · 记录 ID 由**接收端**按内容算，所以重发不会产生重复（幂等）
  · 水位线（发到哪一条）由调用方维护，失败时不前进 —— 这样"漏传"不可能发生

用法（程序里）：
    import lan_upload
    up = lan_upload.RecordUploader("http://192.168.1.20:9113", token="...",
                                   device_id="PC-A", device_name="01_人脸识别与身份核对",
                                   kind="audit", queue_path=r"D:\\data\\lan_queue.ndjson")
    up.send(records, chain={"algo": "sha256", "head": "...", "count": 120})

用法（命令行，便于手工验证）：
    python lan_upload.py --url http://192.168.1.20:9113 --token XXX --health
    python lan_upload.py --url http://192.168.1.20:9113 --token XXX --send-file recs.ndjson
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = 1

PATH_HEALTH = "/health"
PATH_RECORDS = "/api/records"
PATH_ALERT = "/api/ingest_alert"
PATH_LIST = "/api/records"

TOKEN_HEADER = "X-Exam-Token"

# 单次请求超时：局域网内 5 秒足够；超时只意味着"这批没发出去"，不影响本机运行
DEFAULT_TIMEOUT = 5.0

# 单批最多带多少条记录（防止一次 POST 过大被中间设备截断）
DEFAULT_BATCH = 200

# 接收端认识的记录类别（kind）。不在表里的 kind 接收端只做透传存储，不解释。
KIND_AUDIT = "audit"        # 哈希链审计（01 核验记录 / 02 巡检工单 / 03 处置留痕）
KIND_OPLOG = "oplog"        # 界面操作日志（纯文本行）
KIND_CHECK = "check"        # 02 机位核查结论
KIND_ORDER = "order"        # 02 整改工单
KIND_VERIFY = "verify"      # 01 人脸核验结论（结构化）
KIND_ALERT = "alert"        # 业务告警（结构化）
KINDS = (KIND_AUDIT, KIND_OPLOG, KIND_CHECK, KIND_ORDER, KIND_VERIFY, KIND_ALERT)


class LanUploadError(Exception):
    pass


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _post_json(url, body, token="", timeout=DEFAULT_TIMEOUT):
    """
    发一个 JSON POST，返回 (ok, data)。

    失败只返回 (False, {...})，**不抛异常**——调用方（界面/巡检线程）不该被网络问题打断；
    只有参数明显写错（URL 为空）才抛 LanUploadError。
    """
    if not url:
        raise LanUploadError("未配置接入服务地址（形如 http://192.168.1.20:9113）")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:               # 4xx/5xx 也带着 JSON 说明
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:                             # noqa: BLE001
            pass
        try:
            err = json.loads(raw)
        except (ValueError, TypeError):
            err = {"ok": False, "reason": "HTTP %s" % getattr(e, "code", "?")}
        err.setdefault("ok", False)
        err["http_status"] = getattr(e, "code", None)
        return False, err
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, {"ok": False, "reason": "无法连接接入服务：%s" % str(e)[:160]}

    try:
        out = json.loads(raw or "{}")
    except ValueError:
        return False, {"ok": False, "reason": "响应不是合法 JSON：%s" % raw[:160],
                       "http_status": status}
    if not isinstance(out, dict):
        return False, {"ok": False, "reason": "响应不是 JSON 对象"}
    out.setdefault("ok", 200 <= status < 300)
    out["http_status"] = status
    if not out["ok"] and not out.get("reason"):
        out["reason"] = "接入服务返回失败（HTTP %s）" % status
    return bool(out["ok"]), out


def _get_json(url, token="", timeout=DEFAULT_TIMEOUT):
    if not url:
        raise LanUploadError("未配置接入服务地址")
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:                             # noqa: BLE001
            pass
        try:
            err = json.loads(raw)
        except (ValueError, TypeError):
            err = {"ok": False, "reason": "HTTP %s" % getattr(e, "code", "?")}
        err.setdefault("ok", False)
        return False, err
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, {"ok": False, "reason": "无法连接接入服务：%s" % str(e)[:160]}


class RecordUploader(object):
    """
    一台设备上的"记录上报器"。线程不安全的（一次只在一个线程里用），
    需要并发时各自 new 一个实例即可。
    """

    def __init__(self, url, token="", device_id="", device_name="", kind=KIND_AUDIT,
                 queue_path=None, timeout=DEFAULT_TIMEOUT, batch=DEFAULT_BATCH,
                 sender=None):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.device_id = device_id or os.environ.get("COMPUTERNAME") or "device"
        self.device_name = device_name or ""
        self.kind = kind
        self.queue_path = str(queue_path) if queue_path else ""
        self.timeout = float(timeout)
        self.batch = max(1, int(batch))
        self._sender = sender            # 可注入：(url, body, token, timeout) -> (ok, data)

    # ---------------- 队列 ----------------
    def pending(self):
        if not self.queue_path or not os.path.exists(self.queue_path):
            return 0
        n = 0
        with open(self.queue_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    def _enqueue(self, body):
        if not self.queue_path:
            return False
        try:
            os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
            with open(self.queue_path, "a", encoding="utf-8") as f:
                f.write(_canonical(body) + "\n")
            return True
        except OSError:
            return False

    def _rewrite_queue(self, lines):
        try:
            os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
            with open(self.queue_path, "w", encoding="utf-8") as f:
                for ln in lines:
                    f.write(ln if ln.endswith("\n") else ln + "\n")
            return True
        except OSError:
            return False

    # ---------------- 组批 ----------------
    def batch_body(self, records, chain=None):
        recs = list(records or [])
        return {"proto": PROTOCOL_VERSION, "device_id": self.device_id,
                "device_name": self.device_name, "kind": self.kind,
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "chain": dict(chain or {}), "records": recs}

    # ---------------- 发送 ----------------
    def _post(self, body):
        if self._sender is not None:
            return self._sender(self.url + PATH_RECORDS, body, self.token, self.timeout)
        return _post_json(self.url + PATH_RECORDS, body, self.token, self.timeout)

    def send(self, records, chain=None):
        """
        上报一批记录。返回接收端的响应 dict；失败时含 ok=False 与 reason，
        并且已入本地队列（queued 表示是否入队成功）。
        """
        recs = list(records or [])
        if not recs:
            return {"ok": True, "accepted": 0, "duplicated": 0, "skipped": "空批次",
                    "queued": False}
        body = self.batch_body(recs, chain)
        ok, data = self._post(body)
        if ok:
            data["queued"] = False
            return data
        queued = self._enqueue(body)
        data["queued"] = queued
        data["pending"] = self.pending()
        return data

    def send_all(self, records, chain=None):
        """
        按 batch 上限切分成多批发送。返回汇总：
          {ok, accepted, duplicated, queued_batches, failed_batches, total, results[]}
        只要有一批失败就 ok=False（成功那几批不会重发）。
        """
        recs = list(records or [])
        results, accepted, duplicated, queued, failed = [], 0, 0, 0, 0
        for i in range(0, len(recs), self.batch):
            part = recs[i:i + self.batch]
            r = self.send(part, chain=chain if i == 0 else None)
            results.append(r)
            accepted += int(r.get("accepted") or 0)
            duplicated += int(r.get("duplicated") or 0)
            queued += 1 if r.get("queued") else 0
            if not r.get("ok"):
                failed += 1
        return {"ok": failed == 0, "accepted": accepted, "duplicated": duplicated,
                "queued_batches": queued, "failed_batches": failed,
                "total": len(recs), "results": results}

    def flush_queue(self, limit=50):
        """
        补传积压：逐行发，成功就删那一行。返回 {sent, failed, left, online}。
        一行失败就停下（说明网络不通，继续发没意义），剩下的原样保留。
        """
        if not self.queue_path or not os.path.exists(self.queue_path):
            return {"sent": 0, "failed": 0, "left": 0, "online": True}
        with open(self.queue_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        if not lines:
            return {"sent": 0, "failed": 0, "left": 0, "online": True}

        sent, failed = 0, False
        remaining = list(lines)
        for ln in lines[:max(1, int(limit))]:
            try:
                body = json.loads(ln)
            except ValueError:                 # 坏行：丢掉，避免永远卡住队列
                remaining.remove(ln)
                continue
            ok, _data = self._post(body)
            if ok:
                sent += 1
                remaining.remove(ln)
            else:
                failed = True
                break
        self._rewrite_queue(remaining)
        return {"sent": sent, "failed": 1 if failed else 0, "left": len(remaining),
                "online": not failed}

    # ---------------- 连通性 ----------------
    def health(self):
        return _get_json(self.url + PATH_HEALTH, self.token, self.timeout)

    def list_remote(self, kind=None, device_id=None, limit=50):
        q = "?limit=%d" % int(limit)
        if kind:
            q += "&kind=%s" % urllib.parse.quote(kind)
        if device_id:
            q += "&device_id=%s" % urllib.parse.quote(device_id)
        return _get_json(self.url + PATH_LIST + q, self.token, self.timeout)


def post_alert(url, payload, token="", timeout=DEFAULT_TIMEOUT):
    """
    向 03 上报一条**业务告警**（而不是操作记录）。返回 (ok, data)。
    01 的人脸不符、02 的画质异常都可以走这里直达终端；
    接收端复用 03 自己的 ingest_alert（含画质可信度降权、去重、强制确认）。
    """
    return _post_json((url or "").rstrip("/") + PATH_ALERT, payload, token, timeout)


def uploader_for(url, token="", device_id="", device_name="", kind=KIND_AUDIT,
                 queue_path=None, timeout=DEFAULT_TIMEOUT):
    """便捷构造（01/02 的 push_oplog.py 用这一个入口，便于统一替换实现）"""
    return RecordUploader(url=url, token=token, device_id=device_id,
                          device_name=device_name, kind=kind,
                          queue_path=queue_path, timeout=timeout)


# ---------------- 命令行（手工验证用） ----------------
def _read_ndjson(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"ts": "", "action": "raw", "detail": line})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="局域网联动上传客户端（00 集成联动）")
    ap.add_argument("--url", required=True, help="03 接入服务地址，如 http://192.168.1.20:9113")
    ap.add_argument("--token", default=os.environ.get("EXAM_TERMINAL_TOKEN", ""),
                    help="接入令牌（默认取 EXAM_TERMINAL_TOKEN）")
    ap.add_argument("--device-id", default="", help="本机标识（默认计算机名）")
    ap.add_argument("--device-name", default="", help="本机说明，如 01_人脸识别与身份核对")
    ap.add_argument("--kind", default=KIND_AUDIT, choices=list(KINDS))
    ap.add_argument("--queue", default="", help="本地积压队列文件（NDJSON）")
    ap.add_argument("--health", action="store_true", help="只做连通性探测")
    ap.add_argument("--flush", action="store_true", help="只补传本地积压队列")
    ap.add_argument("--send-file", default="", help="把一个 NDJSON 文件里的记录发上去")
    ns = ap.parse_args(argv)

    up = uploader_for(ns.url, token=ns.token, device_id=ns.device_id,
                      device_name=ns.device_name, kind=ns.kind,
                      queue_path=ns.queue or None)
    if ns.health:
        ok, data = up.health()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if ns.flush:
        r = up.flush_queue()
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["failed"] == 0 else 1
    if ns.send_file:
        recs = _read_ndjson(ns.send_file)
        r = up.send(recs)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
