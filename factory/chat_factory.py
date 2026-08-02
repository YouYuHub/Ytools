# 标准库导入
# import re
import asyncio
import json
import uuid
from functools import partial
# import inspect
from typing import Any, Dict, List, AsyncGenerator

# 自定义模块导入
from env_manager import ChatModelConfigurationError, load_var, require_default_chat_config
from chat.chat_llm import ChatLLM
from config import ChatLLMRequest, get_current_dir
from memory.chat_memory import get_chat_memory_manager, cleanup_chat_memory_manager
from memory.file_memory import get_file_memory_manager, cleanup_file_memory_manager
from factory.tool_executor import normalize_tool_calls, prepare_tool_execution, execute_tool_round
from factory.chat_runtime import (
    parse_bool_like,
    parse_int_like,
    frontend_provides_full_history,
    build_request_messages,
    UsageAccumulator,
    estimate_messages_tokens,
    estimate_text_tokens,
    resolve_model_max_input_tokens,
)
from factory import tool_registry


BUILTIN_CHECK_TOOL_EXISTS_NAME = "check_tool_exists"
BUILTIN_CHECK_TOOL_EXISTS_DEF = {
    "type": "function",
    "function": {
        "name": BUILTIN_CHECK_TOOL_EXISTS_NAME,
        "description": "检查指定工具是否在后端实时可用工具中存在",
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "要检查的工具名称"
                }
            },
            "required": ["tool_name"]
        }
    }
}


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


# "注意：目前工具函数仅支持单个使用，不支持并发批量使用；所以请一次最多使用一个工具。"
# 当前工作路径为<{_format_tool_result(get_current_dir())}>
SYS_PROMPT = """
    本轮工具集合由用户选择并由系统注入，你只能调用当前可见工具（如果用户提供了）；禁止臆造、改名或扩展工具。
    程序支持并发工具调用，但并发结果完成顺序不保证；工具返回 [] 表示空值而不是失败。
    历史记录只保留工具调用参数，不保留工具结果全文。
    如果任务已完成，直接给最终答复；如果无需调用工具即可完成，也可直接结束本轮。
    注意：本轮你的思考过程（reasoning_content）只会保留最后 1024 字节的内容，请确保关键决策信息在最后 1k 内。
"""
# 如果你不确定某个工具名是否存在，可调用 check_tool_exists 逐个检查；不要通过遍历猜测全部工具。
# "不管是否调用工具，当你最终回答用户时都需要调用 over_task，因为你只能通过该方式结束当前任务或会话，否则消息可能会越来越长"


async def load_all_tools() -> None:
    """加载并过滤工具，同时构建 MCP 映射。"""
    await tool_registry.refresh_tools_from_mcp(get_current_dir())
    print(f"✅ 已加载 {len(tool_registry.ALL_TOOLS)} 个工具，构建 {len(tool_registry.TOOL_MCP_SERVERS)} 个MCP映射")
print("[INFO] 工具将按需实时刷新（来源: setting/mcp_servers.json）")


def _parse_sse_event(sse_chunk: str) -> dict[str, Any] | None:
    if not isinstance(sse_chunk, str):
        return None
    chunk = sse_chunk.strip()
    if not chunk.startswith("data:"):
        return None
    payload_text = chunk[len("data:"):].strip()
    if payload_text == "[DONE]":
        return {"done": True}
    if not payload_text:
        return None
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        return None


def _filter_tool_calls_fields(event: dict[str, Any]) -> dict[str, Any]:
    """过滤 tool_calls 中的 id 和 type 字段
    只处理 tool_calls 字段, 其他字段直接引用原对象以避免不必要的拷贝
    """
    if not isinstance(event, dict):
        return event
    # 如果没有 tool_calls 字段,直接返回原对象
    if "tool_calls" not in event:
        return event
    tool_calls = event.get("tool_calls")
    if not isinstance(tool_calls, list):
        return event
    # 只处理 tool_calls 字段,其他字段保持原样
    filtered_event = event.copy()
    filtered_tool_calls = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            # 创建新的工具调用对象,排除 id 和 type 字段
            filtered_tc = {k: v for k, v in tc.items() if k not in ("id", "type")}
            filtered_tool_calls.append(filtered_tc)
        else:
            filtered_tool_calls.append(tc)
    filtered_event["tool_calls"] = filtered_tool_calls
    return filtered_event


def _merge_function_call_delta(
    accumulated: List[dict],
    function_call_delta: dict
) -> None:
    """合并流式 function_call 增量数据到累积列表中（兼容老模型）
    规则：
    - function_call 是单个对象，不是列表
    - 累积 name 和 arguments 字符串
    - 将 function_call 转换为与 tool_calls 相同的格式，index 固定为 0
    """
    if not isinstance(function_call_delta, dict):
        return
    # 查找是否已存在 function_call 的记录（index=0）
    existing_fc = None
    for acc_tc in accumulated:
        if acc_tc.get("index") == 0 and acc_tc.get("_is_function_call", False):
            existing_fc = acc_tc
            break
    # 如果不存在，创建新记录
    if existing_fc is None:
        existing_fc = {
            "index": 0,
            "_is_function_call": True,  # 标记这是 function_call 而非 tool_calls
            "function": {
                "name": "",
                "arguments": ""
            }
        }
        accumulated.append(existing_fc)
    # 累加 function.name
    fc_name = function_call_delta.get("name")
    if fc_name:
        existing_fc["function"]["name"] += fc_name
    # 累加 function.arguments
    fc_arguments = function_call_delta.get("arguments")
    if fc_arguments:
        existing_fc["function"]["arguments"] += fc_arguments


def _merge_tool_call_delta(
    accumulated: List[dict],
    tool_calls_delta: list[dict]
) -> None:
    """合并流式工具调用增量数据到累积列表中
    规则：
    - accumulated 包含所有出现过的 index 对应的工具调用
    - 如果 delta 的 index 已存在，则累加该 index 的内容（name, arguments 等）
    - 如果 delta 的 index 不存在，则新增一条记录
    - index 本身是标识符，直接替换而非累加
    """
    if not isinstance(tool_calls_delta, list):
        return
    for delta in tool_calls_delta:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index")
        if index is None:
            continue
        # 查找是否已存在该 index 的记录
        existing_tc = None
        for acc_tc in accumulated:
            if acc_tc.get("index") == index:
                existing_tc = acc_tc
                break
        # 如果不存在，创建新记录
        if existing_tc is None:
            existing_tc = {
                "index": index,
                "id": delta.get("id") or f"call_{uuid.uuid4().hex}",
                "function": {
                    "name": "",
                    "arguments": ""
                },
                "type": "function"
            }
            accumulated.append(existing_tc)
        delta_function = delta.get("function", {})
        if delta.get("id"):
            existing_tc["id"] = delta["id"]
        if delta_function.get("name"):
            existing_tc["function"]["name"] += delta_function["name"]
        if delta_function.get("arguments"):
            existing_tc["function"]["arguments"] += delta_function["arguments"]
        if delta.get("type"):
            existing_tc["type"] = delta["type"]
    # 遵守 ai 给的 index 字段排序
    accumulated.sort(key=lambda x: x["index"])


def _sse_error_payload(message: str) -> str:
    return f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n"


def _serialize_tool_definition(tool_def: Any) -> dict:
    if hasattr(tool_def, "model_dump"):
        return tool_def.model_dump(exclude_none=True)
    if hasattr(tool_def, "dict"):
        return tool_def.dict(exclude_none=True)
    if isinstance(tool_def, dict):
        return tool_def
    return {}


def _tool_name_from_definition(tool_def: dict) -> str:
    if not isinstance(tool_def, dict):
        return ""
    func_info = tool_def.get("function")
    if not isinstance(func_info, dict):
        return ""
    name = func_info.get("name")
    return name if isinstance(name, str) else ""


# 历史摘要与轮次格式化已迁移到 memory.chat_history_format
from memory.chat_history_format import (
    format_tool_call_history_line as _format_tool_call_history_line,
    render_context_summary as _render_summary_state,
    round_entry_to_context_messages as _round_entry_to_context_messages,
)


# 摘要渲染已迁移到 memory.chat_history_format._render_summary_state
# 保留下面这段兼容占位，避免外部引用旧名字时漏改


def _estimate_round_entry_tokens(round_entry: dict[str, Any]) -> int:
    return estimate_messages_tokens(_round_entry_to_context_messages(round_entry))


def _round_entry_to_summary_text(round_entry: dict[str, Any]) -> str:
    lines: list[str] = []
    question = round_entry.get("question")
    if isinstance(question, str) and question.strip():
        lines.append(f"用户问题: {question.strip()}")
    for message in _round_entry_to_context_messages(round_entry):
        role = message.get("role", "")
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()}")
    return "\n".join(lines)


async def _summarize_round_chunk(
    tool_request: ChatLLMRequest,
    previous_summary_state: dict[str, Any] | None,
    chunk_rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_summary_text = _render_summary_state(previous_summary_state)
    chunk_payload = [
        {
            "question": round_entry.get("question", ""),
            "text": _round_entry_to_summary_text(round_entry),
        }
        for round_entry in chunk_rounds
        if isinstance(round_entry, dict)
    ]
    prompt_payload = {
        "previous_summary": previous_summary_text or "",
        "rounds": chunk_payload,
    }
    summary_prompt = (
        "你是会话压缩器。请把给定的旧对话压缩成后续模型继续可用的上下文摘要。"
        "要求：保留用户目标、已确认事实、工具结果、数值、约束条件、未完成事项；"
        "删除寒暄和重复细节；如果有工具状态或待办，请显式列出。"
        "只输出严格 JSON，不要输出多余文本。JSON 结构为："
        "{\"summary\": string, \"key_facts\": [string], \"open_items\": [string], \"tool_state\": [string]}"
    )
    summary_request = ChatLLMRequest(
        messages=[
            {"role": "system", "content": summary_prompt},
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False, indent=2)},
        ],
        max_tokens=max(256, min(1024, int(tool_request.max_tokens or 1024) // 4 or 256)),
        temperature=0.2,
        top_p=1.0,
        presence_penalty=0.0,
        stream=False,
        reasoning_effort="low",
        tool_choice="none",
        parallel_tool_calls=False,
        session_id=tool_request.session_id,
        use_backend_history=False,
        backend_history_rounds=0,
    )

    result = await asyncio.to_thread(partial(ChatLLM.chat_completions, request=summary_request, stream=False))
    summary_text = ""
    if isinstance(result, dict):
        summary_text = str(result.get("content") or result.get("reasoning_content") or "").strip()

    parsed_summary: dict[str, Any] | None = None
    if summary_text:
        try:
            candidate = json.loads(summary_text)
            if isinstance(candidate, dict):
                parsed_summary = candidate
        except Exception:
            parsed_summary = None

    if parsed_summary is None:
        parsed_summary = {
            "summary": summary_text or "旧对话压缩失败，保留简化摘要。",
            "key_facts": [],
            "open_items": [],
            "tool_state": [],
        }

    parsed_summary.setdefault("summary", summary_text or "旧对话压缩失败，保留简化摘要。")
    parsed_summary.setdefault("key_facts", [])
    parsed_summary.setdefault("open_items", [])
    parsed_summary.setdefault("tool_state", [])
    parsed_summary["source_round_count"] = len(chunk_rounds)
    return parsed_summary


async def _compact_session_history_if_needed(
    session_chat_memory,
    tool_request: ChatLLMRequest,
) -> None:
    context_limit = resolve_model_max_input_tokens(default=8192)
    trigger_ratio = float(load_var("HISTORY_COMPACT_TRIGGER_RATIO", "0.8") or 0.8)
    if trigger_ratio <= 0:
        trigger_ratio = 0.8
    if trigger_ratio > 0.95:
        trigger_ratio = 0.95
    budget_limit = max(1024, int(context_limit * trigger_ratio))
    keep_min_rounds = max(1, int(load_var("HISTORY_COMPACT_KEEP_ROUNDS", "4") or 4))
    chunk_rounds_target = max(1, int(load_var("HISTORY_COMPACT_CHUNK_ROUNDS", "3") or 3))

    summary_state = await session_chat_memory.get_context_summary()
    history_entries = await session_chat_memory.get_chat_history()
    round_entries = [
        entry for entry in history_entries
        if isinstance(entry, dict) and entry.get("event") == "chat_round"
    ]
    summarized_count = 0
    if isinstance(summary_state, dict):
        try:
            summarized_count = max(0, int(summary_state.get("source_round_count", 0) or 0))
        except (TypeError, ValueError):
            summarized_count = 0
    summarized_count = min(summarized_count, len(round_entries))
    raw_rounds = round_entries[summarized_count:]
    if len(raw_rounds) <= keep_min_rounds:
        return

    while True:
        summary_message = _render_summary_state(summary_state)
        current_messages: list[dict[str, Any]] = []
        if summary_message:
            current_messages.append({"role": "system", "content": summary_message})
        for round_entry in raw_rounds:
            current_messages.extend(_round_entry_to_context_messages(round_entry))

        if estimate_messages_tokens(current_messages) <= budget_limit:
            return

        if len(raw_rounds) <= keep_min_rounds:
            return

        tail_rounds: list[dict[str, Any]] = []
        tail_tokens = estimate_messages_tokens(current_messages[:1]) if summary_message else 0
        for round_entry in reversed(raw_rounds):
            round_tokens = _estimate_round_entry_tokens(round_entry)
            if tail_rounds and tail_tokens + round_tokens > budget_limit:
                break
            tail_rounds.append(round_entry)
            tail_tokens += round_tokens
        tail_rounds.reverse()

        if not tail_rounds or len(tail_rounds) >= len(raw_rounds):
            return

        summarize_count = len(raw_rounds) - len(tail_rounds)
        summarize_count = max(1, min(summarize_count, chunk_rounds_target))
        summarize_count = min(summarize_count, len(raw_rounds) - keep_min_rounds)
        if summarize_count <= 0:
            return

        chunk_rounds = raw_rounds[:summarize_count]
        summary_state = await _summarize_round_chunk(tool_request, summary_state, chunk_rounds)
        await session_chat_memory.update_context_summary(summary_state)
        raw_rounds = raw_rounds[summarize_count:]


async def tool_chat_server(
    tool_request: ChatLLMRequest
) -> AsyncGenerator[str, None]:
    """工具聊天服务器，按需补齐工具并以 SSE 格式输出模型响应。"""
    try:
        require_default_chat_config()
    except ChatModelConfigurationError as exc:
        yield _sse_error_payload(str(exc))
        return

    # 从请求中获取 session_id
    session_id = getattr(tool_request, 'session_id', 'default')
    await load_all_tools()
    # 计算本轮允许使用的工具：后端实时工具为唯一真相；前端仅允许传 tool_names
    configured_tools = list(tool_registry.ALL_TOOLS)
    configured_tool_servers = dict(tool_registry.TOOL_MCP_SERVERS)
    configured_tool_by_name = {
        _tool_name_from_definition(tool_def): tool_def
        for tool_def in configured_tools
        if _tool_name_from_definition(tool_def)
    }
    requested_tool_names = getattr(tool_request, "tool_names", None)

    if isinstance(requested_tool_names, list):
        requested_names = [str(name).strip() for name in requested_tool_names if str(name).strip()]

        if not requested_names:
            warning_event = {
                "warning": {
                    "code": "NO_TOOL_SELECTED",
                    "message": "本轮未选择任何工具，已按无工具模式继续",
                }
            }
            yield f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n"
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
                if tool_name == BUILTIN_CHECK_TOOL_EXISTS_NAME:
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
                yield f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n"

            round_tools = selected_tools
            round_tool_servers = selected_servers
    else:
        warning_event = {
            "warning": {
                "code": "NO_TOOL_SELECTED",
                "message": "缺少 tool_names 字段，已按无工具模式继续",
            }
        }
        yield f"data: {json.dumps(warning_event, ensure_ascii=False)}\n\n"
        round_tools = []
        round_tool_servers = {}

    # 当且仅当前端有工具选择时，注入内置工具
    if round_tools:
        if BUILTIN_CHECK_TOOL_EXISTS_NAME not in round_tool_servers:
            round_tools = round_tools + [BUILTIN_CHECK_TOOL_EXISTS_DEF]
            round_tool_servers[BUILTIN_CHECK_TOOL_EXISTS_NAME] = "__builtin__"

    tool_request.tools = round_tools
    # print(f"tools: {tool_request.tools}")
    tool_choice = tool_request.tool_choice
    if not isinstance(tool_choice, (str, dict)):
        tool_choice = "auto"
    # extra_body = dict(tool_request.extra_body) if tool_request.extra_body else {}
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
    try:
        use_backend_history = parse_bool_like(
            getattr(tool_request, "use_backend_history", None),
            parse_bool_like(load_var("USE_BACKEND_HISTORY", "true"), True)
        )
        backend_history_rounds = parse_int_like(
            getattr(tool_request, "backend_history_rounds", None),
            parse_int_like(load_var("BACKEND_HISTORY_ROUNDS", 6), 6)
        )

        if use_backend_history:
            if frontend_provides_full_history(incoming_messages):
                print("[INFO] 检测到前端已提供完整历史，本次跳过后端历史拼接")
            else:
                backend_messages = await session_chat_memory.get_context_messages(
                    max_rounds=backend_history_rounds
                )
                if backend_messages:
                    messages = backend_messages + messages
                    print(f"[INFO] 已拼接后端历史消息 {len(backend_messages)} 条（最近 {backend_history_rounds} 轮）")

        latest_user_message = None
        for msg in reversed(incoming_messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    latest_user_message = msg
                break
        if latest_user_message:
            await session_chat_memory.add_chat_history(latest_user_message)
        # 检查是否存在系统提示词，添加一些提示
        if messages[0]["role"] == "system":
            messages[0]["content"] += (f"当前工作路径为<{_format_tool_result(get_current_dir())}>\n" + SYS_PROMPT)
        else:
            messages.insert(0, {
                "role": "system",
                "content": (
                    "你是一个聪明的助手，你需要自行判断是否需要调用工具，并结合你之前使用的工具（如果有）的结果，调用工具的结果只有你可见，请根据用户使用的语言回答用户问题。\n"
                    f"当前工作路径为<{_format_tool_result(get_current_dir())}>\n"
                    + SYS_PROMPT
                ),
            })
        # 这里做文件处理，并添加文件解析内容到 "content" 字段
        user_files = session_file_memory.get_file_memory_chat()
        if user_files:
            messages[0]["content"] += (
                f"\n用户上传的 {len(user_files)} 个文件，解析的内容如下：\n"
                f"{user_files}"
            )
        # run_task = True
        # last_tool_ret = ""  # 上一次调用工具的返回值
        # while run_task:
        while session_chat_memory.run_task:
            print(f"massages: [\n\t{',\n\t'.join(map(str, messages))}\n]")
            full_response = ""
            full_reasoning = ""
            finish_reason = None  # 追踪模型完成原因：stop（正常完成）、length（token 截断）、tool_calls（工具调用）
            pending_finish_payload: dict[str, Any] | None = None  # 延后发送 finish_reason，先发 usage 尾帧
            stream_error = None  # 追踪上游流式错误（与用户手动停止区分，避免误记为"停止任务"）
            tool_calls: List[dict] = []
            usage_accumulator = UsageAccumulator()
            # 将 messages 附加到 tool_request 中
            tool_request.messages = build_request_messages(messages)
            # 注意：ChatLLM.chat_completions(stream=True) 返回 async generator，
            # 外层 tool_chat_server 是 async def，必须用 async for 迭代，
            # 否则会抛 TypeError: 'async_generator' object is not iterable
            async for sse_chunk in ChatLLM.chat_completions(
                request = tool_request, # 传递完整的 ChatLLMRequest 对象（已包含 messages）
                stream = True,
                stop_checker = lambda: (not session_chat_memory.run_task)
            ):
                # print(f"sse_chunk: {sse_chunk}")
                event = _parse_sse_event(sse_chunk)
                if event is None:
                    yield sse_chunk
                    continue
                if event.get("done") or not session_chat_memory.run_task:
                    if pending_finish_payload is not None:
                        yield f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n"
                        pending_finish_payload = None
                    break
                if event.get("error") is not None:
                    yield sse_chunk
                    stream_error = event.get("error")
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
                    event = event.copy()
                    event.pop("finish_reason", None)
                choices = event.get("choices")
                # 部分服务会在 finish_reason 之后再发一帧 choices:[] + usage 统计帧
                # 这类帧需要优先透传 usage，然后再发送 finish_reason，避免前端提前收流
                if isinstance(choices, list) and len(choices) == 0 and event.get("usage") is not None:
                    usage_event = {"type": "usage", "usage": event["usage"]}
                    if event.get("id") is not None:
                        usage_event["id"] = event["id"]
                    yield f"data: {json.dumps(usage_event, ensure_ascii=False)}\n\n"
                    if pending_finish_payload is not None:
                        yield f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n"
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
                    print(f"[DEBUG] function_call_delta: {function_call_delta}")
                    # fc_name = function_call_delta.get("name") or ""
                    # fc_args = function_call_delta.get("arguments") or ""
                    # if fc_name or fc_args:
                    #     print(f"[STREAM] function_call += name:'{fc_name[:30]}', args_len:{len(fc_args)}", flush=True)
                # 过滤 tool_calls 中的 id 和 type 字段后再输出
                filtered_event = _filter_tool_calls_fields(event)
                yield f"data: {json.dumps(filtered_event, ensure_ascii=False)}\n\n"

            if usage_accumulator.count > 0:
                round_usage_total: dict[str, Any] = {}
                usage_accumulator.merge_to(round_usage_total, _merge_usage_values)
                await session_chat_memory.update_current_round_usage(
                    usage_total=round_usage_total,
                    completion_count=usage_accumulator.count,
                )

            # print(f"[DEBUG] tool_calls: {tool_calls}")
            if pending_finish_payload is not None:
                yield f"data: {json.dumps(pending_finish_payload, ensure_ascii=False)}\n\n"
                pending_finish_payload = None
            # 上游模型流出错（如连接被切断）：记录真实错误原因，而不是伪装成"用户停止任务"
            if stream_error is not None:
                print(f"\n[ERROR] 模型流式响应出错: {stream_error}")
                # 记录 ai 已生成的内容
                if full_reasoning:
                    await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
                if full_response:
                    await session_chat_memory.add_chat_history({"role": "assistant", "content": full_response})
                # 记录真实错误，便于事后追溯
                await session_chat_memory.add_chat_history({"role": "assistant", "error": str(stream_error)})
                break  # 任务因错误结束，退出循环
            # 模型回复过程中用户手动停止任务
            if not session_chat_memory.run_task:
                print("停止任务")
                # 记录 ai 生成的内容
                if full_reasoning:
                    await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
                if full_response:
                    await session_chat_memory.add_chat_history({"role": "assistant", "content": full_response})
                # 添加用户手动停止任务消息的记录
                await session_chat_memory.add_chat_history({"role": "user", "content": "停止任务"})
                break  # 任务结束，退出循环
            known_tool_names = list(round_tool_servers.keys())
            # 归一化工具调用，修复流式拼接异常（函数名/参数被连在一起）
            tool_calls = normalize_tool_calls(tool_calls, known_tool_names)
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
                if assistant_message is not None:
                    messages.append(assistant_message)
                    await session_chat_memory.add_chat_history(assistant_message)
                session_chat_memory.run_task = False
                print("\n[INFO] 本轮对话未返回工具调用，任务结束")
                break
            # 构建包含 tool_calls 的 assistant 消息（符合 OpenAI API 规范）
            assistant_message = {"role": "assistant"}
            if full_response:
                assistant_message["content"] = full_response
            elif full_reasoning:
                assistant_message["content"] = f"...{full_reasoning[-100:]}"
            # 思考模型下，带 tool_calls 的 assistant 消息必须回传 reasoning_content，否则 DeepSeek 会报 400
            if full_reasoning:
                assistant_message["reasoning_content"] = full_reasoning[-1024:]
            formatted_tool_calls = []
            for tc in tool_calls:
                function_info = tc.get("function") or {}
                tool_name = function_info.get("name")
                # 只回传已知工具，避免将非法 tool_call 继续发给上游导致 400
                if tool_name not in round_tool_servers:
                    print(f"[WARN] 跳过未知工具调用，不回传上游: {tool_name}")
                    continue
                formatted_tc = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": function_info.get("name", ""),
                        "arguments": function_info.get("arguments", "{}")
                    }
                }
                formatted_tool_calls.append(formatted_tc)
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
                print(f"[WARN] 检测到 {len(blocked_tools)} 个未授权工具调用，已拦截")
                for _, blocked_tool_call, blocked_tool_name, blocked_tool_args in blocked_tools:
                    blocked_tool_call_id = blocked_tool_call.get("id", "") if isinstance(blocked_tool_call, dict) else ""
                    blocked_ret = f"工具 {blocked_tool_name} 未在本轮授权列表中，禁止调用"
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
                    yield f"data: {json.dumps(blocked_event, ensure_ascii=False)}\n\n"
                if not parsed_tools:
                    continue
            if not parsed_tools and not round_tool_servers:
                assistant_message = {"role": "assistant", "content": full_response} if full_response else None
                if assistant_message is not None:
                    messages.append(assistant_message)
                    await session_chat_memory.add_chat_history(assistant_message)
                session_chat_memory.run_task = False
                break
            # 如果没有任何有效工具可执行，退出
            if not parsed_tools:
                print("[WARNING] 没有可执行的有效工具，退出循环")
                break
            print(f"\n[INFO] 准备执行 {len(parsed_tools)} 个工具")
            # 内置工具与 MCP 工具分开执行
            builtin_results = []
            external_parsed_tools = []
            for idx, tc, tn, ta in parsed_tools:
                if tn == BUILTIN_CHECK_TOOL_EXISTS_NAME:
                    query_name = ""
                    if isinstance(ta, dict):
                        query_name = str(ta.get("tool_name", "")).strip()
                    exists = bool(query_name and query_name in configured_tool_by_name)
                    builtin_results.append({
                        "index": idx,
                        "tool_call": tc,
                        "tool_name": tn,
                        "tool_args": ta,
                        "result": {
                            "tool_name": query_name,
                            "exists": exists,
                            "server": configured_tool_servers.get(query_name, "") if exists else "",
                        },
                        "error": None,
                    })
                else:
                    external_parsed_tools.append((idx, tc, tn, ta))

            tool_results = []
            # 使用线程池并发执行 MCP 工具
            if external_parsed_tools:
                max_workers = min(int(load_var(
                    "ONE_TASK_MAX_WORKERS", 3)),
                    len(external_parsed_tools))  # 默认最多 3 个线程
                tool_results.extend(execute_tool_round(
                    parsed_tools=external_parsed_tools,
                    tool_mcp_servers=round_tool_servers,
                    max_workers=max_workers,
                ))
            if builtin_results:
                tool_results.extend(builtin_results)

            if tool_results:
                print(f"[INFO] 有 {len(tool_results)} 个工具执行完成", flush=True)
                # 按索引排序结果
                tool_results.sort(key=lambda x: x['index'])
                # 统一添加所有工具结果到 messages 和历史记录（使用 session_id 隔离）
                # 获取当前会话的记忆管理器
                for tool_result in tool_results:
                    tool_call = tool_result['tool_call']
                    tool_call_id = tool_call.get("id", "")
                    tool_name = tool_result['tool_name']
                    tool_args = tool_result['tool_args']
                    ret = _format_tool_result(tool_result['result'])
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
                    yield f"data: {json.dumps(tool_ret)}\n\n"
                    # 以 tool role 添加工具结果消息（符合 OpenAI API 规范）
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": ret
                    }
                    messages.append(tool_message)
        # 写入结束消息到文件
        await session_chat_memory.add_chat_history({"role": "assistant", "done": "[DONE]"})
        await _compact_session_history_if_needed(session_chat_memory, tool_request)
        # 清理工具管理内存（使用 session_id 隔离）
        await cleanup_chat_memory_manager(session_id)
        await cleanup_file_memory_manager(session_id)
        yield "data: [DONE]\n\n"
    except Exception as ce:
        await session_chat_memory.add_chat_history({"role": "assistant", "error": str(ce)})
        raise ce


async def stop_chat_task(session_id):
    ''' 手动优雅停止当前会话的聊天任务 '''
    try:
        (await get_chat_memory_manager(session_id)).run_task = False
    except Exception as e:
        raise e
