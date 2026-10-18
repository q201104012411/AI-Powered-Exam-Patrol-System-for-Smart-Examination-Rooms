# -*- coding: utf-8 -*-
"""
03 智能终端操作 · 局域网接入服务

为什么需要这个文件：
  `integration_api.py` 是**同一台机器**上的 CLI 接口（00 集成联动用子进程调它），
  03 自身没有任何监听端口。所以"另一台设备上的操作记录传到本终端"这件事，
  过去在代码里根本没有接收端。本文件补上这一段。

它接收什么（另一台设备跑 01 人脸识别 / 02 摄像头核查时）：
  · 操作记录（界面操作日志）
  · 哈希链审计记录（01 的核验记录、02 的巡检/工单留痕）
  · 01 的核查结论、02 的机位核查结论与整改工单
  · 业务告警 —— 走 03 自己的 `ingest_alert`：分级兜底、画质可信度降权、
    去重、红色强制确认，与本机产生的告警**同一条流水线**

安全（三件事必须一起做）：
  1. 默认只监听 127.0.0.1。跨设备要显式设 `EXAM_TERMINAL_LAN_HOST=0.0.0.0`；
  2. 非本机来源**必须**带令牌（`X-Exam-Token`，用 hmac.compare_digest 比较）；
     令牌取 `EXAM_TERMINAL_TOKEN`，没有就首次自动生成到 `data\\lan_token.txt`；
  3. 要监听非本机却又拿不到令牌 -> **拒绝启动**（fail-closed），不静默裸奔。
写入口只有"记录接入 + 告警接入"两个；**复核、远程擦除、命令控制台一律不经此端口**。
只读接口另有：`GET /api/status`（终端状态）、`GET /api/alerts`（告警列表）、
`GET /api/records`（远端记录），供 00 联动层在多机部署时远程查询、也供人工排查。

网页版大数据驾驶舱（2026-10-18 新增）：
  `GET /` 或 `GET /cockpit` 返回 `web\\cockpit.html`（单文件大屏页面，无任何外部 CDN 依赖）；
  `GET /api/cockpit` 返回这一屏要的全部数据（KPI + 趋势 + 明细 + 模块态势，一次给全）。
  页面本身不含数据、不鉴权；**数据接口仍走同一套令牌**（跨设备要在页面右上角填令牌）。
  与 Py GUI 的驾驶舱是**同一份数据口径**（同一个 `store.stats()`），不存在两套统计。

远端记录与本机审计链的关系（重要）：
  远端记录进 `remote_records` 表，**不并进本机 `audit` 哈希链** ——
  并进去本机链会立刻校验失败。本机链上只写一条回执："收到 X 设备 N 条"。

用法：
  python lan_service.py                          # 按 config 的地址/端口前台运行
  python lan_service.py --host 0.0.0.0           # 跨设备（必须已有令牌）
  python lan_service.py --token 自定令牌
  python lan_service.py --print-token            # 查看/生成当前令牌（部署时抄给对端）
  python lan_service.py --selfcheck              # 临时数据目录 + 临时端口自检一遍
"""
import argparse
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PROTOCOL_VERSION = 1
TOKEN_HEADER = "X-Exam-Token"
SERVICE_NAME = "03_智能终端操作 局域网接入服务"

# 一次 POST 的 body 上限：远端一批最多 LAN_BATCH_MAX 条，8MB 足够且能挡住恶意大包
MAX_BODY_BYTES = 8 * 1024 * 1024

# 回环地址：本机访问免令牌（便于单机先跑通、也便于本机运维排查）
LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

# 接收端认识的记录类别（与 00 的 lan_upload.KINDS 对齐；不在表里的也照存，只是标注"未分类"）
KNOWN_KINDS = ("audit", "oplog", "check", "order", "verify", "alert")

# ---- 网页驾驶舱（GET / 与 GET /api/cockpit）----
# 为什么放在这个端口上：03 只有这一个监听口，网页版大屏要跟 Py GUI 看同一份数据，
# 再起一个 Flask 服务既多一个端口又多一套鉴权；直接复用现有服务最省事。
WEB_DIR = os.path.join(BASE, "web")
COCKPIT_HTML = os.path.join(WEB_DIR, "cockpit.html")
MODULE_TTL = 30.0          # 跨模块聚合缓存（网页 5 秒一刷，不能每次都起 8 个子进程）
MODULE_TIMEOUT = 6.0       # 单个模块子进程超时
CHAIN_TTL = 20.0           # 审计链全量校验间隔（O(全表)，不必每 5 秒一次）

#: 网页驾驶舱 HTML 的 mtime 缓存（改完页面不用重启服务）
_HTML_CACHE = {"mtime": 0.0, "data": b""}


def read_cockpit_html():
    """读取 web/cockpit.html（按 mtime 缓存）；文件缺失返回 None。"""
    try:
        mtime = os.path.getmtime(COCKPIT_HTML)
    except OSError:
        return None
    if _HTML_CACHE["mtime"] != mtime or not _HTML_CACHE["data"]:
        try:
            with open(COCKPIT_HTML, "rb") as f:
                _HTML_CACHE["data"] = f.read()
            _HTML_CACHE["mtime"] = mtime
        except OSError:
            return None
    return _HTML_CACHE["data"]


def is_loopback(host):
    return (host or "").strip().lower() in LOOPBACK


def _norm_ip(text):
    """把输入规范成 IPv4（不是 IP 就返回 ""）。storage 里是单一真源，这里只做转发。"""
    try:
        import storage
        return storage.normalize_ipv4(text)
    except Exception:                                 # noqa: BLE001
        return ""


def load_token(create=True):
    """
    令牌来源优先级：EXAM_TERMINAL_TOKEN 环境变量 > data\\lan_token.txt。
    文件不存在且 create=True 时生成一个 24 字节 URL-safe 随机令牌并落盘（0600 权限尽力而为）。
    """
    env = os.environ.get("EXAM_TERMINAL_TOKEN", "").strip()
    if env:
        return env
    import config
    path = str(config.LAN_TOKEN_FILE)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        if not create:
            return ""
        tok = secrets.token_urlsafe(24)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return tok
    except OSError:
        return ""


class LanService(object):
    """
    业务逻辑与 HTTP 分开，便于单元测试直接调（不必真的起服务）。

    store 可注入：测试里传 `storage.Store(db_path=临时库)`；
    生产/正常启动传 None，用 03 自己的 data 目录。
    """

    def __init__(self, store=None, token="", allow_local_no_token=True):
        if store is None:
            import storage
            store = storage.Store()
        self.store = store
        self.token = token or ""
        self.allow_local_no_token = bool(allow_local_no_token)
        # 网页驾驶舱用到的缓存（都是"代价高、变化慢"的东西）
        self._mod_cache = None
        self._mod_at = 0.0
        self._chain = (True, "", 0)
        self._chain_at = 0.0

    # ---------------- 鉴权 ----------------
    def authorized(self, client_ip, headers, path=""):
        """
        返回 (是否放行, 原因)。**fail-closed**：非本机来源一律要令牌；
        没配令牌时非本机来源直接拒（而不是放行）。
        """
        if is_loopback(client_ip) and self.allow_local_no_token:
            return True, "本机访问"
        if not self.token:
            return False, "本机未配置接入令牌（EXAM_TERMINAL_TOKEN 或 data\\lan_token.txt）"
        got = ""
        try:
            got = headers.get(TOKEN_HEADER) or ""
            if not got:
                auth = headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    got = auth[7:].strip()
        except Exception:                             # noqa: BLE001
            got = ""
        if got and hmac.compare_digest(str(got).encode("utf-8"),
                                       str(self.token).encode("utf-8")):
            return True, "令牌校验通过"
        return False, "令牌缺失或错误"

    def _deny(self, client_ip, reason, path):
        """被拒的访问要留痕：被拒绝的尝试往往比成功的更有价值"""
        try:
            self.store.log("接入被拒", "来源=%s 路径=%s 原因=%s" % (client_ip, path, reason),
                           "lan_service")
        except Exception:                             # noqa: BLE001
            pass

    # ---------------- GET /health ----------------
    def health(self):
        import config
        try:
            summary = self.store.summary()
            remote = self.store.remote_summary()
            chain_ok, chain_msg, chain_rows = self.store.verify_chain()
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "reason": "本地库不可用：%s" % str(e)[:160]}
        return {"ok": True, "service": SERVICE_NAME, "proto": PROTOCOL_VERSION,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "data_dir": str(config.DATA_ROOT),
                "alerts": summary.get("total", 0),
                "reviews": summary.get("reviews", 0),
                "audit_chain_ok": bool(chain_ok), "audit_chain": chain_msg,
                "audit_rows": chain_rows,
                "remote_records": remote.get("records", 0),
                "remote_devices": remote.get("devices", 0),
                "remote_by_kind": remote.get("by_kind", {}),
                "token_required": bool(self.token)}

    # ---------------- GET /api/status ----------------
    def status(self):
        """与 integration_api 的 status 同义（供 00 联动层在多机部署时远程查询）"""
        import config
        store = self.store
        chain_ok, chain_msg, n = store.verify_chain()
        s = store.summary()
        remote = store.remote_summary()
        return {"ok": True, "system": "03_智能终端操作",
                "alerts": s.get("total", 0), "reviews": len(store.list_reviews()),
                "audit_chain": chain_msg, "audit_ok": chain_ok, "audit_rows": n,
                "remote_records": remote.get("records", 0),
                "remote_devices": remote.get("devices", 0),
                "data_dir": str(config.DATA_ROOT)}

    # ---------------- GET /api/alerts ----------------
    def list_alerts(self, query):
        """告警列表（只读）。远端巡考客户端/联动层据此看终端上的告警态势。"""
        q = query or {}
        try:
            limit = int((q.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 1000))
        room = (q.get("room_id") or [""])[0]
        level = (q.get("level") or [""])[0]
        status = (q.get("status") or [""])[0]
        rows = self.store.list_alerts(room_id=room or None, level=level or None,
                                      status=status or None, limit=limit)
        return {"ok": True, "alerts": [
            {"alert_id": x.alert_id, "code": x.code, "level": x.level,
             "level_name": x.level_name(), "room_id": x.room_id,
             "seat_no": x.seat_no, "device_id": x.device_id, "status": x.status,
             "detail": x.detail, "created_at": x.created_at,
             "credibility": x.extra.get("credibility"),
             "review_conclusion": x.review_conclusion} for x in rows]}

    # ---------------- GET /api/records ----------------
    def list_records(self, query):
        import config
        q = query or {}
        device_id = (q.get("device_id") or [""])[0]
        kind = (q.get("kind") or [""])[0]
        try:
            limit = int((q.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 1000))
        return {"ok": True, "records": self.store.list_remote_records(
            device_id=device_id or None, kind=kind or None, limit=limit),
            "devices": self.store.remote_devices(),
            "summary": self.store.remote_summary(),
            "data_dir": str(config.DATA_ROOT)}

    # ---------------- GET /api/cockpit（网页版驾驶舱的数据源） ----------------
    def _modules(self, force=False):
        """跨模块态势（带 TTL 缓存）。

        网页每 5 秒拉一次数，如果每次都起 8 个子进程，浏览器那边就会一直等；
        这里按 MODULE_TTL 缓存，只有缓存过期才真的去聚合。
        """
        now = time.time()
        if not force and self._mod_cache is not None and (now - self._mod_at) < MODULE_TTL:
            return self._mod_cache
        rows = []
        try:
            import remote_status
            agg = remote_status.ModuleStatus(ttl=MODULE_TTL, timeout=MODULE_TIMEOUT)
            data = agg.all(force=force)
            for (_key, _parts, _name), (name, text, ok) in zip(
                    remote_status.MODULES, remote_status.ModuleStatus.brief(data)):
                rows.append({"name": name, "brief": text, "ok": bool(ok)})
        except Exception as e:                        # noqa: BLE001
            rows = [{"name": "模块聚合不可用", "brief": str(e)[:80], "ok": False}]
        self._mod_cache = rows
        self._mod_at = now
        return rows

    def cockpit(self, force_modules=False):
        """网页版大数据驾驶舱的一次性快照（只读，与 Py GUI 同一份数据口径）。

        设计取舍：
          · **一次请求给全**：KPI/图表/明细/模块态势一个 JSON 回来，避免浏览器打 6 个请求；
          · 计数全部来自 `store.stats()` 的 SQL 聚合，不在服务端做"最多看 2000 条"的截断；
          · 明细只给态势必需字段（考场/级别/状态），**不带考生姓名与证件号**；
          · 审计链与模块聚合带 TTL，避免每 5 秒把库和子进程折腾一遍。
        """
        import config
        t0 = time.time()
        store = self.store
        try:
            st = store.stats(hours=24, days=7, recent=60)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "reason": "本地库不可用：%s" % str(e)[:160]}
        try:
            remote = store.remote_summary()
            devs = store.remote_devices()
        except Exception:                             # noqa: BLE001
            remote, devs = {}, []

        now = time.time()
        if self._chain_at and (now - self._chain_at) < CHAIN_TTL:
            chain_ok, chain_msg, chain_n = self._chain
        else:
            try:
                chain_ok, chain_msg, chain_n = store.verify_chain()
            except Exception as e:                    # noqa: BLE001
                chain_ok, chain_msg, chain_n = False, str(e)[:120], 0
            self._chain = (bool(chain_ok), chain_msg, int(chain_n or 0))
            self._chain_at = now

        status_text = dict(config.ALERT_STATUS_TEXT)
        by_level = dict(st.get("by_level") or {})
        by_status = dict(st.get("by_status") or {})
        codes = [(c, n) for c, n in (st.get("by_code") or [])]
        code_map = dict(codes)

        recent = []
        for a in (st.get("recent") or [])[:60]:
            try:
                overdue = bool(a.status == "pending" and a.overdue())
            except Exception:                         # noqa: BLE001
                overdue = False
            recent.append({
                "time": (a.created_at or "")[5:16],
                "level": a.level,
                "level_name": config.LEVEL_META.get(a.level, {}).get("name", a.level),
                "code": a.code,
                "code_name": config.ALERT_CODE_TEXT.get(a.code, a.code),
                "room": a.room_id or "—",
                "status": a.status,
                "status_name": status_text.get(a.status, a.status),
                "overdue": overdue,
            })

        return {
            "ok": True,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "interval": 5,
            "kpi": {
                "total": int(st.get("total") or 0),
                "red": int(by_level.get(config.LEVEL_RED, 0) or 0),
                "orange": int(by_level.get(config.LEVEL_ORANGE, 0) or 0),
                "pending": int(by_status.get("pending", 0) or 0),
                "overdue": int(st.get("overdue") or 0),
                "reviews": int(st.get("reviews") or 0),
                "unsynced": int(st.get("unsynced") or 0),
                "last_hour": int(st.get("last_hour") or 0),
                "remote": int(remote.get("records") or 0),
                "devices": int(remote.get("devices") or 0),
                "fire": int(code_map.get("FIRE_ALARM", 0) or 0),
                "smoke": int(code_map.get("FIRE_SMOKE", 0) or 0),
                "audit": int(st.get("audits") or chain_n or 0),
                "chain_ok": bool(chain_ok),
                "chain_msg": chain_msg,
                "remote_last": remote.get("last_received") or "",
            },
            "by_level": by_level,
            "by_status": by_status,
            "by_code": [{"code": c, "name": config.ALERT_CODE_TEXT.get(c, c), "count": n}
                        for c, n in codes[:12]],
            "hourly": [[int(ts), int(n)] for ts, n in (st.get("hourly") or [])],
            "daily": [[int(ts), int(n)] for ts, n in (st.get("daily") or [])],
            "recent": recent,
            "devices": [{"id": d.get("device_id", ""),
                         "name": d.get("device_name", ""),
                         "records": int(d.get("records") or 0),
                         "last": (d.get("last_seen") or "")[5:16]} for d in devs[:20]],
            "modules": self._modules(force=force_modules),
            "status_text": status_text,
            "role_text": dict(config.ROLE_TEXT),
            "data_dir": str(config.DATA_ROOT),
            "cost_ms": round((time.time() - t0) * 1000.0, 1),
        }

    # ---------------- POST /api/records ----------------
    def handle_records(self, body, client_ip="-"):
        """
        接入一批远端记录。返回 (http_status, dict)。
        幂等：同一条记录重发只算 duplicated（记录 ID 由本端按内容算，不信对端给的 ID）。
        """
        import config
        if not isinstance(body, dict):
            return 400, {"ok": False, "reason": "body 必须是 JSON 对象"}
        records = body.get("records")
        if not isinstance(records, list):
            return 400, {"ok": False, "reason": "缺少 records 数组"}
        if not records:
            return 200, {"ok": True, "accepted": 0, "duplicated": 0,
                         "reason": "空批次，未写入"}
        if len(records) > config.LAN_BATCH_MAX:
            return 400, {"ok": False, "reason": "单批最多 %d 条（收到 %d 条），请分批"
                         % (config.LAN_BATCH_MAX, len(records))}
        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return 400, {"ok": False, "reason": "缺少 device_id（对端设备标识）"}
        kind = str(body.get("kind") or "audit").strip() or "audit"
        note = ""
        if kind not in KNOWN_KINDS:
            note = "未分类 kind（已照原样存档）"
        try:
            r = self.store.save_remote_batch(
                device_id=device_id,
                device_name=str(body.get("device_name") or ""),
                kind=kind, records=records, chain=body.get("chain") or {},
                client_ip=client_ip)
            self.store.prune_remote()
        except Exception as e:                        # noqa: BLE001
            return 500, {"ok": False, "reason": "写入失败：%s" % str(e)[:200]}
        r.update({"ok": True, "proto": PROTOCOL_VERSION, "from": client_ip,
                  "note": note})
        return 200, r

    # ---------------- 发布命令（03 当指挥端） ----------------
    def commands_for(self, target, ips=None):
        """GET /api/command?target=xxx&ip=a,b —— 目标设备拉取待执行命令（只读）

        target 是设备标识；ip 是本机 IPv4（可多个，逗号分隔）。
        两者**任一命中**就返回 —— 这样"只知道对端 IP 就下发的命令"也能被领走。
        """
        if not target and not (ips or []):
            return {"ok": False, "reason": "缺少 target（本机设备标识）或 ip"}
        try:
            items = self.store.commands_for(target, limit=20, pending_only=True,
                                            ips=ips or [])
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "reason": "查询失败：%s" % str(e)[:160]}
        return {"ok": True, "target": target, "ips": list(ips or []),
                "count": len(items), "commands": items,
                "types": self.store.COMMAND_TYPES, "proto": PROTOCOL_VERSION}

    def list_commands(self, limit=50):
        """GET /api/commands —— 命令台账（含回执）"""
        try:
            rows = self.store.list_commands(limit=limit)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "reason": "查询失败：%s" % str(e)[:160]}
        return {"ok": True, "commands": rows, "types": self.store.COMMAND_TYPES}

    def handle_publish_command(self, body, client_ip="-"):
        """
        POST /api/command —— 下发一条命令。body:
            {target, kind, params, operator, note}
        target 可以是**设备标识**（PC-A-01 / 计算机名）或**该设备的 IPv4**；
        给 IP 时先按设备台账反查成设备标识再落库，同时把 IP 一并存进 target_ip，
        保证"对端按标识轮询"和"对端按 IP 轮询"都能领到同一条命令。
        kind 必须在白名单里（COMMAND_TYPES），否则拒绝 —— 这条通道**永远不是**
        任意代码执行入口。
        """
        if not isinstance(body, dict):
            return 400, {"ok": False, "reason": "body 必须是 JSON 对象"}
        raw_target = str(body.get("target") or "").strip()
        kind = str(body.get("kind") or "").strip()
        if not raw_target:
            return 400, {"ok": False, "reason": "缺少 target（目标设备标识或 IPv4）"}
        if kind not in self.store.COMMAND_TYPES:
            return 400, {"ok": False, "reason": "命令类型不在白名单：%s（可用：%s）"
                         % (kind, "、".join(sorted(self.store.COMMAND_TYPES)))}
        # 目标归一化：是 IPv4 就先反查设备标识（查不到就保留 IP，对端按 IP 领）
        ip = _norm_ip(raw_target)
        target = raw_target
        resolved = None
        if ip:
            try:
                dev = self.store.device_by_ip(ip)
            except Exception:                         # noqa: BLE001
                dev = None
            if dev and dev.get("device_id"):
                target = dev["device_id"]
                resolved = dev.get("device_name") or ""
        cmd_id = self.store.push_target_command(
            target, kind, params=body.get("params") or {},
            operator=str(body.get("operator") or client_ip),
            note=str(body.get("note") or ""), target_ip=ip)
        if not cmd_id:
            return 500, {"ok": False, "reason": "写入命令队列失败"}
        return 200, {"ok": True, "cmd_id": cmd_id, "target": target,
                     "target_ip": ip, "resolved_from_ip": bool(ip and resolved),
                     "device_name": resolved or "", "kind": kind,
                     "from": client_ip, "proto": PROTOCOL_VERSION}

    def handle_command_result(self, body, client_ip="-"):
        """
        POST /api/command_result —— 目标设备回执。body:
            {cmd_id, ok, result:{...}, operator}
        """
        if not isinstance(body, dict):
            return 400, {"ok": False, "reason": "body 必须是 JSON 对象"}
        cmd_id = str(body.get("cmd_id") or "").strip()
        if not cmd_id:
            return 400, {"ok": False, "reason": "缺少 cmd_id"}
        ok = bool(body.get("ok"))
        result = body.get("result")
        if not isinstance(result, dict):
            result = {"output": str(result)[:2000]} if result is not None else {}
        result.setdefault("from", client_ip)
        try:
            self.store.finish_command(cmd_id, ok=ok, result=result,
                                      operator=str(body.get("operator") or client_ip))
        except Exception as e:                        # noqa: BLE001
            return 500, {"ok": False, "reason": "写入回执失败：%s" % str(e)[:160]}
        return 200, {"ok": True, "cmd_id": cmd_id, "recorded": True,
                     "proto": PROTOCOL_VERSION}

    # ---------------- POST /api/ingest_alert ----------------
    def handle_alert(self, body, client_ip="-"):
        """
        接入一条业务告警：直接复用 03 自己的 integration_api.op_ingest_alert，
        所以分级、去重、画质可信度降权、红色强制确认的行为与本机告警**完全一致**。
        """
        payload = body if isinstance(body, dict) else None
        if payload is None:
            return 400, {"ok": False, "reason": "body 必须是 JSON 对象"}
        if isinstance(payload.get("alert"), dict):     # 允许 {"alert": {...}} 包一层
            payload = dict(payload["alert"])
        if not payload.get("code"):
            return 400, {"ok": False, "reason": "缺少告警码 code"}
        payload.setdefault("source", "lan/%s" % client_ip)
        try:
            import integration_api
            out = integration_api.op_ingest_alert(payload)
        except Exception as e:                        # noqa: BLE001
            return 500, {"ok": False, "reason": "告警接入失败：%s" % str(e)[:200]}
        out = dict(out or {})
        out.setdefault("ok", True)
        out["from"] = client_ip
        out["proto"] = PROTOCOL_VERSION
        return 200 if out.get("ok") else 500, out


def make_handler(service):
    """把 LanService 包成 http.server 的处理类"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ExamTerminalLan/1.0"
        protocol_version = "HTTP/1.1"

        # 默认实现会把每条请求打到 stderr；批量启动 .bat 时窗口会刷屏，这里静音
        def log_message(self, fmt, *args):            # noqa: A003
            pass

        def handle_one_request(self):
            """浏览器/大屏刷新时会直接掐掉 keep-alive 连接。

            http.server 默认会把 ConnectionResetError 打成整段 traceback，
            服务窗口一天下来全是"远程主机强迫关闭了一个现有的连接"；
            对端正常断开不是错误，静默收尾即可（真正的异常仍会抛给上层）。
            """
            try:
                BaseHTTPRequestHandler.handle_one_request(self)
            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, TimeoutError):
                self.close_connection = True

        # ---- 工具 ----
        def _client(self):
            return (self.client_address[0] if self.client_address else "") or ""

        def _send(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, data, "application/json; charset=utf-8")

        def _send_bytes(self, status, data, ctype):
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _cors(self):
            """只给"本机页面"放行跨源读取。

            场景：大屏常把 cockpit.html 直接双击（file://）打开，再连 127.0.0.1:9113，
            这属于跨源 + 自带 X-Exam-Token 头，浏览器会先发 OPTIONS 预检。
            所以这里对 Origin=null（file://）与 127.0.0.1/localhost 回显 CORS 头，
            其它来源一个头都不给 —— 浏览器直接读不到响应体，与"令牌把关"是两道独立的门。
            """
            origin = self.headers.get("Origin") or ""
            allow = ""
            if origin == "null":
                allow = "null"
            elif origin:
                try:
                    host = urllib.parse.urlparse(origin).hostname or ""
                except ValueError:
                    host = ""
                if is_loopback(host):
                    allow = origin
            if allow:
                self.send_header("Access-Control-Allow-Origin", allow)
                self.send_header("Access-Control-Allow-Headers",
                                 "%s, Content-Type" % TOKEN_HEADER)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")

        def _auth(self, path):
            ok, why = service.authorized(self._client(), self.headers, path)
            if not ok:
                service._deny(self._client(), why, path)
                self._send(401, {"ok": False, "reason": why,
                                 "hint": "请在请求头带 %s（对端用 EXAM_TERMINAL_TOKEN 配置）"
                                         % TOKEN_HEADER})
            return ok

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                return None, "缺少请求体"
            if n > MAX_BODY_BYTES:
                return None, "请求体过大（上限 %d 字节）" % MAX_BODY_BYTES
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8", "replace") or "{}"), ""
            except ValueError as e:
                return None, "请求体不是合法 JSON：%s" % str(e)[:120]

        # ---- 路由 ----
        def do_GET(self):                             # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            # ---- 网页版大数据驾驶舱（页面本身不含数据，故不要求令牌）----
            if path in ("/", "/cockpit", "/index.html"):
                html = read_cockpit_html()
                if html is None:
                    self._send(500, {"ok": False,
                                     "reason": "缺少 web/cockpit.html（页面文件未随仓库提供？）"})
                else:
                    self._send_bytes(200, html, "text/html; charset=utf-8")
                return
            # ---- 驾驶舱数据（含告警态势，必须鉴权）----
            if path == "/api/cockpit":
                if not self._auth(path):
                    return
                q = urllib.parse.parse_qs(parsed.query)
                force = (q.get("modules") or [""])[0] in ("1", "true", "force")
                self._send(200, service.cockpit(force_modules=force))
                return
            if path == "/health":
                # 本机（回环）免令牌；跨设备一律要令牌 —— 未配令牌时服务根本不会监听非本机
                if not self._auth(path):
                    return
                self._send(200, service.health())
                return
            if path == "/api/records":
                if not self._auth(path):
                    return
                self._send(200, service.list_records(
                    urllib.parse.parse_qs(parsed.query)))
                return
            if path == "/api/status":
                if not self._auth(path):
                    return
                self._send(200, service.status())
                return
            if path == "/api/alerts":
                if not self._auth(path):
                    return
                self._send(200, service.list_alerts(
                    urllib.parse.parse_qs(parsed.query)))
                return
            # ---- 发布命令：目标设备（01/02）拉取待执行命令 ----
            if path == "/api/command":
                if not self._auth(path):
                    return
                q = urllib.parse.parse_qs(parsed.query)
                # ip 可以是逗号分隔的多条（一台机器常有多个网卡）
                ips = [x.strip() for x in
                       ",".join(q.get("ip") or [""]).split(",") if x.strip()]
                self._send(200, service.commands_for(
                    (q.get("target") or [""])[0], ips=ips))
                return
            # ---- 命令台账（含回执），供命令台/驾驶舱展示 ----
            if path == "/api/commands":
                if not self._auth(path):
                    return
                q = urllib.parse.parse_qs(parsed.query)
                limit = int((q.get("limit") or ["50"])[0] or 50)
                self._send(200, service.list_commands(limit))
                return
            self._send(404, {"ok": False, "reason": "未知路径：%s" % path,
                             "paths": ["/", "/health", "/api/status", "/api/alerts",
                                       "/api/cockpit", "/api/records", "/api/command",
                                       "/api/commands",
                                       "POST /api/records", "POST /api/ingest_alert",
                                       "POST /api/command", "POST /api/command_result"]})

        def do_OPTIONS(self):                         # noqa: N802
            """CORS 预检（file:// 打开大屏时会用到；具体放行规则见 _cors）"""
            try:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_POST(self):                            # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path not in ("/api/records", "/api/ingest_alert",
                            "/api/command", "/api/command_result"):
                self._send(404, {"ok": False, "reason": "未知路径：%s" % path})
                return
            if not self._auth(path):
                return
            body, err = self._read_body()
            if err:
                self._send(400, {"ok": False, "reason": err})
                return
            if path == "/api/command":
                status, out = service.handle_publish_command(body, self._client())
                self._send(status, out)
                return
            if path == "/api/command_result":
                status, out = service.handle_command_result(body, self._client())
                self._send(status, out)
                return
            if path == "/api/records":
                status, out = service.handle_records(body, self._client())
            else:
                status, out = service.handle_alert(body, self._client())
            self._send(status, out)

    return Handler


def build_server(service, host, port):
    handler = make_handler(service)
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    httpd.daemon_threads = True
    return httpd


def serve(host, port, service, banner=True):
    httpd = build_server(service, host, port)
    real_host, real_port = httpd.server_address[0], httpd.server_address[1]
    if banner:
        print("=" * 64)
        print(SERVICE_NAME)
        print("监听地址：%s:%d" % (real_host, real_port))
        if not is_loopback(host):
            print("!" * 64)
            print("警告：正在监听 %s —— 同网段设备可以访问本终端。" % host)
            print("      只开放了「记录接入」和「告警接入」；复核/擦除/命令控制台不经此端口。")
            print("      令牌来自 EXAM_TERMINAL_TOKEN 或 data\\lan_token.txt，请勿外泄。")
            print("!" * 64)
        print("本机自检：http://127.0.0.1:%d/health" % real_port)
        print("对端配置：set EXAM_TERMINAL_URL=http://<本机局域网IP>:%d" % real_port)
        print("=" * 64)
        sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止监听。")
    finally:
        httpd.server_close()


# ---------------- 自检 ----------------
def _selfcheck_child():
    """子进程分支：在临时数据目录里起临时端口，跑一遍真 HTTP 往返"""
    import storage
    results = []

    def chk(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                             ("　— " + detail) if detail else ""))

    store = storage.Store()
    service = LanService(store=store, token="selfcheck-token")
    httpd = build_server(service, "127.0.0.1", 0)
    port = httpd.server_address[1]
    import threading
    import urllib.error
    import urllib.request
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def call(path, body=None, token=None, method=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method or
                                     ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header(TOKEN_HEADER, token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def fetch_text(path):
        """按原文取回（HTML 页面不是 JSON，call() 解析不了）"""
        url = "http://127.0.0.1:%d%s" % (port, path)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return (r.status, r.headers.get("Content-Type", ""),
                        r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, "", e.read().decode("utf-8", "replace")

    try:
        st, out = call("/health")
        chk("健康检查可用", st == 200 and out.get("ok"), "proto=%s" % out.get("proto"))

        recs = [{"ts": "2026-10-15 09:00:01", "action": "人脸核验", "detail": "PASS",
                 "operator": "owner"},
                {"ts": "2026-10-15 09:00:09", "action": "证件核对", "detail": "一致",
                 "operator": "owner"}]
        payload = {"device_id": "SELF-PC", "device_name": "01_人脸识别与身份核对",
                   "kind": "audit",
                   "chain": {"algo": "sha256", "head": "abc123", "count": 2},
                   "records": recs}
        st, out = call("/api/records", payload)
        chk("首批记录接入", st == 200 and out.get("accepted") == 2,
            "accepted=%s duplicated=%s" % (out.get("accepted"), out.get("duplicated")))

        st, out = call("/api/records", payload)
        chk("重发不产生重复（幂等）", st == 200 and out.get("duplicated") == 2
            and out.get("accepted") == 0,
            "accepted=%s duplicated=%s" % (out.get("accepted"), out.get("duplicated")))

        st, out = call("/api/records", {"device_id": "X", "records": []})
        chk("空批次不报错也不写入", st == 200 and out.get("accepted") == 0)

        st, out = call("/api/records", {"device_name": "无 device_id", "records": recs})
        chk("缺 device_id 被拒", st == 400, out.get("reason", ""))

        st, out = call("/api/records", {"device_id": "X", "records": []},
                       token="bad-token")
        chk("本机访问免令牌（回环）", st == 200,
            "带错误令牌仍放行（本机免令牌），HTTP %s" % st)

        # 跨设备来源（非回环）的鉴权：这里直接调判定函数，不必真从另一台机器发请求
        class _H(dict):
            pass

        ok1, why1 = service.authorized("192.168.1.50", _H())
        chk("跨设备来源无令牌被拒", not ok1, why1)
        ok2, why2 = service.authorized("192.168.1.50", _H({TOKEN_HEADER: "selfcheck-token"}))
        chk("跨设备来源带对令牌放行", ok2, why2)
        ok3, why3 = service.authorized("192.168.1.50", _H({TOKEN_HEADER: "错的"}))
        chk("跨设备来源令牌错误被拒", not ok3, why3)
        nob = LanService(store=store, token="")
        ok4, why4 = nob.authorized("192.168.1.50", _H())
        chk("未配令牌时跨设备一律拒绝（fail-closed）", not ok4, why4)

        st, out = call("/api/ingest_alert", {"code": "FACE_MISMATCH", "device_id": "CAM-A01-1",
                                            "room_id": "A-01", "detail": "来自 01 的核验告警"})
        chk("告警接入（走 03 自己的分级流水线）", st == 200 and out.get("ok")
            and out.get("alert_id"), "级别=%s 强制确认=%s" % (out.get("level_name"),
                                                          out.get("force_confirm")))

        st, out = call("/api/ingest_alert", {"detail": "没有 code"})
        chk("缺告警码被拒", st == 400, out.get("reason", ""))

        st, out = call("/api/status")
        chk("只读状态查询可用（供 00 联动层远程查）",
            st == 200 and out.get("ok") and out.get("system") == "03_智能终端操作",
            "告警=%s" % out.get("alerts"))

        st, out = call("/api/alerts?limit=10")
        chk("只读告警列表可用（能看到刚接入的那条）",
            st == 200 and any(x.get("code") == "FACE_MISMATCH"
                              for x in out.get("alerts", [])),
            "告警列表=%d 条" % len(out.get("alerts", [])))

        # ---- 网页版大数据驾驶舱 ----
        import re
        st, ctype, text = fetch_text("/")
        # 判"零外链"要看真标签：先去掉 HTML 注释（注释里就写了"不要用 CDN"这类说明，
        # 朴素子串判断会把说明文字当外链，误判过一次）
        markup = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        has_ext = bool(re.search(r"""(?:src|href)\s*=\s*["']\s*https?://""", markup,
                                 re.I))
        chk("网页驾驶舱页面可打开（无外部 CDN 依赖）",
            st == 200 and "text/html" in (ctype or "") and "<canvas" in text
            and not has_ext,
            "%s %d 字节 外链=%s" % (ctype, len(text), "有" if has_ext else "无"))

        st, out = call("/api/cockpit")
        need = ("kpi", "by_level", "by_code", "hourly", "daily",
                "recent", "devices", "modules", "status_text")
        chk("驾驶舱数据接口字段齐全（页面按这些字段渲染）",
            st == 200 and out.get("ok") and all(k in out for k in need),
            "KPI=%d 项 模块=%d 个" % (len(out.get("kpi") or {}),
                                     len(out.get("modules") or [])))

        st, out = call("/api/cockpit")
        total_db = store.summary().get("total", 0)
        chk("驾驶舱计数与库内一致（同一份 SQL 聚合口径）",
            st == 200 and (out.get("kpi") or {}).get("total") == total_db,
            "接口=%s 库内=%s" % ((out.get("kpi") or {}).get("total"), total_db))

        summary = store.remote_summary()
        chk("远端记录落库分链存储", summary.get("records") == 2,
            "记录=%s 设备=%s 类别=%s" % (summary.get("records"), summary.get("devices"),
                                      summary.get("by_kind")))

        rows = store.list_remote_records(device_id="SELF-PC")
        actions = set((r.get("action") or "") for r in rows)
        chk("远端记录可查（含解密后的明细）",
            len(rows) == 2 and actions == {"人脸核验", "证件核对"},
            "共 %d 条：%s" % (len(rows), "、".join(sorted(actions))))

        ok, msg, n = store.verify_chain()
        chk("本机审计链仍然完整（远端记录没有并进本机链）", ok, msg)

        devs = store.remote_devices()
        chk("远端设备已登记链头", devs and devs[0].get("chain_head") == "abc123",
            "设备=%s 链头=%s" % (devs[0].get("device_id") if devs else "-",
                               devs[0].get("chain_head") if devs else "-"))

        # ---- 按 IPv4 下发命令（2026-10-18 新增） ----
        # 注意：这几项放在最后 —— 它们会往设备台账里再登记一台设备，
        # 上面的"远端设备已登记链头"断言依赖 devs[0] 是 SELF-PC。
        # 真实部署里 IP 是 01/02 上报时自然带上的，这里直接给台账写一个测试 IP。
        store.save_remote_batch("CAM-A01-1", "A-01 前门", "order", [],
                                client_ip="192.168.1.20")
        chk("设备台账记录来源 IPv4（供按 IP 下发用）",
            (store.device_by_ip("192.168.1.20") or {}).get("device_id") == "CAM-A01-1",
            "device_by_ip -> %s" % (store.device_by_ip("192.168.1.20") or {}).get("device_id"))

        st, out = call("/api/command",
                       {"target": "192.168.1.20", "kind": "ping",
                        "operator": "owner"}, method="POST")
        chk("按 IPv4 下发命令：自动反查成设备标识",
            st == 200 and out.get("ok") and out.get("target") == "CAM-A01-1"
            and out.get("target_ip") == "192.168.1.20",
            "target=%s ip=%s" % (out.get("target"), out.get("target_ip")))
        cmd_id = out.get("cmd_id") or ""

        st, out = call("/api/command?target=CAM-A01-1")
        chk("对端用设备标识轮询能领到「按 IP 下发」的命令",
            st == 200 and any(c.get("cmd_id") == cmd_id
                              for c in out.get("commands", [])),
            "领到 %d 条" % len(out.get("commands", [])))

        st, out = call("/api/command", {"target": "192.168.1.99", "kind": "status",
                                        "operator": "owner"}, method="POST")
        unknown_cmd = out.get("cmd_id") or ""
        st, out = call("/api/command?target=NOT-THIS-DEVICE&ip=192.168.1.99")
        chk("台帐里没有的 IP 也能被该 IP 的设备按 ip= 领走",
            st == 200 and any(c.get("cmd_id") == unknown_cmd
                              for c in out.get("commands", [])),
            "target_ip=%s" % (((out.get("commands") or [{}])[0]).get("target_ip")))
        st, out = call("/api/command?target=NOT-THIS-DEVICE")
        chk("别的设备按标识轮询领不到这条（目标隔离）",
            st == 200 and not any(c.get("cmd_id") == unknown_cmd
                                  for c in out.get("commands", [])))
    finally:
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for _n, c, _d in results if c)
    print("局域网接入服务自检：%d/%d 项通过" % (passed, len(results)))
    print(json.dumps({"ok": passed == len(results), "passed": passed,
                      "total": len(results)}, ensure_ascii=False))
    return 0 if passed == len(results) else 1


def _selfcheck_parent():
    """父进程分支：临时数据目录 + 子进程跑上面的检查，绝不碰真实 data\\"""
    import shutil
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp(prefix="lan_service_selfcheck_")
    env = dict(os.environ)
    env["EXAM_TERMINAL_HOME"] = tmp
    env["_EXAM_LAN_SELFCHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, os.path.abspath(__file__), "--selfcheck"]
    print("局域网接入服务自检（临时数据目录：%s）" % tmp)
    try:
        p = subprocess.run(cmd, env=env, capture_output=True)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        print(out.rstrip())
        if p.returncode != 0 and err.strip():
            print(err.rstrip()[-800:])
        return p.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=SERVICE_NAME)
    ap.add_argument("--host", default=None, help="监听地址（默认取 config.LAN_HOST=127.0.0.1）")
    ap.add_argument("--port", type=int, default=None, help="监听端口（默认 config.LAN_PORT=9113）")
    ap.add_argument("--token", default="", help="接入令牌（默认取 EXAM_TERMINAL_TOKEN / data\\lan_token.txt）")
    ap.add_argument("--print-token", action="store_true", help="打印当前令牌后退出")
    ap.add_argument("--selfcheck", action="store_true", help="自检（临时数据目录 + 临时端口）")
    ap.add_argument("--allow-local-no-token", action="store_true", default=True,
                    help="本机（回环）访问免令牌，默认开启")
    ns = ap.parse_args(argv)

    if ns.selfcheck:
        if os.environ.get("_EXAM_LAN_SELFCHECK") == "1":
            return _selfcheck_child()
        return _selfcheck_parent()

    import config
    if ns.print_token:
        print(load_token(create=True))
        return 0

    host = ns.host or config.LAN_HOST
    port = int(ns.port or config.LAN_PORT)
    token = ns.token.strip() or load_token(create=True)

    if not is_loopback(host) and not token:
        print("拒绝启动：要监听 %s（非本机）必须配置接入令牌。" % host)
        print("  方式一：set EXAM_TERMINAL_TOKEN=<一串随机口令>")
        print("  方式二：先跑 python lan_service.py --print-token 生成 data\\lan_token.txt")
        return 2
    if not is_loopback(host) and not ns.token.strip():
        print("提示：本次使用 data\\lan_token.txt（或环境变量）里的令牌，请一并配到对端。")

    print("令牌：%s" % ("已配置（不回显）" if token else "未配置（仅本机可访问）"))
    if is_loopback(host):
        print("当前只监听本机。要跨设备请设 EXAM_TERMINAL_LAN_HOST=0.0.0.0 或加 --host 0.0.0.0")
    svc = LanService(token=token, allow_local_no_token=ns.allow_local_no_token)
    try:
        serve(host, port, svc)
    except OSError as e:
        print("无法监听 %s:%s —— %s" % (host, port, e))
        print("端口可能已被占用，或该地址不属于本机。")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
