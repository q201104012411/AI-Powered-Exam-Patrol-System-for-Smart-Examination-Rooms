# -*- coding: utf-8 -*-
"""
身份证号等敏感字段的哈希与脱敏工具（仅依赖标准库，人脸端与网页端共用）。

落库只存三样，都不含明文：
  - id_card_hash：**密钥化 HMAC**（`hmac1:` 前缀），密钥经 DPAPI 加密后单独存放，
                  数据库被拷走也无法离线爆破；旧数据仍保留 salt+sha256 方案以兼容
  - id_card_salt：旧方案（每人独立随机盐）使用；新方案留空
  - id_card_masked：掩码串，用于界面展示与人工目视核对

为什么升级：身份证号可猜空间约 10^11（地区码×生日×顺序号），
单次 SHA-256 在 GPU 上几秒就能穷举完，所以"加盐 SHA-256"不足以抵抗离线爆破；
密钥化 HMAC 把"必须同时拿到本机用户身份"变成了爆破前提。
"""
import hashlib
import hmac
import os
import re
from pathlib import Path

import config

ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")
ID_CARD_15_RE = re.compile(r"^\d{15}$")

# 密钥化 HMAC 方案：hash 前缀 + 密钥文件（DPAPI 加密，与数据库分离存放）
HMAC_PREFIX = "hmac1:"
KEY_FILE = str(Path(config.DATA_ROOT) / "id_key.enc")
_KEY_CACHE = None


def _load_key(create=True):
    """
    取身份证 HMAC 密钥。密钥文件用 secure_store（DPAPI）加密，
    因此"数据库 + 密钥文件"一起泄露也无法在其他机器上使用。
    """
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    import secure_store
    if os.path.exists(KEY_FILE):
        try:
            _KEY_CACHE = secure_store.read_encrypted(KEY_FILE)
            return _KEY_CACHE
        except Exception:
            return None
    if not create:
        return None
    key = os.urandom(32)
    try:
        secure_store.write_encrypted(KEY_FILE, key)
    except Exception:
        return None                     # 无可用加密后端 -> 回退旧方案
    _KEY_CACHE = key
    return _KEY_CACHE


def hmac_id_card(id_card, key=None, salt=""):
    """
    密钥化哈希：HMAC-SHA256(key, salt + 规范化号码)。
    既保留每条记录独立随机盐（同一号码在不同记录中哈希不同，无法跨记录关联比对），
    又要求攻击者拿到本机用户身份才能爆破——两层都保住。
    """
    key = key if key is not None else _load_key(create=False)
    if not key:
        return ""
    norm = str(id_card).strip().upper()
    return HMAC_PREFIX + hmac.new(key, (str(salt) + norm).encode("utf-8"),
                                  hashlib.sha256).hexdigest()


def reset_key_cache():
    """测试用：清掉进程内密钥缓存"""
    global _KEY_CACHE
    _KEY_CACHE = None


def new_salt(nbytes=16):
    return os.urandom(nbytes).hex()


def hash_id_card(id_card, salt):
    """带盐哈希：相同身份证号在不同记录中哈希不同，防止跨表碰撞分析。"""
    if id_card is None or salt is None:
        return ""
    norm = str(id_card).strip().upper()
    return hashlib.sha256((salt + norm).encode("utf-8")).hexdigest()


def mask_id_card(id_card):
    """330100199001011234 -> 3301**********1234"""
    if not id_card:
        return ""
    s = str(id_card).strip()
    if len(s) < 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def validate_id_card(id_card, strict_checksum=None):
    """
    格式校验（长度 + 可选校验位）。返回 (ok, msg)。
    strict_checksum 默认取 config.STRICT_ID_CHECKSUM：
      生产环境建议开启（校验位能挡掉大量手误/伪造号码）；
      演示与测试数据可关闭，仅校验 18 位格式。
    """
    if not id_card:
        return False, "身份证号不能为空"
    s = str(id_card).strip().upper()
    if ID_CARD_15_RE.match(s):
        return True, ""
    if not ID_CARD_RE.match(s):
        return False, "身份证号格式不正确（应为 18 位，末位可为 X）"
    if strict_checksum is None:
        try:
            import config
            strict_checksum = config.STRICT_ID_CHECKSUM
        except Exception:
            strict_checksum = True
    if not strict_checksum:
        return True, ""
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    total = sum(int(s[i]) * weights[i] for i in range(17))
    if check[total % 11] != s[17]:
        return False, "身份证号校验位不正确（如为演示数据可关闭 STRICT_ID_CHECKSUM）"
    return True, ""


def register_id_card(id_card):
    """
    登记用：返回 (salt, hash, masked)。

    优先使用**密钥化 HMAC 方案**（hash 带 `hmac1:` 前缀，salt 照旧保留）：
    密钥 32 字节随机，经 Windows DPAPI 加密后存于 <数据目录>/id_key.enc，
    与数据库分离。这样即使 exam.db 被整库拷走，没有本机用户身份也**无法离线爆破**
    （身份证号可猜空间约 10^11，单次 SHA-256 几秒就能跑完，所以旧方案不安全）。
    同时每条记录仍用独立随机盐，避免"同一号码哈希相同"造成跨记录关联。

    取不到加密后端时自动回退旧的"每人加盐 SHA-256"方案，保证功能不中断。
    """
    salt = new_salt()
    key = _load_key(create=True)
    if key:
        return salt, hmac_id_card(id_card, key, salt), mask_id_card(id_card)
    return salt, hash_id_card(id_card, salt), mask_id_card(id_card)


def verify_id_card(id_card, salt, stored):
    """
    核验：按存储格式自动选择算法，兼容历史数据。
      · `hmac1:...` -> 用本机密钥重算 HMAC 比对（换机器/换用户会失败，属预期）
      · 其余（裸 hex）-> 旧的 salt + SHA-256 方案
    比对使用 hmac.compare_digest，避免时序侧信道。
    """
    if not stored:
        return False
    stored = str(stored)
    if stored.startswith(HMAC_PREFIX):
        key = _load_key(create=False)
        if not key:
            return False                      # 记录是密钥方案，但本机取不到密钥
        return hmac.compare_digest(hmac_id_card(id_card, key, salt), stored)
    return hmac.compare_digest(hash_id_card(id_card, salt), stored)


def id_hash_scheme(stored):
    """给状态页用：返回该记录使用的哈希方案名称"""
    if not stored:
        return "未登记"
    return "密钥化HMAC(hmac1)" if str(stored).startswith(HMAC_PREFIX) else "旧版加盐SHA256"


def mask_generic(text, keep_head=4, keep_tail=4):
    """通用脱敏，用于日志里防止意外落明文。"""
    if not text:
        return ""
    s = str(text)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]


# 日志脱敏：把 15/18 位数字串（疑似身份证）与 11 位手机号替换掉
_DIGIT18 = re.compile(r"\b\d{17}[\dXx]\b")
_DIGIT15 = re.compile(r"\b\d{15}\b")
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")


def sanitize_for_log(text):
    """写入日志前调用：抹掉疑似身份证号与手机号。"""
    if not text:
        return ""
    s = str(text)
    s = _DIGIT18.sub(lambda m: mask_id_card(m.group(0)), s)
    s = _DIGIT15.sub(lambda m: mask_generic(m.group(0)), s)
    s = _PHONE.sub(lambda m: mask_generic(m.group(0), 3, 4), s)
    return s
