import asyncio
import json
from typing import Any, Optional, List
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
)
from memory.file_memory import cleanup_file_memory_manager
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
            effective_max_rounds = (
                max_rounds
                if max_rounds is not None
                else load_context_compaction_settings().keep_rounds
            )
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
                    # read_file / search_files / read_media）按前端勾选显式注入。
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
            stats = await manager.get_context_token_stats(
                max_rounds=effective_max_rounds,
                tools=context_tools,
                # 系统提示词按会话生效目录计算（worker 内由任务开始时 chdir 保证，
                # 主进程统计侧需显式解析），与真实请求口径一致
                system_prompt_text=build_runtime_system_text(
                    work_dir=resolve_session_work_dir(normalized_session_id)[0]
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
            # 用户已在确认框中明确触发手动压缩；不要再受 keep_rounds 的自动压缩保护限制，
            # force_all 模式会把全部已完成轮次纳入累计摘要。
            enforce=True,
            force_all=True,
            # 手动压缩无 SSE 流可推，仅落盘同一结构的事件行，历史加载时可见
            event_emitter=lambda payload: manager.add_context_compaction_event(payload),
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
                # 手动确认后允许突破 keep_rounds，并覆盖所有未摘要轮次。
                enforce=True,
                force_all=True,
                event_emitter=emit,
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

