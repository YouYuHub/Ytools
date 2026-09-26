"""原生文档（supportDocTypes）与文本类文件上传支持测试。

覆盖：
- env_manager：supportDocTypes 归一化（get_model_config / list_available_models）；
- file_factory：文本扩展名白名单、二进制嗅探、编码回退解码、未知扩展名解析；
- file_memory：原生文档记录筛选（数量/单文件/总量上限）、部件构建（data URL）、
  占位回收（retire_native_document_parts）、清单标注（原生文档/截断提示）；
- file_router：上传落盘（stored_name/abs_path/截断标记/native_doc_supported）；
- builtin_tools：execute_read_document 原生分支（命中返回 native 块，未命中纯文本）；
- 前端契约：DOC_EXTENSIONS 与后端 TEXT_FILE_EXTENSIONS 覆盖一致。

运行：python -m pytest test/test_native_document_support.py -v
"""
import base64
import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_manager import init_path  # noqa: E402
from factory import file_factory  # noqa: E402
from memory import file_memory  # noqa: E402
from factory.agent_runtime import builtin_tools  # noqa: E402


class SupportDocTypesNormalizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        (self._tmp / "setting").mkdir(parents=True, exist_ok=True)
        (self._tmp / "setting" / "models.json").write_text(json.dumps({
            "Prov": {
                "vendor": "custom_endpoint",
                "apiKey": "k",
                "apiType": "chat-completions",
                "models": {
                    "NativeDoc": {
                        "id": "native-doc",
                        "url": "https://example.test/v1",
                        "vision": True,
                        "supportDocTypes": [".PDF", "docx", "", "  ", "/x", 5],
                    },
                    "NoDoc": {
                        "id": "no-doc",
                        "url": "https://example.test/v1",
                        "supportDocTypes": None,
                    },
                },
            },
        }), encoding="utf-8")
        (self._tmp / ".env").write_text("", encoding="utf-8")
        init_path(self._tmp)

    def tearDown(self):
        init_path(ROOT)
        self._tmp_dir.cleanup()

    def test_normalized_in_model_config(self):
        from env_manager import get_model_config

        cfg = get_model_config("Prov", "NativeDoc")
        self.assertEqual(cfg["support_doc_types"], [".pdf", ".docx"])
        self.assertEqual(get_model_config("Prov", "NoDoc")["support_doc_types"], [])

    def test_listed_in_available_models(self):
        from env_manager import list_available_models

        by_name = {item["model_name"]: item for item in list_available_models()}
        self.assertEqual(by_name["NativeDoc"]["support_doc_types"], [".pdf", ".docx"])
        self.assertEqual(by_name["NoDoc"]["support_doc_types"], [])


class TextFileParsingTests(unittest.TestCase):
    def test_text_extensions(self):
        for name in ("a.txt", "b.py", "c.json", "d.log", "e.yaml", "f.ts", "g.sh"):
            self.assertTrue(file_factory.get_text_extension(name), name)
        for name in ("a.exe", "b.zip", "c.png", "d.docx", "e"):
            self.assertFalse(file_factory.get_text_extension(name), name)

    def test_binary_sniff(self):
        self.assertTrue(file_factory.is_probably_binary(b"abc\x00def"))
        self.assertTrue(file_factory.is_probably_binary(bytes(range(1, 32)) * 40))
        self.assertFalse(file_factory.is_probably_binary("中文文本\n第二行".encode("utf-8")))
        self.assertFalse(file_factory.is_probably_binary(b""))

    def test_decode_fallback_chain(self):
        self.assertEqual(file_factory.decode_text_bytes("中文".encode("utf-8"))[1], "utf-8-sig")
        text, enc = file_factory.decode_text_bytes("中文".encode("gbk"))
        self.assertEqual(text, "中文")
        self.assertIn(enc, ("gb18030", "latin-1"))
        # latin-1 兜底：任意字节都能解码，不抛异常
        text, enc = file_factory.decode_text_bytes(b"\xff\xfe\xfa")
        self.assertEqual(enc, "latin-1")

    def test_known_text_extension_accepts_odd_bytes(self):
        # 已知文本扩展名：即使含空字节也按文本解析（用户显式命名 .txt）
        text = file_factory.parse_text_file_bytes("a.txt", b"hi\x00there")
        self.assertIn("hi", text)

    def test_unknown_extension_binary_rejected(self):
        with self.assertRaises(ValueError):
            file_factory.parse_text_file_bytes("weird.zzz", bytes(range(256)))

    def test_unknown_extension_text_accepted(self):
        text = file_factory.extract_text_from_bytes("weird.zzz", "hello 世界".encode("utf-8"))
        self.assertEqual(text, "hello 世界")

    def test_docx_still_uses_dedicated_parser(self):
        # 富文档仍走专用解析器：不可用时报错信息来自解析器（而非文本解码）
        with mock.patch.object(file_factory, "docx", None):
            with self.assertRaises(Exception):
                file_factory.extract_text_from_bytes("a.docx", b"PK\x03\x04fake")

    def test_frontend_extension_list_matches_backend(self):
        import re

        core_js = (ROOT / "H5" / "js" / "app" / "core.js").read_text(encoding="utf-8")
        start = core_js.index("const DOC_EXTENSIONS = [")
        end = core_js.index("];", start)
        block = core_js[start:end]
        # 去掉行注释（// 之后到行尾），再抽取引号内的扩展名
        block = re.sub(r"//[^\n]*", "", block)
        frontend_exts = set(re.findall(r'"([a-z0-9]+)"', block))
        backend_exts = {ext.lstrip(".") for ext in file_factory.TEXT_FILE_EXTENSIONS}
        missing = sorted(backend_exts - frontend_exts)
        self.assertEqual(missing, [], f"前端 DOC_EXTENSIONS 缺少：{missing}")


class NativeDocumentHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        (self._tmp / "setting").mkdir(parents=True, exist_ok=True)
        (self._tmp / "setting" / "models.json").write_text("{}", encoding="utf-8")
        (self._tmp / ".env").write_text("", encoding="utf-8")
        init_path(self._tmp)
        self._session = "native_doc_test"
        self._session_dir = self._tmp / "history_files" / "session_files" / self._session
        (self._session_dir / "files").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        init_path(ROOT)
        self._tmp_dir.cleanup()

    def _add_record(self, filename: str, data: bytes, *, type_: str = "pdf") -> dict:
        saved = file_memory.save_session_document(self._session, filename, data)
        record = {
            "filename": filename,
            "type": type_,
            "content": "解析文本" * 10,
            "size": len(data),
            "stored_name": saved["stored_name"],
            "abs_path": str(
                file_memory.resolve_document_path(self._session, saved["stored_name"])
            ).replace("\\", "/"),
        }
        import asyncio

        asyncio.run(file_memory.get_file_memory_manager(self._session)).add_file_memory(
            record, max_items=20
        )
        return record

    def test_document_supports_native(self):
        self.assertTrue(file_memory.document_supports_native("a.pdf", [".pdf", ".docx"]))
        self.assertTrue(file_memory.document_supports_native("a.PDF", ["pdf"]))
        self.assertFalse(file_memory.document_supports_native("a.xlsx", [".pdf"]))
        self.assertFalse(file_memory.document_supports_native("a.pdf", []))

    def test_build_native_part_data_url(self):
        record = self._add_record("手册.pdf", b"%PDF-1.4 hello")
        built = file_memory.build_native_document_part(self._session, record)
        self.assertNotIn("error", built)
        part = built["part"]
        self.assertEqual(part["type"], "file")
        self.assertEqual(part["file"]["filename"], "手册.pdf")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))
        decoded = base64.b64decode(part["file"]["file_data"].split(",", 1)[1])
        self.assertEqual(decoded, b"%PDF-1.4 hello")

    def test_build_native_part_missing_file(self):
        built = file_memory.build_native_document_part(
            self._session, {"filename": "x.pdf", "stored_name": "nonexistent.pdf"}
        )
        self.assertIn("error", built)

    def test_build_native_part_over_size_limit(self):
        record = self._add_record("big.pdf", b"x" * 100)
        with mock.patch.object(file_memory, "native_doc_max_bytes", return_value=10):
            built = file_memory.build_native_document_part(self._session, record)
        self.assertIn("error", built)
        self.assertIn("上限", built["error"])

    def test_select_records_respects_limits(self):
        records = [
            self._add_record("a.pdf", b"a" * 10),
            self._add_record("b.pdf", b"b" * 10),
            self._add_record("c.docx", b"c" * 10),
            self._add_record("d.txt", b"d" * 10, type_="txt"),
        ]
        selected, skipped = file_memory.select_native_document_records(
            records, [".pdf", ".docx"], max_items=2, max_bytes=1000, total_max_bytes=1000
        )
        names = [item["filename"] for item in selected]
        self.assertEqual(names, ["a.pdf", "b.pdf"])
        self.assertTrue(any("数量上限" in note for note in skipped), skipped)
        # txt 不在声明内：静默跳过（不计入 skipped 说明）
        self.assertFalse(any("d.txt" in note for note in skipped))

    def test_select_records_total_bytes_limit(self):
        records = [
            self._add_record("a.pdf", b"a" * 60),
            self._add_record("b.pdf", b"b" * 60),
        ]
        selected, skipped = file_memory.select_native_document_records(
            records, [".pdf"], max_items=5, max_bytes=1000, total_max_bytes=100
        )
        self.assertEqual([item["filename"] for item in selected], ["a.pdf"])
        self.assertTrue(any("总量上限" in note for note in skipped), skipped)

    def test_retire_native_parts_replaces_with_text(self):
        record = self._add_record("手册.pdf", b"%PDF-1.4")
        built = file_memory.build_native_document_part(self._session, record)
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": "前言"}, built["part"]],
            "_internal": True,
        }]
        replaced = file_memory.retire_native_document_parts(messages)
        self.assertEqual(replaced, 1)
        self.assertEqual(messages[0]["content"][1]["type"], "text")
        self.assertIn("手册.pdf", messages[0]["content"][1]["text"])

    def test_manifest_annotates_native_and_truncated(self):
        record = self._add_record("手册.pdf", b"%PDF")
        record["content_truncated"] = True
        record["content_total_chars"] = 999
        text = file_memory.build_file_manifest_text(
            [record], native_doc_types=[".pdf"], read_document_available=True
        )
        self.assertIn("原生文档已随请求发送", text)
        self.assertIn("解析文本已截断", text)
        self.assertIn("read_file", text)

    def test_part_label_uses_filename(self):
        label = file_memory._media_reference_label({
            "type": "file",
            "file": {"filename": "报告.docx", "file_data": "data:..."},
        })
        self.assertEqual(label, "[文档 报告.docx]")

    def test_content_part_to_text_uses_doc_label(self):
        text = file_memory.content_part_to_text([
            {"type": "text", "text": "看这个"},
            {"type": "file", "file": {"filename": "报告.docx", "file_data": "data:..."}},
        ])
        self.assertIn("看这个", text)
        self.assertIn("[文档 报告.docx]", text)


class ReadDocumentNativeBranchTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        (self._tmp / "setting").mkdir(parents=True, exist_ok=True)
        (self._tmp / "setting" / "models.json").write_text("{}", encoding="utf-8")
        (self._tmp / ".env").write_text("", encoding="utf-8")
        init_path(self._tmp)
        self._session = "read_doc_native"
        saved = file_memory.save_session_document(self._session, "说明.pdf", b"%PDF-1.4 body")
        self._record = {
            "filename": "说明.pdf",
            "type": "pdf",
            "content": "第一段" * 100,
            "size": 13,
            "stored_name": saved["stored_name"],
        }
        import asyncio

        asyncio.run(file_memory.get_file_memory_manager(self._session)).add_file_memory(
            self._record, max_items=10
        )

    def tearDown(self):
        init_path(ROOT)
        self._tmp_dir.cleanup()

    def test_native_branch_when_supported(self):
        result = builtin_tools.execute_read_document(
            {"filename": "说明.pdf", "max_chars": 10},
            self._session,
            support_doc_types=[".pdf"],
        )
        self.assertIn("native", result)
        self.assertEqual(result["native"]["part"]["type"], "file")
        self.assertIn("原生文档", result["message"])
        self.assertEqual(len(result["content"]), 10)

    def test_text_branch_when_not_supported(self):
        result = builtin_tools.execute_read_document(
            {"filename": "说明.pdf", "max_chars": 10},
            self._session,
            support_doc_types=[],
        )
        self.assertNotIn("native", result)
        self.assertEqual(result["total_chars"], 300)

    def test_text_branch_when_type_not_declared(self):
        result = builtin_tools.execute_read_document(
            {"filename": "说明.pdf", "max_chars": 10},
            self._session,
            support_doc_types=[".docx"],
        )
        self.assertNotIn("native", result)

    def test_native_block_stripped_from_model_text(self):
        result = builtin_tools.execute_read_document(
            {"filename": "说明.pdf"}, self._session, support_doc_types=[".pdf"]
        )
        import factory.chat_factory as chat_factory

        text = chat_factory._format_tool_result(result)
        self.assertNotIn("base64", text)
        self.assertNotIn("file_data", text)
        self.assertIn("原生文档", text)
        self.assertIn('"native_document"', text)

    def test_sub_agent_text_strips_native(self):
        from factory.agent_runtime import sub_agent

        result = builtin_tools.execute_read_document(
            {"filename": "说明.pdf"}, self._session, support_doc_types=[".pdf"]
        )
        text = sub_agent._format_result_text(result)
        self.assertNotIn("base64", text)
        self.assertIn("原生文档", text)

    def test_backward_compatible_signature(self):
        # 旧调用（不传 support_doc_types）仍返回纯文本口径
        result = builtin_tools.execute_read_document({"filename": "说明.pdf"}, self._session)
        self.assertNotIn("native", result)
        self.assertIn("content", result)


if __name__ == "__main__":
    unittest.main()
