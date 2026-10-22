# 考生数据保护（专题 05）

> 对应 [考生数据保护.md](考生数据保护.md)：数据分类分级 / 全生命周期控制 /
> 工程技术措施 / 管理措施 / 考生权利响应 / 验收指标。
> 本目录是**可运行、可测试、有可核查证据**的工程实现。
>
> **核心铁律（本版新增）**：已加密的考生数据**任何人都不能修改**，
> 只能**删除**或**新增**。加密由 **哈希（SM3）+ 非对称签名（SM2）** 组成，
> 任何绕过业务层的直接改库都会被验签检出。

## 快速开始

双击 **「运行自检与测试.bat」**（语法检查 → 单元测试 174 项 → 端到端自检 110 项 → 桌面界面冒烟 16 项 → 6 项验收指标）。
全部通过后，双击 **「启动服务.bat」** 打开**桌面管理界面**（Python / tkinter，六标签页）。

命令行（在本目录下）：

```bat
python cli.py summary      :: 摘要 + 6 项 KPI
python cli.py kpi          :: 仅验收指标（要求/实测/证据）
python cli.py report       :: 生成报告并导出（--stdout 可打印全文）
python cli.py verify       :: 审计哈希链校验（篡改可定位）
python cli.py demo         :: 生成演示数据（带 demo 标记）
python cli.py keyring      :: 密钥环体检（换电脑后密钥解不开先看这个）
python gui.py              :: 直接启动桌面管理界面
```

> 需要 Python 3.10+（界面用 **tkinter**，随官方 Python 自带，无需 pip 安装），
> 并安装加密后端：`python -m pip install gmssl cryptography`
> （国密 SM4/SM3/SM2 优先；缺 `gmssl` 时自动回退 AES-256-GCM + SHA-256 + RSA-2048，且**如实标注**不谎报）。

### 启动脚本

| 脚本 | 用途 |
| --- | --- |
| `安装依赖.bat` | 安装 gmssl / cryptography，并自检 tkinter 与加密后端 |
| `启动服务.bat` | 启动**桌面管理界面**（`gui.py`，Python / tkinter） |
| `停止服务.bat` | 关闭桌面管理界面 |
| `切换命令行窗口.bat` | 在「显示/隐藏命令行窗口」间切换 |
| `运行自检与测试.bat` | 一键跑语法检查 + 单元测试 + 自检 + 桌面界面冒烟 + KPI |
| `查看验收报告.bat` | 生成并打开验收报告 |
| `修复密钥环.bat` | 密钥体检 + 备份后重建密钥（换电脑/换账户后界面解不开数据时用） |

> 启动脚本一律是 **GBK（ANSI）编码且不调用 `chcp`**。原因：`.bat` 里若写
> `chcp 65001` 而双击时控制台初始代码页是 936，cmd 读文件的偏移会错位，
> 中文行被从半个字中间截断后当命令执行，屏幕上就是一串
> `'owner' is not recognized as an internal or external command`。
> 因此**不要**用 UTF-8 编辑器另存这些 .bat，也不要在里面加 `chcp`。

账户：`owner/owner`（所有者）、`dpo1/dpo1`（数据保护负责人）、
`auditor1/auditor1`（审计员）、`oper1/oper1`（业务操作员）、`inv01/inv01`（监考员）。

## 它做了什么

| 规范章节 | 落地 | 证据 |
| --- | --- | --- |
| 一 数据分类分级 | `dpp/catalog.py` 六类数据唯一口径 | `cli.py catalog`；目录自洽性校验 |
| 二 全生命周期 | `dpp/lifecycle.py` 采集→传输→存储→使用→共享→销毁 | 每步返回证据；审计链留痕 |
| 三 技术措施 | 国密优先加密、不可逆模板、DPAPI 密钥、脱敏、水印、权限最小化、持续校验、审计哈希链 | `tests/` 174 项；`cli.py algorithm` |
| **三 只读不可改** | **`dpp/integrity.py` SM3 摘要 + SM2 签名；`signatures` 表 + SQLite 触发器纵深防御** | **`cli.py kpi` 第 6 项；桌面界面「完整性验签」页；只读探针** |
| 四 管理措施 | `dpp/governance.py` 台账、替代通道、2 小时上报 | 台账 + 演练记录 |
| 五 权利响应 | `dpp/rights.py` 知情/查阅/复制/更正/删除/撤回同意 | SLA + 撤回即时生效 + **更正 = 删旧+增新** |
| 六 验收指标 | `dpp/governance.py:acceptance()` | `cli.py kpi`；样本不足如实标注 |

## 不可修改（只读）的实现要点

三重纵深防御，任何一层被绕过，下一层仍能发现：

1. **业务层**：`storage.save_candidate()` 已存在即抛 `ReadOnlyViolation`；
   `update_candidate_meta()` 只放行 `legal_hold / legal_hold_reason / consent_id`。
2. **数据库层**：SQLite `BEFORE UPDATE` 触发器按字段比对（`WHEN OLD.x IS NOT NEW.x`），
   改动加密载荷/身份/生物特征列直接 `RAISE(ABORT)`。
3. **密码学层**：每条记录落 `signatures`（SM3 摘要 + SM2 签名）。
   攻击者即使卸掉触发器直接 UPDATE 密文，也**改不出对应签名**（无私钥），
   `verify_all_records()` 立刻报出被篡改的记录。

合规的"更正"因此改为 **DELETE(旧) + INSERT(新)**，全程留痕，链路上不存在原地修改。

## 目录结构

```
05_考生数据保护/
├─ 考生数据保护.md          规范原文
├─ 使用说明.md              完整使用文档
├─ main.py                 端到端自检（110 项）
├─ cli.py                  命令行入口
├─ gui.py                  桌面管理界面（Python / tkinter，六标签页）
├─ demo_data.py            演示数据生成
├─ integration_api.py      供 00_集成联动 调用的 JSON 接口（子进程）
├─ dpp/                    核心包（见 文档/程序结构.txt）
│   ├─ integrity.py        SM3 哈希 + SM2 签名 + 只读可验证
│   ├─ secure_store.py     主密钥安全包裹（DPAPI/Fernet）+ DPAPI 失败诊断
│   └─ keyring.py          密钥环体检 / 备份 / 重置
├─ tests/                  单元测试（174 项）
├─ 文档/程序结构.txt        程序结构说明
├─ 安装依赖.bat / 启动服务.bat / 停止服务.bat
├─ 切换命令行窗口.bat / 运行自检与测试.bat / 查看验收报告.bat
└─ 修复密钥环.bat           密钥解不开时：备份 + 重建密钥（见上文"换电脑"一节）
```

## 换电脑 / 换 Windows 账户后"密钥解不开"怎么办

主密钥与签名私钥是用 **Windows DPAPI** 包裹后存在 `data\keyring\` 的，
它**只认生成它的那个 Windows 用户 + 那台机器**。把整个目录拷到别的电脑或别的
Windows 账户下，文件还在、但本机没有对应的 DPAPI 主密钥，于是必然报：

```
OSError: DPAPI 解密失败：0x8009000B（NTE_BAD_KEY_STATE）
```

这是 DPAPI 的设计，**不是程序故障，也无法绕过**——只有两条路：

1. 回到当初生成密钥的那个 Windows 账户/电脑上跑；
2. 重置密钥环（旧密钥加密的数据只能重建）：

```bat
python cli.py keyring                                   :: 先体检：每个密钥文件能否解开
python cli.py keyring --reset --yes --with-demo         :: 整目录备份后重建密钥 + 演示数据
```

或直接双击 **「修复密钥环.bat」**（会先让你确认）。重置前会把 `data\` 整目录备份成
同级的 `data_backup_<时间戳>\`，旧文件一个都不会丢，只是**解不开了**。

## 安装包加密与防解包（exe 发布）

> 完整的命令、密钥派生与改密码步骤见 [使用说明.md](使用说明.md) 的
> 「八、加密、完整性与密钥 → 安装包加密与防解包」。这里只列要点。

- **加密单文件 exe**：`一键打包.sh` 不再把 payload 明文追加，而是用
  **AES-256-GCM（1 MB 分块，每块校验）** 加密 `payload.zip`，再追加 40 字节
  `ZKINS002` 尾部索引；启动器只有在密钥、salt、自身 stub 哈希三者匹配时才解得开。
- **安装密码**：首次启动输入 **`123456`**（PBKDF2-HMAC-SHA256，120000 轮）；
  通过后写隐藏的 `.install_auth`（`v4`），旧版 `v3` 认证文件自动失效。
- **防调试**：`IsDebuggerPresent` / `CheckRemoteDebuggerPresent`，启动时与解包前各查一次；
  不扫描进程、不调用 `ntdll`，避免被安全软件按“黑客工具”误杀。
- **源码硬化**：解包后业务 `.py` 编译为 `optimize=2` 的 `.pyc`，用每目录随机
  `.zk_key` 异或加密，`.py` 替换为内存加载器；安装目录隐藏。
- **边界**：这是"提高逆向/提取门槛"，不是不可破解的 DRM；安装后本机仍需可读才能运行。
  正式交付应改用强安装密码，并叠加代码签名/授权服务器/原生编译。

## 与其他专题联动

`00_集成联动` 通过 `integration_api.py`（子进程 + JSON）调用本专题，已接通的联动能力：

| 方法 | 作用 |
| --- | --- |
| `protect_candidate(...)` | 受保护登记一名考生（加密+签名） |
| `handoff_candidate(...)` | 取「最小必要」核验包（编号/座次/考场，不含姓名证件号图像） |
| `redact(text)` | 统一脱敏口径 |
| `check_transport(...)` | 传输合规校验（专网 + 加密） |
| `verify_data_integrity()` | 全量完整性验签（是否被篡改） |
| `readonly_probe()` | 只读探针（试改加密数据，必须被拒） |

## 设计原则

1. **控制即留痕** —— 不产生可核查证据的控制，等于没做。
2. **绝不谎报** —— 加密/签名算法以实际后端为准；水印没落到图上就标注"未落图"。
3. **样本不足 ≠ 达标** —— 指标无样本时如实显示"样本不足"并判未达标，绝不用 0/100% 冒充。
4. **不可改就真的改不动** —— 业务层拒绝 + 触发器兜底 + 验签检出，三重纵深防御。
5. **不接管相邻专题** —— 与 02/03 的库、密钥、目录相互独立，只在分类分级目录里登记其口径。

详见 [使用说明.md](使用说明.md) 与 [文档/程序结构.txt](文档/程序结构.txt)。
