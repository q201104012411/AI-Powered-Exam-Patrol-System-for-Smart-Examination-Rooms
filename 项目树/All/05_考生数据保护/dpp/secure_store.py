# -*- coding: utf-8 -*-
r"""
主密钥的安全包裹存储。

后端优先级：
1. Windows DPAPI（CryptProtectData）—— 免依赖，密钥绑定当前用户/机器，
   密文拷到别的机器/别的用户下解不开；
2. cryptography.Fernet —— 跨平台，密钥保存在本地 key 文件（权限收紧）；
3. 无后端 —— **拒绝落盘**，抛 SecureStoreUnavailable。

为什么不在无后端时"用明文兜底"：主密钥一旦明文落盘，
前面所有字段级加密都退化成"只是换了个编码"，会给人"已加密"的错觉。
宁可启动失败，也不静默降级。

本模块另外负责把 DPAPI 的失败**讲清楚**：
`ctypes.windll.crypt32` 不会捕获 GetLastError，直接报 "错误码 0"，
真正的错误码（如 0x8009000B）必须用 WinDLL(..., use_last_error=True) 才拿得到。
0x8009000B 的典型成因：keyring\ 目录是从**别的 Windows 用户/别的电脑**拷贝过来的，
本机 profile 里没有包裹它的 DPAPI 主密钥 —— 这不是 bug，是 DPAPI 的设计，
只能重置密钥环（见 dpp/keyring.py）。
"""
import ctypes
import os
import sys
import uuid

from dpp import config

MAGIC_DPAPI = b"DPAPI1\n"
MAGIC_FERNET = b"FERNET1\n"

# DPAPI 数据块里的 provider GUID：{df9d8cd0-1501-11d1-8c7a-00c04fc297eb} = 用户级 DPAPI
DPAPI_PROVIDER = "df9d8cd0-1501-11d1-8c7a-00c04fc297eb"

# HRESULT → (CryptoAPI 名称, 中文解释与处置建议)
DPAPI_ERRORS = {
    0x8009000B: ("NTE_BAD_KEY_STATE",
                 "该密钥由**其它 Windows 用户或其它电脑**的 DPAPI 主密钥包裹，"
                 "本机 profile 里没有这把主密钥，无法解开。"
                 "（整目录拷贝到新机器/新账户后必然如此）"
                 "处置：用 cli.py keyring --reset 重置密钥环，"
                 "或换回当初生成密钥的那个 Windows 账户/电脑。"),
    0x80090005: ("NTE_BAD_DATA", "密文不是合法的 DPAPI 数据或已损坏（可能是被文本方式改写/截断）"),
    0x80090006: ("NTE_BAD_SIGNATURE", "DPAPI 完整性校验失败：密文被改动过"),
    0x8009000D: ("NTE_NO_KEY", "当前用户 profile 里没有任何 DPAPI 主密钥"),
    0x80070005: ("E_ACCESSDENIED", "系统拒绝访问（权限不足或运行在受限沙箱里）"),
}


class SecureStoreError(RuntimeError):
    """密钥包裹/解包失败的基类（GUI 与 CLI 都按它给中文提示）。"""


class SecureStoreUnavailable(SecureStoreError):
    """本机既没有 DPAPI 也没有 cryptography：拒绝把主密钥明文落盘。"""


class ForeignKeyError(SecureStoreError):
    """密钥由别的 Windows 用户/别的电脑包裹，本机解不开（不可恢复）。"""


class KeyBlobError(SecureStoreError):
    """密钥文件本身损坏或格式不认识。"""


# ---------------------------------------------------------------- DPAPI
def _dpapi_available():
    return sys.platform.startswith("win")


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_LIBS = {}


def _libs():
    """按需装载 crypt32/kernel32，并声明 use_last_error=True 以拿到真实错误码。

    注意：必须用 WinDLL(..., use_last_error=True) —— 用 ctypes.windll.crypt32
    时 ctypes.get_last_error() 恒为 0，错误码会被吞掉（这就是"错误码 0"的来源）。
    """
    if _LIBS:
        return _LIBS["crypt32"], _LIBS["kernel32"]
    if not _dpapi_available():
        raise SecureStoreUnavailable("当前系统不是 Windows，没有 DPAPI 可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.POINTER(_DATA_BLOB),          # pDataIn
                       ctypes.POINTER(ctypes.c_wchar_p),    # ppszDataDescr / szDataDescr
                       ctypes.POINTER(_DATA_BLOB),          # pOptionalEntropy
                       ctypes.c_void_p,                     # pvReserved
                       ctypes.c_void_p,                     # pPromptStruct
                       ctypes.c_ulong,                      # dwFlags
                       ctypes.POINTER(_DATA_BLOB)]          # pDataOut
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    _LIBS["crypt32"] = crypt32
    _LIBS["kernel32"] = kernel32
    return crypt32, kernel32


def describe_error(code):
    """把 DPAPI 错误码翻译成 (名称, 中文建议)。兼容按有符号传来的负值。"""
    code &= 0xFFFFFFFF
    name, text = DPAPI_ERRORS.get(code, ("未知错误", "未收录的 DPAPI 错误"))
    return name, text


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = _libs()

    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _DATA_BLOB(len(data),
                         ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()

    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ctypes.set_last_error(0)
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        code = ctypes.get_last_error() & 0xFFFFFFFF
        name, text = describe_error(code)
        msg = "DPAPI %s 失败：0x%08X（%s）\n%s" % (
            "加密" if protect else "解密", code, name, text)
        if code == 0x8009000B:
            raise ForeignKeyError(msg)
        raise SecureStoreError(msg)
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------- Fernet
def _fernet():
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    key_path = config.KEYRING_DIR / "wrap.key"
    if not key_path.exists():
        key_path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(str(key_path), 0o600)
        except OSError:
            pass
    try:
        return Fernet(key_path.read_bytes().strip())
    except Exception:
        return None


# ---------------------------------------------------------------- 对外
def backend_name():
    if _dpapi_available():
        return "dpapi"
    if _fernet() is not None:
        return "fernet"
    return "none"


def encrypt(data: bytes) -> bytes:
    b = backend_name()
    if b == "dpapi":
        return MAGIC_DPAPI + _dpapi(data, protect=True)
    if b == "fernet":
        return MAGIC_FERNET + _fernet().encrypt(data)
    raise SecureStoreUnavailable(
        "无可用密钥保护后端（Windows DPAPI / cryptography 均不可用），拒绝明文落盘主密钥")


def decrypt(blob: bytes) -> bytes:
    if blob.startswith(MAGIC_DPAPI):
        return _dpapi(blob[len(MAGIC_DPAPI):], protect=False)
    if blob.startswith(MAGIC_FERNET):
        f = _fernet()
        if f is None:
            raise SecureStoreUnavailable("该密钥由 Fernet 包裹，但当前环境缺少 cryptography")
        return f.decrypt(blob[len(MAGIC_FERNET):])
    raise KeyBlobError("密钥文件格式无法识别（可能被篡改或版本不匹配）")


# ---------------------------------------------------------------- 诊断
def dpapi_selftest():
    """DPAPI 在本机当前用户下能否正常加解密（只做一次内存往返，不落盘）。"""
    if not _dpapi_available():
        return False, "非 Windows 平台，无 DPAPI"
    try:
        probe = os.urandom(16)
        if _dpapi(_dpapi(probe, protect=True), protect=False) != probe:
            return False, "DPAPI 往返结果不一致"
        return True, "DPAPI 可用（绑定当前 Windows 用户）"
    except SecureStoreError as exc:
        return False, str(exc).splitlines()[-1]


def blob_master_key(blob: bytes):
    """从 DPAPI 数据块里读出包裹者（本机 profile 的）主密钥 GUID；读不到返回 None。

    结构：DWORD 版本 | GUID provider | DWORD 主密钥版本 | GUID 主密钥 | ...
    """
    if not blob.startswith(MAGIC_DPAPI):
        return None
    raw = blob[len(MAGIC_DPAPI):]
    if len(raw) < 40:
        return None
    try:
        provider = str(uuid.UUID(bytes_le=raw[4:20]))
        master = str(uuid.UUID(bytes_le=raw[24:40]))
    except Exception:                                        # noqa: BLE001
        return None
    return {"provider": provider, "master_key": master,
            "user_level": provider.lower() == DPAPI_PROVIDER}


def windows_master_key_ids():
    """本机当前用户 profile 里的 DPAPI 主密钥 GUID 集合（用于判断密钥是不是本机的）。

    不调用任何 WinAPI：Protect 目录就是 %APPDATA%\\Microsoft\\Protect\\<SID>\\，
    直接列出来即可，避免 ctypes 句柄用错导致进程崩溃。
    """
    if not _dpapi_available():
        return set()
    root = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Protect")
    found = set()
    try:
        for sid in os.listdir(root):
            sub = os.path.join(root, sid)
            if not os.path.isdir(sub):
                continue
            for name in os.listdir(sub):
                if name.lower() != "preferred":
                    found.add(name.lower())
    except OSError:
        return set()
    return found


def inspect_key_file(path, label=""):
    """检查一个密钥文件：格式、包裹者主密钥、能否在本机解开。"""
    path = str(path)
    info = {"file": path, "label": label or os.path.basename(path),
            "exists": os.path.exists(path), "magic": "",
            "master_key": "", "decryptable": False, "error": ""}
    if not info["exists"]:
        info["error"] = "文件不存在"
        return info
    with open(path, "rb") as f:
        blob = f.read()
    if blob.startswith(MAGIC_DPAPI):
        info["magic"] = "DPAPI"
    elif blob.startswith(MAGIC_FERNET):
        info["magic"] = "Fernet"
    else:
        info["magic"] = "未知"
    mk = blob_master_key(blob) or {}
    info["master_key"] = mk.get("master_key", "")
    try:
        decrypt(blob)
        info["decryptable"] = True
    except SecureStoreError as exc:
        info["error"] = str(exc).splitlines()[-1]
    except Exception as exc:                                 # noqa: BLE001
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def probe_keyring():
    """体检 data\\keyring\\ 下的密钥文件，给出可读结论（GUI/CLI 共用）。"""
    files = [inspect_key_file(config.KEYRING_DIR / "master.key.enc", "主密钥"),
             inspect_key_file(config.KEYRING_DIR / "sign.key.enc", "签名私钥")]
    existing = [f for f in files if f["exists"]]
    broken = [f for f in existing if not f["decryptable"]]
    wrap = config.KEYRING_DIR / "wrap.key"
    return {"backend": backend_name(),
            "keyring_dir": str(config.KEYRING_DIR),
            "files": files,
            "wrap_key_ok": _fernet() is not None if wrap.exists() else None,
            "usable": all(f["decryptable"] for f in existing) if existing else None,
            "broken": broken,
            "dpapi_master_keys_here": sorted(windows_master_key_ids()),
            "hint": ("密钥环可用" if existing and not broken else
                     "密钥环不可用：需要重置（修复密钥环.bat 或 cli.py keyring --reset）"
                     if broken else "尚无密钥环，首次使用会自动生成")}


def describe():
    ok, note = dpapi_selftest() if _dpapi_available() else (False, "非 Windows")
    return {"后端": backend_name(),
            "说明": {"dpapi": "Windows DPAPI（绑定当前用户/机器）",
                     "fernet": "cryptography.Fernet（跨平台，密钥文件权限收紧）",
                     "none": "无可用的密钥保护后端：主密钥将拒绝落盘"}[backend_name()],
            "DPAPI 自检": "通过" if ok else "不通过：%s" % note,
            "密钥目录": str(config.KEYRING_DIR)}
