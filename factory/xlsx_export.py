# coding: utf-8
"""
xlsx 导出器（纯标准库实现，零第三方依赖）

仅依赖 zipfile + xml 内置模块，手工拼装最小可用的 xlsx（Excel 2007+ OOXML 包）：
[Content_Types].xml、_rels/.rels、xl/workbook.xml、xl/_rels/workbook.xml.rels、
xl/worksheets/sheet1.xml、xl/styles.xml、xl/sharedStrings.xml。

单元格取值约定：
  - 标量 str/int/float/bool/None：
      int/float（非 bool）→ 原生数值单元格（t="n"）；
      其余 → 共享字符串单元格（None 存空串）。
  - dict {"error": 提示, "raw": 原值}：导出失败的占位单元格，
      写入「原值 + 导出说明」并套错误样式（灰底红字）。

样式索引（styles.xml 内固定三款，与 _write_styles 配套）：
  0 - 默认   1 - 表头（加粗）   2 - 错误占位（灰底红字）
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape

# XML 声明统一放包头，压住 BOM/前导空白（Excel 兼容性最稳）
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# XML 1.0 非法控制字符（\t \n \r 除外）兜底清洗
_ILLEGAL_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

# 单元格总数与列数硬上限，防内存/表格滥用
MAX_CELLS = 2_000_000
MAX_COLS = 256

_STYLE_DEFAULT = 0
_STYLE_HEADER = 1
_STYLE_ERROR = 2


def _sanitize_text(value: str) -> str:
    """清洗字符串：去掉 XML 非法控制字符。"""
    return _ILLEGAL_XML_RE.sub("", value)


def _normalize_cell(raw) -> tuple:
    """单元格值标准化为 (kind, text, error)

    kind: "number" | "string" | "empty"；
    error 非空时 text 为占位说明文字，套错误样式。
    """
    if isinstance(raw, dict):  # 导出失败占位：{"error": 提示, "raw": 原值}
        raw_text = raw.get("raw", "")
        raw_text = _sanitize_text(raw_text if isinstance(raw_text, str) else str(raw_text))
        reason = _sanitize_text(str(raw.get("error", "导出失败")))
        note = f"{raw_text}（Excel 导出失败：{reason}）" if raw_text else f"（Excel 导出失败：{reason}）"
        return ("string", note, True)
    if raw is None:
        return ("empty", "", False)
    if isinstance(raw, bool):
        return ("string", "TRUE" if raw else "FALSE", False)
    if isinstance(raw, (int, float)):
        if raw != raw or raw in (float("inf"), float("-inf")):  # NaN/Inf 非法，落为空
            return ("empty", "", False)
        return ("number", repr(raw), False)
    text = _sanitize_text(str(raw)).strip()
    return ("string", text, False) if text else ("empty", "", False)


def build_xlsx_bytes(rows: list) -> tuple[bytes, int, int]:
    """
    rows: 二维数组（行 → 单元格标量或错误 dict）。
    返回 (xlsx 文件字节, 实际行数, 实际列数)；空表抛 ValueError。
    """
    if not isinstance(rows, list):
        raise ValueError("rows 必须是二维数组")
    matrix: list[list[tuple]] = []
    col_count = 0
    total_cells = 0
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("rows 必须是二维数组")
        cells = [_normalize_cell(raw) for raw in row]
        matrix.append(cells)
        col_count = max(col_count, len(cells))
        total_cells += len(cells)
    if not matrix or col_count == 0:
        raise ValueError("表格内容为空")
    if col_count > MAX_COLS:
        raise ValueError(f"列数超过 xlsx 上限 {MAX_COLS}")
    if total_cells > MAX_CELLS:
        raise ValueError(f"单元格总数超上限 {MAX_CELLS}")
    # 行尾补空单元格，保证矩阵规整（空单元格在 sheet 里直接跳过，不占体积）
    for cells in matrix:
        if len(cells) < col_count:
            cells.extend(("empty", "", False) for _ in range(col_count - len(cells)))

    return _pack_xlsx(matrix), len(matrix), col_count


def _pack_xlsx(matrix: list[list[tuple]]) -> bytes:
    """拼装 sheet XML + 共享字符串表，压缩为 xlsx 包。"""
    shared: list[str] = []          # 共享字符串去重表
    shared_index: dict[str, int] = {}
    sheet_rows: list[str] = []
    for r, cells in enumerate(matrix, start=1):
        row_xml: list[str] = []
        for c, (kind, text, is_error) in enumerate(cells, start=1):
            if not text:
                continue  # 空单元格直接省略（t/s 均无需引用）
            col_name = _col_letter(c - 1)
            style = _STYLE_ERROR if is_error else (_STYLE_HEADER if r == 1 else _STYLE_DEFAULT)
            if kind == "number":
                ref = f'{col_name}{r}'
                # 表头即便为数字也按字符串展示，避免序号列变成数值列语义
                if r == 1:
                    sid = _shared_index(shared, shared_index, text)
                    row_xml.append(f'<c r="{ref}" s="{style}" t="s"><v>{sid}</v></c>')
                else:
                    row_xml.append(f'<c r="{ref}" s="{style}"><v>{text}</v></c>')
            else:
                sid = _shared_index(shared, shared_index, _escape_text(text))
                ref = f"{col_name}{r}"
                row_xml.append(f'<c r="{ref}" s="{style}" t="s"><v>{sid}</v></c>')
        if row_xml:
            sheet_rows.append(f'<row r="{r}">' + "".join(row_xml) + "</row>")

    sheet_xml = (
        _XML_DECL +
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(sheet_rows) + "</sheetData></worksheet>"
    )
    shared_xml = (
        _XML_DECL +
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t xml:space=\"preserve\">{item}</t></si>" for item in shared)
        + "</sst>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types_xml())
        zf.writestr("_rels/.rels", _root_rels_xml())
        zf.writestr("xl/workbook.xml", _workbook_xml())
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        zf.writestr("xl/styles.xml", _styles_xml())
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _shared_index(shared: list, index_map: dict, text: str) -> int:
    """共享字符串索引（同串复用一条记录）。"""
    if text not in index_map:
        index_map[text] = len(shared)
        shared.append(text)
    return index_map[text]


def _escape_text(text: str) -> str:
    """XML 转义 + 把换行转义为 _x000A_（Excel 单元格内换行约定）。"""
    text = escape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "_x000A_")


def _col_letter(zero_based: int) -> str:
    """0 → A，25 → Z，26 → AA ...（xlsx 列名）"""
    letters = ""
    index = zero_based
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            return letters


def _content_types_xml() -> str:
    return (
        _XML_DECL +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        _XML_DECL +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_xml() -> str:
    return (
        _XML_DECL +
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )


def _workbook_rels_xml() -> str:
    return (
        _XML_DECL +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    """三款样式：0 默认 / 1 表头加粗 / 2 错误占位（灰底红字）。"""
    return (
        _XML_DECL +
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        # 字体：0 普通 / 1 加粗（黑） / 2 红色
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><color rgb="FFC00000"/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        # 填充：0 无 / 1 灰底
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEAEAEA"/><bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        # 边框：0 无
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="1" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
