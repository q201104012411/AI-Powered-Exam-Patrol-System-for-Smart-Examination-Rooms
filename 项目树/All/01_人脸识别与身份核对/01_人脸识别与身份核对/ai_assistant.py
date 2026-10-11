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
CHAT_TIMEOUT = 180           # 对话最长等待（秒）：关闭了思考模式后，4B 模型通常几秒就回
WATCH_TIMEOUT = 120          # AI 巡视最长等待（秒）
MAX_HISTORY = 6              # 保留的对话轮数（防止上下文无限增长；4B 模型上下文越长越慢）
API_PROBE_TIMEOUT = 1.0      # 探测 ollama API 是否在线的超时（秒）
API_AVAIL_TTL = 5            # API 可用性缓存有效期（秒）：避免每条消息都额外探测一次

SYSTEM_PROMPT = (
    "你是「智能考场 · 人脸识别与身份核对」终端的本地 AI 助手。\n"
    "每条用户消息都会附带【当前界面状态快照】，请结合快照回答。\n\n"
    "回答规则：\n"
    "1) 直接给答案，不要输出思考过程；\n"
    "2) 使用简体中文，条目化，不超过 6 条；\n"
    "3) 不确定就说不确定，不要编造快照里不存在的信息；\n"
    "4) 身份证号只以掩码/哈希形式存在，不要向用户索要身份证明文。\n\n"
    "你能做的事情：\n"
    "A. 解读核验结果与原因码，并给出下一步处置建议\n"
    "   · PASS（核验通过）：提醒核对证件实物（如尚未核对）。\n"
    "   · NOT_FOUND（库中无此考生）：检查考生编号、准考证号是否输错，或名单未导入。\n"
    "   · ID_MISMATCH / TICKET_MISMATCH / SEAT_MISMATCH：信息不一致，拦截，人工复核。\n"
    "   · CONSENT_MISSING：必须先点「登记单独同意」取得考生授权，否则不能采集人脸。\n"
    "   · NO_FACE / MULTI_FACE：调整摄像头角度、让考生单人正对镜头，重新抓拍。\n"
    "   · LOW_QUALITY / IMAGE_QUALITY_REVIEW：光线不足/人脸过小/模糊，补光或靠近摄像头重拍。\n"
    "   · LIVENESS_UNAVAILABLE：严格模式下只能用实时摄像头抓拍，不能上传本地照片。\n"
    "   · LIVENESS_FAILED：要求考生眨眼/转头，确认不是照片/视频翻拍。\n"
    "   · LOW_SIMILARITY：相似度低于阈值，转人工核验，必要时双人见证。\n"
    "   · AMBIGUOUS（灰区）：不确信区间，必须人工复验，由见证人签字后重验。\n"
    "   · TWIN_SUSPECT / IDENTITY_CONFLICT：撞脸/孪生/混淆风险，必须双人见证复验。\n"
    "   · POSE_EXTREME：让考生正对摄像头、避免仰头/侧脸/歪头。\n"
    "   · NOT_MATCHED：相似度显著偏低，判定非本人，拦截并人工处理。\n\n"
    "B. 系统操作指引\n"
    "   · 入场核验（1:1）：选考生编号 → 开实时视频 → 点「用当前画面核验」或「开始核验」。\n"
    "   · 考场抽查（1:N）：切到 1:N 模式 → 抓拍 → 系统比对全库给出最像考生。\n"
    "   · 自动核验：所有者授权后勾选「自动核验（免点击）」，系统定时抓拍并核验。\n"
    "   · 证件核对：人脸通过后，界面自动带出考生信息，工作人员需对照证件实物点「证件一致」或「证件不符」。\n"
    "   · 登记单独同意：选考生 → 更多操作 → 登记单独同意，需考生单独签署。\n"
    "   · 名单导入：更多操作 → 名单导入，按 Excel 模板填写后上传。\n"
    "   · 建档：首次使用或名单变更后，点「为该考生建档」或「全部建档」生成人脸特征索引。\n\n"
    "C. 常见故障排查\n"
    "   · 视频未开启/摄像头打不开：检查摄像头编号（默认 0），是否被其他程序占用，驱动是否正常。\n"
    "   · 模型首次加载慢：insightface 首次加载 buffalo_l 需数秒，请等待；若提示缺模型，运行 python 工具/download_buffalo_l.py 或手动解压 buffalo_l.zip 到 ~/.insightface/models/buffalo_l。\n"
    "   · ollama 不可用/模型未下载：命令控制台执行 `ollama pull modelscope.cn/Qwen/Qwen3-4B-GGUF`。\n"
    "   · 核验一直失败：检查光线、考生姿态、是否已登记同意、是否开启实时视频。\n\n"
    "D. 合规与安全要点\n"
    "   · 严格模式默认开启：关闭后允许上传照片核验，但会在审计中留痕。\n"
    "   · 大考模式：开启后必须校验身份证哈希（四要素），未登记身份证的考生会被拦截。\n"
    "   · 灰区/撞脸/身份混淆：必须由两名工作人员（含见证人）共同确认，见证人栏必填。\n"
    "   · 所有核验、操作、命令控制台执行都写入审计哈希链，事后不可篡改。\n\n"
    "主动提醒时机：看到快照里「实时视频：未开启」「同意状态：未登记」"
    "「证件核对：尚未核验」「灰区/撞脸风险但无见证人」时，必须主动指出。"
)

WATCH_PROMPT = (
    "以下是考场核验终端操作员当前的界面状态快照。请直接给出结论，不要思考过程。\n"
    "用简体中文 2-4 句话说明：\n"
    "① 操作员当前正在做什么；\n"
    "② 是否存在异常或流程遗漏（如未开实时视频、未登记单独同意、人脸通过后证件未核对）；\n"
    "③ 下一步建议。\n"
    "不要复述快照原文。"
)

# 复用同一个 opener，让多次 API 调用能复用 TCP 连接（keep-alive），减少握手开销。
_API_OPENER = urllib.request.build_opener(urllib.request.HTTPHandler())
_API_AVAIL_CACHE = {"avail": None, "at": 0, "ttl": API_AVAIL_TTL}


def _strip_think(text):
    """Qwen3 等模型会输出 <think>…</think> 思考块，展示时去掉，只留正式回答"""
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    # 有些模型只输出思考块：这时退回原文，宁可显示思考也比空白强
    return cleaned or text.strip()


def _api_available():
    """
    ollama HTTP API 是否在线。带 5 秒缓存，避免连续对话/巡视时每轮都多发一次探测请求。
    """
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
    """
    列出本机可用的 ollama 模型名。优先 HTTP API，回退 `ollama list` 子进程。
    默认模型始终排在最前（即使还没 pull，也允许用户选择后自动下载）。
    """
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
            proc = subprocess.run(["ollama", "list"], capture_output=True,
                                  timeout=timeout)
            text = cmd_console.clean_output(
                cmd_console._decode(proc.stdout or b""))
            for line in text.splitlines():
                parts = line.split()
                # 跳过表头（NAME ID SIZE ...）与空行
                if parts and parts[0].upper() != "NAME":
                    names.append(parts[0])
        except Exception:
            pass
    # 去重且默认模型置顶
    seen, ordered = set(), []
    for n in [DEFAULT_MODEL] + names:
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _flatten_for_cli(messages):
    """把多轮消息压成单条提示词（CLI 回退路径一次只问一轮，历史写进提示词）"""
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
    """解析 /api/chat 的返回：单 JSON 或 NDJSON（旧版 ollama 忽略 stream:false）"""
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
    """调一次 /api/chat。think 仅对 Qwen 系模型启用，避免其它模型 400。"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    # Qwen3 的 /no_think 对应 API 参数：只有 Qwen 模型才发 think:false
    if think and "qwen" in model.lower():
        payload["think"] = False
    req = urllib.request.Request(
        API_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _API_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _parse_api_response(raw)


def ollama_chat(model, messages, timeout=CHAT_TIMEOUT, num_predict=384):
    """
    调 ollama 完成一轮对话。返回 (ok, reply, err, transport)。
    transport: "api"（HTTP API）或 "cli"（ollama run 子进程）。
    """
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    is_qwen = "qwen" in model.lower()

    # 路径一：HTTP API（多轮原生支持，速度更快）
    if _api_available():
        try:
            ok, reply, err = _api_generate(model, messages, timeout,
                                           num_predict=num_predict,
                                           think=is_qwen)
        except urllib.error.HTTPError as e:
            if e.code == 400 and is_qwen:
                # 旧版 ollama 不认识 think 字段：去掉后重试一次
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
        return False, "", (err or "ollama 返回了空回复（模型可能未下载完成）"), "api"

    # 路径二：`ollama run <模型> <提示词>`（shell=False，与 cmd_console 白名单同路径）
    # 结尾附 /no_think：Qwen3 的关闭思考指令，其它模型会把它当普通文本，无害
    prompt = _flatten_for_cli(messages)
    if is_qwen:
        prompt += "\n\n/no_think"
    res = cmd_console.run_ollama(["ollama", "run", model, prompt],
                                 timeout=timeout)
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
    """AI 对话 / 巡视也写入审计哈希链（与命令控制台同一标准）"""
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
        pass                      # 审计失败不丢对话结果


class AIChat:
    """
    多轮对话会话。每条消息自动附带界面快照——这是"AI 知道我在干什么"的实现方式。
    """

    def __init__(self, model=None):
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.history = []       # [{"role","content"}]，只存用户原文与 AI 正式回答

    def ask(self, text, snapshot="", operator="", tier="full", source="gui",
            timeout=CHAT_TIMEOUT):
        text = (text or "").strip()
        if not text:
            return False, "请输入内容", ""
        user_content = text
        if snapshot:
            user_content = ("【当前界面状态快照】\n%s\n\n【我的消息】\n%s"
                            % (snapshot, text))
        messages = ([{"role": "system", "content": SYSTEM_PROMPT}]
                    + self.history
                    + [{"role": "user", "content": user_content}])
        started = time.time()
        ok, reply, err, transport = ollama_chat(self.model, messages, timeout=timeout,
                                                num_predict=384)  # 对话也限长，本地 4B 更快
        duration_ms = int((time.time() - started) * 1000)
        if ok:
            reply = _strip_think(reply)
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            # 只保留最近 N 轮，防止上下文膨胀把本地小模型压垮
            if len(self.history) > MAX_HISTORY * 2:
                self.history = self.history[-MAX_HISTORY * 2:]
        detail = reply if ok else err
        _audit_chat("ai-chat", self.model, text, ok, detail, duration_ms,
                    operator, tier, source,
                    timed_out=("超过" in (err or "") and "秒" in (err or "")))
        return ok, (reply if ok else err), transport


def analyze_activity(snapshot, model=None, operator="", tier="full", source="gui",
                     timeout=WATCH_TIMEOUT):
    """
    AI 巡视：不对话时自动分析"操作员在干什么"。返回 (ok, 分析文本, transport)。
    """
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": WATCH_PROMPT + "\n\n" + (snapshot or "（无快照）")},
    ]
    started = time.time()
    ok, reply, err, transport = ollama_chat(model, messages, timeout=timeout,
                                            num_predict=256)  # 巡视更短更快
    duration_ms = int((time.time() - started) * 1000)
    if ok:
        reply = _strip_think(reply)
    _audit_chat("ai-watch", model, "（自动巡视）", ok, reply if ok else err,
                duration_ms, operator, tier, source)
    return ok, (reply if ok else err), transport
