# -*- coding: utf-8 -*-
r"""
考生数据保护（专题 05）· 工程实现包

对应《考生数据保护.md》，与项目三《合规与数据治理》配套：
  项目三定规则，本包给**工程实现与可核查证据**。

模块导航：
  config          路径、角色动作矩阵、采集/传输/销毁阈值、验收指标口径
  catalog         数据分类分级目录（第一节）—— 唯一口径
  crypto          字段级加密（国密 SM4 优先，回退 AES-256-GCM）+ 不可逆模板
  secure_store    主密钥安全包裹（Windows DPAPI / Fernet；无后端拒绝落盘）+ DPAPI 失败诊断
  keyring         密钥环体检 / 整目录备份 / 重置（换机换用户导致密钥解不开时用）
  desensitize     掩码、去标识化展示、日志脱敏、动态水印
  models          数据模型 + 审计哈希链原语
  storage         SQLite：考生/同意/使用/解密/共享/销毁/权利/通道/措施/事件/审计
  access          账户、角色权限最小化、可见范围隔离、持续校验
  lifecycle       全生命周期控制（采集→传输→存储→使用→共享→销毁）
  rights          考生权利响应（知情/查阅/复制/更正/删除/撤回同意）
  governance      管理措施台账、替代通道与演练、应急上报、验收指标计算
  report          验收报告 / 处理活动登记表 / 销毁记录台账
"""
__version__ = "1.0.0"
__all__ = ["config", "catalog", "crypto", "secure_store", "keyring", "desensitize",
           "models", "storage", "access", "lifecycle", "rights", "governance", "report"]
