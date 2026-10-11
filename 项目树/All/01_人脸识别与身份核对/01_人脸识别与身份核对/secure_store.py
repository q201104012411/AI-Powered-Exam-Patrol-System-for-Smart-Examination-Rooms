# -*- coding: utf-8 -*-
"""
特征索引的静态加密存储。

后端优先级：
1. Windows DPAPI（CryptProtectData）—— 免依赖，密钥绑定当前用户/机器，密文拷走也无法解密；
2. cryptography.Fernet（若已安装）—— 跨平台，密钥保存在本地 key 文件（权限收紧）；
3. 明文（仅在显式设置 ALLOW_PLAINTEXT_INDEX=1 时启用，会在日志中留下警告）。

说明：不自行实现密码算法；无可用后端时**拒绝落盘**，改为内存态索引，
宁可牺牲便利，也不静默地把生物特征明文写到磁盘上。
"""
import os
import sys

MAGIC_DPAPI = b"DPAPI1\n"
MAGIC_FERNET = b"FERNET1\n"
MAGIC_PLAIN = b"PLAIN1\n"


class SecureStoreUnavailable(RuntimeError):
    pass


# ---------------- 后端 1：Windows DPAPI ----------------
def _dpapi_available():
    return sys.platform.startswith("win")


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                      ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                        ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败（错误码 %d）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------- 后端 2：cryptography.Fernet ----------------
def _fernet():
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except Exception:
        return None
    import config
    key_path = config.INDEX_KEY_PATH
    if not key_path.exists():
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:                                    # 尽量收紧权限（Windows 上为占位）
            os.chmod(str(key_path), 0o600)
        except OSError:
            pass
    return Fernet(key_path.read_bytes().strip())


# ---------------- 对外接口 ----------------
def backend_name() -> str:
    if _dpapi_available():
        return "dpapi"
    if _fernet() is not None:
        return "fernet"
    if os.environ.get("ALLOW_PLAINTEXT_INDEX") == "1":
        return "plaintext"
    return "none"


def encrypt(data: bytes) -> bytes:
    b = backend_name()
    if b == "dpapi":
        return MAGIC_DPAPI + _dpapi(data, protect=True)
    if b == "fernet":
        return MAGIC_FERNET + _fernet().encrypt(data)
    if b == "plaintext":
        return MAGIC_PLAIN + data
    raise SecureStoreUnavailable("无可用加密后端，拒绝将特征索引明文落盘")


def decrypt(blob: bytes) -> bytes:
    if blob.startswith(MAGIC_DPAPI):
        return _dpapi(blob[len(MAGIC_DPAPI):], protect=False)
    if blob.startswith(MAGIC_FERNET):
        f = _fernet()
        if f is None:
            raise SecureStoreUnavailable("该索引由 Fernet 加密，但当前环境缺少 cryptography")
        return f.decrypt(blob[len(MAGIC_FERNET):])
    if blob.startswith(MAGIC_PLAIN):
        return blob[len(MAGIC_PLAIN):]
    raise SecureStoreUnavailable("索引文件格式无法识别（可能被篡改或版本不匹配）")


def write_encrypted(path, data: bytes):
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(encrypt(data))
    os.replace(tmp, str(path))


def read_encrypted(path) -> bytes:
    with open(str(path), "rb") as f:
        return decrypt(f.read())
