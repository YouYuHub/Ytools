# coding: utf-8
"""V2 文件历史版本链 REST 接口（docs/file_diff.md §9）。

前端「文件变更统计 + diff 编辑器」的数据出口：
- 读：文件列表 / 版本内容 / 版本时间线 / 总览 diff / 单次 diff；
- 写：hunk 撤回 / 单文件回退 / 编辑器保存 / 保留封版 / 删除跟踪。

所有接口按 session_id 隔离（history_files/session_files/<session>/file_diffs/）。
"""
# fastapi 库导入
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

# 自定义模块导入
from memory import file_history as store

# 创建 API 路由器实例
api_file_history_router = APIRouter(prefix="/file_diff")


class HunkUndoRequest(BaseModel):
    session_id: str = "default"
    key: str
    hunk_index: int
    until_hunk: bool = False


class HunkKeepRequest(BaseModel):
    session_id: str = "default"
    key: str
    hunk_index: int
    until_hunk: bool = False


class RollbackRequest(BaseModel):
    session_id: str = "default"
    key: str
    to_version: Optional[int] = None
    to_round: Optional[int] = None
    target: str = "baseline"


class SaveRequest(BaseModel):
    session_id: str = "default"
    key: str
    content: str
    expected_hash: str


class KeepRequest(BaseModel):
    session_id: str = "default"
    key: str


class CleanupRequest(BaseModel):
    session_id: str = "default"
    clean_only: bool = True


def _store_call(func, *args, **kwargs):
    """统一错误转换：KeyError→404、PermissionError→409、ValueError→400。"""
    try:
        return func(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@api_file_history_router.get("/list")
async def list_changed_files(
    session_id: str = "default",
    hide_clean: bool = True,
):
    """会话内被改文件列表（主页面统计区）：{files: [...], stats: {...}}。

    hide_clean=True（默认）时隐藏"已全部保留/全部撤回"的文件（Total Diff 无
    行数变化）；这类文件的版本链留档仍在磁盘，可用 DELETE /file_diff/delete 清理。
    """
    files = store.list_files(session_id, hide_clean=hide_clean)
    stats = {
        "total": len(files),
        "added": sum(int(item.get("added", 0)) for item in files),
        "removed": sum(int(item.get("removed", 0)) for item in files),
    }
    return JSONResponse(content={"files": files, "stats": stats})


@api_file_history_router.get("/versions")
async def list_versions(session_id: str = "default", key: str = ""):
    """单文件版本时间线（回退目标选择 / 编辑器侧栏）。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    versions = _store_call(store.versions_of, session_id, key.strip())
    return JSONResponse(content={"key": key.strip(), "versions": versions})


@api_file_history_router.get("/content")
async def get_content(
    session_id: str = "default",
    key: str = "",
    v: Optional[int] = None,
):
    """读取某个版本的全文内容（缺省 v=最新）。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    data = _store_call(store.read_content, session_id, key.strip(), v)
    return JSONResponse(content=data)


@api_file_history_router.get("/total_diff")
async def get_total_diff(session_id: str = "default", key: str = ""):
    """Total Diff：diff(当前代基线, 当前内容)——"这轮任务总共改了什么"。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    data = _store_call(store.total_diff, session_id, key.strip())
    return JSONResponse(content=data)


@api_file_history_router.get("/full_view")
async def get_full_view(session_id: str = "default", key: str = "", max_rows: Optional[int] = None):
    """全文视图：基线全文 + 行级差异标记（rows/hunks），供前端 VS Code 式内联 diff。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    data = _store_call(store.full_view, session_id, key.strip(), max_rows=max_rows)
    return JSONResponse(content=data)


@api_file_history_router.get("/change_diff")
async def get_change_diff(session_id: str = "default", key: str = "", v: int = 0):
    """单次修改 Diff：版本 v 相对同代上一版本——"这一刀改了什么"。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    data = _store_call(store.single_diff, session_id, key.strip(), int(v))
    return JSONResponse(content=data)


@api_file_history_router.post("/hunk_undo")
async def undo_hunk(request: HunkUndoRequest):
    """撤回差异块（until_hunk=True 时撤回此处及之后，磁盘同步写回）。"""
    data = _store_call(
        store.hunk_undo,
        request.session_id,
        request.key.strip(),
        int(request.hunk_index),
        until_hunk=bool(request.until_hunk),
    )
    return JSONResponse(content=data)


@api_file_history_router.post("/hunk_keep")
async def keep_hunk(request: HunkKeepRequest):
    """保留差异块（until_hunk=True 时保留此处及之后），其余还原并固化为新代基线。"""
    data = _store_call(
        store.hunk_keep,
        request.session_id,
        request.key.strip(),
        int(request.hunk_index),
        until_hunk=bool(request.until_hunk),
    )
    return JSONResponse(content=data)


@api_file_history_router.post("/sync")
async def sync_from_disk(request: KeepRequest):
    """从磁盘刷新：把外部（VS Code 等）对该文件的最新修改并入版本链并重算 diff。"""
    data = _store_call(store.sync_from_disk, request.session_id, request.key.strip())
    return JSONResponse(content=data)


@api_file_history_router.post("/rollback")
async def rollback_file(request: RollbackRequest):
    """单文件回退：target=baseline / to_version=N / to_round=N（到某轮发起时状态）。"""
    data = _store_call(
        store.rollback,
        request.session_id,
        request.key.strip(),
        to_version=request.to_version,
        to_round=request.to_round,
        target=request.target,
    )
    return JSONResponse(content=data)


@api_file_history_router.post("/save")
async def save_edited_file(request: SaveRequest):
    """编辑器保存：绿色区域编辑后的全文落盘 + 入链（expected_hash 乐观锁）。"""
    data = _store_call(
        store.user_save,
        request.session_id,
        request.key.strip(),
        request.content,
        request.expected_hash,
    )
    return JSONResponse(content=data)


@api_file_history_router.post("/keep")
async def keep_file(request: KeepRequest):
    """保留封版：当前代锁定（不可再撤回），以当前内容开新代基线。"""
    data = _store_call(store.keep, request.session_id, request.key.strip())
    return JSONResponse(content=data)


@api_file_history_router.delete("/delete")
async def delete_tracked_file(session_id: str = "default", key: str = ""):
    """删除单个文件的版本链目录（停止跟踪该文件的变更）。"""
    if not key.strip():
        raise HTTPException(status_code=400, detail="key 不能为空")
    removed = _store_call(store.delete_file_history, session_id, key.strip())
    return JSONResponse(content={"deleted": removed, "key": key.strip()})


@api_file_history_router.post("/cleanup")
async def cleanup_histories(request: CleanupRequest):
    """批量清理留档：clean_only=True 只清已全部保留/撤回（无行数变化）的文件链。"""
    data = _store_call(store.cleanup_file_histories, request.session_id, clean_only=request.clean_only)
    return JSONResponse(content=data)
