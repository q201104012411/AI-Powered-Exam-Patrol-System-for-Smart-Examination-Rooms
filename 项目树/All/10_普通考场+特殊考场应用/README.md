# 10 普通考场 + 特殊考场应用

> 所属：项目六 · 智能化装备与考生数据保护 ｜ 专题 10
> 方案原文：[`普通考场应用.md`](普通考场应用.md) ＋ [`特殊考场应用.md`](特殊考场应用.md)
> 一句话：把「普通考场」和「特殊七类考场」的**配置差异、准入判据、分级告警、数据分域、
> 验收指标**落成**一套平台、多种模板**的可运行实现，并与 01/02/03/06 双向联动。

---

## 一、快速开始

| 操作 | 命令 |
| --- | --- |
| **启动图形界面（零黑窗口）** | 双击 `启动考场应用界面.bat`，或 `pythonw launcher.py` |
| 先铺一套演示数据（9 个考场，含特殊七类） | `python 演示数据.py`（只验证不落库：加 `--sandbox`） |
| 端到端自检（**210 项**） | 双击 `运行自检.bat`，或 `python 自检.py` |
| 启动前两关自检 | 双击 `启动前自检.bat`（端到端 + 界面无头） |
| 界面无头自检（**36 项**） | `python 界面自检.py` |
| 真实数据目录只读体检 | `python 自检.py --real` |
| 不进联动（只跑本模块） | `python 自检.py --nolink` |
| 体检"有没有污染别的模块" | 双击 `检查是否污染其他模块.bat` |

> 界面用 **`pythonw.exe`** 启动，**不出现黑色命令行窗口**；启动失败会弹对话框
> 并把完整堆栈写进 `data\logs\launch.log`，**绝不会"双击了没反应"**。
>
> 自检默认把**本模块数据目录重定向到临时目录**，并把跨系统调用重定向到沙箱，
> **绝不往 01/02/03/06 的真实运行库写测试数据**（`运行自检.bat` 里也是这么做的）。

---

## 二、方案 -> 代码：逐条对应

### 2.1 《普通考场应用.md》

| 方案条目 | 落地位置 | 怎么验证 |
| --- | --- | --- |
| 第二节 端侧点位配置（前部全景 / 侧后特写 / 通道 / 拾音器 / 备用） | `config.ROOM_TYPES["standard"]["points"]` | 自检 `[2]` 普通考场套模板：点位 5 个 |
| 第三节 AI 识别清单 **12 + 8 + 20 + 4 = 44 种** | `config.BEHAVIORS`（另加机考 4 + 口试 3） | 自检 `[1]` 行为清单 12/8/20/4 = 44；`[2]` 列声明 |
| 第三节 疑似行为提示率 ≥ 98.5%、符合教育部行为分类参考 | `config.TARGETS` / `config.MOE_CLASS_MAP` | 自检 `[1]` 指标口径与方案一致 |
| 第四节 橙色 / 红色分级与推送对象 | `config.ALERT_ROUTING` + `behavior.push_targets` | 自检 `[7]` 手机使用 -> 红色；推送含巡考员与上级平台 |
| 第四节 流程图（识别→分级→提示→复核→留痕→回流→误报闭环） | `alerting.raise_behavior_alert` / `resolve_alert` | 自检 `[7]`、`[8]` 复核回流 + 误报 `closed_loop=True` |
| 第五节 1 逐考场标定 + 标定记录 | `admission.set_calibration` + `CALIBRATION_MISSING` | 自检 `[4]` 未标定 -> 判缺陷 |
| 第五节 3 座位表与考生编号映射（**仅编号不显姓名**） | `admission.set_ledger` + `LEDGER_NAME_EXPOSED` | 自检 `[4]` 塞姓名即判阻断 |
| 第五节 4 时钟同步偏差 ≤ 1 秒 | `admission.check_clock` | 自检 `[4]` 2.5 秒 -> 判缺陷 |
| 第五节 5 核查前置（开考前按专题 02 完成质量自检） | `admission.selfcheck_points`（调 02） | 自检 `[4]`、`[6]` |
| 第六节 验收指标 ≥95% / ≥98.5% / ≤5% / ≤3s / ≥99% / 100% 无盲区 | `config.TARGETS` + `kpi.py` | 自检 `[11]` 达标/未达标/缺失三分 |

### 2.2 《特殊考场应用.md》

| 方案条目 | 落地位置 | 怎么验证 |
| --- | --- | --- |
| 第一节 七类特殊考场的配置/算法/合规差异 | `config.ROOM_TYPES`（backup/isolation/accessible/computer/oral/large/classified） | 自检 `[2]` 七类逐类生成差异项 |
| 备用考场：快速启用、1 人 1 机位、提高特写权重 | `params.closeup_weight=1.15`、`caps.fast_enable` | 自检 `[2]` 备用考场关键能力已识别 |
| 隔离考场：独立机位/通道/通风、远程监考、医生协同、**健康信息单独存储不混库** | `data_domain=health`、`caps.remote_invigilate/doctor_link`、`privacy.py` | 自检 `[5]`、`[9]` 健康域独立分区 + 跨域拦截 |
| 无障碍考场：低位核验、加宽通道、语音+文字、**免刷脸替代核验**、人工核验同效 | `points`（人工核验工位 -> `alt_verify`） | 自检 `[5]` 去掉人工核验工位 -> `ALT_CHANNEL_MISSING` |
| 机考考场：双机位、屏幕行为审计、网络日志、作答内容独立保密 | `points`（屏幕行为审计代理）、`data_domain=exam_content` | 自检 `[5]` 关掉屏幕审计 -> `SCREEN_AUDIT_MISSING` |
| 口试考场：高保真录音 ≥ 48kHz、行为权重降低 | `params.audio_sample_rate_min`、`algo_weights` | 自检 `[5]` 采样率不足 -> `AUDIO_QUALITY_LOW` |
| 大教室：多机位阵列、分区管理、**跨机位去重计数** | `params.dedup_required`、`caps.dedup` | 自检 `[5]` 未验证去重 -> `DEDUP_UNVERIFIED`；`[11]` 去重准确率 |
| 涉密考场：**全离线部署、数据不出域、本地运行不上云** | `params.require_offline/no_cloud`、`data_domain=classified` | 自检 `[5]` 未做全离线自检 -> `OFFLINE_UNVERIFIED` |
| 第二节 1 **一套平台、多种模板**（一键套用，避免逐场手工调参） | `rooms.apply_template` + `template_diff` | 自检 `[2]`、`[14]` apply_template |
| 第二节 2 **降级可用**（1 台摄像机 + 1 条链路也能巡考并留痕） | `rooms.degrade_to_minimal` + `MINIMAL_ALLOWED_TYPES` | 自检 `[3]` 降级只裁期望点位、必需点位一个不删 |
| 第二节 3 **人工兜底**（智能只作增强） | 所有模板 `identity_mode`；无障碍 `manual-first` | 自检 `[2]` 无障碍行为清单独立于普通口径 |
| 第二节 4 **分级告警**（隔离/涉密误报容忍度更低） | `params.confidence_min`（隔离 0.70 / 涉密 0.75） | 自检 `[2]` 门槛比较 |
| 第二节 5 **数据分域**（禁止跨域汇聚） | `privacy.can_access/check_access/write_record` | 自检 `[9]` 越权写入被拦 + `access_denied` 留痕 |
| 第三节 隔离考场典型操作流程 | `gui.py` 准入自检页 ①~⑤ | `python 界面自检.py` 全流程 |
| 第四节 差异项（去重 ≥95% / 替代通道强制+演练 / 分域强制 / 全离线 / 最小可用配置） | `kpi.derive_room_metrics` + `KPI_VARIANTS` | 自检 `[11]`、`[12]` 报告单列差异项 |
| 第五节 风险提示（临时启用漏自检、DPIA 前置、无障碍公平性） | 强制流程 `FLOW_OUT_OF_ORDER`、`DPIA_MISSING`、演练强制 | 自检 `[4]`、`[5]`、`[10]` |

---

## 三、跟其他专题怎么联动（**不改动它们的任何一行代码**）

本模块**只复用** `00_集成联动/linkage.py` 已经做好的"子进程 + 最后一行 JSON"协议，
不复制它的实现、不修改 01/02/03/06 的文件：

```
                        ┌──────────────────────────────┐
                        │  10 普通/特殊考场应用         │
                        │  模板 → 准入自检 → 分级告警   │
                        └───────────┬──────────────────┘
                                    │ linkage_client.py
                                    │ （复用 00_集成联动 Linkage）
      ┌──────────────┬──────────────┼──────────────┬──────────────┐
      ▼              ▼              ▼              ▼              ▼
   01 人脸核验    02 画质诊断    03 智能终端    06 智能安检    00 联动总线
   「是不是本人」 「画面可信吗」 「怎么处置留痕」 「门口拦住没」  （协议/沙箱/进程树）
```

| 方向 | 本模块做什么 | 用到的对端 op |
| --- | --- | --- |
| 10 -> 02 | 逐机位画质自检、取告警可信度系数、登记考场点位进机位台账 | `credibility` / `inspect` / `devices` / `save_device` |
| 10 -> 03 | 分级告警送终端（带画质系数，低可信度红色会自动降级）、复核结论回流 | `ingest_alert` / `review_alert` / `list_alerts` |
| 10 -> 01 | 人脸核验作为**增强**（无障碍场景明确拒绝把它当唯一准入） | `verify` |
| 10 -> 06 | 安检过检 / 复检结案（门口阻断与考场巡考衔接） | `ingest_pass` / `reinspect` |
| 10 对外 | 供 00 联动与其他专题调用（31 个 op） | 见 `integration_api.py` |

**两个必须知道的接口细节**（都是对端既有行为，不是本模块的补救）：

1. 03 的复核规则是「**红色预警必须先确认（ack），再填写复核结论**」，
   而 03 对外只开了 4 个 op，**没有 ack 的 op**。本模块因此用 03
   **自己的业务对象**（`storage.Store()` + `alerts.AlertHub().ack(...)`）走一次正规确认——
   与 03 界面点"确认"是同一个方法、同一张表、同一条审计，**03 代码零改动**。
   见 `linkage_client.ack_alert`。
2. 02 对**未知机位**的回答是"未知机位，不降权"（factor=1.0）。如果只看系数，
   一个填错的机位编码也会"看起来通过"——那就是假通过。所以本模块额外读一次
   02 的机位台账（`devices`），把"台账里根本没有这个机位"单独判为
   **`CAMERA_UNREGISTERED` 阻断**。见 `linkage_client.camera_registered_codes`。

---

## 四、设计上最要紧的几条（"不假通过"）

1. **对端不可用就如实标未检**，绝不假装自检通过：
   02 挂了 -> `LINKAGE_UNAVAILABLE` 阻断；机位没登记编码 -> `QUALITY_UNCHECKED` 阻断。
   这正是方案第五节点出的"设备没自检、角度没标定导致漏检"的根源。
2. **重跑准入自检不会清空已办事项**：已套过模板时只重算能力缺口，**不重套模板**
   （重套会把标定记录、覆盖核查、替代通道等现场记录抹掉）。自检 `[4]` 专门盯这条。
3. **特殊考场不降标准也不降门槛**：降级只裁"期望点位"，必需点位一个不删；
   隔离/涉密即使降到最小可用配置，`DPIA`、数据分域、远程在岗仍然阻断。
4. **特殊考场不按普通口径硬判**：无障碍免肢体类行为、口试免交头接耳类行为
   （`rooms.BEHAVIOR_EXCLUSIONS`），并且机考屏幕行为只在机考考场启用。
5. **指标无证据就写"缺失"**：`kpi.py` 不填默认值；合成/演示数据必须带 `data_source`
   标注并在报告里单列说明（与仓库 README 的"如实说明"口径一致）。
6. **跨域访问默认拒绝**：隔离域必须走独立授权；越权访问也留痕（`access_denied`）。
7. **合规动作都要双人/理由**：DPIA 不接受空评估人、不接受自评自批；
   销毁必须操作人 + 见证人 + 理由，且出具销毁记录。
8. **"已办事项"和"缺设备"分开说**：模板里的必需能力分两类——
   `point`（缺实体点位/开关，如屏幕审计代理、高保真录音）和
   `job`（还有一件事没做，如 DPIA、演练、离线自检、去重验证、医生协同）。
   后者在刚套完模板时必然是"缺"，那是正常的（那件事还没做），
   缺陷码给 `CAP_JOB_PENDING` 并附**去哪做**的提示，不会被误当成设备故障。
9. **界面不冻结**：所有跨系统调用（02 画质/台账、03 告警）都丢到工作线程，
   主线程只负责渲染；一个考场 4~6 个机位的自检要 2~6 秒，
   放主线程会被 Windows 判定"未响应"（用户以为程序挂了）。

---

## 五、目录

```
10_普通考场+特殊考场应用\
├── 启动考场应用界面.bat        一键开界面（纯 ASCII + CRLF，pythonw 零黑窗口）
├── 运行自检.bat                端到端自检（210 项）
├── 启动前自检.bat              端到端 + 界面无头 两关
├── 检查是否污染其他模块.bat     体检 01/02/03/06 真实库有没有被写脏
│
├── 普通考场应用.md / 特殊考场应用.md      方案原文（未改动）
│
├── config.py            8 类考场模板、51 种行为、指标口径、缺陷目录、数据域
├── models.py            数据对象（Room / CheckRun / AlertEvent / Partition / AuditLog …）
├── storage.py           SQLite 10 张表 + 哈希链审计
├── rooms.py             ★模板引擎：一键套用、差异项、能力缺口（point/job 两类）、
│                        最小可用配置降级
├── admission.py         ★准入判据：强制流程、点位、覆盖、时钟、座位建模、在岗、合规；
│                        机位自动登记进 02 台账；画质结论来自 02
├── behavior.py          行为清单、考场适用性、置信度修正（类别权重/瞬时降权/画质）
├── alerting.py          ★分级告警：抑制定级、推送分流、送 03、复核回流、误报闭环
├── kpi.py               验收指标（达标/未达标/**缺失**三分，差异项按考场类型）
├── privacy.py           数据分域、独立授权、DPIA/演练/离线/医生协同/日志代理/销毁
├── report.py            验收证据报告（Markdown + CSV，无证据写缺失）
├── linkage_client.py    ★对 00_集成联动 的调用客户端（对端不可用即如实降级）
├── integration_api.py   对外集成接口（31 个 op，子进程 + JSON）
├── gui.py               ★图形界面（7 页签）；耗时动作全部走后台线程，界面不冻结
├── launcher.py          ★启动器（pythonw 入口 + 启动前自检 + 失败弹窗/写日志）
├── 演示数据.py           一键铺 9 个考场的完整演示场景（全部标注 synthetic）
├── 自检.py              端到端自检（15 组 210 项）
├── 界面自检.py          界面无头自检（36 项，含"有没有写脏真实目录"的前后快照比对）
└── 工具_检查其他模块是否被污染.py   只读体检脚本
```

数据目录：`data\`（`rooms.db` + `logs\` + `reports\` + `domains\<域>\<考场>\`），
可用环境变量 `EXAM_ROOM_HOME` 或 `room_home.txt` 重定向（与 01/02/03/06 同款做法）。

---

## 六、代码用法

```python
import sys; sys.path.insert(0, r"...\项目树\All\10_普通考场+特殊考场应用")
import config, storage, rooms, admission, alerting, kpi, privacy, report

store = storage.Store()

# 1) 建档 + 一键套模板（隔离考场）
room = rooms.upsert_room(store, "隔离考场-01", config.TYPE_ISOLATION,
                         operator="考务管理员", apply_reason="发热考生",
                         seats=4)
res = rooms.apply_template(room, operator="考务管理员", store=store)
print(res["diff"]["items"])          # 相对普通考场的差异项，逐条列给你
print(room.caps_missing)             # 必需能力缺口（不补齐不许启用）

# 2) 登记点位 + 准入自检 + 启用
for i, p in enumerate(room.points):
    p["device_code"] = "CAM-ISO-%d" % (i + 1)
rooms.recompute_caps(room)
admission.set_invigilators(store, room, [], remote={"name": "远程监考甲"})
admission.set_ledger(store, room, [{"seat_no": "01", "candidate_id": "C001"}])
admission.set_calibration(store, room)
privacy.ensure_partition(store, room)                      # 健康信息独立分区
privacy.do_dpia(store, room, operator="考务管理员",
                assessor="数据保护专员", conclusion="单独分区、最小必要、15 天销毁")
got = admission.arm(store, room)                            # 不通过就拦住并说明原因
print(got["decision"], got["blockers"])

# 3) 分级告警（自动定级 + 画质降权 + 同类抑制 + 送 03）
r = alerting.raise_behavior_alert(store, room, "B_USE_PHONE", 0.97,
                                  device_code="CAM-ISO-1", seat_no="01",
                                  operator="考务管理员")
print(r["level_name"], r["pushed_to"], (r["external"] or {}).get("alert_id"))
alerting.resolve_alert(store, r["event"], "false_positive",
                       reason="是手机壳反光", operator="巡考员甲")   # 误报闭环

# 4) 验收指标 + 报告
run = kpi.new_run(room, operator="考务管理员", data_source="real",
                  observe_total=40, observe_hit=40, observe_correct=38,
                  observe_false=1, response_sec=2.4)
ev = kpi.evaluate(store, room, run=run)
print(ev["summary"]["verdict"])       # 达标 / 未达标 / 缺失 三分
ok, info = report.write_report(store)
print(info["md"], info["csv"])
```

命令行调其他专题（等价于别的专题调本模块）：

```bat
python integration_api.py --op templates --args "{}"
python integration_api.py --op admit --args "{\"room_id\":\"RMxxxx\"}"
python integration_api.py --op route_alert --args "{\"room_id\":\"RMxxxx\",\"behavior\":\"B_USE_PHONE\",\"confidence\":0.97}"
python integration_api.py --op compliance --args "{}"
python integration_api.py --op report --args "{}"
```

---

## 七、如实说明

1. **本模块不做画质算法，也不做人脸算法**。画质结论来自专题 02、人脸结论来自专题 01、
   告警处置与留痕由专题 03 承担。本模块负责的是"考场该不该放行、告警该报几级、
   数据该分到哪个域、指标达标了没有"——这是专题 10 该管的事。
2. 自检里的**行为数据是脚本化注入的合成数据**（没有真实考场画面时只能如此），
   一律带 `data_source=synthetic` 标注；`kpi.py` 与验收报告都会写明
   "不等于真实考场准确率，真实指标必须用考场现场样本"。
   接入真实摄像头后走 02 取流 + AI 识别，指标才会变成 `real`。
3. 联动是**同机进程间调用**（子进程 + JSON）。搬到多机部署时把各模块的
   `integration_api.py` 包成 HTTP 服务即可，本模块的编排逻辑不用改。
4. `01` 的核验在传"文件来源"时按严格模式返回 `LIVENESS_UNAVAILABLE`（防翻拍），
   这是**设计如此**；要活体结论得走 01 自己的摄像头通道。
5. 无障碍考场"替代核验通道"的判定只认**人工核验工位 / 已启用替代通道**：
   语音文字提示、低位设备都**不算**——因为它们替代不了"核验"这件事，
   拿它们顶替就正好落进"因技术原因拒考"的坑里。
6. `00_集成联动` 自身自检目前 **31/32**：失败项是 `01 返回核验结论 -> None / None`
   （专题 01 的人脸库未建档所致，属既有状态）。本模块的 `[15]` 只做**可达性**判定，
   不把 01 的业务返回算作本模块的通过条件，也不改动 01 的任何文件。
