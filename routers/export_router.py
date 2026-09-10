# coding: utf-8
"""表格导出路由：md 表格（原始文本）→ xlsx 下载

前端把渲染时保存的原始 md 表格文本（| a | b | 分隔符行 | ...，单表包含
表头/分隔/数据行）POST 过来，后端负责完整解析（转义竖线、行内代码、
对齐符号），再由 factory/xlsx_export 生成 xlsx 字节流返回下载。
"""
# fastapi 库导入
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from urllib.parse import quote

# 自定义模块导入
from factory.md_table_export import parse_md_table, matrix_to_xlsx_bytes

# 创建 API 路由器实例
api_export_router = APIRouter(prefix="/export")


class TableExportRequest(BaseModel):
    """导出请求体：markdown 为单张 md 表格的原始文本"""
    markdown: str = Field(min_length=1, max_length=200_000, description="md 表格原始文本")
    filename: str | None = Field(default=None, max_length=60, description="下载文件名（不含扩展名，可省略）")

    @field_validator("filename")
    @classmethod
    def _clean_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = "".join(ch for ch in value.strip() if ch not in '\\/:*?"<>|\r\n\t')
        return cleaned[:60] or None


@api_export_router.post("/table/xlsx", tags=["Export"], summary="导出 md 表格为 xlsx 下载")
async def export_table_xlsx(payload: TableExportRequest) -> Response:
    """解析 md 表格文本并生成 xlsx；解析失败返回 422 + 中文原因。"""
    markdown = payload.markdown
    if "|" not in markdown:
        raise HTTPException(status_code=422, detail="不是有效的 md 表格文本")
    try:
        parsed = parse_md_table(markdown)
    except ValueError as parse_error:
        raise HTTPException(status_code=422, detail=str(parse_error))
    if parsed is None:
        raise HTTPException(status_code=422, detail="不是有效的 md 表格文本")

    try:
        xlsx_bytes, row_count, col_count = matrix_to_xlsx_bytes(parsed)
    except ValueError as value_error:
        raise HTTPException(status_code=422, detail=str(value_error))
    except Exception as build_error:
        raise HTTPException(status_code=500, detail=f"xlsx 生成失败: {build_error}")

    filename = (payload.filename or "table") + ".xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"table.xlsx\"; filename*=UTF-8''{quote(filename)}"
            ),
            "X-Export-Rows": str(row_count),
            "X-Export-Cols": str(col_count),
        },
    )
