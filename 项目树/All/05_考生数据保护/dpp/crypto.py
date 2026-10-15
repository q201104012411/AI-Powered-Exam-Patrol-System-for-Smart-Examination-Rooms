# -*- coding: utf-8 -*-
r"""
敏感数据加解密（国密优先）

设计原则：
1. **不引入新的 pip 依赖**：优先用 gmssl（若已安装）实现 SM4-CBC + SM3；
   没有 gmssl 时回退 cryptography 的 AES-256-GCM（AEAD，自带完整性校验）。
   两条路径都是"认证加密"，密文带版本前缀，升级算法不会读不了老数据。
2. **主密钥不落明文**：32 字节主密钥先生成，再用本目录下 keyring\\ 的
   DPAPI（Windows）/ 文件权限（其它平台）包裹后存盘；数据库被整库拷走也解不开。
3. **字段级加密**：encrypt_field 给业务列用；密文统一带 "enc:" 前缀，
   兼容老明文（decrypt 遇非 enc: 前缀原样返回），便于灰度上线。
4. **算法可核查**：active_algorithm() 如实报出当前实际用的算法，
   报告里标"国密优先"时不会把 AES 冒充成 SM4。
"""
import base64
import hashlib
import hmac
import os
from pathlib import Path

from dpp import config
from dpp import secure_store
from dpp import integrity

_PREFIX = "enc:"

# 算法标识（写在密文里，便于轮换与追溯）
ALG_SM4 = "sm4cbc"
ALG_AES = "aesgcm"

_CACHE = {}


# ---------------------------------------------------------------- 国密能力探测
def _gmssl_available():
    try:
        import gmssl.sm4  # noqa: F401
        return True
    except Exception:
        return False


def _cryptography_available():
    try:
        import cryptography.fernet  # noqa: F401
        return True
    except Exception:
        return False


def active_algorithm():
    """当前实际使用的算法（国密优先，但绝不谎报）。"""
    if _gmssl_available():
        return ALG_SM4
    if _cryptography_available():
        return ALG_AES
    return "none"


def algorithm_text():
    a = active_algorithm()
    return {ALG_SM4: "SM4-CBC + SM3（国密）",
            ALG_AES: "AES-256-GCM（cryptography，国密库缺失时的合规回退）",
            "none": "无可用加密后端（拒绝落盘）"}[a]


# ---------------------------------------------------------------- 主密钥
def _master_key_path():
    return Path(config.KEYRING_DIR) / "master.key.enc"


def _wrap_key(raw: bytes) -> bytes:
    """用平台安全存储包裹主密钥（Windows 走 DPAPI；其它平台走 Fernet/明文兜底）。"""
    return secure_store.encrypt(raw)


def _unwrap_key(blob: bytes) -> bytes:
    return secure_store.decrypt(blob)


def master_key(create=True):
    """
    取 32 字节主密钥。首次调用生成并落盘（DPAPI 包裹）。
    取不到安全后端时**拒绝生成明文密钥**，直接抛错——
    宁可启动失败，也不静默把主密钥明文写到磁盘上。
    """
    if "master" in _CACHE:
        return _CACHE["master"]
    p = _master_key_path()
    if p.exists():
        raw = _unwrap_key(p.read_bytes())
        _CACHE["master"] = raw
        return raw
    if not create:
        return None
    raw = os.urandom(32)
    blob = _wrap_key(raw)                       # 无后端时这里会抛 SecureStoreUnavailable
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, str(p))
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    _CACHE["master"] = raw
    return raw


def reset_cache():
    """测试用：清掉进程内密钥与算法缓存（含完整性签名的密钥缓存）。

    必须连 integrity 的缓存一起清：否则换了 KEYRING_DIR 后，签名模块还拿着
    上一个数据目录里的旧签名密钥，导致"密钥串目录"。
    """
    _CACHE.clear()
    try:
        integrity.reset_cache()
    except Exception:                                        # noqa: BLE001
        pass


def key_fingerprint():
    """密钥指纹（只报前 16 位 SHA256，用于审计里核对"换没换密钥"）"""
    k = master_key(create=False)
    if not k:
        return "-"
    return hashlib.sha256(k).hexdigest()[:16]


def rotate_key(new_key=None):
    """
    密钥轮换：写入新主密钥并返回旧密钥（调用方负责把老数据解密后重加密）。
    本函数不主动迁移数据——迁移必须显式调用 migrate()，避免"以为轮换了其实没换"。
    """
    old = master_key(create=False)
    raw = new_key or os.urandom(32)
    if len(raw) != 32:
        raise ValueError("主密钥必须是 32 字节")
    p = _master_key_path()
    tmp = str(p) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_wrap_key(raw))
    os.replace(tmp, str(p))
    _CACHE["master"] = raw
    return old, raw


# ---------------------------------------------------------------- 具体算法
def _derive(master: bytes, salt: bytes, nbytes: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", master, salt, 10000, dklen=nbytes)


def _sm4_encrypt(master: bytes, data: bytes) -> str:
    """SM4-CBC 加密，密文格式：<iv_hex>$<cipher_hex>$<sm3_tag_hex>"""
    from gmssl.sm4 import CryptSM4, SM4_ENCRYPT
    from gmssl.sm3 import sm3_hash
    iv = os.urandom(16)
    salt = os.urandom(16)
    key = _derive(master, salt, 16)
    c = CryptSM4()
    c.set_key(key, SM4_ENCRYPT)
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad                    # PKCS#7
    out = c.crypt_cbc(iv, padded)
    tag = bytes.fromhex(sm3_hash(list(key + iv + out)))    # SM3 完整性标签
    return "|".join([ALG_SM4, salt.hex(), iv.hex(), out.hex(), tag.hex()])


def _sm4_decrypt(master: bytes, blob: str) -> bytes:
    from gmssl.sm4 import CryptSM4, SM4_DECRYPT
    from gmssl.sm3 import sm3_hash
    _, salt_h, iv_h, ct_h, tag_h = blob.split("|")
    salt, iv, ct = bytes.fromhex(salt_h), bytes.fromhex(iv_h), bytes.fromhex(ct_h)
    key = _derive(master, salt, 16)
    expect = bytes.fromhex(sm3_hash(list(key + iv + ct)))
    if not hmac.compare_digest(expect, bytes.fromhex(tag_h)):
        raise ValueError("SM3 完整性校验失败（密文被篡改或密钥不对）")
    c = CryptSM4()
    c.set_key(key, SM4_DECRYPT)
    padded = c.crypt_cbc(iv, ct)
    pad = padded[-1]
    if not 1 <= pad <= 16:
        raise ValueError("SM4 填充非法")
    return padded[:-pad]


def _aes_encrypt(master: bytes, data: bytes) -> str:
    """AES-256-GCM（AEAD），格式：aesgcm|<salt|nonce|ct|tag 的 hex>"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive(master, salt, 32)
    ct = AESGCM(key).encrypt(nonce, data, b"dpp-v1")     # AAD 绑定用途
    return "%s|%s|%s" % (ALG_AES, salt.hex(),
                         (nonce + ct).hex())


def _aes_decrypt(master: bytes, blob: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _, salt_h, body_h = blob.split("|")
    salt = bytes.fromhex(salt_h)
    body = bytes.fromhex(body_h)
    nonce, ct = body[:12], body[12:]
    key = _derive(master, salt, 32)
    return AESGCM(key).decrypt(nonce, ct, b"dpp-v1")


# ---------------------------------------------------------------- 对外接口
def encrypt_bytes(data: bytes) -> str:
    """加密二进制；返回带 enc: 前缀的字符串。空值原样返回。"""
    if data is None or len(data) == 0:
        return data
    master = master_key()
    alg = active_algorithm()
    if alg == ALG_SM4:
        body = _sm4_encrypt(master, data)
    elif alg == ALG_AES:
        body = _aes_encrypt(master, data)
    else:
        raise RuntimeError("无可用加密后端，拒绝落盘（可安装 gmssl 或 cryptography）")
    return _PREFIX + body


def decrypt_bytes(cipher_text):
    """解密二进制；返回 bytes。空值/明文原样返回。"""
    if cipher_text is None or cipher_text == "":
        return cipher_text
    s = cipher_text if isinstance(cipher_text, str) else cipher_text.decode("utf-8")
    if not s.startswith(_PREFIX):
        return s.encode("utf-8") if isinstance(s, str) else s
    body = s[len(_PREFIX):]
    master = master_key(create=False)
    if not master:
        raise RuntimeError("本机取不到主密钥，无法解密（换机器/换用户属预期）")
    alg = body.split("|", 1)[0]
    if alg == ALG_SM4:
        return _sm4_decrypt(master, body)
    if alg == ALG_AES:
        return _aes_decrypt(master, body)
    raise ValueError("未知的密文算法标识：%s" % alg)


def encrypt_field(plain_text):
    """加密一个文本字段；空值/已加密值原样返回（幂等）。"""
    if plain_text is None or plain_text == "":
        return plain_text
    s = str(plain_text)
    if s.startswith(_PREFIX):
        return s
    return encrypt_bytes(s.encode("utf-8"))


def decrypt_field(cipher_text):
    """解密一个文本字段；空值/明文原样返回。"""
    if cipher_text is None or cipher_text == "":
        return cipher_text
    s = str(cipher_text)
    if not s.startswith(_PREFIX):
        return s
    return decrypt_bytes(s).decode("utf-8")


def is_encrypted(value):
    """判断一个值是否已加密（验收"加密率 100%"时逐列核对用）。"""
    if value is None:
        return False
    s = value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    return s.startswith(_PREFIX)


# ---------------------------------------------------------------- 不可逆与摘要
def irreversible_template(feature_vector):
    """
    人脸特征模板：落**不可逆**摘要（HMAC-SHA256），不是可还原的加密。
    用主密钥做 HMAC 的原因：特征向量本身也是生物特征，明文落库同样违规；
    比对时用同一函数重算比对，等价于"不可逆模板"。
    """
    if feature_vector is None:
        return ""
    raw = feature_vector if isinstance(feature_vector, bytes) \
        else str(feature_vector).encode("utf-8")
    key = master_key()
    return "tpl1:" + hmac.new(key, raw, hashlib.sha256).hexdigest()


def digest(data):
    """带密钥的通用摘要（用于去标识化引用号，防跨表碰撞关联）。"""
    if data is None:
        return ""
    raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
    key = master_key()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def sha256_hex(data):
    """无密钥摘要：用于审计哈希链（链本身不加密）。"""
    raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def secure_bytes(n=16):
    return os.urandom(n)


# ---------------------------------------------------------------- 完整性（哈希 + 非对称）
# 说明：加密防"看不懂"，签名防"被改过"。二者组合才满足
#      "任何加密数据不可修改、只能删除或追加"。
def integrity_text():
    """当前实际使用的完整性算法组合（SM3+SM2 或诚实回退）。"""
    return integrity.integrity_algorithm_text()


def integrity_available():
    return integrity.available()


def integrity_describe():
    return integrity.describe()


def integrity_selftest():
    return integrity.selftest()


def integrity_reset_cache():
    integrity.reset_cache()


def record_digest(canonical_text):
    """一条加密记录的哈希（SM3 优先）。canonical_text 由 integrity.canonical_record 生成。"""
    return integrity.digest(canonical_text)


def record_sign(canonical_text):
    """对一条加密记录的摘要做非对称签名。"""
    return integrity.sign(canonical_text)


def record_verify(canonical_text, signature):
    """用公钥验签：True=未被篡改，False=内容与签名不符（被改过）。"""
    return integrity.verify(canonical_text, signature)


def canonical_record(parts):
    return integrity.canonical_record(parts)
