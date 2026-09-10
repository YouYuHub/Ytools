# coding: utf-8
"""md 表格文本解析 → xlsx 导出的桥接层

与 H5/js/markdown.js 的表格解析保持同一语义：
  - 表头 + 分隔行（|---|:---:|）识别；
  - 转义竖线 \\| 与行内代码 `a | b` 内的竖线不切断分列；
  - 单元格去除 md 行内标记（**粗体**、*斜体*、`行内代码`、[链接](url)），
    还原纯文本；链接优先保留 URL（便于 Excel 直接使用）；
  - 单元格清洗异常不拖垮整表：写入占位说明并在 Excel 中套错误样式
    （显示"原值（Excel 导出失败：原因）"）。
"""
import re

from factory.xlsx_export import build_xlsx_bytes

# 行内代码/转义竖线在分列前先掩码（与前端 markdown.js 同款占位符约定）
PH_OPEN = "\x01"
PH_CLOSE = "\x02"
_PLACEHOLDER_USE_RE = re.compile(PH_OPEN + r"(\d+)" + PH_CLOSE)

# 行内标记清理（顺序敏感：链接 → 粗体 → 斜体 → 反引号）
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_CODE_TICKS_RE = re.compile(r"`{1,3}")

_MASK_RE = re.compile(r"`[^`]*`|\\\|")

# 导出上限（与前端表格复杂度匹配的合理量级）
MAX_ROWS = 5000
MAX_COLS = 64
MAX_CELLS = 200_000


def _mask_inline(line: str) -> tuple[str, list[str]]:
    """行内代码与转义竖线掩码为 PHi 占位，防止分列时被竖线切断。"""
    placeholders: list[str] = []

    def _keep(match: re.Match) -> str:
        placeholders.append(match.group(0))
        return PH_OPEN + str(len(placeholders) - 1) + PH_CLOSE

    return _MASK_RE.sub(_keep, line), placeholders


def _split_row(line: str) -> list[str]:
    """一行 md 表格文本 → 单元格纯文本列表（已还原掩码 + 去行内标记）。"""
    masked, placeholders = _mask_inline(line.strip())
    cells = masked.split("|")
    # 去掉行首/行尾分隔竖线产生的空单元格（中间空单元格是合法空列，保留）
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]

    cleaned: list[str] = []
    for cell in cells:
        raw = _PLACEHOLDER_USE_RE.sub(
            lambda m: _restore_placeholder(placeholders[int(m.group(1))]),
            cell.strip(),
        )
        cleaned.append(_strip_inline_markup(raw))
    return cleaned


def _restore_placeholder(placeholder: str) -> str:
    """还原掩码占位：转义竖线（反斜杠+竖线）还原为竖线（与前端渲染一致）；行内代码原样保留。"""
    if placeholder.startswith("\\") and len(placeholder) >= 2:
        return placeholder[1:]
    return placeholder


def _strip_inline_markup(text: str) -> str:
    """去除行内 md 标记；链接还原为 URL（http/https 时）或标签文本。"""

    def _link_sub(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        return url if url.startswith(("http://", "https://")) else (label or url)

    text = _MD_LINK_RE.sub(_link_sub, text)
    text = _MD_BOLD_RE.sub(lambda m: m.group(1), text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_CODE_TICKS_RE.sub("", text)
    return text.strip()


def parse_md_table(markdown: str) -> tuple[list[list[str]], dict[tuple, str]]:
    """
    解析 md 表格文本。
    返回 (matrix, cell_errors)：
      matrix      - 规整二维数组（首行为表头；行尾缺失列已补空串）
      cell_errors - {(r, c): 错误说明}，生成 xlsx 时套错误占位样式
    非法表格（无分隔行等）抛 ValueError，由路由转为 422。
    """
    lines = [line for line in markdown.replace("\r\n", "\n").split("\n") if line.strip()]
    if len(lines) < 2:
        raise ValueError("表格文本不足两行（至少需要表头行 + 分隔行）")

    header = _split_row(lines[0])
    divider_masked, _ = _mask_inline(lines[1].strip())
    divider_cells = [cell.strip() for cell in divider_masked.split("|")]
    if divider_cells and divider_cells[0] == "":
        divider_cells = divider_cells[1:]
    if divider_cells and divider_cells[-1] == "":
        divider_cells = divider_cells[:-1]
    if not divider_cells or not all(
        re.fullmatch(r":?-{2,}:?", cell) for cell in divider_cells
    ):
        raise ValueError("第二行不是 md 表格分隔行（形如 |---|---|）")

    rows: list[list[str]] = [header]
    cell_errors: dict[tuple, str] = {}
    for line in lines[2:]:
        row_cells: list[str] = []
        try:
            row_cells = _split_row(line)
        except Exception as strip_error:  # 防御：掩码/占位损坏时不丢整行
            placeholders: list[str] = []
            masked, placeholders = _mask_inline(line.strip())
            for c, cell in enumerate(masked.split("|")):
                row_cells.append(cell.strip())
                cell_errors[(len(rows), c)] = f"单元格清洗失败（{strip_error}）"
        rows.append(row_cells)

    if len(rows) > MAX_ROWS or (rows and len(rows[0]) > MAX_COLS):
        raise ValueError(f"表格规模超限（行 ≤ {MAX_ROWS}, 列 ≤ {MAX_COLS}）")

    col_count = max(len(row) for row in rows)
    if col_count * len(rows) > MAX_CELLS:
        raise ValueError(f"单元格总数超上限 {MAX_CELLS}")
    for row in rows:
        row.extend([""] * (col_count - len(row)))
    return rows, cell_errors


def matrix_to_xlsx_bytes(parsed: tuple[list[list[str]], dict[tuple, str]]) -> tuple[bytes, int, int]:
    """把 parse_md_table 结果打包为 xlsx；错误单元格套错误占位样式。"""
    matrix, cell_errors = parsed
    export_rows: list[list] = []
    for r, row in enumerate(matrix):
        export_row: list = []
        for c, value in enumerate(row):
            reason = cell_errors.get((r, c))
            export_row.append({"error": reason, "raw": value} if reason else value)
        export_rows.append(export_row)
    return build_xlsx_bytes(export_rows)
