# -*- coding: utf-8 -*-
r"""
03 智能终端 · 运行模式（考试 / 非考试（教学））

为什么单独一个模块：
  "当前是不是在考试"会影响**好几处**行为（强制确认、红色弹窗、告警标记、界面提示、
  驾驶舱徽标、网页接口…）。如果各处自己读 kv，很容易出现"一半按考试、一半按教学"
  的撕裂状态。所以把读取 / 校验 / 切换 / 留痕收敛到这里，其它地方只调 is_exam()。

存储：`kv['run_mode']`（`storage.get_kv/set_kv`），默认 `exam`。
  为什么放 kv 而不是 config.py：这是**运行期状态**，要跨进程/重启保持，
  而且要能在界面上切；config.py 是静态配置。

两种模式的真实差异（与本模块配套的代码位置）：
  · review.py            —— 教学模式下红色预警不强制"先确认再复核"
  · terminal_gui.py      —— 教学模式下不弹红色弹窗；顶栏/状态栏显示当前模式
  · alerts.py            —— 教学模式下新告警打 extra["run_mode"]="teach"，便于事后区分
  · 驾驶舱 / 网页接口     —— 显示当前模式，避免"以为在考试模式其实不是"
  · 审计链                —— **两种模式都照常写**，切换动作本身也写一条审计（谁切的、为什么）
"""
from __future__ import annotations

import config

KEY = "run_mode"


def normalize(mode):
    """把任意输入规范成合法模式（不认识就回默认，绝不返回 None）。"""
    m = str(mode or "").strip().lower()
    return m if m in config.RUN_MODES else config.RUN_MODE_DEFAULT


def get(store):
    """当前运行模式（读不到/写坏了都回默认考试模式，宁严不松）。"""
    try:
        return normalize(store.get_kv(KEY, config.RUN_MODE_DEFAULT))
    except Exception:
        return config.RUN_MODE_DEFAULT


def is_exam(store):
    """是否处于考试模式（严格口径）。"""
    return get(store) == config.RUN_MODE_EXAM


def is_teach(store):
    return get(store) == config.RUN_MODE_TEACH


def label(mode=None, short=False):
    m = normalize(mode) if mode else None
    table = config.RUN_MODE_SHORT if short else config.RUN_MODE_TEXT
    if m is None:
        return "考试 / 教学"
    return table.get(m, m)


def effects(mode):
    """这个模式"改变了什么"——给确认弹窗和说明文字用（与真实实现一一对应）。"""
    m = normalize(mode)
    if m == config.RUN_MODE_EXAM:
        return [
            "红色预警必须**先确认**才能复核",
            "红色预警会**弹窗提醒**（考试期间不允许漏看）",
            "新告警按考试口径留档（不打教学标记）",
            "审计链、复核留痕照常",
        ]
    return [
        "红色预警**不强制**先确认（可直接复核，结论仍留痕）",
        "红色预警**不弹窗**（只在列表标红 + 状态栏提示，减少课堂打扰）",
        "新告警打 `run_mode=teach` 标记，事后可与考试数据区分",
        "审计链、复核留痕照常（**不清空、不弱化**）",
    ]


def set(store, mode, operator="", note=""):
    """切换运行模式：返回 (ok, 说明)。切换动作本身写审计链（可追溯）。

    不做权限判断 —— 权限由调用方（界面 `session.require(config.RUN_MODE_ACTION)`、
    接口层）先判；这里只保证"值合法 + 落库 + 留痕"。
    """
    m = str(mode or "").strip().lower()
    if m not in config.RUN_MODES:
        return False, "不认识的运行模式：%s（可用：%s）" % (
            mode, "、".join(config.RUN_MODES))
    old = get(store)
    if old == m:
        return True, "已经是「%s」，无需切换" % label(m)
    try:
        store.set_kv(KEY, m)
    except Exception as e:                              # noqa: BLE001
        return False, "写入运行模式失败：%s" % str(e)[:120]
    detail = "由「%s」切换为「%s」%s" % (label(old), label(m),
                                     ("（%s）" % note) if note else "")
    try:
        store.log("切换运行模式", detail, operator or "terminal")
    except Exception:
        pass
    return True, detail


def summary(store):
    """一行摘要（状态栏/驾驶舱用）。"""
    m = get(store)
    if m == config.RUN_MODE_EXAM:
        return "考试模式：红色须强制确认、弹窗提醒"
    return "非考试模式（教学）：不强制确认、不弹窗；留痕照常"
