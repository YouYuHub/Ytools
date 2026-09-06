"""Skills 提示词库接口与管理逻辑测试：

- 建目录 / 播种示例、列表 / 读取 / 新建 / 保存 / 重命名 / 删除全链路；
- 文件名安全：路径逃逸（../、绝对路径、a/b）、非法字符、空名、超长名 → 400；
- 语义冲突：同名新建 / 重命名到已存在 → 409；读写删除不存在的文件 → 404；
- 内容大小上限 → 400；.md 后缀自动补齐。
运行：python -m unittest test.test_prompt_router -v
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prompt import prompt_manager
from routers import prompt_router


class PromptRouterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig_dir = prompt_manager.MD_FILES_DIR
        prompt_manager.MD_FILES_DIR = self._tmp
        # 预置一个占位文件，避免被「空目录自动播种示例提示词」干扰计数
        (self._tmp / "占位.md").write_text("placeholder", encoding="utf-8")
        app = FastAPI()
        app.include_router(prompt_router.api_prompt_router)
        self.client = TestClient(app)

    def tearDown(self):
        prompt_manager.MD_FILES_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---------- 全链路 ----------

    def test_full_lifecycle(self):
        # 新建（自动补 .md）
        resp = self.client.post("/prompts/create", json={"name": "代码评审", "content": "# 评审提示词\n"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "代码评审.md")
        # 同名新建 → 409
        resp = self.client.post("/prompts/create", json={"name": "代码评审.md"})
        self.assertEqual(resp.status_code, 409)
        # 列表：占位 + 新建的两个中，新建的最新排最前
        data = self.client.get("/prompts/list").json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["prompts"][0]["name"], "代码评审.md")
        self.assertIn("updated_at", data["prompts"][0])
        # 读取
        data = self.client.get("/prompts/read", params={"name": "代码评审"}).json()
        self.assertEqual(data["content"], "# 评审提示词\n")
        # 保存覆盖
        resp = self.client.post("/prompts/save", json={"name": "代码评审", "content": "新内容"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.get("/prompts/read", params={"name": "代码评审"}).json()["content"], "新内容"
        )
        # 重命名
        resp = self.client.post("/prompts/rename", json={"old_name": "代码评审", "new_name": "review"})
        self.assertEqual(resp.json()["name"], "review.md")
        self.assertFalse((self._tmp / "代码评审.md").exists())
        # 重命名到已存在 → 409
        self.client.post("/prompts/create", json={"name": "another"})
        resp = self.client.post("/prompts/rename", json={"old_name": "review", "new_name": "another"})
        self.assertEqual(resp.status_code, 409)
        # 删除
        resp = self.client.post("/prompts/delete", json={"name": "review.md"})
        self.assertEqual(resp.json()["state"], "succeed")
        remaining = [item["name"] for item in self.client.get("/prompts/list").json()["prompts"]]
        self.assertNotIn("review.md", remaining)

    def test_missing_files_return_404(self):
        self.assertEqual(self.client.get("/prompts/read", params={"name": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/prompts/delete", json={"name": "nope"}).status_code, 404)
        self.assertEqual(
            self.client.post("/prompts/rename", json={"old_name": "nope", "new_name": "x"}).status_code, 404
        )

    def test_unsafe_or_invalid_names_rejected(self):
        for bad in ["../escape", r"sub\dir", "a/b", "..", ".", "", "a" * 200, "含<非法>字符", "con"]:
            resp = self.client.post("/prompts/create", json={"name": bad})
            self.assertEqual(resp.status_code, 400, f"名称 {bad!r} 应被拒绝")
            resp = self.client.get("/prompts/read", params={"name": bad})
            self.assertEqual(resp.status_code, 400, f"名称 {bad!r} 应被拒绝")

    def test_content_size_limit(self):
        resp = self.client.post("/prompts/save", json={"name": "big", "content": "字" * (512 * 1024)})
        self.assertEqual(resp.status_code, 400)

    def test_read_rejects_directory_escape_even_when_file_exists_outside(self):
        # 在 md_files 目录外放一个同名文件，确保逃逸路径读不到它
        outside = self._tmp.parent / "outside_secret.md"
        outside.write_text("secret", encoding="utf-8")
        try:
            for name in ["../outside_secret.md", "..\\outside_secret.md"]:
                resp = self.client.get("/prompts/read", params={"name": name})
                self.assertIn(resp.status_code, (400, 404))
        finally:
            outside.unlink(missing_ok=True)


class PromptManagerUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig_dir = prompt_manager.MD_FILES_DIR
        prompt_manager.MD_FILES_DIR = self._tmp / "md_files"

    def tearDown(self):
        prompt_manager.MD_FILES_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_ensure_prompt_dir_seeds_sample_once(self):
        prompt_manager.ensure_prompt_dir()
        sample = self._tmp / "md_files" / "示例提示词.md"
        self.assertTrue(sample.is_file())
        before = sample.read_text(encoding="utf-8")
        sample.write_text("用户改过的内容", encoding="utf-8")
        prompt_manager.ensure_prompt_dir()
        # 已有 .md 文件时不再播种覆盖
        self.assertEqual(sample.read_text(encoding="utf-8"), "用户改过的内容")
        self.assertIn("示例", before)

    def test_normalize_name_appends_md_suffix(self):
        prompt_manager.ensure_prompt_dir()
        prompt_manager.create_prompt("tips")
        self.assertTrue((self._tmp / "md_files" / "tips.md").is_file())


if __name__ == "__main__":
    unittest.main()
