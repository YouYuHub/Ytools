"""访客用户配置接口测试（GET/POST /user/profile）。

覆盖：
- 默认值读取（.env 无 USER_NAME → 「访客用户」，persisted=False）；
- 保存后落盘 .env、重复保存不追加重复键、内存 env_vars 同步；
- 清空（空白串）→ 回退默认名且键位保留为空；
- 校验：超长 / 控制字符 → 400，且失败不影响既有值；
- .env 语法冲突（# 注释、${} 变量引用等会被解析改写的输入）→ 400 且回滚旧值；
- 写入含引号/中文/emoji 的合法名（引号由 _format_env_value 自动加引号承载）。

运行：python -m unittest test.test_user_profile_router -v
"""
import importlib
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from env_manager import init_path, load_var


class UserProfileRouterTests(unittest.TestCase):
    def setUp(self):
        from config import PROJECT_ROOT as original_root

        self._repo_root = original_root
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        (self._tmp / "setting").mkdir(parents=True, exist_ok=True)
        (self._tmp / "setting" / "models.json").write_text("{}", encoding="utf-8")
        # 预置一个同前缀键，验证只替换目标键、不误伤其它行
        (self._tmp / ".env").write_text(
            "# 注释行\nDEFAULT_CHAT_WORK_DIR=\"C:/work\"\nUSER_NAME_EXTRA=keep\n",
            encoding="utf-8",
        )
        init_path(self._tmp)

        from routers import user_profile_router
        self.router = importlib.reload(user_profile_router)

        app = FastAPI()
        app.include_router(self.router.api_user_profile_router)
        self.client = TestClient(app)

    def tearDown(self):
        init_path(self._repo_root)
        import config as _config
        _config.PROJECT_ROOT = self._repo_root
        from routers import user_profile_router
        importlib.reload(user_profile_router)
        self._tmp_dir.cleanup()

    # ---------- 辅助 ----------

    def _env_text(self) -> str:
        return (self._tmp / ".env").read_text(encoding="utf-8")

    # ---------- 读取 ----------

    def test_default_when_env_missing(self):
        data = self.client.get("/user/profile").json()
        self.assertEqual(data["state"], "succeed")
        self.assertEqual(data["name"], config.DEFAULT_USER_NAME)
        self.assertFalse(data["persisted"])
        self.assertEqual(data["max_length"], config.MAX_USER_NAME_LENGTH)
        self.assertEqual(data["env_name"], "USER_NAME")
        self.assertIn(".env", data["env_file"])

    def test_get_ignores_invalid_stored_value(self):
        # 直接往 .env 写入超长值（模拟手工编辑）：读取侧回退默认名而不是抛错
        text = self._env_text().replace("USER_NAME_EXTRA=keep", "")
        (self._tmp / ".env").write_text(
            text + "USER_NAME=" + "x" * (config.MAX_USER_NAME_LENGTH + 5) + "\n",
            encoding="utf-8",
        )
        init_path(self._tmp)
        data = self.client.get("/user/profile").json()
        self.assertEqual(data["name"], config.DEFAULT_USER_NAME)
        self.assertFalse(data["persisted"])

    # ---------- 保存 ----------

    def test_save_persists_to_env_and_memory(self):
        resp = self.client.post("/user/profile", json={"name": "  张三  "})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "张三")
        self.assertTrue(data["persisted"])
        self.assertIn("张三", data["message"])
        # 落盘 + 内存同步
        self.assertIn('USER_NAME="张三"', self._env_text())
        self.assertEqual(load_var("USER_NAME"), "张三")
        # 其它键未被破坏
        self.assertIn("USER_NAME_EXTRA=keep", self._env_text())
        self.assertIn('DEFAULT_CHAT_WORK_DIR="C:/work"', self._env_text())
        # 重新读取返回新值
        self.assertEqual(self.client.get("/user/profile").json()["name"], "张三")

    def test_repeat_save_replaces_in_place(self):
        self.client.post("/user/profile", json={"name": "甲"})
        self.client.post("/user/profile", json={"name": "乙"})
        text = self._env_text()
        self.assertEqual(text.count("USER_NAME="), 1)
        self.assertIn('USER_NAME="乙"', text)
        self.assertEqual(load_var("USER_NAME"), "乙")

    def test_clear_restores_default(self):
        self.client.post("/user/profile", json={"name": "临时名"})
        resp = self.client.post("/user/profile", json={"name": "   "})
        data = resp.json()
        self.assertEqual(data["name"], config.DEFAULT_USER_NAME)
        self.assertFalse(data["persisted"])
        self.assertIn(config.DEFAULT_USER_NAME, data["message"])
        self.assertEqual(load_var("USER_NAME") or "", "")

    def test_emoji_and_spaces_survive_roundtrip(self):
        # emoji / 连续空格属 .env 可承载内容（_format_env_value 整体加引号后原样读回）
        for name in ["😀小助手", "a b  c", "访客-002"]:
            resp = self.client.post("/user/profile", json={"name": name})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["name"], name)
            self.assertEqual(self.client.get("/user/profile").json()["name"], name)

    def test_reject_quote_mangled_names(self):
        # .env 读取侧用 strip('"')/strip("'") 去引号，带引号或首尾成对引号的名字
        # 必然被改写，因此保存时读回校验直接拒绝（含单引号包裹与不成对引号）
        self.client.post("/user/profile", json={"name": "原值"})
        for bad in ['名字带"引号"', "'包裹'", 'only"one']:
            resp = self.client.post("/user/profile", json={"name": bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn(".env", resp.json()["detail"])
            self.assertEqual(load_var("USER_NAME"), "原值")

    # ---------- 校验 ----------

    def test_reject_too_long(self):
        resp = self.client.post(
            "/user/profile", json={"name": "名" * (config.MAX_USER_NAME_LENGTH + 1)}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("最长", resp.json()["detail"])

    def test_accept_exact_limit(self):
        exact = "名" * config.MAX_USER_NAME_LENGTH
        resp = self.client.post("/user/profile", json={"name": exact})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], exact)

    def test_reject_control_characters(self):
        for bad in ("a\nb", "a\tb", "a\rb"):
            resp = self.client.post("/user/profile", json={"name": bad})
            self.assertEqual(resp.status_code, 400, bad)

    def test_reject_env_syntax_conflict_and_rollback(self):
        self.client.post("/user/profile", json={"name": "原值"})
        # # 注释截断、已定义变量引用（${}）被展开 —— 都会让保存值变形，必须拒绝并回滚
        for bad in ("甲#乙", "  ${DEFAULT_CHAT_WORK_DIR}  "):
            resp = self.client.post("/user/profile", json={"name": bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn(".env", resp.json()["detail"])
            # 旧值必须保持不变（回滚生效）
            self.assertEqual(load_var("USER_NAME"), "原值")

    def test_undefined_variable_reference_kept_literal(self):
        # 未定义的 ${VAR} 不会被展开（读取侧保留原样），属于可保存内容
        resp = self.client.post("/user/profile", json={"name": "${NO_SUCH_KEY}"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["name"], "${NO_SUCH_KEY}")
        self.assertEqual(self.client.get("/user/profile").json()["name"], "${NO_SUCH_KEY}")

    def test_missing_field_is_422(self):
        self.assertEqual(self.client.post("/user/profile", json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
