# -*- coding: utf-8 -*-
"""考生数据保护 · 单元测试包

组织方式（与项目内 02/03 的 tests/ 保持一致：纯 unittest，无第三方依赖）：

    python -m unittest discover -s tests -v          # 全部
    python -m unittest tests.test_crypto -v          # 单个

测试覆盖《考生数据保护.md》的关键承诺：
    test_catalog.py      数据分类分级目录自洽性
    test_crypto.py       国密优先加密、不可逆模板、密钥保护、落库即密文
    test_desensitize.py  脱敏 / 去标识化 / 日志净化 / 水印
    test_lifecycle.py    全生命周期（同意前置、目的限定、共享最小化、自动销毁）
    test_permissions.py  权限最小化与越权拦截、持续校验风险评分
    test_audit.py        审计哈希链与篡改检测
    test_rights.py       考生权利响应（核验身份、撤回同意即时生效、SLA）
    test_governance.py   管理措施 / 替代通道 / 验收指标（样本不足语义）

所有测试都在临时目录里跑，绝不触碰真实运行目录。
"""
