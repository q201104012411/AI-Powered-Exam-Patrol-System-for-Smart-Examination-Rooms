# -*- coding: utf-8 -*-
r"""
密钥环体检 / 备份 / 重置。

什么时候需要重置：
  · 把整个项目目录从**别的电脑或别的 Windows 账户**拷过来，DPAPI 解不开
    （错误码 0x8009000B，见 dpp/secure_store.py 的说明）；
  · 密钥文件被误删/损坏，或要重新开始一轮演示。

重置前一定先**整目录备份**（默认备份成 data 的同级目录 data_backup_<时间戳>），
再删掉密钥与**用旧密钥加密过、已经解不开**的数据：
  · data\keyring\*.enc / sign.pub      —— 旧密钥
  · data\dpp.db(-wal/-shm)             —— 库里敏感列是旧密钥密文
  · data\vault\*.enc                   —— 人脸原图密文
  · data\exports\*、data\reports\*     —— 旧密钥/旧签名生成的产物
保留 data\logs\（明文运行日志，属于历史记录），备份里也全都有。

重置后可选重新播种演示数据（cli.py keyring --reset --with-demo 即"清空重来"）。
"""
import os
import shutil
import time
from pathlib import Path

from dpp import access
from dpp import config
from dpp import crypto
from dpp import integrity
from dpp import secure_store
from dpp import storage

KEY_FILES = ("master.key.enc", "sign.key.enc", "sign.pub", "wrap.key")
DB_FILES = ("dpp.db", "dpp.db-wal", "dpp.db-shm", "dpp.db-journal")


def status():
    """密钥环体检结论（含每个文件的包裹者与能否解开）。"""
    info = secure_store.probe_keyring()
    out = dict(info)
    try:
        out["主密钥指纹"] = crypto.key_fingerprint()
    except Exception as exc:                                 # noqa: BLE001
        out["主密钥指纹"] = "取不到（%s）" % type(exc).__name__
    try:
        out["签名密钥指纹"] = integrity.key_fingerprint()
    except Exception as exc:                                 # noqa: BLE001
        out["签名密钥指纹"] = "取不到（%s）" % type(exc).__name__
    return out


def backup_data_root(root=None):
    """把数据目录整份复制到 data_backup_<时间戳>\\，返回备份路径。"""
    root = Path(root or config.DATA_ROOT)
    if not root.exists():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = root.parent / ("%s_backup_%s" % (root.name, stamp))
    n = 1
    while dest.exists():
        dest = root.parent / ("%s_backup_%s_%d" % (root.name, stamp, n))
        n += 1
    try:
        shutil.copytree(str(root), str(dest))
    except OSError as exc:
        raise RuntimeError(
            "备份失败：%s\n（请先关闭桌面界面再重置：双击「停止服务.bat」）" % exc)
    return dest


def _rm_file(p: Path):
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("无法删除 %s：%s\n（请先关闭桌面界面再重置）" % (p, exc))


def _clear_dir(d: Path):
    n = 0
    if not d.exists():
        return n
    for p in sorted(d.iterdir()):
        if p.is_file():
            _rm_file(p)
            n += 1
        elif p.is_dir():
            shutil.rmtree(str(p), ignore_errors=True)
            n += 1
    return n


def reset(backup=True, seed_demo=False, seed_accounts=True, log=None):
    """备份 + 重置密钥环 + 清掉旧密钥加密的数据，返回结论字典。"""
    said = log or (lambda _m: None)
    root = Path(config.DATA_ROOT)
    result = {"数据目录": str(root), "备份": "", "已删除": {},
              "主密钥指纹": "", "签名密钥指纹": ""}

    if backup:
        dest = backup_data_root(root)
        result["备份"] = str(dest) if dest else "（数据目录不存在，无需备份）"
        said("已备份数据目录 -> %s" % result["备份"])

    removed = {"密钥": 0, "数据库": 0, "保险箱": 0, "导出": 0, "报告": 0}
    for name in KEY_FILES:
        removed["密钥"] += 1 if _rm_file(config.KEYRING_DIR / name) else 0
    for name in DB_FILES:
        removed["数据库"] += 1 if _rm_file(root / name) else 0
    removed["保险箱"] = _clear_dir(config.VAULT_DIR)
    removed["导出"] = _clear_dir(config.EXPORT_DIR)
    removed["报告"] = _clear_dir(config.REPORT_DIR)
    result["已删除"] = removed
    said("已清理：密钥 %d、数据库文件 %d、保险箱 %d、导出 %d、报告 %d"
         % (removed["密钥"], removed["数据库"], removed["保险箱"],
            removed["导出"], removed["报告"]))

    # 清进程内缓存的旧密钥，否则新密钥写下去了、内存里还是旧的
    crypto.reset_cache()

    master = crypto.master_key()                     # 新主密钥（DPAPI 包裹）
    integrity.sign_key()                             # 新签名密钥对
    result["主密钥指纹"] = crypto.key_fingerprint()
    result["签名密钥指纹"] = integrity.key_fingerprint()
    said("已生成新密钥：主密钥 %s / 签名密钥 %s"
         % (result["主密钥指纹"], result["签名密钥指纹"]))

    store = storage.Store()
    if seed_accounts:
        access.AccountStore(store).ensure_seed()
        said("已重建预置账户（owner/dpo1/auditor1/oper1/inv01）")

    if seed_demo:
        import demo_data                                  # 项目根下的演示数据生成器
        from dpp import governance
        from dpp import lifecycle as lc_mod
        from dpp import rights as rights_mod
        acc = access.AccountStore(store)
        life = lc_mod.Lifecycle(store, accounts=acc)
        gov = governance.Governance(store)
        svc = rights_mod.RightsService(store, lifecycle=life, accounts=acc)
        result["演示数据"] = demo_data.generate(store, acc, life, gov, svc)
        said("已重新播种演示数据")
    result["可用"] = status()["usable"]
    return result
