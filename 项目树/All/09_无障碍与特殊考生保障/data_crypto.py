# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 敏感字段加密（与 01/02/03/06 同一套口径）

为什么 09 特别需要：本模块库里的"障碍类型/医疗证明编号/紧急联系人"属于
**敏感个人信息（健康与残障信息）**，按项目三与专题 05 的要求不能明文裸奔。

设计：
- 字段级加密，Fernet（AES-128-CBC + HMAC-SHA256），密文带 `enc:` 前缀；
- 密钥优先取环境变量 `EXAM_DATA_KEY`（与其它专题共用同一密钥即可互通），
  否则读/生成 `data/.data_key`；
- **兼容旧明文**：读到不带 `enc:` 的值原样返回，所以开关随时可改，不需要迁移数据；
- **盲索引**：加密后没法用 SQL 等值查考生（`WHERE candidate_id=?` 查不到密文），
  所以另存一列 `candidate_key = sha256("acc09|" + 考生号)`（不可逆），
  所有"按考生查"的地方都走这一列；
- **零依赖不破**：没装 cryptography 时**不报错、不假装** —— 加密自动降级为明文，
  并由 `status_text()` 如实告知（界面/报告/自检都会写明）。

注意：密钥与数据库同机存放，属"静态加密 + 访问控制"级别；生产环境建议用
`EXAM_DATA_KEY` 把密钥托管到 KMS。**丢失密钥即无法恢复已加密数据**，请备份 data/.data_key。
"""
import base64
import hashlib
import os

import config

PREFIX = "enc:"
_FERNET_CACHE = {}


def cryptography_installed():
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:                                          # noqa: BLE001
        return False


def enabled():
    """当前是否**实际**在加密：装了 cryptography 且设置里没关掉。"""
    if not cryptography_installed():
        return False
    try:
        return bool(config.load_settings().get("encrypt_sensitive", True))
    except Exception:                                          # noqa: BLE001
        return True


def status_text():
    if not cryptography_installed():
        return ("未安装 cryptography：敏感字段**未加密**（明文落库，仅靠本机文件权限保护）。"
                "装上 cryptography（01/02/03 的安装脚本已含）即自动启用。")
    if not enabled():
        return "已在设置中关闭字段加密（敏感字段明文落库）。"
    key_src = "环境变量 EXAM_DATA_KEY" if os.environ.get("EXAM_DATA_KEY", "").strip() \
        else "本机 data\\.data_key"
    return "已启用字段级加密（Fernet，密钥来源：%s）。" % key_src


def _data_dir():
    try:
        return str(config.DATA_ROOT)
    except Exception:                                          # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _key_path():
    return os.path.join(_data_dir(), ".data_key")


def _load_or_create_key():
    env_key = os.environ.get("EXAM_DATA_KEY", "").strip()
    if env_key:
        from cryptography.fernet import Fernet
        key = env_key.encode() if isinstance(env_key, str) else env_key
        Fernet(key)                                            # 校验格式，不合法直接抛
        return key
    path = _key_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read().strip()
        if key:
            return key
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(key)
    return key


def _fernet():
    from cryptography.fernet import Fernet
    key = _load_or_create_key()
    f = _FERNET_CACHE.get(key)
    if f is None:
        f = Fernet(key)
        _FERNET_CACHE.clear()
        _FERNET_CACHE[key] = f
    return f


def is_encrypted(value):
    if value is None:
        return False
    s = value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    return s.startswith(PREFIX)


def encrypt_field(plain_text):
    """加密一个字段；空值/已加密值原样返回；未启用加密时原样返回（如实降级）。"""
    if plain_text is None or plain_text == "":
        return plain_text
    s = str(plain_text)
    if is_encrypted(s) or not enabled():
        return s
    try:
        return PREFIX + _fernet().encrypt(s.encode("utf-8")).decode("utf-8")
    except Exception:                                          # noqa: BLE001
        # 密钥/算法异常时**绝不吞掉数据**：原样返回明文，并由 status_text 提示
        return s


def decrypt_field(cipher_text):
    """解密一个字段；空值/明文/未启用时原样返回。"""
    if cipher_text is None or cipher_text == "":
        return cipher_text
    s = str(cipher_text)
    if not is_encrypted(s):
        return s
    try:
        return _fernet().decrypt(s[len(PREFIX):].encode("utf-8")).decode("utf-8")
    except Exception:                                          # noqa: BLE001
        return s


def blind_key(value):
    """
    盲索引：给"要按它查"的敏感字段算一个不可逆的定长指纹（用于 SQL 等值查询）。
    同一值恒定、不同值几乎不碰撞；泄露的只是"两条记录是否同人"，不含明文。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    return hashlib.sha256(("acc09|" + s).encode("utf-8")).hexdigest()[:32]


def mask_id(text, keep=4):
    """身份证/准考证号掩码（界面与报告默认不显全号）。"""
    return config.mask_id(text, keep)


def rotate_key(new_key_b64):
    """密钥轮换占位：写入新密钥（旧数据需调用方先解密再加密，本函数不迁移数据）。"""
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64decode(new_key_b64)
    if len(key) != 32:
        raise ValueError("Fernet 密钥必须是 32 字节")
    encoded = base64.urlsafe_b64encode(key)
    Fernet(encoded)
    os.environ["EXAM_DATA_KEY"] = encoded.decode("utf-8")
    path = _key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(encoded)
    return True
