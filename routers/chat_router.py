import asyncio
import io
import json
from typing import Any, Optional, List
import urllib.parse
# fastapi 库导入
from fastapi import APIRouter, Query, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel

# 自定义模块导入
from config import ChatLLMRequest
from env_manager import (
    ChatModelConfigurationError,
    apply_role_parameter_defaults,
    reset_ambient_model_selection,
    set_ambient_model_selection,
)
from factory.chat_factory import (
    build_runtime_system_text,
    tool_chat_server,
    stop_chat_task,
    is_chat_stream_running,
)
from factory.session_worker import peek_worker_proxy
from factory.agent_runtime.context_compaction import (
    compact_session_history_if_needed,
    load_context_compaction_settings,
    resolve_summary_total_budget,
)
from factory.agent_runtime import tool_registry
from factory.agent_runtime.builtin_tools import (
    ASK_USER_TOOL_NAME,
    EDIT_FILE_NAME,
    READ_FILE_NAME,
    READ_MEDIA_NAME,
    SEARCH_FILES_NAME,
    SELECTABLE_BUILTIN_TOOL_NAMES,
    TODO_TOOL_NAME,
    WRITE_FILE_NAME,
    inject_builtin_tools,
)
from memory.chat_memory import (
    ChatMemoryManager,
    get_chat_memory_manager,
    normalize_session_id,
    cleanup_chat_memory_manager,
    chat_memory_file_exists,
    resolve_session_work_dir,
    resolve_session_model_selection,
    export_sessions_to_zip,
    list_zip_sessions,
    import_sessions_from_zip,
    IMPORT_PACKAGE_MAX_BYTES,
    list_session_groups,
    list_session_group_assignments,
    create_session_group,
    update_session_group,
    delete_session_group,
    assign_session_group,
)
from memory.file_memory import (
    cleanup_file_memory_manager,
    count_session_file_memory,
)
from routers.chat_config_router import get_chat_work_dir_config

# 创建 API 路由器实例
api_chat_router = APIRouter()


def _tool_definition_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function_info = tool.get("function")
    if not isinstance(function_info, dict):
        return ""
    name = function_info.get("name")
    return name.strip() if isinstance(name, str) else ""


# 聊天主接口
@api_chat_router.post('/chat_with_tool')
async def chat_with_tool(request: Request):
    """ 用户聊天信息，流式响应

    参数优先级：请求体显式传参 > 会话/全局 model_selection.chat_model.parameter > 默认值。
    未显式提供的生成参数按 select 接口配置的 chat_model 参数自动填充；
    会话已独立选择模型时（_meta.model_selection），按会话生效模型的参数填充。
    """
    body = await request.json()
    # 会话级模型选择：参数默认值填充按会话生效的 chat_model parameter
    # （失效覆盖按角色回退全局；警告由生成任务内的 MODEL_SELECTION_FALLBACK 事件统一提示）
    ambient_token = None
    raw_session_id = body.get("session_id") if isinstance(body, dict) else None
    if raw_session_id:
        try:
            effective_selection, _ = resolve_session_model_selection(normalize_session_id(raw_session_id))
            ambient_token = set_ambient_model_selection(effective_selection)
        except Exception:
            ambient_token = None
    try:
        tool_request = ChatLLMRequest(**apply_role_parameter_defaults(body))
    finally:
        if ambient_token is not None:
            reset_ambient_model_selection(ambient_token)
    return StreamingResponse(tool_chat_server(tool_request), media_type='text/event-stream')


# 停止当前聊天任务接口
@api_chat_router.post('/stop_chat')
async def stop_chat(session_id: str = "default"):
    """ 停止当前聊天任务 """
    try:
        await stop_chat_task(normalize_session_id(session_id))
        return {'stop_chat': 'stopped'}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.post('/inject_message')
async def inject_message(request: Request):
    """运行中注入用户消息（消息引导）。

    任务运行中：把消息投递给会话 worker，生成循环在下一轮检查点（工具结果
    处理完毕后）取出并作为新一轮用户消息继续；任务未运行时返回 not_running，
    前端回退为普通发送。
    请求体：{"session_id": str, "content": 多模态部件列表或字符串}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    session_id = normalize_session_id(str((body or {}).get("session_id") or "default"))
    content = (body or {}).get("content")
    if isinstance(content, str):
        if not content.strip():
            raise HTTPException(status_code=400, detail="content 不能为空")
        message_content: Any = content
    elif isinstance(content, list) and content:
        message_content = content
    else:
        raise HTTPException(status_code=400, detail="content 必须为非空字符串或多模态部件列表")
    from factory.chat_factory import inject_user_message
    result = await inject_user_message(session_id, {"role": "user", "content": message_content})
    if not result.get("ok"):
        return JSONResponse(content={"ok": False, "reason": result.get("reason", "not_running")})
    return JSONResponse(content={"ok": True})


@api_chat_router.post('/cancel_inject_message')
async def cancel_inject_message(request: Request):
    """撤回一条尚未消费的运行中注入消息（消息引导提示行 ×）。

    按文本匹配从注入队列移除；已被生成循环消费时返回 ok=False（不可撤回）。
    请求体：{"session_id": str, "text": str}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    session_id = normalize_session_id(str((body or {}).get("session_id") or "default"))
    text = str((body or {}).get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    from factory.chat_factory import cancel_injected_message
    result = await cancel_injected_message(session_id, text)
    if not result.get("ok"):
        return JSONResponse(content={"ok": False, "reason": result.get("reason", "not_found")})
    return JSONResponse(content={"ok": True})


@api_chat_router.get('/chat_stream/status')
async def chat_stream_status(session_id: str = "default"):
    """
    查询指定会话当前是否仍在后台生成（前端刷新后据此决定是否重连 SSE）
    返回:
        {"running": bool}
    """
    try:
        normalized_session_id = normalize_session_id(session_id)
        running = is_chat_stream_running(normalized_session_id)
        return JSONResponse(content={"running": running, "session_id": normalized_session_id})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.get('/chat_history/sessions')
async def list_chat_history_sessions():
    """
    列出所有聊天历史会话文件
    返回:
        会话文件名列表
    """
    try:
        sessions = ChatMemoryManager.list_chat_sessions()
        return JSONResponse(content=[session.name for session in sessions])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    
@api_chat_router.get('/chat_history/file')
async def get_chat_history_file(session_id: str = "default"):
    """
    下载指定会话的聊天历史文件
    参数:
        session_id: 会话ID
    返回:
        文件响应
    """
    session_id = normalize_session_id(session_id)
    manager = await get_chat_memory_manager(session_id)
    text = await manager.get_file_text()
    if text is None:
        raise HTTPException(status_code=404, detail="Chat session file not found")
    return Response(content=text, media_type="text/plain; charset=utf-8")


@api_chat_router.get('/chat_history/meta')
async def get_chat_history_meta(session_id: str = "default"):
    """
    获取指定会话的聊天历史元数据（jsonl 首行 _meta）
    参数:
        session_id: 会话ID
    返回:
        元数据字典
    """
    try:
        manager = await get_chat_memory_manager(normalize_session_id(session_id))
        meta = await manager.get_session_meta()
        return JSONResponse(content=meta)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.put('/chat_history/title')
async def update_chat_history_title(
    title: str = Query(..., description="新的会话标题", min_length=1),
    session_id: str = "default"
):
    """
    更新指定会话的聊天历史标题（jsonl 首行 _meta 的 title 字段）
    参数:
        title: 新的会话标题
        session_id: 会话ID
    返回:
        操作结果
    """
    try:
        session_id = normalize_session_id(session_id)
        manager = await get_chat_memory_manager(session_id)
        meta = await manager.update_session_title(title)
        return JSONResponse(content={
            "state": "succeed",
            "describe": f"已更新会话 [{session_id}] 的标题",
            "title": meta.get("title"),
            "meta": meta,
        })
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.delete('/chat_history/delete_file')
async def delete_chat_history(session_id: str = "default"):
    """
    删除指定会话的聊天历史文件，并连同该会话上传文件所在目录一并删除。

    上传目录名优先取 jsonl 首行 _meta 记录的 upload_id，旧记录无该字段时
    回退为按 session_id 推导的目录名；任一侧删除失败都会返回 500，
    前端据此提示删除失败。
    参数:
        session_id: 会话ID
    返回:
        操作结果
    """
    session_id = normalize_session_id(session_id)
    try:
        result = ChatMemoryManager.delete_chat_session_file(session_id)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除会话文件失败: {e}")
    # 清理缓存的管理器实例，避免下次操作时重建已删除的目录/文件
    await cleanup_file_memory_manager(session_id)
    await cleanup_chat_memory_manager(session_id)
    return JSONResponse(content=result)


@api_chat_router.delete("/chat_history/delete_lines")
async def delete_chat_history_lines(
    startline: int = Query(..., description="要删除的起始行号(1-based)", ge=1),
    endline: int = Query(..., description="要删除的结束行号(1-based,包含)", ge=1),
    session_id: str = "default"
):
    """
    删除指定会话的聊天历史文件中的指定行范围
    参数:
        startline: 要删除的起始行号(从1开始)
        endline: 要删除的结束行号(从1开始,包含该行)
        session_id: 会话ID
    返回:
        操作结果
    """
    if startline > endline:
        raise HTTPException(status_code=400, detail=f"起始行号({startline})不能大于结束行号({endline})")
    try:
        result = ChatMemoryManager.delete_chat_session_file_line(
            normalize_session_id(session_id), startline, endline
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result)


class MediaTagRemoveRequest(BaseModel):
    """删除消息媒体伪标签的请求体。"""
    tag: str
    replacement: str = "用户已删除/文件不存在"
    session_id: str = "default"


@api_chat_router.post("/chat_history/remove_media_tag")
async def remove_media_tag_from_history(payload: MediaTagRemoveRequest):
    """
    在会话历史 JSONL 中把指定媒体伪标签原文替换为占位说明文本。

    前端删除消息中渲染的 <image>/<audio>/<video>/<pdf> 控件时调用：
    tag 为模型输出的完整标签原文（前端渲染时随控件保存），替换后该轮
    历史回传给模型时只看到占位说明，不再引用已删除/不存在的文件。
    """
    tag = (payload.tag or "").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="tag 不能为空")
    try:
        result = ChatMemoryManager.replace_content_in_chat_session(
            normalize_session_id(payload.session_id),
            tag,
            payload.replacement or "用户已删除/文件不存在",
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result)


class SessionGroupCreateRequest(BaseModel):
    """新建会话分组的请求体。"""
    name: str


class SessionGroupUpdateRequest(BaseModel):
    """更新会话分组（重命名 / 折叠状态）的请求体；None 字段表示不修改。"""
    name: Optional[str] = None
    collapsed: Optional[bool] = None


class SessionGroupAssignRequest(BaseModel):
    """会话归组请求体；group_id 为 None/空 表示移出分组。"""
    session_id: str = "default"
    group_id: Optional[str] = None


@api_chat_router.get("/chat_history/groups")
async def list_chat_session_groups():
    """列出全部会话分组 + 会话归属映射。

    返回:
        {"groups": [...], "assignments": {session_id: group_id}}
        分组定义在 history_files/session_groups.json；归属真源在各会话
        JSONL 首行 _meta.group_id（扫描时只读首行，损坏文件跳过）。
    """
    try:
        groups = list_session_groups()
        assignments = list_session_group_assignments()
        return JSONResponse(content={"groups": groups, "assignments": assignments})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.post("/chat_history/groups")
async def create_chat_session_group(payload: SessionGroupCreateRequest):
    """新建会话分组；同名分组已存在时直接返回既有分组（幂等）。"""
    try:
        group = create_session_group(payload.name)
        return JSONResponse(content={"state": "succeed", "group": group})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_chat_router.put("/chat_history/groups/{group_id}")
async def update_chat_session_group(group_id: str, payload: SessionGroupUpdateRequest):
    """重命名分组 / 更新折叠状态。"""
    try:
        group = update_session_group(
            group_id,
            name=payload.name,
            collapsed=payload.collapsed,
        )
        return JSONResponse(content={"state": "succeed", "group": group})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_chat_router.delete("/chat_history/groups/{group_id}")
async def delete_chat_session_group(group_id: str):
    """删除分组并解除其全部成员会话的归属（成员回到「未分组/最近」）。"""
    try:
        result = delete_session_group(group_id)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_chat_router.put("/chat_history/group_assign")
async def assign_chat_session_group(payload: SessionGroupAssignRequest):
    """把会话加入分组 / 移出分组（group_id 为 None 或空串 = 移出）。"""
    try:
        result = assign_session_group(payload.session_id, payload.group_id)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteRoundsRequest(BaseModel):
    """按轮次号删除历史轮次的请求体（用户消息编辑重发 / 整轮删除）。"""
    session_id: str = "default"
    start_round: int
    mode: str = "truncate"        # truncate=该轮及之后全删；single=仅删该轮整轮
    delete_files: bool = True     # 是否连带清理该范围内不再被引用的用户上传附件
    dry_run: bool = False         # 预演：只返回明细不写盘
    keep_media_refs: list[str] = []  # 清理时排除的媒体 stored_name（编辑重发复用的附件）


def _session_generation_busy(session_id: str) -> bool:
    """判断会话生成任务是否正在运行（编辑删除属破坏性操作，运行中必须拒绝）。

    双路径判断：worker 模式查进程代理的生成态；inline 模式查会话流任务；
    ChatMemoryManager.run_task 作为兜底（inline 任务收尾时置 False）。
    """
    proxy = peek_worker_proxy(session_id)
    if proxy is not None and proxy.is_generation_running():
        return True
    if is_chat_stream_running(session_id):
        return True
    try:
        manager = get_chat_memory_manager(session_id)
    except Exception:
        return False
    # run_task 在任务开始时置 True；主进程缓存实例的兜底判断
    return bool(getattr(manager, "run_task", False))


@api_chat_router.post("/chat_history/delete_rounds")
async def delete_chat_history_rounds(payload: DeleteRoundsRequest):
    """
    按轮次号删除会话历史轮次，支持两种模式：

    - truncate：删除该轮及其后所有轮次（编辑消息重发的 GPT 同款语义）；
    - single：仅删除该轮整轮（用户消息与回复一并删除，后续轮次保留，
      轮次号自动前移）。

    可选清理用户上传附件：媒体（media/ 图片/视频/音频）按引用计数差集
    精确判定（被删轮次引用 − 保留轮次引用），文档（files/）按上传时间窗
    近似判定；两者都只在「保留内容不再引用」时删除。

    dry_run=True 时只返回将删除的轮次与文件明细（state="planned"），
    前端展示确认弹窗后以 dry_run=False 正式执行；正式执行前会先写
    <历史文件>.bak 侧车备份。
    """
    session_id = normalize_session_id(payload.session_id)
    # 生成任务运行中拒绝删除：pending 轮次在 worker 内存里，此刻截断会
    # 交错写入（删除后任务收尾 append 会把已删轮次之后的轮次又接回来）
    if _session_generation_busy(session_id):
        raise HTTPException(
            status_code=409,
            detail="该会话正在生成回复，请等待完成或停止后再编辑/删除轮次",
        )
    try:
        manager = await get_chat_memory_manager(session_id)
        result = await manager.delete_rounds(
            start_round=payload.start_round,
            mode=payload.mode,
            delete_files=payload.delete_files,
            dry_run=payload.dry_run,
            keep_media_refs=payload.keep_media_refs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result)


# 上传 jsonl 历史文件的最大尺寸（20MB，与单文件上传保持同一量级）
_UPLOAD_CHAT_FILE_MAX_BYTES = 20 * 1024 * 1024


@api_chat_router.post('/chat_history/upload_chat_file')
async def upload_chat_history_file(
    file: UploadFile = File(..., description="要导入的 jsonl 聊天历史文件"),
    session_id: str = Query(
        "default",
        description="目标会话ID（可直接传文件名，后端会去掉 _chat.jsonl/.jsonl 后缀并规整）",
    ),
    overwrite: bool = Query(
        False,
        description="是否强制覆盖同名历史文件；False（默认）时同名文件自动追加时间戳另存",
    ),
):
    """
    接收前端上传的 jsonl 聊天历史文件，按现有格式过滤数据后保存到 history_files 目录。

    - 会话标识以文件名为准：目标文件为 `<session_id>_chat.jsonl`；
    - 首行若为 `{"_meta": {...}}` 则作为基础元数据（其中 session_id 字段会被移除）；
    - 其余行通过 `parse_round_entry` 验证，只保留能正确解析为 `chat_round` 的条目；
    - 目标文件已存在且 overwrite=False（默认）时，自动追加时间戳另存为
      `<session_id>_<时间戳>_chat.jsonl`，返回的 session_id 为实际写入值；
    - 写入后通过 `_recompute_meta_from_entries` 重新计算 user_questions / usage / record_count / completion_count。
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="请提供有效的 jsonl 文件")
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="仅支持 .jsonl 格式文件")
    try:
        raw_bytes = await file.read()
    except Exception as read_error:
        raise HTTPException(status_code=400, detail=f"读取上传文件失败: {read_error}")
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if len(raw_bytes) > _UPLOAD_CHAT_FILE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制（最大{_UPLOAD_CHAT_FILE_MAX_BYTES // (1024 * 1024)}MB）",
        )
    session_id = normalize_session_id(session_id)
    try:
        import_result = ChatMemoryManager.import_jsonl_chat_history(
            session_id=session_id,
            raw_bytes=raw_bytes,
            overwrite=overwrite,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析 jsonl 失败: {exc}")
    return JSONResponse(content={
        "state": import_result.get("state", "succeed"),
        "session_id": import_result.get("session_id", session_id),
        "filename": import_result.get("filename", filename),
        "total_lines": import_result.get("total_lines", 0),
        "imported_rounds": import_result.get("imported_rounds", 0),
        "skipped_lines": import_result.get("skipped_lines", 0),
        "record_count": import_result.get("record_count", 0),
        "completion_count": import_result.get("completion_count", 0),
        "title": import_result.get("title"),
        "overwrite": import_result.get("overwrite", bool(overwrite)),
        "collision": import_result.get("collision", False),
        "meta": import_result.get("meta"),
    })


# ---------- 会话分享 / 多会话导入（zip / jsonl，两阶段冲突确认） ----------
# 导入文件大小上限：zip 包含媒体原字节，放宽到 512MB；单 jsonl 沿用 20MB
def _import_file_size_limit(filename: str) -> int:
    return IMPORT_PACKAGE_MAX_BYTES if filename.lower().endswith(".zip") else _UPLOAD_CHAT_FILE_MAX_BYTES


def _read_upload_bytes(file: UploadFile, max_bytes: int, label: str) -> bytes:
    """读取上传文件字节流并校验非空/大小上限。"""
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail=f"请提供有效的{label}")
    try:
        raw_bytes = file.file.read() if getattr(file, "file", None) is not None else asyncio.get_event_loop().run_until_complete(file.read())
    except Exception as read_error:
        raise HTTPException(status_code=400, detail=f"读取上传文件失败: {read_error}")
    if not raw_bytes:
        raise HTTPException(status_code=400, detail=f"上传的{label}为空")
    if len(raw_bytes) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制（最大 {max_bytes // (1024 * 1024)}MB）",
        )
    return raw_bytes


@api_chat_router.get('/chat_history/export_zip')
async def export_chat_sessions_zip(
    session_ids: str = Query(
        ...,
        description="要分享的会话 ID 列表，逗号分隔（单会话即为 zip 分享；至少 1 个，最多 100 个）",
    ),
):
    """
    把一个或多个会话打包为 zip 下载：每个会话一个 `<session>_chat.jsonl` +
    该会话在 session_files/ 下的全部上传数据（media/ 图片视频音频、files/ 文档、
    thumbs/ 缩略图、diffs/ 文件版本链）+ manifest.json 清单。

    单会话且该会话在 session_files/ 下没有任何附件数据、且不带分组归属时，
    直接返回 jsonl 明文（media_type: application/x-ndjson，Content-Disposition
    为 `<session_id>_chat.jsonl`）——前端按 Content-Type 区分落地方式；
    有附件（目录非空）或带分组归属时打 zip（单会话 `<session_id>_chat.zip`，
    manifest 携带 groups 分组定义）。
    多会话始终打 zip（`ytools_sessions_<时间戳>.zip`）。
    """
    ids = [sid.strip() for sid in (session_ids or "").split(",") if sid.strip()]
    try:
        # 单会话快捷路径：session_files 下无任何附件数据 → 只分享 jsonl 明文
        if len(ids) == 1:
            sid = normalize_session_id(ids[0])
            payload = _export_single_session_payload_for_share(sid)
            if payload is not None:
                quoted = urllib.parse.quote(payload["filename"])
                return Response(
                    content=payload["text"].encode("utf-8"),
                    media_type="application/x-ndjson",
                    headers={
                        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
                    },
                )
    except HTTPException:
        raise
    except Exception:
        pass  # 快捷路径失败回退 zip 打包流程
    try:
        zip_bytes, download_name, skipped = export_sessions_to_zip(ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打包会话失败: {exc}")
    quoted = urllib.parse.quote(download_name)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "X-Skipped-Sessions": urllib.parse.quote(",".join(skipped)),
    }
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


def _export_single_session_payload_for_share(session_id: str) -> dict[str, Any] | None:
    """单会话分享判定：会话文件存在且 session_files 附件目录为空时返回 jsonl 明文数据。

    附件目录判定与 _export_session_payload 同口径（目录名优先 _meta.upload_id）；
    目录不存在或递归枚举无文件（media/files/thumbs/diffs 均为空）→ 仅 jsonl。
    会话带分组归属（_meta.group_id）时同样返回 None 回退 zip——分组定义需随
    包携带（manifest.groups），导入端才能按组名还原归属。
    """
    from memory.chat_memory import _export_session_payload, HISTORY_ROOT, _safe_session_id

    payload = _export_session_payload(session_id)
    if payload is None:
        return None
    upload_dir_name = (
        _safe_session_id(payload["session_id"])
        if not payload.get("upload_dir_name")
        else payload["upload_dir_name"]
    )
    session_data_root = HISTORY_ROOT / "session_files" / upload_dir_name
    has_files = session_data_root.is_dir() and any(session_data_root.rglob("*"))
    has_group = bool(str(payload.get("group_id") or "").strip())
    if has_files or has_group:
        return None
    return {"filename": payload["filename"], "text": payload["text"]}


class ImportPreviewResponse(BaseModel):
    type: str  # zip / jsonl
    sessions: List[dict[str, Any]]
    conflicts: List[str]  # 与本地已有会话同名的 session_id 列表
    groups: List[dict[str, Any]] = []  # 随包携带的分组定义（zip v2；jsonl/旧包为空）


@api_chat_router.post('/chat_history/import_preview')
async def preview_chat_import(
    file: UploadFile = File(..., description="要导入的分享包（zip 或 jsonl）"),
):
    """
    导入预检（不落盘）：解析 zip/jsonl 并返回会话清单与本地冲突信息。

    - zip：逐会话返回 {session_id, title, imported_rounds, exists, media_count,
      group_id, group_name}，并附随包分组定义 groups（manifest v2）；
    - jsonl：按单会话解析（与 upload_chat_file 同一解析器），exists 标记本地同名会话。
    前端据 conflicts 弹出「覆盖 / 重命名」决策弹窗后调用 import_package 提交。
    """
    filename = (file.filename or "").strip()
    lower = filename.lower()
    if lower.endswith(".zip"):
        raw_bytes = _read_upload_bytes(file, IMPORT_PACKAGE_MAX_BYTES, "zip 分享包")
        try:
            preview = list_zip_sessions(raw_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        sessions = preview.get("sessions") or []
    elif lower.endswith(".jsonl"):
        raw_bytes = _read_upload_bytes(file, _UPLOAD_CHAT_FILE_MAX_BYTES, "jsonl 文件")
        from memory.chat_memory import _parse_jsonl_payload, _get_chat_history_file, _safe_session_id

        try:
            base_meta, round_entries, total_lines, skipped_lines = _parse_jsonl_payload(raw_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        base_id = normalize_session_id(filename)
        exists = _get_chat_history_file(base_id).exists()
        upload_dir_name = ""
        if isinstance(base_meta, dict) and isinstance(base_meta.get("upload_id"), str):
            upload_dir_name = _safe_session_id(base_meta["upload_id"])
        sessions = [{
            "session_id": base_id,
            "filename": f"{base_id}_chat.jsonl",
            "title": (base_meta or {}).get("title") or base_id,
            "imported_rounds": len(round_entries),
            "skipped_lines": skipped_lines,
            "total_lines": total_lines,
            "exists": exists,
            "upload_dir_name": upload_dir_name,
            "media_count": 0,
        }]
    else:
        raise HTTPException(status_code=400, detail="仅支持 .zip 分享包或 .jsonl 历史文件")
    conflicts = [s["session_id"] for s in sessions if s.get("exists")]
    return JSONResponse(content={
        "type": "zip" if lower.endswith(".zip") else "jsonl",
        "filename": filename,
        "total": len(sessions),
        "sessions": sessions,
        "conflicts": conflicts,
        # 随包携带的分组定义（zip manifest v2；jsonl / 旧包为空列表）
        "groups": (preview.get("groups") or []) if lower.endswith(".zip") else [],
    })


class ImportSubmitRequest(BaseModel):
    """导入提交：随包附带的逐会话冲突决策。"""
    conflict_strategy: str = "ask"
    decisions: dict[str, str] = {}


@api_chat_router.post('/chat_history/import_package')
async def import_chat_package(
    file: UploadFile = File(..., description="要导入的分享包（zip 或 jsonl）"),
    conflict_strategy: str = Query(
        "ask",
        description="全局冲突策略：ask（默认，按 decisions 逐会话决策）/ overwrite / rename / skip",
    ),
    decisions: str = Query(
        "",
        description='逐会话决策 JSON：{"<session_id>": "overwrite|rename|skip"}',
    ),
):
    """
    提交导入（第二阶段）：按预检时用户选择的冲突策略写入会话。

    - zip 包：逐会话导入 jsonl + session_files 数据目录（冲突另存时同步改名
      上传目录归属并回写 _meta.upload_id）；分组归属按包内分组定义的「组名」
      映射还原（本地同名复用、缺失新建），导入结果附带 group_id / group_name；
    - jsonl：单会话导入，冲突策略与 zip 相同（overwrite 覆盖 / rename 另存）。
    返回 {state, imported, skipped, failed} 汇总。
    """
    filename = (file.filename or "").strip()
    lower = filename.lower()
    if lower.endswith(".zip"):
        raw_bytes = _read_upload_bytes(file, IMPORT_PACKAGE_MAX_BYTES, "zip 分享包")
        try:
            decisions_map = json.loads(decisions) if (decisions or "").strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"decisions 参数不是合法 JSON: {exc}")
        if not isinstance(decisions_map, dict):
            raise HTTPException(status_code=400, detail="decisions 参数必须是对象")
        try:
            result = await import_sessions_from_zip(
                raw_bytes,
                conflict_strategy=conflict_strategy,
                decisions=decisions_map,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse(content=result)
    if lower.endswith(".jsonl"):
        raw_bytes = _read_upload_bytes(file, _UPLOAD_CHAT_FILE_MAX_BYTES, "jsonl 文件")
        base_id = normalize_session_id(filename)
        strategy = (conflict_strategy or "ask").strip().lower()
        decision_map: dict[str, str] = {}
        try:
            parsed_decisions = json.loads(decisions) if (decisions or "").strip() else {}
            if isinstance(parsed_decisions, dict):
                decision_map = {str(k): str(v).strip().lower() for k, v in parsed_decisions.items()}
        except json.JSONDecodeError:
            decision_map = {}
        decision = decision_map.get(base_id) or (strategy if strategy != "ask" else "rename")
        # jsonl 单会话导入：复用管理器导入逻辑（与 upload_chat_file 相同，但由决策驱动）
        from memory.chat_memory import _import_jsonl_payload

        if decision == "skip":
            return JSONResponse(content={
                "state": "succeed",
                "imported": [],
                "skipped": [{"session_id": base_id, "reason": "conflict_skip"}],
                "failed": [],
            })
        if decision not in {"overwrite", "rename"}:
            raise HTTPException(status_code=400, detail=f"未知的冲突决策: {decision}")
        try:
            import_result = _import_jsonl_payload(base_id, raw_bytes, overwrite=(decision == "overwrite"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"解析 jsonl 失败: {exc}")
        await cleanup_chat_memory_manager(str(import_result.get("session_id") or base_id))
        return JSONResponse(content={
            "state": "succeed",
            "imported": [import_result],
            "skipped": [],
            "failed": [],
        })
    raise HTTPException(status_code=400, detail="仅支持 .zip 分享包或 .jsonl 历史文件")


@api_chat_router.get('/chat_context/token_stats')
async def get_chat_context_token_stats(
    session_id: str = "default",
    max_rounds: Optional[int] = Query(
        None,
        le=200,
        description="最近保留轮次，<=0 表示全部；省略时跟随 HISTORY_COMPACT_KEEP_ROUNDS",
    ),
    include_tools: bool = Query(False, description="是否额外统计工具定义 token"),
    tool_names: Optional[List[str]] = Query(
        None,
        description="已选择的工具名称；传入后仅统计这些工具定义，需同时 include_tools=true",
    ),
):
    """
    返回指定会话模型上下文构成的 token 统计（与真实请求同口径）。

    口径与 get_context_messages 一致：已压缩轮数 + 最近 max_rounds 轮。
    参数:
        session_id: 会话ID
        max_rounds: 统计时按最近多少轮展开
        include_tools: 是否把当前已加载 MCP 工具定义计入请求上下文 token
    返回:
        token 构成统计（消息/工具/总额、预算占比、轮次分布、压缩计数）
    """
    try:
        normalized_session_id = normalize_session_id(session_id)
        # 只读查询：会话尚不存在时直接返回零值统计，不实例化管理器
        # （其 __init__ 会创建只有 _meta 的空历史文件）
        if not chat_memory_file_exists(normalized_session_id):
            return JSONResponse(content={
                "state": "succeed",
                "session_id": normalized_session_id,
                "exists": False,
                "context_token_limit": 0,
                "messages_tokens": 0,
                "system_prompt_tokens": 0,
                "tool_definition_tokens": 0,
                "file_memory_tokens": 0,
                "request_context_tokens": 0,
                "estimated_budget_ratio": 0.0,
                "rounds": {
                    "total": 0,
                    "summarized": 0,
                    "retained": 0,
                    # isinstance 兼容直连调用（未过 FastAPI 依赖注入）时 Query 默认对象的情况
                    "max_rounds": max_rounds if isinstance(max_rounds, int) else 0,
                },
                "has_context_summary": False,
                "summary_text_length": 0,
                "recent_questions_length": 0,
                "recent_questions_count": 0,
                "context_compress_count": 0,
                "history_compress_count": 0,
                "round_tokens": [],
            })
        manager = await get_chat_memory_manager(normalized_session_id)
        # 上下文窗口按会话生效模型计算（会话独立模型选择覆盖全局默认），
        # 与真实生成任务的模型口径一致
        ambient_token = set_ambient_model_selection(
            resolve_session_model_selection(normalized_session_id)[0]
        )
        try:
            effective_max_rounds = max_rounds if max_rounds is not None else 0
            context_tools = None
            if include_tools:
                available_tools = list(tool_registry.ALL_TOOLS)
                if tool_names is None:
                    context_tools = available_tools
                else:
                    selected_names = {
                        name.strip()
                        for name in tool_names
                        if isinstance(name, str) and name.strip()
                    }
                    context_tools = [
                        tool
                        for tool in available_tools
                        if _tool_definition_name(tool) in selected_names
                    ]
                    # 聊天任务有外部工具时，运行时还会注入 check_tool_exists；
                    # 内置工具（todo_write / ask_user / write_file / edit_file /
                    # read_file / search_files / read_media）按前端勾选显式注入；
                    # read_document 在「会话存在上传文件且携带工具」时由后端自动注入。
                    # 这里保持 token 统计与真实请求的工具 schema 口径一致。
                    if context_tools or selected_names & set(SELECTABLE_BUILTIN_TOOL_NAMES):
                        context_tools, _ = inject_builtin_tools(
                            context_tools,
                            {},
                            include_todo=TODO_TOOL_NAME in selected_names,
                            include_ask_user=ASK_USER_TOOL_NAME in selected_names,
                            include_write_file=WRITE_FILE_NAME in selected_names,
                            include_edit_file=EDIT_FILE_NAME in selected_names,
                            include_read_file=READ_FILE_NAME in selected_names,
                            include_search_files=SEARCH_FILES_NAME in selected_names,
                            include_read_media=READ_MEDIA_NAME in selected_names,
                        )
                        # read_document 自动注入：会话存在上传文件时与真实请求同口径
                        # （真实请求还要求本轮携带工具；此处用于展示口径）
                        if count_session_file_memory(normalized_session_id) > 0:
                            context_tools, _ = inject_builtin_tools(
                                context_tools,
                                {},
                                include_read_document=True,
                            )
            # read_media 指引条件开关：与真实请求同口径（显式传名才提示）。
            # 视觉过滤由 build_sys_prompt 内部按 ambient 会话模型统一判定。
            read_media_selected = False
            if include_tools and tool_names is not None:
                read_media_selected = READ_MEDIA_NAME in selected_names
            stats = await manager.get_context_token_stats(
                max_rounds=effective_max_rounds,
                tools=context_tools,
                # 系统提示词按会话生效目录计算（worker 内由任务开始时 chdir 保证，
                # 主进程统计侧需显式解析），与真实请求口径一致
                system_prompt_text=build_runtime_system_text(
                    work_dir=resolve_session_work_dir(normalized_session_id)[0],
                    include_read_media_note=read_media_selected,
                ),
            )
        finally:
            reset_ambient_model_selection(ambient_token)
        return JSONResponse(content={
            "state": "succeed",
            "session_id": manager.session_id,
            **stats,
        })
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.post('/chat_context/compact_manual')
async def compact_chat_context_manual(
    session_id: str = "default",
    force: bool = Query(
        False,
        description="为 true 时跳过下限守卫：即使上下文估算低于摘要预算也强制压缩",
    ),
    stream: bool = Query(
        False,
        description="以 SSE 流式返回压缩过程（context_compaction start/delta/done，"
                    "与自动压缩事件同构；结尾附加 compaction_manual_result 结果帧）",
    ),
):
    """
    手动触发指定会话的跨轮历史压缩（复用自动压缩同一套流程）。

    会把全部已完成轮次归并为一个累计摘要、重建最近用户问题索引，并把压缩模型
    usage 累计落盘；原始轮次仍保存在历史 JSONL 中，但后续模型上下文不再回传。
    下限守卫：模型上下文估算（仅消息部分）低于"摘要总预算"（模型窗口 × 摘要
    预算比例）时压缩无收益，直接拒绝并提示；stream=true 时以
    compaction_manual_result 结果帧的 error 字段返回，stream=false 返回 400。
    `force=true` 可跳过该守卫。
    `stream=true` 时返回 SSE：压缩模型的思考/摘要增量实时推送（delta 帧
    仅推流不落盘），done 事件携带 summary_text 摘要全文并落盘，最后发送
    `{"event": "compaction_manual_result", compressed_rounds, stats, error}`
    与 `[DONE]`。stream=false 保持原 JSON 响应。
    参数:
        session_id: 会话ID
        force: 跳过下限守卫强制压缩
        stream: 是否以 SSE 返回压缩过程
    返回:
        stream=true: text/event-stream；否则 JSON {compressed_rounds, stats}
    """
    session_id = normalize_session_id(session_id)
    # 兼容直连调用（未过 FastAPI 依赖注入）时 Query 默认对象的情况：
    # Query 实例为 truthy，会把 force/stream 误判为已开启
    force = bool(force) if isinstance(force, bool) else False
    stream = bool(stream) if isinstance(stream, bool) else False
    # 压缩模型/预算按会话生效模型选择计算（stream=true 的实际压缩在 SSE 生成器
    # 的新任务中执行，_manual_compaction_event_stream.run 内会重新设置 ambient）
    try:
        effective_model_selection, _ = resolve_session_model_selection(session_id)
    except Exception:
        effective_model_selection = None
    ambient_token = set_ambient_model_selection(effective_model_selection)
    try:
        manager = await get_chat_memory_manager(session_id)
        compaction_settings = load_context_compaction_settings()
        # 手动压缩的门槛与保留预算统一使用"摘要总预算"（聊天窗口 × 摘要预算比例，
        # 聊天设置中可配）：手动确认后把全部已完成轮次归入累计摘要
        manual_budget_tokens = resolve_summary_total_budget(compaction_settings)
        # 下限守卫：模型上下文估算（仅消息部分，工具定义/系统提示不参与压缩）
        # 低于摘要总预算时，摘要可能不比原文小，压缩没有收益还白烧一次压缩
        # 模型调用——直接提示不能压缩。force=true 按保留参数本意跳过该下限。
        rejection: str | None = None
        if not force:
            context_stats = await manager.get_context_token_stats()
            context_estimate = int(context_stats.get("messages_tokens") or 0)
            if context_estimate < manual_budget_tokens:
                rejection = (
                    f"当前上下文约 {context_estimate} tokens，低于摘要预算 "
                    f"{manual_budget_tokens} tokens（模型窗口 × 摘要预算比例 "
                    f"{compaction_settings.summary_budget_ratio:g}），压缩无收益，"
                    "已取消本次手动压缩。"
                )
        if rejection is not None and not stream:
            raise HTTPException(status_code=400, detail=rejection)

        if stream:
            return StreamingResponse(
                _manual_compaction_event_stream(
                    manager, session_id, manual_budget_tokens, precheck_error=rejection
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        tool_request = ChatLLMRequest(
            messages=[],
            session_id=session_id,
            tools=list(tool_registry.ALL_TOOLS) if tool_registry.ALL_TOOLS else None,
        )
        compressed_rounds = await compact_session_history_if_needed(
            manager,
            tool_request,
            budget_tokens=manual_budget_tokens,
            # 用户已在确认框中明确触发手动压缩：全量把已完成轮次纳入累计摘要
            # （历史轮次窗口已废弃，无自动压缩保护需要放宽）。
            enforce=True,
            force_all=True,
            # 手动压缩无 SSE 流可推，仅落盘同一结构的事件行，历史加载时可见
            event_emitter=lambda payload: manager.add_context_compaction_event(payload),
            # 触发来源 = 用户手动触发
            trigger_reason="manual",
        )
        stats = await manager.get_context_token_stats(
            tools=list(tool_registry.ALL_TOOLS) if tool_registry.ALL_TOOLS else None,
        )
        return JSONResponse(content={
            "state": "succeed",
            "session_id": session_id,
            "compressed_rounds": compressed_rounds,
            "stats": stats,
        })
    except ChatModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=f"压缩模型配置无效: {exc}")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        reset_ambient_model_selection(ambient_token)


async def _manual_compaction_event_stream(
    manager,
    session_id: str,
    budget_tokens: int,
    precheck_error: str | None = None,
):
    """手动压缩的 SSE 生成器。

    - context_compaction start/delta/done 事件结构与聊天内自动压缩完全一致；
      delta 帧（压缩模型思考/正文增量）只实时推送、不落盘，其余事件按同一
      payload 落盘 JSONL（刷新后可回放摘要）；
    - 结尾附加一帧 compaction_manual_result（compressed_rounds/stats/error）
      供前端提示，再发送 [DONE]；
    - `precheck_error` 非空时（上下文低于摘要预算的下限守卫）不发起压缩，
      直接以结果帧带回提示；
    - 客户端断开时取消后台压缩任务，已落盘的 start 由既有孤儿标记机制处理。
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    result: dict[str, Any] = {"compressed_rounds": 0, "stats": None, "error": None}

    async def emit(payload: dict[str, Any]) -> None:
        if isinstance(payload, dict) and payload.get("phase") == "delta":
            await queue.put(payload)
            return
        add_event = getattr(manager, "add_context_compaction_event", None)
        if callable(add_event):
            try:
                await add_event(payload)
            except Exception as exc:
                print(f"[WARN] 手动压缩事件落盘失败：{exc}")
        await queue.put(payload)

    async def run() -> None:
        # 压缩在新任务中执行（create_task 复制上下文不会带出 handler 的 ambient），
        # 这里按会话重新设置模型选择覆盖
        try:
            effective_selection, _ = resolve_session_model_selection(session_id)
        except Exception:
            effective_selection = None
        ambient_token = set_ambient_model_selection(effective_selection)
        try:
            if precheck_error:
                result["error"] = precheck_error
                return
            tool_request = ChatLLMRequest(
                messages=[],
                session_id=session_id,
                tools=list(tool_registry.ALL_TOOLS) if tool_registry.ALL_TOOLS else None,
            )
            result["compressed_rounds"] = await compact_session_history_if_needed(
                manager,
                tool_request,
                budget_tokens=budget_tokens,
                # 手动确认后覆盖所有未摘要轮次（历史轮次窗口已废弃）。
                enforce=True,
                force_all=True,
                event_emitter=emit,
                # 触发来源 = 用户手动触发
                trigger_reason="manual",
            )
            result["stats"] = await manager.get_context_token_stats(
                tools=list(tool_registry.ALL_TOOLS) if tool_registry.ALL_TOOLS else None,
            )
        except ChatModelConfigurationError as exc:
            result["error"] = f"压缩模型配置无效: {exc}"
        except asyncio.CancelledError:
            # 客户端断开（页面刷新/关闭/网络抖动）触发 task.cancel()：
            # 立即补写 aborted 事件闭合 start 行，不等下一次聊天任务开始时
            # 由孤儿标记机制延迟收尾（此前会留下数分钟的"永远转圈"块）。
            # shield 保护落盘本身不被二次取消；shield 也失败则放弃，
            # 交由孤儿机制兜底。
            try:
                add_event = getattr(manager, "add_context_compaction_event", None)
                if callable(add_event):
                    await asyncio.shield(add_event({
                        "event": "context_compaction",
                        "scope": "session",
                        "phase": "aborted",
                        "role": "assistant",
                        "reason": "task_interrupted",
                        "error": None,
                    }))
            except Exception as shield_exc:
                print(f"[WARN] 手动压缩中断标记落盘失败（交由孤儿机制收尾）：{shield_exc}")
            raise
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            reset_ambient_model_selection(ambient_token)
            await queue.put(sentinel)

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                # 心跳：压缩模型长时间无增量输出时（合并大历史等），每 30 秒
                # 发一帧 SSE 注释（": ping"），前端按规范自动忽略注释行；
                # 既防止代理/浏览器把静默连接判死，也让用户确认任务仍在进行。
                payload = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if payload is sentinel:
                break
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield (
            "data: "
            + json.dumps({
                "event": "compaction_manual_result",
                "session_id": session_id,
                "compressed_rounds": result["compressed_rounds"],
                "stats": result["stats"],
                "error": result["error"],
            }, ensure_ascii=False)
            + "\n\n"
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    yield "data: [DONE]\n\n"

