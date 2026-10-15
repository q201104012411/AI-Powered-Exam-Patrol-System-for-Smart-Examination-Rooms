# -*- coding: utf-8 -*-
"""测试共用脚手架：临时数据目录 + 已建库的 Store + 常用会话。"""
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dpp import access, config, governance, lifecycle, models, rights, storage  # noqa: E402


class DppTestCase(unittest.TestCase):
    """所有用例的基类：每个用例一个独立临时数据目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dpp_test_")
        self._old_home = os.environ.get("EXAM_DPP_HOME")
        os.environ["EXAM_DPP_HOME"] = self.tmp
        # 让 config 重新解析（路径是模块级常量，这里直接覆盖实例属性）
        self._saved = {
            "DB_PATH": config.DB_PATH,
            "KEYRING_DIR": config.KEYRING_DIR,
            "EXPORT_DIR": config.EXPORT_DIR,
            "REPORT_DIR": config.REPORT_DIR,
            "VAULT_DIR": config.VAULT_DIR,
            "DATA_ROOT": getattr(config, "DATA_ROOT", None),
        }
        config.DB_PATH = os.path.join(self.tmp, "dpp.db")
        config.KEYRING_DIR = os.path.join(self.tmp, "keyring")
        config.EXPORT_DIR = os.path.join(self.tmp, "exports")
        config.REPORT_DIR = os.path.join(self.tmp, "reports")
        config.VAULT_DIR = os.path.join(self.tmp, "vault")
        config.DATA_ROOT = self.tmp
        for d in (config.KEYRING_DIR, config.EXPORT_DIR, config.REPORT_DIR,
                  config.VAULT_DIR):
            os.makedirs(d, exist_ok=True)

        from dpp import crypto
        crypto.reset_cache()

        self.store = storage.Store(config.DB_PATH)
        self.accounts = access.AccountStore(self.store)
        self.accounts.ensure_seed()
        # 内置账户没有只读角色：按需补一个 viewer 账户，便于测试"只读"边界
        self.accounts.create("viewer1", "viewer1", config.ROLE_VIEWER,
                             display_name="只读查看", by_role=config.ROLE_OWNER)
        self.life = lifecycle.Lifecycle(self.store)
        self.gov = governance.Governance(self.store)
        self.rights = rights.RightsService(self.store, lifecycle=self.life,
                                           accounts=self.accounts)

    def tearDown(self):
        from dpp import crypto
        crypto.reset_cache()
        if self._old_home is None:
            os.environ.pop("EXAM_DPP_HOME", None)
        else:
            os.environ["EXAM_DPP_HOME"] = self._old_home
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------ 便捷方法
    def login(self, username="owner", password=None, confirm_risky=False, **kw):
        """默认以"已登记设备/地点"的正常会话登录（非高风险）。

        需要测试高风险会话时传 place=""、device=""（或 hour=2），
        并用 confirm_risky=True 走二次确认。
        """
        if password is None:
            builtin = dict((r[0], r[1]) for r in config.DEFAULT_ACCOUNTS)
            # 非内置账户（如测试补建的 viewer1）口令默认与用户名相同
            password = builtin.get(username, username)
        kw.setdefault("place", "考点考务办")
        kw.setdefault("device", "DEV-TEST-01")
        session = self.accounts.authenticate(username, password, **kw)
        if confirm_risky:
            session.confirm_risky()
        return session

    def make_consent(self, name="测试考生", scope="face", save=True,
                     cand_ref=""):
        cs = models.Consent(cand_ref=cand_ref, name=name, scope=scope,
                            method="考前告知书签署（电子）", operator="owner",
                            evidence_path="consent/test.pdf")
        if save:
            self.store.save_consent(cs)
        return cs

    def collect_one(self, name="测试考生", id_card_no="330100200801011234",
                    ticket_no="2026T0001", seat_no="01", room_id="A-01",
                    site="一中考点", with_image=False, retention_days=30,
                    scope="face"):
        """完整走一遍合规采集：先定 cand_ref -> 写同意（直接绑定）-> 一次性 collect。

        注意：本系统加密数据只读，不能"先落占位考生再回填"，故先构造 ref。
        """
        owner = self.login("owner")
        cand_ref = models.new_id("C")
        cs = self.make_consent(name, scope=scope, save=False, cand_ref=cand_ref)
        self.store.save_consent(cs)
        img = b"\x00" * (48 * 48 * 3) if with_image else None
        res = self.life.collect(
            owner, name=name, id_card_no=id_card_no, ticket_no=ticket_no,
            seat_no=seat_no, room_id=room_id, site=site,
            feature_vector="feature-%s" % cand_ref,
            face_image_bytes=img, consent_id=cs.consent_id,
            retention_days=retention_days, cand_ref=cand_ref)
        return res
