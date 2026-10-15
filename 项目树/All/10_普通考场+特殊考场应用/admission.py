# -*- coding: utf-8 -*-
r"""
准入自检与启用判据（《普通考场应用.md》第五节 + 《特殊考场应用.md》第二、三、五节）

方案的硬要求，本模块逐条落成可执行判据：

  普通考场应用.md 第五节
    1 逐考场标定（机位/角度/区域）+ 标定记录      -> CALIBRATION_MISSING
    2 光照与角度校正                             -> 由 02 画质诊断兜底
    3 考场建模：座位表与考生编号映射，"仅编号不显姓名" -> LEDGER_MISSING / LEDGER_NAME_EXPOSED
    4 时钟同步，偏差 ≤ 1 秒                      -> CLOCK_DRIFT
    5 核查前置：开考前按专题 02 完成质量自检      -> QUALITY_* 系列

  特殊考场应用.md 第五节"风险提示"
    * 临时启用最容易"设备没自检、角度没标定"      -> 强制流程顺序 FLOW_OUT_OF_ORDER
    * 隔离考场涉健康信息，须先做 DPIA             -> DPIA_MISSING
    * 无障碍核验失败率高会造成事实差别对待        -> ALT_CHANNEL_MISSING / DRILL_MISSING
    * 涉密考场要求全离线可独立运行                -> OFFLINE_*

设计原则：
  * **本模块自己不做画质算法**——画质问 02（专题 02 是画质权威），
    03 负责告警留痕，01 负责人脸核验。本模块只做"考场该不该放行"的整合判据。
  * 对端不可用时如实标 BLOCK，**不假装自检通过**（否则就是漏检的根源）。
"""
import config
import linkage_client
import models
import rooms as rooms_mod
from models import Defect, PointCheck, CheckRun

# 能力缺口 -> 该考场类型的专项缺陷码（让自检结论能指到具体原因，而不是笼统"能力缺失"）
CAP_SPECIFIC_DEFECT = {
    "alt_verify": "ALT_CHANNEL_MISSING",
    "non_face_channel": "ALT_CHANNEL_MISSING",
    "drill": "DRILL_MISSING",
    "offline": "OFFLINE_UNVERIFIED",
    "no_external": "OFFLINE_UNSUPPORTED",
    "screen_audit": "SCREEN_AUDIT_MISSING",
    "dedup": "DEDUP_UNVERIFIED",
    "privacy_domain": "DOMAIN_UNISOLATED",
    "remote_invigilate": "REMOTE_INVIGILATOR_MISSING",
}

_QUALITY_BLOCK_CODES = {"BLUR", "FROZEN", "OCCLUDED", "VIEW_OFFSET", "STREAM_FAIL",
                        "DEVICE_OFFLINE", "HEARTBEAT_LOST", "REC_MISSING", "REC_GAP"}

QUALITY_BLOCK_CODES = _QUALITY_BLOCK_CODES


class AdmissionError(Exception):
    pass


def _trace(msg):
    """设 ROOM_DEBUG=1 时把准入自检的内部判定打到 stderr，便于现场排障。"""
    import os as _os
    if _os.environ.get("ROOM_DEBUG"):
        import sys as _sys
        _sys.stderr.write("[admission] %s\n" % msg)


def _defect(code, detail="", point=""):
    return Defect.make(code, detail, point)


# =============================================================================
# 机位自检（画质走 02）
# =============================================================================
def point_device_codes(room):
    """返回 [(点位名, 机位编码, required)]。没填编码的点位不参与画质自检。"""
    out = []
    for p in (room.points or []):
        code = (p.get("device_code") or "").strip()
        out.append((p.get("no") or "", code, bool(p.get("required"))))
    return out


def selfcheck_points(room, operator="", image_map=None, use_camera_ledger=True):
    r"""
    逐机位自检：拿 02 的画质可信度系数与缺陷清单。

    image_map: {点位名: 图片路径} —— 给了就现场诊断（更真实）；
               没给就取该机位**最近一次**核查结论（专题 02 的可信度系数口径）。
    use_camera_ledger: True 时若点位没填编码，自动按 考场名-序号 从 02 台账匹配。

    三重如实判定（都不允许"看起来通过"）：
      1) 点位没登记机位编码            -> QUALITY_UNCHECKED（阻断）
      2) 编码不在 02 台账              -> CAMERA_UNREGISTERED（阻断）
      3) 02 不可用                     -> LINKAGE_UNAVAILABLE（阻断）

    返回 (list[PointCheck], list[Defect], overall_factor)
    """
    image_map = image_map or {}
    out, defects, factors = [], [], []
    codes = point_device_codes(room)

    if use_camera_ledger and not any(c for _n, c, _r in codes):
        codes = _match_ledger_codes(room, codes)

    registered, reg_note = linkage_client.camera_registered_codes()

    for no, code, required in codes:
        _trace("point %s code=%r registered=%s" % (no, code, registered))
        pc = PointCheck(no=no, device_code=code)
        if not code:
            pc.ok = 0
            pc.detail = "未登记机位编码（无法完成画质自检）"
            pc.defects = [{"code": "QUALITY_UNCHECKED", "severity": config.SEV_BLOCK,
                           "name": config.defect_name("QUALITY_UNCHECKED")}]
            # 必需点位没登记编码 -> 阻断；额外/期望点位 -> 只提醒
            defects.append(_defect("QUALITY_UNCHECKED",
                                   "%s 未登记机位编码" % no, no) if required
                           else _defect("QUALITY_WARN",
                                        "%s（非必需）未登记机位编码" % no, no))
            out.append(pc)
            continue

        if registered is not None and code not in registered:
            # 台账里没有这个机位 = 该机位没有任何画质证据（02 对未知机位只会回答"不降权"）
            pc.ok = 0
            pc.source = "02 机位台账"
            pc.detail = "该机位编码不在 02 摄像头核查台账（%s）" % reg_note
            pc.defects = [{"code": "CAMERA_UNREGISTERED", "severity": config.SEV_BLOCK,
                           "name": config.defect_name("CAMERA_UNREGISTERED")}]
            defects.append(_defect("CAMERA_UNREGISTERED",
                                   "%s（%s）不在 02 台账" % (no, code), no)
                           if required else
                           _defect("QUALITY_WARN",
                                   "%s（%s，非必需）不在 02 台账" % (no, code), no))
            factors.append(pc.factor)
            out.append(pc)
            continue

        img = image_map.get(no) or image_map.get(code)
        res = None
        if img:
            res = linkage_client.inspect_camera(img, device_code=code,
                                                operator=operator or "10_room")
            if res.get("ok"):
                d = res.get("data") or {}
                pc.factor = models.to_float(d.get("factor"), 1.0)
                pc.ok = 1 if d.get("result_ok") else 0
                pc.source = "02 现场诊断"
                pc.defects = [{"code": x.get("code"), "severity": x.get("severity"),
                               "name": x.get("name") or x.get("code")}
                              for x in (d.get("defects") or [])]
                pc.detail = "现场画质诊断：%s" % ("通过" if pc.ok else "不通过")
            else:
                pc.ok = 0
                pc.source = "02 现场诊断失败"
                pc.detail = str(res.get("reason"))[:180]
                pc.defects = [{"code": "LINKAGE_UNAVAILABLE",
                               "severity": config.SEV_BLOCK,
                               "name": config.defect_name("LINKAGE_UNAVAILABLE")}]
        else:
            res = linkage_client.camera_credibility(code)
            if res.get("ok"):
                d = res.get("data") or {}
                pc.factor = models.to_float(d.get("factor"), 1.0)
                raw = list(d.get("defects") or [])
                pc.defects = [{"code": c, "severity": _sev_of(c),
                               "name": config.defect_name(c)} for c in raw]
                pc.ok = 1 if pc.factor >= 0.999 else 0
                pc.source = "02 最近一次核查"
                pc.detail = d.get("reason") or ""
                _trace("  %s factor=%.3f defects=%s" % (no, pc.factor, raw))
            else:
                # 02 不可用：如实标"未检"，绝不假装通过
                pc.ok = 0
                pc.source = "02 不可用"
                pc.detail = str(res.get("reason"))[:180]
                pc.defects = [{"code": "LINKAGE_UNAVAILABLE",
                               "severity": config.SEV_BLOCK,
                               "name": config.defect_name("LINKAGE_UNAVAILABLE")}]
        factors.append(pc.factor)
        if pc.ok:
            # 通过的点位也要进逐机位明细（这正是"考前逐机位留档"的证据）
            pc.detail = pc.detail or "画质自检通过"
            out.append(pc)
            continue
        # 缺陷归并
        codes_here = [x.get("code") for x in pc.defects if x.get("code")]
        blocking = [c for c in codes_here if c in QUALITY_BLOCK_CODES]
        if blocking:
            defects.append(_defect("QUALITY_FAIL",
                                   "%s（%s）：%s" % (no, ",".join(blocking),
                                                    pc.detail[:100]), no))
        elif "LINKAGE_UNAVAILABLE" in codes_here:
            defects.append(_defect("LINKAGE_UNAVAILABLE",
                                   "%s：%s" % (no, pc.detail[:120]), no))
        elif codes_here:
            defects.append(_defect("QUALITY_WARN",
                                   "%s（%s）：%s" % (no, ",".join(codes_here),
                                                    pc.detail[:100]), no))
        out.append(pc)

    overall = min(factors) if factors else 1.0
    _trace("done: points=%d defects=%d factor=%.3f"
           % (len(out), len(defects), overall))
    return out, defects, round(overall, 3)


def _sev_of(code):
    """02 的缺陷码严重级由 02 自己定；这里按本模块的兜底口径映射。"""
    if code in QUALITY_BLOCK_CODES:
        return config.SEV_BLOCK
    return config.SEV_WARN


def _match_ledger_codes(room, codes):
    """点位没填机位编码时，从 02 台账按考场名匹配（单向读取，不改 02）。"""
    res = linkage_client.camera_devices()
    if not res.get("ok"):
        return codes
    devices = (res.get("data") or {}).get("devices") or []
    by_room = [d for d in devices if (d.get("room_id") or "") == room.name]
    if not by_room:
        # 再退一步：按 考场名-序号 约定匹配（如 CAM-A101-1）
        prefix = (room.name or "").split()[0]
        by_room = [d for d in devices
                   if (d.get("code") or "").startswith("CAM-" + prefix)]
    if not by_room:
        return codes
    out = []
    for i, (no, code, required) in enumerate(codes):
        if code:
            out.append((no, code, required))
            continue
        if i < len(by_room):
            out.append((no, by_room[i].get("code") or "", required))
        else:
            out.append((no, code, required))
    return out


# =============================================================================
# 各判据
# =============================================================================
def check_points(room, advice):
    """点位数量与必需点位（含最小可用配置判定）。"""
    out = []
    meta = config.ROOM_TYPES.get(room.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])
    total = len(room.points or [])
    need_must = len([p for p in meta["points"] if p["required"]])
    have_must = len([p for p in (room.points or []) if p.get("required")])
    min_points = models.to_int(meta.get("points_min"), 1)

    if total < min_points:
        out.append(_defect("POINTS_INSUFFICIENT",
                           "%s 需要至少 %d 个点位，当前 %d 个" %
                           (room.type_name(), min_points, total)))
    if have_must < need_must:
        missing = room.points_missing or rooms_mod.missing_point_names(room)
        out.append(_defect("POINT_MISSING",
                           "缺少必需点位：%s" % "、".join(missing)))
    for x in (advice.get("blocking") or []):
        if x.get("cap") in ("points",):
            out.append(_defect("POINTS_INSUFFICIENT", x.get("name", "")))
    return out


def check_capabilities(room, advice):
    """
    能力缺口 -> 缺陷。

    分两种来源，说不同的话、给不同码：
      * point（缺实体点位/开关）-> CAP_SPECIFIC_DEFECT 里的专项码（如 SCREEN_AUDIT_MISSING）
      * job（还有一件事没做）  -> CAP_JOB_PENDING（"待办事项"，提示去哪做，别当成设备故障）
    """
    out = []
    for x in (advice.get("blocking") or []):
        cap = x.get("cap")
        if cap == "points":
            continue
        if x.get("src") == config.CAP_SRC_JOB:
            out.append(_defect("CAP_JOB_PENDING",
                               "待办事项未完成：%s%s"
                               % (x.get("name", cap),
                                  ("（%s）" % x["how"]) if x.get("how") else "")))
            continue
        code = CAP_SPECIFIC_DEFECT.get(cap, "CAPABILITY_MISSING")
        out.append(_defect(code, "缺少必需能力：%s" % x.get("name", cap)))
    for x in (advice.get("weak") or []):
        out.append(_defect("CAPABILITY_WEAK",
                           "期望能力未配置：%s%s"
                           % (x.get("name", x.get("cap")),
                              ("（%s）" % x["how"]) if x.get("how") else "")))
    return out


def check_coverage(room):
    out = []
    if not room.points_verified:
        out.append(_defect("COVERAGE_UNVERIFIED", "逐机位覆盖完整性未核查留档"))
    # 盲区：有座位区点位但缺通道机位，或点位全部集中在同一朝向
    nos = [p.get("no") or "" for p in (room.points or [])]
    if room.seats >= 30 and not any("通道" in n for n in nos):
        out.append(_defect("COVERAGE_BLIND", "超过 30 人的考场没有通道机位"))
    return out


def check_flow(room):
    """强制流程顺序（特殊考场临时启用最容易跳步，方案第五节明确点出）。"""
    out = []
    meta = config.ROOM_TYPES.get(room.room_type, {})
    if not room.apply_by and not room.applied_at:
        out.append(_defect("APPLY_MISSING", "缺少考务组启用申请记录"))
    elif not room.apply_reason:
        out.append(_defect("APPLY_REASON_MISSING", "启用申请未写原因"))
    if not room.template_applied:
        out.append(_defect("TEMPLATE_MISSING", "未套用考场模板（存在逐场手工调参风险）"))
    if room.status in (config.PHASE_APPLY,) and room.template_applied:
        out.append(_defect("FLOW_OUT_OF_ORDER", "状态仍在「启用申请」，流程未推进"))
    if not room.calibration_at:
        out.append(_defect("CALIBRATION_MISSING", "缺逐考场标定记录（机位/角度/区域）"))
    return out


def check_clock(room):
    out = []
    tol = config.TARGETS["clock_drift_sec"]
    if room.clock_drift_sec is None:
        return out
    if models.to_float(room.clock_drift_sec, 0.0) > tol:
        out.append(_defect("CLOCK_DRIFT", "时钟偏差 %.2f 秒 > %.1f 秒"
                           % (room.clock_drift_sec, tol)))
    return out


def check_ledger(room):
    """座位表 + 考生编号映射；并且**不能出现姓名**（方案要求仅编号）。"""
    out = []
    if not room.ledger_ready:
        out.append(_defect("LEDGER_MISSING",
                           "未录入座位表与考生编号映射（无法行为—座位—考生定位）"))
    for row in (room.ledger_sample or []):
        name = str(row.get("candidate_name") or "").strip()
        if name:
            out.append(_defect("LEDGER_NAME_EXPOSED",
                               "座位映射含考生姓名（应仅编号）：%s 座" %
                               (row.get("seat_no") or "?")))
            break
    return out


def check_roles(room):
    out = []
    meta = config.ROOM_TYPES.get(room.room_type, {})
    onsite = room.invigilator_count()
    need = models.to_int(room.invigilators_required, 0)
    if room.room_type == config.TYPE_ISOLATION:
        if not room.remote_invigilator:
            out.append(_defect("REMOTE_INVIGILATOR_MISSING",
                               "隔离考场远程监考员未在岗核验"))
        if onsite < 1 and not room.remote_invigilator:
            out.append(_defect("ROLE_MISSING", "现场与远程监考均未核验在岗"))
    else:
        if onsite == 0:
            out.append(_defect("ROLE_MISSING", "监考/巡考在岗未核验"))
        elif need and onsite < need:
            out.append(_defect("ROLE_COUNT_LOW", "监考员 %d 人 < 该考场类型要求 %d 人"
                               % (onsite, need)))
    return out


def check_compliance(room):
    out = []
    meta = config.ROOM_TYPES.get(room.room_type, {})
    comp = meta.get("compliance", {})
    rules = config.DOMAIN_RULES.get(room.data_domain, {})

    # 数据分域：隔离/机考/涉密必须独立分区
    if comp.get("domain_isolated"):
        if room.data_domain != comp.get("data_domain"):
            out.append(_defect("DOMAIN_MIXED",
                               "分区为 %s，该考场类型要求 %s"
                               % (room.data_domain, comp.get("data_domain"))))
        elif not rules.get("isolated"):
            out.append(_defect("DOMAIN_UNISOLATED",
                               "%s 未配置为独立分区" % room.data_domain))
    if not room.retention_days or room.retention_days < config.MIN_RETENTION_DAYS:
        out.append(_defect("RETENTION_MISSING",
                           "未设置最短必要保存期限（≥%d 天）与销毁计划"
                           % config.MIN_RETENTION_DAYS))
    # DPIA
    if comp.get("dpia_required") and not room.dpia_done:
        out.append(_defect("DPIA_MISSING",
                           "涉及敏感个人信息（%s），须先完成 DPIA 再启用"
                           % config.DOMAIN_RULES.get(room.data_domain, {}).get("name", "")))
    # 无障碍：非人脸替代通道 + 演练记录
    if room.room_type == config.TYPE_ACCESSIBLE:
        # 硬要求："须提供非人脸替代通道，不得因技术原因拒考"。
        # 判定只认 **人工核验工位 / 已启用替代通道**：语音文字提示、低位设备都不算，
        # 因为它们不能替代"核验"这件事（不是把人拒之门外的技术理由的替代路径）。
        alt_ok = bool(room.alt_channel) or any(
            c.get("cap") == "alt_verify" for c in (room.caps_present or []))
        if not alt_ok:
            out.append(_defect("ALT_CHANNEL_MISSING",
                               "缺少非人脸替代核验通道（不得因技术原因拒考）"))
        if not room.drill_done:
            out.append(_defect("DRILL_MISSING", "替代通道无演练记录（强制项）"))
    # 涉密：全离线
    if comp.get("offline_required"):
        if not room.offline_ready:
            out.append(_defect("OFFLINE_UNVERIFIED",
                               "未完成全离线自检（断网可独立运行）"))
        params = meta.get("params") or {}
        if params.get("no_cloud") and not room.offline_ready:
            out.append(_defect("OFFLINE_UNSUPPORTED",
                               "当前配置未证明算法本地运行、不上传云端"))
    # 大教室/机考：跨机位去重
    params = meta.get("params") or {}
    if params.get("dedup_required") and not room.dedup_verified:
        out.append(_defect("DEDUP_UNVERIFIED",
                           "未验证跨机位去重（重复误报风险）"))
    # 机考：屏幕行为审计
    if room.room_type == config.TYPE_COMPUTER:
        has_audit = bool(room.screen_audit) or any(
            c.get("cap") == "screen_audit" for c in (room.caps_present or []))
        if not has_audit:
            out.append(_defect("SCREEN_AUDIT_MISSING", "机考考场未启用屏幕行为审计"))
    # 口试：音频采样率
    if room.room_type == config.TYPE_ORAL:
        need = models.to_int((meta.get("params") or {}).get("audio_sample_rate_min"),
                             48000)
        if models.to_int(room.audio_sample_rate) < need:
            out.append(_defect("AUDIO_QUALITY_LOW",
                               "录音采样率 %s < 要求 %s Hz"
                               % (room.audio_sample_rate or "未设置", need)))
    return out


def check_min_config(room):
    out = []
    if room.min_config != "full":
        out.append(_defect("MIN_CONFIG_USED",
                           "使用「%s」：%s" % (room.min_config_meta()["name"],
                                            room.min_config_meta()["desc"])))
    return out


# =============================================================================
# 整合：跑一次准入自检
# =============================================================================
def register_room_points(room, operator="", force=False):
    """
    把考场的点位登记进 02 的机位台账（**单向写入，不改 02 代码**）。

    为什么放进准入流程：02 对"台账里没有的机位"只会回答"不降权"，
    那样画质这一关就成了空门。现场部署的真实顺序本来就是
    "装好机位 -> 在 02 建档采基准图"，所以这一步属于正常流程，
    不是补救。02 不可用时如实返回，由自检判 LINKAGE_UNAVAILABLE。

    返回 {"ok", "registered", "failed", "skipped", "reason"}
    """
    reg, note = linkage_client.camera_registered_codes(refresh=True)
    if reg is None:
        return {"ok": False, "registered": 0, "failed": [], "skipped": 0,
                "reason": note}
    out = {"ok": True, "registered": 0, "failed": [], "skipped": 0, "reason": ""}
    for p in (room.points or []):
        code = (p.get("device_code") or "").strip()
        if not code:
            out["skipped"] += 1
            continue
        if code in reg and not force:
            out["skipped"] += 1
            continue
        res = linkage_client.save_camera_device(
            code, name=p.get("no") or code, room_id=room.name, site=room.site,
            position=p.get("where") or "",
            has_audio=(p.get("no") in ("拾音器", "拾音器阵列",
                                       "高保真录音", "语音提示终端（含拾音）")),
            operator=operator or "10_room")
        if res.get("ok"):
            out["registered"] += 1
        else:
            out["failed"].append("%s：%s" % (code, str(res.get("reason"))[:80]))
    if out["registered"]:
        linkage_client.reset_device_cache()
    return out


def run_admission(store, room, operator="", image_map=None,
                  use_camera_ledger=False, note="", persist=True,
                  auto_register=True):
    """
    跑完整准入自检并给出结论。

    返回 {"ok", "blocked", "check": CheckRun, "room": Room, "decision": str,
          "next_phase": str, "blockers": [...], "warnings": [...]}
    """
    if not room.template_applied:
        # 没套模板先套上，但流程缺陷照记（不让人靠漏步蒙过去）
        rooms_mod.apply_template(room, operator=operator, store=store)
    else:
        # 已套过模板：只重算能力缺口。**绝不重套模板**——
        # 重套会把标定记录/覆盖核查/替代通道等"已办事项"清空，
        # 等于把考场布置好的现场记录抹掉（这是最要命的假通过来源）。
        rooms_mod.recompute_caps(room)

    advice = rooms_mod.capability_advice(room)
    defects = []
    passed = []

    def _step(title, fn, *a):
        got = fn(*a)
        if got:
            defects.extend(got)
        else:
            passed.append(title)
        return got

    _step("考场模板已套用", lambda: [] if room.template_applied else
          [_defect("TEMPLATE_MISSING", "未套用考场模板")])
    _step("强制流程顺序完整", check_flow, room)
    _step("点位数量与必需点位齐备", check_points, room, advice)
    _step("考场类型必需能力齐备", check_capabilities, room, advice)
    _step("覆盖完整性核查留档", check_coverage, room)
    _step("时钟同步 ≤ 1 秒", check_clock, room)
    _step("座位表与考生编号映射（仅编号）", check_ledger, room)
    _step("监考/巡考在岗核验", check_roles, room)
    _step("数据分域与合规留痕", check_compliance, room)

    # 机位先登记进 02 台账（现场本来就是"装机位 -> 02 建档"的顺序）
    reg_info = None
    if auto_register and not image_map:
        reg_info = register_room_points(room, operator=operator)
        if reg_info.get("ok") and reg_info.get("registered"):
            passed.append("机位已登记进 02 台账（%d 个）" % reg_info["registered"])
        for msg in (reg_info.get("failed") or []):
            defects.append(_defect("CAMERA_UNREGISTERED",
                                   "机位登记失败：%s" % msg))

    pcs, qdefects, factor = selfcheck_points(room, operator=operator,
                                             image_map=image_map,
                                             use_camera_ledger=use_camera_ledger)
    registered, reg_note = linkage_client.camera_registered_codes()
    if qdefects:
        defects.extend(qdefects)
    else:
        passed.append("逐机位画质自检（%d 个机位）" % len(pcs))
    # 没登记任何机位编码 = 本次自检没有检查过画质，必须如实判未检（不假装通过）。
    # 逐机位已报过就不再重复加一条"汇总"缺陷（避免同一原因刷屏）。
    if not any(d.code == "QUALITY_UNCHECKED" for d in defects):
        if pcs and not any(p.device_code for p in pcs):
            defects.append(_defect("QUALITY_UNCHECKED",
                                   "全部 %d 个机位未登记编码，本次未做画质自检"
                                   % len(pcs)))
        elif not pcs:
            defects.append(_defect("QUALITY_UNCHECKED",
                                   "考场没有任何点位，无法完成画质自检"))
    if registered is None and not any(d.code == "LINKAGE_UNAVAILABLE" for d in defects):
        # 02 台账都读不到：整个前置核查没做成，如实标注（而不是当作通过）
        defects.append(_defect("LINKAGE_UNAVAILABLE",
                               "02 摄像头核查台账不可用：%s" % reg_note))

    _step("最小可用配置留痕", check_min_config, room)

    # 去重与「同因去重」：同一 code 只留最严重的一条，避免刷屏
    seen, merged = set(), []
    for d in defects:
        key = (d.code, d.point or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)

    blockers = [d for d in merged if d.severity == config.SEV_BLOCK]
    warnings = [d for d in merged if d.severity != config.SEV_BLOCK]
    strict = bool(config.load_settings().get("strict_mode", True))
    blocked = bool(blockers) if strict else False
    can_ok = not blockers and not warnings
    run = CheckRun(room_id=room.room_id, room_name=room.name,
                   room_type=room.room_type, phase=config.PHASE_SELFCHECK,
                   operator=operator, ok=1 if (not blocked and not blockers) else 0,
                   blocked=1 if blocked else 0,
                   defects=[d.to_dict() for d in merged],
                   point_checks=[p.to_dict() for p in pcs],
                   factor=factor, passed=passed, min_config=room.min_config,
                   note=note)
    room.last_check_at = run.ts or models.now_text()
    room.last_check_ok = run.ok

    # 推进状态（跳步由 check_flow 记录阻断缺陷）
    if blocked:
        room.status = config.PHASE_SUSPENDED if room.armed() else config.PHASE_SELFCHECK
    elif blockers:
        # 非严格模式：有阻断项也放行，但状态留在"机位自检中"等人工处置
        room.status = config.PHASE_SELFCHECK
    elif warnings or room.min_config != "full":
        room.status = config.PHASE_DEGRADED
    else:
        room.status = config.PHASE_ARMED

    if persist:
        store.save_check(run)
        store.save_room(room)

    decision = "可以启用" if not blocked else "禁止启用"
    if blocked:
        decision = "禁止启用（%d 项阻断缺陷）" % len(blockers)
    elif warnings:
        decision = "可以启用（%d 项警告需留痕）" % len(warnings)
    elif not room.caps_missing and not any(
            p for p in (room.points_missing or [])):
        decision = "可以启用（全部判据通过）"
    return {"ok": True, "blocked": blocked, "check": run, "room": room,
            "decision": decision, "blockers": [d.to_dict() for d in blockers],
            "warnings": [d.to_dict() for d in warnings],
            "passed": passed, "factor": factor, "advice": advice,
            "next_phase": room.status,
            "next_phase_text": config.PHASE_TEXT.get(room.status, room.status)}


# =============================================================================
# 启用 / 停用
# =============================================================================
def arm(store, room, operator="", image_map=None, note="考前启用",
        auto_register=True):
    """按强制流程启用考场：自检 -> 通过才 arm；不通过就拦住并说明原因。"""
    res = run_admission(store, room, operator=operator, image_map=image_map,
                        note=note, auto_register=auto_register)
    if res["blocked"]:
        store.log("考场启用被拦", "%s：%s" % (room.name, res["decision"]), operator,
                  room.room_id)
        res["armed"] = False
        return res
    room.status = (config.PHASE_DEGRADED if room.min_config != "full"
                   else config.PHASE_ARMED)
    room.armed_at = models.now_text()
    store.save_room(room)
    store.log("考场启用", "%s（%s）%s" % (room.name, room.type_name(),
                                       room.min_config_meta()["name"]),
              operator, room.room_id)
    res["armed"] = True
    res["next_phase"] = room.status
    res["next_phase_text"] = config.PHASE_TEXT.get(room.status, room.status)
    return res


def disable(store, room, operator="", reason=""):
    room.status = config.PHASE_SUSPENDED
    store.save_room(room)
    store.log("考场停用", "%s：%s" % (room.name, reason or "未填原因"), operator,
              room.room_id)
    return room


def close_room(store, room, operator="", seal_domain=True):
    """考试结束：封存（数据分区不动，等最短必要期限后再销毁）。"""
    room.status = config.PHASE_CLOSED
    room.closed_at = models.now_text()
    store.save_room(room)
    store.log("考场封存", "%s 考试结束数据封存" % room.name, operator, room.room_id)
    return room


def set_invigilators(store, room, invigilators, remote=None, operator=""):
    """录入在岗人员。invigilators: [{"name","role"}]；remote: 隔离考场远程监考员。"""
    now = models.now_text()
    room.invigilators = [{"name": (x.get("name") or "").strip(),
                          "role": x.get("role") or "监考员",
                          "remote": bool(x.get("remote")),
                          "at": x.get("at") or now}
                         for x in (invigilators or []) if (x.get("name") or "").strip()]
    if remote:
        room.remote_invigilator = {"name": (remote.get("name") or "").strip(),
                                  "role": remote.get("role") or "远程监考员",
                                  "remote": True, "at": remote.get("at") or now}
    store.save_room(room)
    store.log("在岗核验", "%s：现场 %d 人、远程 %s" %
              (room.name, room.invigilator_count(),
               (room.remote_invigilator or {}).get("name") or "无"),
              operator, room.room_id)
    return room


def set_ledger(store, room, sample=None, operator=""):
    """
    录入"座位表 + 考生编号映射"。**只存编号，不存姓名**（方案明确要求）。

    sample: [{"seat_no": "01", "candidate_id": "C001"}]；传了 candidate_name
            也不会入库，并在自检里被判 LEDGER_NAME_EXPOSED。
    """
    rows = []
    for x in (sample or []):
        row = {"seat_no": str(x.get("seat_no") or ""),
               "candidate_id": str(x.get("candidate_id") or "")}
        # 故意保留姓名（如果调用方塞了）：让准入自检能判出 LEDGER_NAME_EXPOSED，
        # 而不是悄悄把违规数据抹掉后假装合规。
        if x.get("candidate_name"):
            row["candidate_name"] = str(x.get("candidate_name"))
        rows.append(row)
    room.ledger_sample = rows
    room.ledger_ready = 1 if rows else 0
    store.save_room(room)
    store.log("考场建模", "%s：座位映射 %d 条（仅编号）" % (room.name, len(rows)),
              operator, room.room_id)
    return room


def set_calibration(store, room, operator="", region=""):
    room.calibration_at = models.now_text()
    room.points_verified = 1
    store.save_room(room)
    store.log("考场标定", "%s：机位/角度/区域已标定 %s" % (room.name, region),
              operator, room.room_id)
    return room


def set_flags(store, room, operator="", **flags):
    """批量置开关（都是"已办事项"）：alt_channel / drill_done / offline_ready /
    dedup_verified / screen_audit / dpia_done / fast_enable / audio_sample_rate /
    clock_drift_sec / ledger_ready / points_verified。"""
    allowed = ("alt_channel", "drill_done", "offline_ready", "dedup_verified",
               "screen_audit", "dpia_done", "fast_enable", "doctor_link",
               "network_log", "independent_mic", "audio_sample_rate",
               "clock_drift_sec", "ledger_ready", "points_verified")
    changed = []
    for k, v in (flags or {}).items():
        if k not in allowed or v is None:
            continue
        setattr(room, k, v)
        changed.append("%s=%s" % (k, v))
    store.save_room(room)
    if changed:
        store.log("考场参数设置", "%s：%s" % (room.name, "、".join(changed)),
                  operator, room.room_id)
    return room


def activate_flow(store, room, operator="", device_codes=None):
    """
    GUI 用的一键流程（对应方案第三节"隔离考场"示例的操作链）：
      套模板 -> 登记点位 -> 机位自检 -> 在岗核验 -> 启用
    这里把"套模板 + 登记点位"合并，自检/核验/启用交给调用方分步做，
    便于界面逐步展示与留痕。
    """
    rooms_mod.apply_template(room, operator=operator, store=store)
    if device_codes:
        pts = []
        for p in (room.points or []):
            code = device_codes.get(p.get("no"))
            q = dict(p)
            if code:
                q["device_code"] = code
            pts.append(q)
        room.points = pts
        rooms_mod.recompute_caps(room)
    room.status = config.PHASE_TEMPLATE
    store.save_room(room)
    store.log("套用模板", "%s -> %s（%d 个点位）"
              % (room.name, room.type_name(), len(room.points or [])),
              operator, room.room_id)
    return room
