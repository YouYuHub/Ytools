"""Real CSV/XLSX fixtures: headers, pagination, legacy recovery and bounded output."""
import asyncio
import io
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from factory import file_factory
from factory import table_document as td
from factory.agent_runtime import builtin_tools as bt
from factory.agent_runtime.chat_runtime import estimate_text_tokens, estimate_send_context_tokens
from memory import file_memory


def workbook_bytes():
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet.append(["姓名", "金额", "备注", "开关", "日期", "公式"])
    sheet.append(["小王", 0, None, False, datetime(2026, 10, 10), "=1+2"])
    sheet.append(["小李", "0012", "a|b\n第二行", True, None, None])
    workbook.create_sheet("汇总").append(["总数", 2])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


class TableParserTests(unittest.TestCase):
    def test_csv_quoted_cells_empty_values_and_newlines(self):
        document = td.parse_table("x.csv", b'name,value,note\nA,0012,"a,b\nsecond"\nB,0,\n')
        result = td.table_page(document)
        self.assertEqual(result["headers"], ["name", "value", "note"])
        self.assertIn("0012", result["content"])
        self.assertIn("a,b<br>second", result["content"])
        self.assertIn("| 3 | B | 0 |  |", result["content"])

    def test_tsv_and_chinese_legacy_encoding(self):
        result = td.table_page(td.parse_table("x.tsv", "姓名\t备注\n小王\tNA\n".encode("gb18030")))
        self.assertEqual(result["headers"], ["姓名", "备注"])
        self.assertIn("小王 | NA", result["content"])

    def test_utf16_bom_and_semicolon_csv(self):
        document = td.parse_table("x.csv", "姓名;数量\n甲;0\n".encode("utf-16"))
        self.assertEqual(document.sheets[0].rows, [["姓名", "数量"], ["甲", "0"]])

    def test_no_header_preserves_first_row(self):
        result = td.table_page(td.parse_table("x.csv", b'A,1\nB,2\n'), header_row=0, row_limit=1)
        self.assertEqual(result["headers"], ["列 A", "列 B"])
        self.assertIn("| 1 | A | 1 |", result["content"])
        self.assertEqual(result["next_row"], 2)

    def test_custom_header_duplicate_and_missing_titles(self):
        result = td.table_page(td.parse_table("x.csv", b'title\nname,name,\nA,B,C\n'), header_row=2)
        self.assertEqual(result["headers"], ["name", "name", "列 C"])
        self.assertEqual(result["start_row"], 3)

    def test_pagination_repeats_header_without_duplicating_data(self):
        document = td.parse_table("x.csv", b'id,value\n1,a\n2,b\n3,c\n')
        first = td.table_page(document, row_limit=1)
        second = td.table_page(document, start_row=first["next_row"], row_limit=2)
        self.assertEqual(second["rows_returned"], 2)
        self.assertIn("| 原始行号 | id | value |", second["content"])
        self.assertNotIn("| 2 | 1 | a |", second["content"])
        self.assertFalse(second["has_more"])

    def test_markdown_escaping(self):
        self.assertEqual(td.markdown_cell('<b>|`x`\nend'), '&lt;b&gt;\\|\\`x\\`<br>end')

    def test_empty_csv(self):
        result = td.table_page(td.parse_table("empty.csv", b''))
        self.assertEqual(result["rows_returned"], 0)
        self.assertFalse(result["has_more"])

    def test_xlsx_multisheet_and_formula_without_cache(self):
        document = td.parse_table("x.xlsx", workbook_bytes())
        first = td.table_page(document)
        self.assertEqual(first["sheets"], ["明细", "汇总"])
        self.assertEqual(first["next_sheet"], "汇总")
        self.assertIn("| 小王 | 0 |  | FALSE | 2026-10-10 00:00:00 | =1+2 |", first["content"])
        self.assertIn("a\\|b<br>第二行", first["content"])
        self.assertIn("formula_note", first)
        second = td.table_page(document, "汇总", header_row=0)
        self.assertIn("| 1 | 总数 | 2 |", second["content"])

    def test_real_xls_headers_types_and_multiple_sheets(self):
        fixture = Path(__file__).parent / "fixtures" / "table_sample.xls"
        document = td.parse_table("sample.xls", fixture.read_bytes())
        result = td.table_page(document)
        self.assertEqual(result["headers"], ["姓名", "金额", "备注", "开关", "日期"])
        self.assertIn("小王 | 0 |  | FALSE | 2026-10-10 00:00:00", result["content"])
        self.assertIn("小李 | 0012 | a\\|b<br>第二行 | TRUE", result["content"])
        self.assertEqual(result["next_sheet"], "Summary")
        self.assertIn("| 2 | 2 |", td.table_page(document, "Summary")["content"])

    def test_excel_upload_parser_keeps_column_titles(self):
        pages = file_factory._parse_excel_with_pages(workbook_bytes())
        self.assertEqual(len(pages), 2)
        self.assertIn("姓名 | 金额 | 备注", pages[0]["content"])

    def test_cell_budget_refuses_instead_of_silently_losing_rows(self):
        with patch.object(td, "MAX_TABLE_CELLS", 3), self.assertRaisesRegex(ValueError, "解析预算"):
            td.parse_table("x.csv", b'a,b\nc,d\n')

    def test_xlsx_expansion_budget(self):
        with patch.object(td, "MAX_XLSX_EXPANDED_BYTES", 100), self.assertRaisesRegex(ValueError, "解压"):
            td.parse_table("x.xlsx", workbook_bytes())

    def test_oversized_row_does_not_return_partial_cell(self):
        document = td.parse_table("x.csv", b'title\n' + b'x' * 1000)
        with self.assertRaisesRegex(ValueError, "单行"):
            td.table_page(document, max_chars=100)

    def test_missing_sheet_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "可用工作表"):
            td.table_page(td.parse_table("x.xlsx", workbook_bytes()), "不存在")


class DocumentTableExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.root = patch.object(file_memory, "HISTORY_ROOT", self.folder)
        self.root.start()
        self.sid = "table_tool_test"

    def tearDown(self):
        asyncio.run(file_memory.cleanup_file_memory_manager(self.sid))
        self.root.stop()
        self.tmp.cleanup()

    def local(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def test_auto_csv_table_and_explicit_text(self):
        name = self.local("x.csv", b'name,count\nA,0\nB,2\n')
        result = bt.execute_read_document({"filename": name, "row_limit": 1}, self.sid)
        self.assertEqual(result["mode"], "table")
        self.assertEqual(result["next_row"], 3)
        raw = bt.execute_read_document({"filename": name, "mode": "text"}, self.sid)
        self.assertEqual(raw["content"], 'name,count\nA,0\nB,2\n')

    def test_offset_switches_auto_to_legacy_character_paging(self):
        name = self.local("x.csv", b'head\nA\n')
        result = bt.execute_read_document({"filename": name, "offset": 2, "limit": 3}, self.sid)
        self.assertEqual(result["mode"], "text")
        self.assertEqual(result["content"], "ad\n")

    def test_native_supported_and_forced_table(self):
        name = self.local("x.xlsx", workbook_bytes())
        native = bt.execute_read_document({"filename": name}, self.sid, support_doc_types=[".xlsx"])
        self.assertEqual(native["mode"], "native")
        result = bt.execute_read_document({"filename": name, "mode": "table"}, self.sid, support_doc_types=[".xlsx"])
        self.assertEqual(result["headers"][0], "姓名")

    def test_uploaded_legacy_excel_record_recovers_header_from_original(self):
        saved = file_memory.save_session_document(self.sid, "old.xlsx", workbook_bytes())
        manager = asyncio.run(file_memory.get_file_memory_manager(self.sid))
        manager.add_file_memory({"filename": "old.xlsx", "stored_name": saved["stored_name"], "type": "xlsx",
                                 "content": "小王 0 小李 0012", "content_truncated": True})
        result = bt.execute_read_document({"filename": "old.xlsx"}, self.sid)
        self.assertEqual(result["headers"][0], "姓名")
        self.assertFalse(result["source_truncated"])

    def test_table_cache_updates_after_file_changed(self):
        name = self.local("x.csv", b'old\n1\n')
        self.assertEqual(bt.execute_read_document({"filename": name}, self.sid)["headers"], ["old"])
        Path(name).write_bytes(b'new header\n22\n')
        self.assertEqual(bt.execute_read_document({"filename": name}, self.sid)["headers"], ["new header"])

    def test_table_budget_accounts_for_metadata_and_keeps_cursor(self):
        name = self.local("x.csv", ('姓名,备注\n' + '小王,' + '正文' * 50 + '\n' +
                                      ('小李,数据\n' * 1000)).encode())
        result = bt.execute_read_document({"filename": name, "_max_tokens": 500, "row_limit": 200}, self.sid)
        self.assertNotIn("error", result)
        self.assertLessEqual(estimate_text_tokens(result), 500)
        self.assertGreater(result["rows_returned"], 0)
        self.assertEqual(result["next_row"], result["end_row"] + 1)

    def test_text_budget_no_loss_when_continued(self):
        text = "中文内容" * 3000
        name = self.local("x.txt", text.encode())
        first = bt.execute_read_document({"filename": name, "_max_tokens": 300, "limit": 20000}, self.sid)
        second = bt.execute_read_document({"filename": name, "_max_tokens": 300,
                                          "offset": first["next_offset"]}, self.sid)
        self.assertLessEqual(estimate_text_tokens(first), 300)
        self.assertEqual(first["content"] + second["content"], text[:second["end_char"]])

    def test_no_remaining_window_and_oversized_local_file(self):
        name = self.local("x.csv", b'h\na\n')
        self.assertIn("上下文", bt.execute_read_document({"filename": name, "_max_tokens": 0}, self.sid)["error"])
        with patch.object(bt, "_READ_DOCUMENT_LOCAL_MAX_BYTES", 3):
            self.assertIn("超过读取上限", bt.execute_read_document({"filename": name}, self.sid)["error"])

    def test_parameter_conflicts(self):
        for args in ({"mode": "table", "offset": 0}, {"mode": "text", "row_limit": 2}):
            self.assertIn("error", bt.execute_read_document({"filename": "x.csv", **args}, self.sid))

    def test_budget_uses_effective_window_and_shares_batch(self):
        messages = [{"role": "user", "content": "内容" * 1000}]
        config = {"max_input_tokens": 4096}
        one = bt.document_output_token_budget(model_config=config, messages=messages)
        three = bt.document_output_token_budget(model_config=config, messages=messages, tool_count=3)
        self.assertLess(three, one)
        self.assertLessEqual(three * 3 + estimate_send_context_tokens(messages), 4096 - 512)
        self.assertLess(bt.document_output_token_budget(model_config={"maxInputTokens": 1024}), one)
        self.assertEqual(bt.model_tool_output_char_budget({"maxInputTokens": 8192}), 4096)
        self.assertEqual(bt.model_tool_output_char_budget({"maxInputTokens": 1024}), 512)


if __name__ == "__main__":
    unittest.main()
