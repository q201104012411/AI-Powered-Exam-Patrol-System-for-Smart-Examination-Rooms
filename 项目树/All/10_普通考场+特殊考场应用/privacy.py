# -*- coding: utf-8 -*-
r"""
数据分域与合规留痕（《特殊考场应用.md》第二节第 5 条 + 第四节验收差异项 + 第五节风险）

方案原文要求：
  * 数据分域：隔离考场（健康信息）、机考考场（作答内容）、涉密考场（涉密数据）
    **单独分区存储与授权，禁止跨域汇聚**
  * 隔离考场健康信息属敏感个人信息：单独存储、更高保护等级、不与其他数据混库
  * 隔离考场同时涉及健康信息与考生身份信息 -> 需做 **DPIA** 后再启用
  * 涉密：数据不出域；专项审计与保密审批
  * 无障碍：替代核验通道强制具备并有**演练记录**
  * 按最短必要期限销毁并出具**销毁记录**

本模块把这些落成四类可执行动作：
  1) 分区建立（物理目录 + 元数据 + 独立授权清单）
  2) 跨域访问拦截（默认拒绝；系统角色 + 显式授权 + 未销毁）
  3) 合规动作留痕（DPIA / 演练 / 授权 / 撤权 / 销毁 / 封存 / 离线自检）
  4) 合规体检（哪些考场还不满足强制项）
"""
import os

import config
import models
from models import Grant, Partition, PrivacyRecord

# 隔离域（禁止跨域汇聚 / 需独立授权）
ISOLATED_DOMAINS = tuple(d for d in config.DOMAINS
                         if config.DOMAIN_RULES[d].get("isolated"))


class PrivacyError(Exception):
    pass


# =============================================================================
# 分区
# =============================================================================
def partition_path(domain, room_id):
    """分区的物理落点：<数据根>/domains/<域>/<考场号>/"""
    return os.path.join(str(config.DOMAIN_DIR), domain, room_id)


def domain_dir(domain):
    return os.path.join(str(config.DOMAIN_DIR), domain)


def partition_file(domain, room_id, name="records.jsonl"):
    return os.path.join(partition_path(domain, room_id), name)


def ensure_partition(store, room, operator="", note=""):
    """
    为考场建立数据分区（幂等）。

    规则：该考场类型的合规域 + 是否隔离 + 是否强制加密 + 最短必要保存期限。
    隔离域建目录时同时写 .isolated 标记（物理上就与常规域分开）。
    """
    meta = config.ROOM_TYPES.get(room.room_type, {})
    comp = meta.get("compliance", {})
    domain = comp.get("data_domain") or room.data_domain or config.DOMAIN_EXAM
    rules = config.DOMAIN_RULES.get(domain, config.DOMAIN_RULES[config.DOMAIN_EXAM])
    room.data_domain = domain

    p = store.get_partition(domain, room.room_id)
    if p is None:
        p = Partition(domain=domain, room_id=room.room_id, room_name=room.name,
                      state=config.PARTITION_PENDING,
                      isolated=1 if rules.get("isolated") else 0,
                      encrypted=1 if rules.get("encrypt_fields") else 0,
                      path=partition_path(domain, room.room_id),
                      access_roles=list(rules.get("roles") or []),
                      retention_days=models.to_int(
                          comp.get("min_retention_days"), config.DEFAULT_RETENTION_DAYS),
                      note=note or rules.get("desc", ""))
    else:
        p.room_name = room.name
        p.isolated = 1 if rules.get("isolated") else 0
        p.encrypted = 1 if rules.get("encrypt_fields") else 0
        p.path = partition_path(domain, room.room_id)
        p.access_roles = list(rules.get("roles") or [])
        p.retention_days = models.to_int(comp.get("min_retention_days"),
                                         p.retention_days)
        p.note = note or p.note

    # 物理落点
    try:
        os.makedirs(p.path, exist_ok=True)
        if p.isolated:
            marker = os.path.join(p.path, ".isolated")
            if not os.path.exists(marker):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("isolated domain: %s（禁止跨域汇聚；来源考场 %s）\n"
                            % (domain, room.name))
    except OSError as e:
        p.note = (p.note + "；" if p.note else "") + "目录创建失败：%s" % str(e)[:100]

    p.state = config.PARTITION_ACTIVE
    p.destroy_at = _destroy_deadline(p.retention_days)
    store.save_partition(p)

    # 授权清单里声明的角色，逐个落一条独立授权（方案要求"独立授权"）
    for role in (rules.get("roles") or []):
        if not store.role_authorized(domain, role):
            store.save_grant(Grant(domain=domain, role=role,
                                   operator=_sys_operator(role),
                                   granted_by=operator or "system",
                                   reason="分区建立时按域规则授予（独立授权）"))
    store.log("建立数据分区", "%s -> %s（%s）"
              % (room.name, rules.get("name", domain),
                 "独立分区" if p.isolated else "常规域"),
              operator, room.room_id)
    return p


def _destroy_deadline(retention_days):
    if not retention_days:
        return ""
    import datetime
    d = datetime.datetime.now() + datetime.timedelta(days=models.to_int(retention_days))
    return d.strftime("%Y-%m-%d")


def _sys_operator(role):
    return {"owner": "所有者", "admin": "系统管理员", "patrol": "巡考员",
            "commander": "指挥中心", "doctor": "考点医生",
            "remote_invigilator": "远程监考员", "network_admin": "网络管理员",
            "security_officer": "保密员"}.get(role, role)


def open_partitions(store):
    return [p for p in store.list_partitions()
            if p.state == config.PARTITION_ACTIVE and not p.destroyed_at]


# =============================================================================
# 跨域访问拦截
# =============================================================================
def can_access(store, domain, role, room_id=None):
    """
    默认拒绝：
      1) 域必须存在且分区未销毁
      2) 隔离域必须有**独立授权**（不能只靠角色名里的系统权限）
      3) 非隔离域按域规则里的角色放行
    返回 (bool, 说明)
    """
    rules = config.DOMAIN_RULES.get(domain)
    if not rules:
        return False, "未知数据域：%s" % domain
    if room_id:
        p = store.get_partition(domain, room_id)
        if p is None:
            return False, "该考场在 %s 没有分区" % rules["name"]
        if p.destroyed_at:
            return False, "该分区已于 %s 销毁（%s）" % (p.destroyed_at,
                                                    p.destroy_operator or "未记操作人")
    if role in (rules.get("roles") or []):
        if rules.get("isolated"):
            ok = store.role_authorized(domain, role)
            if not ok:
                return False, "隔离域 %s 未对角色「%s」独立授权" % (rules["name"], role)
            return True, "已独立授权"
        return True, "域规则允许"
    if store.role_authorized(domain, role):
        return True, "已单独授权"
    return False, "角色「%s」不在 %s 的授权范围（默认拒绝）" % (role, rules["name"])


def check_access(store, domain, role, room_id=None, operator="", action="read",
                 audit=True):
    """带留痕的访问校验：拒绝也要留痕（方案要求可审计）。"""
    ok, why = can_access(store, domain, role, room_id=room_id)
    if audit:
        store.log("分区访问%s" % ("通过" if ok else "被拒"),
                  "%s / 角色 %s / 动作 %s：%s" % (domain, role, action, why),
                  operator or role, room_id or "")
    if not ok:
        store.save_privacy(PrivacyRecord(
            action="access_denied", room_id=room_id or "", domain=domain,
            operator=operator or role, result="denied",
            detail="跨域访问被拒（%s / %s）：%s" % (domain, role, why)))
    return ok, why


def write_record(store, room, payload, operator="", role="system", kind="event"):
    """
    往考场的分区写一条业务明细（物理隔离落盘 + 台账留痕）。

    跨域访问会被拦住（这正是"禁止跨域汇聚"的可执行形式）。
    """
    p = store.get_partition(room.data_domain, room.room_id)
    if p is None:
        p = ensure_partition(store, room, operator=operator)
    ok, why = check_access(store, room.data_domain, role, room_id=room.room_id,
                           operator=operator, action="write:%s" % kind)
    if not ok:
        return {"ok": False, "reason": why}
    path = partition_file(room.data_domain, room.room_id)
    line = models.jdump({"ts": models.now_text(), "kind": kind,
                         "room": room.name, "operator": operator,
                         "payload": payload})
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        return {"ok": False, "reason": "分区写入失败：%s" % str(e)[:120]}
    return {"ok": True, "path": path, "domain": room.data_domain}


def read_records(store, room, role="admin", operator="", limit=100):
    ok, why = check_access(store, room.data_domain, role, room_id=room.room_id,
                           operator=operator, action="read")
    if not ok:
        return {"ok": False, "reason": why, "records": []}
    path = partition_file(room.data_domain, room.room_id)
    if not os.path.isfile(path):
        return {"ok": True, "records": [], "path": path}
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(models.jload(line, {}))
    except OSError as e:
        return {"ok": False, "reason": str(e)[:120], "records": []}
    return {"ok": True, "records": out[-int(limit):], "path": path,
            "total": len(out)}


# =============================================================================
# 合规动作
# =============================================================================
def do_dpia(store, room, operator="", assessor="", conclusion="", witness=""):
    """
    DPIA：隔离/机考/涉密启用前必须做。
    离线环境里没人替我们做评估，所以这里**要求填评估人**，避免自评自批。
    """
    if not (assessor or "").strip():
        return False, "DPIA 必须记评估人（不接受空评估人）"
    if assessor.strip() == (operator or "").strip() and operator:
        return False, "评估人不能与操作人相同（避免自评自批）"
    if not (conclusion or "").strip():
        return False, "DPIA 必须写结论（风险点与缓解措施）"
    room.dpia_done = 1
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="dpia", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, witness=assessor,
        result="done", detail="个人信息保护影响评估：%s" % conclusion[:300]))
    store.log("完成 DPIA", "%s（评估人 %s）：%s"
              % (room.name, assessor, conclusion[:120]), operator, room.room_id)
    return True, "DPIA 已留痕（评估人 %s）" % assessor


def do_drill(store, room, operator="", witness="", detail=""):
    """
    无障碍替代核验通道**演练记录**（方案第四节：强制具备并有演练记录）。
    与 06 的"双人见证"同款口径：主操作人与见证人不能是同一人。
    """
    if room.room_type != config.TYPE_ACCESSIBLE:
        return False, "只有无障碍/特殊需求考场需要替代通道演练记录"
    if not (witness or "").strip():
        return False, "演练必须记见证人（双人在场）"
    if witness.strip() == (operator or "").strip():
        return False, "见证人不能与操作人相同"
    room.drill_done = 1
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="drill", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, witness=witness,
        result="done", detail="非人脸替代核验通道演练：%s" % (detail or "按预案完成")))
    store.log("替代通道演练", "%s（操作 %s / 见证 %s）" % (room.name, operator, witness),
              operator, room.room_id)
    return True, "演练记录已留痕"


def do_offline_check(store, room, operator="", offline=True, note=""):
    """涉密考场全离线自检（断网可独立运行）。"""
    room.offline_ready = 1 if offline else 0
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="offline", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, result="pass" if offline else "fail",
        detail="全离线自检：%s %s" % ("通过" if offline else "不通过", note or "")))
    store.log("全离线自检", "%s：%s" % (room.name, "通过" if offline else "不通过"),
              operator, room.room_id)
    return True, "全离线自检%s" % ("通过" if offline else "不通过")


def grant(store, domain, role, operator="", granted_by="", reason=""):
    if domain not in config.DOMAIN_RULES:
        return False, "未知数据域：%s" % domain
    if not (role or "").strip():
        return False, "角色不能为空"
    g = Grant(domain=domain, role=role.strip(), operator=operator,
              granted_by=granted_by or operator, reason=reason or "手动独立授权")
    store.save_grant(g)
    store.save_privacy(PrivacyRecord(action="grant", domain=domain,
                                     operator=operator, result="granted",
                                     detail="分区独立授权：%s / %s %s"
                                            % (domain, role, reason or "")))
    store.log("分区授权", "%s -> %s（%s）" % (domain, role, reason or ""), operator)
    return True, g.to_dict()


def revoke(store, domain, role, operator="", reason=""):
    rows = [g for g in store.list_grants(domain=domain, active_only=True)
            if g.role == role]
    if not rows:
        return False, "该域/角色没有有效授权，无需撤权"
    for g in rows:
        store.revoke_grant(g.grant_id)
    store.save_privacy(PrivacyRecord(action="revoke", domain=domain, room_id="",
                                     operator=operator, result="revoked",
                                     detail="分区撤权：%s / %s %s"
                                            % (domain, role, reason or "")))
    store.log("分区撤权", "%s / %s（%s）" % (domain, role, reason or ""), operator)
    return True, "已撤权 %d 条" % len(rows)


def set_retention(store, room, days, operator="", note=""):
    d = models.to_int(days, 0)
    if d < config.MIN_RETENTION_DAYS:
        return False, "最短必要期限不得少于 %d 天" % config.MIN_RETENTION_DAYS
    room.retention_days = d
    store.save_room(room)
    p = store.get_partition(room.data_domain, room.room_id)
    if p:
        p.retention_days = d
        p.destroy_at = _destroy_deadline(d)
        store.save_partition(p)
    store.save_privacy(PrivacyRecord(
        action="retention", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, result="set",
        detail="最短必要保存期限 %d 天，到期日 %s %s"
               % (d, _destroy_deadline(d), note or "")))
    store.log("设置保存期限", "%s：%d 天" % (room.name, d), operator, room.room_id)
    return True, "已设置 %d 天（到期 %s）" % (d, _destroy_deadline(d))


def seal(store, room, operator="", note=""):
    """考试结束数据封存（分区保留，等最短必要期限）。"""
    p = store.get_partition(room.data_domain, room.room_id)
    if p is None:
        return False, "该考场没有数据分区"
    store.save_privacy(PrivacyRecord(
        action="seal", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, result="sealed",
        detail="考试结束数据封存：%s（到期销毁 %s）%s"
               % (room.name, p.destroy_at, note or "")))
    store.log("数据封存", "%s（域 %s，到期 %s）" % (room.name, room.data_domain,
                                              p.destroy_at),
              operator, room.room_id)
    return True, "已封存（到期销毁 %s）" % p.destroy_at


def destroy(store, room, operator="", witness="", reason=""):
    """
    到期销毁并出具销毁记录（方案第三节末句）。
    销毁必须：记操作人 + 记见证人 + 不与操作人相同 + 写理由。
    """
    p = store.get_partition(room.data_domain, room.room_id)
    if p is None:
        return False, "该考场没有数据分区"
    if p.destroyed_at:
        return False, "该分区已于 %s 销毁过" % p.destroyed_at
    if not (operator or "").strip():
        return False, "销毁必须记操作人"
    if not (witness or "").strip():
        return False, "销毁必须记见证人（双人在场）"
    if witness.strip() == operator.strip():
        return False, "见证人不能与操作人相同"
    if not (reason or "").strip():
        return False, "销毁必须写理由（最短必要期限已到等）"

    removed = 0
    path = p.path or partition_path(p.domain, p.room_id)
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
            for name in _dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
        try:
            os.rmdir(path)
        except OSError:
            pass
    p.destroyed_at = models.now_text()
    p.destroy_operator = operator
    p.state = "destroyed"
    store.save_partition(p)
    room.status = config.PHASE_CLOSED
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="destroy", room_id=room.room_id, room_name=room.name,
        domain=p.domain, operator=operator, witness=witness, result="destroyed",
        detail="销毁记录：%s（删除 %d 个文件）理由：%s"
               % (path, removed, reason)))
    store.log("数据销毁", "%s（域 %s，删除 %d 文件，操作 %s / 见证 %s）"
              % (room.name, p.domain, removed, operator, witness),
              operator, room.room_id)
    return True, {"path": path, "files_removed": removed,
                  "destroyed_at": p.destroyed_at,
                  "certificate": "销毁记录：%s %s 操作人 %s 见证人 %s 理由 %s"
                                 % (models.now_text(), room.name, operator,
                                    witness, reason)}


def do_doctor_link(store, room, operator="", doctor="", note=""):
    """
    隔离考场：登记"医生协同通道"已建立（现场或远程）。

    方案里隔离考场允许"减少现场人员"，所以医生协同必须是**已打通**的一条通道，
    否则考生突发健康状况时没人能进场。这一步属于"已办事项"，做完才算能力齐备。
    """
    if room.room_type != config.TYPE_ISOLATION:
        return False, "只有隔离考场需要登记医生协同通道"
    if not (doctor or "").strip():
        return False, "必须登记对接医生/科室（不接受空值，出事找不到人）"
    room.doctor_link = 1
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="doctor", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, witness=doctor,
        result="linked", detail="医生协同通道已建立：%s %s" % (doctor, note or "")))
    store.log("医生协同登记", "%s：对接 %s" % (room.name, doctor), operator,
              room.room_id)
    return True, "医生协同通道已登记（%s）" % doctor


def do_network_log(store, room, operator="", agent="", note=""):
    """机考考场：登记网络行为日志代理（异常外联要有日志可查）。"""
    if room.room_type != config.TYPE_COMPUTER:
        return False, "只有机考/计算机化考场需要登记网络行为日志代理"
    if not (agent or "").strip():
        return False, "必须登记日志代理名称/地址"
    room.network_log = 1
    room.screen_audit = 1              # 屏幕行为审计与网络日志通常同源启用
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="network_log", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, witness=agent,
        result="enabled", detail="网络行为日志代理：%s %s" % (agent, note or "")))
    store.log("网络日志登记", "%s：代理 %s" % (room.name, agent), operator,
              room.room_id)
    return True, "网络行为日志已登记（%s）" % agent


def do_fast_enable(store, room, operator="", note=""):
    """备用考场：登记"设备预置、即插即用"已就绪（临时启用不停巡）。"""
    if room.room_type != config.TYPE_BACKUP:
        return False, "只有备用考场需要登记即插即用预置"
    room.fast_enable = 1
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="fast_enable", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, result="ready",
        detail="备用考场设备预置就绪：%s" % (note or "机位/链路已预置并验证")))
    store.log("备用考场预置", "%s：即插即用已就绪" % room.name, operator, room.room_id)
    return True, "已登记即插即用预置就绪"


def do_hifi_audio(store, room, operator="", rate=48000, note=""):
    """口试考场：登记高保真录音（≥48kHz）与独立拾音已就绪。"""
    if room.room_type != config.TYPE_ORAL:
        return False, "只有口试/面试考场需要登记高保真录音"
    need = models.to_int((config.ROOM_TYPES[config.TYPE_ORAL].get("params") or {})
                         .get("audio_sample_rate_min"), 48000)
    r = models.to_int(rate, 0)
    if r < need:
        return False, "采样率 %s 低于要求 %s Hz" % (rate, need)
    room.audio_sample_rate = r
    room.independent_mic = 1
    store.save_room(room)
    store.save_privacy(PrivacyRecord(
        action="hifi_audio", room_id=room.room_id, room_name=room.name,
        domain=room.data_domain, operator=operator, result="%dHz" % r,
        detail="高保真录音与独立拾音已就绪：%d Hz %s" % (r, note or "")))
    store.log("录音链路登记", "%s：%d Hz" % (room.name, r), operator, room.room_id)
    return True, "高保真录音与独立拾音已登记（%d Hz）" % r


def overdue_destroy(store, as_of=None):
    """扫出到期应销毁的分区（只列清单，销毁仍需双人操作）。"""
    import datetime
    today = (as_of or datetime.date.today()).strftime("%Y-%m-%d")
    out = []
    for p in store.list_partitions():
        if p.destroyed_at or not p.destroy_at:
            continue
        if p.destroy_at <= today:
            out.append(p)
    return out


# =============================================================================
# 合规体检
# =============================================================================
def compliance_report(store, site=None):
    """
    逐考场合规体检：方案第四节"差异要求"列的强制项，谁还不满足。
    """
    rooms = store.list_rooms(site=site, limit=2000)
    rows, fail = [], []
    for room in rooms:
        meta = config.ROOM_TYPES.get(room.room_type, {})
        comp = meta.get("compliance", {})
        p = store.get_partition(room.data_domain, room.room_id)
        checks = []
        if comp.get("domain_isolated"):
            ok = bool(p and p.isolated and not p.destroyed_at)
            checks.append(("数据分域隔离", ok, "强制独立分区 + 独立授权"))
        if comp.get("dpia_required"):
            checks.append(("DPIA", bool(room.dpia_done), "敏感个人信息启用前评估"))
        if comp.get("drill_required"):
            checks.append(("替代通道演练", bool(room.drill_done), "强制有演练记录"))
        if comp.get("offline_required"):
            checks.append(("全离线可用", bool(room.offline_ready), "断网可独立运行"))
        if room.room_type == config.TYPE_ACCESSIBLE:
            checks.append(("非人脸替代核验", bool(room.alt_channel), "不得因技术原因拒考"))
        checks.append(("最短必要期限", models.to_int(room.retention_days) >=
                       config.MIN_RETENTION_DAYS,
                       "≥%d 天" % config.MIN_RETENTION_DAYS))
        bad = [c for c in checks if not c[1]]
        row = {"room_id": room.room_id, "name": room.name,
               "room_type": room.room_type, "room_type_name": room.type_name(),
               "domain": room.data_domain,
               "domain_name": config.DOMAIN_RULES.get(room.data_domain, {}).get("name", ""),
               "checks": [{"name": c[0], "ok": c[1], "require": c[2]} for c in checks],
               "ok": not bad, "fail": [c[0] for c in bad],
               "partition": p.to_dict() if p else None,
               "destroy_at": (p.destroy_at if p else "")}
        rows.append(row)
        if bad:
            fail.append(row)
    return {"total": len(rows), "ok": len(rows) - len(fail), "fail": fail,
            "rows": rows,
            "isolated_domains": list(ISOLATED_DOMAINS),
            "domains": {d: config.DOMAIN_RULES[d] for d in config.DOMAINS}}


def domain_matrix(store):
    """域 x 角色 授权矩阵（界面展示"独立授权"用）。"""
    out = {}
    for d in config.DOMAINS:
        rules = config.DOMAIN_RULES[d]
        rows = []
        for role in sorted(set(list(rules.get("roles") or []) +
                               [g.role for g in store.list_grants(domain=d,
                                                                 active_only=False)])):
            granted = store.role_authorized(d, role)
            allowed = (role in (rules.get("roles") or []))
            rows.append({"role": role, "role_text": _sys_operator(role),
                         "by_rule": allowed, "granted": granted,
                         "effective": bool(granted) if rules.get("isolated")
                         else bool(allowed or granted)})
        out[d] = {"name": rules["name"], "isolated": bool(rules.get("isolated")),
                  "sensitivity": rules.get("sensitivity", ""),
                  "allow_cross_aggregate": bool(rules.get("allow_cross_aggregate")),
                  "desc": rules.get("desc", ""), "roles": rows}
    return out


def partition_view(store):
    """分区总览（含物理落点、是否隔离、到期日、销毁状态）。"""
    out = []
    for p in store.list_partitions():
        d = p.to_dict()
        d["exists"] = os.path.isdir(p.path) if p.path else False
        d["files"] = _count_files(p.path) if p.path else 0
        out.append(d)
    return out


def _count_files(path):
    n = 0
    try:
        for _root, _dirs, files in os.walk(path):
            n += len(files)
    except OSError:
        return 0
    return n
