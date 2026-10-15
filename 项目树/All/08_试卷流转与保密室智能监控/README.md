# 试卷流转与保密室智能监控

> 实现《[试卷流转与保密室智能监控.md](试卷流转与保密室智能监控.md)》里的全部能力。
> 本目录是**真代码，可运行、有测试**：**122 项单元测试 + 114 项端到端自检 +
> 86 项网页面板接口实测 + 30 项与 00_集成联动的联动自检**。
> 已接入 `00_集成联动` 总线，与 01 人脸 / 02 画质 / 03 终端互通（见第四节）。
> 涉及**真实摄像头/门禁/红外探测器/电子锁/北斗定位/录像平台/现场传感器**的指标，
> 必须靠现场联调后复测，自检不替代现场验收（见文末「范围与如实说明」）。

---

## 一、快速开始

| 操作 | 方式 |
| --- | --- |
| **安装依赖（首次必做）** | 双击 `安装环境.bat`（与本仓 02/03/11 同一套惯例） |
| 打开监控界面（桌面） | 双击 `启动保密室监控界面.bat`（先登录） |
| 打开网页版 | 双击 `启动网页版.bat`（http://127.0.0.1:5020/） |
| 停止网页服务 | 双击 `停止网页版.bat` |
| 隐藏/显示启动器的黑窗口 | 双击 `切换命令行窗口.bat`，或登 owner 后在系统设置里勾选 |
| 一键体检 | 双击 `运行自检与测试.bat`（122 单元 + 114 自检 + 86 网页 + 30 联动） |
| **与 00_集成联动的联动自检** | 双击 `联动自检.bat`（或 `python tests/linkage_selfcheck.py`） |
| 只跑单元测试 | `python -m unittest discover -s tests -t .` |
| 自检（临时数据目录，不污染） | `python main.py` |
| 自检（真实数据目录） | `python main.py --real` |
| 网页面板接口实测 | `python tests/e2e_web_inproc.py` |

**默认账户（桌面端与网页端同一份，首次运行自动预置）**

| 用户名 | 默认口令 | 角色 | 说明 |
| --- | --- | --- | --- |
| `owner` | `EDY_POWERQ` | 所有者 | 全部权限 + 账户管理 + 审计链 + 隐藏 cmd 开关 |
| `teacher1` | `teacher1` | 管理员 | 日常值守作业；不能删机位、不能管账户、不能看审计 |

> 口令用 **PBKDF2-HMAC-SHA256（120000 轮、16 字节随机盐）** 存成 `pbkdf2$salt$dk`，
> 只落哈希不回显明文。**登录后请尽快改口令**。

---

## 二、对 md 文档的逐条落地

### 一、保密室智能监控（7 项）

| 文档监控项 | 技术手段 | 代码落点 | 已实测 |
| --- | --- | --- | --- |
| 无死角视频 | 6 面覆盖（含柜顶/柜底），双路冗余录像 | `vault.CameraManager.register/coverage/blind_spots`；`config.COVER_FACES`（front/back/left/right/top/bottom）、`FACE_CHECKPOINTS`、`REDUNDANT_CHANNELS_REQUIRED=2` | 六面齐 → 覆盖率 100%、盲区 0；只登记 1 面 → 覆盖率 16.7%、5 处盲区；机位台账逐条校验必拍要点 |
| 门禁管控 | 人脸 + 刷卡/密码双因子，双人双锁电子化 | `vault.AccessControl.factor_ok/request_open/report_abnormal_open`；`FACTOR_BIOMETRIC/FACTOR_CREDENTIAL`、`DUAL_CUSTODY_REQUIRED=2` | 双因子+双人 → 放行；单因子 → 拒绝；单人 → 拒绝；尾随 → 放行但另报 `ACCESS_TAILGATING`；未授权开门 → `DOOR_OPEN_ABNORMAL`，时延 0.006s |
| 入侵探测 | 红外双鉴、门窗磁、震动/开柜检测 | `vault.IntrusionMonitor.trigger/in_armed_window/set_armed`；`config.INTRUSION_KINDS`（ir_dual/door_mag/vibration/safe_open）、`ARMED_WINDOWS=[(18,0,24,0),(0,0,8,0)]` | 四类探测器均可触发；23:00 属布防、12:00 撤防；告警时延 0.009s |
| 试卷柜电子锁 | 电子锁 + 开柜记录 + 柜内状态监测 | `vault.SafeMonitor.open_request/report_state/check_inner`；`config.SAFE_STATES`（closed/open/ajar）、`SAFE_INNER_PROBES`（paper_present/tamper/humidity） | 未授权开柜 → `SAFE_OPEN_ABNORMAL`；柜内无试卷/被撬 → 告警；开柜记录全部留档 |
| 环境监测 | 温湿度、水浸、烟感，异常自动告警 | `vault.EnvMonitor.sample`；`config.ENV_THRESHOLDS`（温度 5–35℃、湿度 20–70%）、`ENV_BINARY_PROBES`（water/smoke） | 38℃/80%/水浸/烟感四项越限全部识别并出告警；24℃/45% 正常不告警 |
| 值守替代 | 巡考机器人定时巡查 + 非接触值守 | `vault.PatrolAssistant.patrol_round/check_due` | 巡查轮次可登记；刚巡查过不报缺位；超间隔 → 报值守缺位 |
| 录像留存 | 建议 ≥6 个月，期间不可删改 | `vault.RecordGuard.add_segment/continuity/redundancy_ok/retention_report`；`RECORD_RETAIN_DAYS=180`、`RECORD_WORM=True` | 7 天录像连续无缺口、双路冗余齐备、留存 180 天、**留存期内段全部加锁（unlocked=0）**；挖 1 段立刻报缺口，补齐后恢复 |

### 二、试卷流转环节（5 项）

| 文档环节 | 措施 | 代码落点 | 已实测 |
| --- | --- | --- | --- |
| 领取/交接 | 扫码/RFID 电子交接，双人签字 + 人脸确认 | `flow.HandoverFlow.handover/pickup/completeness_rate` | 双签+双人脸 → 判定完整、不告警；缺人脸 → `HANDOVER_FACE_MISS`；单人 → `HANDOVER_PERSON_MISS` |
| 押运 | 车辆北斗/GPS 定位 + 车内视频回传 + 路线偏离告警 | `flow.EscortFlow.start_task/report_position/report_video/finish_task`；`flow.point_to_polyline_m`（点到折线距离，米）；`ESCORT_DEVIATE_METERS=200`、`ESCORT_DEVIATE_SLA_SEC=30` | 在规划路线不告警（偏移 11m）；偏离 1.5 万米 → `ESCORT_ROUTE_DEVIATE`，时延 0.013s；视频中断 → `ESCORT_VIDEO_LOST` |
| 电子铅封 | 试卷袋电子封签，异常开启即时告警 | `flow.SealFlow.seal/report/intact_bags/broken_bags`；`SEAL_STATES=(sealed,broken,unknown)` | 封签成功 → 袋状态 sealed、进 intact 列表；上报 broken → `SEAL_BROKEN` 并进 broken 列表 |
| 分发至考场 | 分发记录与考场、时间、责任人一一对应 | `flow.DeliveryFlow.deliver/covered` | 齐备（考场+数量+责任人）不告警；三项缺任一 → `DELIVER_RECORD_MISS`×3 |
| 回收与封存 | 回收扫码核验数量，异常差异即时上报 | `flow.RecycleFlow.recycle/diff_rate` | 30/30 + 封签 → 不告警；30/27 → `RECYCLE_COUNT_MISMATCH`（diff=-3）；未封存 → `RECYCLE_MISSING` |

**全链条追溯**：`flow.FlowManager.bag_trace()` 串起 领取 → 交接 → 押运 → 分发 → 回收 → 封存，逐环节留人/时/物/地点。

### 三、关键指标（5 项）

| 文档指标项 | 要求 | 实测口径 |
| --- | --- | --- |
| 保密室视频覆盖率 | 100%，无盲区（逐机位留档验证） | `CameraManager.coverage()`；**该保密室一台机位都没登记 → 标「样本不足」，不当达标也不当未达标** |
| 开柜/开门异常告警时延 | ≤ 5 秒 | `alarms.latency_sec`，按 `raise_alarm(event_epoch=…)` 实测 `now-事件时刻` |
| 交接记录完整率 | 100% 可追溯（人、时、物、地点） | `HandoverFlow.completeness_rate(room_id=…)`，**按保密室分别统计，不跨房间混淆** |
| 录像留存与完整性 | 留存期内连续可回放，无丢段 | `RecordGuard.continuity()/retention_report()`，相邻段缝隙超容差即判丢段/重叠 |
| 押运路线偏离告警 | 偏离即报，≤ 30 秒 | `alarms.latency_sec`（仅 `ESCORT_ROUTE_DEVIATE`） |

> **样本为 0 一律标「样本不足」，绝不当达标** —— 这是本仓库一贯的如实口径，
> 报告与界面都按此呈现。

### 四、验收证据

`report.EvidenceReport.full_report()` 输出七章节验收报告（对应文档第四节）：
【一】保密室无死角核查（机位台账 + 覆盖率 + 盲区）／【二】门禁与开柜告警联调测试记录／
【三】交接·押运演练记录与台账样例／【四】录像完整性与留存期限配置证明／【五】关键指标／
【六】告警台账／【七】审计哈希链校验。

配套导出 CSV 台账：机位台账、告警、交接、押运、分发、回收。

---

## 三、摄像头实时画面（桌面端 + 网页端）

这是本项目明确要求的能力：**连接摄像头 + UI 中显示画面**。

- **桌面端**：`gui.py` 的「实时画面」页签，多路画面网格；取流走 `camera_stream.FrameReader`，
  界面只轮询 `snapshot()` 拿最新帧，**不做阻塞调用**，所以断流/卡死不会冻结界面。
- **网页端**：`GET /api/stream/<cam_id>?token=…` 输出 `multipart/x-mixed-replace`（MJPEG），
  浏览器 `<img src="...">` 直接显示，无需 WebRTC/RTSP 插件。

`camera_stream.FrameReader` 的「永不卡死」内核（与专题 02 逐字节一致）：

1. 后台线程读帧，界面侧只读快照 —— `cap.read()` 永不出现在 UI 线程；
2. 看门狗判定 `stalled`（超时没新帧即标记，界面显示「画面中断」而不是假死）；
3. `release` 异步执行，避免 `cap.release()` 卡住主流程；
4. 断线走**指数退避重连**，并有线程上限。

**接入真实摄像头**：在「系统设置」页登记机位，取流地址填：
- 本机 USB 摄像头 → 填数字序号 `0` / `1` / `2`（界面「探测本机摄像头」可枚举）
- 网络摄像头 → 填 RTSP，如 `rtsp://用户名:口令@192.168.1.64:554/Streaming/Channels/101`
- 本地视频文件 → 直接填路径（便于离线演示/联调）

### 取流内核与其他项目一致（仓库约定）

`camera_stream.py` 在本仓是**多份同步**的公共内核 —— 01 / 02 / 03 / 08 / 11 各放一份，
**五份逐字节完全相同**（`md5 = 3d907de3904cc118842ed93835c9f74e`，均 23021 字节）。
`main.py` 自检里有一条硬校验会实时比对 md5，任何一份被单独改动都会 FAIL。

**调用方式也与其他项目同构**（对外只有三个概念）：

| 场景 | 调用 | 本项目出现位置 |
| --- | --- | --- |
| 持续取流（界面/MJPEG） | `camera_stream.FrameReader(url, width=, height=, fps=)` → `start()` → 轮询 `snapshot()` | `vault.py`、`gui.py`、`网页监控面板/server/app.py` |
| 单帧抓取（抓帧留证） | `camera_stream.open_capture(url, timeout=4.0)` → 用完 `release_async(cap, why)` | `vault.py` |
| 释放 | `camera_stream.release_async(cap, why)` | `vault.py` |

界面层**永不做阻塞调用**：`cap.read()` 只在后台线程里跑，UI 只轮询快照。

桌面端六个页签的实拍截图见 **`界面预览/`**（用合成视频当取流源跑出来的真实界面，
不是设计稿）：`01_实时画面` / `02_保密室监控` / `03_试卷流转` / `04_告警中心` /
`05_关键指标` / `06_系统设置`。实时画面那张里机位状态显示的是「播放中 / fps」。

> 桌面端默认账户：`owner / EDY_POWERQ`（所有者）。双击 `启动保密室监控界面.bat` 即可。

---

## 四、与其他项目的联动（00_集成联动）

本项目已接入 `00_集成联动` 的联动总线。四套系统用**子进程 + JSON** 互相调用
（各自都有 `config.py`/`models.py`，同进程 import 必然撞名，子进程最稳）。

### 4.1 调用契约（与 01/02/03 完全一致）

本项目根目录的 `integration_api.py` 提供：

```bat
python integration_api.py --op <name> --args "<json object>"
# 最后一行输出一个 JSON：{"ok":true, ...}；异常兜底 {"ok":false,"reason":"..."}
```

| op | 入参 | 作用 |
| --- | --- | --- |
| `status` | `{}` | 概览（房间/机位/试卷袋/告警/审计链/数据目录） |
| `rooms` | `{}` | 保密室清单（机位、覆盖率、盲区、环境采样） |
| `vault_status` | `{room_id}` | 某保密室**七项能力体检** + 覆盖率 + 盲区 |
| `cameras` | `{room_id}` | 机位台账（朝向/通道/在线/台账缺项） |
| `raise_alarm` | `{code, room_id, source_kind, source_id, detail, level, event_epoch}` | 往本项目告警总线推一条告警（带时延核算） |
| **`ingest_alert`** | `{code, room_id, device_id, credibility, level, ...}` | **同 03 语义**：接收外部告警，按机位画质可信度降权（<0.6 红降橙） |
| `bag_trace` | `{bag_id}` | 一个试卷袋的**全链条追溯**（人/时/物/地点） |
| `bags` | `{room_id}` | 试卷袋清单 |
| `handover_rate` | `{room_id}` | 交接记录完整率（关键指标③） |
| `kpi` | `{room_id}` | **五项关键指标**实测值 |
| `apply_credibility` | `{device_code, factor, room_id}` | 把 02 给出的机位可信度落到本项目机位台账 |

### 4.2 联动链（已实测跑通 30/30）

```
① 01/02/03/08 四套连通性（各自 --op status）
        ↓
② 02 登记机位 + 采集基准图
        ↓
③ 02 现场诊断：正常画面 -> 可信度 ×1.0；模糊画面 -> BLUR + 可信度 ×0.5
        ↓
④ ★ 02 -> 08：把机位画质告警送进本项目告警总线
     低可信度（<0.6）机位的红色告警【自动降级为橙色】+ 标注原因
     同时把该机位可信度写进本项目机位台账（供报告标注「画质降权」）
        ↓
⑤ 08 自身：机位台账 / 六面覆盖率 / 交接完整率 / 五项 KPI / 试卷袋全链条追溯
        ↓
⑥ 03 与 08 的降权语义一致性对照（0.4 -> 都降橙；1.0 -> 都保持红）
```

### 4.3 怎么用

```bat
:: 在本目录一键跑联动自检（临时沙箱，不污染数据）
联动自检.bat

:: 或在 00_集成联动 目录里统一查看四套状态
cd ..\00_集成联动
python -c "import linkage; print(linkage.Linkage().status())"
```

在别的程序里调本项目：

```python
import sys; sys.path.insert(0, r"...\项目树\All\00_集成联动")
import linkage
lk = linkage.Linkage(sandbox_root=r"D:\tmp")     # 08 默认进沙箱，可用 sandbox_08=False 关闭
lk.vault_status("保密室A")                        # 07 项能力体检
lk.secure_kpi("保密室A")                          # 五项关键指标
lk.secure_alert("CAM_BLUR", "保密室A", device_code="CAM-A01")   # 带 02 可信度的告警
lk.bag_trace("BAG-001")                           # 试卷袋全链条追溯
lk.handover_rate("保密室A")                        # 交接完整率
```

---

## 五、账户、角色与权限

桌面端与网页端 **`import web_accounts` 同一份实现、读写同一个 SQLite kv 键**，所以同一账号两边口令必然一致。

| 能力 | owner | admin | guard | escort | auditor | viewer | 动作键 |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | --- |
| 查看台账/事件/指标/报告 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `view` |
| 看实时画面 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | `live_view` |
| 机位登记/修改 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | `device_manage` |
| **删除机位** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | `device_delete` |
| 门禁/开门操作 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | `access_operate` |
| 入侵/环境/巡查操作 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | `monitor_operate` |
| 试卷柜操作 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | `safe_operate` |
| 试卷流转操作 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | `flow_operate` |
| 押运操作 | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | `escort_operate` |
| 告警处置 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | `alarm_handle` |
| 导出报告 | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | `report_export` |
| 修改本人口令 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | `change_own_pwd` |
| **账户管理** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | `account_manage` |
| **查看审计链** | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | `audit_view` |
| **隐藏命令行窗口开关** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | `hide_cmd_toggle` |

越权不是「藏按钮」就算：桌面端每个受控入口先 `Session.can()` 判定；网页端由 `@require([...])`
装饰器在**服务端**返回 **401（未登录）/ 403（无权限）**。实测已验证 admin 访问
`/api/accounts`、`/api/audit`、`DELETE /api/cameras/...` 均得 403。
**防越级**：只能管理**严格低于**自己等级的角色（owner 不能建 owner）。

---

## 六、告警与审计

- **告警时延**：`raise_alarm(…, event_epoch=事件时刻)` 计算并存入 `alarm.latency_sec`，
  专供 ≤5s / ≤30s 两项指标的现场核查（这是"指标能不能证明"的关键，不是拿发生时间糊弄）。
- **告警去重**：按 `code + source_kind + scope + status=pending + 时间窗（60s）` 走 SQL 精确去重，
  传感器持续上报不刷屏，同时**不跨保密室错并**（不同房间各自一条）。
- **告警分级**：红（立即处置）/ 黄 / 蓝；支持单条确认、处置、批量确认红色告警。
- **哈希链审计**：`AuditChain.calc(prev_hash, ts, action, detail, operator)` → SHA256 串联。
  `verify()` 能定位**首个断点**（实测：篡改中间一条 → 报「第 N 条内容与哈希不符」）。

---

## 七、目录结构

```
08_试卷流转与保密室智能监控/
├─ 试卷流转与保密室智能监控.md   ← 需求文档（本实现的依据）
├─ README.md                      ← 本文件
├─ 使用说明.md                    ← 面向使用者的操作与现场接入说明
├─ 安装环境.bat / 安装环境.py     ← 安装依赖（与本仓 02/03/11 同一套惯例）
├─ config.py                      ← 全部阈值/枚举/角色权限（照文档常量化）
├─ models.py                      ← 数据模型 + 审计哈希链 + 告警/摄像机/事件
├─ storage.py                     ← SQLite 存取（含告警去重用的公开查询入口）
├─ web_accounts.py                ← 账户与权限（桌面端 & 网页端同源）
├─ alarm_hub.py                   ← 告警中心（时延实测 / 去重 / 确认处置）
├─ camera_stream.py               ← 取流内核（与 01/02/03/11 **逐字节一致**）
├─ vault.py                       ← 保密室监控七项
├─ flow.py                        ← 试卷流转五环节
├─ report.py                      ← 五项关键指标 + 七章节验收报告 + 台账导出
├─ gui.py                         ← 桌面端界面（六页签，含实时画面）
├─ main.py                        ← 端到端自检（114 项）
├─ integration_api.py             ← 集成接口（供 00_集成联动 子进程调用）
├─ 网页监控面板/
│  ├─ server/app.py               ← Flask 后端（端口 5020，含 MJPEG 流）
│  └─ web/index.html              ← 单文件 SPA（登录 + 六页签 + 实时画面）
├─ tests/
│  ├─ __init__.py                 ← 测试隔离（EXAM_SECURE_HOME 指向临时目录）
│  ├─ test_core.py                ← 122 项单元测试
│  ├─ e2e_web_inproc.py           ← 86 项网页面板接口实测
│  ├─ linkage_selfcheck.py        ← 30 项与 00 集成联动的联动自检
│  └─ serve_web.py                ← 分离进程方式启停网页后端
├─ 启动保密室监控界面.bat / 启动网页版.bat / 停止网页版.bat
├─ 切换命令行窗口.bat / 运行自检与测试.bat / 联动自检.bat
├─ 界面预览/                      ← 桌面端六页签实拍截图（见第三节）
└─ data/                          ← 运行时数据（可用 EXAM_SECURE_HOME 重定向）
   ├─ secure.db                   ← SQLite
   ├─ evidence/ captures/ seals/ handovers/   ← 留证 / 抓帧 / 封签 / 交接凭证
   ├─ reports/ exports/           ← 报告与 CSV 台账
   └─ logs/
```

---

## 八、范围与如实说明（重要）

1. **本机没有可用摄像头**。自检用 OpenCV 合成的本地视频当取流源，
   验证的是「取流 → 读帧 → 界面渲染 / MJPEG 输出」整条链路**真的通**（实测 seq>90、
   帧尺寸 (360,640,3)、抓帧留证落盘、网页 MJPEG 确实吐出 `image/jpeg` 分片）。
   **真实摄像头的在线率、画面质量、夜视效果必须现场核验。**
2. **没有真实门禁 / 红外双鉴 / 门窗磁 / 震动 / 电子锁 / 水浸 / 烟感 / 北斗终端 / 录像平台**。
   这些设备的事件通过接口注入，判定逻辑与告警时延是**真算的**，
   但"设备本身工作是否正常"必须现场联调。
3. **告警时延是程序内计时**（事件时刻 → 落库），不是端到端物理链路时延。
   现场要拿到真实 ≤5s / ≤30s，需要用真实设备触发并用现场时钟复核。
4. **录像留存 180 天 + WORM** 是**策略配置与校验逻辑**已实现（留存期内段全部加锁、
   到期前不可删改），实际录像文件是否真的 180 天连续可回放，取决于现场录像平台。
5. **五项关键指标中样本为 0 的一律标「样本不足」**，绝不当达标 ——
   报告里看到「样本不足」就是提示需要现场触发样本后复测。
6. 自检与单元测试都用**临时数据目录**，不污染正式 `data/`。
7. **本目录 7 个 `.bat` 一律以 GBK(936) 编码 + CRLF 行尾保存**（见「快速开始」下方提示）。
   这是 Windows `cmd` 解析中文批处理的硬性要求：若用 UTF-8 或 LF-only 保存，
   `cmd` 按 GBK 解析多字节汉字会撕裂引号/括号配对，出现
   `'ncv-python' 不是内部或外部命令` 这类碎片化报错。**改动后务必保持 GBK + CRLF。**

---

## 九、测试结果（本次实测）

| 套件 | 项数 | 结果 |
| --- | --- | --- |
| 单元测试 `tests/test_core.py` | 122 | ✅ 全部通过（16.8s） |
| 端到端自检 `main.py` | 114 | ✅ 全部通过（25.1s） |
| 网页面板接口实测 `tests/e2e_web_inproc.py` | 86 | ✅ 全部通过 |
| **与 00 集成联动的联动自检** `tests/linkage_selfcheck.py` | 30 | ✅ 全部通过 |

其中网页接口实测覆盖：未登录 401（含 MJPEG）、登录失败/成功、`/api/me` 身份、
**admin 越权 403**（账户/审计/删机位）、六面登记与覆盖率、门禁三类判定、
**开/关门告警时延 0.005s**、红外/开柜/环境告警、录像连续性与 180 天 WORM、
交接/铅封/押运/分发/回收全环节、**押运偏离时延 0.003s**、五项 KPI、
报告落盘 + 6 份 CSV、审计链校验、**MJPEG 真实吐帧**、告警去重、口令长度校验。

联动自检覆盖：四套连通性、02 登记机位与基准图、02 画面诊断（正常 ×1.0 / 模糊 BLUR ×0.5）、
**02→08 告警接入与低可信度红降橙**、02 缺陷清单透传、本项目七项体检 / 六面盲区 /
交接完整率 / 五项 KPI / 试卷袋追溯、**03 与 08 降权语义一致性对照**。

> 取流内核一致性由 `main.py` 的硬校验保证：实时比对 `camera_stream.py` 与本仓
> 01/02/03/11 四份副本的 md5，不一致即 FAIL。

> 桌面端另有一条**界面接线冒烟检查**（`main.py` 自检项 `5.1`）：在子进程里真实构建
> `App` → `LoginFrame` → `MainFrame` 并渲染数帧，任一处接线错误即 FAIL。
> 该检查是为一个已修复的缺陷补的回归防线 —— 早先 `App` 把 **Tk 窗口**当外壳对象
> 传给了子框架（子框架读 `master.store`），导致**界面双击直接报 `AttributeError` 打不开**，
> 而单元测试与自检当时都不覆盖 GUI，问题才得以漏网。
