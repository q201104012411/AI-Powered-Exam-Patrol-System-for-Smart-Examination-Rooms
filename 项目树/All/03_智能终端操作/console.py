# -*- coding: utf-8 -*-
"""
命令控制台 · 开关与权限层级（默认**全部关闭**）

为什么单独一个模块、而不是把判断塞进界面：
    命令控制台是**任意代码执行**能力：所有者可以跑任意系统命令与 Python 代码，
    等于把本终端（本机）的完全控制权交给登录所有者账户的人。这种能力的分层规则
    在本端只允许有一份实现（console_tier）——否则"界面放行、后端拒绝"或反过来，
    早晚会错一处，而错的那一次就是整机失守；跨到 01 侧则**复用 01 的判定**，
    不在本端另立一套规则（见文件末尾"与 01 的分工"）。

两把开关（都是标记文件：存在=开启，不存在=关闭，写法与 01 一致）：
    config.CONSOLE_FLAG        总开关，只有所有者能拨；关闭时所有角色一律 none
    config.CONSOLE_ADMIN_FLAG  所有者对管理员的单独授权，只放行 `ollama run <模型> [提示词]`

分层（与 01 完全一致）：
    owner                 -> full   系统命令 / Python 代码
    admin（且被单独授权）  -> ollama 仅 `ollama run <模型> [提示词]`，用于 AI 辅助判断
    其他角色              -> none

为什么管理员只到 ollama 这一级：
    完整命令执行可以改代码、读考生生物特征库、装后门，因此不下放；而 `ollama run`
    是本地大模型的单向问答，借它做辅助判断不触碰系统状态。两级之间的白名单由 01 的
    cmd_console.parse_ollama_command() 保证——03 端**不复制、不放宽**那个解析器。

与 01 的分工（避免出现两套真相）：
    本模块只管"本端开关状态"与界面提示；真正的执行与最终层级判定在 01 的
    cmd_console.execute() 里（它内部还会用 accounts.cmd_console_tier(role) 再判一次）。
    桥接子进程用 EXAM_HOME 把 01 的数据根指到本端数据目录下的 cmd_bridge_home，
    于是"能不能用"来自本端开关，"谁能用哪一级"仍由 01 按 role 判定；01 的审计链与
    临时脚本也落在本端数据目录，**不会往 01 目录写任何文件**（见 bridge_home）。
"""
import os

import config

CONSOLE_TIER_NONE = "none"
CONSOLE_TIER_OLLAMA = "ollama"
CONSOLE_TIER_FULL = "full"

TIER_TEXT = {
    CONSOLE_TIER_NONE: "无权限（命令控制台未开启，或当前角色不在允许范围）",
    CONSOLE_TIER_OLLAMA: "管理员 · 仅 AI 辅助判断（ollama）",
    CONSOLE_TIER_FULL: "所有者 · 完整系统命令 / Python 代码",
}

# 与 01 的 cmd_console 同一套限制：界面提示与后端实际限制不能对不上
CONSOLE_DEFAULT_TIMEOUT = 60
CONSOLE_MAX_TIMEOUT = 600

MODE_TEXT = {"cmd": "系统命令", "python": "Python 代码", "ollama": "AI 辅助(ollama)",
             "ai": "AI 对话"}
MODE_BY_TEXT = dict((v, k) for k, v in MODE_TEXT.items())

# 提示文案集中在这里，界面与将来的调用点共用，避免各处说法不一
RISK_HINT = ("开启后，能打开命令控制台的人即拥有本终端的完全控制权："
             "所有者可执行任意系统命令与 Python 代码，管理员仅可使用 "
             "`ollama run <模型> [提示词]`。每次执行（包括被拒绝的尝试）都会留痕。")

# 01 侧数据根（用于把本端开关镜像成 01 认的标记文件；01 的审计与临时文件也落在这里）
BRIDGE_HOME = config.DATA_ROOT / "cmd_bridge_home"


def console_enabled():
    """命令控制台总开关是否已由所有者开启（默认关闭：标记文件不存在即关闭）"""
    return os.path.exists(str(config.CONSOLE_FLAG))


def set_console(on):
    """
    所有者设置总开关。返回 (ok, msg)。

    只动总开关，**不**顺带撤销管理员授权：授权表达的是"允许管理员用 ollama"这个意愿，
    是否生效由总开关决定。这样关掉再打开，不会悄悄丢掉所有者的授权设置。
    """
    try:
        flag = str(config.CONSOLE_FLAG)
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        if on:
            with open(flag, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(flag):
            os.remove(flag)
    except OSError as e:
        return False, str(e)
    return True, ("已开启命令控制台：所有者可用完整系统命令与 Python 代码，每次执行均写入审计链"
                  if on else
                  "已关闭命令控制台：所有执行入口停用（管理员即使已获授权也一并失效，授权设置保留）")


def console_admin_allowed():
    """所有者是否已授权管理员使用 ollama AI 辅助（默认关闭）"""
    return os.path.exists(str(config.CONSOLE_ADMIN_FLAG))


def set_console_admin(allow):
    """所有者设置：是否授权管理员使用 ollama AI 辅助判断。返回 (ok, msg)"""
    try:
        flag = str(config.CONSOLE_ADMIN_FLAG)
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        if allow:
            with open(flag, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(flag):
            os.remove(flag)
    except OSError as e:
        return False, str(e)
    return True, ("已授权管理员使用 AI 辅助判断（仅 `ollama run <模型>`，且需总开关开启；"
                  "不能执行其它任何命令）" if allow else
                  "已收回管理员的 AI 辅助判断权限")


def ai_chat_allowed():
    """所有者是否已授权非所有者角色使用 AI 多轮对话（默认关闭）。所有者始终可用。"""
    return os.path.exists(str(config.AI_CHAT_FLAG))


def set_ai_chat(allow):
    """所有者设置：是否授权管理员/巡考员等使用 AI 对话。返回 (ok, msg)"""
    try:
        flag = str(config.AI_CHAT_FLAG)
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        if allow:
            with open(flag, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(flag):
            os.remove(flag)
    except OSError as e:
        return False, str(e)
    return True, ("已授权管理员/巡考员使用 AI 对话（需总开关开启）" if allow else
                  "已收回非所有者角色的 AI 对话权限")


def console_tier(role):
    """
    当前角色的命令控制台层级 —— 本端**唯一**的权限判定入口（界面与桥接都调这里）。

        总开关关闭             -> none   （任何人都不行）
        所有者                 -> full
        管理员（且被单独授权）  -> ollama
        其他角色               -> none

    注意这里是 fail-closed：role 为空或不认识，落到最后的 none。
    """
    if not console_enabled():
        return CONSOLE_TIER_NONE
    if role == config.ROLE_OWNER:
        return CONSOLE_TIER_FULL
    if role == config.ROLE_ADMIN and console_admin_allowed():
        return CONSOLE_TIER_OLLAMA
    return CONSOLE_TIER_NONE


def can_ai_chat(role):
    """
    当前角色能否使用「AI 对话」模式。
    规则：
      · 所有者：始终可用，不依赖命令控制台总开关；
      · 非所有者：总开关开启 + 已知角色 + 已授权 AI 对话。
    """
    if role == config.ROLE_OWNER:
        return True
    if not console_enabled():
        return False
    # 只给系统中已定义的角色；未知/空角色即使授权开关开着也不放行
    if role not in config.ROLE_ACTIONS:
        return False
    return ai_chat_allowed()


def console_modes(role):
    """该角色在命令控制台弹窗里可选的可执行类型"""
    # 命令控制台总开关关闭时，弹窗不出现，自然也没有可选类型
    if not console_enabled():
        return []
    modes = []
    tier = console_tier(role)
    if tier == CONSOLE_TIER_FULL:
        modes = ["cmd", "python"]
    elif tier == CONSOLE_TIER_OLLAMA:
        modes = ["ollama"]
    # AI 对话模式：总开关开启，且角色获得 AI 对话授权
    if can_ai_chat(role):
        modes.append("ai")
    return modes


def console_status(role):
    """界面用的状态快照（含提示文案），界面不自己拼权限规则"""
    tier = console_tier(role)
    ai_chat = can_ai_chat(role)
    return {
        "enabled": console_enabled(),
        "admin_allowed": console_admin_allowed(),
        "ai_chat_allowed": ai_chat_allowed(),
        "ai_chat": ai_chat,
        "tier": tier,
        "tier_text": TIER_TEXT[tier],
        "can_run": tier != CONSOLE_TIER_NONE,
        "can_full": tier == CONSOLE_TIER_FULL,
        "can_ai_chat": ai_chat,
        "modes": console_modes(role),
        "role": role,
        "role_text": config.ROLE_TEXT.get(role, role),
        "default_timeout": CONSOLE_DEFAULT_TIMEOUT,
        "max_timeout": CONSOLE_MAX_TIMEOUT,
        "hint": _hint(role, tier, ai_chat),
    }


def _hint(role, tier, ai_chat=False):
    if tier == CONSOLE_TIER_FULL:
        return "已开启：所有者可执行系统命令 / Python 代码 / AI 对话"
    if tier == CONSOLE_TIER_OLLAMA:
        if ai_chat:
            return "已开启：管理员可执行 ollama 单次辅助，并已授权 AI 对话"
        return "已开启：管理员仅可执行 `ollama run <模型> [提示词]` 做 AI 辅助判断"
    if ai_chat:
        return "已开启：当前角色已获授权使用 AI 对话"
    if console_enabled():
        return "总开关已开启；当前角色（%s）无命令控制台权限" % config.ROLE_TEXT.get(role, role)
    if console_admin_allowed() or ai_chat_allowed():
        return "关闭（默认）· 授权设置已保留，但总开关关闭时一律不生效"
    return "关闭（默认）· 开启后所有者可执行任意命令，管理员/巡考员需另行授权"


def _mirror_flag(path, present):
    """把标记文件写成与开关状态一致（幂等）"""
    if present:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("1\n")
    elif os.path.exists(path):
        os.remove(path)


def bridge_home():
    """
    返回供 01 侧使用的数据根目录（把本端两把开关**如实**镜像成 01 认的标记文件）。

    为什么必须镜像：01 的 cmd_console.execute() 内部会用 01 自己的
    accounts.cmd_console_tier(role) 再判一次层级，而那个函数读的是"01 数据根"下的
    cmd_console*.on。子进程里用 EXAM_HOME 把 01 的数据根指到本端目录后：
        · 本端总开关是"能不能用"的唯一出处，"谁能用哪一级"仍由 01 按 role 判定；
        · 01 的审计链、临时脚本、动作日志都落在本端数据目录，不往 01 目录写一个字节
          （这也是"不修改 01 目录任何文件"这条约束得以成立的实现方式）。

    注意：镜像**不等于放行**。两个标记都写上以后，role=patrol 的调用照样会被 01 判成
    none——因为 01 只看角色（标记文件只代表"开关开着"）。换句话说，这里搬运的只是开关
    状态，任何角色都不会因为被搬运而多拿到权限。
    """
    home = str(BRIDGE_HOME)
    os.makedirs(home, exist_ok=True)
    _mirror_flag(os.path.join(home, "cmd_console.on"), console_enabled())
    _mirror_flag(os.path.join(home, "cmd_console_admin.on"), console_admin_allowed())
    # AI 对话授权不需要镜像到 01 的 cmd_bridge_home：03 端本地直接调 ollama，不经过 01 侧判定
    return home
