# -*- coding: utf-8 -*-
r"""
03 智能终端 · 跨模块状态聚合（供驾驶舱展示全系统态势）

为什么要单独一个文件：
  03 的驾驶舱要显示"整个系统"的态势（考生数据、保密室、无障碍、考场应用…），
  但那些模块与本模块的 `config` / `models` / `storage` **同名不同物**，
  同进程 import 必然互相污染 —— 这也是本项目既有约定（00_集成联动/linkage.py）
  一律用"子进程 + 单行 JSON"调对端 `integration_api.py` 的原因。

设计：
  · 只调对端的只读 `status` op，绝不触发写操作；
  · 带 TTL 缓存（默认 30 秒），避免每次刷新都起 5 个子进程；
  · 任何一个模块缺失/报错都**如实标注**，不伪造数据、不影响其它模块；
  · 子进程超时/无输出都按"不可用"处理，不抛出到界面。

用法：
    agg = ModuleStatus()
    data = agg.all()                  # {key: {...}} 带缓存
    rows = ModuleStatus.brief(data)   # [(名称, 摘要文字, 是否可用), ...]  直接给界面用
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

ALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: key -> (目录名, 显示名)
MODULES = (
    ("01", ("01_人脸识别与身份核对", "01_人脸识别与身份核对"), "01 人脸识别与身份核对"),
    ("02", ("02_摄像头核查与视频质量诊断",), "02 摄像头核查与视频质量"),
    ("05", ("05_考生数据保护",), "05 考生数据保护"),
    ("06", ("06_智能安检与手机作弊器材检测",), "06 智能安检"),
    ("08", ("08_试卷流转与保密室智能监控",), "08 试卷流转与保密室"),
    ("09", ("09_无障碍与特殊考生保障",), "09 无障碍与特殊考生"),
    ("10", ("10_普通考场+特殊考场应用",), "10 普通/特殊考场应用"),
    ("11", ("11_摄像头AI识别",), "11 摄像头 AI 识别"),
)

#: 有些模块的 integration_api 不在标准 op 表里，或 status 需要参数 —— 这里只调确实支持的
DEFAULT_TTL = 30.0


def _dir_of(parts):
    """按**逐级拼接**找模块目录。

    为什么不能只按单层拼：01 是"同名嵌套"目录（`01_人脸识别与身份核对/01_人脸识别与身份核对/`），
    单层拼只会得到外层那个没有 integration_api.py 的目录，于是驾驶舱长期把 01 显示成
    "不可用（未找到 integration_api.py）"—— 其实它的接口一直是好的。
    """
    cur = ALL_DIR
    for p in parts:
        nxt = os.path.join(cur, p)
        if not os.path.isdir(nxt):
            nxt = os.path.join(ALL_DIR, p)      # 该段是相对 ALL_DIR 的完整路径
        if not os.path.isdir(nxt):
            return ""
        cur = nxt
    return cur if os.path.isfile(os.path.join(cur, "integration_api.py")) else ""


def _brief(key, st):
    """把一个模块的 status 结果压成一行中文摘要（只挑**真实存在**的字段）"""
    if not isinstance(st, dict) or not st.get("ok"):
        return ""
    if key == "01":
        # 01 的 status 给的是 candidates（考生列表）与 index_ready，不是 accounts
        cands = st.get("candidates")
        n = len(cands) if isinstance(cands, list) else (cands if cands is not None else "—")
        ready = st.get("index_ready")
        return "考生 %s 人 / 特征索引 %s" % (
            n, "已就绪" if ready else ("未就绪" if ready is not None else "—"))
    if key == "02":
        od = st.get("open_orders") or st.get("orders") or {}
        if isinstance(od, dict):
            od = od.get("total", "—")
        return "机位 %s / 工单 %s" % (st.get("devices", "—"), od)
    if key == "05":
        return ("考生 %s 人 / 同意 %s 份 / 加密率 %s / 审计链 %s"
                % (st.get("candidates", "—"), st.get("consents", "—"),
                   st.get("encrypt_rate", "—"), st.get("audit_chain", "—")))
    if key == "06":
        al = st.get("alarms") or {}
        if isinstance(al, dict):
            al = al.get("total", "—")
        return "过检 %s / 报警 %s" % (st.get("passes", st.get("checks", "—")), al)
    if key == "08":
        return ("保密室 %s 间 / 机位 %s / 试卷袋 %s / 报警 %s（红 %s）"
                % (st.get("rooms", "—"), st.get("cameras", "—"), st.get("bags", "—"),
                   st.get("alarms", "—"), st.get("open_red", "—")))
    if key == "09":
        need = st.get("hard_fail") or []
        return ("通道 %s/%s 可用 / 档案 %s 人 / 硬指标未达标 %d 项"
                % (st.get("channels_usable", "—"), st.get("channels", "—"),
                   st.get("profiles", "—"), len(need)))
    if key == "10":
        al = st.get("alerts") or {}
        return ("考场 %s（普通 %s / 特殊 %s，已布防 %s）/ 告警 %s（红 %s）"
                % (st.get("rooms", "—"), st.get("standard_rooms", "—"),
                   st.get("special_rooms", "—"), st.get("armed", "—"),
                   al.get("total", "—"), al.get("red", "—")))
    if key == "11":
        return "机位 %s" % st.get("devices", st.get("cameras", "—"))
    return ""


class ModuleStatus(object):
    """跨模块 status 聚合（带 TTL 缓存，线程安全）"""

    def __init__(self, ttl=DEFAULT_TTL, timeout=25):
        self.ttl = float(ttl)
        self.timeout = float(timeout)
        self._cache = {}
        self._ts = 0.0
        self._lock = threading.Lock()

    # ---------------- 单个模块 ----------------
    def call(self, key, parts, op="status", args=None, cwd=None):
        """调某个模块的 integration_api.py，返回其 JSON（失败返回 ok=False 的字典）"""
        d = cwd or _dir_of(parts)
        if not d:
            return {"ok": False, "reason": "未找到该模块的 integration_api.py"}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cmd = [sys.executable, os.path.join(d, "integration_api.py"),
               "--op", op, "--args", json.dumps(args or {}, ensure_ascii=False)]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "env": env, "cwd": d}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            p = subprocess.run(cmd, timeout=self.timeout, **kwargs)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "调用超时（%ss）" % int(self.timeout)}
        except Exception as e:
            return {"ok": False, "reason": "调用失败：%s" % e}
        out = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        for line in reversed(out):                     # 取最后一行 JSON
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "reason": (err[-1][:120] if err else
                                       "无 JSON 输出（退出码 %s）" % p.returncode)}

    # ---------------- 全部（带缓存） ----------------
    def all(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._cache and (now - self._ts) < self.ttl:
                return self._cache
        data = {}
        for key, parts, name in MODULES:
            st = self.call(key, parts)
            st = dict(st) if isinstance(st, dict) else {"ok": False, "reason": "返回格式异常"}
            st["_name"] = name
            st["_brief"] = _brief(key, st)
            data[key] = st
        with self._lock:
            self._cache = data
            self._ts = time.time()
        return data

    def age(self):
        """缓存年龄（秒）；0 表示从未刷新"""
        return (time.time() - self._ts) if self._ts else 0.0

    @staticmethod
    def brief(data):
        """给界面用的行列表：[(显示名, 摘要或原因, 是否可用), ...]（按 MODULES 顺序）"""
        rows = []
        for key, _parts, name in MODULES:
            st = (data or {}).get(key) or {}
            ok = bool(st.get("ok"))
            text = st.get("_brief") or st.get("reason") or "无数据"
            rows.append((name, text, ok))
        return rows
