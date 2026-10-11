# -*- coding: utf-8 -*-
"""
智能终端 · 本地 AI 助手（ollama）

与 01 侧保持架构一致但提示词面向智能终端场景：
  · 告警分级处置（橙/红）
  · 复核留痕、误报闭环
  · 手持核验、考场时钟、断网补传
  · 终端统一管控与巡检

数据不出本机：直接访问本地 ollama HTTP API，失败时回退到 ollama CLI。
"""
import json
import os
import re
import subprocess
import time
import urllib.request

DEFAULT_MODEL = "modelscope.cn/Qwen/Qwen3-4B-GGUF"
API_CHAT_URL = "http://127.0.0.1:11434/api/chat"
API_TAGS_URL = "http://127.0.0.1:11434/api/tags"
API_PROBE_TIMEOUT = 3
API_AVAIL_TTL = 5
CHAT_TIMEOUT = 120

_API_OPENER = urllib.request.build_opener(urllib.request.HTTPHandler())
_API_AVAIL_CACHE = {"avail": None, "at": 0, "ttl": API_AVAIL_TTL}

_SYSTEM_PROMPT = (
    "你是「智能终端操作端」的本地 AI 助手，服务考场巡考、告警处置与复核留痕工作。\n"
    "回答规则：\n"
    "1) 使用简体中文，条目化，直接给结论，不要输出思考过程；\n"
    "2) 不确定就说不确定，不要编造考生、考场、告警等不存在的信息；\n"
    "3) 身份证号、手机号等敏感信息只以掩码或哈希形式出现，不要索要明文；\n"
    "4) 涉及红色预警等高风险结论，必须提醒人工复核与见证人留痕。\n\n"
    "你能回答：告警含义与处置建议、复核流程、断网补传、手持核验、\n"
    "考场时钟/统一管控/终端账户权限等操作问题。"
)

_FREE_PROMPT = "你是一个本地 AI 助手，请用中文自然、友好地回答用户。"

# ---------------- 按界面区分的提示词 ----------------
# 同一个 AI 面板在不同页签下承担不同职责，提示词必须跟着界面走，
# 否则模型只能用通用口吻兜圈子（用户反馈"感觉傻傻的"）。
INTERFACE_PROMPTS = {
    "alerts": (
        "【当前界面】告警处置。\n"
        "你现在的职责是帮操作员**快速处置告警**：\n"
        "· 解释告警码含义（MULTI_FACE 多人同框 / NO_INVIGILATOR 监考缺位 / PHONE_DETECTED 手机等作弊器材 / "
        "REPEATED_TOUCH_LINE 多次触碰警戒线 / BIG_TURN 大幅度偏转 / GAZE_SHIFT 眼神游移 / TURN_HEAD 频繁转头 / "
        "LOOK_DOWN 长时间低头 / SLEEPY 闭眼趴睡 / HEAD_TURN 转脸偏头 / STAND_UP 起立 / LEAN_FORWARD 前倾趴桌）；\n"
        "· 给出处置顺序建议（红色预警必须先确认再复核，橙色可批量处理）；\n"
        "· 提示 SLA 超时、去重规则（同考场+座位+告警码 60 秒内只保留一条）；\n"
        "· 回答务必落到「下一步点哪个按钮」（确认选中 / 一键上报 / 复制编号 / 立即补传）。\n"
        "不要讲复核留痕的细节（那是另一个页签的事）。"
    ),
    "review": (
        "【当前界面】复核留痕。\n"
        "你现在的职责是帮操作员**完成复核闭环并留痕**：\n"
        "· 解释复核结论取值（confirmed 属实 / false_positive 误报 / need_onsite 需现场核实）各自后果；\n"
        "· 强调红色预警必须双人见证、复核理由要写清依据；\n"
        "· 提示误报会回写以修正设备可信度，不要为了省事把真警标成误报；\n"
        "· 涉及审计链时说明复核记录写入哈希链、不可篡改。\n"
        "回答落在「怎么填复核结论与理由」上。"
    ),
    "verify": (
        "【当前界面】手持核验。\n"
        "你现在的职责是帮操作员**完成人脸/身份核验**：\n"
        "· 解释核验结论（PASS 通过 / FACE_MISMATCH 人脸不符 / NO_INVIGILATOR 无此人 / 活体失败）；\n"
        "· 说明核验失败的标准处置：先核对证件原件，再走人工复验并留痕；\n"
        "· 提醒活体与现场性要求、同意状态、见证人；\n"
        "· 只谈核验动作本身，不要扩散到告警派单。"
    ),
    "monitor": (
        "【当前界面】监控查看。\n"
        "你现在的职责是帮操作员**看懂实时监控与 AI 框选识别**：\n"
        "· 解释三色框（绿=行为正常 / 橙=异常行为 / 红=越界或触碰警戒线）与青色人脸小框；\n"
        "· 解释逐人编号（#1 #2 …）与行为标签（起立、大幅度偏转、眼神游移、东张西望、长时间低头、闭眼趴睡、前倾趴桌）；\n"
        "· 区分「考场监考」有 AI、「非考场监控」无 AI；\n"
        "· 说明持续异常（约 0.7 秒 / 20 帧）才会生成告警，避免误报刷屏；\n"
        "· 摄像头配置（推流地址/区域）仅所有者可改。\n"
        "不要回答告警派单流程，聚焦「画面里发生了什么」。"
    ),
    "ops": (
        "【当前界面】运行状态与管控。\n"
        "你现在的职责是帮所有者/管理员**运维这套终端**：\n"
        "· 解释断网补传（outbox 队列、重连后自动补传）、考场标准时钟与校时；\n"
        "· 解释终端账户权限矩阵（所有者 / 管理员 / 巡考员 / 监考员 / 核验员各自能做什么）；\n"
        "· 解释权限控制台各开关（命令控制台总开关、授权管理员 ollama、允许非所有者 AI 对话、隐藏命令行窗口）；\n"
        "· 生成演示告警仅所有者可用，且来源标注为 demo，不冒充真实接入；\n"
        "· 涉及不可逆操作（清空本地数据）必须提醒先备份并确认。"
    ),
}


def build_system_prompt(interface=None, free_mode=False):
    """
    组装系统提示词：自由对话模式用宽松提示；否则 = 通用基座 + 当前界面职责。
    interface 取值见 INTERFACE_PROMPTS；未知或不传则退回通用基座。
    """
    if free_mode:
        return _FREE_PROMPT
    base = _SYSTEM_PROMPT
    part = INTERFACE_PROMPTS.get((interface or "").strip())
    if part:
        return base + "\n\n" + part
    return base


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
        with _API_OPENER.open(API_TAGS_URL, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for m in data.get("models", []):
            n = m.get("name") or m.get("model")
            if n:
                names.append(n)
    except Exception:
        pass
    return names


def _parse_api_response(raw):
    try:
        data = json.loads(raw)
    except Exception as e:
        return False, "", "ollama 返回非 JSON：%s" % e
    if data.get("done") is not True and "error" not in data:
        return False, "", "ollama 响应未结束：%s" % raw[:200]
    if data.get("error"):
        return False, "", "ollama 错误：%s" % data["error"]
    msg = (data.get("message") or data.get("response") or {})
    text = (msg.get("content") if isinstance(msg, dict) else str(msg)).strip()
    if not text:
        return False, "", "ollama 返回了空回复"
    return True, text, ""


def _api_generate(model, messages, timeout, num_predict=384):
    is_qwen = "qwen" in model.lower()
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    if is_qwen:
        payload["think"] = False
    req = urllib.request.Request(
        API_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _API_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _parse_api_response(raw)


def _cli_generate(model, messages, timeout, num_predict=384):
    """HTTP 不可用时回退到 ollama CLI（只取最后一条 user 内容做单轮问答）"""
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break
    if not user_text:
        return False, "", "无可发送内容"
    cmd = ["ollama", "run", model]
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    try:
        p = subprocess.run(cmd, input=user_text, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "", "ollama CLI 超时（%.0fs）" % timeout
    except Exception as e:
        return False, "", "ollama CLI 调用失败：%s" % e
    out = _clean_output((p.stdout or "") + (p.stderr or ""))
    if not out.strip():
        return False, "", "ollama CLI 无输出（模型 %s 可能未下载）" % model
    return True, out, ""


def _clean_output(text):
    if not text:
        return ""
    text = re.sub(r"\r", "", text)
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
    return text.strip()


def ollama_chat(model, messages, timeout=CHAT_TIMEOUT, num_predict=384):
    if _api_available():
        try:
            ok, reply, err = _api_generate(model, messages, timeout, num_predict)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "", "模型 %s 未安装，请先执行 ollama pull %s" % (model, model), "api"
            return False, "", "ollama API 错误：HTTP %s" % e.code, "api"
        except Exception as e:
            return False, "", "ollama API 调用失败：%s" % e, "api"
        if ok and reply.strip():
            return True, reply, "", "api"
        # 空回复或失败时，用极简提示重试一次
        try:
            ok2, reply2 = _api_generate(
                model,
                [{"role": "system", "content": _FREE_PROMPT},
                 {"role": "user", "content": (messages[-1].get("content") or "")}],
                timeout, num_predict)[:2]
            if ok2 and reply2.strip():
                return True, reply2, "", "api"
        except Exception:
            pass
        return False, "", (err or "ollama 返回了空回复"), "api"
    return _cli_generate(model, messages, timeout, num_predict) + ("cli",)


class AIChat(object):
    MAX_HISTORY = 6

    def __init__(self, model=None):
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.history = []

    def ask(self, text, operator="", tier="full", source="terminal",
            timeout=CHAT_TIMEOUT, free_mode=False, interface=None):
        text = (text or "").strip()
        if not text:
            return False, "请输入内容", ""
        system_prompt = build_system_prompt(interface, free_mode=free_mode)
        messages = ([{"role": "system", "content": system_prompt}]
                    + self.history
                    + [{"role": "user", "content": text}])
        started = time.time()
        ok, reply, err, transport = ollama_chat(self.model, messages, timeout=timeout,
                                                num_predict=(512 if free_mode else 384))
        duration_ms = int((time.time() - started) * 1000)
        if ok:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply})
            if len(self.history) > self.MAX_HISTORY * 2:
                self.history = self.history[-self.MAX_HISTORY * 2:]
        detail = reply if ok else err
        # 简单审计日志（不依赖 storage，避免循环导入）
        try:
            from storage import Store
            Store().log("ai-chat", "%s | %s | ok=%s | %s" % (
                operator, ("free" if free_mode else "normal"), ok,
                (detail or "")[:200]), operator)
        except Exception:
            pass
        return ok, (reply if ok else err), transport


def analyze_activity(snapshot, model=None, operator="", tier="full",
                     source="terminal", timeout=CHAT_TIMEOUT, interface=None):
    """不对话时的自动分析：提示词同样跟随当前界面，避免答非所问。"""
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    scene = INTERFACE_PROMPTS.get((interface or "").strip(), "")
    messages = [
        {"role": "system",
         "content": ("你是智能终端巡视员。" + (scene + "\n" if scene else "")
                     + "根据状态快照判断：1)操作员在做什么；2)有无异常；3)下一步建议。"
                       "2-4 句话，不复述快照。")},
        {"role": "user", "content": (snapshot or "（无快照）")},
    ]
    started = time.time()
    ok, reply, err, transport = ollama_chat(model, messages, timeout=timeout,
                                            num_predict=256)
    if ok:
        pass
    try:
        from storage import Store
        Store().log("ai-watch", "%s | ok=%s | %s" % (operator, ok, (reply if ok else err)[:200]),
                    operator)
    except Exception:
        pass
    return ok, (reply if ok else err), transport
