# coding: utf-8
"""文件历史版本链 REST 接口测试（面板级批量操作回归）。

背景：keep_all / revert_all 初版误复用 KeepRequest（key 必填），
前端只发 {session_id} 导致 422 Unprocessable Content、面板报
「全部保留失败：[object Object]」。本套件锁定：
- body 只含 session_id（与前端 API.fileKeepAll/fileRevertAll 一致）→ 200；
- 空体 {}（session_id 走默认值）→ 200；
- 批量语义端到端：keep_all 封版后 Total 归零；revert_all 磁盘恢复基线。
运行：python -m unittest test.test_file_history_router -v
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import file_history as store
from routers import file_history_router

_SESSION = "pytest-history-router"


class FileHistoryRouterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig_root = store.HISTORY_DIFF_ROOT
        store.HISTORY_DIFF_ROOT = self._tmp
        self.app = FastAPI()
        self.app.include_router(file_history_router.api_file_history_router)
        self.client = TestClient(self.app)
        # 造两个文件的未决变更（磁盘内容由工具写入模拟）
        self.target = self._tmp / "proj" / "main.py"
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text("a = 10\nb = 2\nc = 3\n", encoding="utf-8", newline="")
        store.record_change(
            _SESSION, path=str(self.target), display_path=str(self.target),
            old_text="a = 1\nb = 2\nc = 3\n", new_text="a = 10\nb = 2\nc = 3\n",
            tool="edit_file", round_number=1,
        )
        self.target2 = self._tmp / "proj" / "util.py"
        self.target2.write_text("x = 2\ny = 3\n", encoding="utf-8", newline="")
        store.record_change(
            _SESSION, path=str(self.target2), display_path=str(self.target2),
            old_text="x = 1\n", new_text="x = 2\ny = 3\n",
            tool="edit_file", round_number=1,
        )

    def tearDown(self):
        store.HISTORY_DIFF_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_keep_all_accepts_session_only_body(self):
        """回归锁定：前端只发 {session_id}，不得因缺 key 字段 422。"""
        resp = self.client.post(
            "/file_diff/keep_all", json={"session_id": _SESSION}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["kept_count"], 2)
        self.assertEqual(data["skipped"], [])
        # 默认 cleanup=True：封版后留档目录一并清理
        self.assertEqual(data["cleaned_count"], 2)
        listing = self.client.get(
            f"/file_diff/list?session_id={_SESSION}&hide_clean=false"
        ).json()
        self.assertEqual(listing["stats"]["total"], 0)
        self.assertEqual(listing["files"], [])  # 留档已清，index 不再有条目

    def test_revert_all_accepts_session_only_body(self):
        resp = self.client.post(
            "/file_diff/revert_all", json={"session_id": _SESSION}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["reverted_count"], 2)
        # 磁盘写回基线
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), "a = 1\nb = 2\nc = 3\n"
        )
        self.assertEqual(self.target2.read_text(encoding="utf-8"), "x = 1\n")

    def test_revert_all_removes_new_file_on_disk(self):
        """新建文件（基线为空）批量撤回：磁盘文件应被删除而非留 0 字节。"""
        new_file = self._tmp / "proj" / "created_by_tool.txt"
        new_file.write_text("hello\n", encoding="utf-8", newline="")
        store.record_change(
            _SESSION, path=str(new_file), display_path=str(new_file),
            old_text="", new_text="hello\n",
            tool="write_file", round_number=1,
        )
        resp = self.client.post(
            "/file_diff/revert_all", json={"session_id": _SESSION}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["reverted_count"], 3)
        by_path = {item["path"]: item for item in data["reverted"]}
        self.assertTrue(by_path[str(new_file)]["disk_removed"])
        self.assertFalse(new_file.exists())  # 不留空文件
        self.assertFalse(by_path[str(self.target)]["disk_removed"])

    def test_batch_endpoints_default_session_on_empty_body(self):
        """空体 {}：session_id 走默认值，不应 422（422 时 detail 为数组）。"""
        resp = self.client.post("/file_diff/keep_all", json={})
        self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
