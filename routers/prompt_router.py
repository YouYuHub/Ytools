"""提示词库（Skills）接口：管理 prompt/md_files/ 下的 Markdown 提示词文件。

前端 Skills 对话框通过这组接口完成新建 / 列表 / 读取 / 保存 / 重命名 / 删除；
文件本身也可在文件系统中直接编辑，两侧行为一致（读盘即最新内容）。
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prompt import prompt_manager

# 实例化APIRouter
api_prompt_router = APIRouter(prefix="/prompts")


class PromptNameRequest(BaseModel):
    name: str


class PromptCreateRequest(PromptNameRequest):
    content: str = ""


class PromptSaveRequest(PromptNameRequest):
    content: str = ""


class PromptRenameRequest(BaseModel):
    old_name: str
    new_name: str


def _convert_error(exc: Exception) -> HTTPException:
    """把 prompt_manager 的领域异常映射为 HTTP 语义。"""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=f"提示词文件不存在: {exc}")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=f"同名提示词文件已存在: {exc}")
    return HTTPException(status_code=500, detail=f"提示词文件操作失败: {exc}")


@api_prompt_router.get("/list")
async def api_list_prompts():
    """列出全部提示词文件（按更新时间倒序）。

    Returns:
        {"prompts": [{"name", "size_bytes", "updated_at"}, ...], "total": 数量}
    """
    try:
        prompts = prompt_manager.list_prompts()
        return {"prompts": prompts, "total": len(prompts)}
    except OSError as e:
        raise _convert_error(e)


@api_prompt_router.get("/read")
async def api_read_prompt(name: str = Query(..., description="提示词文件名（可省略 .md 后缀）")):
    """读取单个提示词的 Markdown 原文。"""
    try:
        return prompt_manager.read_prompt(name)
    except (ValueError, FileNotFoundError, OSError) as e:
        raise _convert_error(e)


@api_prompt_router.post("/create")
async def api_create_prompt(req: PromptCreateRequest):
    """新建提示词文件（同名返回 409）。content 可选，默认空文件。"""
    try:
        return prompt_manager.create_prompt(req.name, req.content)
    except (ValueError, FileExistsError, OSError) as e:
        raise _convert_error(e)


@api_prompt_router.post("/save")
async def api_save_prompt(req: PromptSaveRequest):
    """保存提示词内容（存在则覆盖，不存在则创建）。"""
    try:
        return prompt_manager.save_prompt(req.name, req.content)
    except (ValueError, OSError) as e:
        raise _convert_error(e)


@api_prompt_router.post("/rename")
async def api_rename_prompt(req: PromptRenameRequest):
    """重命名提示词文件（目标同名返回 409）。"""
    try:
        return prompt_manager.rename_prompt(req.old_name, req.new_name)
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as e:
        raise _convert_error(e)


@api_prompt_router.post("/delete")
async def api_delete_prompt(req: PromptNameRequest):
    """删除提示词文件。"""
    try:
        prompt_manager.delete_prompt(req.name)
        return {"state": "succeed", "name": req.name}
    except (ValueError, FileNotFoundError, OSError) as e:
        raise _convert_error(e)
