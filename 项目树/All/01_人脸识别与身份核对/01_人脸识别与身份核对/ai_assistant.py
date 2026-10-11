# -*- coding: utf-8 -*-
"""
AI 助手：基于本地 ollama 大模型的多轮对话 + 当前操作感知（AI 巡视）。

两个能力：
  1. AI 对话（AIChat）：多轮对话，每条消息都自动附带"操作员当前在干什么"
     的界面状态快照，所以可以直接问"我现在该做什么""刚才为什么被拦截"；
  2. AI 巡视（analyze_activity）：不对话时，自动分析当前界面快照，
     用两三句话说明操作员在做什么、有没有流程遗漏、下一步建议。

安全与隐私边界（与 cmd_console 一致的设计立场，改动前请先读完）：
  · 只调用 ollama：优先走本机 HTTP API（127.0.0.1:11434），
    API 不可用时回退到 `ollama run <提示词>` 子进程（shell=False，
    与 cmd_console 白名单同一条执行路径）——不引入任何新的执行面；
  · 模型与数据都在本机，快照不会出本机；
  · 快照只含界面状态与结果摘要（结果区本身已按项目隐私策略脱敏），
    不主动收集身份证号等明文敏感信息；
  · 每次对话 / 巡视都写入审计哈希链（复用 cmd_console._audit），
    与命令控制台同一套留痕标准；
  · 权限沿用 accounts.cmd_console_tier：调用方（GUI）负责判层级，
    本模块本身不做权限判定，但 tier 会写入审计，拒绝路径由调用方留痕。
"""
import json
import re
import subprocess
import time
import urllib.request
import urllib.error

import cmd_console

# ---------------- 参数 ----------------
DEFAULT_MODEL = "modelscope.cn/Qwen/Qwen3-4B-GGUF"
API_CHAT_URL = "http://127.0.0.1:11434/api/chat"
API_TAGS_URL = "http://127.0.0.1:11434/api/tags"
CHAT_TIMEOUT = 120             # 关闭了思考模式后，本地 4B 通常几秒到几十秒回
WATCH_TIMEOUT = 90
MAX_HISTORY = 6                # 保留的对话轮数
API_PROBE_TIMEOUT = 1.0
API_AVAIL_TTL = 5              # API 可用性缓存（秒）
NUM_CTX = 2048                 # 限制上下文窗口：4B 模型窗口越小推理越快

# ---------------- 知识库（按主题拆分，按需注入，避免每条消息都背整本书） ----------------
_BASE_PROMPT = (
    "你是「智能考场 · 人脸识别与身份核对」终端的本地 AI 助手。\n"
    "每条用户消息都会附带【当前界面状态快照】，请结合快照回答。\n\n"
    "回答规则：\n"
    "1) 直接给答案，不要输出思考过程；\n"
    "2) 使用简体中文，条目化，不超过 6 条；\n"
    "3) 不确定就说不确定，不要编造快照里不存在的信息；\n"
    "4) 身份证号只以掩码/哈希形式存在，不要向用户索要身份证明文。"
)

# 巡视独立使用更短的系统提示，不再背整本知识库
WATCH_SYSTEM_PROMPT = (
    "你是考场核验终端的 AI 巡视员。只根据状态快照做极简判断：\n"
    "1) 操作员在做什么；2) 有无异常或遗漏；3) 下一步建议。\n"
    "用简体中文 2-4 句话，不要思考过程，不要复述快照原文。"
)

WATCH_PROMPT = (
    "以下是当前界面状态快照。请直接给出结论：\n"
    "① 操作员当前正在做什么；\n"
    "② 是否存在异常或流程遗漏（如未开实时视频、未登记单独同意、人脸通过后证件未核对）；\n"
    "③ 下一步建议。"
)

_REASON_CODES_KB = (
    "核验结果与处置建议：\n"
    "PASS（通过）：提醒核对证件实物（如尚未核对）。\n"
    "NOT_FOUND（库中无此考生）：检查编号/准考证号是否输错，或名单未导入。\n"
    "ID/TICKET/SEAT_MISMATCH（信息不一致）：拦截并人工复核。\n"
    "CONSENT_MISSING：必须先「登记单独同意」取得考生授权，否则不能采集人脸。\n"
    "NO_FACE / MULTI_FACE：调整摄像头角度，让考生单人正对镜头后重拍。\n"
    "LOW_QUALITY / IMAGE_QUALITY_REVIEW：补光、靠近摄像头、避免模糊后重拍。\n"
    "LIVENESS_UNAVAILABLE：严格模式下只能用实时摄像头抓拍，不能上传本地照片。\n"
    "LIVENESS_FAILED：要求考生眨眼/转头，确认不是照片/视频翻拍。\n"
    "LOW_SIMILARITY：相似度低于阈值，转人工核验，必要时双人见证。\n"
    "AMBIGUOUS（灰区）：不确信区间，必须人工复验，由见证人签字后重验。\n"
    "TWIN_SUSPECT / IDENTITY_CONFLICT：撞脸/孪生/混淆风险，必须双人见证复验。\n"
    "POSE_EXTREME：让考生正对摄像头，避免仰头/侧脸/歪头。\n"
    "NOT_MATCHED：相似度显著偏低，判定非本人，拦截并人工处理。"
)

_OPERATIONS_KB = (
    "系统操作指引：\n"
    "1:1 入场核验：选考生编号 → 开实时视频 → 点「用当前画面核验」或「开始核验」。\n"
    "1:N 考场抽查：切到 1:N 模式 → 抓拍 → 系统比对全库给出最像考生。\n"
    "自动核验：所有者授权后勾选「自动核验（免点击）」，系统定时抓拍核验。\n"
    "证件核对：人脸通过后，界面自动带出考生信息，工作人员需对照证件实物点「证件一致」或「证件不符」。\n"
    "登记单独同意：选考生 → 更多操作 → 登记单独同意，需考生单独签署。\n"
    "名单导入：更多操作 → 名单导入，按 Excel 模板填写后上传。\n"
    "建档：首次使用或名单变更后，点「为该考生建档」或「全部建档」生成人脸特征索引。"
)

_TROUBLESHOOTING_KB = (
    "常见故障排查：\n"
    "视频未开启/摄像头打不开：检查摄像头编号（默认 0），是否被其他程序占用，驱动是否正常。\n"
    "模型首次加载慢：insightface 首次加载 buffalo_l 需数秒，请等待；若提示缺模型，运行 python 工具/download_buffalo_l.py 或手动解压 buffalo_l.zip 到 ~/.insightface/models/buffalo_l。\n"
    "ollama 不可用/模型未下载：命令控制台执行 `ollama pull modelscope.cn/Qwen/Qwen3-4B-GGUF`。\n"
    "核验一直失败：检查光线、考生姿态、是否已登记同意、是否开启实时视频。"
)

_COMPLIANCE_KB = (
    "合规与安全要点：\n"
    "严格模式默认开启：关闭后允许上传照片核验，但会在审计中留痕。\n"
    "大考模式：开启后必须校验身份证哈希（四要素），未登记身份证的考生会被拦截。\n"
    "灰区/撞脸/身份混淆：必须由两名工作人员（含见证人）共同确认，见证人栏必填。\n"
    "所有核验、操作、命令控制台执行都写入审计哈希链，事后不可篡改。"
)

# 触发关键词：命中才注入对应知识库
_REASON_KEYS = ["失败", "拦截", "原因", "为什么", "不通过", "相似度", "灰区", "撞脸",
                "liveness", "low", "mismatch", "not_found", "not matched", "无法", "错误"]
_OPERATION_KEYS = ["怎么", "如何", "步骤", "流程", "操作", "建档", "导入", "登记",
                   "同意", "开始", "核验", "开启", "抓拍", "使用"]
_TROUBLE_KEYS = ["打不开", "慢", "卡", "错误", "不可用", "失败", "无法", "没反应",
                 "ollama", "摄像头", "模型", "加载"]
_COMPLIANCE_KEYS = ["合规", "同意", "见证人", "严格模式", "大考模式", "审计", "隐私", "双人"]


def _build_system_prompt(user_message="", snapshot="", include_all=False):
    """
    动态系统提示：根据用户问题和快照，只加载最相关的知识库。
    这样大多数问题的输入 token 比背整本书短很多，本地 4B 模型响应更快。
    """
    combined = ((user_message or "") + " " + (snapshot or "")).lower()
    sections = []
    if include_all or any(k in combined for k in _REASON_KEYS):
        sections.append(_REASON_CODES_KB)
    if include_all or any(k in combined for k in _OPERATION_KEYS):
        sections.append(_OPERATIONS_KB)
    if include_all or any(k in combined for k in _TROUBLE_KEYS):
        sections.append(_TROUBLESHOOTING_KB)
    if include_all or any(k in combined for k in _COMPLIANCE_KEYS):
        sections.append(_COMPLIANCE_KB)
    # 完全没命中关键词时兜底（比如用户只打个"你好"）：只加载常用操作 + 合规，
    # 不背整本知识库——这样既省 token 又省时间，还能正常回答基础问题。
    if not sections:
        sections = [_OPERATIONS_KB, _COMPLIANCE_KB]
    return _BASE_PROMPT + "\n\n" + "\n\n".join(sections)


# ---------------- ollama HTTP 客户端 ----------------
_API_OPENER = urllib.request.build_opener(urllib.request.HTTPHandler())
_API_AVAIL_CACHE = {"avail": None, "at": 0, "ttl": API_AVAIL_TTL}


def _strip_think(text):
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    return cleaned or text.strip()


def _api_available():
    now = time.time()
    if _API_AVAIL_CACHE["avail"] is not None and now - _API_AVAIL_CACHE["at"] < _API_AVAIL_CACHE["ttl"]:
        return _API_AVAIL_CACHE["avail"]
    try:
        with _API_OPENER.open(API_TAGS_URL, timeout=API_PROBE_TIMEOUT):
            avail = True
    except Exception:
        avail = False
    _API_AVAIL_CACHE.update({"avail": avail, "at": now})
    return avail


def list_models(timeout=10):
    names = []
    try:
        with _API_OPENER.open(API_TAGS_URL, timeout=API_PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for m in data.get("models", []):
            n = (m.get("name") or m.get("model") or "").strip()
            if n:
                names.append(n)
    except Exception:
        pass
    if not names:
        try:
            proc = subprocess.run(["ollama", "list"], capture_output=True, timeout=timeout)
            text = cmd_console.clean_output(cmd_console._decode(proc.stdout or b""))
            for line in text.splitlines():
                parts = line.split()
                if parts and parts[0].upper() != "NAME":
                    names.append(parts[0])
        except Exception:
            pass
    seen, ordered = set(), []
    for n in [DEFAULT_MODEL] + names:
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _flatten_for_cli(messages):
    parts = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append("【系统设定】\n" + content)
        elif role == "assistant":
            parts.append("【你之前的回答】\n" + content)
        else:
            parts.append("【操作员】\n" + content)
    parts.append("请直接回复最后一条【操作员】消息，不要输出思考过程。")
    return "\n\n".join(parts)


def _parse_api_response(raw):
    try:
        data = json.loads(raw)
        return True, ((data.get("message") or {}).get("content") or ""), ""
    except json.JSONDecodeError:
        parts = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                parts.append((d.get("message") or {}).get("content") or "")
            except Exception:
                pass
        if parts:
            return True, "".join(parts), ""
        return False, "", "无法解析 ollama 的返回"


def _api_generate(model, messages, timeout, num_predict=384, think=False):
    is_qwen = "qwen" in model.lower()
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": num_predict},  # 不强制 num_ctx：让 ollama 用模型默认窗口，避免截断
    }
    if think and is_qwen:
        payload["think"] = False
    req = urllib.request.Request(
        API_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _API_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _parse_api_response(raw)


def ollama_chat(model, messages, timeout=CHAT_TIMEOUT, num_predict=384):
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    is_qwen = "qwen" in model.lower()

    if _api_available():
        try:
            ok, reply, err = _api_generate(model, messages, timeout,
                                           num_predict=num_predict, think=is_qwen)
        except urllib.error.HTTPError as e:
            if e.code == 400 and is_qwen:
                try:
                    ok, reply, err = _api_generate(model, messages, timeout,
                                                   num_predict=num_predict)
                except Exception as e2:
                    return False, "", "ollama API 调用失败：%s" % e2, "api"
            elif e.code == 404:
                return False, "", ("模型 %s 未安装。请先在命令控制台执行：\n"
                                   "ollama pull %s\n（或直接用本功能对话，ollama 会自动下载）"
                                   % (model, model)), "api"
            else:
                return False, "", "ollama API 错误：HTTP %s" % e.code, "api"
        except Exception as e:
            return False, "", "ollama API 调用失败：%s" % e, "api"
        if ok and reply.strip():
            return True, reply, "", "api"
        # 第一次得到空回复：可能是提示词太长/上下文截断导致模型无话可讲，
        # 用极简系统提示重试一次，优先保证用户能看到输出。
        try:
            ok2, reply2, _ = _api_generate(
                model,
                [{"role": "system", "content": "你是一个本地 AI 助手，请直接回答用户。"},
                 {"role": "user", "content": (messages[-1].get("content") or "")}],
                timeout, num_predict=num_predict)
            if ok2 and reply2.strip():
                return True, reply2, "", "api"
        except Exception:
            pass
        return False, "", (err or "ollama 返回了空回复（模型可能未下载完成）"), "api"

    prompt = _flatten_for_cli(messages)
    if is_qwen:
        prompt += "\n\n/no_think"
    res = cmd_console.run_ollama(["ollama", "run", model, prompt], timeout=timeout)
    if res["ok"] and (res["stdout"] or "").strip():
        return True, res["stdout"], "", "cli"
    err = (res["stderr"] or res["stdout"] or "").strip()
    if res.get("timed_out"):
        err = "等待超过 %d 秒，已终止。本地模型首次加载 / 下载较慢，可稍后再试。" % timeout
    elif not err:
        err = "ollama 不可用或未安装模型 %s" % model
    return False, "", err, "cli"


def _audit_chat(kind, model, text, ok, detail, duration_ms, operator, tier, source,
                timed_out=False):
    result = {
        "ok": ok,
        "exit_code": 0 if ok else (None if not detail else 1),
        "stdout": (detail or "")[:4000] if ok else "",
        "stderr": "" if ok else (detail or "")[:4000],
        "duration_ms": duration_ms,
        "truncated": False,
        "timed_out": timed_out,
    }
    try:
        cmd_console._audit("%s[%s]：%s" % (kind, model, (text or "")[:300]),
                           kind, operator, source, result, tier or "full")
    except Exception:
        pass


class AIChat:
    def __init__(self, model=None):
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.history = []

    def ask(self, text, snapshot="", operator="", tier="full", source="gui",
            timeout=CHAT_TIMEOUT, free_mode=False):
        text = (text or "").strip()
        if not text:
            return False, "请输入内容", ""
        if free_mode:
            # 自由对话：不注入考场知识库和界面快照，只保留最基本的助手设定
            messages = [
                {"role": "system", "content": "你是一个本地 AI 助手，请用中文自然、友好地回答用户。"},
            ] + self.history + [{"role": "user", "content": text}]
        else:
            user_content = text
            if snapshot:
                user_content = ("【当前界面状态快照】\n%s\n\n【我的消息】\n%s"
                                % (snapshot, text))
            system_prompt = _build_system_prompt(text, snapshot)
            messages = ([{"role": "system", "content": system_prompt}]
                        + self.history
                        + [{"role": "user", "content": user_content}])
        started = time.time()
        ok, reply, err, transport = ollama_chat(self.model, messages, timeout=timeout,
                                                num_predict=(512 if free_mode else 384))
        duration_ms = int((time.time() - started) * 1000)
        if ok:
            reply = _strip_think(reply)
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            if len(self.history) > MAX_HISTORY * 2:
                self.history = self.history[-MAX_HISTORY * 2:]
        detail = reply if ok else err
        _audit_chat("ai-chat", self.model, text, ok, detail, duration_ms,
                    operator, tier, source,
                    timed_out=("超过" in (err or "") and "秒" in (err or "")))
        return ok, (reply if ok else err), transport


def analyze_activity(snapshot, model=None, operator="", tier="full", source="gui",
                     timeout=WATCH_TIMEOUT):
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    # 巡视用独立短提示词，不背整本知识库，响应更快
    messages = [
        {"role": "system", "content": WATCH_SYSTEM_PROMPT},
        {"role": "user", "content": WATCH_PROMPT + "\n\n" + (snapshot or "（无快照）")},
    ]
    started = time.time()
    ok, reply, err, transport = ollama_chat(model, messages, timeout=timeout,
                                            num_predict=256)
    duration_ms = int((time.time() - started) * 1000)
    if ok:
        reply = _strip_think(reply)
    _audit_chat("ai-watch", model, "（自动巡视）", ok, reply if ok else err,
                duration_ms, operator, tier, source)
    return ok, (reply if ok else err), transport
