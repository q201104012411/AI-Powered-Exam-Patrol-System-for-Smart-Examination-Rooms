# -*- coding: utf-8 -*-
"""
网页操作面板集成测试（需要 flask/bcrypt/pyjwt）。

为避免影响真实数据，本测试在临时数据库上运行，并**先复制一份真实库**，
从而顺带验证"旧库明文身份证号自动迁移"这条路径。

运行：python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import sqlite3
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "网页操作面板", "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)          # 网页端模块（app/auth/db）所在目录

try:
    import flask  # noqa: F401
    HAS_FLASK = True
except Exception:
    HAS_FLASK = False

_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP, exist_ok=True)

import config  # noqa: E402

REAL_DB = str(config.DB_PATH)
TEST_DB = os.path.join(_TMP, "web_%s.db" % uuid.uuid4().hex[:8])

# 让 store / db / app 都指向临时库
from pathlib import Path  # noqa: E402
config.DB_PATH = Path(TEST_DB)

if HAS_FLASK:
    if os.path.exists(REAL_DB):
        shutil.copyfile(REAL_DB, TEST_DB)          # 复制真实库，顺带验证迁移
    import db as webdb                             # noqa: E402
    webdb.DB_PATH = TEST_DB
    webdb.INIT_PWD_FILE = os.path.join(_TMP, "初始密码_%s.txt" % uuid.uuid4().hex[:6])
    import app as webapp                           # noqa: E402
    import accounts as accounts_mod                # noqa: E402
    # 权限标记文件重定向到临时目录，避免污染真实数据目录
    accounts_mod.MANUAL_ADD_FLAG = os.path.join(_TMP, "manual_add.on")
    accounts_mod.AUTO_VERIFY_ADMIN_FLAG = os.path.join(_TMP, "auto_verify_admin.on")
    # 两个新的所有者开关同样重定向，并复位为出厂默认（大考模式关 / 严格模式开）
    accounts_mod.MAJOR_EXAM_FLAG = os.path.join(_TMP, "major_exam.on")
    accounts_mod.STRICT_MODE_FLAG = os.path.join(_TMP, "strict_mode.on")
    for _flag in (accounts_mod.MAJOR_EXAM_FLAG, accounts_mod.STRICT_MODE_FLAG):
        try:
            if os.path.exists(_flag):
                os.remove(_flag)
        except OSError:
            pass
else:
    webdb = None
    webapp = None
    accounts_mod = None


@unittest.skipUnless(HAS_FLASK, "需要 flask/bcrypt/pyjwt（网页操作面板依赖）")
class TestWebPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 重置管理员密码，拿到一个已知口令（避免依赖历史遗留口令）
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("DELETE FROM users")
            conn.commit()
        finally:
            conn.close()
        info = webdb.init_db()
        # 多数用例需要"手动添加考生"处于开启状态（另有专门用例验证默认关闭）
        with open(accounts_mod.MANUAL_ADD_FLAG, "w", encoding="utf-8") as f:
            f.write("1\n")
        with open(webdb.INIT_PWD_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        cls.pwd = [ln.split("：", 1)[1].strip() for ln in text.splitlines()
                   if ln.startswith("初始密码")][0]
        cls.client = webapp.app.test_client()
        cls.token = None

    @classmethod
    def tearDownClass(cls):
        for p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm", webdb.INIT_PWD_FILE,
                  accounts_mod.MANUAL_ADD_FLAG, accounts_mod.AUTO_VERIFY_ADMIN_FLAG,
                  accounts_mod.MAJOR_EXAM_FLAG, accounts_mod.STRICT_MODE_FLAG):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ---------- 工具 ----------
    def _login(self, pwd=None):
        r = self.client.post("/api/login", json={"username": "admin",
                                                 "password": pwd or self.pwd})
        return r

    def _auth(self, method, path, **kw):
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return getattr(self.client, method)(path, headers=headers, **kw)

    # ---------- 用例 ----------
    def test_01_login_and_me(self):
        r = self._login()
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertTrue(data["token"])
        self.assertTrue(data["user"]["must_change_password"])
        type(self).token = data["token"]

        r = self._auth("get", "/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["username"], "admin")

    def test_02_wrong_password_rejected_and_logged(self):
        r = self._login("WrongPass123")
        self.assertEqual(r.status_code, 401)
        conn = sqlite3.connect(TEST_DB)
        try:
            n = conn.execute("SELECT COUNT(*) FROM login_log WHERE ok=0").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(n, 1)

    def test_03_requires_token(self):
        r = self.client.get("/api/candidates")
        self.assertEqual(r.status_code, 401)

    def test_04_candidates_are_masked(self):
        r = self._auth("get", "/api/candidates")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()
        if not rows:
            # 临时库可能来自空库：先登记一条，再校验脱敏
            self._auth("post", "/api/candidates", json={"candidates": [
                {"candidate_id": "T-WEB-00", "name": "脱敏测试",
                 "id_card": "330100199001011234", "ticket_no": "T9000",
                 "seat_no": "Z-00", "reference_photo": ""}]})
            rows = self._auth("get", "/api/candidates").get_json()
        self.assertTrue(rows, "登记后应能读到考生数据")
        for row in rows:
            self.assertNotIn("id_card", row, "接口不得下发身份证号明文字段")
            self.assertIn("id_card_masked", row)
            if row["id_card_masked"]:
                self.assertIn("*", row["id_card_masked"])
        raw = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("330100199001011234", raw)

    def test_05_upsert_with_plaintext_and_keep_semantics(self):
        cid = "T-WEB-01"
        payload = {"candidates": [{"candidate_id": cid, "name": "网页测试",
                                   "id_card": "11010519491231002X",
                                   "ticket_no": "T9001", "seat_no": "Z-01",
                                   "reference_photo": "", "consent_status": "已同意"}]}
        r = self._auth("post", "/api/candidates", json=payload)
        self.assertEqual(r.status_code, 200, r.data)

        rows = {x["candidate_id"]: x for x in self._auth("get", "/api/candidates").get_json()}
        self.assertEqual(rows[cid]["id_card_masked"], "1101**********002X")
        self.assertEqual(rows[cid]["consent_status"], "已同意")

        # 再保存一次但不提供身份证号：应保留原登记
        payload["candidates"][0]["ticket_no"] = "T9002"
        payload["candidates"][0]["id_card"] = ""
        r = self._auth("post", "/api/candidates", json=payload)
        self.assertEqual(r.status_code, 200, r.data)
        rows = {x["candidate_id"]: x for x in self._auth("get", "/api/candidates").get_json()}
        self.assertEqual(rows[cid]["id_card_masked"], "1101**********002X")
        self.assertEqual(rows[cid]["ticket_no"], "T9002")

    def test_06_invalid_id_card_rejected_and_rolled_back(self):
        cid = "T-WEB-02"
        r = self._auth("post", "/api/candidates", json={"candidates": [
            {"candidate_id": cid, "name": "非法号码", "id_card": "123"}]})
        self.assertEqual(r.status_code, 400)
        rows = {x["candidate_id"] for x in self._auth("get", "/api/candidates").get_json()}
        self.assertNotIn(cid, rows)

    def test_07_consent_endpoint(self):
        r = self._auth("post", "/api/candidates/T-WEB-01/consent",
                       json={"consent_status": "已拒绝"})
        self.assertEqual(r.status_code, 200)
        rows = {x["candidate_id"]: x for x in self._auth("get", "/api/candidates").get_json()}
        self.assertEqual(rows["T-WEB-01"]["consent_status"], "已拒绝")
        r = self._auth("post", "/api/candidates/T-WEB-01/consent",
                       json={"consent_status": "随便填"})
        self.assertEqual(r.status_code, 400)

    def test_08_admin_cannot_escalate(self):
        """账户管理只归所有者：管理员连"看用户列表"都不允许"""
        r = self._auth("get", "/api/users")
        self.assertEqual(r.status_code, 403, r.data)

        # 管理员也不能新建账户（含教师），更不能自删
        r = self._auth("post", "/api/users", json={"username": "x1",
                                                   "password": "Admin@2026",
                                                   "role": "teacher"})
        self.assertEqual(r.status_code, 403, r.data)

    def test_09_change_password_deletes_init_file(self):
        # 注意：/api/users 现已只归所有者，这里改用 /api/me 判断是否需要改密
        me = self._auth("get", "/api/me").get_json()
        if not me.get("must_change_password"):
            self.skipTest("口令已修改过")
        r = self._auth("post", "/api/change-password",
                       json={"old_password": self.pwd, "new_password": "Admin@2026x"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(os.path.exists(webdb.INIT_PWD_FILE),
                         "改密后应删除初始密码文件")
        type(self).token = self._login("Admin@2026x").get_json()["token"]

    def test_10_status_endpoint_reports_compliance(self):
        r = self._auth("get", "/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("审计链", data)
        self.assertIn("未登记单独同意", data)
        self.assertIn("特征索引", data)

    def test_11_no_plaintext_left_in_db_file(self):
        """迁移后数据库文件里不应残留明文身份证号"""
        self.assertTrue(os.path.exists(TEST_DB))
        with open(TEST_DB, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"330100199001011234", raw)
        wal = TEST_DB + "-wal"
        if os.path.exists(wal):
            with open(wal, "rb") as f:
                self.assertNotIn(b"330100199001011234", f.read())

    def test_12_jwt_secret_not_hardcoded(self):
        import auth as webauth
        self.assertNotIn(webauth.SECRET_KEY,
                         ("", "change-me-in-production-9f8a7b6c5d4e3f2a1b", "secret"))
        self.assertGreaterEqual(len(webauth.SECRET_KEY), 32)

    def test_13_manual_add_requires_owner_switch(self):
        """手动添加考生默认关闭：所有者没开时保存应被拒；开启后可用"""
        flag = accounts_mod.MANUAL_ADD_FLAG
        try:
            if os.path.exists(flag):
                os.remove(flag)                       # 模拟默认关闭
            r = self._auth("post", "/api/candidates",
                           json={"candidates": [{"candidate_id": "M001",
                                                 "name": "手动测试"}]})
            self.assertEqual(r.status_code, 403, r.data)
            self.assertIn("已关闭", r.get_json().get("error", ""))
        finally:
            with open(flag, "w", encoding="utf-8") as f:   # 恢复开启
                f.write("1\n")
        r = self._auth("post", "/api/candidates",
                       json={"candidates": [{"candidate_id": "M001", "name": "手动测试"}]})
        self.assertEqual(r.status_code, 200, r.data)

    def test_14_upload_roster_only_owner(self):
        """名单导入（文件）仅所有者；管理员调用应 403"""
        import io
        csv = ("考生编号,姓名,身份证号\nR001,名单甲,330100199001011234\n").encode("utf-8")
        # 当前登录用户是 admin（管理员）→ 应被拒
        r = self._auth("post", "/api/roster-import",
                       data={"file": (io.BytesIO(csv), "r.csv")},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403, r.data)

    # ---------- 所有者开关（大考模式 / 严格模式） ----------
    def test_15_login_reports_modes_without_forcing_password_change(self):
        """/api/login 仍返回 must_change_password（供前端提示），并附带两个开关的状态"""
        r = self._login()
        if r.status_code == 401:
            # test_09 已改过口令；用例之间不应依赖执行顺序，这里补一次
            r = self._login("Admin@2026x")
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertIn("must_change_password", data["user"])
        modes = data.get("modes") or {}
        self.assertIn("major_exam", modes)
        self.assertIn("strict_mode", modes)
        self.assertIn("identity_title", modes)
        # 默认：大考模式关（三要素）、严格模式开
        self.assertFalse(modes["major_exam"])
        self.assertTrue(modes["strict_mode"])
        self.assertTrue(modes["identity_title"].startswith("三要素"))

    def test_16_owner_settings_readable_by_any_role(self):
        """模式状态任何登录用户都可读（前端要据此显示正确文案）"""
        r = self._auth("get", "/api/owner-settings")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("major_exam", r.get_json())

    def test_17_owner_settings_write_is_owner_only(self):
        """写开关仅 owner：管理员 403，且不得改动任何标记文件"""
        r = self._auth("post", "/api/owner-settings", json={"major_exam": True})
        self.assertEqual(r.status_code, 403, r.data)
        self.assertFalse(os.path.exists(accounts_mod.MAJOR_EXAM_FLAG),
                         "管理员被拒后不应写出标记文件")

    def test_18_owner_can_toggle_both_switches(self):
        """所有者可改两个开关：标记文件写入/删除 + 文案随之变化"""
        import auth as webauth
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("DELETE FROM users WHERE username='owner'")
            conn.execute(
                "INSERT INTO users (username, password_hash, role, display_name, "
                "must_change_password, created_at) VALUES (?,?,?,?,1,?)",
                ("owner", webauth.hash_password("Owner@2026x"), "owner", "系统所有者",
                 "2026-01-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()
        r = self.client.post("/api/login", json={"username": "owner",
                                                "password": "Owner@2026x"})
        self.assertEqual(r.status_code, 200, r.data)
        owner_token = r.get_json()["token"]

        def as_owner(method, path, **kw):
            headers = kw.pop("headers", {})
            headers["Authorization"] = "Bearer " + owner_token
            return getattr(self.client, method)(path, headers=headers, **kw)

        # 打开大考模式 -> 标记文件出现，文案变成四要素
        r = as_owner("post", "/api/owner-settings", json={"major_exam": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(os.path.exists(accounts_mod.MAJOR_EXAM_FLAG))
        self.assertTrue(r.get_json()["major_exam"])
        self.assertTrue(r.get_json()["identity_title"].startswith("四要素"))

        # 关闭严格模式 -> 标记文件出现，strict_mode 变 False
        r = as_owner("post", "/api/owner-settings", json={"strict_mode": False})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(os.path.exists(accounts_mod.STRICT_MODE_FLAG))
        self.assertFalse(r.get_json()["strict_mode"])

        # 复原默认：大考模式关 / 严格模式开
        r = as_owner("post", "/api/owner-settings",
                     json={"major_exam": False, "strict_mode": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(os.path.exists(accounts_mod.MAJOR_EXAM_FLAG))
        self.assertFalse(os.path.exists(accounts_mod.STRICT_MODE_FLAG))

        # 空请求应被拒
        r = as_owner("post", "/api/owner-settings", json={})
        self.assertEqual(r.status_code, 400, r.data)

    def test_19_status_reports_owner_switches(self):
        r = self._auth("get", "/api/status")
        self.assertEqual(r.status_code, 200)
        modes = r.get_json().get("所有者开关") or {}
        self.assertIn("major_exam", modes)
        self.assertIn("strict_mode", modes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
