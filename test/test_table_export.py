# -*- coding: utf-8 -*-
"""表格导出（md → xlsx）单元测试"""
import io
import re
import zipfile

import pytest

from factory.md_table_export import parse_md_table, matrix_to_xlsx_bytes
from factory.xlsx_export import build_xlsx_bytes


MD_SIMPLE = "| a | b |\n|---|---|\n| 1 | 2 |"


def _sheet_xml(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read("xl/worksheets/sheet1.xml").decode("utf-8")


class TestParseMdTable:
    def test_basic_parse(self):
        rows, errors = parse_md_table(MD_SIMPLE)
        assert rows == [["a", "b"], ["1", "2"]]
        assert errors == {}

    def test_inline_code_pipe_not_split(self):
        md = "| cmd | ok |\n|---|---|\n| `x | y` | 是 |"
        rows, _ = parse_md_table(md)
        assert rows[1][0] == "x | y"

    def test_link_keeps_url_and_bold_stripped(self):
        md = "| a |\n|---|\n| **入口** [docs](https://example.com) |"
        rows, _ = parse_md_table(md)
        assert rows[1][0] == "入口 https://example.com"

    def test_inline_markup_stripped(self):
        md = "| x |\n|---|\n| **bold** *it* `code` |"
        rows, _ = parse_md_table(md)
        assert rows[1][0] == "bold it code"

    def test_escaped_pipe_unescaped_like_frontend(self):
        md = "| 含 \\| 转义 | c |\n|---|---|\n| 1 | 2 |"
        rows, _ = parse_md_table(md)
        assert rows[0][0] == "含 | 转义"

    def test_ragged_rows_padded(self):
        md = "| a | b | c |\n|---|---|---|\n| 1 |\n| x | y | z |"
        rows, _ = parse_md_table(md)
        assert rows[1] == ["1", "", ""]
        assert rows[2] == ["x", "y", "z"]

    def test_invalid_divider_raises(self):
        with pytest.raises(ValueError):
            parse_md_table("| a |\n| 不是分隔行 |")

    def test_too_few_lines_raises(self):
        with pytest.raises(ValueError):
            parse_md_table("| a |")

    def test_oversize_raises(self):
        md = "| h |\n|---|\n" + "\n".join("| r |" for _ in range(5001))
        with pytest.raises(ValueError):
            parse_md_table(md)


class TestBuildXlsx:
    def test_basic_build(self):
        data, rc, cc = build_xlsx_bytes([["h1", "h2"], ["v1", "v2"]])
        assert (rc, cc) == (2, 2)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/sharedStrings.xml" in names

    def test_number_cell_native(self):
        data, _, _ = build_xlsx_bytes([["h"], [12.5]])
        sheet = _sheet_xml(data)
        # OOXML 规范：数值单元格省略 t 属性（默认即 n）；带 t="s" 的才是共享字符串
        assert "<v>12.5</v>" in sheet
        row2 = sheet.split('<row r="2">')[1]
        assert 't="s"' not in row2

    def test_header_row_not_number(self):
        data, _, _ = build_xlsx_bytes([["1"], ["2"]])
        sheet = _sheet_xml(data)
        assert 't="n"' not in sheet  # 表头一律按字符串

    def test_error_cell_style(self):
        data, _, _ = build_xlsx_bytes([["h"], [{"error": "清洗失败", "raw": "v"}]])
        sheet = _sheet_xml(data)
        assert 's="2"' in sheet
        assert "导出失败" in zipfile.ZipFile(io.BytesIO(data)).read("xl/sharedStrings.xml").decode("utf-8")

    def test_empty_table_raises(self):
        for bad in ([], [[]], ["abc"]):
            with pytest.raises(ValueError):
                build_xlsx_bytes(bad)

    def test_col_limit_raises(self):
        with pytest.raises(ValueError):
            build_xlsx_bytes([[""] * 257])

    def test_newline_escaped_in_shared_strings(self):
        data, _, _ = build_xlsx_bytes([["a\nb"]])
        ss = zipfile.ZipFile(io.BytesIO(data)).read("xl/sharedStrings.xml").decode("utf-8")
        assert "_x000A_" in ss

    def test_matrix_to_xlsx_bridge(self):
        parsed = parse_md_table(MD_SIMPLE)
        data, rc, cc = matrix_to_xlsx_bytes(parsed)
        assert (rc, cc) == (2, 2)
        assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None
