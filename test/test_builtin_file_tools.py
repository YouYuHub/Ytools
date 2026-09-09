"""内置文件编辑工具（write_file / edit_file）单元测试。

覆盖：
- 工具定义与注入（include_write_file / include_edit_file）
- write_file：覆盖/追加/自动建目录/参数校验
- edit_file：精确替换/0 匹配提示/多处歧义/replace_all/CRLF 保持/编码写回
- try_execute_builtin_file_tool 分发与错误结构化
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt


class BuiltinFileToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="builtin_file_ut_")
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---------- 注入 ----------

    def test_inject_write_and_edit_file(self):
        tools, servers = bt.inject_builtin_tools(
            [], {}, include_write_file=True, include_edit_file=True
        )
        names = {t["function"]["name"] for t in tools}
        self.assertIn("write_file", names)
        self.assertIn("edit_file", names)
        self.assertEqual(servers["write_file"], "__builtin__")
        self.assertEqual(servers["edit_file"], "__builtin__")
        # 不勾选时不注入
        tools2, servers2 = bt.inject_builtin_tools([], {})
        names2 = {t["function"]["name"] for t in tools2}
        self.assertNotIn("write_file", names2)
        self.assertNotIn("edit_file", names2)

    def test_selectable_names_include_file_tools(self):
        self.assertIn("write_file", bt.SELECTABLE_BUILTIN_TOOL_NAMES)
        self.assertIn("edit_file", bt.SELECTABLE_BUILTIN_TOOL_NAMES)
        self.assertTrue(bt.is_builtin_tool("write_file"))
        self.assertTrue(bt.is_builtin_tool("edit_file"))

    # ---------- write_file ----------

    def test_write_file_creates_and_overwrites(self):
        result = bt.execute_write_file({
            "full_file_name": "sub/dir/a.txt",
            "content": "第一行\n第二行",
        })
        target = Path(self._tmp) / "sub" / "dir" / "a.txt"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "第一行\n第二行")
        self.assertTrue(result["created"])
        self.assertFalse(result["action"] == "append")
        self.assertIn("[write_file]", result["message"])

    def test_write_file_append(self):
        p = Path(self._tmp) / "log.txt"
        p.write_text("hello", encoding="utf-8")
        result = bt.execute_write_file({
            "full_file_name": str(p),
            "content": "-world",
            "append": True,
        })
        self.assertEqual(p.read_text(encoding="utf-8"), "hello-world")
        self.assertEqual(result["action"], "append")
        self.assertFalse(result["created"])

    def test_write_file_rejects_bad_args(self):
        with self.assertRaises(ValueError):
            bt.execute_write_file({"full_file_name": "", "content": "x"})
        with self.assertRaises(ValueError):
            bt.execute_write_file({"full_file_name": "a.txt", "content": 123})
        with self.assertRaises(ValueError):
            bt.execute_write_file({
                "full_file_name": "big.txt",
                "content": "x" * (bt._WRITE_EDIT_MAX_CHARS + 1),
            })

    # ---------- edit_file ----------

    def _make_file(self, name: str, content: str, encoding: str = "utf-8") -> str:
        path = Path(self._tmp) / name
        path.write_text(content, encoding=encoding, newline="")
        return str(path)

    def test_edit_file_single_replacement(self):
        path = self._make_file("code.py", "def foo():\n    return 1\n")
        result = bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "return 1",
            "new_string": "return 42",
        })
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("return 42", text)
        self.assertNotIn("return 1", text)
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["matched_occurrences"], 1)

    def test_edit_file_zero_match_hint(self):
        path = self._make_file("hint.txt", "alpha line\nbeta line\n")
        with self.assertRaises(ValueError) as ctx:
            bt.execute_edit_file({
                "full_file_name": path,
                "old_string": "alpha lines",
                "new_string": "x",
            })
        message = str(ctx.exception)
        self.assertIn("0 处匹配", message)
        self.assertIn("最相似", message)

    def test_edit_file_ambiguous_requires_replace_all(self):
        path = self._make_file("dup.txt", "same\nsame\n")
        with self.assertRaises(ValueError) as ctx:
            bt.execute_edit_file({
                "full_file_name": path,
                "old_string": "same",
                "new_string": "other",
            })
        self.assertIn("歧义", str(ctx.exception))
        result = bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "same",
            "new_string": "other",
            "replace_all": True,
        })
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "other\nother\n")

    def test_edit_file_preserves_crlf(self):
        path = self._make_file("crlf.txt", "line1\r\nline2\r\n")
        bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "line1",
            "new_string": "LINE1",
        })
        raw = Path(path).read_bytes()
        self.assertIn(b"LINE1\r\nline2", raw)

    def test_edit_file_gbk_roundtrip(self):
        path = self._make_file("gbk.txt", "中文内容\n", encoding="gbk")
        result = bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "中文内容",
            "new_string": "改好的中文",
        })
        self.assertEqual(result["encoding"], "gbk")
        self.assertEqual(Path(path).read_text(encoding="gbk"), "改好的中文\n")

    def test_edit_file_delete_via_empty_new_string(self):
        path = self._make_file("del.txt", "keep A\nremove me\nkeep B\n")
        bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "remove me\n",
            "new_string": "",
        })
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "keep A\nkeep B\n")

    def test_try_execute_dispatch_and_error_shape(self):
        # 非目标名称返回 None
        self.assertIsNone(bt.try_execute_builtin_file_tool("todo_write", {}))
        self.assertIsNone(bt.try_execute_builtin_file_tool("list_dir", {}))
        # 正常分发
        ok = bt.try_execute_builtin_file_tool(
            "write_file", {"full_file_name": "d.txt", "content": "ok"}
        )
        self.assertIn("message", ok)
        # 异常转结构化 error
        bad = bt.try_execute_builtin_file_tool(
            "edit_file", {"full_file_name": "missing.txt", "old_string": "a", "new_string": "b"}
        )
        self.assertIn("error", bad)
        self.assertEqual(bad.get("tool"), "edit_file")

    # ---------- read_file ----------

    def test_read_file_lines_with_numbers(self):
        path = self._make_file("demo.txt", "alpha\nbeta\ngamma\n")
        result = bt.execute_read_file({"full_file_name": path})
        self.assertEqual(result["total_lines"], 3)
        self.assertEqual(result["range_start"], 1)
        self.assertEqual(result["range_end"], 3)
        self.assertFalse(result["has_more"])
        self.assertIn("1| alpha", result["content"])
        self.assertIn("[read_file]", result["message"])

    def test_read_file_range_and_no_line_numbers(self):
        path = self._make_file("range.txt", "l1\nl2\nl3\nl4\n")
        result = bt.execute_read_file({
            "full_file_name": path, "start_line": 2, "end_line": 3,
            "show_line_numbers": False,
        })
        self.assertEqual(result["content"], "l2\nl3")
        self.assertTrue(result["has_more"])

    def test_read_file_rejects_missing_and_binary(self):
        with self.assertRaises(FileNotFoundError):
            bt.execute_read_file({"full_file_name": "ghost.txt"})
        binary = Path(self._tmp) / "bin.dat"
        binary.write_bytes(b"\x00\x01\x02" * 100)
        with self.assertRaises(ValueError):
            bt.execute_read_file({"full_file_name": str(binary)})

    def test_read_file_single_long_line_char_chunk(self):
        long_line = "x" * (bt._READ_FILE_MAX_CHARS + 5000)
        path = self._make_file("long.txt", long_line + "\n")
        first = bt.execute_read_file({"full_file_name": path})
        self.assertEqual(first["mode"], "char_chunk")
        self.assertIn("char_offset=", first["message"])
        offset = first["next_char_offset"]
        self.assertTrue(offset)
        second = bt.execute_read_file({
            "full_file_name": path, "char_offset": offset,
        })
        # 第二段读完：无后续偏移
        self.assertIsNone(second["next_char_offset"])

    # ---------- search_files ----------

    def test_search_files_basic_regex(self):
        self._make_file("a.py", "def foo():\n    return TARGET_1\n")
        self._make_file("b.md", "some TARGET_2 text\n")
        Path(self._tmp, "node_modules").mkdir()
        Path(self._tmp, "node_modules", "c.js").write_text("TARGET_3\n", encoding="utf-8")
        result = bt.execute_search_files({"pattern": r"TARGET_\d"})
        self.assertEqual(result["files_matched"], 2)
        self.assertEqual(result["total_matches"], 2)
        self.assertNotIn("node_modules", result["message"])
        self.assertIn("a.py:2", result["message"])
        self.assertIn(">2| ", result["message"])

    def test_search_files_literal_and_case(self):
        self._make_file("case.txt", "Hello World\nhello world\n")
        literal = bt.execute_search_files({
            "pattern": "hello world", "is_regex": False,
        })
        self.assertEqual(literal["files_matched"], 1)  # 仅小写行命中
        insensitive = bt.execute_search_files({
            "pattern": "HELLO WORLD", "is_regex": False, "ignore_case": True,
        })
        self.assertEqual(insensitive["total_matches"], 2)

    def test_search_files_empty_and_bad_regex(self):
        self._make_file("empty_hit.txt", "nothing here\n")
        none = bt.execute_search_files({"pattern": "zzz-not-exist"})
        self.assertIn("（无匹配结果）", none["message"])
        with self.assertRaises(ValueError):
            bt.execute_search_files({"pattern": "("})  # 非法正则
        with self.assertRaises(ValueError):
            bt.execute_search_files({"pattern": ""})

    def test_search_files_default_max_depth_recurses(self):
        # 不传 max_depth 时应按 schema 默认值递归子目录（而非只扫当前目录一层）
        Path(self._tmp, "sub", "deep").mkdir(parents=True)
        Path(self._tmp, "sub", "deep", "nested.txt").write_text(
            "DEEP_TARGET\n", encoding="utf-8")
        result = bt.execute_search_files({"pattern": r"DEEP_TARGET"})
        self.assertEqual(result["files_matched"], 1)

    def test_search_files_is_regex_false_is_literal(self):
        # is_regex=False 必须按字面匹配：正则语义会把 "a.c" 匹配到 "abc"
        self._make_file("lit.txt", "abc x\na.c x\n")
        literal = bt.execute_search_files({
            "pattern": "a.c", "is_regex": False,
        })
        self.assertEqual(literal["total_matches"], 1)   # 仅字面行命中
        regex = bt.execute_search_files({"pattern": "a.c"})
        self.assertEqual(regex["total_matches"], 2)     # 正则两行都命中

    def test_search_files_max_results_cap(self):
        for i in range(5):
            self._make_file(f"m{i}.txt", f"hit{i}\n")
        result = bt.execute_search_files({
            "pattern": r"hit\d", "max_results": 2,
        })
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_matches"], 5)

    def test_builtin_names_and_injection_for_new_tools(self):
        tools, servers = bt.inject_builtin_tools(
            [], {}, include_read_file=True, include_search_files=True
        )
        names = {t["function"]["name"] for t in tools}
        self.assertIn("read_file", names)
        self.assertIn("search_files", names)
        self.assertEqual(servers["read_file"], "__builtin__")
        self.assertEqual(servers["search_files"], "__builtin__")
        self.assertTrue(bt.is_builtin_tool("read_file"))
        self.assertTrue(bt.is_builtin_tool("search_files"))


if __name__ == "__main__":
    unittest.main()
