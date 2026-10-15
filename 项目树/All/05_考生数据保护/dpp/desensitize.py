# -*- coding: utf-8 -*-
r"""
去标识化展示与动态水印（《考生数据保护.md》第三节第 2、3 条）

1. 去标识化展示：默认以编号/座次展示；人脸图像与明文证件号需授权解密查看，
   且查看行为全程留痕（留痕动作由 storage/logs 负责，本模块只做"能不能看"）。
2. 动态水印：视频回看与截图自动叠加"操作人 + 时间 + 终端"水印，防外泄溯源。
   无图像处理库（PIL）时**不假装打过水印**：返回水印文本并如实标注"未落图"。
"""
import re
import time

from dpp import config

try:
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    HAS_PIL = True
except Exception:
    HAS_PIL = False


# ---------------------------------------------------------------- 掩码
def mask_id_card(id_card):
    """证件号掩码：330100199001011234 -> 3301**********1234"""
    if not id_card:
        return ""
    s = str(id_card).strip()
    if len(s) <= config.MASK_KEEP_HEAD + config.MASK_KEEP_TAIL:
        return "*" * len(s)
    return (s[:config.MASK_KEEP_HEAD]
            + "*" * (len(s) - config.MASK_KEEP_HEAD - config.MASK_KEEP_TAIL)
            + s[-config.MASK_KEEP_TAIL:])


def mask_ticket_no(ticket_no):
    """准考证号掩码：保留前 3 位与后 2 位。"""
    if not ticket_no:
        return ""
    s = str(ticket_no)
    if len(s) <= 5:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def mask_seat_no(seat_no):
    """座位号掩码：只保留最后 2 位。"""
    if not seat_no:
        return ""
    s = str(seat_no)
    if len(s) <= 2:
        return "*" * len(s)
    return "*" * (len(s) - 2) + s[-2:]


def mask_name(name):
    """姓名掩码：张三 -> 张*；欧阳修 -> 欧*修"""
    if not name:
        return ""
    s = str(name).strip()
    if len(s) == 1:
        return s
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_generic(text, keep_head=4, keep_tail=4):
    """通用脱敏（日志防意外落明文用）。"""
    if not text:
        return ""
    s = str(text)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]


def deidentify(candidate):
    """
    把一条考生记录渲染成**去标识化**展示串：只给编号与座次，不给姓名与证件明文。
    这是巡考客户端默认视图（权限最小化）。
    """
    if candidate is None:
        return "—"
    ref = candidate.get("ref") or "—"
    room = candidate.get("room_id") or "—"
    seat = candidate.get("seat_no") or "—"
    return "考生编号 %s ｜ 考场 %s ｜ 座次 %s" % (ref, room, mask_seat_no(seat))


def reveal_text(candidate, session=None):
    """
    授权解密后的完整展示（含姓名与证件明文的掩码态）。
    注意：这里**仍然打码**——完整明文只在 reveal 动作且留痕后由业务层单独提供；
    本函数用于"已授权但界面仍要防偷窥"的场景。
    """
    if candidate is None:
        return "—"
    return "%s（%s）｜考场 %s ｜座次 %s ｜证件 %s ｜准考证 %s" % (
        candidate.get("name") or "—",
        candidate.get("ref") or "—",
        candidate.get("room_id") or "—",
        candidate.get("seat_no") or "—",
        mask_id_card(candidate.get("id_card_no")),
        mask_ticket_no(candidate.get("ticket_no")),
    )


# ---------------------------------------------------------------- 日志脱敏
# 注意：不能用 \b ——中文与数字相邻时没有单词边界（"电话138..."），
# 会漏掉紧跟在汉字后面的号码。改用"非数字/非长串"前后视断言。
_LEFT = r"(?<!\d)"
_DIGIT18 = re.compile(_LEFT + r"\d{17}[\dXx](?!\d)")
_DIGIT15 = re.compile(_LEFT + r"\d{15}(?!\d)")
_PHONE = re.compile(_LEFT + r"1[3-9]\d{9}(?!\d)")
# 18 位身份证优先于 15 位；手机号最后处理，避免被 18 位规则的部分匹配吃掉
_LONG_DIGITS = re.compile(_LEFT + r"\d{15,18}(?!\d)")


def sanitize_for_log(text):
    """写日志前调用：抹掉疑似证件号与手机号，防止日志意外落明文。

    顺序很关键：先处理 18 位/15 位长串，再处理手机号，
    避免"18 位串里恰好包含 11 位手机号"被二次打码成怪样子。
    """
    if text is None:
        return ""
    s = str(text)

    def _mask_long(m):
        g = m.group(0)
        if len(g) == 18:
            return mask_id_card(g)
        return mask_generic(g)

    s = _LONG_DIGITS.sub(_mask_long, s)
    s = _PHONE.sub(lambda m: mask_generic(m.group(0), 3, 4), s)
    return s


# ---------------------------------------------------------------- 动态水印
def watermark_text(operator, terminal="巡考终端", ts=None):
    """生成三联水印文本：操作人 + 时间 + 终端"""
    return config.WATERMARK_FMT.format(
        operator=operator or "未知操作人",
        ts=ts or time.strftime("%Y-%m-%d %H:%M:%S"),
        terminal=terminal or "未知终端")


def watermark_image(img, operator, terminal="巡考终端", ts=None):
    """
    在 PIL 图像上平铺水印。
    返回 (image, info)：info 里如实标注是否真的落到了图上（has_pil / applied）。
    没有 PIL 时返回原图 + applied=False，绝不谎报"已加水印"。
    """
    text = watermark_text(operator, terminal, ts)
    if not HAS_PIL or img is None:
        return img, {"applied": False, "has_pil": HAS_PIL, "text": text,
                     "reason": "缺少 Pillow，未把水印落到图像上（仅返回水印文本）"}
    from PIL import Image, ImageDraw, ImageFont
    if not isinstance(img, Image.Image):
        img = Image.fromarray(img)
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    try:
        font = ImageFont.truetype("msyh.ttc", 20)
    except Exception:
        font = ImageFont.load_default()
    step_x, step_y = config.WATERMARK_TILE_STEP
    alpha = int(255 * config.WATERMARK_TILE_ALPHA)
    w, h = base.size
    for y in range(0, h + step_y, step_y):
        for x in range(0, w + step_x, step_x):
            draw.text((x, y), text, font=font, fill=(255, 0, 0, alpha))
    out = Image.alpha_composite(base, layer).convert("RGB")
    return out, {"applied": True, "has_pil": True, "text": text, "reason": ""}


def watermark_info():
    return {"has_pil": HAS_PIL, "format": config.WATERMARK_FMT,
            "alpha": config.WATERMARK_TILE_ALPHA, "step": list(config.WATERMARK_TILE_STEP)}


def describe():
    return {"默认脱敏": config.MASK_DEFAULT, "Pillow": HAS_PIL,
            "水印格式": config.WATERMARK_FMT,
            "水印可落图": HAS_PIL}
