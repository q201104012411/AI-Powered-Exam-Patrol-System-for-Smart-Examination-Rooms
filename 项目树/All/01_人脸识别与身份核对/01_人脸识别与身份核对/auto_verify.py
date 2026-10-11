# -*- coding: utf-8 -*-
"""
自动核验程序（无人值守）：无需任何点击，自动抓帧 → 自动判断 → 自动输出与留痕。

权限：**仅所有者（owner）可开启**
  · 首次运行需输入所有者口令授权；加 --remember 可记住 8 小时（写 data/auto_verify.enabled）；
  · 也可先跑 --authorize 单独授权、--deauthorize 撤销授权。

用法：
  python auto_verify.py                       # 默认 1:N 连续核验（考场抽查）
  python auto_verify.py --mode 1:1 --candidate C001
  python auto_verify.py --interval 1.5 --camera 0 --device GATE-B-01
  python auto_verify.py --authorize           # 只授权，不开摄像头
  python auto_verify.py --deauthorize         # 撤销授权

停止：Ctrl+C

限制（如实说明）：本通道是无人值守的，**没有输入身份证明文的手段**。
因此当所有者开启「大考模式」（核验必须使用身份证）时，1:1 模式会**拒绝启动**并提示
改用核验界面/网页端逐人核验，避免"自动放行却跳过了身份证校验"。
1:N 抽查本就不比对身份要素，不受影响。
常规模式（默认）下本通道只做"人脸 + 现场性"判定，身份要素不参与（准考证号无从输入），
证件仍需工作人员对照实物目视核对。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import accounts          # noqa: E402
import config            # noqa: E402
import liveness          # noqa: E402
import video             # noqa: E402
from audit import AuditLogger       # noqa: E402
from face_index import FaceIndex    # noqa: E402
from store import CandidateStore    # noqa: E402
from verifier import FaceVerifier   # noqa: E402

AUTH_FILE = os.path.join(str(config.DATA_ROOT), "auto_verify.enabled")
AUTH_HOURS = 8


def _line(ch="=", n=68):
    print(ch * n)


def prompt_owner():
    """要求输入所有者账户口令（最多 3 次）"""
    _line()
    print("  自动核验需要所有者授权（账户：owner）")
    _line()
    for _ in range(3):
        try:
            u = input("所有者用户名（直接回车=owner）：").strip() or "owner"
            p = input("口令：").strip()
        except EOFError:
            return None
        user, err = accounts.authenticate(u, p, ip="auto-verify")
        if user and user.get("role") == accounts.ROLE_OWNER:
            print("授权成功：%s（%s）" % (user["username"], accounts.role_text(user["role"])))
            return user
        print("授权失败：%s" % (err or "该账户不是所有者，无法开启自动核验"))
    return None


def write_auth(user):
    data = {"operator": user["username"],
            "authorized_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": (datetime.now() + timedelta(hours=AUTH_HOURS)).isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def read_auth():
    if not os.path.exists(AUTH_FILE):
        return None
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if datetime.fromisoformat(d["expires_at"]) < datetime.now():
            return None
        return d
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="自动核验程序（仅所有者可开启）")
    ap.add_argument("--mode", default="1:N", choices=["1:1", "1:N"], help="核验模式")
    ap.add_argument("--candidate", default="", help="1:1 模式的考生编号")
    ap.add_argument("--camera", type=int, default=0, help="摄像头编号")
    ap.add_argument("--interval", type=float, default=2.0, help="两次核验间隔（秒）")
    ap.add_argument("--device", default="AUTO-GATE-01", help="设备编号")
    ap.add_argument("--authorize", action="store_true", help="只授权（生成授权文件），不开摄像头")
    ap.add_argument("--deauthorize", action="store_true", help="撤销授权")
    ap.add_argument("--remember", action="store_true", help="授权后记住 8 小时，免再次输口令")
    args = ap.parse_args()

    # ---- 授权管理 ----
    if args.deauthorize:
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
            print("已撤销自动核验授权。")
        else:
            print("当前没有授权文件。")
        return 0

    if args.authorize:
        user = prompt_owner()
        if not user:
            return 1
        d = write_auth(user)
        print("已授权，有效期至 %s" % d["expires_at"])
        return 0

    auth = read_auth()
    operator = "owner"
    if auth:
        operator = auth.get("operator", "owner")
        print("使用已保存的授权：所有者 %s（有效至 %s）" % (operator, auth["expires_at"]))
    else:
        user = prompt_owner()
        if not user:
            print("未获授权，退出。")
            return 1
        operator = user["username"]
        if args.remember:
            d = write_auth(user)
            print("已记住授权，有效期至 %s" % d["expires_at"])

    if args.mode == "1:1" and not args.candidate:
        print("[错误] 1:1 模式必须指定 --candidate（例如 --candidate C001）")
        return 1

    # 大考模式要求逐个考生输入身份证原件明文做哈希比对；本无人值守通道没有输入手段，
    # 因此**拒绝启动**，避免"自动放行却跳过了身份证校验"这种静默放宽。
    if accounts.major_exam_enabled() and args.mode == "1:1":
        print("[已拒绝启动] 当前处于「大考模式」：核验必须使用身份证，"
              "而本无人值守通道无法输入身份证明文。")
        print("             请改用「启动核验界面.bat」或网页端「实时核验」逐人核验；")
        print("             或由所有者在「所有者设置」中关闭「大考模式」。")
        return 2

    # ---- 准备核验环境 ----
    candidates = CandidateStore().load()
    index = FaceIndex()
    index.load()
    if index.size == 0:
        print("[错误] 特征索引为空，请先执行：python enroll.py build")
        return 1
    verifier = FaceVerifier(candidates, index, device_id=args.device,
                            logger=AuditLogger(), operator=operator)
    print("报名库 %d 人 ｜ 特征索引 %d 人" % (len(candidates), index.size))
    print("身份要素：%s" % accounts.identity_mode_text())

    stream = video.VideoStream(camera_index=args.camera)
    print("正在打开摄像头 %d ..." % args.camera)
    if not stream.start():
        print("[错误] %s" % (stream.error or "摄像头打开失败"))
        return 1

    _line()
    print("  自动核验已启动（无需任何点击）")
    print("  模式：%s　间隔：%.1f 秒　设备：%s　摄像头：%d"
          % (args.mode, args.interval, args.device, args.camera))
    print("  按 Ctrl+C 停止")
    _line()

    stats = {"total": 0, "PASS": 0, "MANUAL": 0, "FAIL": 0}
    try:
        while True:
            frame, _seq = stream.latest()
            if frame is None:
                time.sleep(0.2)
                continue
            token = liveness.make_token(args.device, source="camera")
            try:
                # 自动模式：人脸匹配即自动通过（免人工点击），仍写 AUTO_PASS 留痕
                if args.mode == "1:1":
                    rec = verifier.verify_face_first(args.candidate, live_images=[frame],
                                                     capture_token=token, auto_confirm=True)
                else:
                    rec = verifier.verify_1toN(frame, capture_token=token, auto_confirm=True)
            except Exception as e:
                print("[%s] 核验异常：%s" % (time.strftime("%H:%M:%S"), str(e)[:80]))
                time.sleep(args.interval)
                continue

            stats["total"] += 1
            stats[rec.result] = stats.get(rec.result, 0) + 1
            tail = ""
            if rec.result == "PASS":
                tail = "→ 请人工核对证件：" + (rec.id_card_display or "")
            elif rec.reason_code in ("AMBIGUOUS", "TWIN_SUSPECT", "IDENTITY_CONFLICT",
                                     "POSE_EXTREME", "IMAGE_QUALITY_REVIEW"):
                tail = "→ 需人工复验"
            print("[%s] %-6s %-22s 考生=%-6s 分数=%-8s %s" % (
                time.strftime("%H:%M:%S"), rec.result, rec.reason_code,
                rec.candidate_id or "-", str(rec.score), tail))
            if stats["total"] % 20 == 0:
                print("  ---- 累计：核验 %d ｜ 通过 %d ｜ 复验 %d ｜ 拦截 %d ----"
                      % (stats["total"], stats["PASS"], stats["MANUAL"], stats["FAIL"]))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _line()
        print("已停止。本次统计：核验 %d ｜ 通过 %d ｜ 转人工复验 %d ｜ 拦截 %d"
              % (stats["total"], stats["PASS"], stats["MANUAL"], stats["FAIL"]))
    finally:
        try:
            stream.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
