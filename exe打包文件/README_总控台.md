# 智能考场 AI 巡考系统 · 单文件总控台

> **仓库说明**：成品 `智能考场AI巡考系统.exe` 512MB、`runtime\` 508MB，均**不入库**
> （超过 GitHub 普通仓库单文件 100MB 限制）。本目录只保留启动器源码
> `总控台.py` / `launcher_auth.py` / `模块清单.json`；构建时把本目录（含各模块）
> 复制到发布包目录，再放入 `runtime\` 后打包。没有 `runtime\` 时，总控台会自动
> 使用系统 Python，源码树可直接运行，不需要 exe 打包目录。

## 〇、源码树直接运行（不打包 exe）

```bat
cd /d "项目树\All"
python 总控台.py
```

每个模块目录也可以单独运行（例如 `02_摄像头核查与视频质量诊断\gui.py`、
`11_告警上传与终端处置演示\gui.py`）；需要 `sso.py` / `ui_theme.py` 时，
模块会自动把包根加入 `sys.path`。依赖按各目录 `安装环境.bat` / `README.md` 安装。

## 一、双击什么

`智能考场AI巡考系统.exe`（发布包目录下，约 512.6 MB）

- SHA256：`9C2EA395F2523D96B3BAC771846890A6FAF8147A9A4987FC70118194949BDFC9`
- 第一次运行：会先把运行环境解压到
  `%LOCALAPPDATA%\智能考场AI巡考系统\app-<hash>`，控制台窗口显示解压进度；
  解压完成后自动打开「总控台」。
- 若 `%LOCALAPPDATA%` 不可写（受限账户/沙箱/组策略），会自动回退到
  `%TEMP%\智能考场AI巡考系统\app-<hash>`，再不行就解压到 exe 同目录的
  `智能考场AI巡考系统\app-<hash>`；**不会删除以前版本的 app-* 解压目录**。
- 以后运行：直接复用已解压目录，只闪一下控制台窗口。
- 换电脑/删掉 `%LOCALAPPDATA%\智能考场AI巡考系统` 后，下次运行会重新解压。

## 二、总控台怎么用

1. 启动后先在「统一登录」输入一次账户 + 口令；
2. 左侧是「系统结构目录」，按 **考场业务 / 安全与保障 / 系统集成** 分组；
3. 单击模块看右侧说明；**双击模块**（或回车、点「进入模块」）直接启动，不再重复输入口令；
4. 右上角「账户管理」只有 owner 能进：新增账户、重置口令、改“可进入模块”范围；
5. 需要换人时点右上角「切换账号」，重新登录一次即可。

## 三、默认账户（首次登录后请改口令）

| 账户 | 口令 | 角色 | 可进入模块 |
| --- | --- | --- | --- |
| owner | EDY_POWERQ | 所有者 | 全部 |
| admin | admin | 管理员 | 01/02/03/05/06/07/08/09/10/11 |
| patrol1 | patrol1 | 巡考员 | 01/02/03/10/11 |
| inv01 | inv01 | 监考员 | 03/10 |
| check1 | check1 | 核验员 | 01 |

> 这些默认值与 03 智能终端一致。口令只存 PBKDF2-HMAC-SHA256 加盐哈希，
> 配置文件是解压目录里的 `总控台账户.json`，不存明文。
> 登录/进入成功与失败都会写 `总控台审计.jsonl`。

## 四、两个容易误解的点

1. **总控台账户是“进入模块”的统一门禁**，登录一次后通过签名 SSO 令牌
   自动进入 01/02/03/05/08 等模块，不再要求第二遍口令；
   直接双击模块自己的 .bat/.py（不经过总控台）时，模块仍保留原有登录页。
2. **这是界面入口门禁，不是文件系统权限**。能进解压目录的人仍可直接
   `runtime\python.exe <模块脚本>`。要防这一层需要 NTFS ACL 或把模块放到受控服务端。

## 五、目录说明

```
智能考场AI巡考系统.exe   单文件成品（启动器 + payload + 自带 Python 运行时）
总控台.py                目录树 + 统一登录 + 启动逻辑
launcher_auth.py         总控台账户（PBKDF2）
sso.py                   总控台 ↔ 模块的单点登录令牌（HMAC）
ui_theme.py              模块统一深色主题（与总控台同配色）
模块清单.json            模块/分组/入口/角色配置
icon\icon.png            窗口/任务栏图标（exe 文件图标由启动器资源嵌入）
runtime\                 自带 Python 3.12 + 依赖（不依赖系统 Python）
00..13\                  各子系统源码与数据
一键打包.sh              Git Bash 一键打包（图标取 icon/icon.png）
_build\                  打包工具（launcher 源码、pack.py、assemble.ps1）
```

04 巡考机器人只有方案文档，没有可运行入口，树里显示“缺入口”。

## 六、重新打包（仅打包机）

> `_build\`（launcher/main.go、pack.py、assemble.ps1）与 `runtime\` 体积大，**未入库**；
> 下面是打包机上的本地步骤，源码仓库只保留启动器代码。

### 一键打包（推荐，Git Bash）

```bash
cd exe打包文件
bash 一键打包.sh           # 完整打包
bash 一键打包.sh --check   # 只检查环境 + 试转图标，不生成大文件
```

脚本会自动完成：PNG → 多尺寸 ico → rsrc 资源 → `go build`（把图标编进启动器）
→ `pack.py` → 组装（启动器 + payload.enc + 40 字节尾部索引（AES-256-GCM））→ 校验图标/索引/SHA256。
依赖 Git Bash、Python 3.8+（Pillow）、Go 1.20+；`rsrc.exe` 缺失时会自动获取。

### 手工分步

```powershell
cd exe打包文件
# 1) 重新压 payload（排除 _build/缓存/日志/附件）
runtime\python.exe _build\pack.py
# 2) 重新组装 exe
powershell -ExecutionPolicy Bypass -File _build\assemble.ps1
```

`assemble.ps1` 输出本目录的 `智能考场AI巡考系统.exe`。

## 七、图标

- 源图：`icon/icon.png`（512×512 RGBA）。
- 文件/控制台图标：`_build/app.ico`（16/24/32/48/64/128/256 多尺寸），由
  `rsrc -arch amd64 -ico app.ico -o launcher/rsrc.syso` 生成资源，
  再用 `go build -o launcher_icon.exe .` 编进启动器；`assemble.ps1` 默认用它。
- 总控台窗口/任务栏图标：`总控台.py` 的 `_set_window_icon()` 用
  `root.iconphoto(True, tk.PhotoImage(file="icon/icon.png"))` 设置，
  登录框/账户管理等 Toplevel 也会继承。
- 换图标：替换 `icon/icon.png` → 重新生成 `app.ico` → 重新生成 `rsrc.syso` →
  重新编译 `launcher_icon.exe` → 重新 `pack.py` → `assemble.ps1`。
- Windows 资源管理器有图标缓存：替换同名 exe 后如仍显示旧图标，按 F5、
  重启资源管理器，或把 exe 复制成新文件名查看。
