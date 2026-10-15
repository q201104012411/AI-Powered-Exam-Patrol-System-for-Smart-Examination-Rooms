# -*- coding: utf-8 -*-
"""数据分类分级目录（《考生数据保护.md》第一节）单元测试"""
import unittest

from tests._base import DppTestCase
from dpp import catalog, config


class TestCatalog(DppTestCase):

    def test_categories_match_spec(self):
        """六类数据必须齐全，且与规范表格一一对应。"""
        keys = set(catalog.keys())
        self.assertEqual(
            keys,
            {"face_template", "face_image", "exam_video", "alert_clip",
             "review_record", "enroll_identity"},
            "分类分级类别与规范不一致")

    def test_sensitive_levels(self):
        """敏感级别：人脸模板/人脸图像/考场视频/身份报名为高，告警中高，复核中。"""
        self.assertEqual(catalog.level_of("face_template"), "高")
        self.assertEqual(catalog.level_of("face_image"), "高")
        self.assertEqual(catalog.level_of("exam_video"), "高")
        self.assertEqual(catalog.level_of("enroll_identity"), "高")
        self.assertEqual(catalog.level_of("alert_clip"), "中高")
        self.assertEqual(catalog.level_of("review_record"), "中")

    def test_retention_positive(self):
        """保留期限要么是正数天数，要么为 None（沿用既有规定，不由本系统自动销毁）。"""
        auto_keys = {c.key for c in catalog.auto_destroyable()}
        for k in catalog.keys():
            d = catalog.retention_days(k)
            if d is None:
                self.assertNotIn(k, auto_keys,
                                 "%s 保留期限为 None 却被标记为可自动销毁" % k)
            else:
                self.assertGreater(d, 0, "%s 的保留期限非法：%s" % (k, d))

    def test_high_sensitivity_requires_encryption(self):
        """高敏感类别必须登记"必须加密的字段"。"""
        for k in catalog.keys():
            if catalog.level_of(k) in ("高", "中高"):
                fields = catalog.required_encrypted_fields(k)
                self.assertTrue(fields, "%s 为%s敏感却未登记必须加密字段"
                                % (k, catalog.level_of(k)))

    def test_auto_destroy_flags(self):
        """人脸模板/图像、告警片段可自动销毁；考场视频、复核记录、身份报名不可。"""
        auto_keys = {c.key for c in catalog.auto_destroyable()}
        self.assertIn("face_template", auto_keys)
        self.assertIn("face_image", auto_keys)
        self.assertIn("alert_clip", auto_keys)
        self.assertNotIn("exam_video", auto_keys,
                         "考场视频涉及考务规定，不应自动销毁")
        self.assertNotIn("enroll_identity", auto_keys,
                         "身份报名信息沿用既有考务系统规定，不应自动销毁")

    def test_validate_self_consistent(self):
        """目录自洽性校验必须通过（缺字段/坏期限都要被判出）。"""
        ok, problems = catalog.validate()
        self.assertTrue(ok, "目录自洽性校验未通过：%s" % problems)
        self.assertEqual(problems, [])

    def test_table_markdown_has_all_categories(self):
        md = catalog.table_markdown()
        for k in catalog.keys():
            self.assertIn(catalog.get(k).name, md)

    def test_config_retention_matches_catalog(self):
        """配置里的图像保留天数不得超过目录承诺的 30 天上限。"""
        self.assertLessEqual(config.CAPTURE["retention_image_days"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
