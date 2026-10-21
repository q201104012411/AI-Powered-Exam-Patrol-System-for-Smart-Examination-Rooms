# -*- coding: utf-8 -*-
"""04 巡考机器人 · Windows 端自检。

不连接真车：启动 robot_sim 模拟服务，验证 RobotLink 的遥测/控制/急停/参数/画面。
用法：python selftest.py     （全部通过退出码 0）
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import robot_link
import robot_sim


def main() -> int:
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print(("  PASS " if cond else "  FAIL ") + name +
              (("  [" + detail + "]") if detail and not cond else ""))

    print("04 巡考机器人 · Windows 端自检")
    print("=" * 60)
    port = 18089
    sim = robot_sim.SimCar("127.0.0.1", port).start()
    try:
        time.sleep(0.5)
        link = robot_link.RobotLink("127.0.0.1:%d" % port)
        st = link.state()
        check("连接模拟小车 /api/state", isinstance(st, dict) and st.get("ok") is not False,
              str(st)[:120])
        check("返回电量", "battery" in st or "voltage" in st, str(st)[:120])

        link.cmd(0.05, 0.0)
        time.sleep(0.5)
        st2 = link.state()
        check("速度指令可下发", isinstance(st2, dict), str(st2)[:120])

        link.estop(True)
        st3 = link.state()
        check("急停可锁定", bool(st3.get("estop") or st3.get("estop_on") or
                                st3.get("safe", {}).get("estop")), str(st3)[:120])
        link.estop(False)

        params = link.params()
        check("巡线参数可读取", isinstance(params, dict), str(params)[:120])

        try:
            data = link.snap("astra")
            status = 200
            check("画面 /api/snap 可用", status == 200 and len(data) > 100,
                  "status=%s len=%s" % (status, len(data) if data else 0))
        except Exception as exc:                              # noqa: BLE001
            check("画面 /api/snap 可用", False, str(exc))
    finally:
        try:
            sim.stop()
        except Exception:                                    # noqa: BLE001
            pass

    passed = sum(1 for _, ok, _ in checks if ok)
    print("=" * 60)
    print("通过 %d/%d" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
