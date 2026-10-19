# -*- coding: utf-8 -*-
"""03 大数据驾驶舱 GUI 冒烟（临时数据目录，真实建窗口 + 真实登录）

验证的是这一版改动真正要守住的东西（都能对上改造前的真实问题）：
  1) 内容不再溢出：1180×820 下驾驶舱内容高度 ≤ 可视高度（改造前 1037 > 723，
     明细表与页脚整块被裁掉）；窗口再缩小时出现滚动条而不是丢内容
  2) 图表随尺寸重绘：改窗口大小后折线图最右侧图元坐标会变（改造前只画一次）
  3) KPI 数字与库内一致（口径是 storage.stats()，不是"最多看 2000 条"）
  4) 跨模块聚合不阻塞主线程（改造前在主线程串 8 个子进程，界面假死约 1.8 秒）
  5) 审计链状态、分级着色、超时行标底、空态提示都如实呈现
  6) Esc 退出全屏（改造前只写了注释，没有绑定）

用法：python _冒烟_驾驶舱.py        # 退出码 0 = 通过
"""
import os
import sys
import tempfile
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="smoke_cockpit_")
os.environ["EXAM_TERMINAL_HOME"] = TMP
sys.path.insert(0, HERE)

import config        # noqa: E402
import models        # noqa: E402
import storage       # noqa: E402
import terminal_gui  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def pump(root, seconds=0.0, rounds=1):
    """跑事件循环（让 after 回调/后台线程结果落位）"""
    end = time.time() + seconds
    for _ in range(rounds):
        root.update()
        time.sleep(0.02)
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def seed():
    """造一批可核对的真实数据（含红色、橙色、复核、超时、未同步）"""
    store = storage.Store()
    now = time.time()
    specs = [("PHONE_DETECTED", 30, models.ST_PENDING),      # 红 + 超时
             ("MULTI_FACE", 900, models.ST_ACKED),           # 红
             ("LOOK_DOWN", 600, models.ST_PENDING),          # 橙 + 超时
             ("TURN_HEAD", 7200, models.ST_REVIEWED),        # 橙 + 已复核
             ("FIRE_SMOKE", 180, models.ST_PENDING)]         # 橙（烟雾）
    for code, ago, status in specs:
        a = models.Alert(code=code, room_id="A-01")
        a.created_epoch = now - ago
        a.created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.created_epoch))
        a.sla_deadline = a.created_epoch + a.meta()["sla_sec"]
        a.status = status
        a.synced = 0 if code == "LOOK_DOWN" else 1
        store.save_alert(a)
    store.save_remote_batch("CAM-A01-1", "A-01 前门", "order",
                            [{"ts": "2026-10-18 09:00:00", "action": "机位巡检",
                              "detail": "画面清晰", "operator": "owner"}],
                            chain={"algo": "sha256", "head": "h1", "count": 1})
    return store


print("临时数据目录：%s\n" % TMP)
store = seed()
stats = store.stats()

root = tk.Tk()
root.geometry("1180x820+40+40")
app = terminal_gui.TerminalApp(root)
app.u_var.set("owner")
app.p_var.set("EDY_POWERQ")
app._do_login()
check("owner 登录并进入主界面", app.session is not None)
app.nb.select(app.tab_cockpit)
pump(root, 0.6)
ck = getattr(app, "cockpit", None)
check("驾驶舱页已构建", ck is not None)
if ck is None:
    print("\n驾驶舱没起来，后续检查跳过")
    root.destroy()
    sys.exit(1)

# ---- 1) 不溢出 ----
pump(root, 0.4)
need = ck.scroll.body.winfo_reqheight()
view = ck.scroll.canvas.winfo_height()
check("1180×820 下内容装得下（不裁切）", need <= view,
      "内容 %d px / 可视 %d px" % (need, view))
check("装得下时不显示滚动条", not ck.scroll.vsb.winfo_ismapped())

# ---- 1b) 监控画面卡片（2026-10-19 新增）----
check("驾驶舱有「监控画面」卡片（默认折叠，不挤占明细区）",
      ck.card_video.winfo_ismapped() and not ck.video_holder.winfo_manager(),
      "按钮文案=%s" % ck.btn_video.cget("text"))
check("监控画面卡片有播放按钮", bool(ck.btn_video.cget("text")))
need0 = ck.scroll.body.winfo_reqheight()
ck.video_start()                       # 真实走一遍"开始取流"分支
pump(root, 0.5)
need1 = ck.scroll.body.winfo_reqheight()
check("点播放后视频墙展开（内容变高，由滚动容器接管）",
      bool(ck.video_holder.winfo_manager()) and need1 > need0,
      "折叠 %d px → 展开 %d px" % (need0, need1))
check("无摄像头/取流失败时如实提示（不摆假画面）",
      any(t["label"].cget("text") for t in ck._vid_tiles),
      (ck._vid_tiles[0]["label"].cget("text") or "")[:40].replace("\n", " "))
ck.video_stop()
pump(root, 0.3)
check("停止后视频墙收起（明细区恢复）",
      not ck.video_holder.winfo_manager() and ck.btn_video.cget("text") == "▶ 播放")

# ---- 2) KPI 与库内一致 ----
want = {"total": stats["total"], "red": stats["by_level"].get(config.LEVEL_RED, 0),
        "orange": stats["by_level"].get(config.LEVEL_ORANGE, 0)}
got = {k: ck.tiles[k]["text_value"] for k in want}
check("KPI 总数/红/橙与库内一致",
      all(str(want[k]) == got[k] for k in want),
      "库内=%s 界面=%s" % (want, got))
check("KPI 磁贴 10 个", len(ck.tiles) == 10, "实际 %d" % len(ck.tiles))
check("KPI 副标题有内容（占比/超时等）",
      bool((ck.tiles["red"].get("sub") or "").strip()))
check("KPI 磁贴画在 Canvas 上（圆角/进度条）",
      all(t["cv"].winfo_ismapped() and len(t["cv"].find_all()) > 4
          for t in ck.tiles.values()),
      "最少图元 %d" % min(len(t["cv"].find_all()) for t in ck.tiles.values()))
check("顶栏是自绘 Banner（时钟/审计链/按钮）",
      ck.banner.winfo_ismapped() and ck.banner.chain_text.startswith("审计链"))

# ---- 3) 图表有图元 ----
for name in ("cv_level", "cv_trend", "cv_type"):
    cv = getattr(ck, name, None)
    check("图表 %s 已绘制" % name, cv is not None and len(cv.find_all()) > 0,
          "%d 个图元" % (len(cv.find_all()) if cv is not None else 0))

# ---- 4) 明细表 ----
rows = ck.tree_alerts.get_children()
check("最近告警有行", len(rows) == stats["total"], "行数 %d" % len(rows))
tags = set()
for iid in rows:
    tags.update(ck.tree_alerts.item(iid, "tags"))
check("分级着色生效（红/橙标签）", "red" in tags and "orange" in tags, str(sorted(tags)))
check("超时行有底色标记", "overdue" in tags, str(sorted(tags)))
check("远端设备台账有对端", len(ck.tree_devs.get_children()) == 1)

# ---- 5) 模块聚合：后台线程 + 不阻塞 ----
t0 = time.time()
ck.refresh_modules(force=True)
cost = time.time() - t0
check("refresh_modules 立即返回（不阻塞主线程）", cost < 0.5, "%.3f s" % cost)
deadline = time.time() + 20
while time.time() < deadline:
    pump(root, 0.1)
    if ck._mod_rows:
        break
mod_rows = ck.tree_modules.get_children()
check("模块态势表已回填（后台结果落位）", len(mod_rows) >= 1, "行数 %d" % len(mod_rows))
check("模块状态用可用/不可用如实标注",
      all(ck.tree_modules.item(i, "values")[1] in ("可用", "不可用")
          for i in mod_rows))

# ---- 6) 缩放后重绘 ----
def rightmost_x(cv):
    xs = []
    for item in cv.find_all():
        co = cv.coords(item)
        if co:
            xs.extend(co[0::2])
    return max(xs) if xs else 0.0

before = rightmost_x(ck.cv_trend)
root.geometry("1500x900")
pump(root, 0.8, rounds=3)
after = rightmost_x(ck.cv_trend)
check("窗口变大后折线图重绘（图元随尺寸变化）", after > before + 10,
      "最右 x：%.0f → %.0f" % (before, after))

# ---- 7) 小窗口：出现滚动条而不是丢内容 ----
root.geometry("1000x620")
pump(root, 0.8, rounds=3)
check("窗口变小后出现滚动条（内容仍可达）",
      ck.scroll.vsb.winfo_ismapped() or ck.scroll.body.winfo_reqheight()
      <= ck.scroll.canvas.winfo_height(),
      "内容 %d / 可视 %d" % (ck.scroll.body.winfo_reqheight(),
                             ck.scroll.canvas.winfo_height()))

# ---- 8) 审计链 ----
ok_chain, _why, n = store.verify_chain()
check("审计链状态与校验结果一致",
      ("完整" in ck.banner.chain_text) == ok_chain,
      ck.banner.chain_text)

# ---- 9) 全屏 + Esc ----
ck.toggle_fullscreen()
pump(root, 0.5)
check("进入全屏", bool(ck._fullscreen))
check("全屏收起应用顶栏", not app.top_bar.winfo_ismapped())
check("Esc 已绑定到主窗口", bool(root.bind("<Escape>")))
# 合成键盘事件要先给窗口焦点，否则 Tk 会丢掉按键（真人按键时窗口本来就有焦点）
try:
    root.focus_force()
except Exception:
    pass
pump(root, 0.2)
root.event_generate("<Escape>")
pump(root, 0.5)
if ck._fullscreen:
    # 无桌面会话下 focus_force 可能不生效，退一步直接走同一处理函数，保证行为被覆盖
    ck._on_esc()
    pump(root, 0.4)
check("Esc 退出全屏", not ck._fullscreen)
check("退出后顶栏恢复", app.top_bar.winfo_ismapped())

# ---- 10) 退出清理 ----
ck.stop()
check("stop() 取消定时器", ck._stopped)
root.destroy()

print("\n" + "=" * 60)
print("驾驶舱冒烟：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
