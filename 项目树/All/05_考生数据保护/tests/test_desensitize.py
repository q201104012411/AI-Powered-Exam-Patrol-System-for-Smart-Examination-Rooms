# -*- coding: utf-8 -*-
"""去标识化展示 · 日志脱敏 · 动态水印（第三节第 2、3 条）单元测试"""
import os
import unittest

from tests._base import DppTestCase
from dpp import config, desensitize


class TestMasking(DppTestCase):

    def test_mask_id_card_keeps_head_tail(self):
        out = desensitize.mask_id_card("330100200801011234")
        self.assertEqual(out, "3301**********1234")
        self.assertNotIn("2008010112", out, "中间出生日期段必须被抹掉")

    def test_mask_id_card_short_input(self):
        """短串（异常长度）整体打码，避免"保留前后"导致反而全泄露。"""
        self.assertEqual(desensitize.mask_id_card("12345678"), "*" * 8)

    def test_mask_id_card_empty(self):
        self.assertEqual(desensitize.mask_id_card(""), "")
        self.assertEqual(desensitize.mask_id_card(None), "")

    def test_mask_name_two_and_three(self):
        self.assertEqual(desensitize.mask_name("张三"), "张*")
        self.assertEqual(desensitize.mask_name("欧阳修"), "欧*修")
        self.assertEqual(desensitize.mask_name("欧"), "欧")

    def test_mask_ticket_and_seat(self):
        self.assertEqual(desensitize.mask_ticket_no("2026T0001"), "202****01")
        self.assertEqual(desensitize.mask_seat_no("01"), "**")

    def test_deidentify_has_no_direct_identifier(self):
        """默认视图不得出现姓名/证件号明文。"""
        s = desensitize.deidentify({"ref": "CD123", "room_id": "A-01",
                                    "seat_no": "01", "name": "张三",
                                    "id_card_no": "330100200801011234"})
        self.assertIn("CD123", s)
        self.assertNotIn("张三", s)
        self.assertNotIn("330100200801011234", s)

    def test_reveal_text_still_masks(self):
        """即使授权查看，界面渲染仍打码（防偷窥），明文只走单独的 reveal 通道。"""
        s = desensitize.reveal_text({
            "name": "张三", "ref": "CD1", "room_id": "A-01", "seat_no": "01",
            "id_card_no": "330100200801011234", "ticket_no": "2026T0001"})
        self.assertNotIn("330100200801011234", s)
        self.assertIn("3301**********1234", s)


class TestLogSanitize(DppTestCase):

    def test_mask_18_digit_id(self):
        out = desensitize.sanitize_for_log("证件 330100200801011234")
        self.assertNotIn("330100200801011234", out)
        self.assertIn("3301**********1234", out)

    def test_mask_18_digit_adjacent_to_chinese(self):
        """汉字紧跟号码时也必须被抹掉（不能用 \\b，汉字无单词边界）。"""
        out = desensitize.sanitize_for_log("证件330100200801011234")
        self.assertNotIn("330100200801011234", out)

    def test_mask_15_digit_id(self):
        out = desensitize.sanitize_for_log("老证件 330100200801011")
        self.assertNotIn("330100200801011", out)

    def test_mask_phone_adjacent_to_chinese(self):
        out = desensitize.sanitize_for_log("电话13800138000")
        self.assertNotIn("13800138000", out)
        self.assertIn("138****8000", out)

    def test_mask_id_with_x_checkdigit(self):
        out = desensitize.sanitize_for_log("33010020080101123X")
        self.assertNotIn("33010020080101123", out)

    def test_normal_text_untouched(self):
        """普通文本不能被误伤。"""
        s = "考生 A-01 座次 07 于 12:30 触发告警"
        self.assertEqual(desensitize.sanitize_for_log(s), s)

    def test_none_safe(self):
        self.assertEqual(desensitize.sanitize_for_log(None), "")


class TestWatermark(DppTestCase):

    def test_watermark_text_has_three_parts(self):
        t = desensitize.watermark_text("oper1", "巡考终端-R7",
                                       ts="2026-01-01 09:00:00")
        self.assertIn("oper1", t)
        self.assertIn("2026-01-01 09:00:00", t)
        self.assertIn("巡考终端-R7", t)

    def test_watermark_never_lies_about_pil(self):
        """没有 Pillow 时必须如实标注 applied=False，不许假装打过水印。"""
        img, info = desensitize.watermark_image(None, "oper1")
        self.assertFalse(info["applied"])
        if not desensitize.HAS_PIL:
            self.assertFalse(info["has_pil"])

    def test_watermark_applied_when_pil_available(self):
        if not desensitize.HAS_PIL:
            self.skipTest("本机未安装 Pillow，跳过落图用例")
        from PIL import Image
        img = Image.new("RGB", (120, 60), (255, 255, 255))
        out, info = desensitize.watermark_image(img, "oper1", "R7",
                                                ts="2026-01-01 09:00:00")
        self.assertTrue(info["applied"])
        self.assertTrue(info["has_pil"])
        self.assertIsNotNone(out)
        self.assertEqual(out.size, img.size)

    def test_watermark_info_shape(self):
        info = desensitize.watermark_info()
        self.assertIn("has_pil", info)
        self.assertIn("format", info)


if __name__ == "__main__":
    unittest.main(verbosity=2)
