# -*- coding: utf-8 -*-
"""
统一管控与稳定性（《智能终端.md》第二节第 5 条、第三节）

统一管控在终端侧的形态：
  · 配置快照：平台下发 -> 本地生效（键值存 SQLite）
  · 应用白名单：不在白名单内的应用视为违规（仅记录与告警，不做进程杀灭）
  · 外设管控：摄像头/麦克风/USB 存储/蓝牙/WiFi-Direct 开关（记录策略状态）
  · 远程擦除：平台下发 wipe 指令 -> 清空本地数据目录（留审计，执行前需确认）
  · 远程升级：登记升级任务（本模块只做任务登记与校验，不含实际刷机）

稳定性：
  · soak_tick() 按心跳写 data/logs/soak.log
  · soak_status() 从日志算连续运行时长，用于"连续运行 ≥72 小时"的验收取证
"""
import json
import os
import shutil
import time

import config
import models

CONFIG_KEY = "fleet.config"
WIPE_KEY = "fleet.wipe"


class FleetError(Exception):
    pass


class FleetManager(object):
    def __init__(self, store):
        self.store = store

    # ---------------- 配置快照 ----------------
    def default_config(self):
        return {
            "app_whitelist": list(config.MDM_APP_WHITELIST_DEFAULT),
            "peripherals": dict(config.MDM_PERIPHERAL_DEFAULT),
            "version": "0.1.0",
            "upgrade_channel": "stable",
            "terminal_locked": False,
        }

    def config_snapshot(self):
        cfg = self.store.get_kv(CONFIG_KEY)
        if cfg is None:
            cfg = self.default_config()
            self.store.set_kv(CONFIG_KEY, cfg)
        return cfg

    def set_config(self, patch, operator="platform"):
        cfg = self.config_snapshot()
        patch = dict(patch or {})
        # 外设是子项：平台只下发其中一个时不能把其余清空，这里做浅合并
        if isinstance(patch.get("peripherals"), dict):
            per = dict(cfg.get("peripherals") or {})
            per.update(patch["peripherals"])
            patch["peripherals"] = per
        cfg.update(patch)
        self.store.set_kv(CONFIG_KEY, cfg)
        self.store.log("管控配置更新", models.canonical(patch)[:200], operator)
        return cfg

    # ---------------- 应用白名单 ----------------
    def check_app(self, app_name):
        wl = self.config_snapshot().get("app_whitelist", [])
        ok = app_name in wl
        if not ok:
            self.store.log("违规应用检测", "不在白名单：%s" % app_name, "terminal")
        return ok, ("允许" if ok else "不在白名单（仅记录，未强杀进程）")

    # ---------------- 外设管控 ----------------
    def set_peripheral(self, name, enabled, operator="platform"):
        cfg = self.config_snapshot()
        per = cfg.setdefault("peripherals", {})
        if name not in per:
            return False, "未知外设：%s" % name
        per[name] = bool(enabled)
        self.store.set_kv(CONFIG_KEY, cfg)
        self.store.log("外设策略变更", "%s=%s" % (name, enabled), operator)
        return True, "%s 已%s" % (name, "开启" if enabled else "关闭")

    def peripheral_status(self):
        return self.config_snapshot().get("peripherals", {})

    # ---------------- 远程擦除 ----------------
    def request_wipe(self, operator, reason):
        """登记擦除请求（不立即执行）"""
        self.store.set_kv(WIPE_KEY, {"requested_at": models.now_ts(),
                                     "operator": operator, "reason": reason,
                                     "done": False})
        self.store.log("远程擦除请求", reason[:120], operator)
        return True, "擦除请求已登记，需二次确认后执行"

    def wipe_pending(self):
        w = self.store.get_kv(WIPE_KEY)
        return bool(w and not w.get("done"))

    def execute_wipe(self, confirm_code, operator):
        """
        执行远程擦除：清空运行数据（告警/复核留痕/补传队列/远端记录）。

        **重要修正（2026-10-15）**：旧实现是"删 data 下的 .db 文件 + rmtree 附件与队列目录"，
        但终端**正在运行**，SQLite 连接持有 terminal.db 的句柄 ⇒ 必然报
        `WinError 32 另一个程序正在使用此文件`（用户实际遇到的就是这个），
        而且 attachments/outbox 可能已被删、数据库没删掉 —— 半途状态比不擦更糟。

        现在改为走 storage.purge_data 的**库内清理**：
          · 不删数据库文件，因此运行中也能真正清干净；
          · 不销毁终端账户（终端账户另有独立入口管理）；
          · 审计链与本次擦除留痕保留（谁在什么时候擦了什么是可追溯的）。

        必须传入 confirm_code == 'WIPE' 才会执行 —— 避免误触。
        """
        if confirm_code != "WIPE":
            return False, "确认码不正确（需输入 WIPE）"
        if not self.wipe_pending():
            return False, "没有待执行的擦除请求"
        try:
            ok, msg, detail = self.store.purge_data("all", mode="hard",
                                                    operator=operator,
                                                    reason="远程擦除（fleet.execute_wipe）")
        except Exception as e:                        # noqa: BLE001
            return False, "擦除失败：%s" % e
        if not ok:
            return False, "擦除未完成：%s" % msg
        try:
            self.store.vacuum()
        except Exception:
            pass
        self.store.set_kv(WIPE_KEY, {"requested_at": "", "operator": operator,
                                     "reason": "已执行", "done": True})
        # purge_data 已经写过审计链，这里不再重复写；把 detail 一起回给调用方便于排查
        return True, "已清空运行数据（库内清理，未删除数据库文件）：%s" % (
            msg or detail or "")

    # ---------------- 远程升级（任务登记） ----------------
    def push_upgrade(self, version, url, checksum="", operator="platform"):
        if not version or not url:
            return False, "version 与 url 必填"
        cmd = self.store.push_command("upgrade", {"version": version, "url": url,
                                                  "checksum": checksum},
                                      operator=operator)
        self.store.log("远程升级任务", "%s -> %s" % (version, url), operator)
        return True, cmd

    # ---------------- 指令应用 ----------------
    def apply_pending(self):
        """把平台下发的指令逐条生效；返回处理结果列表"""
        results = []
        for c in self.store.pending_commands():
            kind = c["kind"]
            try:
                payload = json.loads(c["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            if kind == "set_config":
                self.set_config(payload, operator=c.get("operator") or "platform")
                results.append((c["cmd_id"], kind, "已生效"))
            elif kind == "peripheral":
                ok, msg = self.set_peripheral(payload.get("name"), payload.get("enabled"),
                                              operator=c.get("operator") or "platform")
                results.append((c["cmd_id"], kind, msg))
            elif kind == "wipe":
                self.request_wipe(c.get("operator") or "platform",
                                  payload.get("reason", "平台下发远程擦除"))
                results.append((c["cmd_id"], kind, "已登记，待二次确认"))
            elif kind == "upgrade":
                results.append((c["cmd_id"], kind,
                                "已登记升级任务（仅登记与校验，不执行刷机）"))
            elif kind == "report":
                results.append((c["cmd_id"], kind, "已上报（%s）" % payload.get("alert_id")))
            else:
                results.append((c["cmd_id"], kind, "未知指令，已忽略"))
            self.store.mark_command_applied(c["cmd_id"])
        return results

    # ---------------- 稳定性（72 小时） ----------------
    def soak_tick(self, note=""):
        """写一条心跳；返回当前已连续运行的秒数"""
        os.makedirs(str(config.LOG_DIR), exist_ok=True)
        line = models.canonical({"ts": models.now_ts(), "epoch": models.now_epoch(),
                                 "pid": os.getpid(), "note": note})
        try:
            with open(str(config.SOAK_LOG), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        return self.soak_status()["longest_sec"]

    def soak_status(self):
        """
        从 soak.log 计算连续运行时长。
        判定"连续"：相邻心跳间隔 <= 3 倍心跳周期视为未中断。
        """
        path = str(config.SOAK_LOG)
        if not os.path.exists(path):
            return {"ticks": 0, "longest_sec": 0, "longest_hours": 0.0,
                    "latest": "", "target_hours": config.SOAK_TARGET_HOURS,
                    "passed": False, "note": "尚无心跳记录（需实际运行产生）"}
        epochs, latest = [], ""
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except (ValueError, TypeError):
                    continue
                epochs.append(float(o.get("epoch", 0)))
                latest = o.get("ts", latest)
        if not epochs:
            return {"ticks": 0, "longest_sec": 0, "longest_hours": 0.0, "latest": "",
                    "target_hours": config.SOAK_TARGET_HOURS, "passed": False,
                    "note": "心跳记录为空"}
        epochs.sort()
        gap = config.SOAK_HEARTBEAT_SEC * 3
        best = cur = 0.0
        prev = None
        for e in epochs:
            if prev is None:
                cur = 0.0
            elif e - prev <= gap:
                cur += e - prev
            else:
                cur = 0.0
            best = max(best, cur)
            prev = e
        hours = round(best / 3600.0, 3)
        return {"ticks": len(epochs), "longest_sec": round(best, 1),
                "longest_hours": hours, "latest": latest,
                "target_hours": config.SOAK_TARGET_HOURS,
                "passed": hours >= config.SOAK_TARGET_HOURS,
                "note": "达标" if hours >= config.SOAK_TARGET_HOURS else "未达标（继续运行积累）"}

    def status(self):
        cfg = self.config_snapshot()
        return {"版本": cfg.get("version"), "白名单": cfg.get("app_whitelist"),
                "外设": cfg.get("peripherals"),
                "待执行指令": len(self.store.pending_commands()),
                "擦除待确认": self.wipe_pending(),
                "稳定性": self.soak_status()}


def sleep(sec):
    time.sleep(sec)
