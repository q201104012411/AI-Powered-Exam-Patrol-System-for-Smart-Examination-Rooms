# -*- coding: utf-8 -*-
"""
审计日志：哈希链防篡改 + 大小轮转 + 到期清理 + 链路校验。

旧版问题：纯文本 append，任何人改一行都不会被发现，"可审计"不成立。
本版：每条记录写入前计算 hash = sha256(prev_hash + 记录内容)，形成链式结构；
     verify_chain() 可检出任何删改（含删除中间行）。

关于"到期清理"与哈希链的矛盾：删除历史记录本身就会打断链。本版的处理是
——清理时先把原文件整体备份为 .purged.bak，然后**重建**留下的记录链，
并在新链头部写入一条 CHAIN_RESET 记录说明清理动作。这样既不违背保留期限要求，
又能保证"清理之后"的日志依然可校验。
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta

import config
from models import VerifyRecord
from pii import sanitize_for_log


def canonical(core: dict) -> str:
    return json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_hash(prev_hash: str, core: dict) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(canonical(core).encode("utf-8"))
    return h.hexdigest()


class AuditLogger:
    def __init__(self, log_path: str = None, enforce_chain: bool = None):
        self.log_path = str(log_path or config.AUDIT_LOG_PATH)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        self.enforce_chain = config.AUDIT_HASH_CHAIN if enforce_chain is None else enforce_chain
        self._tail = None                      # 链尾缓存，避免每次写都全量扫描
        if self.enforce_chain:
            self._archive_legacy_log()

    def _archive_legacy_log(self):
        """
        旧版日志没有哈希字段，无法事后补链。首次使用新版本时把旧日志整体归档为
        .v1bak，并开启新链（写一条 CHAIN_RESET 说明），保证"链路可校验"这件事成立。
        """
        if not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0:
            return
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    has_hash = '"hash"' in line
                    if has_hash:
                        return              # 已是新格式
                    break
            bak = self.log_path + ".v1bak"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(self.log_path, bak)
            reset = VerifyRecord(timestamp=datetime.now().isoformat(timespec="seconds"),
                                 device_id="system", candidate_id=None, mode="MAINT",
                                 result="PASS", reason_code="CHAIN_RESET",
                                 reason="检测到旧版无哈希链日志，已归档为 %s 并启用哈希链审计"
                                        % os.path.basename(bak))
            self._write_record(reset, "")
        except OSError:
            pass

    # ---------------- 内部 ----------------
    def _rotate_if_needed(self):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > config.LOG_MAX_BYTES:
                bak = self.log_path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(self.log_path, bak)
                self._tail = None
        except OSError:
            pass

    def last_hash(self) -> str:
        if self._tail is not None:
            return self._tail
        if not os.path.exists(self.log_path):
            return ""
        last = ""
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line).get("hash", "") or last
                except json.JSONDecodeError:
                    continue
        return last

    def _write_record(self, record: VerifyRecord, prev: str):
        data = record.to_dict()
        if self.enforce_chain:
            record.prev_hash = prev
            record.hash = compute_hash(prev, record.core_for_hash())
            data = record.to_dict()
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._tail = record.hash or ""
        return data

    # ---------------- 写 ----------------
    def log(self, record: VerifyRecord):
        self._rotate_if_needed()
        record.reason = sanitize_for_log(record.reason)
        record.operator = sanitize_for_log(record.operator)
        record.witness = sanitize_for_log(record.witness)
        return self._write_record(record, self.last_hash())

    def clear_all(self, operator: str, witness: str = "", keep_backup: bool = True) -> dict:
        """
        清空审计日志。**仅所有者可调用**（权限判定在界面/accounts.can_clear_logs）。
        做法：先整体备份 -> 清空 -> 写一条 CHAIN_RESET 记录说明"谁清的、清了多少条"。
        这样"清空日志"这个动作本身仍然可审计、可追溯。
        """
        import shutil
        removed = len(self.query())
        backup = None
        if keep_backup and os.path.exists(self.log_path):
            backup = "%s.cleared_%s.bak" % (self.log_path,
                                            time.strftime("%Y%m%d_%H%M%S"))
            try:
                shutil.copy2(self.log_path, backup)
            except OSError:
                backup = None
        # 清空
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("")
        self._last = None
        rec = VerifyRecord(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"), device_id="AUDIT",
            candidate_id=None, mode="CHAIN_RESET", result="PASS",
            reason_code="CHAIN_RESET",
            reason="日志由所有者清空：共清除 %d 条，备份=%s" % (removed, backup or "无"),
            operator=operator, witness=witness, manual_required=False,
        )
        self._write_record(rec, self.last_hash())
        return {"removed": removed, "backup": backup}

    # ---------------- 读 ----------------
    def query(self, candidate_id: str = None, limit: int = None):
        if not os.path.exists(self.log_path):
            return []
        records = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if candidate_id is None or rec.get("candidate_id") == candidate_id:
                    records.append(rec)
        return records[-limit:] if limit else records

    def verify_chain(self):
        """校验哈希链完整性 -> {"ok","total","broken_at","reason"}"""
        if not os.path.exists(self.log_path):
            return {"ok": True, "total": 0, "broken_at": None, "reason": "日志为空"}
        prev = ""
        total = 0
        with open(self.log_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "total": total, "broken_at": lineno, "reason": "JSON 解析失败"}
                if not self.enforce_chain:
                    total += 1
                    continue
                if rec.get("prev_hash", "") != prev:
                    return {"ok": False, "total": total, "broken_at": lineno,
                            "reason": "前序哈希不匹配（可能存在删除/重排）"}
                core = dict(rec)
                stored = core.pop("hash", "")
                if compute_hash(prev, core) != stored:
                    return {"ok": False, "total": total, "broken_at": lineno,
                            "reason": "记录内容与哈希不一致（被篡改）"}
                prev = stored
                total += 1
        return {"ok": True, "total": total, "broken_at": None, "reason": "链路完整"}

    # ---------------- 到期清理 ----------------
    def purge_expired(self, days: int = None, dry_run: bool = False):
        """删除超过保留期限的核验记录；原文件备份，留存的记录重建哈希链。"""
        days = config.RETENTION_DAYS if days is None else int(days)
        if not os.path.exists(self.log_path):
            return {"scanned": 0, "removed": 0, "kept": 0, "backup": None}
        cutoff = datetime.now() - timedelta(days=days)
        kept, removed, scanned, corrupted = [], 0, 0, 0
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                scanned += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    corrupted += 1
                    continue
                try:
                    ts = datetime.fromisoformat(rec.get("timestamp", ""))
                except ValueError:
                    kept.append(rec)          # 时间戳异常时保守保留，不误删
                    continue
                if ts < cutoff:
                    removed += 1
                else:
                    kept.append(rec)

        if not removed or dry_run:
            return {"scanned": scanned, "removed": removed, "kept": len(kept),
                    "corrupted": corrupted, "backup": None}

        bak = self.log_path + ".purged.bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(self.log_path, bak)

        reset = VerifyRecord(timestamp=datetime.now().isoformat(timespec="seconds"),
                             device_id="system", candidate_id=None, mode="MAINT",
                             result="PASS", reason_code="CHAIN_RESET",
                             reason="按保留期限 %d 天清理 %d 条记录；原日志备份为 %s"
                                    % (days, removed, os.path.basename(bak)))
        prev = self._write_record(reset, "")["hash"]
        for rec in kept:
            rec.pop("hash", None)
            rec.pop("prev_hash", None)
            r = VerifyRecord(**rec)
            prev = self._write_record(r, prev)["hash"]

        return {"scanned": scanned, "removed": removed, "kept": len(kept),
                "corrupted": corrupted, "backup": bak}
