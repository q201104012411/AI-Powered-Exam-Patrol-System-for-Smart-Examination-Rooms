# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 手机集中保管（智能储物柜）

考生手机统一存放、取件留痕 —— 这是"保管闭环"，也是**手机丢失纠纷**的唯一依据。

三件事：
  1) 柜体/柜格台账：登记柜体（容量自动生成柜格），柜格状态可查
  2) 存/取留痕：谁、什么时候、用哪种方式（人脸/扫码/证件/应急）开了哪个格，
     存的是什么机型，全部写 locker_logs，且与安检过检记录（event_id）关联
  3) 异常：超时未取（考后集中取件）、柜门异常、管理员应急开柜（须写明理由）

纪律：
  · 开柜人必须与柜内登记考生一致，否则**拒绝人脸/扫码开柜**，只能管理员应急开柜；
  · 应急开柜必须写理由并留痕（双人在场由 report 体现，界面提示）；
  · 本模块只记录"手机型号/数量"这类**物品信息**，不采集手机内容。
"""
import config
import models


# ---------------- 柜体与柜格 ----------------
def add_locker(store, code, name="", site="", cells_total=20, address="",
               proto="manual", operator="", note=""):
    """登记一台储物柜并自动生成柜格。返回 (ok, Locker|原因)"""
    code = (code or "").strip()
    if not code:
        return False, "柜体编码必填"
    if store.get_locker(code=code):
        return False, "柜体编码已存在：%s" % code
    try:
        total = int(cells_total)
    except (TypeError, ValueError):
        return False, "柜格数量必须是整数"
    if total <= 0 or total > 500:
        return False, "柜格数量应在 1~500 之间（当前 %s）" % cells_total
    lk = models.Locker(code=code, name=name or code, site=site, cells_total=total,
                       address=address, proto=proto, note=note)
    store.save_locker(lk)
    n = store.ensure_cells(lk)
    store.log("登记储物柜", "%s（%d 格，新建 %d）" % (code, total, n), operator or "system")
    return True, lk


def cell_map(store, locker_code, state=None):
    """柜格列表（界面画格位图用）。"""
    return store.list_cells(locker_code=locker_code, state=state)


def _pick_free_cell(store, locker_code, prefer_no=None):
    cells = store.list_cells(locker_code=locker_code, state=config.CELL_FREE)
    if not cells:
        return None
    if prefer_no:
        for c in cells:
            if c.cell_no == prefer_no:
                return c
    return cells[0]


def store_device(store, candidate_id="", candidate_name="", device_desc="",
                 locker_code="", cell_no="", ticket_no="", seat_no="",
                 event_id="", method="manual", operator="", note=""):
    """
    存入手机：自动分配空闲柜格（也可指定 cell_no）并留痕。
    返回 (ok, Cell|原因)
    """
    lockers = store.list_lockers()
    if not lockers:
        return False, "还没有登记储物柜（先到「手机保管柜」页登记一台）"
    lk = store.get_locker(code=locker_code) if locker_code else lockers[0]
    if lk is None:
        return False, "柜体不存在：%s" % locker_code
    if not lk.enabled:
        return False, "柜体已停用：%s" % lk.code
    cand = (candidate_id or "").strip()
    if not cand:
        return False, "存入必须绑定考生（身份证/准考证号），否则无法追溯"
    # 同一考生不允许占用两格（否则"手机是不是存了"说不清）
    already = store.find_cells_by_candidate(cand)
    if already:
        return False, "该考生已有未取柜格：%s（%s）" % (already[0].cell_no,
                                                  already[0].device_desc or "已存物品")
    cell = _pick_free_cell(store, lk.code, prefer_no=cell_no)
    if cell is None:
        return False, "柜体 %s 已无空闲柜格（共 %d 格）" % (lk.code, lk.cells_total)
    cell.state = config.CELL_OCCUPIED
    cell.candidate_id = cand
    cell.candidate_name = candidate_name or ""
    cell.ticket_no = ticket_no or ""
    cell.seat_no = str(seat_no or "")
    cell.device_desc = device_desc or "手机"
    cell.event_id = event_id or ""
    cell.stored_at = models.now_ts()
    cell.stored_epoch = models.now_epoch()
    cell.taken_at = ""
    cell.taken_epoch = 0.0
    cell.method = method
    cell.overdue_notified = False
    store.save_cell(cell)
    store.log_locker(models.LockerLog(
        locker_code=lk.code, cell_no=cell.cell_no, action="store", method=method,
        operator=operator, candidate_id=cand, candidate_name=cell.candidate_name,
        device_desc=cell.device_desc, note=note or "存入手机"))
    store.log("存入手机", "%s %s -> %s/%s" % (cand, cell.device_desc, lk.code,
                                          cell.cell_no), operator or "system")
    return True, cell


def take_device(store, locker_code="", cell_no="", candidate_id="", method="face",
                device_desc="", operator="", note=""):
    """
    取走手机：校验开柜人与柜内登记考生一致，然后释放柜格并留痕。
    返回 (ok, Cell|原因)
    """
    cell = None
    if cell_no:
        lockers = [store.get_locker(code=locker_code)] if locker_code else store.list_lockers()
        for lk in lockers:
            if lk is None:
                continue
            c = store.get_cell(lk.code, cell_no)
            if c and c.state == config.CELL_OCCUPIED:
                cell = c
                break
    elif candidate_id:
        cells = store.find_cells_by_candidate(candidate_id)
        if cells:
            cell = cells[0]
    if cell is None:
        return False, "没找到已存手机的柜格（柜号/考生号对一下）"
    cand = (candidate_id or "").strip()
    if method != "manual" and cand and cell.candidate_id and cand != cell.candidate_id:
        return False, ("开柜人与柜内登记考生不一致（柜内 %s，来人 %s）："
                       "须管理员用「应急开柜」并写明理由"
                       % (config.mask_id(cell.candidate_id), config.mask_id(cand)))
    cell.state = config.CELL_FREE
    cell.taken_at = models.now_ts()
    cell.taken_epoch = models.now_epoch()
    cell.method = method
    store.save_cell(cell)
    store.log_locker(models.LockerLog(
        locker_code=cell.locker_code, cell_no=cell.cell_no, action="take", method=method,
        operator=operator, candidate_id=cell.candidate_id,
        candidate_name=cell.candidate_name,
        device_desc=device_desc or cell.device_desc,
        note=note or "取走手机（开柜方式：%s）" % config.OPEN_METHODS.get(method, method)))
    # 释放柜格后清空占用信息，但**留痕不删**（日志里能查回来）
    freed = "%s|%s" % (cell.candidate_id, cell.device_desc)
    cell.candidate_id = ""
    cell.candidate_name = ""
    cell.ticket_no = ""
    cell.seat_no = ""
    cell.device_desc = ""
    cell.event_id = ""
    cell.stored_at = ""
    cell.stored_epoch = 0.0
    cell.overdue_notified = False
    cell.note = "最近一次占用：%s（%s 取走）" % (config.mask_id(freed.split("|")[0]),
                                           config.OPEN_METHODS.get(method, method))
    store.save_cell(cell)
    store.log("取走手机", "%s -> %s/%s（%s）" % (config.mask_id(freed.split("|")[0]),
                                          cell.locker_code, cell.cell_no,
                                          config.OPEN_METHODS.get(method, method)),
              operator or "system")
    return True, cell


def emergency_open(store, locker_code, cell_no, operator, reason):
    """
    管理员应急开柜：必须写理由（否则拒绝），并留痕。
    用于：开柜人身份核验失败、考生忘带证件、柜门故障等。
    """
    if not (reason or "").strip():
        return False, "应急开柜必须写明理由（留痕要求）"
    cell = store.get_cell(locker_code, cell_no)
    if cell is None:
        return False, "柜格不存在：%s/%s" % (locker_code, cell_no)
    store.log_locker(models.LockerLog(
        locker_code=locker_code, cell_no=cell_no, action="emergency", method="manual",
        operator=operator, candidate_id=cell.candidate_id,
        candidate_name=cell.candidate_name, device_desc=cell.device_desc,
        note="应急开柜：%s" % reason[:200]))
    store.log("应急开柜", "%s/%s 理由：%s" % (locker_code, cell_no, reason[:120]),
              operator or "system")
    return True, "已记录应急开柜（理由已留痕）"


def mark_fault(store, locker_code, cell_no, fault=True, operator="", note=""):
    """柜门异常：标记为故障/恢复正常（故障格不再分配）。"""
    cell = store.get_cell(locker_code, cell_no)
    if cell is None:
        return False, "柜格不存在"
    if fault:
        if cell.state == config.CELL_OCCUPIED:
            return False, "该格已存手机，先取走再报故障（避免手机被锁死在故障格里）"
        cell.state = config.CELL_FAULT
    else:
        if cell.state != config.CELL_FAULT:
            return False, "该格当前不是故障状态"
        cell.state = config.CELL_FREE
    cell.note = note or cell.note
    store.save_cell(cell)
    store.log_locker(models.LockerLog(
        locker_code=locker_code, cell_no=cell_no, action="fault", method="manual",
        operator=operator, note=("报故障：" if fault else "故障恢复：") + (note or "")))
    store.log("柜格故障" if fault else "柜格恢复", "%s/%s" % (locker_code, cell_no),
              operator or "system")
    return True, ("已标记故障" if fault else "已恢复正常")


def scan_overdue(store, link=None, notify_log=True):
    """
    超时未取扫描（考后集中取件提醒）。返回超时柜格列表；
    第一次发现会写一条 overdue 日志（以及可选的 03 上报）。
    """
    out = []
    for cell in store.list_cells(state=config.CELL_OCCUPIED):
        if cell.overdue() and not cell.overdue_notified:
            cell.overdue_notified = True
            store.save_cell(cell)
            if notify_log:
                store.log_locker(models.LockerLog(
                    locker_code=cell.locker_code, cell_no=cell.cell_no,
                    action="overdue", method="manual", operator="system",
                    candidate_id=cell.candidate_id, candidate_name=cell.candidate_name,
                    device_desc=cell.device_desc,
                    note="超时未取（已存 %.0f 分钟）"
                         % ((models.now_epoch() - cell.stored_epoch) / 60.0)))
                store.log("保管超时提醒", "%s/%s 超 %d 分钟未取"
                          % (cell.locker_code, cell.cell_no,
                             int((models.now_epoch() - cell.stored_epoch) / 60.0)),
                          "system")
            if link is not None:
                try:
                    link.report_locker(cell)
                except Exception:                     # noqa: BLE001
                    pass
            out.append(cell)
        elif cell.overdue():
            out.append(cell)
    return out


def trace(store, candidate_id=None, cell_no=None, locker_code=None, limit=50):
    """
    存取追溯（丢手机纠纷时用）：按考生号或柜格号把存取链拉出来。
    """
    logs = store.list_locker_logs(limit=limit, candidate_id=candidate_id,
                                  cell_no=cell_no, locker_code=locker_code)
    return sorted(logs, key=lambda x: x.epoch)


def stats(store):
    return store.cell_stats()


def usage_by_locker(store):
    """每台柜体的占用情况（界面/报告用）。"""
    out = []
    for lk in store.list_lockers():
        cells = store.list_cells(locker_id=lk.locker_id)
        occ = sum(1 for c in cells if c.state == config.CELL_OCCUPIED)
        fault = sum(1 for c in cells if c.state == config.CELL_FAULT)
        out.append({"code": lk.code, "name": lk.name, "site": lk.site,
                    "total": len(cells), "occupied": occ, "fault": fault,
                    "free": len(cells) - occ - fault,
                    "usage": (occ / float(len(cells))) if cells else 0.0,
                    "status": lk.status})
    return out
