# -*- coding: utf-8 -*-
r"""
12 告警上传与终端处置演示
==========================

把这条链真正跑通，一步不省：

    02 摄像头核查诊断 ──告警+画质可信度──> 上传 ──> 03 智能终端接入
                                                        │
                        ┌───────────────────────────────┘
                        ▼
              待处置 ──确认──> 已确认 ──复核──> 已复核（哈希链留痕）

三个场景：
  A 常规闭环    02 发现画质问题 -> 上传 -> 03 接入 -> 确认 + 复核「成立」
  B 强制确认    红色预警上传到 03 -> 必须先确认才能复核（演示顺序约束）
  C 红转橙      画质可信度 < 0.6 的红色告警，03 自动降为橙色待复核（防误报堆叠）

另外附带两条边界：重复上传被去重、监考员角色无法复核（权限最小化）。

安全约定：默认**沙箱跑**（--sandbox），02/03 的数据目录都重定向到
          data\\_demo_sandbox\\，绝不动真实运行数据。
          要打到真实数据用 --no-sandbox，会二次确认并要求 --yes。

用法：
    python demo_main.py                    # 沙箱跑全部场景
    python demo_main.py --scenario A       # 只跑 A
    python demo_main.py --keep             # 跑完保留沙箱目录（0 表示保留全部）
    python demo_main.py --list-interp      # 只打印会用到哪些解释器
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from upload_client import CameraLink, TerminalLink, DOWNGRADE_BELOW, UploadError  # noqa: E402
from terminal_console import TerminalConsole                                     # noqa: E402
from disposition import DispositionDesk                                          # noqa: E402

ALL_DIR = os.path.dirname(HERE)
DIR_02 = os.path.join(ALL_DIR, "02_摄像头核查与视频质量诊断")
DIR_03 = os.path.join(ALL_DIR, "03_智能终端操作")
SANDBOX_DEFAULT = os.path.join(HERE, "data", "_demo_sandbox")


# ============================================================ 终端输出小工具
class Pal(object):
    """纯 ASCII 线框；不依赖任何终端字体假设。"""

    W = 78
    on = True

    @classmethod
    def rule(cls, ch="="):
        print(ch * cls.W if cls.on else "")

    @classmethod
    def title(cls, text):
        cls.rule("=")
        print("  " + text)
        cls.rule("=")

    @classmethod
    def step(cls, text):
        print("\n-- %s" % text)

    @classmethod
    def kv(cls, k, v, indent=4):
        print("%s%s: %s" % (" " * indent, k, v))

    @classmethod
    def ok(cls, text):
        print("  [OK]   %s" % text)

    @classmethod
    def fail(cls, text):
        print("  [FAIL] %s" % text)

    @classmethod
    def info(cls, text):
        print("  [ -- ] %s" % text)

    @classmethod
    def warn(cls, text):
        print("  [ !! ] %s" % text)


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def preview_upload(payload):
    """打印 JSON-RPC 形状的报文（按 UTF-8 原始字节算长度，中文不会虚高）。"""
    import json
    body = json.dumps(payload, ensure_ascii=False)
    print("    POST /alerts  (JSON, UTF-8, %d 字节)" % len(body.encode("utf-8")))
    print("    " + body)


# ============================================================ 合成画面
def synth_frame(kind, path):
    """
    造一张合成画面，让 02 真的有东西可诊断（不依赖任何真实摄像头）。

    kind='clean'     -> 高对比棋盘 + 边缘丰富：02 判为正常
    kind='blur'      -> 缩小再放大 + 低对比：02 命中 LOW_CONTRAST（warn 级）
    kind='veryblur'   -> 低对比 + 强模糊：02 命中 BLUR（fail 级）
                        fail 级才会把画质可信度打到 ×0.5，从而触发 03 的"红转橙"

    这三个档位是实测量出来的：02 的阈值见 02/config.py 的 QUALITY
    （blur_min=60 拉普拉斯方差、contrast_min=22 亮度标准差）。
    """
    import cv2
    import numpy as np

    h, w = 720, 1280
    if kind == "veryblur":
        # 低对比大块棋盘：std ≈ 19（高于 contrast_min×0.8，BLUR 才不会被 _fuse 抑制）
        img = np.full((h, w, 3), 90, np.uint8)
        block = 160
        for y in range(0, h, block):
            for x in range(0, w, block):
                if ((x // block) + (y // block)) % 2 == 0:
                    img[y:y + block, x:x + block] = 128
        small = cv2.resize(img, (w // 8, h // 8), interpolation=cv2.INTER_AREA)
        img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        img = cv2.GaussianBlur(img, (0, 0), 3)
    else:
        img = np.full((h, w, 3), 235, np.uint8)
        block = 40
        for y in range(0, h, block):
            for x in range(0, w, block):
                if ((x // block) + (y // block)) % 2 == 0:
                    img[y:y + block, x:x + block] = (60, 60, 60)
        cv2.rectangle(img, (80, 80), (w - 80, h - 80), (0, 90, 200), 6)
        cv2.putText(img, "EXAM ROOM A-01", (110, 150),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
        if kind == "blur":
            small = cv2.resize(img, (w // 24, h // 24), interpolation=cv2.INTER_AREA)
            img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
            img = cv2.GaussianBlur(img, (0, 0), 9)
            img = (img.astype(np.float32) * 0.35 + 128).astype(np.uint8)   # 压低对比度

    os.makedirs(os.path.dirname(path), exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise RuntimeError("合成画面编码失败")
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return path


# ============================================================ 演示主体
class Demo(object):
    def __init__(self, home_02, home_03, keep_sandbox, verbose=False):
        self.home_02 = home_02
        self.home_03 = home_03
        self.keep_sandbox = keep_sandbox
        self.verbose = verbose
        self.cam = CameraLink(home=home_02)
        self.term = TerminalLink(home=home_03)
        self.tc = TerminalConsole(DIR_03, home_03, verbose=verbose)
        self.tc.assert_sandbox()
        self.desk = DispositionDesk(self.tc)
        self.results = []

    # ---------------- 准备 ----------------
    def tc_data_dir_02(self):
        """02 的数据目录（沙箱时是沙箱内的；真实模式下由 02 的 config 决定）。"""
        return self.home_02 or os.path.join(DIR_02, "data")

    def preflight(self):
        Pal.step("0. 环境自检")
        Pal.kv("python", sys.executable)
        for name, d in (("02", DIR_02), ("03", DIR_03)):
            mark = "存在" if os.path.isdir(d) else "缺失"
            Pal.kv("%s 目录" % name, "%s (%s)" % (d, mark))
        ok02, why02 = self.cam.p.available()
        ok03, why03 = self.term.p.available()
        Pal.kv("02 集成接口", why02)
        Pal.kv("03 集成接口", why03)
        Pal.kv("02 数据目录", self.tc_data_dir_02())
        Pal.kv("03 数据目录", self.tc.data_dir())
        if not (ok02 and ok03):
            raise UploadError("02/03 集成接口不可用，无法演示")
        Pal.ok("两个系统的数据目录都已重定向到沙箱，真实运行数据不会被改动")

    def prepare_camera(self):
        """登记机位 + 各采集一张基准图，让 02 后续诊断有比对基准。"""
        Pal.step("1. 02 侧准备：登记机位、采集基准图")
        devices = [
            ("CAM-A01-1", "A-01", "1 考场", "前门上方", "clean"),
            ("CAM-A01-2", "A-01", "1 考场", "后墙中部", "clean"),
        ]
        for code, room, site, pos, kind in devices:
            self.cam.ensure_device(code=code, name="%s %s" % (room, pos),
                                   room_id=room, site=site, position=pos,
                                   url="rtsp://192.168.1.21/ch1", fov_deg=95)
            shot = os.path.join(self.home_02, "demo_frames", "%s_base.jpg" % code)
            synth_frame(kind, shot)
            res = self.cam.baseline(code, shot)
            Pal.ok("机位 %s 已登记，基准图清晰度 %.1f" % (code, res.get("sharpness", 0)))
        return [d[0] for d in devices]

    # ---------------- 场景 ----------------
    def _inspect_and_upload(self, device_code, kind, code, room, seat="",
                            detail_extra="", candidate_id="", candidate_name="",
                            credibility_override=None):
        """
        02 诊断一张画面 -> 把结论作为告警上传到 03。
        返回 (inspect 结果, 上传结果, 实际用的可信度, 说明)。
        """
        shot = os.path.join(self.home_02, "demo_frames",
                            "%s_%s.jpg" % (device_code, kind))
        synth_frame(kind, shot)
        ins = self.cam.inspect(shot, device_code=device_code)
        cred = self.cam.credibility(device_code)
        factor = credibility_override if credibility_override is not None else cred.get("factor", 1.0)
        defects = ins.get("defects", [])
        names = "、".join("%s(%s)" % (d.get("name"), d.get("severity")) for d in defects) or "无"
        detail = ("02 现场画面诊断：%s；机位画质可信度 ×%.1f（%s）%s"
                  % (names, factor, cred.get("reason", ""), detail_extra))
        payload = {"code": code, "device_id": device_code, "room_id": room,
                   "seat_no": seat, "detail": detail, "credibility": round(factor, 3),
                   "candidate_id": candidate_id, "candidate_name": candidate_name,
                   "source": "02_摄像头核查与视频质量诊断"}
        preview_upload(payload)
        up = self.term.upload_alert(code=code, device_id=device_code, room_id=room,
                                    seat_no=seat, detail=detail,
                                    candidate_id=candidate_id,
                                    candidate_name=candidate_name,
                                    credibility=factor)
        return ins, up, factor, cred.get("reason", "")

    def scenario_A(self, devices, operator="patrol1"):
        """常规闭环：画质告警 -> 上传 -> 确认/复核成立。"""
        Pal.title("场景 A · 常规闭环：02 画质告警 -> 03 接入 -> 处置「成立」")
        dev = devices[0]
        Pal.info("02 诊断一张现场画面并出告警（此处命中 warn 级 LOW_CONTRAST）：")
        ins, up, factor, why = self._inspect_and_upload(
            dev, "blur", "VIDEO_BLUR", "A-01", detail_extra="；来源=日常巡检")
        res = self._dispose_and_report(up, operator,
                                       conclusion="confirmed",
                                       reason="画面确实模糊，已通知运维擦拭镜头并调整对焦",
                                       title="场景 A 处置")
        self.results.append(("A 常规闭环", res))
        return res

    def scenario_B(self, devices, operator="patrol1"):
        """红色预警：必须先确认再复核。"""
        Pal.title("场景 B · 红色预警：03 强制「先确认后复核」")
        dev = devices[0]
        ins, up, factor, why = self._inspect_and_upload(
            dev, "clean", "MULTI_FACE", "A-01", seat="05",
            detail_extra="；同一画面检测到 2 张人脸",
            candidate_id="330102200801011234", candidate_name="张某某")

        alert_id = up.get("alert_id")
        sess, _ = self.tc.login(operator)
        if sess is None:
            Pal.fail("账户 %s 不存在，无法处置" % operator)
            self.results.append(("B 强制确认后复核", False))
            return False
        if self.tc.get_alert(alert_id) is not None:
            a = self.tc.get_alert(alert_id)
            Pal.kv("03 接入结果", "级别=%s 强制确认=%s 状态=%s"
                   % (self.tc.level_name(a.level), a.needs_force_confirm(), a.status))

            Pal.step("试着**跳过确认**直接复核（应当被 03 挡下）")
            ok, msg = self.desk.review(alert_id, "confirmed", "跳过确认试试", sess)
            if ok:
                Pal.fail("竟然复核成功了 —— 强制确认约束失效！")
                self.results.append(("B 跳过确认", False))
            else:
                Pal.ok("被 03 正确挡下：%s" % msg)
                self.results.append(("B 跳过确认被拦截", True))

        res = self._dispose_and_report(up, operator, conclusion="confirmed",
                                       reason="监考员现场核实：确为考生抬头张望，已口头警告",
                                       title="场景 B 处置（自动补确认）", show_header=False)
        self.results.append(("B 强制确认后复核", res))
        return res

    def scenario_C(self, devices, operator="admin"):
        """红转橙：画质可信度低，红色告警被 03 降级。"""
        Pal.title("场景 C · 红转橙：画质可信度 < %.1f，红色预警自动降级" % DOWNGRADE_BELOW)
        dev = devices[1]
        Pal.info("先让 02 真的判出 fail 级缺陷（BLUR），画质可信度会被打到 ×0.5；")
        Pal.info("再用这个可信度上传一条**红色**告警 PHONE_DETECTED：")
        ins, up, factor, why = self._inspect_and_upload(
            dev, "veryblur", "PHONE_DETECTED", "A-01", seat="12",
            detail_extra="；疑似手机屏幕反光",
            candidate_id="330102200802029876", candidate_name="李某某")
        Pal.kv("实际使用的画质可信度", "×%.2f（%s）" % (factor, why))
        Pal.kv("03 判定结果", "降级=%s 当前级别=%s"
               % (up.get("downgraded"), self.tc.level_name(up.get("level", ""))))
        if up.get("downgraded"):
            Pal.ok("红色已按联动约定降为橙色待复核 —— 防画质问题堆成误报")
            self.results.append(("C 红转橙生效", True))
        else:
            Pal.warn("本次未触发降级（可信度 ×%.2f 未低于 %.1f）"
                     % (factor, DOWNGRADE_BELOW))
            self.results.append(("C 红转橙未触发", False))

        res = self._dispose_and_report(up, operator, conclusion="false_positive",
                                       reason="回看录像为镜片污渍造成的反光，非手机；已开工单清洁",
                                       title="场景 C 处置（误报 -> 算法迭代闭环）",
                                       show_header=False)
        self.results.append(("C 误报闭环", res))
        return res

    def scenario_D(self, devices, operator="patrol1"):
        """边界：重复上传被去重。"""
        Pal.title("场景 D · 边界：同考场+座位+告警码在 60 秒内重复上传 -> 去重")
        dev = devices[0]
        first = self.term.upload_alert(code="LOOK_DOWN", device_id=dev, room_id="A-01",
                                       seat_no="07", detail="持续低头 45 秒",
                                       credibility=1.0)
        second = self.term.upload_alert(code="LOOK_DOWN", device_id=dev, room_id="A-01",
                                        seat_no="07", detail="持续低头 45 秒",
                                        credibility=1.0)
        Pal.kv("第 1 次上传", "created=%s alert_id=%s" % (first.get("created"), first.get("alert_id")))
        Pal.kv("第 2 次上传", "created=%s alert_id=%s" % (second.get("created"), second.get("alert_id")))
        same = (first.get("alert_id") == second.get("alert_id") and second.get("created") is False)
        if same:
            Pal.ok("重复上传被去重（未产生新告警）—— 03/alerts.py 的 60 秒去重窗口")
        else:
            Pal.fail("重复上传未被去重")
        self.results.append(("D 重复上传去重", same))
        return same

    def scenario_E(self, devices):
        """边界：监考员不能复核（权限最小化）。"""
        Pal.title("场景 E · 边界：监考员角色无复核权（不能自己判自己）")
        dev = devices[0]
        up = self.term.upload_alert(code="LOOK_DOWN", device_id=dev, room_id="A-01",
                                    seat_no="09", detail="频繁转头 6 次/分钟",
                                    credibility=1.0)
        alert_id = up.get("alert_id")
        sess, _ = self.tc.login("inv01")
        if sess is None:
            Pal.fail("预置账户 inv01 不存在")
            self.results.append(("E 监考员无复核权", False))
            return False
        Pal.kv("inv01 可见考场", sess.room_filter())
        ok, msg = self.desk.review(alert_id, "confirmed", "监考员试着自己复核", sess)
        if ok:
            Pal.fail("监考员竟然复核成功 —— 权限约束失效！")
            self.results.append(("E 监考员无复核权", False))
        else:
            Pal.ok("被 03 正确拒绝：%s" % msg)
            self.results.append(("E 监考员无复核权", True))
        return not ok

    # ---------------- 处置 + 展示 ----------------
    def _dispose_and_report(self, up, operator, conclusion, reason, title,
                            show_header=True):
        alert_id = up.get("alert_id")
        if show_header:
            Pal.kv("上传回执", "alert_id=%s 级别=%s 新建=%s 降级=%s"
                   % (alert_id, self.tc.level_name(up.get("level", "")),
                      up.get("created"), up.get("downgraded")))
        sess, _ = self.tc.login(operator)
        if sess is None:
            Pal.fail("账户 %s 不存在，无法处置" % operator)
            return False
        Pal.info("处置身份：%s" % sess.describe())

        res = self.desk.dispose(alert_id, sess, conclusion=conclusion, reason=reason)

        Pal.step("%s：逐步结果" % title)
        for s in res.steps:
            (Pal.ok if s.ok else Pal.fail)("%s -> %s" % (s.name, s.detail))

        if res.timeline:
            Pal.step("全过程留痕（03 的 review.timeline）")
            for ev in res.timeline:
                print("    %-19s %-14s %-12s %s"
                      % (ev["ts"], ev["event"], ev["who"], ev["detail"]))
        Pal.kv("最终状态", res.final_status + ("（已进入算法迭代闭环）" if res.closed_loop else ""))
        return res

    # ---------------- 收尾 ----------------
    def final_report(self):
        Pal.title("汇总")
        ok, msg, n = self.tc.verify_chain()
        Pal.kv("03 审计哈希链", msg + ("  完整性=%s" % ("通过" if ok else "失败")))
        Pal.kv("03 告警总数", self.tc.summary().get("total"))
        Pal.step("演示检查点")
        all_ok = True
        for name, res in self.results:
            good = res.ok if hasattr(res, "ok") else bool(res)
            all_ok = all_ok and good
            (Pal.ok if good else Pal.fail)(name)
        Pal.step("本次产生的审计条目（最近 12 条）")
        for row in self.tc.audit_rows(limit=12)[::-1]:
            print("    %-19s %-12s %-14s %s"
                  % (row["ts"], row["action"], row["operator"], row["detail"][:44]))
        Pal.rule("=")
        if all_ok:
            print("  演示结果：全部检查点通过")
        else:
            print("  演示结果：存在未通过项，见上面的 [FAIL]")
        Pal.rule("=")
        return all_ok


# ============================================================ 入口
def build_parser():
    ap = argparse.ArgumentParser(
        description="02 -> 03 告警上传与终端处置演示",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", action="append", default=None,
                    choices=["A", "B", "C", "D", "E"],
                    help="只跑指定场景，可重复（默认全部）")
    ap.add_argument("--sandbox", dest="sandbox", action="store_true", default=True,
                    help="沙箱数据目录（默认开启）")
    ap.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                    help="**写真实数据目录**，需同时给 --yes")
    ap.add_argument("--yes", action="store_true", help="确认真实数据目录操作")
    ap.add_argument("--home", default=None,
                    help="沙箱根目录（默认 <本演示目录>\\data\\_demo_sandbox）")
    ap.add_argument("--keep", action="store_true", help="跑完保留沙箱数据")
    ap.add_argument("--operator", default="patrol1",
                    help="处置身份（03 预置账户）：patrol1 / admin / owner ...")
    ap.add_argument("--list-interp", action="store_true", help="只打印解释器信息")
    ap.add_argument("--quiet", action="store_true", help="不打印横幅")
    return ap


def main(argv=None):
    ns = build_parser().parse_args(argv)

    if ns.list_interp:
        print("python        : %s" % sys.executable)
        print("02 目录       : %s" % DIR_02)
        print("03 目录       : %s" % DIR_03)
        print("02 集成接口   : %s" % os.path.join(DIR_02, "integration_api.py"))
        print("03 集成接口   : %s" % os.path.join(DIR_03, "integration_api.py"))
        return 0

    Pal.on = not ns.quiet
    if Pal.on:
        Pal.rule("=")
        print("  12 告警上传与终端处置演示    %s" % _now())
        Pal.rule("=")

    # ---- 数据目录决策 ----
    sandbox_root = None
    if ns.sandbox:
        sandbox_root = ns.home or SANDBOX_DEFAULT
    else:
        if not ns.yes:
            print("\n[中止] --no-sandbox 会写入真实运行数据目录。")
            print("       确认要这么做请追加 --yes；否则去掉 --no-sandbox 用沙箱跑。")
            return 2

    if sandbox_root:
        # 沙箱环境变量必须在 import 02/03 的 config 之前设好；子进程会继承
        os.environ["EXAM_CAMERA_HOME"] = os.path.join(sandbox_root, "02")
        os.environ["EXAM_TERMINAL_HOME"] = os.path.join(sandbox_root, "03")
        home_02 = os.environ["EXAM_CAMERA_HOME"]
        home_03 = os.environ["EXAM_TERMINAL_HOME"]
        if not ns.keep:
            shutil.rmtree(sandbox_root, ignore_errors=True)
        os.makedirs(home_02, exist_ok=True)
        os.makedirs(home_03, exist_ok=True)
        Pal.info("沙箱根目录：%s" % sandbox_root)
    else:
        home_02 = None
        home_03 = None
        Pal.warn("正在使用**真实数据目录**（02/03 的 data\\），演示记录会留在里面")

    try:
        demo = Demo(home_02, home_03, keep_sandbox=ns.keep)
    except UploadError as exc:
        print("\n[环境不满足] %s" % exc)
        return 1

    try:
        demo.preflight()
        devices = demo.prepare_camera()

        wanted = set(ns.scenario or ["A", "B", "C", "D", "E"])
        if "A" in wanted:
            demo.scenario_A(devices, operator=ns.operator)
        if "B" in wanted:
            demo.scenario_B(devices, operator=ns.operator)
        if "C" in wanted:
            demo.scenario_C(devices, operator=ns.operator)
        if "D" in wanted:
            demo.scenario_D(devices, operator=ns.operator)
        if "E" in wanted:
            demo.scenario_E(devices)

        all_ok = demo.final_report()
        if sandbox_root:
            if ns.keep:
                Pal.info("沙箱数据已保留：%s" % sandbox_root)
                Pal.info("想清掉直接删这个目录即可。")
            else:
                shutil.rmtree(sandbox_root, ignore_errors=True)
                Pal.info("沙箱数据已清理（加 --keep 可保留）")
        return 0 if all_ok else 1
    except UploadError as exc:
        print("\n[上传失败] %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\n[中断]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
