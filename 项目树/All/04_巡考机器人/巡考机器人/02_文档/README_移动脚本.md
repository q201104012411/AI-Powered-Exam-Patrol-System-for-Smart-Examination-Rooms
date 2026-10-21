# 巡考机器人 · 移动脚本使用说明

> 依据桌面《巡考机器人.md》实现"定时巡更 / 按路线巡逻 / 巡更点位打卡留痕 / 遇人即停 / 巡查报告"，
> 针对你的真车（**172.19.12.55**）逐项适配并实测。

---

## 当前进度（2026-10-16 更新，详见 [进度.md](<进度.md>)）

- ✅ 脚本完成、已部署到小车 `~/patrol_robot/`，ROS1/ROS2 双版本 + 车下仿真器 + 一键启动脚本
- ✅ **真车实测（架空）**：基础动作 5/5；完整路线 **13/13、100%**，里程 13.67 m，耗时 127.8 s，2 个打卡点
- ✅ 直行误差 −15~−20 mm；阿克曼圆弧转向误差 −1.7°~−2.0°（容差 2°）
- ✅ **真机避障双向验证**：阈值 0.5m 遇 0.36m 障碍 → 原地中止（0.00m）；阈值 0.25m → 正常走完 0.281m
- ✅ **纠正了一个隐藏的安全隐患**：按 URDF 推断的"雷达倒装 180°"与实际数据不符，
  按 180° 避障等于盯盲区（0 个有效回波）= 没有避障；已用相机交叉验证改为 **0°**，并加了扇区有效性自检
- ✅ 小车环境修复：`~/.bashrc` 的 `ROS_HOSTNAME` 旧 IP 已改为动态取值（备份 `~/.bashrc.bak.2026-10-15_203612`）
- ✅ **落地轮次（2026-10-16）**：避障在地面正确拦停；发现并修复**阿克曼直行跑偏**——加直行航向闭环后，
  同样 0.18m 直行的真实航向变化由 **−4.87° 降到 −0.12°**
- ✅ **无线化（2026-10-19）**：车已连 `303WIFI`，wlan0 = **172.19.12.66**（autoconnect=yes），拔掉网线后远程验证通过
- ✅ **倾角保护（2026-10-19）**：新增 `--max-tilt`（默认 3°）+ 自检第 6 项，车体 >3° 直接拒绝移动
- ✅ **SIGTERM 真机验证（2026-10-19）**：运动中（0.05m/s）收到信号 → 末 5 条指令全 0、进程退出、报告记 `aborted`
- ⚠️ 车带线缆，只能 0.2m 级短距往返；**长距离里程标定与 90° 圆弧转角标定仍待做**（需 ≥1.5m 空间）
- ❌ 仍待做：**真实转向几何标定**、**现场点位距离标定**
- 原始测试记录在 [测试记录/](<测试记录>)，其中 `测试记录/真车_20261016/` 是最近一轮一键脚本+避障验证

---

## 一、你的小车实测档案（脚本就是按这些事实写的）

| 项目 | 实测结果 | 对脚本的影响 |
| --- | --- | --- |
| 主机 | Jeson（aarch64）：有线 `172.19.12.55` / **WiFi `172.19.12.66`**（`303WIFI`，已设开机自动连接） | 脚本部署在 `~/patrol_robot/`；拔网线后走 WiFi |
| ROS | **ROS1 Melodic**（`/opt/ros/melodic`），工作空间 `~/wheeltec_robot` 已编译 | 用 `patrol_move_ros1.py` |
| Python | 2.7.17（rospy 官方路径）+ 3.6.9（py3 rospy 也可用） | ROS1 脚本 **py2/py3 双兼容** |
| 车型 | `car_mode = senior_akm` → **阿克曼前轮转向** | 不能原地转圈、不能横移；`turn` 走"边走边转"圆弧，`turn_rev` 倒车圆弧 |
| 转向量纲 | `teb_local_planner_params.yaml` 里 `cmd_angle_instead_rotvel: False` | `angular.z` = 偏航角速度（标准 ROS 约定），圆弧控制按此闭环 |
| 底盘话题 | `/cmd_vel`（geometry_msgs/Twist），驱动 `wheeltec_robot.cpp` 直接转发 STM32 | 脚本直接发 `/cmd_vel` |
| 里程计 | `/odom`（nav_msgs/Odometry），另有 EKF 的 `/odom_combined` | 默认用 `/odom` 闭环，可用 `--odom-topic /odom_combined` |
| 雷达 | `/scan`（lsn10，448 点，range_max 100m）。URDF 写的是 `base_to_laser` rpy=`3.14 0 0`，但**实测 `/scan` 与车体同向**（见下方"雷达朝向实测"） | 避障默认 `--scan-offset-deg 0`；配 `tools/scan_align_check.py` 复核 |
| 电池 | `/PowerVoltage`（std_msgs/Float32） | 写入巡更报告的起止电压 |
| 速度平滑 | `base_serial.launch` 的 `smoother` 默认 **false** → 驱动直接订阅 `/cmd_vel` | 与脚本直接对接；若开 `smoother:=true`，指令经 yocs 平滑器，上限受 `wheeltec_smoother.yaml` 限制 |
| cmd_vel 看门狗 | **驱动节点没有**，只把速度转发 STM32 | 脚本在任何退出路径（正常/异常/Ctrl+C/SIGTERM）都补发零速度；**不要用 `kill -9`** |
| ROS Master | `~/.bashrc` 里写的是旧 IP `http://172.16.238.247:11311`（不可达） | 每个终端先 `export ROS_MASTER_URI=http://localhost:11311`（脚本会检测并提醒） |

---

## 二、交付物

| 文件 | 用途 |
| --- | --- |
| `patrol_move_ros1.py` | **真车用**（ROS1 Melodic）：巡更移动主脚本 |
| `route_patrol_ros1.yaml` | 真车路线示例（阿克曼版：圆弧转弯 + 三点调头） |
| `tools/fake_base_sim_ros1.py` | ROS1 假底盘仿真器：不动电机即可验证脚本（含阿克曼、雷达朝向、障碍注入） |
| `run_patrol.sh` | **一键巡更**：环境兜底 + master/odom/scan/电量/前方净空预检 + 安全低速默认参数 + 自动存日志 |
| `tools/scan_align_check.py` | **雷达朝向判定工具**：用深度相机剖面与雷达剖面做交叉验证，给出 `--scan-offset-deg` 该填多少 |
| `patrol_move_ros2.py` | ROS2 Humble 版（如果有另一台 ROS2 小车/工程用） |
| `route_patrol_ros2.yaml` / `tools/fake_base_sim_ros2.py` | ROS2 版配套路线与仿真器 |

真车上已经放好：`~/patrol_robot/patrol_move_ros1.py`、`~/patrol_robot/route_patrol_ros1.yaml`、`~/patrol_robot/tools/fake_base_sim_ros1.py`。

---

## 三、真车快速上手

```bash
# 终端 A：启动底盘（会自动起 roscore）
export ROS_MASTER_URI=http://localhost:11311
export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')
roslaunch turn_on_wheeltec_robot turn_on_wheeltec_robot.launch

# 终端 C：雷达（**底盘 launch 里不含雷达**，必须单独启动）
export ROS_MASTER_URI=http://localhost:11311
roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch
rostopic hz /scan          # 确认有数据

# 终端 B：跑巡更
cd ~/patrol_robot
export ROS_MASTER_URI=http://localhost:11311
export ROS_HOSTNAME=$(hostname -I | awk '{print $1}')

python patrol_move_ros1.py --route route_patrol_ros1.yaml --dry-run   # 1) 先预演（离线可跑，不需要 roscore）
python patrol_move_ros1.py --scan-check                              # 2) 雷达朝向自检（不驱动底盘）
python patrol_move_ros1.py --forward 0.3 --speed 0.12                # 3) 单步慢速试跑
python patrol_move_ros1.py --route route_patrol_ros1.yaml --speed 0.12   # 4) 慢速跑整条路线
python patrol_move_ros1.py --route route_patrol_ros1.yaml --loop 0   # 5) 无限循环巡更（Ctrl+C 停）
```

> ⚠️ 小车 `~/.bashrc` 里 `ROS_MASTER_URI=http://172.16.238.247:11311` 是**旧 IP，连不上**，
> 交互终端里不覆盖它，`roslaunch` 会直接报 `Unable to communicate with master!`。
> 想一劳永逸可以直接改掉（脚本启动前也会自动体检并给出提示）：
> ```bash
> cp ~/.bashrc ~/.bashrc.bak.$(date +%F)
> sed -i 's|^export ROS_MASTER_URI=.*|export ROS_MASTER_URI=http://localhost:11311|' ~/.bashrc
> sed -i "s|^export ROS_HOSTNAME=.*|export ROS_HOSTNAME=\$(hostname -I \| awk '{print \$1}')|" ~/.bashrc
> ```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--car-mode auto\|akm\|diff\|omni` | 默认 auto：读小车 launch 里的 `car_mode`（此车识别为 akm） |
| `--speed 0.12` | 直行速度（首次实跑建议 ≤ 0.12 m/s） |
| `--akm-turn-speed 0.08` | 阿克曼转弯时的前进速度，越小圆弧越紧凑越安全 |
| `--stop-distance 0.35` `--scan-arc 35` | 前方 ±35° 内 0.35m 有障碍就停车 |
| `--obstacle-policy wait\|abort\|ignore` | 遇障碍：等待清除 / 中止任务 / 忽略 |
| `--no-scan` | 关闭避障（无雷达或调试时用） |
| `--yaw-sign -1` | 应急开关：万一实车"左转"变"右转"（角速度方向相反）时使用 |
| `--max-tilt 3` | 车体倾角上限（度）。超过就拒绝移动（压在障碍物上/地面不平的保护），`0` 关闭 |
| `--imu-calibrate` | 零位校准：车摆平后执行，把当前姿态记成 0° 存 `imu_offset.json`，之后自动扣除 |
| `--yaw-source imu\|odom` | 转向反馈源，默认 **imu**（实测 odom 航向高估约 16%） |
| `./webui/robot_web.py` | 网页遥控面板（8080）+ 摄像头 MJPEG（8081） |
| `--no-heading-hold` | 关闭直行航向修正（默认开启；阿克曼车直行实测会跑偏 ~5°/0.18m） |
| `--dist-tol 0.005` | 收紧到位容差（默认 0.02m，这就是每次"误差 −18mm"的来源） |
| `--skip-master-check` | 跳过启动前的 ROS Master 连通性检查 |
| `--loop 0 --interval 60` | 无限巡更，每轮间隔 60s |
| `--report FILE.json` / `--no-report` | 巡更记录路径（默认 `~/patrol_reports/patrol_*.json`） |

---

### 更省事：一键脚本 `run_patrol.sh`

它把"环境变量 + 起飞前自检 + 安全低速默认参数 + 存日志"都包好了：

```bash
cd ~/patrol_robot
./run_patrol.sh --check                                   # 只自检，不动车
./run_patrol.sh --forward 0.3                             # 单步（默认 0.12m/s）
./run_patrol.sh --route route_patrol_ros1.yaml            # 跑路线（默认 0.12m/s / 转 0.08m/s）
./run_patrol.sh --route route_patrol_ros1.yaml --speed 0.2 --loop 0   # 显式参数覆盖默认值
```

自检输出实测（2026-10-16）：

```
[1/5] ROS Master      ✔ master 可连接
[2/5] 里程计 /odom     ✔ 有数据，约 20.0 Hz
[3/5] 雷达 /scan       ✔ 有数据，约 10.0 Hz（避障可用）
[4/5] 电池电压         ✔ 电池 22.9 V
[5/5] 车头前方净空     ✔ 前方净空 0.37 m（扇区有效回波 61 个）
```

自检不过就**不会启动巡更**（退出码非 0），避免"以为有避障其实没有"。
运行日志自动存到 `~/patrol_reports/run_YYYYmmdd_HHMMSS.log`。

---

### 雷达朝向实测（重要，与 URDF 不一致）

`turn_on_wheeltec_robot` 的 URDF 里 `base_to_laser` 是 `3.14 0 0`（即理论倒装 180°），
但**实测这台车的 `/scan` 原始角度与车体同向**，证据（`tools/scan_align_check.py`）：

| 补偿角假设 | 与深度相机剖面的吻合度 |
| --- | --- |
| **+0°** | 中位差 0.94 m（90 个可比角度）✅ |
| +45° | 0.91 m（90 个）—— 与 0° 基本并列 |
| **+180°** | **0 个可比角度**（该方向 448 点里一个有效回波都没有）❌ |
| ±90° / ±135° | 1.26~2.00 m（明显更差） |

结论：**用 `--scan-offset-deg 0`**（脚本默认值已改成 0）。
若按 180° 配置，避障扇区正好落在无回波的盲区里 —— 看着像有避障，实际完全不触发。

判定方法（可复现）：`python tools/scan_align_check.py`
（相机装在车头、光轴朝车体 +x，用它的距离剖面和雷达剖面比对，谁吻合车头就在哪边。）

脚本每次启动也会自检：扇区有效回波 < 3 个会**显式报警**并写入报告事件 `scan_sector_empty`。

---

## 四、路线文件怎么写（阿克曼重点）

```yaml
route_name: "考点夜间巡更 A 线（阿克曼版）"
default_speed: 0.18         # m/s 直行
akm_turn_speed: 0.10        # m/s 阿克曼转弯前进速度
steps:
  - {forward: 2.00, name: "一楼楼道直行"}
  - {turn: -90, name: "右转进连廊"}       # 阿克曼 = 右转圆弧，需前方留 1m 左右净空
  - {checkpoint: 8.0, name: "保密室门口"}  # 停留 8s + 打卡留痕
  - {turn_rev: -60, name: "调头第1步：倒车右转"}   # 三点调头
  - {turn: -120, name: "调头第2步：前进右转"}
  - {wait: 2.0, name: "待机点整备"}       # 只等待，不算打卡
```

- `forward/back`：米；`turn/turn_rev`：度（+左/−右）；`checkpoint`：打卡停留秒数；`wait`：纯等待。
- **阿克曼**：不要写 `strafe`（横移不支持，脚本会直接报错停下）；走道尽头用 `turn_rev` + `turn` 组合做三点调头。
- 打卡点位 = 脚本里的 `checkpoint`，会写进 JSON 巡更记录的 `checkpoints`（含时间、停留时长、odom 坐标、电压）。

## 五、巡更报告（JSON）示例字段

```json
{
  "route_name": "考点夜间巡更 A 线（阿克曼版）",
  "car_mode": "akm", "duration_s": 93.2, "total_distance_m": 10.3,
  "checkpoints": [{"name": "试卷保密室门口", "time": "...", "dwell_s": 8.0, "odom_x": 3.42, "voltage": 11.8}],
  "events": [{"type": "obstacle", "time": "...", "detail": "前方 0.22m 处检测到障碍/行人，已停车等待"}],
  "steps": [{"name": "一楼楼道直行", "type": "forward", "target": 2.0, "actual": 1.985, "error": -0.015, "ok": true}],
  "steps_ok": 13, "steps_total": 13, "task_completion_rate": 100.0, "result": "completed"
}
```

> 用 `python`（2.7）跑时，报告里的中文是 `\uXXXX` 转义（合法 JSON，任何查看器都能正常显示）；
> 想直接看到中文，用 `python3 patrol_move_ros1.py ...`（py3 rospy 在车上实测可用），或 `json.tool` 转换。

---

## 六、已经验证过的内容（真机 ROS1 栈 + ROS2 栈）

在**小车本机**用假底盘仿真（不接电机、不动车）实测：

| 测试 | 结果 |
| --- | --- |
| 直行闭环 | 0.5m → 实测 0.487m（误差 −13mm） |
| 阿克曼圆弧左转 90° | 实测 88.07°（误差 −1.93°，容差 2°） |
| 阿克曼倒车右转 45° | 实测 −43.08° |
| 完整路线 13 个动作 | 100%（13/13），里程 10.30m，耗时 93.2s，2 个打卡点，报告生成 |
| 避障（仿真，雷达倒装 180° 场景） | 前方 0.22m → 停车等待 → 清除后继续，目标 1.5m 实测 1.481m |
| `--obstacle-policy abort` / `ignore` | 分别正确中止 / 正确忽略通过 |
| 里程计看门狗 | 中途杀掉仿真（odom 掉线）→ 0.5s 内停车并中止 |
| SIGTERM / Ctrl+C | 补发零速度后再退出（`/cmd_vel` 末尾均为 0），记录写成 `aborted` |
| 雷达朝向自检（仿真） | 障碍放在车头时，最近距离出现在 ±165° 扇区 → 正确识别倒装 |
| py2.7 / py3 语法与运行 | 均通过（`python` 与 `python3` 都能跑） |

### 真机实跑（2026-10-16，架空，轮子离地）

| 测试 | 结果 |
| --- | --- |
| 完整路线（13 步，关避障） | 13/13、100%，里程 13.67 m、127.8 s，2 个打卡点，电池 23.4 V 稳定 |
| 避障「该拦就拦」 | 阈值 0.5 m，实测前方 0.36 m → 立即中止，**里程 0.00 m** |
| 避障「不该拦就不拦」 | 阈值 0.25 m → 正常完成，目标 0.30 m 实测 0.281 m（−19 mm） |
| 一键脚本预检 | master/odom 20.0Hz/scan 10.0Hz/电池 22.9V/前方净空 0.37m 全部通过 |
| 扇区自检 | 启动时打印「扇区 (车体 ±35°) 有效回波 77 个，最近 0.37 m」 |
| 架空对照（重要） | odom 累计转 +44.9° 时 IMU 真实只转 +2.87° → 转弯几何必须落地验证 |

### 落地实跑（2026-10-16，地面，带线缆）

| 测试 | 结果 |
| --- | --- |
| 直行航向漂移（关修正） | 前进 0.18 m，IMU 真实航向变化 **−4.87°** |
| 直行航向漂移（开修正，默认） | 前进 0.18 m，IMU 真实航向变化 **−0.12°** ✅ |
| 避障地面拦停 | 前方实测 0.32 m（阈值 0.35）→ 立即中止，车未动 |
| 短距往返精度 | 前进/后退 0.18~0.28 m，误差稳定 −17~−19 mm（= `--dist-tol` 2cm 容差，非控制误差） |
| IMU 漂移 | 静置期间约 0.6°/min，不能当长期绝对航向；4 秒窗口内的变化量可用 |
| IMU 零位 | 加速度计有静态偏置（本车 pitch 5.5°/roll 2.6°），已用 `--imu-calibrate` 归零 |
| 转向精度（IMU 反馈） | 目标 20° → 真实 17.94°（脚本测 18.01°），差 2° = 到位容差 |
| 地面短路线（一键脚本） | 6/6、100%，含打卡/前进圆弧/倒车圆弧/退回，里程 0.84m、19s |

ROS2 Humble 版（另一套环境）也在假底盘上跑通了同样的用例（含 12 步路线、避障、看门狗、SIGTERM）。

## 七、真车首次实跑建议（安全流程）

1. 场地清空 3m×3m，地面干燥；人手随时能按小车急停/断电。
2. 先 `--dry-run` 确认路线步骤与耗时。
3. 再 `--forward 0.3 --speed 0.12` 单步确认方向和刹车距离。
4. `--scan-check` 放个纸箱在车头验证避障方向正确。
5. 用 `--speed 0.12 --akm-turn-speed 0.08` 跑整条路线；确认无误后再逐步提速。
6. 阿克曼车转弯需要空间：先按"圆弧半径 ≈ 0.8~1.2m"给走道留余量；不够就用 `turn_rev` 做三点调头。

## 八、网页遥控面板（`webui/`）

浏览器里开车：**按住才动、松开就停**，带急停、避障拦截、倾角保护和摄像头画面。

| 文件 | 说明 |
| --- | --- |
| `webui/robot_web.py` | 服务端：rospy 发布 `/cmd_vel` + 标准库 HTTP（不需要 flask），py2/py3 通用 |
| `webui/index.html` | 页面：方向键盘、速度滑条、急停、遥测、摄像头 |

启动（小车上）：

```bash
source /opt/ros/melodic/setup.bash && source ~/wheeltec_robot/devel/setup.bash
export ROS_MASTER_URI=http://localhost:11311 ROS_HOSTNAME=172.19.12.66

# 1) 控制页（8080）
cd ~/patrol_robot/webui && python3 robot_web.py

# 2) 摄像头 MJPEG（8081，另开终端）
~/wheeltec_robot/devel/lib/web_video_server/web_video_server _port:=8081
```

浏览器打开 **http://172.19.12.66:8080/**（车上本机或同网段电脑都可以）。

| 参数 | 说明 |
| --- | --- |
| `--max-linear 0.20` / `--max-angular 0.60` | 网页能给出的最大速度（硬限幅） |
| `--stop-distance 0.35` | 前方 ±25° 内小于此距离时**拒绝前进**（后退不受限） |
| `--max-tilt 3` | 倾角上限，超过拒绝移动（配合 `imu_offset.json` 零位） |
| `--watchdog 0.6` | 多久没收到网页指令就停车（关标签/断网自动停） |
| `--dry-run` | 只记录不发布，用来远程验证接口 |
| `--port 8080` | 监听端口 |

页面上的安全语义：
- 方向键**按住才发速度**（每 120ms 一次），松开/移出/页面失焦/关闭标签 → 立刻发停车；
- 服务器另有 0.6s 看门狗兜底，**网页崩了车也会停**；
- `E-STOP` 一键锁定，除了解除急停，任何指令都被忽略；
- 顶部横幅会显示当前被拦的原因（急停 / 车体倾斜 / 前方障碍）；
- 遥测区显示电池、odom 位置与航向、pitch/roll、四个方向净空（前/左/右/后）。

⚠️ 已知机器人特性：相机（USB）启动后**雷达偶尔会"进程活着但不发数据"**，
表现为页面净空全空、`rostopic hz /scan` 报 no new messages。
处理：重启雷达 `roslaunch turn_on_wheeltec_robot wheeltec_lidar.launch`（不用重启其它节点）。

---

## 九、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 报"连不上 ROS Master" | 底盘没启动；或 `ROS_MASTER_URI` 还是旧 IP `172.16.238.247` → 改成 `http://localhost:11311`（脚本已做启动前体检并给出提示） |
| 报"5 秒内未收到 /odom" | 底盘 launch 没起 / 串口没占用成功（`ls /dev/wheeltec_controller`）；看底盘终端有没有报错 |
| 一直提示"未收到 /scan" | 雷达没启动（底盘 launch 不含雷达），或话题名不同 → `--no-scan` 或 `--scan-topic` 指定 |
| 避障方向不对（后方有障碍也停，或前方有障碍不停） | 补偿角错 → 跑 `python tools/scan_align_check.py` 按结论设置 `--scan-offset-deg`；脚本启动时会自检扇区有效回波数，为 0 会报警 |
| 转弯转不动、超时中止 | 阿克曼必须有前进/倒车速度才转向；确认用的是 `patrol_move_ros1.py`（ROS1 版）且 `--car-mode akm` |
| 左转变右转 | 用 `--yaw-sign -1` 应急 |
| 想完全停下 | 终端 `Ctrl+C`（脚本会补发零速度）；紧急情况直接按小车急停/断电 |
| 中文报告乱码 | 用 `python3` 跑，或 `python -m json.tool` 查看 |
| 想先确认路线对不对 | `--dry-run` 完全离线可跑（不需要 roscore、不会动车），几秒到一两分钟出预估 |
| 换网/拔插网线后 `/odom`、`/scan` 没数据，但 `rosnode list` 正常 | ROS1 节点用**启动时的 IP** 注册，换 IP 后旧进程地址失效 → **重启整套 ROS**，并 `export ROS_HOSTNAME=<当前IP>; export ROS_IP=<当前IP>` |
| 自检报"车体倾斜 pitch>3°" | 有轮子压在网线/纸板/线槽上，或地面不平 → 把车摆平再跑；若**看着是平的**，跑一次 `python patrol_move_ros1.py --imu-calibrate` 归零（IMU 加速度计有偏置，本车实测偏 5.5°） |
| 转向总是少转几度 | odom 航向比真实大（本车约 16%）→ 确认用的是默认 `--yaw-source imu`；还想更准就把 `--angle-tol` 收到 0.5~1° |
