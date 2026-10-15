# -*- coding: utf-8 -*-
r"""
03 智能终端 · 跨机账号管理（远程查看 / 新建 / 重置口令 / 删除 01、02 的登录账号）

需求背景：
  01 人脸识别、02 摄像头核查各有自己的登录账号体系（三套互不相通）：
    · 03 终端账户  = terminal.db 的 kv['accounts']（本模块自己管）
    · 01 登录账号  = 01/data/exam.db 的 users 表（owner/admin/teacher）
    · 02 登录账号  = 02/data/camera.db 的 kv['web.accounts']（owner/admin）
  以前只能在各自机器上改；现在由 03 充当"账号管理中心"。

安全设计（四道闸，缺一不可）：
  1. **03 侧权限**：调用方必须是 owner（`require("manage")`），管理员不可用；
  2. **03 侧留痕**：每次调用先写本机审计链（谁、对谁、做了什么）；
  3. **对端越级防护**：01/02 的 accounts 会再判一次"只能管严格低于自己的角色"，
     并保留"至少一名管理员""不能删所有者"等既有约束；
  4. **不落地明文**：口令只在内存里传给对端子进程，03 不写库、不落日志。

实现方式：
  沿用本项目既有的"子进程 + 单行 JSON"范式（见 00_集成联动/linkage.py 的说明），
  因为这些模块与本模块的 config/models 同名，同进程 import 必然互相污染。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import config

ALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 目标模块 -> (相对目录候选, 显示名, 角色映射)
TARGETS = {
    "01": {
        "dirs": ("01_人脸识别与身份核对/01_人脸识别与身份核对",
                 "01_人脸识别与身份核对"),
        "name": "01 人脸识别与身份核对",
        # 03 的角色 -> 01 的角色（03 的 owner 即系统最高权限，映射为 01 的 owner）
        "role_map": {config.ROLE_OWNER: "owner", config.ROLE_ADMIN: "admin",
                     config.ROLE_PATROL: "teacher", config.ROLE_INVIGILATOR: "teacher",
                     config.ROLE_COMMANDER: "admin", config.ROLE_CHECKER: "teacher",
                     config.ROLE_VIEWER: "teacher"},
        "roles": {"owner": "所有者", "admin": "管理员", "teacher": "教师"},
    },
    "02": {
        "dirs": ("02_摄像头核查与视频质量诊断",),
        "name": "02 摄像头核查与视频质量诊断",
        "role_map": {config.ROLE_OWNER: "owner", config.ROLE_ADMIN: "admin",
                     config.ROLE_PATROL: "admin", config.ROLE_INVIGILATOR: "admin",
                     config.ROLE_COMMANDER: "admin", config.ROLE_CHECKER: "admin",
                     config.ROLE_VIEWER: "admin"},
        "roles": {"owner": "所有者", "admin": "管理员"},
    },
}


def dir_of(key):
    """找到目标模块目录（含 integration_api.py 的那个）"""
    for rel in TARGETS.get(key, {}).get("dirs", ()):
        d = os.path.join(ALL_DIR, *rel.split("/"))
        if os.path.isfile(os.path.join(d, "integration_api.py")):
            return d
    return ""


def map_role(key, role_03):
    """把 03 的角色映射成目标模块的角色"""
    m = TARGETS.get(key, {}).get("role_map", {})
    mapped = m.get(role_03)
    if mapped:
        return mapped
    # 兜底：未知角色一律降为最低档，绝不因映射缺失而给高权限
    return "teacher" if key == "01" else "admin"


class AccountBridge(object):
    """跨机账号操作。所有方法返回对端原样 JSON（含 ok/reason）。"""

    def __init__(self, timeout=30):
        self.timeout = float(timeout)

    # ---------------- 底层 ----------------
    def call(self, key, op, args=None):
        d = dir_of(key)
        if not d:
            return {"ok": False, "reason": "目标模块不可用（未找到 %s 的 integration_api.py）"
                                           % TARGETS.get(key, {}).get("name", key)}
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
            return {"ok": False, "reason": "调用超时（%ds）" % int(self.timeout)}
        except Exception as e:
            return {"ok": False, "reason": "调用失败：%s" % e}
        for line in reversed((p.stdout or b"").decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "reason": (err[-1][:160] if err else
                                       "无 JSON 输出（退出码 %s）" % p.returncode)}

    # ---------------- 只读 ----------------
    def list_accounts(self, key):
        r = self.call(key, "account_list")
        if r.get("ok"):
            r["target_name"] = TARGETS.get(key, {}).get("name", key)
        return r

    # ---------------- 写（都由 03 侧调用者负责权限与留痕） ----------------
    def create(self, key, username, password, role_03, display_name="", operator=""):
        return self.call(key, "account_create", {
            "username": username, "password": password,
            "role": map_role(key, role_03), "display_name": display_name,
            "by_role": map_role(key, operator or config.ROLE_OWNER)})

    def reset_password(self, key, username, new_password, operator=""):
        return self.call(key, "account_reset", {
            "username": username, "password": new_password,
            "by_role": map_role(key, operator or config.ROLE_OWNER)})

    def delete(self, key, username, operator=""):
        return self.call(key, "account_delete", {
            "username": username,
            "by_role": map_role(key, operator or config.ROLE_OWNER)})

    # ---------------- 只读汇总（给驾驶舱/界面用） ----------------
    def all_summary(self):
        """{key: {"name":..., "ok":..., "accounts":[...], "reason":...}}"""
        out = {}
        for key in TARGETS:
            r = self.list_accounts(key)
            out[key] = {"name": TARGETS[key]["name"], "ok": bool(r.get("ok")),
                        "accounts": r.get("accounts") or [],
                        "reason": r.get("reason") or "",
                        "roles": r.get("roles") or TARGETS[key]["roles"]}
        return out


#: 03 侧权限要求：账号管理仅所有者（与 terminal_gui 的"终端账户"一致）
REQUIRED_ACTION = "manage"
