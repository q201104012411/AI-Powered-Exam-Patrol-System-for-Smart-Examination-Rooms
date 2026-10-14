# -*- coding: utf-8 -*-
r"""
多模态大模型（VLM）检测引擎 —— YOLO11n 的"看图判读"升级件

定位
====
`person_detect.PersonTracker` 负责【跟踪 + 画框 + 行为标签 + 越界判定】，
本模块只替换它的【检测源】：把画面送给**能看图的大模型**，拿回
人框 / 行为 / 可疑物品（手机、小抄、耳机…），再交回原有链路。
好处：换模型后行为判据是"模型看懂画面"，而不是原来的框几何猜测。

与 YOLO11n 的关系（本版本保留 YOLO11n 作为离线兜底）
====================================================
    vlm_provider = api / ollama ：多模态为主引擎。后台线程按间隔刷新，
                                 结果新鲜时用它的框；没结果/超时/报错时
                                 本帧回落 YOLO11n —— 界面永远不会"瞎掉"。
    vlm_provider = off（默认）  ：完全等价于原版（纯 YOLO11n），不影响任何现有功能。

两种后端，随时可切换（设置里改 vlm_provider 即可）
==================================================
    ollama —— 本机 Ollama：POST {base}/api/chat，图片走 messages[].images 的 base64；
              可用性用 GET /api/tags + POST /api/show（看 capabilities 里有没有 vision）。
    api    —— OpenAI 兼容的云端多模态接口：POST {base}/chat/completions，
              messages 里用 image_url: data:image/jpeg;base64,...，Bearer 鉴权。

为什么要后台线程 + 间隔
========================
多模态一次推理通常 1~10 秒（本机 CPU 更慢），逐帧同步调用会把界面卡死。所以：
    · 每 vlm_interval 秒最多发起一次请求；
    · 请求在守护线程里跑，主线程永远只取"最近一次成功结果"，绝不阻塞；
    · 结果超过 vlm_interval * 3 秒未刷新即判为过期，过期就回落 YOLO。

输出契约（提示词强制 JSON，`format: json` 双保险）
==================================================
    {"persons": [{"box": [x1, y1, x2, y2], "action": "起立/站立", "confidence": 0.9}],
     "objects": [{"box": [x1, y1, x2, y2], "label": "手机",     "confidence": 0.8}],
     "summary": "一句话描述"}

    box 统一用 **0~1000 的相对坐标**（大模型对归一化坐标更稳），本模块负责
    换算成像素、裁剪到画面内；同时兼容 0~1 小数、绝对像素、以及
    {"x","y","w","h"} 形式的返回。

本模块只用标准库（urllib/json/base64/threading），不引入新的第三方依赖。
"""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

# 行为/颜色常量与 person_detect 保持同一套（避免两处词表漂移）
try:
    import person_detect as pd_mod
except Exception:            # pragma: no cover - 单测时允许注入
    pd_mod = None

# ---------------- 后端与默认值 ----------------
PROVIDER_OFF = "off"
PROVIDER_API = "api"
PROVIDER_OLLAMA = "ollama"
PROVIDERS = (PROVIDER_OFF, PROVIDER_API, PROVIDER_OLLAMA)

PROVIDER_LABELS = {
    PROVIDER_OFF: "关闭（只用 YOLO11n）",
    PROVIDER_API: "OpenAI 兼容 API",
    PROVIDER_OLLAMA: "本机 Ollama",
}

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_API_URL = ""                     # 例：https://dashscope.aliyuncs.com/compatible-mode/v1
DEFAULT_INTERVAL_S = 3.0                 # 每路每 N 秒最多一次多模态调用
DEFAULT_MAX_DIM = 896                    # 送模型的图片长边（越小越快、越省 token）
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_JPEG_Q = 80
STALE_FACTOR = 3.0                       # 结果超过 interval*STALE_FACTOR 秒判过期 → 回落 YOLO
PROBE_TTL_S = 10.0                       # 可用性探测结果缓存

# 设置键（GUI/设置文件/环境变量三处共用一份，避免漏改）
SETTING_KEYS = ("vlm_provider", "vlm_model", "vlm_base_url", "vlm_api_key",
                "vlm_interval", "vlm_max_dim", "vlm_timeout")

ENV_KEYS = {
    "vlm_provider": "EXAM_VLM_PROVIDER",
    "vlm_model": "EXAM_VLM_MODEL",
    "vlm_base_url": "EXAM_VLM_BASE_URL",
    "vlm_api_key": "EXAM_VLM_API_KEY",
    "vlm_interval": "EXAM_VLM_INTERVAL",
    "vlm_max_dim": "EXAM_VLM_MAX_DIM",
    "vlm_timeout": "EXAM_VLM_TIMEOUT",
}

# 可疑物品词表（模型自由发挥时也尽量归一到这几个标签）
OBJECT_LABELS = {
    "手机": "手机", "cell phone": "手机", "phone": "手机", "smartphone": "手机",
    "小抄": "小抄", "cheat sheet": "小抄", "纸条": "小抄", "note": "小抄",
    "耳机": "耳机", "earphone": "耳机", "earbuds": "耳机", "headphone": "耳机",
    "智能手表": "智能手表", "watch": "智能手表", "smartwatch": "智能手表",
    "平板": "平板", "tablet": "平板", "ipad": "平板",
    "书本": "书本", "book": "书本", "资料": "书本",
}

# 大模型给的行为词 → 本项目的行为常量（person_detect.ACT_*）
# 模型输出可能五花八门，这里做归一；匹配不到的保留原文（不丢信息）。
# 注意顺序：**具体说法在前**，泛化词在后（否则 "turn head" 会先被 "turn" 吃掉）。
ACTION_ALIASES = (
    (("正常", "正常姿态", "坐着", "端坐", "听课", "normal"), "正常姿态"),
    (("起立", "站立", "站起", "站起来", "stand"), "起立/站立"),
    (("趴桌", "前倾", "低头趴", "趴下", "前趴", "lean"), "前倾/趴桌"),
    (("转脸偏头", "偏头", "歪头", "turn head", "head turn"), "转脸偏头"),
    (("使用手机", "玩手机", "看手机", "用手机", "phone"), "使用手机"),
    (("东张西望", "左右张望", "张望", "环顾", "look around"), "东张西望"),
    (("长时间低头", "低头", "看书", "写字", "look down"), "长时间低头"),
    (("眼神", "游移", "飘忽", "gaze"), "眼神游移"),
    (("闭眼", "睡觉", "趴睡", "入睡", "sleep"), "闭眼/趴睡"),
    (("偏转", "转头", "侧身", "侧转", "回头", "turn"), "大幅度偏转"),
    (("坐下", "坐姿", "sit"), "坐下"),
    (("靠左",), "靠左"),
    (("靠右",), "靠右"),
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


# =====================================================================
#  配置
# =====================================================================
def _is_local_url(url):
    """是不是本机地址（本机 OpenAI 兼容服务通常没有 API Key，不必逼用户编一个）"""
    text = (url or "").strip().lower()
    if not text:
        return False
    for host in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1"):
        if host in text:
            return True
    return False


class VlmConfig(object):
    """多模态引擎配置（设置文件 / 环境变量 → 统一解析）。

    优先级：显式传入的参数 > 环境变量 > 设置文件 > 内置默认值。
    环境变量优先是为了"临时切一下试试"不用改 JSON。
    """

    def __init__(self, provider=PROVIDER_OFF, model="", base_url="", api_key="",
                 interval=DEFAULT_INTERVAL_S, max_dim=DEFAULT_MAX_DIM,
                 timeout=DEFAULT_TIMEOUT_S, jpeg_quality=DEFAULT_JPEG_Q,
                 use_env=True):
        self.provider = _norm_provider(provider)
        self.model = (model or "").strip()
        self.base_url = (base_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.interval = _as_float(interval, DEFAULT_INTERVAL_S, 0.5, 600.0)
        self.max_dim = int(_as_float(max_dim, DEFAULT_MAX_DIM, 224, 4096))
        self.timeout = _as_float(timeout, DEFAULT_TIMEOUT_S, 3.0, 600.0)
        self.jpeg_quality = int(_as_float(jpeg_quality, DEFAULT_JPEG_Q, 30, 95))
        if use_env:
            self._apply_env()
        if not self.base_url:
            self.base_url = DEFAULT_OLLAMA_URL if self.provider == PROVIDER_OLLAMA \
                else DEFAULT_API_URL

    def _apply_env(self):
        env = os.environ
        if env.get("EXAM_VLM_PROVIDER", "").strip():
            self.provider = _norm_provider(env["EXAM_VLM_PROVIDER"])
        if env.get("EXAM_VLM_MODEL", "").strip():
            self.model = env["EXAM_VLM_MODEL"].strip()
        if env.get("EXAM_VLM_BASE_URL", "").strip():
            self.base_url = env["EXAM_VLM_BASE_URL"].strip()
        if env.get("EXAM_VLM_API_KEY", "").strip():
            self.api_key = env["EXAM_VLM_API_KEY"].strip()
        if env.get("EXAM_VLM_INTERVAL", "").strip():
            self.interval = _as_float(env["EXAM_VLM_INTERVAL"], self.interval, 0.5, 600.0)
        if env.get("EXAM_VLM_MAX_DIM", "").strip():
            self.max_dim = int(_as_float(env["EXAM_VLM_MAX_DIM"], self.max_dim, 224, 4096))
        if env.get("EXAM_VLM_TIMEOUT", "").strip():
            self.timeout = _as_float(env["EXAM_VLM_TIMEOUT"], self.timeout, 3.0, 600.0)

    # ---- 状态 ----
    @property
    def enabled(self):
        """是否真的能用：关了 / 没填模型 / 缺 key（非本机）/ 没填地址 → 都算不可用"""
        if self.provider not in (PROVIDER_API, PROVIDER_OLLAMA):
            return False
        if not self.model:
            return False
        if not self.base_url:
            return False
        if (self.provider == PROVIDER_API and not self.api_key
                and not _is_local_url(self.base_url)):
            return False
        return True

    def missing_reason(self):
        """不能用的原因（给界面如实显示用）"""
        if self.provider == PROVIDER_OFF:
            return "多模态已关闭（vlm_provider=off）"
        if not self.model:
            return "未填模型名（vlm_model）"
        if not self.base_url:
            return "未填接口地址（vlm_base_url）"
        if (self.provider == PROVIDER_API and not self.api_key
                and not _is_local_url(self.base_url)):
            return "未填 API Key（vlm_api_key；本机地址可留空）"
        return ""

    def describe(self):
        if not self.enabled:
            return "多模态未启用：%s" % (self.missing_reason() or "配置不完整")
        return "%s / %s" % (PROVIDER_LABELS.get(self.provider, self.provider), self.model)

    def to_dict(self):
        return {"provider": self.provider, "model": self.model,
                "base_url": self.base_url, "api_key": self.api_key,
                "interval": self.interval, "max_dim": self.max_dim,
                "timeout": self.timeout}

    @classmethod
    def from_settings(cls, settings, use_env=True):
        """从 AI 设置 dict（data/ai_settings.json）构造"""
        s = settings or {}
        return cls(provider=s.get("vlm_provider", PROVIDER_OFF),
                   model=s.get("vlm_model", ""),
                   base_url=s.get("vlm_base_url", ""),
                   api_key=s.get("vlm_api_key", ""),
                   interval=s.get("vlm_interval", DEFAULT_INTERVAL_S),
                   max_dim=s.get("vlm_max_dim", DEFAULT_MAX_DIM),
                   timeout=s.get("vlm_timeout", DEFAULT_TIMEOUT_S),
                   use_env=use_env)


def _norm_provider(value):
    v = str(value or "").strip().lower()
    if v in ("api", "openai", "openai-compatible", "http", "兼容api", "云端api"):
        return PROVIDER_API
    if v in ("ollama", "local", "本机", "本机ollama"):
        return PROVIDER_OLLAMA
    if v in ("", "off", "none", "0", "false", "关闭"):
        return PROVIDER_OFF
    return PROVIDER_OFF


def _as_float(value, default, lo, hi):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:                       # NaN
        return default
    return max(lo, min(hi, f))


# =====================================================================
#  模型返回解析
# =====================================================================
def extract_json(text):
    """从模型回复里抠出 JSON（容忍 ```json 围栏、前后废话、单对象/数组）。

    返回 (dict, 错误说明)。解析不出来时 dict 为 None —— 调用方按"本次失败"
    处理并回落 YOLO，绝不猜。
    """
    if not text:
        return None, "模型返回为空"
    raw = str(text).strip()
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    for candidate in (raw, _slice_braces(raw), _slice_brackets(raw)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, list):
            data = {"persons": data}
        if isinstance(data, dict):
            return data, ""
    return None, "模型返回不是可解析的 JSON：%s" % raw[:120].replace("\n", " ")


def _slice_braces(text):
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1] if 0 <= i < j else ""


def _slice_brackets(text):
    i, j = text.find("["), text.rfind("]")
    return text[i:j + 1] if 0 <= i < j else ""


def _norm_action(label):
    """把模型给的行为词归一到本项目的行为常量"""
    text = str(label or "").strip()
    if not text:
        return "正常姿态"
    low = text.lower()
    for keys, target in ACTION_ALIASES:
        for k in keys:
            if k in text or k in low:
                return target
    return text


def _norm_object(label):
    text = str(label or "").strip()
    low = text.lower()
    for k, v in OBJECT_LABELS.items():
        if k in text or k in low:
            return v
    return text or "可疑物品"


def _coord_pair(box, fw, fh):
    """把模型给的 box 归一成像素 (x1, y1, x2, y2)；失败返回 None。

    坐标契约（提示词里写死）：**0~1000 相对坐标**为主。识别顺序：
      · 全部 ≤ 1.5        → 当作 0~1 小数
      · 全部 ≤ 1000 且 ≥0 → 当作 0~1000 相对（默认契约）
      · 其余（有值 >1000）→ 当作绝对像素
    另兼容 {"x","y","w","h"} 写法。歧义（比如绝对像素正好都 ≤1000）时按契约优先
    相对坐标 —— 用 工具_多模态检测.py --image 看一眼画出来的框就能确认模型是否守约。
    """
    abs_pixels = False
    if isinstance(box, dict):
        x, y = box.get("x"), box.get("y")
        w, h = box.get("w", box.get("width")), box.get("h", box.get("height"))
        if None in (x, y, w, h):
            return None
        try:
            vals = [float(x), float(y), float(x) + float(w), float(y) + float(h)]
        except (TypeError, ValueError):
            return None
        # {x,y,w,h} 这种写法基本可以确定是像素坐标（相对坐标一般不会写成 w/h）
        abs_pixels = True
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        try:
            vals = [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
        except (TypeError, ValueError):
            return None
    else:
        return None
    x1, y1, x2, y2 = vals
    if x2 <= x1 or y2 <= y1:          # 退化框
        return None
    if max(vals) <= 1.5:              # 0~1 小数
        px = [x1 * fw, y1 * fh, x2 * fw, y2 * fh]
    elif abs_pixels or max(vals) > 1000.0:
        px = [x1, y1, x2, y2]         # 绝对像素（>1000 不可能是相对坐标）
    else:                             # 0~1000 相对（提示词里的默认契约）
        px = [x1 / 1000.0 * fw, y1 / 1000.0 * fh,
              x2 / 1000.0 * fw, y2 / 1000.0 * fh]
    x1 = int(max(0, min(fw - 1, round(px[0]))))
    y1 = int(max(0, min(fh - 1, round(px[1]))))
    x2 = int(max(0, min(fw, round(px[2]))))
    y2 = int(max(0, min(fh, round(px[3]))))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def parse_result(payload, fw, fh, min_conf=0.0):
    """把模型 JSON 变成 (persons, objects)。

    persons: [{"rect":(x,y,w,h), "action":行为, "confidence":float, "raw_action":原文}]
    objects: [{"rect":(x,y,w,h), "label":标签, "confidence":float}]
    坐标已换算成像素并裁到画面内；框不合法的条目直接丢弃（不猜、不凑数）。
    """
    persons, objects = [], []
    if not isinstance(payload, dict):
        return persons, objects
    for item in _as_list(payload.get("persons") or payload.get("people")
                         or payload.get("person")):
        if not isinstance(item, dict):
            continue
        rect = _coord_pair(item.get("box", item.get("bbox", item.get("rect"))), fw, fh)
        if rect is None:
            continue
        conf = _as_float(item.get("confidence", item.get("score", 1.0)), 1.0, 0.0, 1.0)
        if conf < min_conf:
            continue
        raw_action = str(item.get("action", item.get("behavior", "")) or "")
        persons.append({"rect": rect, "action": _norm_action(raw_action),
                        "raw_action": raw_action, "confidence": conf,
                        "phone": bool(item.get("phone") or item.get("has_phone")),
                        "desc": str(item.get("desc", item.get("description", "")) or "")})
    for item in _as_list(payload.get("objects") or payload.get("items")
                         or payload.get("suspicious")):
        if not isinstance(item, dict):
            continue
        rect = _coord_pair(item.get("box", item.get("bbox", item.get("rect"))), fw, fh)
        if rect is None:
            continue
        conf = _as_float(item.get("confidence", item.get("score", 1.0)), 1.0, 0.0, 1.0)
        if conf < min_conf:
            continue
        label = _norm_object(item.get("label", item.get("name", item.get("class", ""))))
        objects.append({"rect": rect, "label": label, "confidence": conf,
                        "phone": label == "手机"})
    return persons, objects


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


# =====================================================================
#  提示词
# =====================================================================
SYSTEM_PROMPT = (
    "你是考场视频监看的多模态判读器。你只做一件事：看图，输出 JSON。\n"
    "严格规则：\n"
    "1) 只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块以外的文字；\n"
    "2) 坐标用 0~1000 的相对坐标（左上角 0,0，右下角 1000,1000）；\n"
    "3) 没看到就返回空数组，**不要编造**人和物品；\n"
    "4) 每个人给一个 action，取值只能用这些词之一：\n"
    "   正常姿态 / 坐下 / 起立/站立 / 大幅度偏转 / 前倾/趴桌 / 东张西望 /\n"
    "   长时间低头 / 靠左 / 靠右 / 眼神游移 / 闭眼/趴睡 / 转脸偏头 / 使用手机；\n"
    "5) objects 里只放**考试违规或可疑物品**：手机 / 小抄 / 耳机 / 智能手表 /\n"
    "   平板 / 书本；普通文具（笔、橡皮、水杯、准考证）不要放进来。\n"
)

USER_PROMPT = (
    "请判读这张考场画面，输出 JSON：\n"
    '{"persons":[{"box":[x1,y1,x2,y2],"action":"正常姿态","confidence":0.0}],'
    '"objects":[{"box":[x1,y1,x2,y2],"label":"手机","confidence":0.0}],'
    '"summary":"一句话说明画面里在发生什么"}\n'
    "要点：① 逐人给框，坐着的人也要框出上半身；② 只要有人手上/桌上有手机、\n"
    "小抄、耳机等，就放进 objects 并在该人的 action 里体现；③ 拿不准的写低 confidence。"
)


# =====================================================================
#  后端客户端
# =====================================================================
class VlmError(Exception):
    """多模态调用失败（消息面向使用者，可直接显示在界面上）"""


class _BaseClient(object):
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    # ---- 子类实现 ----
    def probe(self):
        """探测可用性 → (ok: bool, 说明: str)"""
        raise NotImplementedError

    def analyze(self, frame_bgr):
        """送一帧 → 模型返回的原始文本"""
        raise NotImplementedError

    # ---- 公共 ----
    def encode_frame(self, frame_bgr):
        """缩放 + JPEG + base64（控制体积，避免大图把请求撑爆/拖慢）"""
        img = frame_bgr
        h, w = img.shape[:2]
        long_edge = max(h, w)
        if long_edge > self.cfg.max_dim > 0:
            s = self.cfg.max_dim / float(long_edge)
            img = cv2.resize(img, (max(2, int(w * s)), max(2, int(h * s))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality])
        if not ok:
            raise VlmError("画面编码为 JPEG 失败")
        return img.shape[:2], base64.b64encode(buf.tobytes()).decode("ascii")

    def analyze_parsed(self, frame_bgr):
        """送一帧 → (persons, objects, 原始文本, 耗时秒)"""
        t0 = time.time()
        text = self.analyze(frame_bgr)
        dt = time.time() - t0
        payload, err = extract_json(text)
        if payload is None:
            raise VlmError(err)
        fh, fw = frame_bgr.shape[:2]
        persons, objects = parse_result(payload, fw, fh)
        return persons, objects, text, dt


class OllamaClient(_BaseClient):
    """本机（或局域网）Ollama：/api/chat + images(base64)"""

    name = "ollama"

    def _url(self, path):
        return self.cfg.base_url.rstrip("/") + path

    def _post(self, path, body, timeout):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _get(self, path, timeout):
        req = urllib.request.Request(self._url(path), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def probe(self):
        try:
            tags = self._get("/api/tags", timeout=min(5.0, self.cfg.timeout))
        except urllib.error.URLError as e:
            return False, "连不上 Ollama（%s）：%s" % (self.cfg.base_url, e)
        except Exception as e:
            return False, "Ollama 探测失败：%s" % e
        names = [m.get("name", "") for m in (tags.get("models") or [])]
        if not names:
            return False, "Ollama 已连上，但本机没有任何模型（先 ollama pull %s）" \
                % (self.cfg.model or "<模型名>")
        hit = _match_model(names, self.cfg.model)
        if not hit:
            return False, "Ollama 已连上，但没有模型 %s（本机有：%s）" \
                % (self.cfg.model, "、".join(names[:6]))
        vision, why = self._vision_capability(hit)
        if vision is False:
            return False, "模型 %s 不能看图：%s" % (hit, why)
        return True, "Ollama 就绪：%s%s" % (hit, ("（%s）" % why) if why else "")

    def _vision_capability(self, model):
        """查 /api/show 的 capabilities 判断是不是视觉模型。

        老版本 Ollama 不返回 capabilities —— 这时按 None 处理（不武断判死），
        真送图时若模型不支持，会在 analyze 阶段如实报错。
        """
        try:
            info = self._post("/api/show", {"model": model}, timeout=min(8.0, self.cfg.timeout))
        except Exception as e:
            return None, "无法确认（%s）" % e
        caps = info.get("capabilities")
        if isinstance(caps, list):
            if "vision" in [str(c).lower() for c in caps]:
                return True, "capabilities: vision"
            return False, "capabilities 里没有 vision（只有 %s）" % "、".join(map(str, caps))
        proj = info.get("projector_info") or info.get("mmproj") or ""
        if proj:
            return True, "带多模态投影文件"
        return None, ""

    def analyze(self, frame_bgr):
        (_, _), b64 = self.encode_frame(frame_bgr)
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": USER_PROMPT, "images": [b64]}],
            "stream": False,
            "format": "json",                 # 让 Ollama 直接约束成 JSON
            "options": {"temperature": 0},
        }
        try:
            out = self._post("/api/chat", body, timeout=self.cfg.timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise VlmError("Ollama 返回 %s：%s" % (e.code, detail or e.reason))
        except urllib.error.URLError as e:
            raise VlmError("连不上 Ollama（%s）：%s" % (self.cfg.base_url, e))
        except Exception as e:
            raise VlmError("Ollama 调用异常：%s" % e)
        msg = (out or {}).get("message") or {}
        text = msg.get("content") or (out or {}).get("response") or ""
        if not text:
            raise VlmError("Ollama 没返回内容：%s" % json.dumps(out, ensure_ascii=False)[:160])
        return text


class OpenAIClient(_BaseClient):
    """OpenAI 兼容的多模态接口（通义千问 VL、GLM-4V、自建 vLLM 等）"""

    name = "api"

    def _url(self, path):
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base + path
        return base + "/v1" + path

    def _headers(self):
        head = {"Content-Type": "application/json"}
        if self.cfg.api_key:                 # 本机服务可能不需要鉴权，没填就不发这个头
            head["Authorization"] = "Bearer %s" % self.cfg.api_key
        return head

    def probe(self):
        req = urllib.request.Request(self._url("/models"), headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=min(8.0, self.cfg.timeout)) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return False, "接口返回 %s（检查 base_url / API Key）" % e.code
        except urllib.error.URLError as e:
            return False, "连不上接口（%s）：%s" % (self.cfg.base_url, e)
        except Exception as e:
            return False, "接口探测失败：%s" % e
        names = [m.get("id", "") for m in (data.get("data") or [])]
        if names and self.cfg.model and not _match_model(names, self.cfg.model):
            return False, "接口可用，但模型列表里没有 %s（有：%s）" \
                % (self.cfg.model, "、".join(names[:8]))
        return True, "接口就绪：%s%s" % (self.cfg.model,
                                       ("（模型列表 %d 个）" % len(names)) if names else "")

    def analyze(self, frame_bgr):
        (_, _), b64 = self.encode_frame(frame_bgr)
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": [
                             {"type": "text", "text": USER_PROMPT},
                             {"type": "image_url",
                              "image_url": {"url": "data:image/jpeg;base64,%s" % b64}}]}],
            "temperature": 0,
            "max_tokens": 1200,
        }
        req = urllib.request.Request(self._url("/chat/completions"),
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                out = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise VlmError("接口返回 %s：%s" % (e.code, detail or e.reason))
        except urllib.error.URLError as e:
            raise VlmError("连不上接口（%s）：%s" % (self.cfg.base_url, e))
        except Exception as e:
            raise VlmError("接口调用异常：%s" % e)
        choices = (out or {}).get("choices") or []
        if not choices:
            raise VlmError("接口没返回 choices：%s"
                           % json.dumps(out, ensure_ascii=False)[:160])
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):        # 少数接口返回分段内容
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        if not content:
            raise VlmError("接口返回内容为空")
        return content


def _match_model(names, want):
    """模型名匹配：容忍 ollama 的 :latest / 大小写 / 命名空间前缀"""
    want = (want or "").strip()
    if not want:
        return ""
    for n in names:
        if n == want or n.split(":")[0] == want.split(":")[0]:
            return n
    low = want.lower()
    for n in names:
        if low in n.lower():
            return n
    return ""


def make_client(cfg):
    if cfg.provider == PROVIDER_OLLAMA:
        return OllamaClient(cfg)
    if cfg.provider == PROVIDER_API:
        return OpenAIClient(cfg)
    return None


# =====================================================================
#  引擎：后台刷新 + 新鲜度判定 + 如实状态
# =====================================================================
class VlmEngine(object):
    """一路一个实例：后台按间隔刷新多模态结果，主线程只读缓存。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = make_client(cfg)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._pending_frame = None
        self._last_call_at = 0.0
        self._last_ok_at = 0.0
        self._last_ms = 0.0
        self._persons = []
        self._objects = []
        self._summary = ""
        self._last_error = ""
        self._last_text = ""
        self._calls = 0
        self._fails = 0
        self._probe_cache = (None, "", 0.0)

    # ---- 状态 ----
    def fresh(self):
        """最近一次结果还在保鲜期内？"""
        if not self._persons and not self._objects:
            return False
        age = time.time() - self._last_ok_at
        return age <= self.cfg.interval * STALE_FACTOR

    def result(self):
        with self._lock:
            return list(self._persons), list(self._objects), self._summary

    def status_dict(self):
        with self._lock:
            age = (time.time() - self._last_ok_at) if self._last_ok_at else None
            return {"provider": self.cfg.provider, "model": self.cfg.model,
                    "enabled": self.cfg.enabled, "calls": self._calls,
                    "fails": self._fails, "last_ms": self._last_ms,
                    "age": age, "fresh": self.fresh(),
                    "persons": len(self._persons), "objects": len(self._objects),
                    "summary": self._summary, "error": self._last_error}

    def status_text(self):
        """一行中文状态（界面顶部/工具都用它，不美化、不撒谎）"""
        if not self.cfg.enabled:
            return "多模态未启用（%s）；检测源=YOLO11n" % (self.cfg.missing_reason() or "未配置")
        s = self.status_dict()
        head = "%s/%s" % (PROVIDER_LABELS.get(self.cfg.provider, self.cfg.provider),
                          self.cfg.model)
        if not self._last_ok_at:
            tail = "尚未返回结果"
            if self._last_error:
                tail += "（%s）" % _short(self._last_error)
            return "多模态 %s：%s；当前回落 YOLO11n" % (head, tail)
        state = "结果新鲜" if s["fresh"] else "结果已过期，回落 YOLO11n"
        tail = "最近 %.2fs，%d 人/%d 物品，%s" % (self._last_ms / 1000.0,
                                                s["persons"], s["objects"], state)
        if self._last_error and s["age"] is not None and s["age"] > self.cfg.interval * 2:
            tail += "；最近一次失败：%s" % _short(self._last_error)
        return "多模态 %s：%s" % (head, tail)

    def probe(self, force=False):
        """探测后端可用性（带 TTL 缓存）→ (ok, 说明)"""
        if not self.cfg.enabled:
            return False, self.cfg.missing_reason() or "多模态未配置"
        ok, msg, at = self._probe_cache
        if (not force) and at and (time.time() - at) < PROBE_TTL_S:
            return ok, msg
        if self.client is None:
            return False, "没有可用的后端客户端"
        ok, msg = self.client.probe()
        self._probe_cache = (ok, msg, time.time())
        return ok, msg

    # ---- 刷新 ----
    def maybe_refresh(self, frame):
        """主线程调用：到点了就丢给后台线程，**立即返回**（绝不阻塞渲染）"""
        if not self.cfg.enabled or frame is None:
            return
        now = time.time()
        with self._lock:
            busy = self._thread is not None and self._thread.is_alive()
            due = (now - self._last_call_at) >= self.cfg.interval
            if busy or not due:
                return
            self._last_call_at = now
            self._pending_frame = frame.copy()   # 拷贝：渲染线程会继续改原帧
            self._thread = threading.Thread(target=self._worker, name="vlm-detect",
                                            daemon=True)
            self._thread.start()

    def _worker(self):
        with self._lock:
            frame = self._pending_frame
            self._pending_frame = None
        if frame is None:
            return
        self._calls += 1
        t0 = time.time()
        try:
            persons, objects, text, dt = self.client.analyze_parsed(frame)
        except VlmError as e:
            self._fails += 1
            with self._lock:
                self._last_error = str(e)
            return
        except Exception as e:                        # 兜底：任何异常都不能打死识别
            self._fails += 1
            with self._lock:
                self._last_error = "%s: %s" % (type(e).__name__, e)
            return
        with self._lock:
            self._persons, self._objects = persons, objects
            self._last_text = text
            self._summary = str((extract_json(text)[0] or {}).get("summary", "") or "")
            self._last_ok_at = time.time()
            self._last_ms = dt * 1000.0 or (time.time() - t0) * 1000.0
            self._last_error = ""

    def close(self):
        self._stop.set()


def _short(text, n=60):
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# =====================================================================
#  Tracker：把多模态结果接进 person_detect 的跟踪/画框链路
# =====================================================================
if pd_mod is not None:
    _Base = pd_mod.PersonTracker
    _COLOR_OBJ = getattr(pd_mod, "COLOR_WARN", (0, 165, 255))
    _COLOR_BAD = getattr(pd_mod, "COLOR_BAD", (0, 0, 255))
    _ABNORMAL = tuple(getattr(pd_mod, "ABNORMAL_ACTIONS", ()))
    _draw_label = getattr(pd_mod, "draw_label", None)
else:                                            # pragma: no cover
    _Base = object
    _COLOR_OBJ = (0, 165, 255)
    _COLOR_BAD = (0, 0, 255)
    _ABNORMAL = ()
    _draw_label = None

ACT_PHONE = "使用手机"


class VlmPersonTracker(_Base):
    """多模态为主、YOLO11n 兜底的逐人检测器。

    对外接口与 person_detect.PersonTracker **完全一致**
    （analyze_only / draw_boxes / stable_count / start / stop / person_color），
    所以 02、03 的界面代码不需要为新引擎改判断逻辑 —— 只多认几个 vlm_* 参数。
    """

    def __init__(self, *args, **kwargs):
        self._vlm_provider_hint = kwargs.get("vlm_provider", PROVIDER_OFF)
        vlm_kwargs = dict((k, kwargs.pop(k)) for k in list(kwargs)
                          if k in SETTING_KEYS)
        super(VlmPersonTracker, self).__init__(*args, **kwargs)
        cfg = VlmConfig(provider=vlm_kwargs.pop("vlm_provider", PROVIDER_OFF),
                        model=vlm_kwargs.pop("vlm_model", ""),
                        base_url=vlm_kwargs.pop("vlm_base_url", ""),
                        api_key=vlm_kwargs.pop("vlm_api_key", ""),
                        interval=vlm_kwargs.pop("vlm_interval", DEFAULT_INTERVAL_S),
                        max_dim=vlm_kwargs.pop("vlm_max_dim", DEFAULT_MAX_DIM),
                        timeout=vlm_kwargs.pop("vlm_timeout", DEFAULT_TIMEOUT_S))
        self.vlm = VlmEngine(cfg)
        self._vlm_persons = []
        self._vlm_objects = []
        self._vlm_used_frame = -1
        self._vlm_last_source = "yolo"

    # ---------------- 检测源 ----------------
    def _detect(self, frame):
        """多模态优先：结果新鲜就用它的框；否则本帧回落 YOLO11n（原来的链）。"""
        if self.vlm.cfg.enabled:
            self.vlm.maybe_refresh(frame)
            persons, objects, _ = self.vlm.result()
            if self.vlm.fresh() and (persons or objects):
                fh, fw = frame.shape[:2]
                self._vlm_persons = [p for p in persons if _in_frame(p["rect"], fw, fh)]
                self._vlm_objects = [o for o in objects if _in_frame(o["rect"], fw, fh)]
                self._vlm_last_source = "vlm"
                return [tuple(int(v) for v in p["rect"]) for p in self._vlm_persons]
            self._vlm_last_source = "yolo"
        else:
            self._vlm_last_source = "yolo"
        self._vlm_persons, self._vlm_objects = [], []
        return super(VlmPersonTracker, self)._detect(frame)

    # ---------------- 结果增强 ----------------
    def analyze_only(self, frame, boundary_ratio=None, min_hits=None):
        """在原有跟踪结果上，用多模态的判读覆盖行为标签（模型看懂了画面）。"""
        persons, violations, line_y = super(VlmPersonTracker, self).analyze_only(
            frame, boundary_ratio=boundary_ratio, min_hits=min_hits)
        if not persons or self._vlm_last_source != "vlm":
            for p in persons:
                p["source"] = "yolo"
            return persons, violations, line_y
        for p in persons:
            p["source"] = "vlm"
            hit = _match_person(p["rect"], self._vlm_persons)
            if hit is None:
                continue
            action = hit["action"]
            behaviors = []
            if action and action != "正常姿态":
                behaviors.append(action)
            if hit.get("phone") and ACT_PHONE not in behaviors:
                behaviors.append(ACT_PHONE)
            p["action"] = action or p.get("action", "")
            p["vlm_action"] = action
            p["vlm_confidence"] = hit.get("confidence", 0.0)
            if hit.get("desc"):
                p["vlm_desc"] = hit["desc"]
            p["behaviors"] = behaviors
            if behaviors and action in _ABNORMAL:
                p["bad"] = True
        # 与多模态给出的"可疑物品"关联：物品框中心落在人框内 → 记到该人身上
        for obj in self._vlm_objects:
            owner = _owner_of(obj["rect"], persons)
            if owner is not None:
                owner.setdefault("vlm_objects", []).append(obj)
                owner["has_phone"] = owner.get("has_phone") or obj.get("phone")
                if obj.get("phone") and ACT_PHONE not in owner["behaviors"]:
                    owner["behaviors"].append(ACT_PHONE)
        violations = sum(1 for p in persons if p.get("bad"))
        return persons, violations, line_y

    def draw_boxes(self, frame, persons, line_y=None, scale=1.0, offset=(0, 0),
                   font_size=13, draw_line=True, **kw):
        """先画原来的人框/警戒线，再叠加多模态给出的可疑物品框。"""
        super(VlmPersonTracker, self).draw_boxes(
            frame, persons, line_y=line_y, scale=scale, offset=offset,
            font_size=font_size, draw_line=draw_line)
        if not self._vlm_objects:
            return
        ox, oy = offset
        for obj in self._vlm_objects:
            x, y, w, h = obj["rect"]
            rx = int(x * scale + ox)
            ry = int(y * scale + oy)
            rw = max(2, int(w * scale))
            rh = max(2, int(h * scale))
            color = _COLOR_BAD if obj.get("phone") else _COLOR_OBJ
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), color, 2)
            label = "⚠ %s %.2f" % (obj.get("label", "可疑物品"),
                                   float(obj.get("confidence", 0.0)))
            if _draw_label is not None:
                ty = ry - 18 if ry > 22 else ry + rh + 2
                _draw_label(frame, label, color, rx, ty, font_size)

    # ---------------- 状态 ----------------
    def vlm_status(self):
        return self.vlm.status_text()

    def vlm_status_dict(self):
        d = self.vlm.status_dict()
        d["source"] = self._vlm_last_source
        return d

    def vlm_summary(self):
        return self.vlm.status_dict().get("summary", "")

    def stop(self):
        try:
            self.vlm.close()
        except Exception:
            pass
        return super(VlmPersonTracker, self).stop()

    def start(self):
        self.vlm._stop.clear()
        return super(VlmPersonTracker, self).start()


# =====================================================================
#  工具函数（界面与 CLI 共用）
# =====================================================================
def _in_frame(rect, fw, fh):
    x, y, w, h = rect
    return w > 0 and h > 0 and x < fw and y < fh and (x + w) > 0 and (y + h) > 0


def _match_person(rect, vlm_persons):
    """把跟踪器里的某个框对上多模态给出的人框（取 IoU/包含度最高的那个）"""
    best, best_score = None, 0.0
    for item in vlm_persons:
        score = max(_iou_rect(rect, item["rect"]), _iomin_rect(rect, item["rect"]))
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 0.25 else None


def _owner_of(obj_rect, persons):
    """物品属于谁：物品框中心落在哪个人框里（没有就返回 None）"""
    ox = obj_rect[0] + obj_rect[2] / 2.0
    oy = obj_rect[1] + obj_rect[3] / 2.0
    for p in persons:
        x, y, w, h = p["rect"]
        if x <= ox <= x + w and y <= oy <= y + h:
            return p
    return None


def _iou_rect(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1, yi1 = max(ax, bx), max(ay, by)
    xi2, yi2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    union = aw * ah + bw * bh - inter
    return inter / float(union) if union > 0 else 0.0


def _iomin_rect(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1, yi1 = max(ax, bx), max(ay, by)
    xi2, yi2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    small = min(aw * ah, bw * bh)
    return inter / float(small) if small > 0 else 0.0


def tracker_class_for(provider):
    """按设置选类：provider=api/ollama → 多模态 Tracker；否则返回 None（用原版）"""
    if _norm_provider(provider) in (PROVIDER_API, PROVIDER_OLLAMA):
        return VlmPersonTracker
    return None


def accepted_params(base_cls, provider=None):
    """界面构造参数时要用的"可接受参数名"集合。

    多模态 Tracker 用 **kwargs 转发给基类，签名里看不到基类参数，
    所以这里把【基类签名】和【多模态自己的 vlm_* 参数】取并集 ——
    否则界面按签名过滤时会把 dense_mode 之类的常规参数也一并丢掉。
    """
    import inspect
    names = set(inspect.signature(base_cls.__init__).parameters)
    if _norm_provider(provider) in (PROVIDER_API, PROVIDER_OLLAMA):
        names |= set(inspect.signature(VlmPersonTracker.__init__).parameters)
        names |= set(SETTING_KEYS)
    return names


def settings_defaults():
    """给 AI 设置文件补默认键（各项目的 AI_SETTINGS_DEFAULT 直接用）"""
    return {"vlm_provider": PROVIDER_OFF, "vlm_model": "", "vlm_base_url": "",
            "vlm_api_key": "", "vlm_interval": DEFAULT_INTERVAL_S,
            "vlm_max_dim": DEFAULT_MAX_DIM, "vlm_timeout": DEFAULT_TIMEOUT_S}


def probe_text(cfg):
    """给工具/界面用的一行探测结果"""
    engine = VlmEngine(cfg)
    ok, msg = engine.probe(force=True)
    return ok, msg


if __name__ == "__main__":                        # pragma: no cover
    c = VlmConfig.from_settings({})
    print("多模态引擎自检")
    print("  配置   ：%s" % c.describe())
    print("  可用   ：%s" % ("是" if c.enabled else "否（%s）" % c.missing_reason()))
    print("  提示词 ：%d 字" % (len(SYSTEM_PROMPT) + len(USER_PROMPT)))
    print("  可选项 ：%s" % "、".join("%s=%s" % (k, ENV_KEYS[k]) for k in SETTING_KEYS))
