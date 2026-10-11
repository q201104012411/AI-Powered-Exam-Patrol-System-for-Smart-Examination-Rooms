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
    API 不可用时回退到 `ollama run <模型> <提示词>` 子进程（shell=False，
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
CHAT_TIMEOUT = 300            # 对话最长等待（秒）：本地 4B 模型 CPU 推理可能较慢
WATCH_TIMEOUT = 180           # AI 巡视最长等待（秒）
MAX_HISTORY = 10              # 保留的对话轮数（防止上下文无限增长）
API_PROBE_TIMEOUT = 1.5       # 探测 ollama API 是否在线的超时（秒）

SYSTEM_PROMPT = (
    "你是「智能考场 · 人脸识别与身份核对」终端的内置 AI 助手，运行在本地 ollama 上。\n"
    "每条用户消息都会附带【当前界面状态快照】，请结合快照理解操作员正在做什么。\n"
    "你的职责：\n"
    "1) 回答操作员的问题，尤其是关于当前操作、核验结果、拦截原因的疑问；\n"
    "2) 发现流程遗漏（未开视频、未登记同意、证件未人工核对等）时主动提醒；\n"
    "3) 回答使用简体中文，简洁、条目化，不超过 6 行；\n"
    "4) 不要编造快照里不存在的信息；不确定就说不确定。"
)

WATCH_PROMPT = (
    "以下是考场核验终端操作员当前的界面状态快照。请用简体中文 2-4 句话说明：\n"
    "① 操作员当前正在做什么；\n"
    "② 是否存在异常或流程遗漏（如未开实时视频、未登记单独同意、人脸通过后证件未核对）；\n"
    "③ 下一步建议。\n"
    "直接给结论，不要复述快照原文。"
)


def _strip_think(text):
    """Qwen3 等模型会输出 <think>…</think> 思考块，展示时去掉，只留正式回答"""
    if not text:
        return ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    # 有些模型只输出思考块：这时退回原文，宁可显示思考也比空白强
    return cleaned or text.strip()


def _api_available():
    """ollama HTTP API 是否在线（快速探测，不在线不阻塞）"""
    try:
        with urllib.request.urlopen(API_TAGS_URL, timeout=API_PROBE_TIMEOUT):
            return True
    except Exception:
        return False


def list_models(timeout=10):
    """
    列出本机可用的 ollama 模型名。优先 HTTP API，回退 `ollama list` 子进程。
    默认模型始终排在最前（即使还没 pull，也允许用户选择后自动下载）。
    """
    names = []
    try:
        with urllib.request.urlopen(API_TAGS_URL, timeout=API_PROBE_TIMEOUT) as resp:
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
    parts.append("请直接回复最后一条【操作员】消息。")
    return "\n\n".join(parts)


def ollama_chat(model, messages, timeout=CHAT_TIMEOUT):
    """
    调 ollama 完成一轮对话。返回 (ok, reply, err, transport)。
    transport: "api"（HTTP API）或 "cli"（ollama run 子进程）。
    """
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    # 路径一：HTTP API（多轮原生支持，速度更快）
    if _api_available():
        try:
            payload = json.dumps({
                "model": model,
                "messages": messages,
                "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                API_CHAT_URL, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            reply = (data.get("message") or {}).get("content") or ""
            if reply.strip():
                return True, reply, "", "api"
            return False, "", "ollama 返回了空回复（模型可能未下载完成）", "api"
        except urllib.error.HTTPError as e:
            # 404 = 模型不存在：提示先 pull，不再回退 CLI 重复报错
            if e.code == 404:
                return False, "", ("模型 %s 未安装。请先在命令控制台执行：\n"
                                   "ollama pull %s\n（或直接用本功能对话，ollama 会自动下载）"
                                   % (model, model)), "api"
            return False, "", "ollama API 错误：HTTP %s" % e.code, "api"
        except Exception as e:
            return False, "", "ollama API 调用失败：%s" % e, "api"

    # 路径二：`ollama run <模型> <提示词>`（shell=False，与 cmd_console 白名单同路径）
    res = cmd_console.run_ollama(["ollama", "run", model, _flatten_for_cli(messages)],
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
        ok, reply, err, transport = ollama_chat(self.model, messages, timeout=timeout)
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
    ok, reply, err, transport = ollama_chat(model, messages, timeout=timeout)
    duration_ms = int((time.time() - started) * 1000)
    if ok:
        reply = _strip_think(reply)
    _audit_chat("ai-watch", model, "（自动巡视）", ok, reply if ok else err,
                duration_ms, operator, tier, source)
    return ok, (reply if ok else err), transport
