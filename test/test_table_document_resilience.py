"""Regression: malformed xlsx styles must not kill text/table/auto reads.

Server badcase: exporters emit empty <fill/> or fills whose first child is an
extension node; openpyxl Fill.from_tree returns None / raises TypeError and the
whole workbook load aborts, so every read_document mode failed. The parser now
repairs such style nodes and, if openpyxl still fails, falls back to raw XML.
"""
import asyncio
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from factory import table_document as td
from factory.agent_runtime import builtin_tools as bt
from memory import file_memory


def workbook_bytes():
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet.append(["姓名", "金额", "备注"])
    sheet.append(["小王", 12.5, "a"])
    sheet.append(["小李", 3, "b"])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def rewrite_styles(data, transform):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "xl/styles.xml":
                payload = transform(payload.decode("utf-8")).encode("utf-8")
            dst.writestr(item, payload)
    return out.getvalue()


def empty_fill_bytes():
    # <fill/> with no children: Fill.from_tree -> None -> TypeError(expected Fill)
    return rewrite_styles(workbook_bytes(), lambda s: re.sub(
        r"<fill><patternFill\s*/></fill>", "<fill/>", s, count=1))


def ext_first_fill_bytes():
    # first child is an extension node: Fill() takes no arguments / unexpected kw
    return rewrite_styles(workbook_bytes(), lambda s: re.sub(
        r"<fill><patternFill\s*/></fill>",
        '<fill><extLst><ext uri="{TEST}"/></extLst></fill>', s, count=1))


def minimal_raw_xlsx():
    """Hand-built workbook exercising shared/inline strings, gap rows, cached formula."""
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            '</Relationships>'),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">'
            '<si><t>姓名</t></si><si><t>小王</t></si></sst>'),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>金额</t></is></c></row>'
            '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="B3"><v>12.5</v></c>'
            '<c r="C3"><f>B3*2</f><v>25</v></c></row>'
            '</sheetData></worksheet>'),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, payload in parts.items():
            dst.writestr(name, payload)
    return out.getvalue()


class StyleRepairTests(unittest.TestCase):
    def test_healthy_workbook_not_rewritten(self):
        self.assertIsNone(td._repair_xlsx_styles(workbook_bytes()))
        document = td.parse_table("x.xlsx", workbook_bytes())
        self.assertFalse(document.style_repair)
        self.assertFalse(document.raw_fallback)
        self.assertNotIn("style_note", td.table_page(document))

    def test_empty_fill_repaired_and_data_intact(self):
        document = td.parse_table("x.xlsx", empty_fill_bytes())
        self.assertTrue(document.style_repair)
        self.assertFalse(document.raw_fallback)
        page = td.table_page(document)
        self.assertEqual(page["headers"], ["姓名", "金额", "备注"])
        self.assertIn("小王 | 12.5 | a", page["content"])
        self.assertIn("style_note", page)

    def test_extension_first_fill_repaired(self):
        document = td.parse_table("x.xlsx", ext_first_fill_bytes())
        self.assertTrue(document.style_repair)
        self.assertFalse(document.raw_fallback)
        self.assertIn("小李 | 3 | b", td.table_page(document)["content"])

    def test_repair_keeps_fill_count_and_order(self):
        repaired = td._repair_xlsx_styles(empty_fill_bytes())
        self.assertIsNotNone(repaired)
        with zipfile.ZipFile(io.BytesIO(repaired)) as archive:
            styles = archive.read("xl/styles.xml").decode("utf-8")
        self.assertIn('count="2"', styles)
        self.assertEqual(styles.count("<fill>"), 2)
        self.assertNotIn("<fill/>", styles)


class RawFallbackTests(unittest.TestCase):
    def test_openpyxl_failure_downgrades_to_raw_xml(self):
        with patch.object(td, "_repair_xlsx_styles", return_value=None), \
                patch.object(td, "_xlsx_openpyxl_sheets",
                             side_effect=TypeError("expected <class 'openpyxl.styles.fills.Fill'>")):
            document = td.parse_table("x.xlsx", workbook_bytes())
        self.assertTrue(document.raw_fallback)
        self.assertIn("expected", document.raw_error)
        page = td.table_page(document)
        self.assertEqual(page["headers"], ["姓名", "金额", "备注"])
        self.assertIn("小王 | 12.5 | a", page["content"])
        self.assertIn("raw_note", page)

    def test_real_openpyxl_error_on_empty_fill_without_repair(self):
        # No mocking of openpyxl: the genuine TypeError must trigger the fallback.
        with patch.object(td, "_repair_xlsx_styles", return_value=None):
            document = td.parse_table("x.xlsx", empty_fill_bytes())
        self.assertTrue(document.raw_fallback)
        self.assertIn("小王 | 12.5 | a", td.table_page(document)["content"])

    def test_raw_parser_shared_inline_gap_and_cached_formula(self):
        with patch.object(td, "_xlsx_openpyxl_sheets",
                          side_effect=TypeError("expected <class 'openpyxl.styles.fills.Fill'>")):
            document = td.parse_table("x.xlsx", minimal_raw_xlsx())
        self.assertTrue(document.raw_fallback)
        self.assertEqual(document.sheets[0].name, "数据")
        self.assertEqual(document.sheets[0].rows,
                         [["姓名", "金额"], [], ["小王", "12.5", "25"]])
        self.assertFalse(document.formula_fallback)  # cached value present

    def test_budget_refusal_is_not_downgraded(self):
        with patch.object(td, "MAX_TABLE_CELLS", 3):
            with self.assertRaisesRegex(ValueError, "解析预算"):
                td.parse_table("x.xlsx", workbook_bytes())

    def test_markdown_text_carries_fallback_note(self):
        with patch.object(td, "_xlsx_openpyxl_sheets", side_effect=TypeError("boom")):
            document = td.parse_table("x.xlsx", workbook_bytes())
        text = "\n".join(td.markdown_sheets(document))
        self.assertIn("底层 XML 直读", text)
        self.assertIn("小王", text)


class ReadDocumentModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.root = patch.object(file_memory, "HISTORY_ROOT", self.folder)
        self.root.start()
        self.sid = "resilience_tool_test"

    def tearDown(self):
        asyncio.run(file_memory.cleanup_file_memory_manager(self.sid))
        self.root.stop()
        self.tmp.cleanup()

    def local(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def test_all_text_modes_survive_malformed_styles(self):
        name = self.local("bad.xlsx", empty_fill_bytes())
        auto = bt.execute_read_document({"filename": name}, self.sid)
        self.assertNotIn("error", auto)
        self.assertEqual(auto["mode"], "table")
        self.assertEqual(auto["headers"][0], "姓名")
        self.assertIn("容错", auto["message"])
        table = bt.execute_read_document({"filename": name, "mode": "table"}, self.sid)
        self.assertNotIn("error", table)
        text = bt.execute_read_document({"filename": name, "mode": "text"}, self.sid)
        self.assertNotIn("error", text)
        self.assertIn("小王", text["content"])
        self.assertIn("容错", text["content"])  # markdown_sheets 首表注记行

    def test_native_unsupported_points_to_table_and_text(self):
        name = self.local("bad.xlsx", empty_fill_bytes())
        result = bt.execute_read_document({"filename": name, "mode": "native"}, self.sid)
        self.assertIn("error", result)
        self.assertIn("mode=table", result["error"])
        self.assertIn("mode=text", result["error"])

    def test_raw_fallback_message_reaches_tool_result(self):
        name = self.local("bad.xlsx", empty_fill_bytes())
        with patch.object(td, "_repair_xlsx_styles", return_value=None):
            result = bt.execute_read_document({"filename": name, "mode": "table"}, self.sid)
        self.assertNotIn("error", result)
        self.assertIn("底层 XML 直读", result["message"])
        self.assertIn("小王", result["content"])


if __name__ == "__main__":
    unittest.main()
