# -*- coding: utf-8 -*-
"""
考场敏感数据 · 字段级加密工具

设计原则：
- 不引入新的 pip 依赖（依赖 install_env.py 已安装的 cryptography）；
- 加密密钥优先从环境变量 EXAM_DATA_KEY 读取，否则自动生成并保存到 data/.data_key；
- 使用 Fernet（AES-128-CBC + HMAC-SHA256）做字段级对称加密；
- 加密后的字符串带 "enc:" 前缀，便于识别已加密字段并兼容旧明文数据；
- 所有加密/解密函数对空值安全，空字符串/None 原样返回。

注意：.data_key 文件位于 data/ 目录下，该目录已被 .gitignore 排除，不会提交到仓库。
"""
import base64
import os

import config


def _data_dir():
    """返回数据目录（优先用 config.DATA_ROOT，回退当前目录下的 data）"""
    try:
        return str(config.DATA_ROOT)
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _key_path():
    return os.path.join(_data_dir(), ".data_key")


def _load_or_create_key():
    """加载或创建 32 字节 Fernet 密钥。"""
    env_key = os.environ.get("EXAM_DATA_KEY", "").strip()
    if env_key:
        try:
            # 校验是否是合法 Fernet 密钥
            from cryptography.fernet import Fernet
            _ = Fernet(env_key.encode() if isinstance(env_key, str) else env_key)
            return env_key.encode() if isinstance(env_key, str) else env_key
        except Exception as e:
            raise RuntimeError("环境变量 EXAM_DATA_KEY 不是有效的 Fernet 密钥：%s" % e)

    path = _key_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read().strip()
        if key:
            return key

    # 生成新密钥并保存
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(key)
    return key


_PREFIX = "enc:"
_FERNET_CACHE = {}


def _fernet():
    """延迟初始化 Fernet 并缓存（避免每个字段都重新派生密钥）。"""
    from cryptography.fernet import Fernet
    key = _load_or_create_key()
    f = _FERNET_CACHE.get(key)
    if f is None:
        f = Fernet(key)
        _FERNET_CACHE.clear()
        _FERNET_CACHE[key] = f
    return f


def _is_encrypted(value):
    """判断一个值是否已经是加密字符串。"""
    if value is None:
        return False
    s = value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    return s.startswith(_PREFIX)


def encrypt_field(plain_text):
    """加密一个文本字段；空值/已加密值原样返回。"""
    if plain_text is None or plain_text == "":
        return plain_text
    s = str(plain_text)
    if _is_encrypted(s):
        return s
    token = _fernet().encrypt(s.encode("utf-8"))
    return _PREFIX + token.decode("utf-8")


def decrypt_field(cipher_text):
    """解密一个文本字段；空值/明文原样返回。"""
    if cipher_text is None or cipher_text == "":
        return cipher_text
    s = str(cipher_text)
    if not _is_encrypted(s):
        return s
    token = s[len(_PREFIX):].encode("utf-8")
    return _fernet().decrypt(token).decode("utf-8")


def encrypt_bytes(data):
    """加密二进制数据；空 bytes/None 原样返回。返回 base64 编码的字符串（带 enc: 前缀）。"""
    if data is None or len(data) == 0:
        return data
    token = _fernet().encrypt(data)
    return _PREFIX + token.decode("utf-8")


def decrypt_bytes(cipher_text):
    """解密二进制数据；空/None 原样返回。返回 bytes。"""
    if cipher_text is None or cipher_text == "":
        return cipher_text
    s = cipher_text if isinstance(cipher_text, str) else cipher_text.decode("utf-8")
    if not _is_encrypted(s):
        return s.encode("utf-8") if isinstance(s, str) else s
    token = s[len(_PREFIX):].encode("utf-8")
    return _fernet().decrypt(token)


def mask_ticket_no(ticket_no):
    """准考证号掩码：保留前 3 位和后 2 位，中间用 * 替换。"""
    if not ticket_no:
        return ""
    s = str(ticket_no)
    if len(s) <= 5:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def mask_seat_no(seat_no):
    """座位号掩码：只保留最后 2 位。"""
    if not seat_no:
        return ""
    s = str(seat_no)
    if len(s) <= 2:
        return "*" * len(s)
    return "*" * (len(s) - 2) + s[-2:]


def rotate_key(new_key_b64):
    """
    密钥轮换占位接口：把新密钥写入环境变量/文件，旧数据需由调用方先解密再加密。
    本函数仅做新密钥校验与写入，不主动迁移数据。
    """
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64decode(new_key_b64)
    if len(key) != 32:
        raise ValueError("Fernet 密钥必须是 32 字节")
    # 校验格式
    encoded = base64.urlsafe_b64encode(key)
    Fernet(encoded)
    os.environ["EXAM_DATA_KEY"] = encoded.decode("utf-8")
    path = _key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(encoded)
    return True
