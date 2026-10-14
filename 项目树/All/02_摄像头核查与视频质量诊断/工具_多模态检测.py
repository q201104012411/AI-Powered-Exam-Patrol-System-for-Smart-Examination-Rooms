# -*- coding: utf-8 -*-
r"""
多模态大模型（VLM）检测 · 联调工具

用途：不起界面就能把"多模态看图判读"这一环单独跑通 —— 换模型、试 API、
      看模型到底圈出了什么（框对不对、行为对不对、有没有把手机认出来）。

用法：
  # 1) 看配置与连通性（默认动作，不联网也能跑）
  python 工具_多模态检测.py --check

  # 2) 本机拉一个视觉模型（走 ollama pull，需联网，几 GB）
  python 工具_多模态检测.py --pull                 # 默认 qwen2.5vl:3b
  python 工具_多模态检测.py --pull minicpm-v

  # 3) 配好之后写进 data\ai_settings.json，界面里立刻生效（改设置会重建检测器）
  python 工具_多模态检测.py --provider ollama --model qwen2.5vl:3b --write-settings

  # 4) 用一张图验证（输出 JSON + 画框图，落 data\reports\）
  python 工具_多模态检测.py --image data\captures\考场1.jpg
  python 工具_多模态检测.py --image x.jpg --raw          # 连模型原文一起打印

  # 5) 临时试一次别的后端（不写设置文件）
  python 工具_多模态检测.py --provider api --model qwen-vl-max \
         --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
         --api-key sk-xxx --image x.jpg

  # 6) 只想看提示词（模型不守约时，先看提示词有没有说清楚）
  python 工具_多模态检测.py --prompt

说明：本工具只用标准库 + OpenCV（与项目其它工具一致），不引入新依赖。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cv2                                  # noqa: E402
import numpy as np                          # noqa: E402

import vlm_detect as vlm                    # noqa: E402

PULL_DEFAULT = "qwen2.5vl:3b"
PULL_SUGGEST = ("qwen2.5vl:3b（推荐，约 3~4GB）", "minicpm-v（约 5GB）",
                "llava:7b（较老但小）", "qwen2.5vl:7b（更准更慢）")

# 常见 OpenAI 兼容多模态接口（base_url 直接填进 vlm_base_url；模型名填 vlm_model）
API_PRESETS = (
    "通义千问 VL：https://dashscope.aliyuncs.com/compatible-mode/v1  模型 qwen-vl-max / qwen-vl-plus",
    "智谱 GLM-4V：https://open.bigmodel.cn/api/paas/v4               模型 glm-4v-flash / glm-4v-plus",
    "OpenAI：https://api.openai.com/v1                               模型 gpt-4o / gpt-4o-mini",
    "本机 Ollama（OpenAI 兼容）：http://127.0.0.1:11434/v1           模型 qwen2.5vl:3b（需先 ollama pull）",
    "自建 vLLM / LM Studio：http://<主机>:<端口>/v1                  模型按服务端实际名字填",
)


# ---------------------------------------------------------------------
#  设置文件（与界面共用同一个 data\ai_settings.json）
# ---------------------------------------------------------------------
def settings_path():
    """定位 AI 设置文件：优先各项目 config 里的定义，其次 data\ai_settings.json"""
    try:
        import config
        p = getattr(config, "AI_SETTINGS_PATH", None)
        if p is None:
            root = getattr(config, "DATA_ROOT", None)
            p = (Path(root) / "ai_settings.json") if root is not None else None
        if p is not None:
            return Path(p)
    except Exception:
        pass
    return HERE / "data" / "ai_settings.json"


def load_settings():
    p = settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(merged):
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = load_settings()
        cur.update(merged)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print("[设置] 写入失败：%s" % e)
        return False


def report_dir():
    try:
        import config
        d = getattr(config, "REPORT_DIR", None)
        if d:
            return Path(d)
    except Exception:
        pass
    return HERE / "data" / "reports"


# ---------------------------------------------------------------------
#  配置解析
# ---------------------------------------------------------------------
def build_config(args):
    """命令行 > 设置文件（环境变量在 VlmConfig 里自动叠加）"""
    s = load_settings()
    provider = args.provider or s.get("vlm_provider", "off")
    model = args.model or s.get("vlm_model", "")
    base_url = args.base_url or s.get("vlm_base_url", "")
    api_key = args.api_key or s.get("vlm_api_key", "")
    interval = args.interval if args.interval is not None else s.get("vlm_interval", 3.0)
    return vlm.VlmConfig(provider=provider, model=model, base_url=base_url,
                         api_key=api_key, interval=interval)


def print_config(cfg):
    print("设置文件：%s" % settings_path())
    print("后端    ：%s" % vlm.PROVIDER_LABELS.get(cfg.provider, cfg.provider))
    print("模型    ：%s" % (cfg.model or "（未填）"))
    print("接口地址：%s" % (cfg.base_url or "（未填）"))
    print("API Key ：%s" % ("已填（%d 字符）" % len(cfg.api_key) if cfg.api_key else "（未填）"))
    print("间隔    ：%.1f 秒/路   送模型长边：%d   超时：%.0f 秒"
          % (cfg.interval, cfg.max_dim, cfg.timeout))
    print("可用性  ：%s" % ("可用" if cfg.enabled else "不可用（%s）" % cfg.missing_reason()))


# ---------------------------------------------------------------------
#  子命令
# ---------------------------------------------------------------------
def cmd_check(args):
    cfg = build_config(args)
    print("=" * 78)
    print("多模态（VLM）检测 · 配置与连通性")
    print("=" * 78)
    print_config(cfg)
    print()
    if not cfg.enabled:
        print("提示：多模态没配好时，界面里仍然照常跑 YOLO11n（不会瞎、不会崩）。")
        if cfg.provider in (vlm.PROVIDER_OFF, vlm.PROVIDER_OLLAMA):
            print("      本机 Ollama 想快速起步：")
            print("        python 工具_多模态检测.py --pull")
            print("        python 工具_多模态检测.py --provider ollama --model %s --write-settings"
                  % PULL_DEFAULT)
        if cfg.provider in (vlm.PROVIDER_OFF, vlm.PROVIDER_API):
            print("      云端 API 想用起来：填 base_url + api_key + 模型名（下面任选一家）")
            for item in API_PRESETS:
                print("        - %s" % item)
            print("        例：python 工具_多模态检测.py --provider api --model qwen-vl-max \\")
            print("               --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \\")
            print("               --api-key sk-xxx --write-settings")
        return 0
    engine = vlm.VlmEngine(cfg)
    ok, msg = engine.probe(force=True)
    print("连通性  ：%s —— %s" % ("通过" if ok else "不通过", msg))
    print()
    if cfg.provider == vlm.PROVIDER_API:
        print("已按 OpenAI 兼容协议调用：POST %s/chat/completions（Bearer 鉴权，图片走 data URI）"
              % cfg.base_url.rstrip("/"))
        print()
    if cfg.provider == vlm.PROVIDER_OLLAMA:
        try:
            import urllib.request
            with urllib.request.urlopen(cfg.base_url.rstrip("/") + "/api/tags",
                                        timeout=5) as r:
                names = [m.get("name", "") for m in
                         (json.loads(r.read().decode("utf-8", "replace"))
                          .get("models") or [])]
            print("本机已有模型：")
            for n in names:
                print("  - %s" % n)
        except Exception as e:
            print("（读模型列表失败：%s）" % e)
        if not ok and "没有模型" in msg:
            print()
            print("建议先拉一个能看图的模型（任选，需联网）：")
            for s in PULL_SUGGEST:
                print("  python 工具_多模态检测.py --pull %s" % s.split("（")[0])
    print()
    print("下一步：")
    print("  python 工具_多模态检测.py --image <一张考场图>     # 看框画得对不对")
    print("  界面里：AI 设置 → 多模态大模型 → 测试连接")
    return 0 if ok else 1


def cmd_pull(args):
    model = args.pull if isinstance(args.pull, str) and args.pull else PULL_DEFAULT
    exe = "ollama"
    print("=" * 78)
    print("拉取视觉模型：%s" % model)
    print("=" * 78)
    print("说明：多模态必须能「看图」。纯文本模型（如 Qwen3-4B）会拉下来但用不了。")
    print("      下载体积通常 3~6GB，走 ollama 官方源；中断了重跑本命令即可续传。")
    print()
    try:
        rc = subprocess.call([exe, "pull", model])
    except FileNotFoundError:
        print("[失败] 找不到 ollama 命令：请先安装 Ollama（项目里 大模型本地部署\\OllmaSetup.exe）")
        return 2
    except Exception as e:
        print("[失败] %s" % e)
        return 2
    if rc != 0:
        print("[失败] ollama pull 返回 %d" % rc)
        return rc
    print()
    print("已拉取 %s。接着写入设置并验证：" % model)
    print("  python 工具_多模态检测.py --provider ollama --model %s --write-settings" % model)
    print("  python 工具_多模态检测.py --check")
    return 0


def cmd_prompt(args):
    print("=" * 78)
    print("系统提示词")
    print("=" * 78)
    print(vlm.SYSTEM_PROMPT)
    print("=" * 78)
    print("用户提示词")
    print("=" * 78)
    print(vlm.USER_PROMPT)
    print()
    print("（提示词在 vlm_detect.py 顶部的 SYSTEM_PROMPT / USER_PROMPT 里，可直接改）")
    return 0


def _label(img, text, x, y, color, font_size=13):
    """画标签：优先用 person_detect 的位图渲染（PIL，支持中文），退化到 cv2。

    cv2.putText 用的是 Hershey 字体，画中文会变成 "??????" —— 所以这里复用
    项目自己的 draw_label（人框标签用的同一套），保证"转脸偏头/手机"能看清。
    """
    try:
        import person_detect as pd
        if hasattr(pd, "draw_label"):
            pd.draw_label(img, text, color, int(x), max(0, int(y) - 18), font_size)
            return
    except Exception:
        pass
    cv2.putText(img, text, (int(x), max(14, int(y) - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)


def _draw(frame, persons, objects, note):
    """把模型结果画出来，人工核对框准不准"""
    out = frame.copy()
    for p in persons:
        x, y, w, h = p["rect"]
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 200, 0), 2)
        _label(out, "%s %.2f" % (p["action"], p.get("confidence", 0.0)),
               x, y, (0, 200, 0))
    for o in objects:
        x, y, w, h = o["rect"]
        color = (0, 0, 255) if o.get("phone") else (0, 165, 255)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        _label(out, "%s %.2f" % (o["label"], o.get("confidence", 0.0)),
               x, y, color)
    cv2.putText(out, note, (8, out.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def cmd_image(args):
    cfg = build_config(args)
    path = Path(args.image)
    if not path.is_absolute():
        path = (Path.cwd() / path)
    data = np.fromfile(str(path), dtype=np.uint8)      # 兼容中文路径
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        print("[失败] 读不到图片：%s" % path)
        return 2
    print("=" * 78)
    print("多模态判读：%s  %dx%d" % (path.name, frame.shape[1], frame.shape[0]))
    print("=" * 78)
    print_config(cfg)
    print()
    if not cfg.enabled:
        print("[不可用] %s" % cfg.missing_reason())
        print("先在设置里配好（或用 --provider/--model/--base-url/--api-key 临时指定）。")
        return 2
    client = vlm.make_client(cfg)
    ok, msg = vlm.VlmEngine(cfg).probe(force=True)
    print("连通性  ：%s —— %s" % ("通过" if ok else "不通过", msg))
    print()
    try:
        persons, objects, text, dt = client.analyze_parsed(frame)
    except vlm.VlmError as e:
        print("[调用失败] %s" % e)
        return 3
    print("耗时    ：%.2f 秒" % dt)
    print("人数    ：%d" % len(persons))
    for p in persons:
        print("   · %-10s conf=%.2f  box=%s%s"
              % (p["action"], p["confidence"], p["rect"],
                 "  ← 原文：%s" % p["raw_action"] if p["raw_action"] != p["action"] else ""))
    print("可疑物品：%d" % len(objects))
    for o in objects:
        print("   · %-8s conf=%.2f  box=%s" % (o["label"], o["confidence"], o["rect"]))
    payload, err = vlm.extract_json(text)
    summary = (payload or {}).get("summary", "") if payload else ""
    if summary:
        print("画面描述：%s" % summary)
    if args.raw:
        print("-" * 78)
        print("模型原文：")
        print(text)
    out_dir = report_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_path = out_dir / ("多模态检测_%s.jpg" % stamp)
        note = "%s | %s | %.2fs | %d persons, %d objects" % (
            cfg.provider, cfg.model, dt, len(persons), len(objects))
        cv2.imencode(".jpg", _draw(frame, persons, objects, note))[1].tofile(str(img_path))
        print()
        print("画框图已写入：%s" % img_path)
        json_path = out_dir / ("多模态检测_%s.json" % stamp)
        json_path.write_text(json.dumps(
            {"image": str(path), "provider": cfg.provider, "model": cfg.model,
             "seconds": dt, "persons": persons, "objects": objects,
             "summary": summary, "raw": text}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("原始结果已写入：%s" % json_path)
    except Exception as e:
        print("（写报告失败，不影响上面的结果：%s）" % e)
    return 0


def cmd_write_settings(args):
    merged = {}
    if args.provider:
        merged["vlm_provider"] = vlm._norm_provider(args.provider)
    if args.model:
        merged["vlm_model"] = args.model
    if args.base_url:
        merged["vlm_base_url"] = args.base_url
    if args.api_key:
        merged["vlm_api_key"] = args.api_key
    if args.interval is not None:
        merged["vlm_interval"] = args.interval
    if not merged:
        print("[跳过] 没有要给设置文件写入的值（用 --provider/--model/--base-url/--api-key/--interval）")
        return 1
    if save_settings(merged):
        print("已写入 %s：" % settings_path())
        for k, v in merged.items():
            print("  %s = %s" % (k, v))
        print()
        print("界面里下次重建检测器即生效（改完 AI 设置点保存、或重启界面）。")
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(
        description="多模态大模型（VLM）检测联调工具：查配置、拉模型、写设置、验图片")
    ap.add_argument("--check", action="store_true", help="看配置与连通性（默认）")
    ap.add_argument("--pull", nargs="?", const=PULL_DEFAULT, default=None,
                    metavar="MODEL", help="ollama pull 一个视觉模型（默认 %s）" % PULL_DEFAULT)
    ap.add_argument("--prompt", action="store_true", help="打印发给模型的提示词")
    ap.add_argument("--image", help="用一张图片验证（输出 JSON + 画框图）")
    ap.add_argument("--raw", action="store_true", help="连模型原文一起打印")
    ap.add_argument("--write-settings", dest="write_settings", action="store_true",
                    help="把下面这些参数写进 data\\ai_settings.json")
    ap.add_argument("--provider", help="off / api / ollama")
    ap.add_argument("--model", help="模型名，如 qwen2.5vl:3b 或 qwen-vl-max")
    ap.add_argument("--base-url", dest="base_url", help="接口地址（留空=按后端默认）")
    ap.add_argument("--api-key", dest="api_key", help="API Key（仅 api 后端需要）")
    ap.add_argument("--interval", type=float, default=None, help="调用间隔（秒）")
    args = ap.parse_args()

    if args.pull:
        return cmd_pull(args)
    if args.prompt:
        return cmd_prompt(args)
    if args.write_settings:
        return cmd_write_settings(args)
    if args.image:
        return cmd_image(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
