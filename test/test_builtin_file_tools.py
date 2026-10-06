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
from unittest.mock import patch

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

    def test_file_tool_schemas_and_descriptions_are_aligned(self):
        definitions = {
            definition["function"]["name"]: definition["function"]
            for definition in (
                bt.WRITE_FILE_TOOL_DEFINITION,
                bt.EDIT_FILE_TOOL_DEFINITION,
                bt.READ_FILE_TOOL_DEFINITION,
                bt.SEARCH_FILES_TOOL_DEFINITION,
            )
        }

        write = definitions["write_file"]
        self.assertIn("mode=overwrite", write["description"])
        self.assertIn("edit_file", write["description"])
        self.assertEqual(write["parameters"]["properties"]["mode"]["enum"], [
            "create", "overwrite", "append",
        ])
        self.assertIn("mode", write["parameters"]["required"])
        self.assertNotIn("create_only", write["description"])

        edit = definitions["edit_file"]
        self.assertIn("old_string", edit["description"])
        self.assertIn("edits", edit["description"])
        self.assertIn("二者不可混用", edit["description"])
        properties = edit["parameters"]["properties"]
        self.assertIn("不可与 edits 同用", properties["old_string"]["description"])
        self.assertIn("只传 edits", properties["edits"]["description"])

        read = definitions["read_file"]
        self.assertIn("start_line", read["parameters"]["properties"])
        self.assertIn("limit", read["parameters"]["properties"])
        self.assertNotIn("end_line", read["parameters"]["properties"])

        search = definitions["search_files"]
        self.assertIn("跨文件搜索", search["description"])
        self.assertIn("只搜索内容，不枚举文件名", search["description"])
        self.assertIn("file_pattern 只筛选待搜索文件", search["description"])
        self.assertIn("不返回文件清单", search["parameters"]["properties"]["file_pattern"]["description"])
        self.assertIn("pattern", search["parameters"]["required"])

    # ---------- write_file ----------

    def test_write_file_creates_and_overwrites(self):
        result = bt.execute_write_file({
            "full_file_name": "sub/dir/a.txt",
            "content": "第一行\n第二行",
        })
        target = Path(self._tmp) / "sub" / "dir" / "a.txt"
        self.assertTrue(target.exists())
        self.assertTrue(result["success"])
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
        self.assertEqual(result["content_hash"], bt._content_hash("hello-world"))

    def test_write_file_guards_existing_content(self):
        path = Path(self._tmp) / "guard.txt"
        path.write_text("before", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "FILE_ALREADY_EXISTS"):
            bt.execute_write_file({"full_file_name": str(path), "content": "after", "create_only": True})
        with self.assertRaisesRegex(ValueError, "FILE_MODIFIED_EXTERNALLY"):
            bt.execute_write_file({"full_file_name": str(path), "content": "after", "expected_hash": "stale"})
        self.assertEqual(path.read_text(encoding="utf-8"), "before")
        bt.execute_write_file({"full_file_name": str(path), "content": "after", "expected_hash": bt._content_hash("before")})
        self.assertEqual(path.read_text(encoding="utf-8"), "after")

    def test_write_file_explicit_modes_and_conflicts(self):
        path = Path(self._tmp) / "mode.txt"
        created = bt.execute_write_file({"full_file_name": str(path), "content": "first", "mode": "create"})
        self.assertEqual(created["action"], "create")
        with self.assertRaisesRegex(ValueError, "FILE_ALREADY_EXISTS"):
            bt.execute_write_file({"full_file_name": str(path), "content": "lost", "mode": "create"})
        with self.assertRaisesRegex(ValueError, "冲突"):
            bt.execute_write_file({"full_file_name": str(path), "content": "lost", "mode": "overwrite", "append": True})
        self.assertEqual(path.read_text(encoding="utf-8"), "first")
        overwritten = bt.execute_write_file({"full_file_name": str(path), "content": "second", "mode": "overwrite"})
        self.assertEqual(overwritten["action"], "overwrite")
        bt.execute_write_file({"full_file_name": str(path), "content": "+tail", "mode": "append"})
        self.assertEqual(path.read_text(encoding="utf-8"), "second+tail")

    def test_create_only_does_not_replace_concurrently_created_file(self):
        path = Path(self._tmp) / "race.txt"
        def concurrent_create(_source, destination):
            Path(destination).write_text("other writer", encoding="utf-8")
            raise FileExistsError(destination)
        with patch.object(bt.os, "link", side_effect=concurrent_create):
            with self.assertRaisesRegex(ValueError, "FILE_ALREADY_EXISTS"):
                bt.execute_write_file({"full_file_name": str(path), "content": "mine", "create_only": True})
        self.assertEqual(path.read_text(encoding="utf-8"), "other writer")

    def test_write_file_append_detects_existing_encoding(self):
        path = Path(self._tmp) / "gbk.txt"
        path.write_text("中文", encoding="gbk")
        result = bt.execute_write_file({"full_file_name": str(path), "content": "追加", "append": True})
        self.assertEqual(result["encoding"], "gbk")
        self.assertEqual(path.read_text(encoding="gbk"), "中文追加")

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
        self.assertTrue(result["success"])
        self.assertTrue(result["_model_result"]["success"])
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["matched_occurrences"], 1)

    def test_edit_file_reports_clear_errors_for_parameter_shapes(self):
        with self.assertRaisesRegex(ValueError, "单次替换必须同时提供 old_string 和 new_string"):
            bt.execute_edit_file({"full_file_name": "x.txt", "old_string": "old"})
        with self.assertRaisesRegex(ValueError, "参数格式冲突.*只选一种"):
            bt.execute_edit_file({
                "full_file_name": "x.txt",
                "old_string": "old",
                "new_string": "new",
                "edits": [{"old_string": "a", "new_string": "b"}],
            })

    def test_edit_file_accepts_read_file_numbered_lines(self):
        path = self._make_file("numbered.py", "first\nsecond\nthird\n")
        result = bt.execute_edit_file({"full_file_name": path, "old_string": "1| first\n2| second", "new_string": "changed"})
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "changed\nthird\n")
        self.assertIn("已去除 read_file 行号", result["message"])

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
        self.assertEqual(
            bt.execute_read_file({"full_file_name": path})["content_hash"],
            bt._content_hash("LINE1\r\nline2\r\n"),
        )

    def test_edit_file_gbk_roundtrip(self):
        path = self._make_file("gbk.txt", "中文内容\n", encoding="gbk")
        result = bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "中文内容",
            "new_string": "改好的中文",
        })
        self.assertEqual(result["encoding"], "gbk")
        self.assertEqual(Path(path).read_text(encoding="gbk"), "改好的中文\n")

    def test_edit_file_preserves_content_beyond_old_eight_mib_boundary(self):
        path = self._make_file("large.txt", "TARGET\n" + "x" * (8 * 1024 * 1024) + "TAIL")
        bt.execute_edit_file({"full_file_name": path, "old_string": "TARGET", "new_string": "CHANGED"})
        data = Path(path).read_text(encoding="utf-8")
        self.assertTrue(data.startswith("CHANGED\n"))
        self.assertTrue(data.endswith("TAIL"))

    def test_edit_file_wrong_encoding_and_stale_hash_leave_original_untouched(self):
        path = self._make_file("encoded.txt", "中文 TARGET\n", encoding="gbk")
        original = Path(path).read_bytes()
        with self.assertRaisesRegex(ValueError, "无法无损识别"):
            bt.execute_edit_file({"full_file_name": path, "old_string": "TARGET", "new_string": "DONE", "encoding": "utf-8"})
        self.assertEqual(Path(path).read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "FILE_MODIFIED_EXTERNALLY"):
            bt.execute_edit_file({"full_file_name": path, "old_string": "TARGET", "new_string": "DONE", "expected_hash": "stale"})
        self.assertEqual(Path(path).read_bytes(), original)

    def test_edit_file_replace_failure_keeps_original(self):
        path = self._make_file("atomic.txt", "TARGET\n")
        with patch.object(bt.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                bt.execute_edit_file({"full_file_name": path, "old_string": "TARGET", "new_string": "DONE"})
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "TARGET\n")
        self.assertEqual(list(Path(self._tmp).glob(".atomic.txt.*.tmp")), [])

    def test_edit_file_delete_via_empty_new_string(self):
        path = self._make_file("del.txt", "keep A\nremove me\nkeep B\n")
        bt.execute_edit_file({
            "full_file_name": path,
            "old_string": "remove me\n",
            "new_string": "",
        })
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "keep A\nkeep B\n")

    def test_edit_file_batch_is_all_or_nothing(self):
        path = self._make_file("batch.txt", "first\nsecond\nthird\n")
        bt.execute_edit_file({"full_file_name": path, "edits": [
            {"old_string": "first", "new_string": "FIRST"},
            {"old_string": "second", "new_string": "SECOND"},
        ]})
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "FIRST\nSECOND\nthird\n")
        with self.assertRaisesRegex(ValueError, "第 2 项"):
            bt.execute_edit_file({"full_file_name": path, "edits": [
                {"old_string": "FIRST", "new_string": "lost"},
                {"old_string": "missing", "new_string": "never"},
            ]})
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "FIRST\nSECOND\nthird\n")

    def test_try_execute_dispatch_and_error_shape(self):
        # 非目标名称返回 None
        self.assertIsNone(bt.try_execute_builtin_file_tool("todo_write", {}))
        self.assertIsNone(bt.try_execute_builtin_file_tool("list_dir", {}))
        # 正常分发
        ok = bt.try_execute_builtin_file_tool(
            "write_file", {"full_file_name": "d.txt", "content": "ok", "mode": "create"}
        )
        self.assertIn("message", ok)
        self.assertTrue(ok["success"])
        # 异常转结构化 error
        bad = bt.try_execute_builtin_file_tool(
            "edit_file", {"full_file_name": "missing.txt", "old_string": "a", "new_string": "b"}
        )
        self.assertIn("error", bad)
        self.assertEqual(bad.get("tool"), "edit_file")
        self.assertIs(bad.get("success"), False)

        invalid_calls = [
            ("write_file", {"full_file_name": "", "content": "x", "mode": "create"}),
            ("edit_file", {"full_file_name": "x.txt", "edits": [], "old_string": "x", "new_string": "y"}),
            ("read_file", {"full_file_name": "missing.txt"}),
            ("search_files", {"pattern": ""}),
        ]
        for name, args in invalid_calls:
            with self.subTest(tool=name):
                result = bt.try_execute_builtin_file_tool(name, args)
                self.assertIs(result.get("success"), False)
                self.assertIn("error", result)

    # ---------- read_file ----------

    def test_read_file_rejects_large_file_before_full_read(self):
        path = Path(self._tmp) / "large.txt"
        with path.open("wb") as handle:
            handle.truncate(65)
        with patch.object(bt, "_READ_FILE_MAX_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "读取上限"):
                bt.execute_read_file({"full_file_name": str(path)})

    def test_search_files_zero_limit_still_has_hard_cap(self):
        path = Path(self._tmp) / "large.txt"
        path.write_bytes(b"TARGET" + b"x" * 65)
        with patch.object(bt, "_SEARCH_HARD_FILE_MAX_BYTES", 64):
            result = bt.execute_search_files({"pattern": "TARGET", "max_file_mb": 0})
        self.assertTrue(result["success"])
        self.assertEqual(result["files_scanned"], 0)
        self.assertIn("跳过 1 个", result["message"])

    def test_read_file_lines_with_numbers(self):
        path = self._make_file("demo.txt", "alpha\nbeta\ngamma\n")
        result = bt.execute_read_file({"full_file_name": path})
        self.assertTrue(result["success"])
        self.assertEqual(result["total_lines"], 3)
        self.assertEqual(result["range_start"], 1)
        self.assertEqual(result["range_end"], 3)
        self.assertFalse(result["has_more"])
        self.assertIn("1| alpha", result["content"])
        self.assertIn("[read_file]", result["message"])
        self.assertNotIn("alpha", result["message"])

    def test_read_file_empty_and_truncated_range_metadata(self):
        empty = self._make_file("empty.txt", "")
        result = bt.execute_read_file({"full_file_name": empty})
        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["total_lines"], 0)
        long_path = self._make_file("many.txt", ("x" * 1000 + "\n") * 100)
        limited = bt.execute_read_file({"full_file_name": long_path})
        self.assertTrue(limited["success"])
        self.assertTrue(limited["truncated"])
        self.assertTrue(limited["has_more"])
        self.assertEqual(limited["next_start_line"], limited["range_end"] + 1)
        self.assertNotIn("已省略", limited["content"])
        continued = bt.execute_read_file({"full_file_name": long_path,
                                          "start_line": limited["next_start_line"]})
        self.assertIn(f'{limited["next_start_line"]}| ', continued["content"])

    def test_read_file_range_and_no_line_numbers(self):
        # end_line 不再暴露给模型，但旧调用仍可单独使用。
        path = self._make_file("range.txt", "l1\nl2\nl3\nl4\n")
        result = bt.execute_read_file({
            "full_file_name": path, "start_line": 2, "end_line": 3,
            "show_line_numbers": False,
        })
        self.assertEqual(result["content"], "l2\nl3")
        self.assertTrue(result["has_more"])

    def test_read_file_limit_and_argument_validation(self):
        path = self._make_file("paged.txt", "\n".join(f"line {i}" for i in range(1, 11)))
        first = bt.execute_read_file({"full_file_name": path, "limit": 3})
        self.assertEqual(first["range_end"], 3)
        second = bt.execute_read_file({"full_file_name": path, "start_line": first["next_start_line"], "limit": 3})
        self.assertEqual(second["range_start"], 4)
        self.assertNotIn("line 3", second["content"])
        with self.assertRaisesRegex(ValueError, "不能同时"):
            bt.execute_read_file({"full_file_name": path, "limit": 3, "end_line": 5})
        with self.assertRaisesRegex(ValueError, "布尔值"):
            bt.execute_read_file({"full_file_name": path, "show_line_numbers": "false"})

    def test_read_file_wrong_explicit_encoding_rejected(self):
        path = self._make_file("utf8.txt", "你好")
        with self.assertRaisesRegex(ValueError, "指定编码"):
            bt.execute_read_file({"full_file_name": path, "encoding": "ascii"})

    def test_read_file_model_budget_supports_followup_chunk(self):
        path = self._make_file("budget.txt", "a" * 1400)
        first = bt.execute_read_file({"full_file_name": path, "_max_chars": 512})
        self.assertEqual(first["mode"], "char_chunk")
        self.assertEqual(first["next_char_offset"], 512)
        second = bt.execute_read_file({"full_file_name": path, "_max_chars": 512,
                                       "char_offset": first["next_char_offset"]})
        self.assertEqual(second["char_offset"], 512)

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
        result = bt.execute_search_files({"pattern": r"TARGET_\d", "is_regex": True})
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
            bt.execute_search_files({"pattern": "(", "is_regex": True})  # 非法正则
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
        regex = bt.execute_search_files({"pattern": "a.c", "is_regex": True})
        self.assertEqual(regex["total_matches"], 2)     # 正则两行都命中
        self.assertEqual(bt.execute_search_files({"pattern": "a.c"})["total_matches"], 1)
        with self.assertRaisesRegex(ValueError, "布尔值"):
            bt.execute_search_files({"pattern": "a.c", "is_regex": "false"})

    def test_search_files_max_results_cap(self):
        for i in range(5):
            self._make_file(f"m{i}.txt", f"hit{i}\n")
        result = bt.execute_search_files({
            "pattern": r"hit\d", "is_regex": True, "max_results": 2,
        })
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_matches"], 5)

    def test_search_files_scan_limit_reports_partial_counts(self):
        self._make_file("a.txt", "hit\n")
        self._make_file("b.txt", "hit\n")
        result = bt.execute_search_files({"pattern": "hit", "max_scanned_files": 1})
        self.assertTrue(result["scan_truncated"])
        self.assertEqual(result["files_checked"], 1)
        self.assertEqual(result["total_matches"], 1)

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
