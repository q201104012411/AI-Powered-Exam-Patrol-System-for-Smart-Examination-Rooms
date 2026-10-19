# -*- coding: utf-8 -*-
r"""
只读检查：07 是否污染了其它模块的真实运行数据。

为什么要这个工具：07 的联动测试会真的调用 02/03/06 的 integration_api。如果沙箱没生效，
测试数据就会写进它们的真实库（camera.db / terminal.db / security.db），
以后看现场数据就会看到"自检造出来的告警"。所以每次交付前跑一遍这个工具。

**只读**：sqlite 用 mode=ro 打开，不写一行。

怎么读结果：
  · 刚跑完 07 自检 -> "来自 07 的告警"应为 **0**（自检把 02/03/06 重定向到了临时沙箱）；
  · 生产运行 -> 03 里出现 07 的告警是**正常联动**（source 含 07），不是污染；
  · 06/02 的真实库不该有 07 写入的任何记录（07 只读 06 的报警、只读 02 的机位台账）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = os.path.dirname(HERE)


def stat_line(path):
    if not os.path.isfile(path):
        return "    （文件不存在）"
    st = os.stat(path)
    return "    大小 %.1f KB ｜ 最后写入 %s" % (
        st.st_size / 1024.0,
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))


def query(path, sql, params=()):
    """
    只读查询：先把库（连同 -wal/-shm）**复制到临时目录**再读。

    为什么不直接 `mode=ro`：SQLite 的 WAL 模式在只读打开时也会触碰 -shm，
    会让"最后写入时间"变化，看起来像被写脏。复制一份读，能保证源库零副作用。
    """
    tmp = tempfile.mkdtemp(prefix="rf_pollution_")
    base = os.path.join(tmp, os.path.basename(path))
    try:
        shutil.copy2(path, base)
        for suffix in ("-wal", "-shm"):
            if os.path.isfile(path + suffix):
                shutil.copy2(path + suffix, base + suffix)
        conn = sqlite3.connect(base)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except (sqlite3.DatabaseError, OSError) as e:
        return [{"_error": str(e)}]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def show(title, path, items):
    print("=" * 76)
    print("[%s] %s" % (title, path))
    print(stat_line(path))
    if not os.path.isfile(path):
        print("    -> 未被创建，干净")
        return
    for label, sql, params in items:
        rows = query(path, sql, params)
        if rows and "_error" in rows[0]:
            print("    %-16s ERR %s" % (label, rows[0]["_error"]))
            continue
        if sql.strip().upper().startswith("SELECT COUNT"):
            print("    %-16s = %s" % (label, rows[0][0]))
        else:
            print("    %-16s:" % label)
            for r in rows:
                print("        " + " | ".join(str(x) for x in tuple(r)))


def main():
    print("07 无线信号监测与屏蔽管控 · 其它模块数据污染检查（只读）")
    print("本模块目录：%s" % HERE)
    print("检查时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    show("03 智能终端", os.path.join(ALL, "03_智能终端操作", "data", "terminal.db"), [
        ("告警总数", "SELECT COUNT(*) FROM alerts", ()),
        ("来自 07 的告警",
         "SELECT COUNT(*) FROM alerts WHERE source LIKE '%07%'"
         " OR device_id LIKE 'RF-%'", ()),
        ("最近 8 条", "SELECT alert_id, code, room_id, source, created_at "
                     "FROM alerts ORDER BY rowid DESC LIMIT 8", ()),
        ("远端设备", "SELECT COUNT(*) FROM remote_devices", ()),
        ("审计条数", "SELECT COUNT(*) FROM audit", ()),
    ])
    show("06 智能安检", os.path.join(ALL, "06_智能安检与手机作弊器材检测",
                                    "data", "security.db"), [
        ("报警总数", "SELECT COUNT(*) FROM alarms", ()),
        ("过检总数", "SELECT COUNT(*) FROM passes", ()),
        ("最近 5 条", "SELECT alarm_id, device_code, room_id, kinds "
                     "FROM alarms ORDER BY rowid DESC LIMIT 5", ()),
    ])
    show("02 摄像头核查", os.path.join(ALL, "02_摄像头核查与视频质量诊断",
                                     "data", "camera.db"), [
        ("机位总数", "SELECT COUNT(*) FROM devices", ()),
        ("07 编号机位", "SELECT COUNT(*) FROM devices WHERE code LIKE 'CAM-RM101%'", ()),
        ("最近 5 个机位", "SELECT code, room_id, name FROM devices "
                         "ORDER BY rowid DESC LIMIT 5", ()),
    ])
    print("=" * 76)
    print("[07 本模块真实数据目录] %s" % os.path.join(HERE, "data"))
    data_dir = os.path.join(HERE, "data")
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir))[:20]:
            full = os.path.join(data_dir, name)
            print("    %-24s %s" % (name, "目录" if os.path.isdir(full)
                                     else "%.1f KB" % (os.path.getsize(full) / 1024.0)))
    else:
        print("    （尚未创建：还没在真实目录跑过界面/演示）")
    print("=" * 76)
    print("结论口径：")
    print("  · 刚跑完自检 -> 03 的“来自 07 的告警”= 0 才是干净（自检走沙箱）；")
    print("  · 生产运行 -> 03 出现 07 告警是正常联动（source 含 07），不是污染；")
    print("  · 06/02 真实库不应出现 07 的写入（07 只读取它们的接口）。")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
