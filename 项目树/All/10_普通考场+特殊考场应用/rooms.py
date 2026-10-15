# -*- coding: utf-8 -*-
r"""
考场模板引擎（《特殊考场应用.md》第二节"通用设计要求"第 1、2 条）

职责：
  * 建考场 / 一键套用模板（点位、算法、参数、告警、合规、行为开关）
  * 计算"该考场类型应有的能力"与"实际具备的能力"，给出缺口（阻断/期望）
  * 最小可用配置降级（1 台摄像机 + 1 条链路也能完成巡考并留痕）
  * 把方案里的"明确不适用行为"落到行为开关上（无障碍/口试/隔离等）

设计要点：
  1) 模板是**声明式**的：差异全部写在 config.ROOM_TYPES[type]["params"/"caps"] 里，
     本模块只做「能力 <- 点位/开关」的推断，不硬编码任何一类考场。
  2) 降级不降标准：能不能降级由 config.MINIMAL_ALLOWED_TYPES 决定；
     涉密/隔离/无障碍即使降到最小可用配置，其**合规必需能力**仍不可省。
  3) 不因技术原因拒考：无障碍考场缺替代核验通道时判 blocking，且不允许
     用"人脸核验"顶替（否则就是技术性拒考）。
"""
import config
import models
from models import jload

# 点位名 -> 能力 的推断表（点位配齐即视为具备该能力）
POINT_CAPABILITY = {
    "备用机位": "redundant_camera",
    "备用机位（建议）": "redundant_camera",
    "侧/后部特写机位": "multi_angle",
    "前部全景机位": "multi_angle",
    "中部全景机位": "multi_angle",
    "后部全景机位": "multi_angle",
    "通道机位": "multi_angle",
    "正面机位": "multi_angle",
    "侧后方 45° 机位": "dual_camera",
    "考生正面机位": "multi_angle",
    "考官机位": "multi_angle",
    "加宽通道机位": "multi_angle",
    "低位核验终端": "accessibility",
    "语音+文字双提示终端": "non_face_channel",
    # "人工核验工位"是唯一能顶起 alt_verify 的点位：
    # 语音+文字提示只是"看得见/听得见"，不等于"有人工核验通道"，
    # 所以不能拿它去满足"不得因技术原因拒考"那条硬要求。
    "人工核验工位": "alt_verify",
    "远程监考视讯": "remote_invigilate",
    "屏幕行为审计代理": "screen_audit",
    "高保真录音": "hifi_audio",
    "拾音器": "audio",
    "拾音器阵列": "audio",
    "语音提示终端（含拾音）": "audio",
    "独立拾音": "independent_mic",
}

# 明确不适用的行为（考场类型 -> 行为码）：避免"特殊考场按普通口径硬判"
BEHAVIOR_EXCLUSIONS = {
    config.TYPE_ACCESSIBLE: {
        "B_STAND_UP", "B_LEAN_FORWARD", "B_GAZE_SHIFT", "B_HAND_SIGNAL",
        "B_LOOK_DOWN", "G_COLLECTIVE_STAND", "G_LEAVE_SEAT",
    },
    config.TYPE_ORAL: {
        "B_WHISPER", "B_GAZE_SHIFT", "B_LOOK_DOWN", "B_SLEEPY",
        "G_MULTI_TALK", "G_NOISE_RISE", "G_COLLECTIVE_TURN",
        "I_CHAT", "I_SIT", "I_BEND_OVER",
    },
    config.TYPE_ISOLATION: {
        "G_MULTI_TALK", "G_CROWD_GATHER", "G_COLLECTIVE_TURN",
        "G_COLLECTIVE_STAND", "G_LEAVE_SEAT", "G_CHAIN_PASS",
    },
    config.TYPE_BACKUP: {"G_CROWD_GATHER", "G_COLLECTIVE_STAND"},
    config.TYPE_LARGE: set(),
    config.TYPE_COMPUTER: {"B_PASS_ITEM", "G_CHAIN_PASS"},
    config.TYPE_CLASSIFIED: {"G_CROWD_GATHER", "G_NOISE_RISE"},
    config.TYPE_STANDARD: set(),
}

# 只在特定考场类型启用的行为类别/行为码
TYPE_ONLY_BEHAVIORS = {
    config.CAT_SCREEN: (config.TYPE_COMPUTER,),
    config.CAT_AUDIO: (config.TYPE_ORAL,),
}


class RoomError(Exception):
    pass


def normalize_type(room_type):
    if room_type in config.ROOM_TYPES:
        return room_type
    raise RoomError("未知考场类型：%s（可选：%s）"
                    % (room_type, "、".join(config.ROOM_TYPES)))


def default_points(room_type):
    """模板点位清单的深拷贝（带 required 标记与安装信息）。"""
    meta = config.ROOM_TYPES[normalize_type(room_type)]
    return [dict(p) for p in meta["points"]]


def create_room(name, room_type=config.TYPE_STANDARD, operator="", **kw):
    """新建考场（套模板由 apply_template 负责，这里只落基础信息 + 启用申请）。"""
    rt = normalize_type(room_type)
    meta = config.ROOM_TYPES[rt]
    room = models.Room(
        name=(name or "").strip() or "未命名考场",
        room_type=rt,
        site=kw.get("site") or config.load_settings().get("site", ""),
        building=kw.get("building") or "", floor=kw.get("floor") or "",
        seats=models.to_int(kw.get("seats"), meta["seats_default"]),
        session_name=kw.get("session_name") or "",
        apply_by=kw.get("apply_by") or operator,
        apply_reason=kw.get("apply_reason") or "",
        applied_at=models.now_text() if (kw.get("apply_reason") or operator) else "",
        status=config.PHASE_APPLY if not kw.get("skip_apply") else config.PHASE_TEMPLATE,
        invigilators_required=models.to_int(kw.get("invigilators_required"),
                                            meta.get("invigilators", 2)),
        data_domain=meta["compliance"]["data_domain"],
        retention_days=models.to_int(kw.get("retention_days"),
                                     meta["compliance"]["min_retention_days"]),
        note=kw.get("note") or "")
    room.alert_policy = dict(config.ALERT_POLICY)
    return room


def apply_template(room, operator="", room_type=None, keep_points=False, store=None):
    r"""
    一键套用考场模板：把该类型模板的差异整体落到考场对象上。

    store 给了就顺手落库（省得调用方忘了保存，导致"内存里套了模板、库里还是空的"）。

    返回 dict：{"room":..., "diff": {...}}，diff 里逐项列出**与普通考场的差异**，
    便于界面/报告解释"特殊考场差异项"到底差在哪（方案要求"避免逐场手工调参"）。
    """
    if room_type:
        room.room_type = normalize_type(room_type)
    rt = normalize_type(room.room_type)
    meta = config.ROOM_TYPES[rt]
    base = config.ROOM_TYPES[config.TYPE_STANDARD]

    if not keep_points:
        room.points = default_points(rt)
    room.points_missing = missing_point_names(room)

    # 能力开关回到"按模板重新配置"的起点：这些开关代表**实测/已办事项**，
    # 换模板（或新建考场）后必须重新核验，不能沿用上一类的结论。
    room.drill_done = 0
    room.offline_ready = 0
    room.fast_enable = 0
    room.doctor_link = 0
    room.network_log = 0
    room.independent_mic = 0
    room.dedup_verified = 0
    room.screen_audit = 0
    room.dpia_done = 0
    room.alt_channel = 0
    room.audio_sample_rate = 0
    room.points_verified = 0
    room.calibration_at = ""

    # 算法/参数/告警/合规差异
    room.alert_policy = dict(config.ALERT_POLICY)
    room.alert_policy.update(meta.get("alerts") or {})
    room.data_domain = meta["compliance"]["data_domain"]
    room.retention_days = max(room.retention_days or 0,
                              meta["compliance"]["min_retention_days"],
                              config.MIN_RETENTION_DAYS)
    room.degrade_used = 0
    room.min_config = "full"

    # 行为开关：全量启用 -> 去掉该类型明确不适用的 -> 类别白名单
    room.enabled_behaviors = enabled_behaviors_for(rt)

    # 能力缺口
    recompute_caps(room)

    room.template_applied = 1
    room.template_at = models.now_text()
    if room.status in (config.PHASE_APPLY, ""):
        room.status = config.PHASE_TEMPLATE

    if store is not None:
        store.save_room(room)

    diff = template_diff(rt)
    return {"room": room, "diff": diff, "operator": operator}


def template_diff(room_type):
    """逐项列出该类型相对普通考场的差异（供界面/报告显示）。"""
    rt = normalize_type(room_type)
    if rt == config.TYPE_STANDARD:
        return {"type": rt, "name": config.ROOM_TYPES[rt]["name"], "items": [],
                "note": "普通考场为基准模板"}
    meta = config.ROOM_TYPES[rt]
    base = config.ROOM_TYPES[config.TYPE_STANDARD]
    items = []

    # 点位差异
    bt = {p["no"] for p in base["points"]}
    mt = {p["no"] for p in meta["points"]}
    for no in sorted(mt - bt):
        items.append({"field": "点位", "change": "新增", "value": no})
    for no in sorted(bt - mt):
        items.append({"field": "点位", "change": "取消", "value": no})
    if meta["points_min"] != base["points_min"]:
        items.append({"field": "最小机位数", "change": "调整",
                      "value": "%s -> %s" % (base["points_min"], meta["points_min"])})
    # 算法/参数差异
    for k, v in sorted((meta.get("params") or {}).items()):
        items.append({"field": "算法参数", "change": "调整",
                      "value": "%s = %s" % (k, v)})
    items.append({"field": "算法口径", "change": "调整",
                  "value": meta["algo"]["note"]})
    # 告警差异
    for k, v in sorted((meta.get("alerts") or {}).items()):
        items.append({"field": "告警策略", "change": "调整",
                      "value": "%s = %s" % (k, v)})
    push = config.SPECIAL_PUSH.get(rt) or {}
    for x in push.get("extra", []):
        items.append({"field": "推送对象", "change": "新增", "value": x})
    for x in push.get("remove", []):
        items.append({"field": "推送对象", "change": "取消", "value": x})
    # 合规差异
    bc, mc = base["compliance"], meta["compliance"]
    for k in ("data_domain", "domain_isolated", "min_retention_days",
              "dpia_required", "offline_required", "offline_mode",
              "identity_mode", "drill_required"):
        if bc.get(k) != mc.get(k):
            items.append({"field": "合规", "change": "调整",
                          "value": "%s: %s -> %s" % (k, bc.get(k), mc.get(k))})
    # 行为差异
    excl = BEHAVIOR_EXCLUSIONS.get(rt) or set()
    for code in sorted(excl):
        b = config.behavior_dict().get(code)
        items.append({"field": "行为开关", "change": "停用",
                      "value": "%s %s" % (code, b["name"] if b else "")})
    return {"type": rt, "name": meta["name"], "items": items,
            "acceptance": mc["acceptance_note"],
            "note": " / ".join(meta["scenarios"])}


def enabled_behaviors_for(room_type):
    """该考场类型应启用的行为码清单（含附加的机考/口试行为）。"""
    rt = normalize_type(room_type)
    excl = BEHAVIOR_EXCLUSIONS.get(rt) or set()
    out = []
    for t in list(config.BEHAVIORS) + list(config.EXTRA_BEHAVIORS):
        code, _name, cat = t[0], t[1], t[2]
        only = TYPE_ONLY_BEHAVIORS.get(cat)
        if only and rt not in only:
            continue
        if code in excl:
            continue
        out.append(code)
    return out


def missing_point_names(room):
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    have = {p.get("no") for p in (room.points or [])}
    return [p["no"] for p in meta["points"] if p["required"] and p["no"] not in have]


def present_capabilities(room):
    """
    按已配置点位 + 已办事项 + 显式开关推断实际具备的能力。

    **纯快照**：不查外部系统、不产生副作用，套模板后立刻可算。
    两类来源：
      * point（点位）：配了就有
      * job（已办事项）：做了才有——DPIA/演练/离线自检/分配域/登记日志代理…
        这类在刚套完模板时必然是"缺"的，那是正常的（那件事还没做）。
    """
    have = set()
    for p in (room.points or []):
        cap = POINT_CAPABILITY.get(p.get("no") or "")
        if cap:
            have.add(cap)
    mic = meta_mic(room.room_type)
    if mic and mic.get("level") == config.REQ_MUST:
        have.add("audio")
    if room.screen_audit:
        have.add("screen_audit")
    if room.offline_ready:
        have.add("offline")
        have.add("no_external")
    if room.fast_enable:
        have.add("fast_enable")
    if room.doctor_link:
        have.add("doctor_link")
    if room.network_log:
        have.add("network_log")
    if room.independent_mic:
        have.add("independent_mic")
    if room.alt_channel:
        have.add("alt_verify")
    if room.drill_done:
        have.add("drill")
    if room.dedup_verified:
        have.add("dedup")
    if room.audio_sample_rate:
        try:
            meta = config.ROOM_TYPES.get(room.room_type, {})
            need = models.to_int((meta.get("params") or {}).get(
                "audio_sample_rate_min"), 0)
            if not need or models.to_int(room.audio_sample_rate) >= need:
                have.add("hifi_audio")
        except (TypeError, ValueError):
            pass
    # privacy_domain = "该考场的数据落在一个独立分区域里"：
    # 它是"分域"这件事的结果，不靠点位，靠数据域本身的隔离属性判定。
    rules = config.DOMAIN_RULES.get(room.data_domain or "", {})
    if rules.get("isolated"):
        have.add("privacy_domain")
    return have


def meta_mic(room_type):
    return config.ROOM_TYPES.get(room_type, {}).get("mic")


def recompute_caps(room):
    """
    重算能力缺口：must 缺失 -> caps_missing（阻断）；expect 缺失 -> caps_weak。

    每条缺口都带 ``src``（point/job），让上层能把"缺实体点位"和
    "还有一件事没做"分开说人话、分别给处置建议与缺陷码。
    """
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    have = present_capabilities(room)
    missing, weak, present = [], [], []
    for cap, spec in (meta.get("caps") or {}).items():
        level = spec["level"]
        src = spec.get("src") or config.CAP_SRC_POINT
        item = {"cap": cap, "name": spec["name"], "src": src}
        if cap in have:
            present.append(item)
        elif level == config.REQ_MUST:
            missing.append(dict(item, how=CAP_HOWTO.get(cap, "")))
        else:
            weak.append(dict(item, how=CAP_HOWTO.get(cap, "")))
    room.caps_present = present
    room.caps_missing = missing
    room.caps_weak = weak
    room.points_missing = missing_point_names(room)
    return room


# 每项能力"怎么补齐"的操作提示（界面/报告直接显示，省得对着缺口发呆）
CAP_HOWTO = {
    "fast_enable": "在「准入自检」页勾选「设备预置即插即用」",
    "remote_invigilate": "登记远程监考员（准入自检③ 或 set_room 的 remote_invigilator）",
    "doctor_link": "在「合规留痕」页做一次 DPIA/协同确认，或在准入自检③ 登记医生协同",
    "privacy_domain": "本考场数据域已隔离（隔离/机考/涉密自动具备）；"
                      "若仍为常规域，请先在「数据分域」页建独立分区",
    "drill": "在「合规留痕」页记录一次替代通道演练（需见证人）",
    "screen_audit": "在「准入自检」页勾选「屏幕行为审计」",
    "network_log": "在「准入自检」页勾选「屏幕行为审计」并登记网络行为日志代理",
    "hifi_audio": "在「准入自检」页把录音采样率设为 48000 或以上",
    "independent_mic": "在「准入自检」页把录音采样率设为独立拾音要求值，"
                       "或在「考场台账」补一个「独立拾音」点位",
    "dedup": "在「准入自检」页勾选「跨机位去重已验证」",
    "offline": "在「合规留痕」页做一次全离线自检",
    "no_external": "在「合规留痕」页做一次全离线自检（会一并确认不上云）",
    "alt_verify": "补一个「人工核验工位」点位，或在准入自检页勾选替代核验通道",
    "non_face_channel": "配「人工核验工位」或「语音+文字双提示终端」点位",
    "accessibility": "配低位核验终端 / 加宽通道 / 语音+文字双提示点位",
    "multi_angle": "按模板补齐必需机位",
    "dual_camera": "按模板补齐正面 + 侧后方 45° 双机位",
    "redundant_camera": "补一个备用机位（与主特写同向）",
    "audio": "配拾音器 / 语音提示终端",
}


def import_points(room, points):
    r"""
    导入实际点位（来自 02 摄像头核查台账或手工录入）。

    points 每项：{no, device_code?, where?, angle?, target?, required?}
    只保留该考场模板里存在的点位名；未知点位按"额外点位"计入（不参与能力推断）。
    """
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    valid = {p["no"]: p for p in meta["points"]}
    out = []
    for p in (points or []):
        no = (p.get("no") or p.get("name") or "").strip()
        if not no:
            continue
        base = dict(valid.get(no) or {})
        base.update({k: v for k, v in p.items() if v not in (None, "")})
        base.setdefault("no", no)
        base.setdefault("required", no in valid)
        base["in_template"] = no in valid
        out.append(base)
    room.points = out
    recompute_caps(room)
    return room


def add_point(room, no, device_code="", required=None, **kw):
    """
    加一个点位。

    required 显式给了就用（必填项），没给才按模板推断——这样"先删掉人工核验工位
    再加回来"这类操作不会悄悄把必需点位降成期望点位。
    """
    pts = list(room.points or [])
    tmpl_required = any(p["no"] == no and p.get("required")
                        for p in config.ROOM_TYPES.get(
                            room.room_type, {}).get("points", []))
    req = bool(required) if required is not None else tmpl_required
    for p in pts:
        if p.get("no") == no:
            p.update({"device_code": device_code})
            p.update({k: v for k, v in kw.items() if v not in (None, "")})
            p["required"] = req
            return recompute_caps(room)
    pts.append(dict({"no": no, "device_code": device_code, "required": req}, **kw))
    room.points = pts
    return recompute_caps(room)


def remove_point(room, no):
    room.points = [p for p in (room.points or []) if p.get("no") != no]
    return recompute_caps(room)


def point_device(room, no):
    for p in (room.points or []):
        if p.get("no") == no:
            return p.get("device_code") or ""
    return ""


def capability_advice(room):
    """
    给出能力缺口的处置建议：能否降级、降级到哪一档、哪些缺口仍必须补齐。

    返回 {"blocking": [...], "weak": [...], "suggest_level": "full/reduced/minimal",
          "can_arm": bool, "message": str}
    """
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    total_points = len([p for p in (room.points or [])])
    must_points = len([p for p in (room.points or []) if p.get("required")])
    need_must = len([p for p in meta["points"] if p["required"]])
    missing_must_points = need_must - must_points

    blocking = list(room.caps_missing or [])
    weak = list(room.caps_weak or [])

    # 降级档位建议
    level = "full"
    if missing_must_points > 0:
        level = "reduced"
    min_points = meta["points_min"]
    if total_points < min_points or (need_must - must_points) > 0 and total_points <= min_points:
        level = "minimal"
    if total_points == 0 or (need_must > 0 and total_points < min_points):
        level = "minimal"

    allowed = room.room_type in config.MINIMAL_ALLOWED_TYPES
    if level == "minimal" and not allowed:
        # 普通考场/机考/口试/大教室：不适用"临机降级"，点位不足就是点位不足
        blocking.append({"cap": "points", "name":
                         "该考场类型不允许降到最小可用配置（点位不足需补齐）"})
    can_arm = not blocking
    msg = "能力齐备，可按标准配置启用"
    if blocking:
        msg = "存在 %d 项必需能力缺口：%s" % (
            len(blocking), "；".join(x["name"] for x in blocking))
    elif weak:
        msg = "必需能力齐备（%d 项期望能力未配置，建议补齐）" % len(weak)
    return {"blocking": blocking, "weak": weak, "suggest_level": level,
            "can_arm": can_arm, "message": msg,
            "points": total_points, "points_required_present": must_points,
            "points_required_need": need_must}


def degrade_to_minimal(room, level="minimal", operator="", store=None):
    """
    降级到最小可用配置（《特殊考场应用.md》第二节第 2 条）。

    只裁剪**期望/非必需**点位，必需点位一个不删；降级后重算能力缺口，
    并把降级本身记为留痕（降级可用 ≠ 降低标准）。
    """
    if level not in config.MIN_CONFIG_LEVELS:
        raise RoomError("未知降级档位：%s" % level)
    if level == "minimal" and room.room_type not in config.MINIMAL_ALLOWED_TYPES:
        raise RoomError("该考场类型（%s）不允许降级到最小可用配置：%s"
                        % (room.type_name(),
                           "、".join(config.type_name(t) for t in
                                     config.MINIMAL_ALLOWED_TYPES)))
    keep_required = level in ("minimal", "reduced")
    # reduced：保留必需点位 + 1 个期望点位（留一条冗余链路）
    # minimal：只保留必需点位（1 机位 + 1 链路也能巡考并留痕）
    extra_keep = 1 if level == "reduced" else 0
    kept, dropped, extra_taken = [], [], 0
    for p in (room.points or []):
        if not p.get("required") and keep_required:
            if extra_taken < extra_keep:
                extra_taken += 1
                kept.append(p)
            else:
                dropped.append(p.get("no"))
            continue
        kept.append(p)
    room.points = kept
    room.min_config = level
    room.degrade_used = 1
    recompute_caps(room)
    if store is not None:
        store.save_room(room)
    return {"room": room, "level": level,
            "level_name": config.MIN_CONFIG_LEVELS[level]["name"],
            "kept": [p.get("no") for p in kept], "dropped": dropped,
            "operator": operator,
            "message": "已降级为%s：%s" % (config.MIN_CONFIG_LEVELS[level]["name"],
                                        config.MIN_CONFIG_LEVELS[level]["desc"])}


def restore_full(room):
    room.min_config = "full"
    room.degrade_used = 0
    room.points = default_points(room.room_type)
    return recompute_caps(room)


def upsert_room(store, name, room_type=config.TYPE_STANDARD, operator="", **kw):
    """按名字 upsert 考场（同名即更新，避免重复建档）。"""
    room = store.get_room_by_name((name or "").strip())
    if room is None:
        room = create_room(name, room_type, operator=operator, **kw)
    else:
        if room_type and room_type != room.room_type:
            room.room_type = normalize_type(room_type)
        for k in ("site", "building", "floor", "session_name", "apply_reason",
                  "note"):
            if kw.get(k):
                setattr(room, k, kw[k])
        if kw.get("seats"):
            room.seats = models.to_int(kw["seats"], room.seats)
    if kw.get("apply_reason"):
        room.apply_reason = kw["apply_reason"]
        room.applied_at = room.applied_at or models.now_text()
    return store.save_room(room)


def room_brief(room):
    """给界面/接口用的紧凑视图。"""
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    return {
        "room_id": room.room_id, "name": room.name, "room_type": room.room_type,
        "room_type_name": room.type_name(), "special": room.is_special(),
        "status": room.status, "status_text": room.status_text(),
        "seats": room.seats, "session_name": room.session_name,
        "session_state": room.session_state,
        "min_config": room.min_config,
        "min_config_name": room.min_config_meta()["name"],
        "points": len(room.points or []),
        "points_required": len([p for p in (room.points or []) if p.get("required")]),
        "points_need": len([p for p in meta["points"] if p["required"]]),
        "caps_missing": len(room.caps_missing or []),
        "caps_present": len(room.caps_present or []),
        "behaviors": len(room.enabled_behaviors or []),
        "confidence_min": room.confidence_min(),
        "data_domain": room.data_domain,
        "data_domain_name": config.DOMAIN_RULES.get(room.data_domain, {}).get("name", ""),
        "last_check_at": room.last_check_at,
        "last_check_ok": bool(room.last_check_ok),
    }


def template_catalog():
    """模板清单（含差异项），给界面展示与接口 templates。"""
    out = []
    for t in config.TYPE_ORDER:
        meta = config.ROOM_TYPES[t]
        out.append({
            "type": t, "name": meta["name"], "short": meta["short"],
            "special": meta["special"], "scope": meta["scope"],
            "seats_default": meta["seats_default"],
            "invigilators": meta.get("invigilators", 0),
            "remote_invigilators": meta.get("remote_invigilators", 0),
            "points": [dict(p) for p in meta["points"]],
            "points_min": meta["points_min"],
            "dev_spec": meta["dev_spec"],
            "mic": meta.get("mic"),
            "extras": meta.get("extras", []),
            "algo": meta["algo"],
            "params": meta.get("params", {}),
            "alerts": meta.get("alerts", {}),
            "compliance": meta["compliance"],
            "caps": [{"cap": k, **v} for k, v in sorted(meta["caps"].items())],
            "cap_musts": sorted(k for k, v in meta["caps"].items()
                                if v["level"] == config.REQ_MUST),
            "behaviors": len(enabled_behaviors_for(t)),
            "scenarios": meta["scenarios"],
            "diff": template_diff(t),
        })
    return out
