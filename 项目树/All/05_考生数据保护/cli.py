#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""考生数据保护 · 命令行入口

用法示例：
    python cli.py report                     生成完整报告
    python cli.py summary                    打印摘要
    python cli.py verify                     校验审计哈希链
    python cli.py demo                       生成演示数据（写入真实运行目录）
    python cli.py destroy --now              执行到期自动销毁
    python cli.py rights --list              列出权利申请
    python cli.py rights --handle RQ-xxx --approve --result "已删除"
    python cli.py kpi                        打印六大验收指标
    python cli.py catalog                    打印分类分级表
    python cli.py permission                 打印权限矩阵
    python cli.py algorithm                  打印当前加密算法与密钥指纹
    python cli.py keyring                    密钥环体检（换机器后密钥解不开时先看这个）
    python cli.py keyring --reset --yes      备份后重置密钥环（清掉旧密钥加密的数据）

设计说明：
  - 本入口默认使用 **真实运行目录**（%EXAM_DPP_HOME% 或 dpp_home.txt 指向的目录，
    否则为脚本同级 data/）。自检 main.py 会切换为临时目录。
  - 所有写操作都会留下审计记录；命令行同样受权限最小化约束。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dpp import (  # noqa: E402
    access,
    catalog,
    config,
    crypto,
    governance,
    lifecycle as lc_mod,
    report as report_mod,
    rights as rights_mod,
    storage as storage_mod,
)


def _parse_rooms(text):
    if not text:
        return []
    return [r.strip() for r in text.replace("，", ",").split(",") if r.strip()]


def _open_store(args):
    # Store.__init__ 内部会建表（WAL 模式）
    return storage_mod.Store(args.db or None)


def _reporter(store):
    """构造完整接线的 Reporter（lifecycle/gov/rights 全部注入，
    否则验收指标会因未接入生命周期管理器而显示「样本不足」）。"""
    life = lc_mod.Lifecycle(store)
    gov = governance.Governance(store)
    svc = rights_mod.RightsService(store, lifecycle=life)
    return report_mod.Reporter(store, lifecycle=life, gov=gov, rights=svc)


def _default_password(username):
    for row in config.DEFAULT_ACCOUNTS:
        if row[0] == username:
            return row[1]
    return ""


def _login(store, args, action):
    """命令行登录：优先 --user/--pwd；否则用指定（默认 owner）账户的内置口令。"""
    acc = access.AccountStore(store)
    acc.ensure_seed()
    username = args.user or "owner"
    password = args.pwd
    if password is None:
        password = _default_password(username)
    session = acc.authenticate(
        username, password, place=args.place or "", device=args.device or "",
        hour=args.hour,
    )
    # 高风险会话（未登记设备/地点/异常时段）执行敏感动作需二次确认；
    # 命令行下由 --confirm-risky 显式确认，避免"脚本不知不觉越权跑过去"。
    if getattr(args, "confirm_risky", False):
        session.confirm_risky()
    session.require(action)
    return session


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------
def cmd_summary(args):
    store = _open_store(args)
    rep = _reporter(store)
    print(rep.console_summary())
    return 0


def cmd_report(args):
    store = _open_store(args)
    rep = _reporter(store)
    print(rep.console_summary())
    print()
    text = rep.full_report()
    out = rep.export_all()
    print("[导出]")
    for k, v in out.items():
        print("  %-16s %s" % (k, v))
    if args.stdout:
        print()
        print(text)
    return 0


def cmd_verify(args):
    store = _open_store(args)
    ok, detail, n = store.verify_chain()
    print(("PASS " if ok else "FAIL ") + detail)
    return 0 if ok else 1


def cmd_demo(args):
    import demo_data

    store = _open_store(args)
    acc = access.AccountStore(store)
    acc.ensure_seed()
    life = lc_mod.Lifecycle(store)
    gov = governance.Governance(store)
    svc = rights_mod.RightsService(store)
    info = demo_data.generate(store, acc, life, gov, svc)
    print("[演示数据已写入]")
    for k, v in info.items():
        print("  %-16s %s" % (k, v))
    print()
    print(_reporter(store).console_summary())
    return 0


def cmd_catalog(args):
    print(catalog.table_markdown())
    ok, problems = catalog.validate()
    print()
    print(("PASS " if ok else "FAIL ") + "分类分级目录自洽性校验")
    for p in problems:
        print("  - " + p)
    return 0 if ok else 1


def cmd_permission(args):
    store = _open_store(args)
    life = lc_mod.Lifecycle(store)
    rows = life.permission_matrix()
    actions = sorted(config.ALL_ACTIONS, key=lambda a: config.ACTION_TEXT.get(a, a))
    print("权限矩阵（角色 × 动作，Y=允许 / -=拒绝；默认拒绝）")
    print()
    width = max(len(config.ACTION_TEXT.get(a, a)) for a in actions) + 2
    header = "角色".ljust(14) + "".join(
        config.ACTION_TEXT.get(a, a).ljust(width) for a in actions)
    print(header)
    print("-" * len(header))
    for row in rows:
        line = ("%s(%s)" % (row["role_text"], row["role"])).ljust(14)
        for a in actions:
            line += ("Y" if a in row["actions"] else "-").ljust(width)
        print(line)
    return 0


def cmd_destroy(args):
    store = _open_store(args)
    life = lc_mod.Lifecycle(store)
    if args.by_request:
        session = _login(store, args, "destroy")
        res = life.destroy_by_request(
            session, args.by_request, reason=args.reason or "考生删除权请求",
            witness=args.witness or "", force=args.force,
        )
    else:
        res = life.run_auto_destroy(operator=args.user or "system")
    print("[销毁结果]")
    for k, v in res.items():
        if isinstance(v, dict):
            print("  %-16s" % k)
            for kk, vv in v.items():
                print("      %-14s %s" % (kk, vv))
        elif isinstance(v, list):
            print("  %-16s %s" % (k, "、".join(str(x) for x in v) or "（无）"))
        else:
            print("  %-16s %s" % (k, v))
    return 0


def cmd_use(args):
    store = _open_store(args)
    life = lc_mod.Lifecycle(store)
    session = _login(store, args, "use")
    try:
        res = life.use(
            session, args.cand, purpose=args.purpose, action=args.action,
            terminal=args.terminal or args.device or "巡考终端",
        )
    except lc_mod.LifecycleError as exc:
        # 目的限定/权限拒绝：uses 表与审计链已留痕
        print("拒绝：%s" % exc)
        return 1
    print("[使用结果]")
    for k, v in res.items():
        print("  %-16s %s" % (k, v))
    return 0 if res.get("allowed") else 1


_RIGHT_ALIAS = {
    "知情": "know", "know": "know",
    "查阅": "access", "access": "access",
    "复制": "copy", "copy": "copy",
    "更正": "correct", "correct": "correct",
    "删除": "delete", "delete": "delete",
    "撤回同意": "withdraw", "撤回": "withdraw", "withdraw": "withdraw",
}


def _norm_right(text):
    return _RIGHT_ALIAS.get((text or "").strip(), (text or "").strip())


def cmd_rights(args):
    store = _open_store(args)
    svc = rights_mod.RightsService(store)
    if args.list:
        rows = store.list_rights()
        if not rows:
            print("（暂无权利申请）")
            return 0
        print("权利申请台账（共 %d 条）" % len(rows))
        print("  %-14s %-8s %-8s %-8s %-8s %s" % (
            "申请编号", "权利", "状态", "渠道", "身份", "考生编号"))
        for r in rows:
            right_text = rights_mod.RIGHTS_TEXT.get(r.right, r.right)
            verified = "待核验" if "【待核验身份】" in (r.detail or "") else "已核验"
            ch = getattr(r, "channel", "") or ""
            print("  %-14s %-8s %-8s %-8s %-8s %s" % (
                r.req_id, right_text, r.status, ch, verified, r.cand_ref))
        return 0
    if args.handle:
        session = _login(store, args, "rights_decide")
        res = svc.handle(
            session, args.handle, result=args.result or "",
            reason=args.reason or "", approve=args.approve,
        )
        print("[处理结果]")
        for k, v in res.items():
            if k == "data" and isinstance(v, dict):
                print("  %-16s" % "提供的脱敏数据")
                for kk, vv in v.items():
                    print("      %-16s %s" % (kk, vv))
            else:
                print("  %-16s %s" % (k, v))
        return 0 if res.get("status") != "rejected" else 1
    if args.submit_cand:
        session = _login(store, args, "rights")
        res = svc.submit(
            session, args.submit_cand, right=_norm_right(args.right),
            detail=args.detail or "", identity_verified=args.verified,
            alt_channel_used=args.alt_channel,
        )
        print("[受理结果]")
        for k, v in res.items():
            print("  %-16s %s" % (k, v))
        if not args.verified:
            print("  提示：身份未核验，仅登记不受理；核验后请用 --handle 处理。")
        return 0
    print("请指定 --list / --handle / --submit-cand 之一")
    return 2


def cmd_kpi(args):
    store = _open_store(args)
    life = lc_mod.Lifecycle(store)
    svc = rights_mod.RightsService(store)
    gov = governance.Governance(store)
    acc = gov.acceptance(lifecycle=life, rights=svc)
    print("验收指标（第六节）")
    print("=" * 70)
    for it in acc["items"]:
        flag = "[OK]" if it["达标"] else "[!!]"
        print("%s %-14s 要求 %-10s 实测 %s" % (
            flag, it["指标项"], it["要求"], it["实测"]))
        print("     证据：%s" % it["证据"])
    print("-" * 70)
    print("达标 %d/%d 项%s" % (
        acc["passed"], acc["total"],
        "（全部达标）" if acc["all_passed"] else "（存在未达标项）"))
    return 0 if acc["all_passed"] else 1


def cmd_incident(args):
    store = _open_store(args)
    gov = governance.Governance(store)
    session = _login(store, args, "approve")
    res = gov.report_incident(
        session, level=args.level, title=args.title,
        detail=args.detail or "", handler=args.handler or "",
    )
    print("[上报结果]")
    for k, v in res.items():
        print("  %-16s %s" % (k, v))
    return 0


def cmd_algorithm(args):
    from dpp import secure_store as ss

    crypto.master_key()   # 确保主密钥已生成
    print("加密算法      : %s" % crypto.algorithm_text())
    print("主密钥保护    : %s（%s）"
          % (ss.describe().get("后端"), ss.describe().get("说明")))
    print("密钥指纹      : %s" % crypto.key_fingerprint())
    return 0


def cmd_keyring(args):
    """密钥环体检 /（可选）备份后重置。

    换机器或换 Windows 账户后，DPAPI 包裹的密钥本机解不开（0x8009000B），
    这时只能重置；重置前会把整个数据目录备份成同级 data_backup_<时间戳>\\。
    """
    from dpp import keyring as keyring_mod

    if not args.reset:
        info = keyring_mod.status()
        print("密钥环体检")
        print("=" * 70)
        print("密钥目录      : %s" % info["keyring_dir"])
        print("包裹后端      : %s" % info["backend"])
        print("结论          : %s" % info["hint"])
        for f in info["files"]:
            line = "  %-8s %-6s 可解开=%-5s" % (
                f["label"], f["magic"] or "-", "是" if f["decryptable"] else "否")
            if f["master_key"]:
                line += " 包裹者主密钥=%s" % f["master_key"]
            print(line)
            if f["error"]:
                print("           %s" % f["error"])
        print("主密钥指纹    : %s" % info["主密钥指纹"])
        print("签名密钥指纹  : %s" % info["签名密钥指纹"])
        print("本机 DPAPI 主密钥 %d 把" % len(info["dpapi_master_keys_here"]))
        print()
        if info["usable"] is False:
            print("处置：python cli.py keyring --reset --yes --with-demo")
            print("      或双击「修复密钥环.bat」（会先整目录备份）")
        return 0 if info["usable"] is not False else 4

    if not args.yes:
        print("重置会：先整目录备份 -> 删除旧密钥 -> 删除用旧密钥加密且已解不开的数据")
        print("        （dpp.db / vault / exports / reports），然后重新生成密钥。")
        print("确认请显式加 --yes：")
        print("    python cli.py keyring --reset --yes --with-demo")
        return 2

    res = keyring_mod.reset(backup=not args.no_backup,
                            seed_demo=args.with_demo, log=print)
    print()
    print("重置完成")
    print("=" * 70)
    print("数据目录      : %s" % res["数据目录"])
    print("备份          : %s" % (res["备份"] or "-"))
    print("主密钥指纹    : %s" % res["主密钥指纹"])
    print("签名密钥指纹  : %s" % res["签名密钥指纹"])
    print("密钥环可用    : %s" % ("是" if res["可用"] else "否"))
    if "演示数据" in res:
        print("演示数据      :")
        for k, v in res["演示数据"].items():
            print("  %-16s %s" % (k, v))
    return 0


# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="cli.py",
        description="考生数据保护 · 命令行入口（数据分类分级 / 全生命周期 / 权利响应 / 验收指标）",
    )
    p.add_argument("--db", default="", help="指定 sqlite 数据库路径（默认运行目录）")
    p.add_argument("--user", default="", help="登录账号（默认 owner）")
    p.add_argument("--pwd", default=None, help="登录口令（默认取内置账户口令）")
    p.add_argument("--place", default="", help="登录地点（用于持续校验风险评分）")
    p.add_argument("--device", default="", help="登录终端（用于持续校验风险评分）")
    p.add_argument("--hour", type=int, default=None, help="登录小时（0-23，用于风险评分）")
    p.add_argument("--confirm-risky", action="store_true",
                   help="对高风险会话执行敏感动作时进行二次确认（留痕）")

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("summary", help="打印摘要")
    rp = sub.add_parser("report", help="生成报告并导出")
    rp.add_argument("--stdout", action="store_true", help="同时把全文打印到标准输出")

    sub.add_parser("verify", help="校验审计哈希链")

    sub.add_parser("demo", help="生成演示数据")

    sub.add_parser("catalog", help="打印分类分级表")
    sub.add_parser("permission", help="打印权限矩阵")
    sub.add_parser("kpi", help="打印验收指标")
    sub.add_parser("algorithm", help="打印当前加密算法与密钥指纹")

    kr = sub.add_parser("keyring", help="密钥环体检 / 备份 / 重置")
    kr.add_argument("--status", action="store_true", help="只体检（默认动作）")
    kr.add_argument("--reset", action="store_true",
                    help="先整目录备份，再重置密钥并清掉旧密钥加密的数据")
    kr.add_argument("--yes", action="store_true", help="确认执行重置（必须显式给出）")
    kr.add_argument("--no-backup", action="store_true", help="重置前不备份（危险）")
    kr.add_argument("--with-demo", action="store_true", help="重置后重新播种演示数据")

    de = sub.add_parser("destroy", help="执行销毁")
    de.add_argument("--now", action="store_true", help="立即执行到期自动销毁（默认行为）")
    de.add_argument("--by-request", default="", help="按考生请求销毁，值为 cand_ref")
    de.add_argument("--reason", default="", help="销毁原因")
    de.add_argument("--witness", default="", help="见证人（强制销毁需双人）")
    de.add_argument("--force", action="store_true", help="强制销毁（需双人确认）")

    us = sub.add_parser("use", help="登记一次数据使用（含目的限定校验）")
    us.add_argument("--cand", required=True, help="考生编号 cand_ref")
    us.add_argument("--purpose", required=True, help="使用目的")
    us.add_argument("--action", required=True, help="使用动作")
    us.add_argument("--terminal", default="", help="使用终端（覆盖全局 --device）")

    ri = sub.add_parser("rights", help="考生权利响应")
    ri.add_argument("--list", action="store_true", help="列出权利申请")
    ri.add_argument("--submit-cand", default="", help="提交权利申请，值为 cand_ref")
    ri.add_argument("--right", default="查阅", help="权利类型：知情/查阅/复制/更正/删除/撤回同意")
    ri.add_argument("--detail", default="", help="申请详情")
    ri.add_argument("--verified", action="store_true", help="身份已核验")
    ri.add_argument("--alt-channel", action="store_true", help="经替代通道受理")
    ri.add_argument("--handle", default="", help="处理某条申请，值为 req_id")
    ri.add_argument("--result", default="", help="处理结果")
    ri.add_argument("--reason", default="", help="不予处理/拒绝理由")
    ri.add_argument("--approve", action="store_true", help="通过（默认通过）")

    inc = sub.add_parser("incident", help="上报安全事件")
    inc.add_argument("--level", default="一般", help="事件级别：一般/较大/重大/特别重大")
    inc.add_argument("--title", required=True, help="事件标题")
    inc.add_argument("--detail", default="", help="事件详情")
    inc.add_argument("--handler", default="", help="处置人")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    handler = {
        "summary": cmd_summary,
        "report": cmd_report,
        "verify": cmd_verify,
        "demo": cmd_demo,
        "catalog": cmd_catalog,
        "permission": cmd_permission,
        "kpi": cmd_kpi,
        "algorithm": cmd_algorithm,
        "keyring": cmd_keyring,
        "destroy": cmd_destroy,
        "use": cmd_use,
        "rights": cmd_rights,
        "incident": cmd_incident,
    }[args.cmd]
    try:
        return handler(args)
    except (access.AccessError, lc_mod.LifecycleError, rights_mod.RightsError) as exc:
        print("拒绝：%s" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
