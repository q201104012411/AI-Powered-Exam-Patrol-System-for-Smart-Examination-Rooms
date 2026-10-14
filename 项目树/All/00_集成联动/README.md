# 00 集成联动 · 多套系统互相调用

把各套系统串成一条链：**谁（01）+ 画面可不可信（02）+ 有没有作弊行为模式（作弊识别）+ 怎么处置（03）+ 门口拦不拦得住（06）**。

| 系统 | 回答的问题 | 提供的能力 |
| --- | --- | --- |
| **01 人脸识别与身份核对** | 这个人**是不是本人** | 1:1 / 1:N 人脸核验、证件信息带出、哈希链审计 |
| **02 摄像头核查与视频质量诊断** | 这个机位**画面可不可信** | 无参考画质诊断、基准图比对（遮挡/视角偏移）、工单闭环、**告警可信度系数** |
| **作弊识别**（`作弊识别\` + `cheat_bridge.py`） | 这段动作序列**像不像在获取/传递答案** | 加权风险分数 + 滑动窗口 + **持续超阈才告警**（单帧瞬时动作自动抑制）；摄像头路径下手部/物体信号**如实标注为不可得** |
| **03 智能终端操作** | 告警**怎么处置、怎么留痕** | 分级展示、红色强制确认、复核留痕、断网补传、考场时钟、终端权限 |
| **06 智能安检与手机作弊器材检测** | 手机/耳机**拦没拦住** | 安检门判据引擎、复检处置、手机集中保管、特殊考生人工通道、检出率证据 |

## 一、一键验证联动

| 操作 | 命令 |
| --- | --- |
| 联动端到端自检（**39 项**） | 双击 `启动联动自检.bat` 或 `python main.py` |
| 用真实数据目录（不建临时沙箱） | `python main.py --real` |

> 自检默认把 **02/03/06 的数据目录重定向到临时目录**（不污染真实运行数据）；
> **01 用真实数据**——否则人脸库里没人，核验演示不出效果。
> 沙箱重定向变量会整套传给每个子进程，**嵌套调用**（06 收到报警后自己再调 03）
> 也不会漏回真实目录。

## 二、联动链（已实测跑通 39/39）

```
① 02 登记机位 + 采集基准图
        ↓
② 02 现场画面诊断
     正常画面 -> 无缺陷            -> 可信度 ×1.0
     模糊画面 -> BLUR(fail)        -> 可信度 ×0.5
        ↓
③ 02 -> 03 告警接入（带上该机位的可信度系数）
     可信度 0.5 的机位：红色预警【自动降级为橙色】+ 标注原因，防止画质误报堆积
     可信度 1.0 的机位：保持红色 + 强制确认
        ↓
④ 01 人脸核验（附机位画质上下文）
     可信度 < 0.8 -> 结论标注「该机位画质可信度 ×0.5，核验结论需人工复核」
        ↓
⑤ 03 复核结论回流留痕 + 审计链校验
     画质问题本身由 02 的整改工单闭环（发现→派单→整改→复核）
        ↓
⑥ 06 安检门判据引擎
     手机/无线耳机 -> PHONE_DETECTED（红）   金属/强磁/鞋底 -> SECURITY_ALARM（橙）
        ↓
     经本模块进 03 分级处置 -> 06 复检结论（放行/暂扣/转人工通道/误报）留痕
     查获器材自动存入手机保管柜（人-物-时闭环）
```

实测输出摘录：

```
PASS 正常画面判为正常          缺陷：无
PASS 正常机位可信度不降权      factor=1.0
PASS 模糊画面判为异常且识别为 BLUR   缺陷：['BLUR']
PASS 该机位可信度被降权        factor=0.5
PASS 低可信度机位的红色预警被降级为橙色   level=orange 降级=True
PASS 画质正常机位的红色预警保持红色且需强制确认  level=red force_confirm=True
PASS 核验结论附带了机位画质可信度      ×0.5
PASS 画质不可信的机位会给出人工复核提示
PASS 复核结论已回流留痕
PASS 03 审计链完整
PASS 06 判出手机报警（双判据 -> 红）
PASS 安检报警已送进 03 终端      告警号 ALd293feff9ff9 级别 red
PASS 告警码为 PHONE_DETECTED（手机/耳机类）
PASS 复检结论在 06 留痕（查获暂扣并入柜）
联动自检通过：39/39 项全部符合预期
```

## 三、为什么用子进程 + JSON 而不是直接 import

各套系统**各自都有 `config.py` / `models.py` / `storage.py`**，
同进程 import 必然撞名（互相覆盖）。所以每套各留一个 `integration_api.py`：

```
01_人脸识别与身份核对\01_人脸识别与身份核对\integration_api.py   ops: verify / status
02_摄像头核查与视频质量诊断\integration_api.py                   ops: credibility / inspect /
                                                                     devices / save_device /
                                                                     baseline / status
03_智能终端操作\integration_api.py                               ops: ingest_alert /
                                                                     review_alert /
                                                                     list_alerts / status
06_智能安检与手机作弊器材检测\integration_api.py                 ops: ingest_pass / list_alarms /
                                                                     reinspect / report_alarm /
                                                                     add_locker / store_phone /
                                                                     take_phone / replay_test /
                                                                     metrics / report / status
```

统一调用形式（本模块 `linkage.Project.call` 就是这么调的）：

```bat
python integration_api.py --op credibility --args "{\"device_code\":\"CAM-A01-1\"}"
# 输出最后一行 JSON：{"ok":true,"factor":0.5,"reason":"该机位存在 画面模糊/失焦，可信度 ×0.5",...}
```

好处：命名空间互不干扰、可以单独调试、哪套崩了不影响其他两套。
换任何一套的实现（甚至换成 Java/Go 服务），只要能接这个 JSON 协议，联动层不用改。

## 四、代码用法（在自己程序里调）

```python
import sys; sys.path.insert(0, r"...\项目树\All\00_集成联动")
import linkage

lk = linkage.Linkage()                       # 都用真实数据目录
lk = linkage.Linkage(sandbox_root=r"D:\tmp") # 02/03 走临时目录
lk = linkage.Linkage(sandbox_root=r"D:\tmp", sandbox_keys=("02", "03", "06"))

lk.status()                                  # 各套各自状态（01/02/03/06）
lk.camera_credibility("CAM-A01-1")           # -> (factor, reason, 详情)
lk.inspect_site("现场画面.jpg", device_code="CAM-A01-1")   # 02 诊断
lk.raise_alert("PHONE_DETECTED", "CAM-A01-1", room_id="A-01", seat_no="12")
        # 02 的可信度自动带上；低可信度时红色自动降级
lk.face_verify("face.jpg", candidate="C001", device_code="CAM-A01-1")
        # 01 核验 + 机位画质上下文与人工复核提示
lk.close_alert(alert_id, "confirmed", "已现场核实")        # 复核回流 03

lk.security_pass({"device_code": "GATE-A01", "candidate_id": "C001",
                  "sensors": {"phone": 0.91, "millimeter": 0.72}})
        # -> {result: alarm, kinds: [mobile], level: red, alarm_id: ...}
lk.security_status()                         # 06 概览（过检/待处置/效率/保管柜）
lk.security_alarms(limit=10, pending_only=True)
lk.security_reinspect(alarm_id, "detain", operator="linkage")   # 06 复检结案
```

## 五、目录

```
00_集成联动\
├── 启动联动自检.bat      一键跑通整条链
├── linkage.py            联动总线（Project 子进程句柄 + Linkage 业务编排）
├── main.py               端到端自检（39 项）
├── cheat_bridge.py       作弊识别结合层（把逐人检测结果转成时序特征并驱动引擎）
├── 作弊识别\             考生作弊智能识别引擎（零第三方依赖：界面/演示/自检/75 项测试）
└── README.md
```

## 五之二、作弊识别怎么进来的（2026-10-14）

链路里的位置：**02 画面可不可信 → 作弊识别（动作序列）→ 03 分级处置**。

```
③.5 作弊识别（时序引擎）-> 03 告警
     安静答题/伸懒腰（瞬时动作）  -> 不告警（时间维度抑制）
     持续偷看邻座 / 传递纸条 / 使用手机 -> 触发告警（红色）
        ↓
     经 00 送进 03 -> 同样带 02 的机位画质可信度：低可信度机位自动降级为橙色
```

实测（`python main.py` 里 `[4/7]` 一节）：引擎可用、安静不告警、伸懒腰被抑制、
持续偷看告警、使用手机告警、告警进 03、带可信度并按可信度降级 —— 共 7 项。

**如实说明**：摄像机路径下，引擎自己声明 **手部交互 / 违禁物体 / 表情 = 不可得**（没有骨骼点与物体模型），
结合层原样透出；物体类证据要**多模态大模型判读**才拿得到。角度是**估计值**，需按机位标定。


## 六、如实说明

1. 联动是**同一台机器上的进程间调用**（子进程 + JSON）。搬到多机部署时，
   把 `integration_api.py` 包成 HTTP 服务即可，`linkage.py` 的编排逻辑不用改。
2. 01 的核验在传"文件来源"时按严格模式返回 `LIVENESS_UNAVAILABLE`（防翻拍），
   这是**设计如此**；现场实时视频流走 01 自己的摄像头通道才有活体结论。
3. 02 的"可信度系数"依据该机位**最近一次**核查结果（fail ×0.5 / warn ×0.8 / 正常 ×1.0，下限 0.3）。
   所以联动自检里顺序很重要：先诊断、后发告警。
   安检门（06）不是摄像机机位，02 会回答"未知机位，不降权"，因此安检告警不会被降级。
4. 06 的报警**默认由 06 自己推给 03**（`06/linkage_bridge.py`，可在 06 界面逐条上报或开自动上报）；
   本模块的 `security_*` 接口是给脚本与联动自检用的入口。
