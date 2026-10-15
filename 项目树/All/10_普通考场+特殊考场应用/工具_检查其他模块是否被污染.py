# -*- coding: utf-8 -*-
"""只读检查：各模块真实运行库是否被专题 10 的自检污染。"""
import os
import sqlite3
import sys

ALL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [
    ("02", os.path.join(ALL, "02_摄像头核查与视频质量诊断", "data", "camera.db"),
     [("devices", "SELECT code, room_id, name, created_at FROM devices ORDER BY rowid DESC LIMIT 10"),
      ("checks", "SELECT COUNT(*) FROM checks"),
      ("audits", "SELECT COUNT(*) FROM audits")]),
    ("03", os.path.join(ALL, "03_智能终端操作", "data", "terminal.db"),
     [("alerts", "SELECT COUNT(*) FROM alerts"),
      ("audit", "SELECT COUNT(*) FROM audit"),
      ("reviews", "SELECT COUNT(*) FROM reviews"),
      ("来自专题10的告警（应为 0）",
       "SELECT COUNT(*) FROM alerts WHERE source LIKE '%10_room%'"
       " OR source LIKE '%linkage/10%' OR source LIKE '10%'"),
      ("recent", "SELECT alert_id, code, room_id, source FROM alerts ORDER BY rowid DESC LIMIT 8")]),
    ("06", os.path.join(ALL, "06_智能安检与手机作弊器材检测", "data", "security.db"),
     [("devices", "SELECT COUNT(*) FROM devices"),
      ("alarms", "SELECT COUNT(*) FROM alarms"),
      ("recent", "SELECT alarm_id, device_code, room_id FROM alarms ORDER BY rowid DESC LIMIT 6")]),
    ("01", os.path.join(ALL, "01_人脸识别与身份核对",
                        "01_人脸识别与身份核对", "data", "exam.db"),
     [("tables", "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]),
]


def main():
    for key, path, queries in TARGETS:
        print("=" * 70)
        print("[%s] %s" % (key, path))
        if not os.path.isfile(path):
            print("    （库不存在，未被创建 -> 干净）")
            continue
        st = os.stat(path)
        import time
        print("    mtime = %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(st.st_mtime)))
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        c.row_factory = sqlite3.Row
        for label, sql in queries:
            try:
                rows = c.execute(sql).fetchall()
                if sql.strip().upper().startswith("SELECT COUNT"):
                    print("    %-10s = %s" % (label, rows[0][0]))
                else:
                    print("    %-10s:" % label)
                    for r in rows:
                        print("        " + " | ".join(str(x) for x in tuple(r)))
            except Exception as e:                       # noqa: BLE001
                print("    %-10s ERR %s" % (label, e))
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
