# 标准库导入
# import re
import asyncio
import copy
import json
import time
import uuid
from collections import deque
# import inspect
from typing import Any, Dict, List, AsyncGenerator

# 自定义模块导入
from env_manager import (
    ChatModelConfigurationError,
    load_var,
    require_default_chat_config,
    set_ambient_model_selection,
)
from chat.chat_llm import ChatLLM
from config import (
    ChatLLMRequest,
    DEFAULT_CONTEXT_HISTORY_ROUNDS,
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_SUB_AGENT_ENABLED,
    DEFAULT_TOOL_CALL_STREAM_TIMEOUT_SECONDS,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    get_current_dir,
)
from memory.chat_memory import (
    get_chat_memory_manager,
    cleanup_chat_memory_manager,
    normalize_session_id,
)
from memory.file_memory import (
    cleanup_file_memory_manager,
    get_file_memory_manager,
    resolve_message_media_refs,
)
from memory.chat_history_format import content_part_to_text
from factory.agent_runtime import tool_registry
from factory.agent_runtime.builtin_tools import (
    ASK_ANSWER_PREFIX,
    ASK_USER_TOOL_NAME,
    ASK_USER_PLACEHOLDER_DEFINITION,
    BUILTIN_TOOL_SERVER_KEY,
    CHECK_TOOL_EXISTS_NAME,
    EDIT_FILE_NAME,
    READ_FILE_NAME,
    SEARCH_FILES_NAME,
    SUB_AGENT_TOOL_NAME,
    TODO_TOOL_NAME,
    WRITE_FILE_NAME,
    READ_MEDIA_NAME,
    collect_media_references,
    execute_builtin_tool,
    inject_builtin_tools,
    is_builtin_tool,
    normalize_ask_questions,
    normalize_sub_agent_task,
    normalize_todo_items,
    execute_read_media,
    load_any_media_model_part,
    roll_recent_media_parts,
    try_execute_builtin_file_tool,
)
from factory.agent_runtime.chat_runtime import (
    parse_bool_like,
    parse_int_like,
    parse_return_length,
    frontend_provides_full_history,
    build_request_messages,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_request_context_tokens,
    estimate_text_tokens,
    estimate_tool_definition_tokens,
    resolve_model_max_input_tokens,
    UsageAccumulator,
    # V1 sub_agent：以下原私有实现迁入 chat_runtime，父/子循环共用
    parse_sse_event as _parse_sse_event,  # noqa: F401
    filter_tool_calls_fields as _filter_tool_calls_fields,  # noqa: F401
    merge_function_call_delta as _merge_function_call_delta,  # noqa: F401
    merge_tool_call_delta as _merge_tool_call_delta,  # noqa: F401
    REASONING_PLACEHOLDER as _REASONING_PLACEHOLDER,  # noqa: F401
    latest_reasoning_content as _latest_reasoning_content,  # noqa: F401
    reasoning_content_for_tool_call as _reasoning_content_for_tool_call,  # noqa: F401
    retain_latest_reasoning,
    copy_for_request,
)
from factory.agent_runtime.context_compaction import (
    ContextCompactionError,
    _find_current_round_start,
    build_oversized_tool_feedback,
    compact_active_round_context_if_needed,
    compact_session_history_if_needed,
    load_context_compaction_settings,
    make_oversized_result_preview,
    resolve_oversized_result_token_threshold,
    resolve_context_compaction_threshold,
    resolve_summary_total_budget,
)
from factory.agent_runtime.tool_executor import (
    execute_tool_round,
    normalize_tool_calls,
    prepare_tool_execution,
)
from factory.agent_runtime.sub_agent import (
    SubAgentContext,
    load_sub_agent_limits,
    new_agent_id,
    run_sub_agent_batch,
)
from factory.session_worker import (
    get_worker_proxy,
    peek_worker_proxy,
    sweep_dead_worker_proxies,
)


# P1 首调用预算检查：文件记忆超窗降级为摘要时的最大字符数
# （与 memory/file_memory.py 的 get_file_memory_text 摘要语义一致）
_FIRST_CALL_FILE_MEMORY_MAX_CHARS = 3000

# 记录当前后台运行的会话生成任务（session_id -> _SessionStream）
# 前端刷新/断开 SSE 连接时，后台任务不会被取消，事件持续写入缓冲与 JSONL
_SESSION_STREAMS: dict[str, "_SessionStream"] = {}
_SESSION_STREAM_MAX_BUFFER = 20000  # 有界环形缓冲：防止长时间无消费时内存膨胀


# ---- 思考回传 / 工具流超时的配置读取（保留在本命名空间） ----
# 这些 loader 曾随共享纯函数一起迁入 agent_runtime.chat_runtime，但父循环
# 走 chat_factory.load_var 的路径（测试/运行时可直接 patch 本命名空间），
# 因此这里保留本地实现；chat_runtime 侧同签名函数供子任务等独立调用方使用。
# 思考回传整形函数（_retain_latest_reasoning / _copy_for_request）改为
# 显式传 limit 的包装，保证 cf.* 打桩对下游行为生效。
def _load_reasoning_return_max_length(default: int = DEFAULT_REASONING_RETURN_MAX_LENGTH) -> int:
    return parse_return_length(
        load_var("REASONING_RETURN_MAX_LENGTH", default), default
    )


def _load_tool_call_stream_timeout(
    default: float = DEFAULT_TOOL_CALL_STREAM_TIMEOUT_SECONDS,
) -> float:
    """工具调用流式阶段（SSE 输出 tool_calls 期间）无输出超时秒数（0=不限制）。"""
    raw = load_var("TOOL_CALL_STREAM_TIMEOUT_SECONDS", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else 0.0


def _retain_latest_reasoning(messages: List[dict]) -> None:
    retain_latest_reasoning(messages, limit=_load_reasoning_return_max_length())


def _copy_for_request(messages: List[dict]) -> List[dict]:
    return copy_for_request(messages, limit=_load_reasoning_return_max_length())


def _format_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def _merge_usage_values(total: dict[str, Any], delta: dict[str, Any]) -> None:
    if not isinstance(delta, dict):
        return
    for key, value in delta.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            existed = total.get(key, 0)
            if not isinstance(existed, (int, float)) or isinstance(existed, bool):
                existed = 0
            total[key] = existed + value
        elif isinstance(value, dict):
            child = total.get(key)
            if not isinstance(child, dict):
                child = {}
                total[key] = child
            _merge_usage_values(child, value)


# 思考过程（reasoning_content）回传长度：0=不回传，负数=全部回传，正数=保留末尾 N 字符
def _messages_debug_summary(messages: List[dict[str, Any]]) -> str:
    """单行概要描述每条消息（角色+内容形态），供模型调用前的调试打印。

    媒体部件只显示 media:// 引用名或 data: 前缀，绝不输出 base64 数据：
    旧实现直接 str(messages) 整包打印，带图轮次每次模型调用都会向控制台
    刷出 MB 级 base64，且同一张图在任务循环的多次调用中反复出现，
    容易被误判为"历史重复携带图片"。
    """
    lines: list[str] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            lines.append(f"[{index}]<非dict:{type(msg).__name__}>")
            continue
        role = msg.get("role") or "?"
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(f"<{type(part).__name__}>")
                    continue
                part_type = str(part.get("type") or "?")
                if part_type == "text":
                    text = str(part.get("text") or "")
                    parts.append(f"text({len(text)}字)")
                elif part_type == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:"):
                        url = url.split(",", 1)[0] + ",<base64省略>"
                    parts.append(f"image({url[:80]})")
                elif part_type == "input_audio":
                    parts.append("audio(<base64省略>)")
                else:
                    parts.append(part_type)
            content_desc = "[" + ", ".join(parts) + "]"
        else:
            text = "" if content is None else str(content)
            extra = ""
            if msg.get("tool_calls"):
                extra += f"+{len(msg['tool_calls'])}个工具调用"
            if msg.get("tool_call_id"):
                extra += f" tool_call_id={msg['tool_call_id']}"
            preview = text[:60].replace("\n", "\\n")
            content_desc = f"{len(text)}字'{preview}'{extra}"
        lines.append(f"[{index}]{role}:{content_desc}")
    return "; ".join(lines)


def _replace_todo_context(messages: List[dict[str, Any]], todos: list[dict[str, str]]) -> None:
    """用一条紧凑的任务状态 system 消息替换历史 todo 调用与结果。"""
    todo_call_ids = set()
    compacted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            compacted.append(message)
            continue
        if message.get("_todo_summary"):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            kept_calls = []
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
                if function.get("name") == TODO_TOOL_NAME:
                    if tool_call.get("id"):
                        todo_call_ids.add(tool_call["id"])
                else:
                    kept_calls.append(tool_call)
            if kept_calls:
                message["tool_calls"] = kept_calls
            elif not message.get("content"):
                continue
        if (
            message.get("role") == "tool"
            and (message.get("_tool_name") == TODO_TOOL_NAME
                 or message.get("tool_call_id") in todo_call_ids)
        ):
            continue
        compacted.append(message)
    if todos:
        symbols = {"done": "✓", "in_progress": "→", "pending": "○"}
        lines = ["当前任务："]
        lines.extend(f"{symbols.get(item.get('status'), '○')} {item.get('content', '')}" for item in todos)
        compacted.append({"role": "system", "content": "\n".join(lines), "_todo_summary": True})
    messages[:] = compacted


# 当前工作路径为<{_format_tool_result(get_current_dir())}>
# 本轮工具集合由用户选择并由系统注入，你只能调用当前可见工具（如果用户提供了）；禁止臆造、改名或扩展工具。
# 如果任务已完成，直接给最终答复。
# 系统提示词构建已模块化到 factory/system_prompt.py（工作路径 + 环境说明 +
# 工具规则 + 回传长度/工具超时等可变配置说明）。此处 re-export 保持既有
# 导入路径（routers/chat_router 等）不变；build_runtime_system_text 每次构造
# 都实时读取 load_var，用户改配置后下一轮对话即生效。
from factory.system_prompt import (  # noqa: F401
    build_media_tag_prompt,
    build_runtime_system_text,
    build_sys_prompt,
)


async def load_all_tools() -> None:
    """加载并过滤工具，同时构建 MCP 映射。"""
    # 发送路径复用工具探测缓存：缓存新鲜时不再逐服务拉起 MCP 子进程重探
    # （子进程冷启动可能耗时数秒，逐消息探测会拖慢每条消息的首字节），
    # 过期才重探；servers 配置变更时热重载线程会强制刷新缓存
    ttl = tool_registry.tools_cache_ttl_seconds()
    if ttl > 0 and tool_registry.get_cached_tools_payload(ttl) is not None:
        return
    await tool_registry.refresh_tools_from_mcp(get_current_dir())
    print(f"✅ 已加载 {len(tool_registry.ALL_TOOLS)} 个工具，构建 {len(tool_registry.TOOL_MCP_SERVERS)} 个MCP映射")
print("[INFO] 工具将按需实时刷新（来源: setting/mcp_servers.json）")


# _parse_sse_event / _filter_tool_calls_fields / _merge_function_call_delta /
# _merge_tool_call_delta 已迁入 factory/agent_runtime/chat_runtime.py，
# 由上方 import 别名 re-export（父/子 Agent 循环共用，见 docs/sub_agent_v1.md §6.2）


def _sse_error_payload(message: str) -> str:
    return f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n"


def _tool_name_from_definition(tool_def: dict) -> str:
    if not isinstance(tool_def, dict):
        return ""
    func_info = tool_def.get("function")
    if not isinstance(func_info, dict):
        return ""
    name = func_info.get("name")
    return name if isinstance(name, str) else ""



class _SessionStream:
    """一次后台 Agent 生成任务的环形事件缓冲 + 订阅管理。

    - 生成循环独立任务运行：页面刷新不再中止生成；
    - 后到的消费者回放"任务起点"起的全部事件（含 reconnect 标记）：
      运行中任务的 chat_round 在任务收尾才写 JSONL，历史读不到，
      回放必须覆盖整个任务才能在刷新后重建此前轮次的界面；
    - emit 为非阻塞追加（有界 deque），消费端断线不影响生产端。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.task: asyncio.Task | None = None
        self.buffer = deque(maxlen=_SESSION_STREAM_MAX_BUFFER)
        self.seq = 0              # 已放入缓冲的事件总条数
        self.round_start_seq = 0  # 任务回放起点（首个循环检查点记录，此后不变）
        self._round_start_set = False
        self.done = False
        self.question_text = ""   # 本轮初始用户提问（回放时补前端气泡用）
        self.user_stop_requested = False  # 手动停止触发的取消（区别于新消息打断/服务关停）
        self.cond = asyncio.Condition()
        # 运行中注入的用户消息队列（消息引导，inline 模式）：inject 接口写入，
        # 生成循环每轮检查点经 pop_injected_message 取出；与 worker 模式的
        # _WorkerStreamAdapter.pop_injected_message 保持鸭子类型一致
        self.injected_messages: deque = deque(maxlen=16)

    def pop_injected_message(self) -> dict | None:
        """非阻塞取一条运行中注入的用户消息；队列为空返回 None。"""
        if self.injected_messages:
            return self.injected_messages.popleft()
        return None

    def emit(self, chunk: str) -> None:
        if not isinstance(chunk, str):
            return
        self.buffer.append(chunk)
        self.seq += 1

    def notify(self) -> None:
        async def notify_all() -> None:
            async with self.cond:
                self.cond.notify_all()
        try:
            asyncio.get_running_loop().create_task(notify_all())
        except RuntimeError:
            pass

    def notify_threadsafe(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """供 worker 事件 reader 线程调用：把唤醒调度回主事件循环。"""
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.notify)
        except RuntimeError:
            pass

    def set_round_start(self) -> None:
        # 每轮循环开始处都会调用，但只有首次生效（任务起点）。运行中任务
        # 的 chat_round 尚未落盘（任务收尾才写 JSONL），刷新重连必须回放
        # 整个任务的事件才能重建此前轮次的界面；若每轮都重置起点，前端
        # 只能收到当前轮的实时数据，任务早期步骤会全部丢失
        if not self._round_start_set:
            self.round_start_seq = self.seq
            self._round_start_set = True

    async def finish(self) -> None:
        self.done = True
        async with self.cond:
            self.cond.notify_all()

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done() and not self.done


def _get_session_stream(session_id: str) -> _SessionStream | None:
    return _SESSION_STREAMS.get(session_id)


def _set_session_stream(session_id: str) -> _SessionStream:
    if len(_SESSION_STREAMS) > 128:
        for done_sid, done_stream in list(_SESSION_STREAMS.items()):
            if done_stream.done and (done_stream.task is None or done_stream.task.done()):
                _SESSION_STREAMS.pop(done_sid, None)
    stream = _SessionStream(session_id)
    _SESSION_STREAMS[session_id] = stream
    return stream


async def _stream_emit(stream: _SessionStream, chunk: str) -> None:
    stream.emit(chunk)
    stream.notify()


async def _emit_compaction_event(
    stream: _SessionStream,
    session_chat_memory: Any,
    payload: dict[str, Any],
) -> None:
    """压缩进度事件：先推送 SSE 帧，再按同一份 payload 持久化。

    顺序刻意如此：实时显示优先，落盘不得阻塞推送（Windows 上文件可能被
    搜索索引/杀软短暂占用，落盘重试会拖慢 SSE）；落盘失败只影响历史
    加载，不阻断实时展示，打 WARN 即可。两处字段结构完全一致。

    例外：phase="delta" 是压缩模型的思考/正文增量帧，只用于实时渲染，
    体量大且无回放价值，不落盘（历史回放使用 done 事件的 summary_text）。
    round scope 事件归入当前 chat_round.events，session scope 仍为独立 JSONL 行。
    """
    await _stream_emit(stream, f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
    if payload.get("phase") == "delta":
        return
    try:
        add_event = getattr(session_chat_memory, "add_context_compaction_event", None)
        if callable(add_event):
            await add_event(payload)
    except Exception as exc:
        print(f"[WARN] 压缩事件落盘失败：{exc}")


async def _apply_session_todo(session_chat_memory, tool_args: Any) -> tuple[dict[str, Any], list[dict[str, str]] | None]:
    """校验并落盘模型提交的任务计划；返回 (工具结果, 规整后的列表或 None)。

    校验不依赖模型自觉：id 缺失/重复、多个 in_progress 等模型常见违约由
    normalize_todo_items 自动修复并随结果告警（提示下次保持稳定 id）；
    上一版计划直接读会话 _meta.todo（不信任模型自报的旧状态），保证全量
    更新时未携带 id 的同内容步骤继承原 id；硬性错误（content 空/超长、
    status 非法、超 20 项）返回带条目序号的纠错原因，帮助模型一次修正。
    """
    raw_todos = tool_args.get("todos") if isinstance(tool_args, dict) else None
    try:
        prev_items = await session_chat_memory.get_session_todo()
    except Exception:
        prev_items = []
    items, notices, error_reason = normalize_todo_items(
        raw_todos, prev_items=prev_items)
    if items is None:
        return {"error": error_reason or "todos 参数无效"}, None
    await session_chat_memory.update_session_todo(items)
    done_count = sum(1 for item in items if item["status"] == "done")
    in_progress_count = sum(1 for item in items if item["status"] == "in_progress")
    result: dict[str, Any] = {
        "message": f"任务计划已更新（共 {len(items)} 项，已完成 {done_count} 项，进行中 {in_progress_count} 项）",
        "current plan state": {
            "total": len(items),
            "done": done_count,
            "in_progress": in_progress_count,
            "pending": len(items) - done_count - in_progress_count,
        },
        # 回写规整后的完整列表：模型据此对齐自动分配/继承出的最终 id，
        # 避免长对话中工具结果被截断后丢失 id 记忆
        "todos": items,
    }
    if notices:
        result["warnings"] = notices
    return result, items


def _request_has_new_user_message(tool_request: ChatLLMRequest) -> bool:
    for msg in tool_request.messages or []:
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        if role == "user" and content_part_to_text(content).strip():
            return True
    return False


async def _compact_task_context_if_needed(
    messages: List[dict[str, Any]],
    tool_request: ChatLLMRequest,
    session_chat_memory: Any,
    stream: Any,
    *,
    threshold: int,
    backend_history_rounds: int,
    runtime_sys_text: str,
    file_block_text: str,
    event_emitter: Any,
) -> List[dict[str, Any]] | None:
    """任务内上下文预算管理：达到阈值时压缩历史并重建内存消息。

    触发：全量上下文估算 > threshold（单轮阈值 = min(聊天,压缩)窗口 × 比例）。
    行为：
    1. 跨轮压缩持久层历史（force_all：不保留原始轮次；预算 = 阈值 × 0.4）——
       老轮次压成累计摘要并重建全历史最近问题索引，压缩游标推进；
    2. 从持久层重建内存消息的历史部分：主 system（persona+运行时文本+文件块）
       保持在首位，累计摘要 system 与最近问题排在其后；
    3. 当前任务轮的消息（最后一条用户问题起）原样保留，交由单轮压缩继续管理。

    返回重建后的 messages；未触发返回 None。
    """
    context_now = estimate_request_context_tokens(messages, tool_request.tools)
    if threshold <= 0 or context_now <= threshold:
        return None
    round_start = _find_current_round_start(messages)
    active_messages = messages[round_start:] if round_start is not None else []
    system_message = messages[0] if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system" else None
    # 历史预算 = 阈值 × 0.4；摘要与最近问题由 summary-only 视图统一回传。
    history_budget = max(2048, int(threshold * 0.4))
    print(
        f"[INFO] 任务内上下文 {context_now} tokens 超过阈值 {threshold}，"
        f"压缩持久层历史（预算 {history_budget}）并重建内存历史部分"
    )
    await compact_session_history_if_needed(
        session_chat_memory,
        tool_request,
        settings=load_context_compaction_settings(),
        budget_tokens=history_budget,
        enforce=True,
        force_all=True,
        event_emitter=event_emitter,
    )
    history_messages = await session_chat_memory.get_context_messages(
        # <=0 表示无限窗口（keep_rounds=0），原样透传；否则至少保留 1 轮
        max_rounds=backend_history_rounds if backend_history_rounds <= 0 else max(1, backend_history_rounds)
    )
    # 主 system（persona+运行时文本+文件块）保持在首位，累计摘要/最近问题排在
    # 其后：不再把运行时文本混入【历史压缩摘要】消息，也不向已含文件块的
    # system_message 重复追加 file_block_text。
    if system_message is not None:
        rebuilt = [dict(system_message)]
    else:
        rebuilt = [{
            "role": "system",
            "content": (
                "你是一个聪明的助手，正在使用 Ytools 程序帮忙用户，你需要自行判断是否需要调用工具，并结合你之前使用的工具（如果有）的结果，调用工具的结果只有你可见，请根据用户使用的语言回答用户问题。\n"
                + runtime_sys_text
                + file_block_text
            ),
        }]
    rebuilt.extend(history_messages)
    rebuilt.extend(active_messages)
    after_tokens = estimate_request_context_tokens(rebuilt, tool_request.tools)
    print(f"[INFO] 任务内历史重建完成：{context_now} -> {after_tokens} tokens")
    return rebuilt


# 任务内自动压缩连续失败上限：达到后本任务停止自动压缩尝试（推 warning 通知）
_AUTO_COMPACTION_MAX_CONSECUTIVE_FAILURES = 2


async def _emit_sub_agent_event(session_chat_memory, stream, payload: dict[str, Any]) -> None:
    """sub_agent 事件统一出口：JSONL 落盘（timestamp 补齐 + 检查点快照）+ SSE 推送。

    JSONL 与 SSE 携带同构字段（SSE 另有 delta/heartbeat 推流事件，不落盘）。
    落盘失败不阻断推流；两次写入都在事件循环内串行完成（子任务事件只在
    事件循环同步守卫块内写历史，禁止工作线程写历史——_write_guard 跨进程
    文件锁不可重入）。
    """
    try:
        await session_chat_memory.add_sub_agent_event(payload)
    except Exception as persist_error:
        print(f"[WARN] sub_agent 事件落盘失败（phase={payload.get('phase')}）: {persist_error}")
    try:
        await _stream_emit(
            stream,
            f"data: {json.dumps({'event': 'sub_agent', **payload}, ensure_ascii=False)}\n\n",
        )
    except Exception as sse_error:
        print(f"[WARN] sub_agent 事件推送失败（phase={payload.get('phase')}）: {sse_error}")


def _build_sub_agent_contexts(
    sub_agent_calls: list[tuple[int, dict, str, list[dict] | None]],
    *,
    round_tools: list[dict[str, Any]],
    round_tool_servers: dict[str, str],
    configured_tool_names: set[str],
    configured_tool_servers: dict[str, str],
    session_id: str,
    session_chat_memory,
    stream,
    stop_checker,
) -> list["SubAgentContext"]:
    """把父循环收集的 sub_agent 调用转成 SubAgentContext 列表（docs/sub_agent_v1.md §6.1）。

    工具快照在派发时刻固化：
    - 剔除 sub_agent（V1 禁止嵌套）与 check_tool_exists（子任务工具已知）；
    - ask_user 替换为占位定义（子任务无用户通道，占位执行返回明确错误）；
    - read_media 若父级本轮未启用，子级同样不注入。
    """
    limits = load_sub_agent_limits()
    used_agent_ids: set[str] = set()
    contexts: list["SubAgentContext"] = []
    for agent_index, (tool_index, tc, task_text, initial_todo) in enumerate(sub_agent_calls):
        tool_call_id = tc.get("id", "") if isinstance(tc, dict) else ""
        # 子工具白名单 = 父级本轮工具快照 − sub_agent − check_tool_exists；
        # ask_user 真实定义替换为占位定义（同名同参，执行返回不可用说明）
        child_tools: list[dict[str, Any]] = []
        child_servers: dict[str, str] = {}
        ask_user_replaced = False
        for tool_def in round_tools:
            tool_name = _tool_name_from_definition(tool_def)
            if not tool_name or tool_name == SUB_AGENT_TOOL_NAME or tool_name == CHECK_TOOL_EXISTS_NAME:
                continue
            if tool_name == ASK_USER_TOOL_NAME:
                child_tools.append(dict(ASK_USER_PLACEHOLDER_DEFINITION))
                child_servers[ASK_USER_TOOL_NAME] = BUILTIN_TOOL_SERVER_KEY
                ask_user_replaced = True
                continue
            child_tools.append(tool_def)
            if tool_name in round_tool_servers:
                child_servers[tool_name] = round_tool_servers[tool_name]
        if not ask_user_replaced:
            # 父级本轮没有 ask_user：仍注入占位定义，避免子模型幻觉出提问工具
            child_tools.append(dict(ASK_USER_PLACEHOLDER_DEFINITION))
            child_servers[ASK_USER_TOOL_NAME] = BUILTIN_TOOL_SERVER_KEY
        contexts.append(SubAgentContext(
            agent_id=new_agent_id(used_agent_ids),
            parent_agent_id="main",
            parent_tool_call_id=tool_call_id,
            parent_tool_index=tool_index,
            agent_index=agent_index,
            session_id=session_id,
            task=task_text,
            initial_todo=initial_todo,
            tools=child_tools,
            tool_servers=child_servers,
            configured_tool_names=configured_tool_names,
            configured_tool_servers=configured_tool_servers,
            max_rounds=limits["max_rounds"],
            timeout_seconds=limits["timeout_seconds"],
            reply_max_chars=limits["reply_max_chars"],
            emit_event=lambda payload: _emit_sub_agent_event(session_chat_memory, stream, payload),
            stop_checker=stop_checker,
        ))
        used_agent_ids.add(contexts[-1].agent_id)
    return contexts


def is_chat_stream_running(session_id: str) -> bool:
    stream = _get_session_stream(session_id)
    return bool(stream and stream.is_running())


async def _run_chat_generation(
    tool_request: ChatLLMRequest,
    stream: _SessionStream,
) -> None:
    """后台独立任务：执行完整 Agent 生成循环，事件写入 stream 缓冲。"""
    # 从请求中获取 session_id
    session_id = getattr(tool_request, 'session_id', 'default')
    # 会话级模型选择：_meta.model_selection 按角色覆盖 → 全局默认；
    # 失效覆盖（模型已从 models.json 删除）发 warning 并回退该角色全局默认。
    # ambient 覆盖仅在本 asyncio 任务上下文内生效（create_task 复制上下文，任务结束自动失效），
    # 任务内聊天/压缩模型与参数的读取（require_default_chat_config、get_role_selection 等）按会话生效
    try:
        from memory.chat_memory import resolve_session_model_selection
        effective_model_selection, model_warnings = resolve_session_model_selection(session_id)
    except Exception as selection_exc:
        effective_model_selection, model_warnings = None, [f"会话模型选择解析失败: {selection_exc}"]
    for model_warning in model_warnings:
        selection_warning_event = {
            "warning": {"code": "MODEL_SELECTION_FALLBACK", "message": model_warning}
        }
        await _stream_emit(
            stream, f"data: {json.dumps(selection_warning_event, ensure_ascii=False)}\n\n"
        )
    set_ambient_model_selection(effective_model_selection)
    try:
        require_default_chat_config()
    except ChatModelConfigurationError as exc:
        await _stream_emit(stream, _sse_error_payload(str(exc)))
        return
    await load_all_tools()
    # 计算本轮允许使用的工具：后端实时工具为唯一真相；前端仅允许传 tool_names
    configured_tools = list(tool_registry.ALL_TOOLS)
    configured_tool_servers = dict(tool_registry.TOOL_MCP_SERVERS)
    configured_tool_by_name = {
        _tool_name_from_definition(tool_def): tool_def
        for tool_def in configured_tools
        if _tool_name_from_definition(tool_def)
    }
    configured_tool_names = set(configured_tool_by_name)
    requested_tool_names = getattr(tool_request, "tool_names", None)
    # 请求未携带 tool_names（None，区别于显式空列表）时回退会话工具选择：
    # _meta.tool_selection 覆盖 → mcp_servers.json 的 inputs 全局默认；
    # 前端显式传 [] 仍表示无工具模式，不受影响
    if requested_tool_names is None:
        try:
            from memory.chat_memory import resolve_session_tool_selection
            effective_selection, selection_warning = resolve_session_tool_selection(session_id)
        except Exception as selection_exc:
            effective_selection, selection_warning = None, f"会话工具选择解析失败: {selection_exc}"
        if selection_warning:
            fallback_warning_event = {
                "warning": {"code": "TOOL_SELECTION_FALLBACK", "message": selection_warning}
            }
            await _stream_emit(
                stream, f"data: {json.dumps(fallback_warning_event, ensure_ascii=False)}\n\n"
            )
        if effective_selection:
            fallback_names = [
                str(name).strip()
                for tool_names in effective_selection.values()
                for name in (tool_names if isinstance(tool_names, list) else [])
                if isinstance(name, str) and str(name).strip()
            ]
            if fallback_names:
                requested_tool_names = fallback_names
                tool_request.tool_names = fallback_names
    # 先初始化：未携带 tool_names（无工具模式）时下游引用（todo_requested 等）也要可用
    requested_names: list[str] = []
    if isinstance(requested_tool_names, list):
        requested_names = [str(name).strip() for name in requested_tool_names if str(name).strip()]
        if not requested_names:
            warning_event = {
                "warning": {
                    "code": "NO_TOOL_SELECTED",
                    "message": "本轮未选择任何工具，已按无工具模式继续",
                }
            }
            await _stream_emit(stream, f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n")
            round_tools = []
            round_tool_servers = {}
        else:
            selected_tools: list[dict] = []
            selected_servers: dict[str, str] = {}
            ignored_names: list[str] = []
            seen_names = set()
            for tool_name in requested_names:
                if tool_name in seen_names:
                    continue
                seen_names.add(tool_name)
                if is_builtin_tool(tool_name):
                    continue
                real_tool = configured_tool_by_name.get(tool_name)
                if real_tool is None:
                    ignored_names.append(tool_name)
                    continue
                selected_tools.append(real_tool)
                if tool_name in configured_tool_servers:
                    selected_servers[tool_name] = configured_tool_servers[tool_name]
            if ignored_names:
                warning_event = {
                    "warning": {
                        "code": "UNKNOWN_TOOL_IGNORED",
                        "message": "前端传入了未注册工具名，已忽略",
                        "ignored_tools": ignored_names,
                    }
                }
                await _stream_emit(stream, f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n")
            round_tools = selected_tools
            round_tool_servers = selected_servers
    else:
        warning_event = {
            "warning": {
                "code": "NO_TOOL_SELECTED",
                "message": "缺少 tool_names 字段，已按无工具模式继续",
            }
        }
        await _stream_emit(stream, f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n")
        round_tools = []
        round_tool_servers = {}
    # 当且仅当前端有工具选择时，注入本地工具；无工具时不向上游发送 tools 字段。
    # todo_write / ask_user / write_file / edit_file / read_file / search_files /
    # sub_agent 例外：用户在工具选择中勾选（内置工具分组）即注入，即使未选择任何 MCP 工具。
    todo_requested = TODO_TOOL_NAME in (requested_names or [])
    ask_requested = ASK_USER_TOOL_NAME in (requested_names or [])
    write_file_requested = WRITE_FILE_NAME in (requested_names or [])
    edit_file_requested = EDIT_FILE_NAME in (requested_names or [])
    read_file_requested = READ_FILE_NAME in (requested_names or [])
    search_files_requested = SEARCH_FILES_NAME in (requested_names or [])
    read_media_requested = READ_MEDIA_NAME in (requested_names or [])
    sub_agent_requested = (
        SUB_AGENT_TOOL_NAME in (requested_names or [])
        and load_var("SUB_AGENT_ENABLED", DEFAULT_SUB_AGENT_ENABLED)
    )
    round_tools, round_tool_servers = inject_builtin_tools(
        round_tools, round_tool_servers, include_todo=todo_requested,
        include_ask_user=ask_requested,
        include_write_file=write_file_requested,
        include_edit_file=edit_file_requested,
        include_read_file=read_file_requested,
        include_search_files=search_files_requested,
        include_read_media=read_media_requested,
        include_sub_agent=sub_agent_requested,
    )
    tool_request.tools = round_tools or None
    messages: List[dict] = []
    for message in tool_request.messages or []:
        if hasattr(message, "model_dump"):
            msg = message.model_dump(exclude_none=True)
        elif hasattr(message, "dict"):
            msg = message.dict(exclude_none=True)
        else:
            msg = message
        if isinstance(msg, dict):
            msg = {k: v for k, v in msg.items() if v is not None}
        messages.append(msg)
    incoming_messages = list(messages)
    # 聊天记录，用于记录历史聊天所有信息
    session_chat_memory = await get_chat_memory_manager(session_id)
    session_file_memory = await get_file_memory_manager(session_id)
    # 新生成任务必须显式置回运行态：上一个被中断的任务可能在 finally 里置 False，
    # 否则本次 while 首轮检查直接跳过，导致新轮空转不生成
    session_chat_memory.run_task = True
    # 父级停止信号（层级取消入口）：run_task=False → 父循环与全部子任务一起停止。
    # 命名定义供模型调用 stop_checker 与 sub_agent 派发（_build_sub_agent_contexts）共享，
    # 语义与原先内联 lambda 完全一致。
    stop_checker = lambda: (not session_chat_memory.run_task)  # noqa: E731
    try:
        # 覆盖式重新回答：新消息是对最新 ask_user 提问的回答时，先截断该提问
        # 轮之后的旧回答轮，保证一个问题只有一个答案轮次。截断方法内置保护：
        # 提问轮之后若已开启普通新任务（非回答轮），则不动历史按追加处理。
        try:
            latest_answer_text = ""
            for msg in reversed(incoming_messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    latest_answer_text = content_part_to_text(msg.get("content")).strip()
                    break
            if latest_answer_text.startswith(ASK_ANSWER_PREFIX):
                removed_rounds = await session_chat_memory.truncate_rounds_for_reanswer()
                if removed_rounds:
                    print(f"[INFO] 覆盖式重新回答：已截断提问后的 {removed_rounds} 个旧轮次")
        except Exception as trunc_error:
            print(f"[WARN] 覆盖式回答截断失败（按追加处理）: {trunc_error}")
        use_backend_history = parse_bool_like(
            getattr(tool_request, "use_backend_history", None),
            parse_bool_like(load_var("USE_BACKEND_HISTORY", "true"), True)
        )
        configured_history_value = load_var("HISTORY_COMPACT_KEEP_ROUNDS", None)
        if configured_history_value is None:
            # 兼容旧版仅配置 BACKEND_HISTORY_ROUNDS 的项目；一旦聊天设置写入
            # HISTORY_COMPACT_KEEP_ROUNDS，用户设置应优先于旧环境变量。
            configured_history_value = load_var(
                "BACKEND_HISTORY_ROUNDS", DEFAULT_CONTEXT_HISTORY_ROUNDS
            )
        configured_history_rounds = parse_int_like(
            configured_history_value,
            DEFAULT_CONTEXT_HISTORY_ROUNDS,
        )
        backend_history_rounds = parse_int_like(
            getattr(tool_request, "backend_history_rounds", None),
            configured_history_rounds,
        )
        history_compaction_settings = load_context_compaction_settings()
        frontend_history_provided = frontend_provides_full_history(incoming_messages)
        if frontend_history_provided:
            # 旧客户端可能把完整历史一并提交；只保留最后一个用户问题开始的当前任务，
            # 历史部分统一从后端累计摘要重建，避免原始轮次绕过摘要-only 策略。
            current_start = _find_current_round_start(messages)
            if current_start is None:
                messages = []
                incoming_messages = []
            else:
                messages = messages[current_start:]
                incoming_messages = list(messages)
            print("[INFO] 检测到前端完整历史，已丢弃历史部分并改用后端累计摘要")
        # 检查是否存在系统提示词，添加一些提示。
        # runtime_sys_text 为任务级运行时附加文本（工作路径+系统提示），
        # 任务内历史重建（压缩后）与降级链重建候选消息时复用同一份文本。
        # 主 system 必须先于后端历史拼接合成：拼接后 messages[0] 会是后端的
        # 【历史压缩摘要】system 消息，不能把运行时文本追加给它，否则 persona 缺位。
        runtime_sys_text = build_runtime_system_text()
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] += runtime_sys_text
        else:
            messages.insert(0, {
                "role": "system",
                "content": (
                    "你是一个聪明的助手，正在使用 Ytools 程序帮忙用户，你需要自行判断是否需要调用工具，并结合你之前使用的工具（如果有）的结果，调用工具的结果只有你可见，请根据用户使用的语言回答用户问题。\n"
                    + runtime_sys_text
                ),
            })
        if use_backend_history:
            try:
                # 中断恢复：上次任务可能在压缩进行到一半时被终止（无 done 信号）。
                # 压缩结果采用"完成后一次性写入"，中断不会留下半成品数据——
                # 此处把孤儿 start 标记为 aborted（前端失效对应条目）。
                for orphan in await session_chat_memory.find_orphan_compaction_events():
                    await session_chat_memory.mark_compaction_aborted(orphan)
                    print(
                        f"[INFO] 检测到未完成的压缩（scope={orphan.get('scope')}，"
                        f"开始于 {orphan.get('timestamp')}），本次将重新压缩"
                    )
            except Exception as exc:
                print(f"[WARN] 压缩中断状态检查失败：{exc}")
            try:
                await compact_session_history_if_needed(
                    session_chat_memory,
                    tool_request,
                    settings=history_compaction_settings,
                    event_emitter=lambda payload: _emit_compaction_event(stream, session_chat_memory, payload),
                )
            except ContextCompactionError as exc:
                # 压缩模型与聊天模型均失败：终止任务，不回退到原始历史。
                detail = f"上下文压缩失败，任务已终止：{exc}"
                print(f"[ERROR] {detail}")
                session_chat_memory.run_task = False
                await _stream_emit(stream, _sse_error_payload(detail))
                return
            except Exception as exc:
                detail = f"上下文历史压缩失败，任务已终止：{exc}"
                print(f"[ERROR] {detail}")
                session_chat_memory.run_task = False
                await _stream_emit(stream, _sse_error_payload(detail))
                return
            backend_messages = await session_chat_memory.get_context_messages(
                max_rounds=backend_history_rounds
            )
            if backend_messages:
                # 主 system 保持在首位：累计摘要/最近问题/历史轮次排在其后
                messages = [messages[0]] + backend_messages + messages[1:]
                print(f"[INFO] 已拼接累计摘要和最近问题（消息 {len(backend_messages)} 条）")
        latest_user_message = None
        latest_user_text = ""
        for msg in reversed(incoming_messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                # 多模态消息 content 为部件列表：取文本部件作为问题文本，
                # 原始消息（含 media:// 媒体引用）原样落盘
                latest_user_text = content_part_to_text(msg.get("content")).strip()
                if latest_user_text:
                    latest_user_message = msg
                break
        if latest_user_message:
            await session_chat_memory.add_chat_history(latest_user_message)
            if not stream.question_text:
                stream.question_text = latest_user_text
        # read_media 的 media:// 引用常规集合（当前任务轮用户消息中出现的引用）；
        # 当前策略：工具由用户提供、无安全边界——该集合仅作为 media:// 引用的
        # 常规过滤传给 execute_read_media（本地/网络来源不在此列、不受过滤）。
        # 注意：必须在下方 resolve_message_media_refs 之前收集——该解析会把
        # media:// 就地改写为 data:/base64，改写后再扫描将永远得到空集。
        try:
            current_task_media_references = collect_media_references(messages)
        except Exception as media_collect_error:
            current_task_media_references = []
            print(f"[WARN] 当前任务媒体引用收集失败（read_media 将不可用）: {media_collect_error}")
        # 多媒体引用解析：仅解析当前轮消息 content 列表里的 media:// 引用为
        # 上游可用的 data URL / base64（image_url.url；input_audio.data 为纯
        # base64）。历史轮次用户消息为纯文本口径（媒体部件为 [图片] 等占位
        # 引用，见 chat_history_format），不回传图片数据；已压缩轮次（摘要/
        # 问题索引）不携带媒体。用户消息此刻已按原始引用落盘，历史 JSONL
        # 不会膨胀。
        try:
            unresolved_media = resolve_message_media_refs(session_id, messages)
            if unresolved_media:
                print(
                    f"[WARN] {len(unresolved_media)} 个媒体引用解析失败（文件缺失或非法），"
                    f"已原样保留: {unresolved_media[:3]}"
                )
        except Exception as media_error:
            print(f"[WARN] 媒体引用解析失败（按原始引用发送）: {media_error}")
        # 这里做文件处理，并添加文件解析内容到 "content" 字段
        user_files = session_file_memory.get_file_memory_chat()
        file_memory_suffix = ""
        # 当前生效的文件块文本：重建降级候选消息时按此复现系统提示内容
        active_file_block = ""
        if user_files:
            file_memory_suffix = (
                f"\n用户上传的 {len(user_files)} 个文件，解析的内容如下：\n"
                f"{user_files}"
            )
            active_file_block = file_memory_suffix
            messages[0]["content"] += file_memory_suffix
        # 当前任务计划注入系统提示（跨轮感知；模型可用 todo_write 全量更新）
        try:
            session_todo = await session_chat_memory.get_session_todo()
        except Exception:
            session_todo = []
        if session_todo:
            todo_lines = []
            for item in session_todo:
                mark = {"done": "[x]", "in_progress": "[>]"}.get(item.get("status"), "[ ]")
                todo_lines.append(f"{mark} {item.get('content', '')}")
            todo_suffix = (
                "\n当前任务计划（可调用 todo_write 全量更新，保持与最新进展同步）：\n"
                + "\n".join(todo_lines)
            )
            messages[0]["content"] += todo_suffix
        # P1：首次模型调用前的上下文预算检查。
        # 组装完系统提示/文件内容后，预估整个请求上下文（消息+工具定义）；
        # 超窗请求直接发往上游只会得到 400 或静默截断，这里先诊断并按需降级。
        # 基准使用模型窗口（硬限制）；单轮压缩阈值（窗口×比例）由任务内压缩负责。
        try:
            first_call_tokens = estimate_request_context_tokens(messages, tool_request.tools)
            model_window = resolve_model_max_input_tokens(default=8192)
            if first_call_tokens > model_window:
                print(
                    f"[WARN] 首次模型调用上下文 {first_call_tokens} tokens 超过模型窗口 {model_window}"
                )
                if user_files and file_memory_suffix:
                    # 文件记忆无上限（get_file_memory_chat 返回全部文件完整内容），
                    # 是最常见的超窗来源：降级为纯文本摘要后重新估算。
                    downgraded_text = session_file_memory.get_file_memory_text(
                        max_total_chars=_FIRST_CALL_FILE_MEMORY_MAX_CHARS
                    )
                    if messages[0]["content"].endswith(file_memory_suffix):
                        messages[0]["content"] = messages[0]["content"][:-len(file_memory_suffix)]
                    if downgraded_text:
                        messages[0]["content"] += (
                            f"\n用户上传的 {len(user_files)} 个文件，按上下文预算降级为摘要：\n"
                            f"{downgraded_text}"
                        )
                    downgraded_tokens = estimate_request_context_tokens(messages, tool_request.tools)
                    active_file_block = (
                        f"\n用户上传的 {len(user_files)} 个文件，按上下文预算降级为摘要：\n"
                        f"{downgraded_text}"
                        if downgraded_text
                        else ""
                    )
                    print(
                        f"[WARN] 文件记忆已降级为摘要：{first_call_tokens} -> {downgraded_tokens} tokens"
                    )
                    warning_event = {
                        "warning": {
                            "code": "CONTEXT_BUDGET_FILE_DOWNGRADED",
                            "message": (
                                f"上传文件内容超过模型窗口预算（{first_call_tokens} > {model_window} tokens），"
                                "已降级为摘要文本，可能影响文件内容的完整性。"
                            ),
                        }
                    }
                    await _stream_emit(stream, f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n")
                    first_call_tokens = downgraded_tokens
                if first_call_tokens > model_window:
                    # ===== 切换到更小窗口模型后的历史降级链 =====
                    # 强制跨轮压缩：预算按实际可用空间计算（窗口-系统-文件-当前请求-
                    # 工具定义-安全余量），把全部历史纳入累计摘要，不再回传原始轮次。
                    system_tokens = estimate_message_tokens(messages[0])
                    tools_tokens = estimate_tool_definition_tokens(tool_request.tools)
                    frontend_tokens = estimate_messages_tokens(
                        messages[max(0, len(messages) - len(incoming_messages)):]
                    )
                    history_allowance = max(
                        1024,
                        model_window - system_tokens - tools_tokens - frontend_tokens
                        - max(1024, model_window // 16),
                    )
                    try:
                        await compact_session_history_if_needed(
                            session_chat_memory,
                            tool_request,
                            budget_tokens=history_allowance,
                            enforce=True,
                            force_all=True,
                            event_emitter=lambda payload: _emit_compaction_event(stream, session_chat_memory, payload),
                        )
                    except ContextCompactionError as exc:
                        # 压缩模型与聊天模型均失败：终止任务，不再走后续降级
                        detail = f"上下文压缩失败（聊天模型重试仍不可用），任务已终止：{exc}"
                        print(f"[ERROR] {detail}")
                        session_chat_memory.run_task = False
                        await _stream_emit(stream, _sse_error_payload(detail))
                        return
                    except Exception as exc:
                        detail = f"强制历史压缩失败，任务已终止：{exc}"
                        print(f"[ERROR] {detail}")
                        session_chat_memory.run_task = False
                        await _stream_emit(stream, _sse_error_payload(detail))
                        return

                    # 继续用累计摘要视图重建候选；runtime_sys_text / active_file_block
                    # 复用任务级变量，不能回退为原始历史轮次。
                    def _assemble_candidate(backend_msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
                        # 复用任务已合成的主 system（persona+运行时文本+文件块，
                        # 含预算降级后的版本）：摘要/最近问题排在其后，
                        # 不再把运行时文本混入【历史压缩摘要】消息。
                        if (
                            messages
                            and isinstance(messages[0], dict)
                            and messages[0].get("role") == "system"
                        ):
                            main_system = copy.deepcopy(messages[0])
                        else:
                            main_system = {
                                "role": "system",
                                "content": (
                                    "你是一个聪明的助手，正在使用 Ytools 程序帮忙用户，你需要自行判断是否需要调用工具，并结合你之前使用的工具（如果有）的结果，调用工具的结果只有你可见，请根据用户使用的语言回答用户问题。\n"
                                    + runtime_sys_text
                                    + active_file_block
                                ),
                            }
                        return [main_system] + list(backend_msgs or []) + copy.deepcopy(incoming_messages)

                    degraded_messages = None
                    question_budgets = (10_000, 5_000, 2_000, 1_000, 256)
                    for question_budget in question_budgets:
                        candidate_backend = await session_chat_memory.get_context_messages(
                            max_rounds=backend_history_rounds,
                            recent_questions_token_budget=question_budget,
                        )
                        candidate = _assemble_candidate(candidate_backend)
                        if estimate_request_context_tokens(candidate, tool_request.tools) <= model_window:
                            degraded_messages = candidate
                            if question_budget != question_budgets[0]:
                                print("[INFO] 摘要上下文已按窗口收紧最近问题索引")
                            break
                    if degraded_messages is not None:
                        messages = degraded_messages
                        first_call_tokens = estimate_request_context_tokens(messages, tool_request.tools)
                        history_tokens = max(
                            0, first_call_tokens - system_tokens - tools_tokens - frontend_tokens
                        )
                        print(
                            f"[INFO] 首次模型调用上下文已按新模型窗口降级："
                            f"{first_call_tokens} tokens（摘要-only，最近问题预算已收紧，"
                            f"后端历史约 {history_tokens} tokens）"
                        )
                    else:
                        # 全部降级后仍超限，才报压缩错误
                        detail = (
                            f"首次模型调用上下文预估 {first_call_tokens} tokens，"
                            f"超过模型窗口 {model_window} tokens。"
                            f"组成：系统提示+文件 {system_tokens}、后端历史 "
                            f"{max(0, first_call_tokens - system_tokens - tools_tokens - frontend_tokens)}、"
                            f"当前请求 {frontend_tokens}、工具定义 {tools_tokens}。"
                            "已尝试强制压缩历史并收紧最近问题索引仍无法放入窗口，"
                            "请清理上传文件或开启新会话后重试。"
                        )
                        print(f"[ERROR] {detail}")
                        await _stream_emit(stream, _sse_error_payload(detail))
                        return
        except Exception as exc:
            print(f"[WARN] 首次调用预算检查跳过：{exc}")
        # 超大工具结果拒绝策略（仅任务内解析一次）：
        # 阈值 = min(聊天窗口, 压缩窗口) × 系数，连续超长达到上限后终止任务
        compaction_settings = load_context_compaction_settings()
        oversized_result_threshold = resolve_oversized_result_token_threshold(compaction_settings)
        # 任务内上下文预算管理阈值：与跨轮压缩统一使用 trigger_ratio
        context_compaction_threshold = resolve_context_compaction_threshold(compaction_settings)
        max_oversized_rejections = compaction_settings.max_oversized_rejections
        consecutive_oversized_count = 0
        # 任务内自动压缩连续失败熔断：失败不再终止任务，但连续多次失败后停止
        # 尝试（每批工具结果都会触发检查，避免反复白烧失败的模型调用）
        auto_compact_failures = 0
        auto_compact_disabled_notified = False

        # 运行中注入的用户消息（消息引导）：每轮检查点取出，作为新一轮
        # 用户消息追加到上下文并落盘；模型在下一轮调用时即可看到。
        # worker 模式 stream=_WorkerStreamAdapter、inline 模式 stream=_SessionStream，
        # 两者均提供 pop_injected_message()（鸭子类型，缺省跳过）。
        async def _consume_injected_message() -> bool:
            """取出一条注入消息加入上下文/落盘/推 SSE；队列为空返回 False。

            两个调用时机：每轮循环顶部（工具结果处理完毕后），以及任务
            收尾检查点（模型未调用工具、准备结束任务前）——后者保证注入
            消息在当前 SSE 结束后立即开启下一轮请求，而不是等到任务结束。
            """
            pop_injected = getattr(stream, "pop_injected_message", None)
            injected_message = pop_injected() if callable(pop_injected) else None
            if not (isinstance(injected_message, dict) and injected_message.get("content")):
                return False
            inject_text = content_part_to_text(injected_message.get("content")).strip()
            messages.append({
                "role": "user",
                "content": injected_message.get("content"),
                "_internal": True,
            })
            await session_chat_memory.add_chat_history({
                "role": "user",
                "content": injected_message.get("content"),
            })
            await _stream_emit(
                stream,
                f"data: {json.dumps({'event': 'message_injected', 'text': inject_text}, ensure_ascii=False)}\n\n",
            )
            print(f"[INFO] 已注入运行中用户消息（消息引导）: {inject_text[:80]}")
            return True

        # run_task = True
        # last_tool_ret = ""  # 上一次调用工具的返回值
        # while run_task:
        while session_chat_memory.run_task:
            stream.set_round_start()
            # 每轮检查点：取出注入消息，本轮模型调用即可看到
            try:
                await _consume_injected_message()
            except Exception as inject_error:
                print(f"[WARN] 处理注入消息失败（跳过）: {inject_error}")
            # 占位化只作用于"发往上游的请求副本"，不再原地改 messages：
            # messages 里的 reasoning_content 必须保持模型原始输出——
            # 每一轮（含旧工具轮）的真实思考都完整保留并随 add_chat_history
            # 原样落盘，历史回放/审计永远能看到全部思考过程；
            # 原地占位化会把"数据源"也污染掉，落盘跟着失真成 "..."。
            # 请求侧语义：副本中最多只保留一条真实思考——最近一次 API 调用
            # 输出的那条（本轮没有时向前回溯最近一次真实思考），按回传长度
            # 裁剪；找不到真实思考或 limit==0 时用 "..." 占位。历史 assistant
            # （含旧工具轮）一律剥离 reasoning_content，严格上游的字段校验
            # 仍满足（见 _copy_for_request）。
            # 调试打印改为单行概要：不再整包 str(messages)（带图轮次会刷出
            # MB 级 base64，且任务循环每轮重复打印同一张图，易误判为重复携带）
            print(f"[DEBUG] 模型调用消息概要：{_messages_debug_summary(messages)}")
            # 大上下文下整包转储会向控制台刷 MB 级文本并拖慢循环，只打印概要
            # print(f"[DEBUG] 本轮模型调用：消息 {len(messages)} 条")
            full_response = ""
            full_reasoning = ""
            finish_reason = None  # 追踪模型完成原因：stop（正常完成）、length（token 截断）、tool_calls（工具调用）
            pending_finish_payload: dict[str, Any] | None = None  # 延后发送 finish_reason，先发 usage 尾帧
            stream_error = None  # 追踪上游流式错误（与用户手动停止区分，避免误记为"停止任务"）
            tool_calls: List[dict] = []
            usage_accumulator = UsageAccumulator()
            # 将 messages 附加到 tool_request 中（请求侧副本占位化，
            # 运行时 messages 保持模型原始输出）
            tool_request.messages = build_request_messages(_copy_for_request(messages))
            # 工具调用流式阶段无输出超时（0=不限制）：部分服务商在 SSE 输出
            # tool_calls 期间会无限卡住（连接不断、永无后续事件），HTTP 读
            # 超时（默认 1800s）既兜不住也不该终止任务。首个 tool_calls
            # 增量到达后开始按"事件间隔"计时（期间任意后续事件都会续期，
            # finish_reason 到达后停止计时）；超时只放弃本次工具调用（以
            # 失败结果反馈模型并继续运行），不终止任务。
            tool_call_stream_timeout = _load_tool_call_stream_timeout()
            tool_phase_deadline: float | None = None
            tool_phase_timeout_hit = False
            # 手动迭代 async generator（而非 async for）：需要对工具调用阶段
            # 施加"事件间隔"超时；超时/提前退出时显式 aclose 立即断开上游连接
            sse_iter = ChatLLM.chat_completions(
                request = tool_request, # 传递完整的 ChatLLMRequest 对象（已包含 messages）
                stream = True,
                stop_checker = stop_checker  # 父级停止信号（与 sub_agent 派发共享同一 closure）
            ).__aiter__()
            try:
                while True:
                    if tool_phase_deadline is None or tool_call_stream_timeout <= 0:
                        try:
                            sse_chunk = await sse_iter.__anext__()
                        except StopAsyncIteration:
                            break
                    else:
                        remaining = tool_phase_deadline - time.monotonic()
                        if remaining <= 0:
                            # finish_reason 已到达后的尾部停顿（usage/[DONE]
                            # 尾帧迟到）不算工具调用失败：按正常收尾处理
                            tool_phase_timeout_hit = finish_reason is None
                            break
                        try:
                            sse_chunk = await asyncio.wait_for(
                                sse_iter.__anext__(), timeout=remaining
                            )
                        except asyncio.TimeoutError:
                            tool_phase_timeout_hit = finish_reason is None
                            break
                        except StopAsyncIteration:
                            break
                    # 工具调用阶段收到任意事件都续期（含 usage/finish 等尾帧）
                    if tool_phase_deadline is not None and tool_call_stream_timeout > 0:
                        tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
                    # print(f"sse_chunk: {sse_chunk}")
                    event = _parse_sse_event(sse_chunk)
                    if event is None:
                        await _stream_emit(stream, sse_chunk)
                        continue
                    if event.get("done") or not session_chat_memory.run_task:
                        if pending_finish_payload is not None:
                            await _stream_emit(stream, f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n")
                            pending_finish_payload = None
                        break
                    if event.get("error") is not None:
                        await _stream_emit(stream, sse_chunk)
                        if event.get("retrying"):
                            # 网络失败重试帧：ChatLLM 内部正在按配置自动重试，
                            # 只透传给前端提示，不终止任务；连续失败达到重试上限时
                            # 上游会改发 retrying=False 的终止性错误帧。
                            # 重试是瞬态过程（多数情况下下一次就成功），中间失败
                            # 不落盘——只保留前端实时提示；最终失败由下方终止帧
                            # 统一落盘（附带完整重试统计），避免轮次被过程性
                            # 事件塞满。注意此处不能 continue 掉任务状态维护。
                            continue
                        stream_error = event.get("error")
                        stream_error_meta = event if isinstance(event, dict) else {}
                        session_chat_memory.run_task = False
                        break
                    # ChatLLM 做了统一处理，会单独发送 usage 事件（不带 finish_reason / choices）
                    # 只要本事件携带 usage 就尝试收集，后续由 completion_id/指纹去重
                    usage_accumulator.collect(event)
                    # 捕获 finish_reason 信号（stop/length/tool_calls）
                    if event.get("finish_reason") is not None:
                        finish_reason = event["finish_reason"]
                        pending_finish_payload = {"finish_reason": finish_reason}
                        if event.get("id") is not None:
                            pending_finish_payload["id"] = event["id"]
                        if event.get("usage") is not None:
                            pending_finish_payload["usage"] = event["usage"]
                        # 模型输出已结束（后续只剩 usage/[DONE] 尾帧）：保留
                        # 计时但标记 finish 已到达——尾帧若停顿按正常收尾，
                        # 不再判定为工具调用失败（超时分支检查 finish_reason）
                        event = event.copy()
                        event.pop("finish_reason", None)
                    choices = event.get("choices")
                    # 部分服务会在 finish_reason 之后再发一帧 choices:[] + usage 统计帧
                    # 这类帧需要优先透传 usage，然后再发送 finish_reason，避免前端提前收流
                    if isinstance(choices, list) and len(choices) == 0 and event.get("usage") is not None:
                        usage_event = {"type": "usage", "usage": event["usage"]}
                        if event.get("id") is not None:
                            usage_event["id"] = event["id"]
                        await _stream_emit(stream, f"data: {json.dumps(usage_event, ensure_ascii=False)}\n\n")
                        if pending_finish_payload is not None:
                            await _stream_emit(stream, f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n")
                            pending_finish_payload = None
                        continue
                    content = event.get("content")
                    reasoning_content = event.get("reasoning_content")
                    tool_calls_delta = event.get("tool_calls")
                    function_call_delta = event.get("function_call")  # 兼容老模型
                    if content:
                        full_response += content
                        print(f"{content}", end="", flush=True)
                    if reasoning_content:
                        full_reasoning += reasoning_content
                        print(f"{reasoning_content}", end="", flush=True)
                    # 优先处理 tool_calls，如果为空再处理 function_call（兼容老模型）
                    if tool_calls_delta:
                        _merge_tool_call_delta(tool_calls, tool_calls_delta)
                        if tool_call_stream_timeout > 0:
                            tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
                        # 调试：打印 tool_calls 累积情况
                        # for delta in tool_calls_delta:
                        #     if isinstance(delta, dict):
                        #         idx = delta.get("index", "?")
                        #         func = delta.get("function", {})
                        #         name_part = func.get("name") or ""  # 确保不是 None
                        #         args_part = func.get("arguments") or ""  # 确保不是 None
                        #         if name_part or args_part:
                        #             print(f"[STREAM] tool_call[{idx}] += name:'{name_part[:30]}', args_len:{len(args_part)}", flush=True)
                    elif function_call_delta:
                        # 兼容老模型的 function_call 字段
                        _merge_function_call_delta(tool_calls, function_call_delta)
                        if tool_call_stream_timeout > 0:
                            tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
                        print(f"[DEBUG] function_call_delta: {function_call_delta}")
                        # fc_name = function_call_delta.get("name") or ""
                        # fc_args = function_call_delta.get("arguments") or ""
                        # if fc_name or fc_args:
                        #     print(f"[STREAM] function_call += name:'{fc_name[:30]}', args_len:{len(fc_args)}", flush=True)
                    # 过滤 tool_calls 中的 id 和 type 字段后再输出
                    filtered_event = _filter_tool_calls_fields(event)
                    await _stream_emit(stream, f"data: {json.dumps(filtered_event, ensure_ascii=False)}\n\n")
            finally:
                # 显式关闭上游响应流：超时/提前 break 时立即断开连接，
                # 不让卡死的 SSE 连接残留在后台
                try:
                    await sse_iter.aclose()
                except Exception:
                    pass
            if usage_accumulator.count > 0:
                round_usage_total: dict[str, Any] = {}
                usage_accumulator.merge_to(round_usage_total, _merge_usage_values)
                await session_chat_memory.update_current_round_usage(
                    usage_total=round_usage_total,
                    completion_count=usage_accumulator.count,
                )
            # print(f"[DEBUG] tool_calls: {tool_calls}")
            if pending_finish_payload is not None:
                await _stream_emit(stream, f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n")
                pending_finish_payload = None
            # 上游模型流出错（如连接被切断/重试达到上限）：记录真实错误原因，
            # 而不是伪装成"用户停止任务"。中间重试过程不落盘（仅前端提示），
            # 这里是唯一落盘点：附带完整重试统计，追溯信息不丢。
            if stream_error is not None:
                print(f"\n[ERROR] 模型流式响应出错: {stream_error}")
                # 记录 ai 已生成的内容
                if full_reasoning:
                    await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
                if full_response:
                    await session_chat_memory.add_chat_history({"role": "assistant", "content": full_response})
                # 记录真实错误，便于事后追溯；附带重试统计（本次流内的失败次数
                # 与重试上限），重试过程本身不落盘、由该终止记录统一呈现
                error_record: dict[str, Any] = {"role": "assistant", "error": str(stream_error)}
                final_retry_count = stream_error_meta.get("retry")
                final_max_attempts = stream_error_meta.get("max_attempts")
                if final_retry_count is not None:
                    error_record["retry"] = final_retry_count
                if final_max_attempts is not None:
                    error_record["max_attempts"] = final_max_attempts
                if stream_error_meta.get("step"):
                    error_record["step"] = stream_error_meta.get("step")
                if stream_error_meta.get("error_detail"):
                    error_record["error_detail"] = stream_error_meta.get("error_detail")
                await session_chat_memory.add_chat_history(error_record)
                break  # 任务因错误结束，退出循环
            # 模型回复过程中用户手动停止任务
            if not session_chat_memory.run_task:
                print("停止任务")
                # 记录 ai 生成的内容
                if full_reasoning:
                    await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
                if full_response:
                    await session_chat_memory.add_chat_history({"role": "assistant", "content": full_response})
                # 手动停止：不再写入"停止任务"假消息，直接以 stopped 状态收尾当前轮次
                await session_chat_memory.stop_current_round()
                break  # 任务结束，退出循环
            known_tool_names = list(round_tool_servers.keys())
            # 归一化工具调用，修复流式拼接异常（函数名/参数被连在一起）
            tool_calls = normalize_tool_calls(tool_calls, known_tool_names)
            # 工具调用流式阶段超时且没积累出任何可用调用（首个增量都没有，
            # 或者调用结构损坏被归一化丢弃）：不终止任务，以内部消息提示
            # 模型重新发起工具调用（内部消息不落盘，与截断续写路径一致）
            if tool_phase_timeout_hit and not tool_calls:
                timeout_notice = (
                    f"刚才的工具调用因上游服务在工具调用阶段超过 "
                    f"{int(tool_call_stream_timeout)} 秒无输出而失败（接口卡死，"
                    "参数可能不完整），本次调用已放弃。请重新发起工具调用或继续回答。"
                )
                print(f"[WARN] 工具调用流式阶段超时（未收到完整调用），提示模型重试")
                messages.append({"role": "user", "content": timeout_notice, "_internal": True})
                await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'TOOL_CALL_STREAM_TIMEOUT', 'message': timeout_notice}}, ensure_ascii=False)}\n\n")
                continue
            # 检查 tool_calls 是否为空
            if not tool_calls:
                # 如果 finish_reason 是 "length"，说明模型输出因 max_tokens 不足被截断
                # 需要提示模型继续完成回答，而不是按"未调用工具"的逻辑处理
                if finish_reason == "length":
                    print(f"[WARN] 模型输出被截断(finish_reason=length)，提示模型继续完成回答")
                    assistant_message = {}
                    if full_response:
                        assistant_message = {"role": "assistant", "content": full_response}
                    elif full_reasoning:
                        assistant_message = {"role": "assistant", "content": f"...{full_reasoning[-100:]}"}
                    messages.append(assistant_message)
                    messages.append({"role": "user", "content": "你的回答因为长度限制被截断了，请继续。", "_internal": True})
                    continue  # 让模型继续，不进入"未调用工具"的逻辑
                assistant_message = {"role": "assistant", "content": full_response} if full_response else None
                if assistant_message is None and full_reasoning:
                    assistant_message = {"role": "assistant", "content": ""}
                if assistant_message is not None:
                    # 思考模型的最终回答同样落盘思考过程（SSE 已实时展示，
                    # JSONL 缺失会导致历史回放/审计看不到思考）。落盘永远
                    # 保存模型原始思考全文，不按回传长度裁剪——裁剪只是
                    # "回传"语义，由请求副本（_copy_for_request）承担。
                    # 历史回放不重发 reasoning，无上游 400 风险。
                    if full_reasoning:
                        assistant_message["reasoning_content"] = full_reasoning
                    messages.append(assistant_message)
                    await session_chat_memory.add_chat_history(assistant_message)
                # 任务收尾检查点：注入队列还有待处理消息时不结束任务，
                # 以该消息立即开启下一轮模型调用（当前 SSE 已结束、下次
                # 请求发起前生效，避免注入消息要等任务全部完成后才发起）
                try:
                    if await _consume_injected_message():
                        print("[INFO] 注入消息接管任务收尾：继续下一轮模型调用")
                        continue
                except Exception as inject_error:
                    print(f"[WARN] 处理注入消息失败（按任务结束处理）: {inject_error}")
                session_chat_memory.run_task = False
                print("\n[INFO] 本轮对话未返回工具调用，任务结束")
                break
            # 构建包含 tool_calls 的 assistant 消息（符合 OpenAI API 规范）
            assistant_message = {"role": "assistant"}
            if full_response:
                assistant_message["content"] = full_response
            elif full_reasoning:
                assistant_message["content"] = f"...{full_reasoning[-100:]}"
            # 带 tool_calls 的 assistant 消息：思考过程落盘模型原始全文
            # （本轮有思考就存本轮的；没有就不写字段，也不拿历史旧思考
            # 冒充本轮落盘）。裁剪/占位符只是"回传"语义——严格上游要求
            # 工具调用链每条 assistant 必须带 reasoning_content——该契约
            # 完全由请求副本（_copy_for_request）在发请求前补齐：本轮思考
            # → 回退上下文最近真实思考 → "..." 占位，长度按
            # REASONING_RETURN_MAX_LENGTH 裁剪（0=只回占位符）。
            if full_reasoning:
                assistant_message["reasoning_content"] = full_reasoning
            formatted_tool_calls = []
            for tc in tool_calls:
                function_info = tc.get("function") or {}
                tool_name = function_info.get("name")
                if not tool_name:
                    # 结构损坏（连函数名都没有）：无法形成有意义的调用与
                    # 结果配对，跳过（下方 prepare 也会跳过它，不产生孤儿）
                    continue
                # 未知/未授权工具同样保留在 assistant.tool_calls 里：上游对
                # 「assistant 声明的每个 tool_call_id 必须有配对的 tool 结果
                # 消息」做严格校验——若这里剔除而下方 blocked_tools 又为它
                # 生成 tool 结果，会形成"结果无声明"的孤儿 → 上游 400 断流、
                # 任务直接终止（用户切换工具后模型模仿历史调用旧工具时必现）。
                # 保留声明 + 下方统一拦截反馈，模型能自我纠正，任务继续。
                formatted_tc = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": function_info.get("name", ""),
                        "arguments": function_info.get("arguments", "{}")
                    }
                }
                formatted_tool_calls.append(formatted_tc)
            # 工具调用流式阶段超时（调用结构已完整/部分可用）：不执行工具，
            # 把本次调用按失败处理反馈模型并继续运行——assistant（tool_calls）
            # 消息与每个工具结果成对落盘，保持工具调用链契约完整，模型可
            # 基于失败结果重试或继续回答
            if tool_phase_timeout_hit:
                timeout_message = (
                    f"工具调用失败：模型服务在输出工具调用的过程中超过 "
                    f"{int(tool_call_stream_timeout)} 秒无任何响应（上游接口疑似卡死），"
                    "本次调用已被中止，参数可能不完整，请重新发起工具调用。"
                )
                print(f"[WARN] 工具调用流式阶段超时：放弃 {len(formatted_tool_calls)} 个调用，反馈模型继续")
                if formatted_tool_calls:
                    assistant_message["tool_calls"] = formatted_tool_calls
                    messages.append(assistant_message)
                    await session_chat_memory.add_chat_history(assistant_message)
                    for formatted_tc in formatted_tool_calls:
                        timeout_tool_name = formatted_tc["function"]["name"]
                        timeout_call_id = formatted_tc["id"]
                        timeout_arguments = formatted_tc["function"]["arguments"]
                        await session_chat_memory.add_chat_history({
                            "role": "tool",
                            "tool_call_id": timeout_call_id,
                            "tool_name": timeout_tool_name,
                            "arguments": timeout_arguments,
                            "result": timeout_message,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": timeout_call_id,
                            "content": timeout_message,
                            "_tool_name": timeout_tool_name,
                        })
                        await _stream_emit(stream, f"data: {json.dumps({'tool_return': {'function_name': timeout_tool_name, 'arguments': timeout_arguments, 'result': timeout_message, 'timeout': True}}, ensure_ascii=False)}\n\n")
                else:
                    # 部分调用连工具名都不完整：不写空 tool_calls 的 assistant
                    # 消息（回传上游会 400），改用内部消息提示模型重试
                    messages.append({"role": "user", "content": timeout_message, "_internal": True})
                await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'TOOL_CALL_STREAM_TIMEOUT', 'message': timeout_message}}, ensure_ascii=False)}\n\n")
                continue
            if not formatted_tool_calls:
                # 极端防御：本轮所有调用连函数名都无法解析（全部损坏），
                # 不写带空 tool_calls 的 assistant 消息（回传上游会 400），
                # 以内部消息提示模型重新发起或直接回答，任务继续不终止
                broken_notice = (
                    "刚才的工具调用格式损坏（未能解析出函数名），本次调用已放弃。"
                    "请重新发起工具调用，或直接回答用户。"
                )
                print("[WARN] 工具调用全部损坏（无函数名），提示模型重试")
                messages.append({"role": "user", "content": broken_notice, "_internal": True})
                await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'TOOL_CALL_BROKEN', 'message': broken_notice}}, ensure_ascii=False)}\n\n")
                continue
            assistant_message["tool_calls"] = formatted_tool_calls
            messages.append(assistant_message)
            await session_chat_memory.add_chat_history(assistant_message)
            # 验证 tool_calls 的完整性
            # print(f"\n[DEBUG] 接收到 {len(tool_calls)} 个工具调用")
            # for i, tc in enumerate(tool_calls):
            #     func_info = tc.get("function", {})
            #     tool_name = func_info.get("name", "unknown")
            #     arguments_str = func_info.get("arguments", "")
            #     print(f"  [{i}] 工具名: {tool_name}")
            #     print(f"      参数字符串长度: {len(arguments_str)}")
            #     # 尝试验证 JSON 是否完整
            #     if arguments_str:
            #         try:
            #             parsed_args = json.loads(arguments_str)
            #             print(f"      ✅ 参数JSON格式正确，键: {list(parsed_args.keys())}")
            #         except json.JSONDecodeError as e:
            #             print(f"      ❌ 参数JSON不完整或格式错误: {e}")
            #             print(f"      参数字符串预览: {arguments_str[:200]}...")
            plan = prepare_tool_execution(tool_calls, known_tool_names)
            parsed_tools = plan.parsed_tools
            if plan.has_parse_error:
                print("[WARNING] 检测到工具参数解析错误，可能导致工具执行失败")
            blocked_tools = [item for item in parsed_tools if item[2] not in round_tool_servers]
            parsed_tools = [item for item in parsed_tools if item[2] in round_tool_servers]
            if blocked_tools:
                print(f"[WARNING] 检测到 {len(blocked_tools)} 个未授权工具调用，已拦截")
                for _, blocked_tool_call, blocked_tool_name, blocked_tool_args in blocked_tools:
                    blocked_tool_call_id = blocked_tool_call.get("id", "") if isinstance(blocked_tool_call, dict) else ""
                    blocked_ret = (
                        f"工具 {blocked_tool_name} 不在本轮可用工具列表中"
                        "（可能不存在，或已被用户取消选择），本次调用未执行。"
                        "请改用本轮可用的工具完成任务，或直接向用户说明该工具当前不可用。"
                    )
                    await session_chat_memory.add_chat_history(
                        input_text={
                            "role": "tool",
                            "tool_call_id": blocked_tool_call_id,
                            "tool_name": blocked_tool_name,
                            "arguments": json.dumps(blocked_tool_args, ensure_ascii=False),
                            "result": blocked_ret,
                        }
                    )
                    blocked_tool_message = {
                        "role": "tool",
                        "tool_call_id": blocked_tool_call_id,
                        "content": blocked_ret,
                        "_tool_name": blocked_tool_name,
                    }
                    messages.append(blocked_tool_message)
                    blocked_event = {
                        "tool_return": {
                            "function_name": blocked_tool_name,
                            "arguments": blocked_tool_args,
                            "result": blocked_ret,
                            "blocked": True,
                        }
                    }
                    await _stream_emit(stream, f"data: {json.dumps(blocked_event, ensure_ascii=False)}\n\n")
                if not parsed_tools:
                    continue
            if not parsed_tools and not round_tool_servers:
                # over_task 语义为"模型宣布任务结束"：先为该声明生成配对的
                # tool 结果（assistant.tool_call_id 与 tool 结果一一配对的
                # 契约），再以普通回答收尾——否则历史里留下无结果孤儿，
                # 下一轮回放发上游时校验失败断流
                if plan.over_task_call:
                    _, ot_tc, _, _ = plan.over_task_call
                    ot_id = ot_tc.get("id", "") if isinstance(ot_tc, dict) else ""
                    ot_ret = "任务已由模型宣布结束（over_task）。"
                    await session_chat_memory.add_chat_history({
                        "role": "tool",
                        "tool_call_id": ot_id,
                        "tool_name": "over_task",
                        "arguments": "{}",
                        "result": ot_ret,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": ot_id,
                        "content": ot_ret,
                        "_tool_name": "over_task",
                    })
                assistant_message = {"role": "assistant", "content": full_response} if full_response else None
                if assistant_message is not None:
                    messages.append(assistant_message)
                    await session_chat_memory.add_chat_history(assistant_message)
                session_chat_memory.run_task = False
                break
            # 如果没有任何有效工具可执行，退出
            if not parsed_tools:
                # 同上：over_task 结束信号也要生成配对结果再退出，
                # 避免落盘历史里出现"声明无结果"的孤儿 tool_call
                if plan.over_task_call:
                    _, ot_tc, _, _ = plan.over_task_call
                    ot_id = ot_tc.get("id", "") if isinstance(ot_tc, dict) else ""
                    ot_ret = "任务已由模型宣布结束（over_task）。"
                    await session_chat_memory.add_chat_history({
                        "role": "tool",
                        "tool_call_id": ot_id,
                        "tool_name": "over_task",
                        "arguments": "{}",
                        "result": ot_ret,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": ot_id,
                        "content": ot_ret,
                        "_tool_name": "over_task",
                    })
                print("[WARNING] 没有可执行的有效工具，退出循环")
                break
            print(f"\n[INFO] 准备执行 {len(parsed_tools)} 个工具")
            # 工具开始执行事件：参数已完整、即将调用。前端据此把对应工具块
            # 置为"执行中"，等待结果期间有可感知状态。内置工具（write_file/
            # edit_file/read_file/search_files/todo_write 等）与外部 MCP 工具
            # 统一推送；被拦截的未授权工具不推送（它们会立即返回拒绝结果）。
            try:
                for _, start_tc, start_tn, start_ta in parsed_tools:
                    start_event = {
                        "tool_start": {
                            "function_name": start_tn,
                            "arguments": start_ta,
                            # V1 sub_agent：补 tool_call_id（additive），
                            # 前端据此在子任务事件到达前预建独立块；
                            # 旧前端忽略未知字段不受影响
                            "tool_call_id": (
                                start_tc.get("id", "") if isinstance(start_tc, dict) else ""
                            ),
                        }
                    }
                    await _stream_emit(
                        stream,
                        f"data: {json.dumps(start_event, ensure_ascii=False)}\n\n",
                    )
            except Exception as start_emit_error:
                print(f"[WARNING] tool_start 事件推送失败: {start_emit_error}")
            # 内置工具与 MCP 工具分开执行
            builtin_results = []
            external_parsed_tools = []
            sub_agent_calls: list[tuple[int, dict, str, list[dict] | None]] = []
            todo_updated = False
            ask_user_pending = False
            ask_user_questions: list[dict[str, Any]] = []
            # read_media 已注入的引用与待注入坐标（本轮任务内累计，防止重复读取）；
            # 坐标为 (reference, quality) 对，注入时现场加载 base64 部件
            read_media_injected_refs: set[str] = set()
            read_media_pending_parts: list[tuple[str, Any]] = []
            for idx, tc, tn, ta in parsed_tools:
                if tn == TODO_TOOL_NAME:
                    # 模型自我规划：校验并写入会话 todo（_meta.todo），随后推 SSE 给前端
                    todo_result, todo_items = await _apply_session_todo(session_chat_memory, ta)
                    if todo_items is not None:
                        todo_updated = True
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": todo_result,
                        "error": None,
                    })
                    continue
                if tn == ASK_USER_TOOL_NAME:
                    # 向用户提问：落一个占位工具结果保持 tool_calls 消息契约完整，
                    # 推 SSE 事件给前端渲染交互卡片；用户回答将作为下一条用户
                    # 消息开启新任务，届时模型基于回答继续
                    questions = normalize_ask_questions(
                        ta.get("questions") if isinstance(ta, dict) else None)
                    if questions is None:
                        ask_result: dict[str, Any] = {
                            "error": "questions 参数无效：需要 1-3 个对象数组，每个对象包含"
                                     "非空 question（≤200 字符）与可选 options（0-6 个非空字符串，每项 ≤100 字符）",
                        }
                    else:
                        ask_result = {
                            "status": "waiting_user",
                            "message": "问题已展示给用户，当前任务已暂停；用户的回答将作为下一条用户消息到达，"
                                       "收到后请基于回答继续任务",
                            "questions": questions,
                        }
                        ask_user_pending = True
                        # 并行多个 ask_user 调用时问题合并推送，不互相覆盖
                        ask_user_questions.extend(questions)
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": ask_result,
                        "error": None,
                    })
                    continue
                if tn == READ_MEDIA_NAME:
                    # 读取当前任务媒体：把 media:// 引用解析为媒体加载坐标
                    #（reference+quality），数据本体（base64）不进入工具结果文本
                    #（避免刷 JSONL / 超大结果截断），在下方统一处理时按坐标
                    # 现场加载为 user 消息部件（仅内存）
                    read_media_result = execute_read_media(
                        ta,
                        session_id,
                        current_task_media_references,
                        already_injected=read_media_injected_refs,
                    )
                    if read_media_result.get("ok"):
                        read_media_injected_refs.update(
                            read_media_result.get("injected_references") or []
                        )
                        loaded_meta = read_media_result.get("loaded") or []
                        read_media_pending_parts.extend([
                            (item["reference"], item.get("quality"))
                            for item in loaded_meta
                            if item.get("reference")
                        ])
                        # 任务内累计滚动窗口：只保留最近 5 个部件；
                        # 被挤出的引用从已注入集合移除，再次读取可重新注入
                        evicted_refs: set[str] = set()
                        read_media_pending_parts = roll_recent_media_parts(
                            read_media_pending_parts,
                            evicted=evicted_refs,
                        )
                        read_media_injected_refs.difference_update(evicted_refs)
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": read_media_result,
                        "error": None,
                    })
                    continue
                if tn == SUB_AGENT_TOOL_NAME:
                    # 子智能体派发：解析 task/todo；真正的执行（并发 gather）
                    # 在下方 external_parsed_tools 阶段统一进行，保证子任务与
                    # MCP 工具同轮并发（builtin_results 只放校验错误占位）。
                    task_text, task_error = normalize_sub_agent_task(
                        ta.get("task") if isinstance(ta, dict) else None)
                    if task_error is not None:
                        builtin_results.append({
                            "index": idx,
                            "tool_call": tc,
                            "tool_name": tn,
                            "tool_args": ta,
                            "result": {"error": task_error},
                            "error": None,
                        })
                        continue
                    initial_todo = None
                    if isinstance(ta, dict) and isinstance(ta.get("todo"), list):
                        items, _notices, todo_error = normalize_todo_items(
                            ta.get("todo"), prev_items=[])
                        if todo_error is None and items is not None:
                            initial_todo = items
                        elif todo_error:
                            print(f"[WARN] sub_agent 预置 todo 无效，忽略：{todo_error}")
                    sub_agent_calls.append((idx, tc, task_text, initial_todo))
                    continue
                builtin_result = execute_builtin_tool(
                    tn,
                    ta,
                    configured_tool_names,
                    configured_tool_servers,
                    enabled_tool_names=set(round_tool_servers),
                )
                if builtin_result is not None:
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": builtin_result,
                        "error": None,
                    })
                    continue
                # 内置文件编辑工具（write_file/edit_file）：服务端本地执行，
                # 结构化结果（path/replacements 等）为后续文件 diff 预留
                file_tool_result = try_execute_builtin_file_tool(tn, ta)
                if file_tool_result is not None:
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": file_tool_result,
                        "error": None,
                    })
                else:
                    external_parsed_tools.append((idx, tc, tn, ta))
            tool_results = []
            # 使用线程池并发执行 MCP 工具。
            # execute_tool_round 是同步阻塞函数（内部 ThreadPoolExecutor +
            # future.result 等待），必须放到工作线程执行：直接在事件循环里
            # 调用会冻结整个服务（工具卡住时 token_stats/stop_chat 等所有
            # 请求无响应），手动停止也无法生效。
            if external_parsed_tools:
                max_workers = min(int(load_var(
                    "ONE_TASK_MAX_WORKERS", 3)),
                    len(external_parsed_tools))  # 默认最多 3 个线程
                tool_results.extend(await asyncio.to_thread(
                    execute_tool_round,
                    parsed_tools=external_parsed_tools,
                    tool_mcp_servers=round_tool_servers,
                    max_workers=max_workers,
                ))
            # sub_agent 子任务并发执行：与上面 MCP 线程池调用串行开始（to_thread
            # 已让出事件循环），但子任务之间 gather 并发；信号量限流在
            # run_sub_agent_batch 内部。子任务事件经 emit 回调写入 JSONL 并推 SSE。
            if sub_agent_calls:
                sub_contexts = _build_sub_agent_contexts(
                    sub_agent_calls,
                    round_tools=round_tools,
                    round_tool_servers=round_tool_servers,
                    configured_tool_names=configured_tool_names,
                    configured_tool_servers=configured_tool_servers,
                    session_id=session_id,
                    session_chat_memory=session_chat_memory,
                    stream=stream,
                    stop_checker=stop_checker,
                )
                tool_results.extend(await run_sub_agent_batch(sub_contexts))
            if builtin_results:
                tool_results.extend(builtin_results)
            if todo_updated:
                try:
                    todos = await session_chat_memory.get_session_todo()
                    await _stream_emit(
                        stream,
                        f"data: {json.dumps({'event': 'todo', 'todos': todos}, ensure_ascii=False)}\n\n",
                    )
                except Exception as todo_emit_error:
                    print(f"[WARN] todo 事件推送失败: {todo_emit_error}")
            if tool_results:
                print(f"[INFO] 有 {len(tool_results)} 个工具执行完成", flush=True)
                # 按索引排序结果
                tool_results.sort(key=lambda x: x['index'])
                oversized_abort = False
                # 统一添加所有工具结果到 messages 和历史记录（使用 session_id 隔离）
                # 获取当前会话的记忆管理器
                for tool_result in tool_results:
                    tool_call = tool_result['tool_call']
                    tool_call_id = tool_call.get("id", "")
                    tool_name = tool_result['tool_name']
                    tool_args = tool_result['tool_args']
                    ret = _format_tool_result(tool_result['result'])
                    # 超大结果拒绝：超过阈值时结果不进入模型上下文，
                    # 改为写入"输出过长"反馈让模型重新考虑工具使用；
                    # 原始结果只保留截断预览到 JSONL，连续超长达到上限后终止任务
                    if oversized_result_threshold > 0 and estimate_text_tokens(ret) > oversized_result_threshold:
                        consecutive_oversized_count += 1
                        estimated_tokens = estimate_text_tokens(ret)
                        oversized_feedback = build_oversized_tool_feedback(
                            tool_name, estimated_tokens, oversized_result_threshold, result_text=ret
                        )
                        print(
                            f"[WARN] 工具 {tool_name} 返回结果过大"
                            f"（估算 {estimated_tokens} tokens > 拒绝阈值 {oversized_result_threshold}），"
                            f"连续 {consecutive_oversized_count}/{max_oversized_rejections} 次，"
                            "已告知模型重新考虑工具使用"
                        )
                        if consecutive_oversized_count >= max_oversized_rejections:
                            # 达到连续拒绝上限：终止任务
                            await session_chat_memory.add_chat_history(
                                {"role": "assistant", "content": oversized_feedback}
                            )
                            await _stream_emit(
                                stream,
                                f"data: {json.dumps({'content': oversized_feedback}, ensure_ascii=False)}\n\n",
                            )
                            session_chat_memory.run_task = False
                            oversized_abort = True
                            break
                        # JSONL 记录：不落完整结果，落截断预览 + 拒绝信息（保留关键头尾）
                        await session_chat_memory.add_chat_history(
                            input_text={
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "arguments": json.dumps(tool_args, ensure_ascii=False),
                                "result": oversized_feedback,
                                "result_preview": make_oversized_result_preview(ret),
                                "oversized": True,
                                "oversized_estimated_tokens": estimated_tokens,
                            }
                        )
                        # 发送结果到前端 SSE（携带超长标记，前端仍按普通 tool_return 渲染）
                        oversized_event = {
                            "tool_return": {
                                "function_name": tool_name,
                                "arguments": tool_args,
                                "result": oversized_feedback,
                                "oversized": True,
                            }
                        }
                        await _stream_emit(stream, f"data: {json.dumps(oversized_event, ensure_ascii=False)}\n\n")
                        # 以 tool role 添加超长反馈消息（符合 OpenAI API 规范），模型将看到并重新规划
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": oversized_feedback,
                            "_tool_name": tool_name,
                            "_oversized": True,
                        })
                        continue
                    # 正常结果：重置连续超长计数，按原逻辑落盘
                    consecutive_oversized_count = 0
                    await session_chat_memory.add_chat_history(
                        input_text={
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "arguments": json.dumps(tool_args, ensure_ascii=False),
                            "result": ret
                        }
                    )
                    # 发送工具调用结果到前端 SSE
                    tool_ret = {
                        "tool_return": {
                            "function_name": tool_name,
                            "arguments": tool_args,
                            "result": ret
                        }
                    }
                    if tool_name == SUB_AGENT_TOOL_NAME and isinstance(tool_result.get("_sub_agent"), dict):
                        # 子任务最终回复：前端不渲染为普通工具气泡，
                        # 而是在对应子任务块尾部显示"最终回复已返回父智能体"引用条；
                        # 轨迹数据已由 sub_agent 事件流渲染，这里只带状态聚合
                        tool_ret["tool_return"]["sub_agent"] = tool_result["_sub_agent"]
                    await _stream_emit(stream, f"data: {json.dumps(tool_ret)}\n\n")
                    # 以 tool role 添加工具结果消息（符合 OpenAI API 规范）
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": ret,
                        "_tool_name": tool_name,
                    }
                    messages.append(tool_message)
                if todo_updated:
                    try:
                        latest_todos = await session_chat_memory.get_session_todo()
                        _replace_todo_context(messages, latest_todos)
                    except Exception as todo_compact_error:
                        print(f"[WARN] todo 上下文归并失败：{todo_compact_error}")
                if ask_user_pending and not oversized_abort:
                    # 推送提问事件：前端渲染问题/选项/自由输入卡片；
                    # 回答作为下一条用户消息发送，与本轮任务解耦
                    try:
                        ask_event = {"event": "ask_user", "questions": ask_user_questions}
                        await _stream_emit(
                            stream,
                            f"data: {json.dumps(ask_event, ensure_ascii=False)}\n\n",
                        )
                    except Exception as ask_emit_error:
                        print(f"[WARN] ask_user 事件推送失败: {ask_emit_error}")
                    # 本轮任务到此暂停：占位工具结果已落盘、消息契约完整，
                    # 置 run_task=False 后循环正常退出（done 标记 + 收尾压缩）
                    session_chat_memory.run_task = False
                if oversized_abort:
                    break
                # read_media 数据注入：按坐标现场加载媒体 base64 部件，作为 user
                # 消息追加进 messages（仅内存，不落盘历史——media:// 引用已随用户
                # 消息落盘，这里只为让模型看到数据）。占位计费口径已覆盖 user 多模
                # 态部件。加载失败的坐标以占位文本告知模型，避免静默丢失。
                if read_media_pending_parts and not oversized_abort:
                    media_parts = []
                    for reference, quality in read_media_pending_parts:
                        info = load_any_media_model_part(session_id, reference, quality=quality)
                        if info is not None and info.get("part") is not None:
                            media_parts.append(info["part"])
                        elif isinstance(info, dict) and info.get("error"):
                            # 加载器带错误说明（如"未找到，可能已删除"）：原样告知模型
                            media_parts.append({
                                "type": "text",
                                "text": f"[媒体 {reference} 读取失败：{info['error']}]",
                            })
                        else:
                            media_parts.append({
                                "type": "text",
                                "text": f"[媒体 {reference} 未找到，可能已删除或读取失败]",
                            })
                    media_message: dict[str, Any] = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "以下是你刚才调用 read_media 读取的媒体内容"
                                    "（按引用顺序排列）："
                                ),
                            },
                            *media_parts,
                        ],
                        "_internal": True,
                    }
                    messages.append(media_message)
                    read_media_pending_parts = []
                # 任务内上下文预算管理：全量上下文达到单轮阈值（窗口×比例）时，
                # 压缩持久层历史（老轮次→摘要块+最近问题）并重建内存历史部分。
                # 此前历史只在请求开始/收尾时压缩，长任务中会一路膨胀到窗口上限。
                try:
                    rebuilt_messages = await _compact_task_context_if_needed(
                        messages,
                        tool_request,
                        session_chat_memory,
                        stream,
                        threshold=context_compaction_threshold,
                        backend_history_rounds=backend_history_rounds,
                        runtime_sys_text=runtime_sys_text,
                        file_block_text=active_file_block,
                        event_emitter=lambda payload: _emit_compaction_event(stream, session_chat_memory, payload),
                    )
                except asyncio.CancelledError:
                    raise
                except ContextCompactionError as exc:
                    # 自动压缩失败不再终止任务：此时上下文通常仍在模型窗口内，
                    # 原样继续生成（超大结果拒绝仍在保护）；下一批工具结果写入后
                    # 会再次尝试压缩。连续失败达到上限后本任务停止尝试，避免
                    # 每批都白烧一次失败的模型调用。
                    auto_compact_failures += 1
                    detail = f"任务内历史压缩失败（连续第 {auto_compact_failures} 次）：{exc}"
                    print(f"[WARN] {detail}")
                    if (
                        auto_compact_failures >= _AUTO_COMPACTION_MAX_CONSECUTIVE_FAILURES
                        and not auto_compact_disabled_notified
                    ):
                        auto_compact_disabled_notified = True
                        notify = (
                            f"自动压缩连续 {auto_compact_failures} 次失败，本次任务将不再自动压缩"
                            "（对话继续，上下文按原样回传）；请检查压缩模型的 API Key/网络，"
                            "或稍后使用手动压缩。"
                        )
                        await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'CONTEXT_COMPACTION_DISABLED', 'message': notify}}, ensure_ascii=False)}\n\n")
                    else:
                        await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'CONTEXT_COMPACTION_FAILED', 'message': f'自动压缩失败，已跳过并继续对话：{exc}'}}, ensure_ascii=False)}\n\n")
                    rebuilt_messages = None
                except Exception as exc:
                    print(f"[WARN] 任务内历史压缩跳过：{exc}")
                    rebuilt_messages = None
                if rebuilt_messages is not None:
                    messages = rebuilt_messages
                    auto_compact_failures = 0
                try:
                    current_tool_result_count = await session_chat_memory.get_current_round_tool_result_count()
                    round_compaction = await compact_active_round_context_if_needed(
                        messages,
                        tool_request,
                        compression_index=current_tool_result_count,
                        event_emitter=lambda payload: _emit_compaction_event(stream, session_chat_memory, payload),
                    )
                except asyncio.CancelledError:
                    raise
                except ContextCompactionError as exc:
                    # 单轮压缩失败同样不终止任务：本轮工具轨迹按原样保留，
                    # 由超大结果拒绝与窗口硬限制兜底。
                    print(f"[WARN] 单轮上下文压缩失败（本轮轨迹原样保留）：{exc}")
                    await _stream_emit(stream, f"data: {json.dumps({'warning': {'code': 'ROUND_COMPACTION_FAILED', 'message': f'本轮工具轨迹压缩失败，已按原样继续：{exc}'}}, ensure_ascii=False)}\n\n")
                    round_compaction = None
                except Exception as exc:
                    print(f"[WARN] 单轮工具上下文压缩跳过：{exc}")
                    round_compaction = None
                if round_compaction is not None and round_compaction.triggered:
                    messages = round_compaction.messages
                        # 单轮压缩的 start/done 已由 event_emitter 按实际发生顺序
                        # 写入当前 chat_round.events，不再重复保存顶层摘要状态。
        # 写入结束消息到文件
        await session_chat_memory.add_chat_history({"role": "assistant", "done": "[DONE]"})
        try:
            final_compaction_settings = load_context_compaction_settings()
            await compact_session_history_if_needed(
                session_chat_memory,
                tool_request,
                settings=final_compaction_settings,
                event_emitter=lambda payload: _emit_compaction_event(stream, session_chat_memory, payload),
            )
        except ContextCompactionError as exc:
            detail = f"收尾历史压缩失败（聊天模型重试仍不可用）：{exc}"
            print(f"[ERROR] {detail}")
            await _stream_emit(stream, _sse_error_payload(detail))
        except Exception as exc:
            print(f"[WARN] 结束后的历史压缩跳过：{exc}")
    except asyncio.CancelledError:
        # 服务端关停/新消息打断/用户手动停止取消：尽力把已生成内容落盘。
        # 手动停止（stream.user_stop_requested）时以 stopped 状态收尾轮次，
        # 与生成流中的优雅停止路径同构；其余取消保持 interrupted 语义
        # （error 事件落盘，进程重启后同样可恢复）。
        try:
            if getattr(stream, "user_stop_requested", False):
                await session_chat_memory.stop_current_round()
            else:
                await session_chat_memory.add_chat_history({
                    "role": "assistant",
                    "error": "生成任务已取消（新消息打断或服务停止）",
                })
        except Exception:
            pass
        raise
    except Exception as ce:
        # 错误同时写历史与推 SSE：只落盘不推送会让前端在无事件的长连接上无限等待
        try:
            await session_chat_memory.add_chat_history({"role": "assistant", "error": str(ce)})
            await _stream_emit(stream, _sse_error_payload(f"生成任务异常终止: {ce}"))
        except Exception:
            pass
        raise ce
    finally:
        # 清理工具管理内存（使用 session_id 隔离）
        try:
            session_chat_memory.run_task = False
        except Exception:
            pass
        await cleanup_chat_memory_manager(session_id)
        await cleanup_file_memory_manager(session_id)
        await stream.finish()


# 生成任务执行模式：process（默认，每会话独立 worker 进程 + os.chdir 会话目录）
# 或 inline（生成循环留在主进程，全局 cwd 语义，供测试与故障兜底）。
# 懒加载 .env 的 CHAT_WORKER_MODE；测试可直接补丁本变量。
_CHAT_WORKER_MODE: str | None = None


def _generation_use_worker() -> bool:
    global _CHAT_WORKER_MODE
    mode = (_CHAT_WORKER_MODE or "").strip().casefold()
    if not mode:
        try:
            mode = str(load_var("CHAT_WORKER_MODE", "process") or "process").strip().casefold()
        except Exception:
            mode = "process"
        _CHAT_WORKER_MODE = mode
    return mode not in {"inline", "main", "off", "false", "0", "no"}


async def _wait_worker_idle(proxy, timeout: float = 15.0) -> None:
    """等待会话 worker 完全退出当前生成任务；超时强制终止兜底。

    JSONL 写入是「临时文件 + 原子替换」，强制终止不会损坏已落盘数据；
    未收尾的当前轮由 worker 重启后的检查点恢复机制标记 interrupted。
    """
    deadline = time.monotonic() + timeout
    while proxy.is_generation_running():
        if time.monotonic() >= deadline:
            proxy.terminate("等待旧生成任务退出超时")
            return
        await asyncio.sleep(0.05)


async def _wait_worker_generation(proxy, stream) -> None:
    """worker 模式下占位 stream.task 的等待任务：worker 报告完成后退出。

    保留 stream.task 的既有语义（可 cancel、done 表示生成结束）。
    worker 进程 spawn + 初始化有秒级延迟，必须先等到 task_started
    （is_generation_running 翻转）再判断结束，否则会把"尚未开始"误判成
    "已完成"，导致流内事件全部丢失。等待 started 不设固定时限：进程
    存活就继续等（重型 MCP 冷启动探测 + 模型长首字延迟可能超过任意
    固定值，误判会直接终止仍在生成的任务）；进程死亡时 reader 线程
    会合成 task_done 收尾，短暂等待其兜底后强制 finish，避免流挂死。
    """
    while not proxy.is_generation_running():
        if stream.done:
            return
        if not proxy.is_alive():
            # 进程已退出：正常情况 reader 已合成 task_done 收尾；短暂等待
            # 兜底后仍未收尾则强制 finish（与旧 60s 超时语义等价但更精准）
            for _ in range(50):
                if stream.done:
                    return
                await asyncio.sleep(0.1)
            break
        await asyncio.sleep(0.05)
    while not stream.done and proxy.is_generation_running():
        await asyncio.sleep(0.1)
    if not stream.done:
        await stream.finish()


async def _start_session_generation(proxy, stream, tool_request: ChatLLMRequest) -> bool:
    """启动一次生成任务。

    worker 模式：把请求序列化派发给会话 worker（懒启动进程），并把等待任务
    挂到 stream.task；inline 模式：保持旧行为，在主进程内起 asyncio 任务。
    返回 False 表示启动失败（已向流内写入 error 事件）。
    """
    # 会话配置快照：首次正式开始任务时把当前全局默认（模型/工具/工作目录）
    # 固化为会话独立配置，此后该会话不再跟随全局设置变化（幂等，快照过即跳过）。
    try:
        session_chat_memory = await get_chat_memory_manager(
            normalize_session_id(getattr(tool_request, "session_id", "default"))
        )
        await session_chat_memory.ensure_session_config_snapshot()
    except Exception as snapshot_error:
        print(f"[WARN] 会话配置快照失败（不影响本轮对话）: {snapshot_error}")
    if proxy is not None:
        proxy.bind_stream(stream)
        started = proxy.start_generation(tool_request.model_dump())
        if not started:
            await _stream_emit(
                stream,
                _sse_error_payload("会话 worker 进程启动失败，请查看服务端日志"),
            )
            await stream.finish()
            return False
        stream.task = asyncio.create_task(_wait_worker_generation(proxy, stream))
        return True
    stream.task = asyncio.create_task(_run_chat_generation(tool_request, stream))
    return True


# 聊天 SSE 心跳间隔（秒）：生成任务长时间无增量输出（模型长思考/慢工具/
# 上游网关缓冲）时连接会静默，nginx 等反向代理默认 60s 无数据即断连，
# 浏览器/中间层也可能把静默连接判死——用户端表现为"任务被错误终止"。
# 每 _SSE_HEARTBEAT_SECONDS 秒发一帧 SSE 注释（": ping"）保活链路；
# 前端 readSseResponse 只解析 "data:" 行，注释帧会被自动忽略。
_SSE_HEARTBEAT_SECONDS = 15.0


async def tool_chat_server(
    tool_request: ChatLLMRequest
) -> AsyncGenerator[str, None]:
    """聊天流式入口：后台生成任务 + 可重连消费。

    - 首个请求启动后台生成任务（默认派发到会话独立 worker 进程，
      CHAT_WORKER_MODE=inline 时回退主进程 asyncio 任务）；
    - 页面刷新后的重连请求（无用户消息）只订阅事件缓冲，从任务起点回放；
    - 携带新用户消息的请求会先平滑打断旧任务，再重新开始一轮生成。
    """
    session_id = getattr(tool_request, 'session_id', 'default')
    proxy = get_worker_proxy(session_id) if _generation_use_worker() else None
    if proxy is not None:
        proxy.set_loop(asyncio.get_running_loop())
        sweep_dead_worker_proxies()
    stream = _get_session_stream(session_id)
    creating = False
    if proxy is not None:
        # worker 模式的"活跃"判断：worker 命令循环按顺序处理命令，同一会话的
        # 请求天然串行（pipelined），start_generation 只是把 generate 命令排入
        # 队列——首次请求发出后到 task_started 事件回流前的窗口内
        # is_generation_running 仍是 False，此时绝不能把任务判为不活跃。
        # （并发重连 + 发送竞争时误判会让同一消息被派发两次，轮次里出现
        # 两条相同 user 事件。）
        task_active = (
            proxy.is_generation_running()
            or stream is not None
            and stream.task is not None
            and not stream.task.done()
        )
    else:
        task_active = stream is not None and stream.task is not None and not stream.task.done()
    if stream is None or stream.done or not task_active:
        stream = _set_session_stream(session_id)
        if not await _start_session_generation(proxy, stream, tool_request):
            return
        creating = True
    elif _request_has_new_user_message(tool_request):
        # 已存在生成任务且本次请求携带新用户消息：显式打断旧任务并等待其
        # 完全退出后再启动新任务。旧任务的收尾处理会以 error/interrupted
        # 事件收尾当前轮次并落盘已积累的工具调用/思考，避免新旧两个生成
        # 循环交错写入同一 pending 轮次（仅靠 run_task 标记存在竞态窗口：
        # 新任务会把标记置回 True，旧循环可能继续运行）。
        # 新消息打断不是手动停止：先清掉可能残留的 user_stop_requested，
        # 保证旧任务按 interrupted 语义落盘。
        stream.user_stop_requested = False
        if proxy is not None:
            proxy.request_stop(reason="interrupt")
            await _wait_worker_idle(proxy)
        else:
            old_task = stream.task
            try:
                (await get_chat_memory_manager(session_id)).run_task = False
            except Exception:
                pass
            if old_task is not None:
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass
                except Exception as old_error:
                    print(f"[WARN] 旧生成任务退出异常（已忽略）: {old_error}")
        stream = _set_session_stream(session_id)
        if not await _start_session_generation(proxy, stream, tool_request):
            return
        creating = True
    try:
        if stream.done:
            seen = stream.seq  # 已完成的生成：不重复回放（内容已在 JSONL）
        else:
            seen = stream.round_start_seq
            if not creating:
                # 附接：先发回放标记，前端据此补建“当前轮提问”气泡后再接事件
                marker = {"replay": True, "question_text": stream.question_text or ""}
                yield f"data: {json.dumps(marker, ensure_ascii=False)}\n\n"
        while True:
            while seen < stream.seq:
                base = stream.seq - len(stream.buffer)
                if seen < base:
                    seen = base  # 环形缓冲已滚动：跳过过期片段
                chunk = stream.buffer[seen - base]
                seen += 1
                yield chunk
            if stream.done:
                break
            # 心跳：模型长时间无增量输出时（长思考/慢工具/网关缓冲等），
            # SSE 连接会静默——nginx 等代理默认 60s 无数据即断连、浏览器
            # 也可能把静默连接判死，用户端表现为"任务被错误终止"。与手动
            # 压缩流一致，每 SSE_HEARTBEAT_SECONDS 发一帧 SSE 注释
            # （": ping"），前端按规范自动忽略注释行（readSseResponse 只
            # 解析 "data:" 行）。后台生成任务不受消费端影响，ping 仅用于
            # 保活链路与提示"任务仍在进行"。
            ping_due = False
            async with stream.cond:
                if seen >= stream.seq and not stream.done:
                    try:
                        await asyncio.wait_for(
                            stream.cond.wait(), timeout=_SSE_HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
            if seen >= stream.seq and not stream.done:
                ping_due = True
            if ping_due:
                yield ": ping\n\n"
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        # 仅消费端被取消（页面刷新/关闭）：后台生成任务不受影响
        raise
    finally:
        if stream.task is not None and stream.task.done() and not stream.done:
            await stream.finish()


async def inject_user_message(session_id: str, message: dict) -> dict:
    """运行中注入用户消息（消息引导）的主进程入口。

    worker 模式：向会话 worker 发送 inject 命令，生成循环在下一轮检查点
    （工具结果处理完毕后）取出并作为新一轮用户消息继续；
    inline 模式：直接写入 _SessionStream 的注入队列。
    返回 {"ok": bool, "reason": str}；任务未运行时 ok=False（前端回退普通发送）。
    """
    session_id = normalize_session_id(session_id)
    if _generation_use_worker():
        proxy = peek_worker_proxy(session_id)
        if proxy is None or not proxy.inject_message(message or {}):
            return {"ok": False, "reason": "not_running"}
        return {"ok": True}
    stream = _get_session_stream(session_id)
    if stream is None or stream.done or not stream.is_running():
        return {"ok": False, "reason": "not_running"}
    stream.injected_messages.append(message or {})
    return {"ok": True}


async def cancel_injected_message(session_id: str, text: str) -> dict:
    """撤回一条尚未消费的运行中注入消息（消息引导提示行的 × 按钮）。

    worker 模式：向会话 worker 发送 cancel_inject 命令，按文本匹配移除
    注入队列中未消费的一条；inline 模式：直接从 _SessionStream 注入队列
    移除。消息已被生成循环取走（已消费）时返回 ok=False，前端提示不可撤回。
    """
    session_id = normalize_session_id(session_id)
    target = (text or "").strip()
    if not target:
        return {"ok": False, "reason": "empty"}
    if _generation_use_worker():
        proxy = peek_worker_proxy(session_id)
        if proxy is None or not proxy.cancel_injected_message(target):
            return {"ok": False, "reason": "not_running"}
        return {"ok": True}
    stream = _get_session_stream(session_id)
    if stream is None:
        return {"ok": False, "reason": "not_running"}
    removed = False
    remaining: deque = deque()
    while stream.injected_messages:
        item = stream.injected_messages.popleft()
        item_text = ""
        if isinstance(item, dict):
            item_text = content_part_to_text(item.get("content")).strip()
        if not removed and item_text == target:
            removed = True
            continue
        remaining.append(item)
    stream.injected_messages.extend(remaining)
    return {"ok": removed, "reason": "" if removed else "not_found"}


async def stop_chat_task(session_id):
    ''' 手动停止当前会话的聊天任务。

    worker 模式（默认）：向会话 worker 发送 stop 命令，worker 内置
    run_task=False 并取消生成任务，事件经 IPC 回流收尾；随后确认 worker
    已完全退出当前任务（超时则强制终止兜底）。
    inline 模式保持原两步停止：
    1. 置 run_task=False：生成循环在下一次检查点（流式事件/工具结果处理）主动退出；
    2. 取消后台生成任务（asyncio.Task.cancel）：工具执行已挪到工作线程，事件循环
       不再被阻塞，即使当前正卡在工具调用上，取消也能在 await 点生效并触发
       CancelledError 收尾（user_stop_requested 标记使其按"手动停止"落盘）。
    取消只作用于生成任务本身，不影响 SSE 消费端连接。
    '''
    session_id = normalize_session_id(session_id)
    if _generation_use_worker():
        proxy = peek_worker_proxy(session_id)
        stream = _get_session_stream(session_id)
        if stream is not None:
            stream.user_stop_requested = True
        if proxy is not None:
            proxy.request_stop(reason="user")
            await _wait_worker_idle(proxy, timeout=15.0)
        return
    try:
        (await get_chat_memory_manager(session_id)).run_task = False
    except Exception:
        pass
    stream = _get_session_stream(session_id)
    if stream is None:
        return
    stream.user_stop_requested = True
    task = stream.task
    if task is not None and not task.done():
        task.cancel()
        # 不要在这里立即返回：调用方需要确认后台任务已经退出，避免停止后
        # 仍有 finally/历史写入在后台运行，重载或启动下一轮时与旧任务交错。
        # 工具执行在工作线程中时，cancel 会先让生成协程退出；工具线程本身
        # 由其超时配置收尾，不再阻塞本请求的事件循环。
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[WARN] 停止会话任务时忽略后台异常: {exc}")
