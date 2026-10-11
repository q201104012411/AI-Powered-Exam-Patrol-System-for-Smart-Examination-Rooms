# -*- coding: utf-8 -*-
"""
界面操作日志：大小轮转 + 脱敏。

旧版问题：无限增长、且原文写入（一旦有人把身份证号敲进备注就会落明文）。
本版：写入前统一脱敏（15/18 位数字与手机号），超过阈值自动轮转。
"""
import os
from datetime import datetime

import config
from pii import sanitize_for_log


class ActionLogger:
    def __init__(self, log_path: str = None):
        self.log_path = str(log_path or config.ACTION_LOG_PATH)
        self.operator = ""              # 当前登录用户；登录后由界面设置，写入每条记录
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def set_operator(self, name):
        self.operator = (name or "").strip()

    def clear(self, operator="", keep_backup=True):
        """
        清空操作日志。**仅所有者可调用**（权限判定在界面/accounts.can_clear_logs）。
        清空前先备份，并在新日志首行记录"谁在什么时候清了日志"。
        返回 {"backup": str|None, "removed": int}
        """
        if not os.path.exists(self.log_path):
            return {"backup": None, "removed": 0}
        with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln for ln in f if ln.strip()]
        backup = None
        if keep_backup:
            backup = "%s.cleared_%s.bak" % (self.log_path, datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                os.replace(self.log_path, backup)
            except OSError:
                backup = None
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("[%s] 日志已清空 | 操作人=%s 清除 %d 条 | 备份=%s\n"
                    % (datetime.now().isoformat(timespec="seconds"),
                       operator or self.operator or "unknown", len(lines), backup or "无"))
        return {"backup": backup, "removed": len(lines)}

    def _rotate_if_needed(self):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > config.LOG_MAX_BYTES:
                bak = self.log_path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(self.log_path, bak)
        except OSError:
            pass

    def log(self, action: str, detail: str = ""):
        self._rotate_if_needed()
        ts = datetime.now().isoformat(timespec="seconds")
        who = ("[%s] " % self.operator) if self.operator else ""
        line = "[%s] %s%s" % (ts, who, sanitize_for_log(action))
        if detail:
            line += " | " + sanitize_for_log(detail)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return line

    def query(self, keyword: str = None, limit: int = 200):
        if not os.path.exists(self.log_path):
            return []
        records = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                if keyword is None or keyword in line:
                    records.append(line.rstrip("\n"))
        return records[-limit:]
