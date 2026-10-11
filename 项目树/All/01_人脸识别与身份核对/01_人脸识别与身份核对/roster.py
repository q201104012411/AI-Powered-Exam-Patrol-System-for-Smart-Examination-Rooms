# -*- coding: utf-8 -*-
"""
名单导入：从 Excel(.xlsx) / CSV 批量导入考生名单。

说明：
  · 身份证号走 store.upsert_many -> 加盐 HMAC 哈希，明文不落库；
  · 表头支持中英文常见写法（见 HEADER_ALIASES）；
  · 仅"考生编号"必填，其余列可缺省。
权限：本模块只负责解析与写库，"谁有权导入"由调用方（GUI/网页）用
      accounts.can_clear_logs(role) 判定——即仅所有者。
"""
import csv
import os

# 表头别名：字段 -> 可接受的中英文表头
HEADER_ALIASES = {
    "candidate_id": ["candidate_id", "考生编号", "考生号", "编号", "考号", "考生id"],
    "name": ["name", "姓名", "考生姓名", "名字"],
    "id_card": ["id_card", "身份证号", "身份证", "证件号", "身份证号码"],
    "ticket_no": ["ticket_no", "准考证号", "准考证", "准考证号码"],
    "seat_no": ["seat_no", "座位号", "座位", "考场座位"],
    "reference_photo": ["reference_photo", "参考照", "照片", "照片路径", "参考照片"],
    "consent_status": ["consent_status", "同意状态", "单独同意", "同意"],
}
REQUIRED = ("candidate_id",)


def _norm(v):
    return "" if v is None else str(v).strip()


def read_rows(path):
    """读取 Excel / CSV，返回 (数据行列表, 表头列表)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError("缺少 openpyxl，无法读取 Excel。请先 pip install openpyxl，或改用 CSV 名单。")
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        data = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            data = [r for r in csv.reader(f)]
    else:
        raise RuntimeError("仅支持 .xlsx / .csv 名单文件（当前：%s）" % (ext or "未知"))
    if not data:
        return [], []
    header = [_norm(h) for h in data[0]]
    return data[1:], header


def map_columns(header):
    """表头 -> {字段: 列序号}"""
    low = [h.lower() for h in header]
    idx = {}
    for field, names in HEADER_ALIASES.items():
        for i, h in enumerate(header):
            if h in names or low[i] in [n.lower() for n in names]:
                idx[field] = i
                break
    return idx


def parse_roster(path):
    """解析名单 -> (items, warnings)；缺必填列会抛错"""
    rows, header = read_rows(path)
    if not header:
        raise RuntimeError("名单是空文件（没有表头行）")
    idx = map_columns(header)
    missing = [f for f in REQUIRED if f not in idx]
    if missing:
        raise RuntimeError("名单缺少必需列「考生编号」；当前表头：%s" % "、".join(header))
    items, warnings = [], []
    seen = set()
    for n, row in enumerate(rows, start=2):
        def get(field):
            i = idx.get(field)
            return _norm(row[i]) if (i is not None and i < len(row)) else ""
        cid = get("candidate_id")
        if not cid:
            continue                              # 跳过空行
        if cid in seen:
            warnings.append("第 %d 行考生编号重复，已跳过：%s" % (n, cid))
            continue
        seen.add(cid)
        items.append({
            "candidate_id": cid,
            "name": get("name"),
            "id_card": get("id_card"),
            "ticket_no": get("ticket_no"),
            "seat_no": get("seat_no"),
            "reference_photo": get("reference_photo"),
            "consent_status": get("consent_status") or "未登记",
        })
    if not items:
        raise RuntimeError("名单里没有有效数据行（第 2 行起为空）")
    return items, warnings


def import_roster(path, operator="roster-import"):
    """
    导入并写库（身份证号加盐哈希）。返回统计：
      {"total": 行数, "imported": 写入数, "warnings": [...], "columns": {...}}
    """
    from store import CandidateStore
    items, warnings = parse_roster(path)
    store = CandidateStore()
    n = store.upsert_many(items, operator=operator)
    return {"total": len(items), "imported": n, "warnings": warnings,
            "columns": sorted(map_columns(read_rows(path)[1]).keys())}
