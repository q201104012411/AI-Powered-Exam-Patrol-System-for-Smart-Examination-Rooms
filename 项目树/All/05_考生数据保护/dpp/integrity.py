# -*- coding: utf-8 -*-
r"""
数据完整性保护：哈希（SM3）+ 非对称签名（SM2）

本模块回答一个问题：**"这条已加密的考生数据，有没有被人动过？"**

设计（对应用户要求"任何人都不能动加密的数据，只能删除或上传新的数据"）：

1. **哈希（SM3）**：把一条加密记录的规范内容做 SM3 摘要。
   - 有 gmssl 时用 SM3（国密）；
   - 缺 gmssl 时用 SHA-256，并在 integrity_algorithm() 里**如实标注**，
     绝不把 SHA-256 冒充成 SM3。

2. **非对称签名（SM2）**：用私钥对摘要签名，公钥可验签。
   - 有 gmssl 时用 SM2 签名（含 SM3 杂凑）；
   - 缺 gmssl 时回退 cryptography 的 RSA-2048 + SHA-256/PKCS1v15；
   - 两套都是"私钥签名、公钥验签"，满足非对称签名语义。

3. **只读不可改的落地方式**：
   - 每条加密记录（candidates / consents / vault 图像）写库时，
     同时落一条 (record_ref, digest, signature, alg) 到 signatures 表；
   - 任何"修改"尝试 —— 无论走业务 API 还是直接改库 —— 都会让
     重算的摘要与存储的摘要/signature 对不上，verify_record() 立刻报错；
   - 因此系统层面只有两条合法路径：**追加新记录** 或 **删除整条记录**。
     没有"就地修改加密数据"这条路。

4. **私钥保护**：签名私钥与主密钥同一存放策略（DPAPI / Fernet 包裹），
   不落明文。公钥可以公开导出（verify 只需要公钥）。

关于"为什么签名能防改库"：
  攻击者若直接 UPDATE 数据库里的密文，他改不出对应的 SM2/RSA 签名
  （没有私钥），也改不动已经算好的 SM3 摘要。verify_record() 用公钥验签
  即可发现篡改。这就是"哈希 + 非对称算法"组合的价值。
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from dpp import config
from dpp import secure_store

# ---------------------------------------------------------------- 算法标识
HASH_SM3 = "sm3"
HASH_SHA256 = "sha256"
SIG_SM2 = "sm2"
SIG_RSA = "rsa2048"

SIG_PREFIX_SM2 = "sigsm2:"
SIG_PREFIX_RSA = "sigrsa:"

_CACHE = {}


# ---------------------------------------------------------------- 能力探测
def _gmssl_available():
    try:
        from gmssl import sm2 as _sm2  # noqa: F401
        from gmssl import sm3 as _sm3  # noqa: F401
        return True
    except Exception:
        return False


def _cryptography_available():
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
        return True
    except Exception:
        return False


def hash_algorithm():
    """当前实际使用的哈希算法（国密优先，但绝不谎报）。"""
    return HASH_SM3 if _gmssl_available() else HASH_SHA256


def signature_algorithm():
    """当前实际使用的非对称签名算法（国密优先，但绝不谎报）。"""
    if _gmssl_available():
        return SIG_SM2
    if _cryptography_available():
        return SIG_RSA
    return "none"


def integrity_algorithm_text():
    h = hash_algorithm()
    s = signature_algorithm()
    ht = {HASH_SM3: "SM3", HASH_SHA256: "SHA-256"}[h]
    st = {SIG_SM2: "SM2（SM3 杂凑签名）",
          SIG_RSA: "RSA-2048 + SHA-256（无国密库时的合规回退）",
          "none": "无可用签名后端"}[s]
    return "%s + %s" % (ht, st)


def available():
    return _gmssl_available() or _cryptography_available()


# ---------------------------------------------------------------- 摘要
def digest(data) -> str:
    """
    计算内容摘要（十六进制）。有 gmssl 用 SM3，否则用 SHA-256。
    data 可以是 str / bytes / dict / list。
    """
    raw = _to_bytes(data)
    if _gmssl_available():
        from gmssl.sm3 import sm3_hash
        return sm3_hash(list(raw))
    return hashlib.sha256(raw).hexdigest()


def _to_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def canonical_record(parts) -> str:
    """
    把一条记录规范成"可复现的字符串"，作为签名输入。
    parts 是 list[(key, value)]；按 key 排序，避免字段顺序影响摘要。

    关键：**数值归一化**。同一个数字从 Python 传进来可能是 int（如 30），
    从 SQLite 读回来却是 float（30.0）；若直接 json.dumps 会得到 "30" 与 "30.0"，
    摘要有差别 -> 明明没被人改，验签却失败。这里把整数化的小数统一成 int，
    让"签名时"与"验签时"对同一条记录算出同一个摘要。
    """
    if isinstance(parts, dict):
        items = sorted(parts.items())
    else:
        items = sorted(parts, key=lambda kv: kv[0])
    return json.dumps([[k, _norm(v)] for k, v in items], ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def _norm(v):
    """数值归一化：float 且是整数值 -> int；其余原样。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    return v


# ---------------------------------------------------------------- 密钥
def _sign_key_path():
    return Path(config.KEYRING_DIR) / "sign.key.enc"


def _pub_key_path():
    return Path(config.KEYRING_DIR) / "sign.pub"


def _gen_sm2_keypair():
    """自算 SM2 密钥对：私钥 32 字节随机数 d，公钥 P = d*G。
    自行推导基点乘法**只用 gmssl 的椭圆曲线表**，不自己发明曲线参数。"""
    from gmssl import sm2 as sm2mod
    ecc = sm2mod.default_ecc_table
    n = int(ecc["n"], 16)
    # 私钥 d ∈ [1, n-1]
    d = int.from_bytes(os.urandom(32), "big") % (n - 1) + 1
    priv_hex = "%064x" % d
    pub_hex = _sm2_derive_pub(priv_hex, ecc)
    return priv_hex, pub_hex


def _sm2_derive_pub(priv_hex, ecc):
    """由私钥推导 SM2 公钥（仿射点乘），返回 128 位十六进制（04 去掉）。"""
    p = int(ecc["p"], 16)
    a = int(ecc["a"], 16)
    gx = int(ecc["g"][:64], 16)
    gy = int(ecc["g"][64:], 16)
    d = int(priv_hex, 16)

    def inv(x):
        return pow(x, p - 2, p)

    def add(P1, P2):
        if P1 is None:
            return P2
        if P2 is None:
            return P1
        x1, y1 = P1
        x2, y2 = P2
        if x1 == x2 and (y1 + y2) % p == 0:
            return None
        if P1 == P2:
            lam = (3 * x1 * x1 + a) * inv(2 * y1) % p
        else:
            lam = (y2 - y1) * inv(x2 - x1) % p
        x3 = (lam * lam - x1 - x2) % p
        y3 = (lam * (x1 - x3) - y1) % p
        return (x3, y3)

    def mul(k, P):
        R = None
        while k:
            if k & 1:
                R = add(R, P)
            P = add(P, P)
            k >>= 1
        return R

    x, y = mul(d, (gx, gy))
    return "%064x%064x" % (x, y)


def _gen_rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


def sign_key(create=True):
    """
    取签名私钥。首次调用生成并落盘（平台安全存储包裹，绝不明文）。
    返回 (priv_bytes, pub_bytes)。
    """
    if "sign" in _CACHE:
        return _CACHE["sign"]
    priv_p, pub_p = _sign_key_path(), _pub_key_path()
    if priv_p.exists() and pub_p.exists():
        priv = secure_store.decrypt(priv_p.read_bytes())
        pub = pub_p.read_bytes()
        _CACHE["sign"] = (priv, pub)
        return priv, pub
    if not create:
        return None, None
    if _gmssl_available():
        priv_hex, pub_hex = _gen_sm2_keypair()
        priv = priv_hex.encode("ascii")
        pub = pub_hex.encode("ascii")
    elif _cryptography_available():
        priv, pub = _gen_rsa_keypair()
    else:
        raise RuntimeError("无可用签名后端（需要 gmssl 或 cryptography），拒绝生成签名密钥")
    blob = secure_store.encrypt(priv)          # 无后端时这里会抛错
    priv_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(priv_p) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, str(priv_p))
    with open(str(pub_p), "wb") as f:
        f.write(pub)
    try:
        os.chmod(str(priv_p), 0o600)
    except OSError:
        pass
    _CACHE["sign"] = (priv, pub)
    return priv, pub


def reset_cache():
    _CACHE.clear()


def public_key_text():
    """公钥（可公开，验签只需它）。"""
    _, pub = sign_key(create=False)
    if not pub:
        return ""
    return pub.decode("ascii", "replace")


def key_fingerprint():
    """签名公钥指纹（审计里核对"换没换签名密钥"）。"""
    _, pub = sign_key(create=False)
    if not pub:
        return "-"
    return digest(pub)[:16]


# ---------------------------------------------------------------- 签名
def sign(payload) -> str:
    """对 payload（str/bytes/dict）签名，返回带算法前缀的字符串。"""
    raw = _to_bytes(payload)
    priv, pub = sign_key()
    if _gmssl_available():
        from gmssl import sm2 as sm2mod
        # 注意：gmssl 的 sign_with_sm3 必须同时提供公钥，否则它内部拼的 Z 值不对，
        # 生成的签名用真公钥验不过（这是个很隐蔽的坑，已实测确认）。
        c = sm2mod.CryptSM2(private_key=priv.decode("ascii"),
                            public_key=pub.decode("ascii"))
        sig = c.sign_with_sm3(raw)
        if isinstance(sig, bytes):
            sig = sig.hex()
        # gmssl 会去掉前导零，导致签名十六进制长度可能 <128（奇数长度），
        # 直接 hex 解回会崩。这里统一左补零到 64 字节（128 位十六进制）。
        sig = str(sig)
        if len(sig) < 128:
            sig = sig.rjust(128, "0")
        return SIG_PREFIX_SM2 + sig
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(priv, password=None)
    sig = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return SIG_PREFIX_RSA + base64.b64encode(sig).decode("ascii")


def verify(payload, signature) -> bool:
    """用公钥验签。签名非法或为空一律返回 False（不抛异常，便于批量核对）。"""
    if not signature:
        return False
    raw = _to_bytes(payload)
    sig = str(signature)
    try:
        if sig.startswith(SIG_PREFIX_SM2):
            from gmssl import sm2 as sm2mod
            _, pub = sign_key()
            c = sm2mod.CryptSM2(private_key="", public_key=pub.decode("ascii"))
            # gmssl 的 verify_with_sm3 要的是**十六进制字符串**（不是 bytes），
            # 这里直接传 hex 串；补零逻辑与 sign() 对称。
            hexsig = sig[len(SIG_PREFIX_SM2):].rjust(128, "0")
            return bool(c.verify_with_sm3(hexsig, raw))
        if sig.startswith(SIG_PREFIX_RSA):
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            _, pub = sign_key()
            key = serialization.load_pem_public_key(pub)
            key.verify(base64.b64decode(sig[len(SIG_PREFIX_RSA):]), raw,
                       padding.PKCS1v15(), hashes.SHA256())
            return True
    except Exception:
        return False
    return False


def describe():
    return {
        "哈希算法": {"sm3": "SM3（国密）", "sha256": "SHA-256（无国密库时的回退）"}
                    [hash_algorithm()],
        "签名算法": integrity_algorithm_text().split(" + ", 1)[1],
        "组合": integrity_algorithm_text(),
        "签名密钥指纹": key_fingerprint(),
        "公钥": (public_key_text()[:48] + "…") if public_key_text() else "-",
        "可签名": available(),
    }


def selftest():
    """自检：签名可验、篡改必须被发现。返回 (ok, 说明)。"""
    if not available():
        return False, "无可用签名后端（需要 gmssl 或 cryptography）"
    payload = {"a": 1, "b": "考生", "c": [1, 2, 3]}
    sig = sign(payload)
    if not verify(payload, sig):
        return False, "正常签名验签失败"
    tampered = {"a": 2, "b": "考生", "c": [1, 2, 3]}
    if verify(tampered, sig):
        return False, "篡改后的内容竟然验签通过（签名无效）"
    return True, "签名验签正常，篡改可被检出（算法 %s）" % integrity_algorithm_text()
