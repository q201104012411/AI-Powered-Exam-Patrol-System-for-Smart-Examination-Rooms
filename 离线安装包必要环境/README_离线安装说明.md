# 智能考场 AI 巡考系统 · pip 依赖离线安装包

> 目标机器**完全断网**也能把本系统跑起来所需的一切 Python 第三方依赖，全部以
> `wheel`（预编译轮子）形式打包在本目录，不下载、不编译、不联网。

- **适用平台**：Windows 10 / 11（64 位）
- **适用 Python**：CPython **3.12**（`win_amd64`）
  - 本包内 `numpy / scipy / opencv-python / onnxruntime` 等是 **cp312 专用轮子**，
    装到 3.10 / 3.11 / 3.13 / 3.15 上会直接失败（见「常见问题 ②」）。
- **包体积**：核心链约 **145 MB**，可选链约 **264 MB**，整个目录约 **410 MB**

---

## 一、目录结构

```
离线安装包必要环境\
├─ 一键离线安装.bat              ← 【双击这个】启动安装器（自动进入中文菜单）
├─ install_offline.py            ← 实际干活的安装器（检查→安装→校验→提示）
├─ requirements-offline.txt      ← 直接依赖清单（带中文说明，给人看）
├─ requirements-lock.txt         ← 全量锁定清单（53 个包，含传递依赖，给 pip 用）
├─ requirements-lock-可选-模型导出.txt
├─ 清单_SHA256.txt                ← 102 个 wheel 的 SHA256 + 字节数（完整性核对）
│
├─ wheelhouse\                   ← 【核心】53 个 wheel，144.2 MB
│                                   01/02/03/05/06/08/09/10/11/12/13/00 全部运行依赖
├─ wheelhouse_可选_模型导出\       ← 45 个 wheel，263.6 MB（ultralytics + torch）
│                                   只有"重新导出 YOLO11n → ONNX"时才需要
├─ bootstrap\                    ← pip / setuptools / wheel（离线修复 pip 用）
└─ _src\                         ← 生成脚本（重新制作离线包时用，可删）
```

> **为什么 `.bat` 里是英文？** `cmd.exe` 按系统 ANSI 代码页解析批处理文件，批处理里
> 直接写中文（UTF-8 字节）会被误读并撕裂命令行——本项目原有的
> `install_pip环境.bat` 是用 GBK 存的才没出问题。为彻底避免这个坑，
> 本包的 `.bat` 只保留 ASCII 控制逻辑，**全部中文界面都由 `install_offline.py`
> 输出**（Python 的 UTF-8 处理是可靠的）。

---

## 二、快速开始（推荐）

1. 把这个 `离线安装包必要环境` 文件夹整体拷到目标机器（保持它在项目根目录下）。
2. 确认目标机器已装 **Python 3.12（64 位）**，安装时勾选：
   - `[x] Add python.exe to PATH`
   - `[x] tcl/tk and IDLE` ← **必须勾**，否则所有 GUI 起不来
   - 没装的话，项目里已附带安装包：
     `..\项目树\All\01_人脸识别与身份核对\01_人脸识别与身份核对\资源\python-3.12.4-amd64.exe`
3. 双击 **`一键离线安装.bat`**，选 `[1] 一键离线安装全部依赖`。
4. 看到 `离线环境安装完成，全部校验通过！` 即可。

安装器会依次做 6 件事：

| 步骤 | 内容 |
| --- | --- |
| 1 | 检查解释器：3.12 / 64 位 / tkinter / sqlite3 |
| 2 | 检查离线资源：wheel 数量、锁定清单、bootstrap，并逐文件比对 `清单_SHA256.txt` |
| 3 | 检查 pip，缺失时用 `bootstrap\` 内的 wheel **离线**补装 |
| 4 | 卸载 `opencv-python-headless` / `opencv-contrib-*`（HOG 缺失头号原因），再 `--no-index` 批量安装 |
| 5 | 逐个模块校验 + `HOGDescriptor` 专检 + 打印版本快照 |
| 6 | 打印后续步骤（人脸模型解压位置、各子系统启动脚本） |

日志写到 `..\logs\install_offline.log`。

双击后看到的菜单：

```
 [1] 一键离线安装全部依赖（推荐，含自动校验）
 [2] 只检查环境与离线包完整性（不安装）
 [3] 只做依赖校验（装完后复检）
 [4] 一键离线安装 +「YOLO 模型导出」可选链
 [5] 强制重装 wheelhouse 内全部包
 [0] 退出
```

---

## 三、手动安装（不想用 bat 时）

```bat
:: 先用 py -3.12 确认解释器
py -3.12 -c "import sys; print(sys.executable)"

:: 离线安装核心依赖（--no-index 表示绝不联网）
py -3.12 -m pip install --no-index --find-links=wheelhouse --upgrade -r requirements-lock.txt

:: 只要哪些子方案？也可以只装核心几个
py -3.12 -m pip install --no-index --find-links=wheelhouse "numpy==2.5.4" "opencv-python==4.14.0.94" "Pillow==12.3.0"
```

常用参数（短横线、长横线都行）：

```bat
一键离线安装.bat -c    :: 只检查环境与离线包完整性，不安装
一键离线安装.bat -v    :: 只做依赖校验
一键离线安装.bat -o    :: 额外装 YOLO 模型导出可选链
一键离线安装.bat -f    :: 强制重装 wheelhouse 内全部包
一键离线安装.bat -h    :: 查看全部参数
```

等价的 Python 直调：

```bat
py -3.12 install_offline.py -c
py -3.12 install_offline.py -v
py -3.12 install_offline.py -f
py -3.12 install_offline.py -o
py -3.12 install_offline.py -p D:\Python312\python.exe
```

---

## 四、装了什么？为什么装？

### 核心链（`wheelhouse\`，53 个包）

| 包 | 版本 | 谁需要 | 作用 / 不装的后果 |
| --- | --- | --- | --- |
| `numpy` | 2.5.4 | 几乎全部 | 数值计算、图像矩阵 |
| `opencv-python` | **4.14.0.94** | 01/02/03/08/11/12/13 | 取流 + 图像处理。**必须 4.x**：5.x 已删除 `HOGDescriptor`，装了 02/03/11 的 AI 框选识别会静默失效 |
| `Pillow` | 12.3.0 | 01/02/03/05/08/11/13 | 画面显示、图片格式转换 |
| `insightface` | 2.1 | 01 | 人脸检测/对齐/识别（SCRFD + ArcFace） |
| `onnxruntime` | 1.31.0 | 01/02/03/11/13 | ONNX 推理后端（人脸模型 + YOLO11n 检测链）。缺它检测链会静默回落到 HOG+SVM |
| `onnx` | 1.23.2 | 01（insightface 依赖） | ONNX 图解析 |
| `scipy` | 1.18.1 | 02/03/11 | 匈牙利匹配跟踪；缺则自动回退贪婪最近邻 |
| `flask` | 3.0.3 | 01/02/08 | 网页操作面板 / 网页核查面板 / 保密室网页面板 |
| `flask-cors` | 4.0.1 | 01/02/08 | 网页跨域 |
| `flask-limiter` | 3.8.0 | 01 | 登录与接口限流 |
| `PyJWT` | 2.9.0 | 01 | JWT 令牌签发与校验 |
| `bcrypt` | 4.2.0 | 01 | 口令哈希 |
| `cryptography` | 50.0.2 | 01/02/03/06/08/09 | Fernet 字段级加密、AESGCM、RSA 签名。**缺则这些模块如实降级为明文**（不会假装加密） |
| `gmssl` | 3.2.2 | 05 | 国密 SM2 / SM3 / SM4 |
| `pyserial` | 3.5 | 06 | 安检门串口行协议接入；缺则界面如实显示"未安装 pyserial" |
| `openpyxl` | 3.1.5 | 01 | Excel 考生名册导入导出、Excel 模板 |
| `python-docx` | 1.2.0 | 工具 | Word 项目书 / 报告生成 |

其余 36 个是上述包的**传递依赖**（`Werkzeug / Jinja2 / click / blinker / limits /
ordered-set / Deprecated / wrapt / protobuf / flatbuffers / requests / urllib3 /
certifi / charset-normalizer / idna / scikit-image / imageio / networkx / tifffile /
lazy-loader / tqdm / colorama / cffi / pycparser / pycryptodomex / lxml /
markdown-it-py / mdurl / MarkupSafe / ml_dtypes / typing-extensions / packaging /
et-xmlfile / pygments / rich / …），已全部锁定版本。

### 可选链（`wheelhouse_可选_模型导出\`，45 个包）

| 包 | 版本 | 说明 |
| --- | --- | --- |
| `ultralytics` | 8.4.178 | 重新导出 YOLO11n → ONNX |
| `onnxslim` | 0.1.98 | 导出时图简化 |
| `torch` / `torchvision` | 2.14.1 / 0.29.1 | ultralytics 的推理后端（**CPU 版**，导出不需要显卡） |
| `opencv-python` | **4.14.0.94** | 已用约束钉回 4.x（ultralytics 默认会拉 5.x，破坏 HOG） |
| 其余 | — | matplotlib / polars / sympy / pyyaml / psutil / httpx / … |

> **日常运行不需要这一链**：项目已自带 `models\yolo11n.onnx`（02/03/11 各一份）。
> 只有你要换模型（如 `yolo11s`）或重新导出时才需要。

---

## 五、pip 之外还需要的环境（已随项目附带，不用装）

| 环境 | 位置 | 说明 |
| --- | --- | --- |
| Python 3.12.4 | `项目树\All\01_人脸识别与身份核对\01_人脸识别与身份核对\资源\python-3.12.4-amd64.exe` | 25.5 MB，勾选 PATH + tcl/tk |
| 人脸模型 buffalo_l | 同上目录 `buffalo_l.zip`（275 MB） | 解压到 `%USERPROFILE%\.insightface\models\buffalo_l` |
| Ollama（本地大模型） | `大模型本地部署\OllmaSetup.exe`（1.5 GB） | VLM 多模态判读；不装则作弊识别的物体类证据不可得 |
| go2rtc（小米摄像头） | `摄像头\小米摄像头go2rtc\bin\go2rtc.exe` | RTSP 转流 |
| YOLO11n ONNX 权重 | `项目树\All\02_摄像头核查与视频质量诊断\models\yolo11n.onnx` | 已自带，无需下载 |

`tkinter` / `sqlite3` 是 Python 标准库的一部分，**不是 pip 包**，只能用官方安装包勾选
`tcl/tk` 获得——这也是本安装器第一步就检查它的原因。

---

## 六、常见问题

### ① `HOGDescriptor` 校验失败，02/03/11 的 AI 框选识别不可用

原因几乎总是装了三者之一：`opencv-python-headless`、`opencv-contrib-python`、
或 `opencv-python` 5.x。修复：

```bat
py -3.12 -m pip uninstall -y opencv-python-headless opencv-contrib-python opencv-python
py -3.12 -m pip install --no-index --find-links=wheelhouse --force-reinstall "opencv-python==4.14.0.94"
```

本安装器在安装前会自动卸载这些冲突包，所以**正常走一遍 `一键离线安装.bat` 即可**。

### ② 报错 `Could not find a version that satisfies the requirement numpy==2.5.4` / 找不到 cp312 轮子

说明跑安装器的 Python 不是 3.12。用 `py -0p` 看本机有哪些版本：

```bat
py -0p
py -3.12 -m pip --version
py -3.12 install_offline.py
```

本机同时存在 3.15 之类的更高版本时，`python` 默认可能指向别的版本，所以
**一律显式写 `py -3.12`**。`一键离线安装.bat` 已内置 `py -3.12` 优先探测。

### ③ `python -m pip` 不存在（pip 被删或 Python 装歪了）

`bootstrap\` 里已备好 pip 的 wheel，安装器会自动离线补装；也可手动：

```bat
py -3.12 -m ensurepip --upgrade
py -3.12 -m pip install --no-index --find-links=bootstrap --upgrade pip setuptools wheel
```

### ④ 装完 `import insightface` 报找不到模型

pip 包装好了，但 `buffalo_l` 模型还没解压。把
`项目树\All\01_人脸识别与身份核对\01_人脸识别与身份核对\资源\buffalo_l.zip`
解压成 `%USERPROFILE%\.insightface\models\buffalo_l\`（该目录下应直接看到
`det_10g.onnx`、`w600k_r50.onnx` 等文件，不要多套一层）。

### ⑤ 想装到 Python 3.13 / 3.11

本包内是 cp312 轮子，不支持。请在**有网**的机器上重新取轮子：

```bat
py -3.13 -m pip download -r _src\requirements-core.in -d wheelhouse --only-binary=:all: ^
    -c _src\optional-constraints.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
py -3.13 _src\生成锁定清单.py
```

---

## 七、重新制作 / 更新这个离线包（需要有网的机器）

```bat
:: 1. 改依赖：编辑 _src\requirements-core.in
:: 2. 重新抓轮子（本机 Python 3.12）
py -3.12 -m pip download -r _src\requirements-core.in -d wheelhouse ^
    --only-binary=:all: --python-version 3.12 --implementation cp --abi cp312 ^
    --platform win_amd64 -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 3. 可选链（注意带约束，否则会拉 opencv 5.x）
py -3.12 -m pip download ultralytics onnxslim -d wheelhouse_可选_模型导出 ^
    -c _src\optional-constraints.txt --only-binary=:all: ^
    --python-version 3.12 --implementation cp --abi cp312 --platform win_amd64 ^
    -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 4. 重新生成 requirements-lock*.txt 与 清单_SHA256.txt
py -3.12 _src\生成锁定清单.py

:: 5. 自检
一键离线安装.bat -c
```

> `--python-version / --implementation / --abi / --platform` 四个参数让 pip 在
> **当前机器**上也能确定性地抓取 cp312-win_amd64 轮子；若制作机本身就是
> Python 3.12 且不跨版本，也可省略。

---

## 八、与 `环境必要\install_pip环境.bat` 的关系

| | `环境必要\install_pip环境.bat` | 本离线包 |
| --- | --- | --- |
| 网络 | 需要（清华源） | **完全不需要** |
| Python 版本 | 3.10 / 3.11 / 3.12 / 3.13 都试 | 只支持 3.12（轮子已按 cp312 固定） |
| 版本可复现 | 每次装到最新 | 53 个包版本**全部锁定** |
| 完整性校验 | 无 | 102 个文件逐个比对 SHA256 |
| 模块校验 | 4 项 | 16 项模块 + HOG 专检 + 版本快照 |
| 适用场景 | 开发机 | **考场 / 内网 / 比赛现场等断网机器** |

两者可以配合：有网时用前者顺手，进考场前用后者保证一致。

---

## 九、本包的实测验证记录

出包时在**干净的虚拟环境**（`py -3.12 -m venv`，无任何第三方包）里做过完整验证：

| 验证项 | 结果 |
| --- | --- |
| `install_offline.py`（`--no-index` 全离线安装） | 成功，16 项模块校验 + HOGDescriptor 全部 `[OK]` |
| `opencv-python` 实装版本 | `4.14.0.94`（4.x，`HOGDescriptor` 存在） |
| `11_摄像头AI识别\自检.py`（用离线装好的解释器跑） | **40 项通过，0 项失败** |
| 可选链离线解析（`pip install --dry-run --no-index`） | 可解，`torch 2.14.1 / torchvision 0.29.1 / ultralytics 8.4.178`，且 `opencv-python` 保持 4.14.0.94 未被升到 5.x |
| `清单_SHA256.txt` | 102 个文件全部比对通过 |
| `一键离线安装.bat` 退出码 | `-c` / `-h` 返回 0 |

> 本机（开发机）Python 3.12 当前还**缺 `gmssl` 和 `pyserial`** 两个包，
> 跑 `一键离线安装.bat -v` 会看到 `[失败] gmssl`；走一遍菜单 `[1]` 即可补齐
> （其余包本机已满足，pip 会直接跳过）。

---

*本离线包由项目依赖静态扫描生成：扫描了全仓 454 个 Python 文件的 import 语句、
各子方案的 `安装环境.py` / `requirements*.txt` / 安装类 `.bat`，汇总出上表，
再按 `Windows x86_64 + CPython 3.12` 拉取对应 wheel。*
