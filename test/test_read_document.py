# -*- coding: utf-8 -*-
"""read_document 内置工具 + 文件清单（方案二：清单 + 按需读取）单元测试。

覆盖：
- 文件清单/索引构建（小文件内联、大文件节选 + read_document 提示、总预算收缩）；
- execute_read_document 执行（分页、越界、未找到、中文文件名）；
- read_document 工具注入与识别（inject_builtin_tools / is_builtin_tool / check_tool_exists）；
- token_stats 计入文件清单块（file_memory_tokens 字段与 request_context_tokens 口径）。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory import file_memory
from memory.file_memory import (
    FILE_MEMORY_TOTAL_MAX_CHARS,
    build_file_index_text,
    build_file_manifest_text,
    count_session_file_memory,
    find_file_memory_record,
    get_file_memory_manager,
)
from factory.agent_runtime import builtin_tools
from factory.agent_runtime.builtin_tools import (
    execute_builtin_tool,
    execute_read_document,
    inject_builtin_tools,
    is_builtin_tool,
)


class FileManifestBuildTests(unittest.TestCase):
    """清单/索引纯函数：预算与提示语义。"""

    def test_empty_records_returns_empty(self):
        self.assertEqual(build_file_manifest_text([]), "")
        self.assertEqual(build_file_index_text([]), "")

    def test_small_file_inlined_full_content(self):
        content = "短文件内容" * 50  # 250 字 < 4000 内联上限
        records = [{"filename": "note.txt", "type": "txt", "size": 300, "content": content}]
        text = build_file_manifest_text(records, read_document_available=True)
        self.assertIn("【文件 1】note.txt", text)
        self.assertIn(content, text)
        # 小文件块内不出现「已节选开头」与按文件名的读取提示（intro 的通用说明除外）
        self.assertNotIn("已节选开头", text)
        self.assertNotIn('read_document(filename="note.txt")', text)

    def test_large_file_excerpt_with_read_document_hint(self):
        content = "B" * 12000
        records = [{"filename": "big.pdf", "type": "pdf", "size": 90000, "content": content}]
        text = build_file_manifest_text(records, read_document_available=True)
        self.assertIn("big.pdf", text)
        self.assertIn("共约 12000 字", text)
        self.assertIn('read_document(filename="big.pdf")', text)
        self.assertIn("B" * 2000, text)
        self.assertNotIn("B" * 2001, text)

    def test_read_document_hint_hidden_when_unavailable(self):
        records = [{"filename": "big.pdf", "type": "pdf", "size": 90000, "content": "B" * 12000}]
        text = build_file_manifest_text(records, read_document_available=False)
        self.assertNotIn("read_document", text)
        self.assertIn("节选", text)

    def test_total_budget_trims_and_notes_omission(self):
        records = [
            {"filename": f"f{i}.txt", "type": "txt", "size": 9999, "content": "X" * 12000}
            for i in range(10)
        ]
        text = build_file_manifest_text(records)
        self.assertLessEqual(len(text), FILE_MEMORY_TOTAL_MAX_CHARS + 300)
        self.assertIn("省略", text)

    def test_index_lists_names_only(self):
        records = [{"filename": "a.pdf", "type": "pdf", "size": 2048, "content": "Y" * 9000}]
        text = build_file_index_text(records)
        self.assertIn("a.pdf", text)
        self.assertIn("索引", text)
        self.assertNotIn("Y" * 10, text)

    def test_newest_first_order(self):
        records = [
            {"filename": "old.txt", "type": "txt", "size": 10, "content": "旧"},
            {"filename": "new.txt", "type": "txt", "size": 10, "content": "新"},
        ]
        text = build_file_manifest_text(records)
        self.assertLess(text.index("【文件 1】old.txt"), text.index("【文件 2】new.txt"))


class ReadDocumentExecutionTests(unittest.TestCase):
    """execute_read_document：真实记录文件读写（临时 HISTORY_ROOT 隔离）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = file_memory.HISTORY_ROOT
        file_memory.HISTORY_ROOT = Path(self._tmp)
        self.sid = "read_doc_test"

    def tearDown(self):
        file_memory.HISTORY_ROOT = self._orig_root
        try:
            asyncio.run(file_memory.cleanup_file_memory_manager(self.sid))
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _add_file(self, filename, content, ftype="txt", size=None):
        manager = asyncio.run(get_file_memory_manager(self.sid))
        manager.add_file_memory({
            "filename": filename,
            "type": ftype,
            "content": content,
            "size": size if size is not None else len(content),
        })

    def test_execute_paginates(self):
        content = "".join(str(i % 10) for i in range(1000))
        self._add_file("doc.txt", content)
        result = execute_read_document(
            {"filename": "doc.txt", "start_char": 100, "max_chars": 50}, self.sid
        )
        self.assertEqual(result["content"], content[100:150])
        self.assertEqual(result["total_chars"], 1000)
        self.assertEqual(result["start_char"], 100)
        self.assertEqual(result["end_char"], 150)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_start_char"], 150)

    def test_execute_reads_to_end(self):
        self._add_file("doc.txt", "abcdef")
        result = execute_read_document({"filename": "doc.txt", "start_char": 3}, self.sid)
        self.assertEqual(result["content"], "def")
        self.assertFalse(result["has_more"])
        self.assertNotIn("next_start_char", result)

    def test_max_chars_clamped(self):
        self._add_file("big.txt", "Z" * 30000)
        result = execute_read_document(
            {"filename": "big.txt", "max_chars": 10 ** 9}, self.sid
        )
        self.assertEqual(len(result["content"]), 20000)

    def test_missing_filename_error(self):
        result = execute_read_document({}, self.sid)
        self.assertIn("error", result)

    def test_not_found_lists_available_files(self):
        self._add_file("exists.txt", "hello")
        result = execute_read_document({"filename": "ghost.txt"}, self.sid)
        self.assertIn("error", result)
        self.assertIn("exists.txt", result["error"])

    def test_start_beyond_length_error(self):
        self._add_file("doc.txt", "abc")
        result = execute_read_document({"filename": "doc.txt", "start_char": 99}, self.sid)
        self.assertIn("error", result)

    def test_chinese_filename_matched_by_original_name(self):
        self._add_file("报告.pdf", "PDF 内容", ftype="pdf")
        result = execute_read_document({"filename": "报告.pdf"}, self.sid)
        self.assertEqual(result["content"], "PDF 内容")
        record = find_file_memory_record(self.sid, "报告.pdf")
        self.assertIsNotNone(record)
        self.assertEqual(record["content"], "PDF 内容")

    def test_count_and_find_record(self):
        self._add_file("alpha.txt", "aaa")
        self._add_file("beta.csv", "bbb", ftype="csv")
        self.assertEqual(count_session_file_memory(self.sid), 2)
        record = find_file_memory_record(self.sid, "beta.csv")
        self.assertIsNotNone(record)
        self.assertEqual(record["content"], "bbb")


class ReadDocumentInjectionTests(unittest.TestCase):
    """工具注入/识别/check_tool_exists 联动。"""

    def test_inject_adds_definition(self):
        tools, servers = inject_builtin_tools([], {}, include_read_document=True)
        names = [t.get("function", {}).get("name") for t in tools]
        self.assertIn("read_document", names)
        self.assertEqual(
            servers.get("read_document"), builtin_tools.BUILTIN_TOOL_SERVER_KEY
        )

    def test_inject_idempotent(self):
        tools, servers = inject_builtin_tools([], {}, include_read_document=True)
        tools2, servers2 = inject_builtin_tools(
            tools, servers, include_read_document=True
        )
        names = [t.get("function", {}).get("name") for t in tools2]
        self.assertEqual(names.count("read_document"), 1)

    def test_is_builtin_tool(self):
        self.assertTrue(is_builtin_tool("read_document"))

    def test_check_tool_exists_knows_read_document(self):
        result = execute_builtin_tool(
            "check_tool_exists",
            {"tool_name": "read_document"},
            set(),
            {},
            enabled_tool_names={"read_document"},
        )
        self.assertTrue(result["exists"])
        self.assertFalse(result["disabled"])

    def test_read_document_definition_shape(self):
        definition = builtin_tools.READ_DOCUMENT_TOOL_DEFINITION
        function = definition["function"]
        self.assertEqual(function["name"] == "read_document", True)
        self.assertIn("filename", function["parameters"]["properties"])
        self.assertIn("start_char", function["parameters"]["properties"])
        self.assertIn("max_chars", function["parameters"]["properties"])


class FileMemoryStatsTests(unittest.IsolatedAsyncioTestCase):
    """token_stats 计入文件清单块：file_memory_tokens 与 request_context_tokens 口径。"""

    async def asyncSetUp(self):
        import memory.chat_memory as chat_memory

        self._tmp_chat = tempfile.mkdtemp()
        self._tmp_files = tempfile.mkdtemp()
        self._orig_chat_root = chat_memory.HISTORY_ROOT
        self._orig_file_root = file_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp_chat)
        file_memory.HISTORY_ROOT = Path(self._tmp_files)
        self.sid = "file_stats_test"
        self._chat_memory = chat_memory

    async def asyncTearDown(self):
        self._chat_memory.HISTORY_ROOT = self._orig_chat_root
        file_memory.HISTORY_ROOT = self._orig_file_root
        try:
            await self._chat_memory.cleanup_chat_memory_manager(self.sid)
        except Exception:
            pass
        try:
            await file_memory.cleanup_file_memory_manager(self.sid)
        except Exception:
            pass
        shutil.rmtree(self._tmp_chat, ignore_errors=True)
        shutil.rmtree(self._tmp_files, ignore_errors=True)

    async def test_stats_include_file_manifest(self):
        file_manager = await get_file_memory_manager(self.sid)
        file_manager.add_file_memory({
            "filename": "doc.txt",
            "type": "txt",
            "content": "内容段落" * 100,
            "size": 400,
        })
        manager = self._chat_memory.ChatMemoryManager(self.sid)
        stats = await manager.get_context_token_stats()
        self.assertGreater(stats["file_memory_tokens"], 0)
        self.assertEqual(
            stats["request_context_tokens"],
            stats["messages_tokens"]
            + stats["system_prompt_tokens"]
            + stats["tool_definition_tokens"]
            + stats["file_memory_tokens"],
        )

    async def test_stats_zero_without_files(self):
        manager = self._chat_memory.ChatMemoryManager(self.sid)
        stats = await manager.get_context_token_stats()
        self.assertEqual(stats["file_memory_tokens"], 0)
        self.assertEqual(
            stats["request_context_tokens"],
            stats["messages_tokens"]
            + stats["system_prompt_tokens"]
            + stats["tool_definition_tokens"],
        )


if __name__ == "__main__":
    unittest.main()
