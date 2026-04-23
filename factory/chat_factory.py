# py 库导入
# import re
import json
# import time
import asyncio
# import inspect
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, AsyncGenerator

# 自定义模块导入
from chat.chat_tool import ChatTool
from config import Message, ChatToolRequest, get_current_dir
from load_env import load_var
from memory.chat_memory import get_chat_memory_manager, cleanup_chat_memory_manager
from memory.file_memory import get_file_memory_manager, cleanup_file_memory_manager
from util.mcp_client import call_mcp_tool


ALL_TOOLS: List[Dict[str, Any]] = []
TOOL_MCP_SERVERS: Dict[str, str] = {}


def _filter_parameters(params: dict) -> dict:
    """
    过滤 parameters 对象，移除非JSON Schema标准字段
    保留 type, properties, required, items, default 等标准字段
    Args:
        params: 原始parameters对象
    Returns:
        过滤后的parameters对象
    """
    if not isinstance(params, dict):
        return params
    filtered = {}
    # JSON Schema 标准字段白名单（包含 default）
    standard_fields = {
        "type", "properties", "required", "items", 
        "additionalProperties", "enum", "const",
        "anyOf", "allOf", "oneOf", "not",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern",
        "minItems", "maxItems", "uniqueItems",
        "format", "default"  # ✅ 保留 default 字段
    }
    for key, value in params.items():
        if key in standard_fields:
            # 递归处理嵌套的 properties
            if key == "properties" and isinstance(value, dict):
                filtered[key] = {
                    prop_name: _filter_parameter_property(prop_def)
                    for prop_name, prop_def in value.items()
                }
            # 递归处理 items
            elif key == "items" and isinstance(value, dict):
                filtered[key] = _filter_parameters(value)
            else:
                filtered[key] = value
        # 忽略 title 等非标准字段
    return filtered


def _filter_parameter_property(prop_def: dict) -> dict:
    """
    过滤单个参数属性的定义
    Args:
        prop_def: 参数属性定义
    Returns:
        过滤后的参数属性定义
    """
    if not isinstance(prop_def, dict):
        return prop_def
    filtered = {}
    # JSON Schema 标准字段（包含 default）
    standard_fields = {
        "type", "description", "enum", "const",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern",
        "format", "default", "items",  # ✅ 保留 default 字段
        "anyOf", "allOf", "oneOf"
    }
    for key, value in prop_def.items():
        if key in standard_fields:
            if key == "items" and isinstance(value, dict):
                filtered[key] = _filter_parameters(value)
            else:
                filtered[key] = value
    return filtered


def _filter_tool_for_api(tool_def: dict) -> dict:
    """
    过滤工具定义，只保留OpenAI API标准字段
    移除 server_file、title 等非标准字段
    Args:
        tool_def: 原始工具定义（包含server_file等额外字段）
    Returns:
        符合OpenAI API标准的工具定义
    """
    if not isinstance(tool_def, dict):
        return tool_def
    filtered = {}
    # 保留 type 字段
    if "type" in tool_def:
        filtered["type"] = tool_def["type"]
    # 处理 function 字段
    if "function" in tool_def and isinstance(tool_def["function"], dict):
        func_info = tool_def["function"]
        filtered_func = {}
        # 只保留标准字段：name, description, parameters
        if "name" in func_info:
            filtered_func["name"] = func_info["name"]
        if "description" in func_info:
            filtered_func["description"] = func_info["description"]
        # 处理 parameters，递归过滤非标准字段
        if "parameters" in func_info and isinstance(func_info["parameters"], dict):
            filtered_func["parameters"] = _filter_parameters(func_info["parameters"])
        filtered["function"] = filtered_func
    return filtered


# 从 tools.json 加载所有工具并过滤为标准 API 格式
def _load_and_filter_tools() -> tuple[list, dict]:
    """
    从 tools.json 加载工具定义，同时完成：
    1. 过滤出符合OpenAI API标准的工具定义
    2. 构建工具名到MCP服务器文件的映射
    Returns:
        (filtered_tools, tool_mcp_servers): 过滤后的工具列表和MCP服务器映射
    """
    tools_json_path = get_current_dir() + "/tools.json"
    with open(tools_json_path, "r", encoding="utf-8") as f:
        raw_tools = json.load(f)
    filtered_tools = []
    tool_mcp_servers = {}
    for tool_def in raw_tools:
        if not isinstance(tool_def, dict):
            continue
        # 提取 server_file 用于构建映射（在过滤前）
        func_info = tool_def.get("function", {})
        tool_name = func_info.get("name")
        server_file = func_info.get("server_file")
        if tool_name and server_file:
            tool_mcp_servers[tool_name] = server_file
        # 过滤工具定义为API标准格式
        filtered_tool = _filter_tool_for_api(tool_def)
        if filtered_tool:
            filtered_tools.append(filtered_tool)
    return filtered_tools, tool_mcp_servers


# 加载并过滤工具，同时构建 MCP 映射
async def load_all_tools() -> None:
    """ 加载并过滤工具，同时构建MCP映射 """
    global ALL_TOOLS, TOOL_MCP_SERVERS
    ALL_TOOLS, TOOL_MCP_SERVERS = _load_and_filter_tools()
    print(f"✅ 已加载 {len(ALL_TOOLS)} 个工具，构建 {len(TOOL_MCP_SERVERS)} 个MCP映射")
    # for name, server in TOOL_MCP_SERVERS.items():
    #     print(f"   - {name}: {server}")
asyncio.run(load_all_tools())   # 初始化工具加载
print(f"ALL_TOOLS: {ALL_TOOLS}\n\n\nTOOL_MCP_SERVERS: {TOOL_MCP_SERVERS}\n")


def _parse_tool_call(tool_call: dict) -> tuple[str | None, dict]:
    if not isinstance(tool_call, dict):
        return None, {}
    function_info = tool_call.get("function") or {}
    tool_name = function_info.get("name")
    arguments = function_info.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {"arguments": arguments}
    return tool_name, arguments


def _format_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def _tool_result_assistant_message(tool_name: str, tool_args: dict, result: str) -> dict:
    # history_block = _build_tool_history_block_text() 没必要添加历史，因为会追加 assistant 消息
    content = (
        # f"\n{tool_name}工具调用结果：[\n"
        "\n工具调用结果：[\n"
        f"function: {tool_name}\n"
        f"arguments: {json.dumps(tool_args, ensure_ascii=False)}\n"
        f"result: {result}\n]\n"
    )
    return {"role": "assistant", "content": content}


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


def _merge_function_call_delta(accumulated: List[dict], function_call_delta: dict) -> None:
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


def _merge_tool_call_delta(accumulated: List[dict], tool_calls_delta: list[dict]) -> None:
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
                # "id": "",
                "function": {
                    "name": "",
                    "arguments": ""
                },
                # "type": "function"
            }
            accumulated.append(existing_tc)
        delta_function = delta.get("function", {})
        # 累加 id（如果有的话）
        # if delta.get("id"):
        #     existing_tc["id"] += delta["id"]
        # 累加 function.name
        if delta_function.get("name"):
            existing_tc["function"]["name"] += delta_function["name"]
        # 累加 function.arguments
        if delta_function.get("arguments"):
            existing_tc["function"]["arguments"] += delta_function["arguments"]
            # print(existing_tc["function"]["arguments"], end="")
        # 设置 type（如果有的话）
        # if delta.get("type"):
        #     existing_tc["type"] = delta["type"]
    # 遵守 ai 给的 index 字段排序
    accumulated.sort(key=lambda x: x["index"])


def _invoke_tool_function(name: str, arguments: dict) -> Any:
    """调用 MCP 工具（同步包装器，在线程池中执行）"""
    # 检查工具是否在MCP映射中
    if name not in TOOL_MCP_SERVERS:
        raise ValueError(f"工具 {name} 未在MCP服务器中注册，无法调用")
    # 获取对应的MCP服务器文件
    mcp_server_file = TOOL_MCP_SERVERS[name]
    # 在线程池中运行异步MCP调用
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            call_mcp_tool(
                function_name=name,
                arguments=arguments,
                mcp_service_file=mcp_server_file
            )
        )
        return result
    finally:
        loop.close()


async def tool_chat_server(tool_request: ChatToolRequest, api_url=None) -> AsyncGenerator[str, None]:
    """工具聊天服务器，按需补齐工具并以 SSE 格式输出模型响应。"""
    # 从请求中获取 session_id
    session_id = getattr(tool_request, 'session_id', 'default')
    # 获取所有工具
    tool_request.tools = ALL_TOOLS
    # print(f"tools: {tool_request.tools}")
    tool_choice = tool_request.tool_choice
    if not isinstance(tool_choice, (str, dict)):
        tool_choice = "auto"
    # extra_body = dict(tool_request.extra_body) if tool_request.extra_body else {}
    messages: List[dict] = []
    for message in tool_request.messages or []:
        if hasattr(message, "model_dump"):
            messages.append(message.model_dump())
        elif hasattr(message, "dict"):
            messages.append(message.dict())
        else:
            messages.append(message)
    # 聊天记录，用于记录历史聊天所有信息
    session_chat_memory = await get_chat_memory_manager(session_id)
    session_file_memory = await get_file_memory_manager(session_id)
    try:
        await session_chat_memory.add_chat_history(messages)   # 不需要记录项目中的系统提示词
        # 检查是否存在系统提示词，添加一些提示
        if messages[0]["role"] == "system":
            messages[0]["content"] += (
                f"\n当前工作路径为<{_format_tool_result(get_current_dir())}>\n"
                # "注意：目前工具函数仅支持单个使用，不支持并发批量使用；所以请一次最多使用一个工具。"
                "程序支持并发工具，你可以选择并发工具调用哦！\n"
                "工具返回结果为 [] 表示空值\n"
                "历史工具调用记录将通过 assistant 消息追加。\n"
                "你需要调用 over_task() 函数（该函数无参数）才能结束你的任务，这将是任务最终的结束信号！\n"
                "- 当你选择调用 over_task() 函数结束当前一次或多次工具调用任务的时候，请向用户总结说明你的操作（over_task 除外），简短说明概要即可。\n"
                # "不管是否调用工具，当你最终回答用户时都需要调用 over_task，因为你只能通过该方式结束当前任务或会话，否则消息可能会越来越长"
            )
        else:
            messages.insert(0, {
                "role": "system",
                "content": (
                    "你是一个聪明的助手，你需要自行判断是否需要调用工具，并结合你之前使用的工具（如果有）的结果，调用工具的结果只有你可见，需要你回答用户问题（根据用户使用的语言回答）；\n"
                    f"当前工作路径为<{_format_tool_result(get_current_dir())}>\n"
                    "程序支持并发工具，你可以选择并发工具调用哦！\n"
                    "工具返回结果为 [] 表示空值\n"
                    "历史工具调用记录将通过 assistant 消息追加。\n"
                    "你需要调用 over_task() 函数（该函数无参数）才能结束你的任务，这将是任务最终的结束信号！\n"
                    "- 当你选择调用 over_task() 函数结束当前一次或多次工具调用任务的时候，请向用户总结说明你的操作（over_task 除外），简短说明概要即可。\n"
                    # "不管是否调用工具，当你最终回答用户时都需要调用 over_task，因为你只能通过该方式结束当前任务或会话，否则消息可能会越来越长"
                ),
            })
        # 这里做文件处理，并添加文件解析内容到 "content" 字段
        user_files = session_file_memory.get_file_memory_chat()
        if user_files:
            messages[0]["content"] += (
                f"\n用户上传了 {len(user_files)} 个文件，解析的内容如下：\n"
                f"{user_files}"
            )
        # run_task = True
        count_nouse_tool = 0  # 无用工具调用次数
        # last_tool_ret = ""  # 上一次调用工具的返回值
        # while run_task:
        while session_chat_memory.run_task:
            print(f"massages: [\n\t{',\n\t'.join(map(str, messages))}\n]")
            full_response = ""
            full_reasoning = ""
            tool_calls: List[dict] = []
            # 将 messages 附加到 tool_request 中
            tool_request.messages = [Message(**msg) if isinstance(msg, dict) else msg for msg in messages]
            async for sse_chunk in ChatTool.chat_with_history_sse(api_url=api_url,
                request=tool_request, # 传递完整的 ChatToolRequest 对象（已包含 messages）
            ):
                # print(f"sse_chunk: {sse_chunk}")
                event = _parse_sse_event(sse_chunk)
                if event is None:
                    yield sse_chunk
                    continue
                if event.get("done") or not session_chat_memory.run_task:
                    break
                if event.get("error") is not None:
                    yield sse_chunk
                    # run_task = False
                    session_chat_memory.run_task = False
                    break
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
            # print(f"[DEBUG] tool_calls: {tool_calls}")
            # 模型回复过程中用户手动停止任务
            if not session_chat_memory.run_task:
                print("用户手动停止任务")
                # 记录 ai 生成的内容
                if full_reasoning:
                    await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
                if full_response:
                    await session_chat_memory.add_chat_history({"role": "assistant", "content": full_response})
                # 添加用户手动停止任务消息的记录
                await session_chat_memory.add_chat_history({"role": "user", "content": "用户手动停止任务"})
                break  # 任务结束，退出循环
            # 检查 tool_calls 是否为空
            if not tool_calls:
                count_nouse_tool += 1 # 不调用工具次数
                if len(messages) == 2 or count_nouse_tool >= int(load_var("WITH_OUT_TOOL_COUNT", 3)): # 第一次对话就不调用工具或者不使用工具次数达到或超过指定次数
                    # 如果模型本次流式响应输出了内容，则补充一条文本型的 assistant 历史消息。
                    if full_response:   # 优先考虑模型回答的内容
                        assistant_message = {"role": "assistant", "content": full_response}
                        messages.append(assistant_message)
                    elif full_reasoning: # 否则使用模型推理的推理内容
                        assistant_message = {"role": "assistant", "content": f"...{full_reasoning[-100:]}"}   # 只保留最近 100 个字符
                        messages.append(assistant_message)
                    # run_task = False  # 停止任务
                    session_chat_memory.run_task = False  # 停止任务
                else:
                    # 提醒模型使用 over_task 函数结束任务
                    if full_response:   # 优先考虑模型回答的内容
                        assistant_message = {"role": "assistant", "content": full_response}
                        messages.append(assistant_message)
                    elif full_reasoning: # 否则使用模型推理的推理内容
                        assistant_message = {"role": "assistant", "content": f"...{full_reasoning[-100:]}"}   # 只保留最近 100 个字符
                        messages.append(assistant_message)
                assistant_message = {"role": "user", "content": "你可以选择调用 over_task 结束或者调用其他工具，请注意：你只能通过调用 over_task 工具结束任务，不能通过其他方式结束任务，不调用工具会继续任务！"}
                messages.append(assistant_message)
                print("\n[INFO] 本轮对话未返回工具调用，继续任务")
                continue  # 继续
            # 只要有思考过程就添加模型聊天思考历史记录到文件
            if full_reasoning:
                await session_chat_memory.add_chat_history({"role": "assistant", "reasoning_content": full_reasoning})
            # 如果模型本次流式响应输出了内容，则补充一条文本型的 assistant 历史消息。
            if full_response:   # 优先考虑模型回答的内容
                assistant_message = {"role": "assistant", "content": full_response}
                messages.append(assistant_message)
                # 添加模型聊天响应历史记录到文件
                await session_chat_memory.add_chat_history(assistant_message)
            elif full_reasoning: # 否则使用模型推理的推理内容
                messages.append({"role": "assistant", "content": f"...{full_reasoning[-100:]}"}) # 只保留最近 100 个字符
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
            # 解析所有工具调用，分离 over_task 和其他工具
            parsed_tools = []
            over_task_call = None
            has_parse_error = False
            for i, tool_call in enumerate(tool_calls, 1):
                tool_name, tool_args = _parse_tool_call(tool_call)
                if not tool_name:
                    print(f"[WARNING] 第{i}个工具调用解析失败（无工具名），跳过")
                    continue
                # 检查是否是 over_task 工具
                if tool_name == "over_task":
                    print("\n[INFO] 检测到 over_task 信号，将在此轮工具执行完成后结束任务")
                    over_task_call = (i, tool_call, tool_name, tool_args)
                    # 注意：不立即 break，继续收集其他工具
                else:
                    # 验证参数是否为空（可能是JSON解析失败导致）
                    func_info = tool_call.get("function", {})
                    arguments_str = func_info.get("arguments", "")
                    if arguments_str and not tool_args:
                        print(f"[ERROR] 工具 {tool_name} 有参数字符串但解析后为空，可能是JSON不完整！")
                        print(f"        原始参数字符串: {arguments_str[:300]}...")
                        has_parse_error = True
                    parsed_tools.append((i, tool_call, tool_name, tool_args))
            # 如果有解析错误，警告用户
            if has_parse_error:
                print("[WARNING] 检测到工具参数解析错误，可能导致工具执行失败")
            # 如果没有任何有效工具可执行，退出
            if not parsed_tools and not over_task_call:
                print("[WARNING] 没有可执行的有效工具，退出循环")
                break
            print(f"\n[INFO] 准备执行 {len(parsed_tools)} 个工具" + (" + over_task" if over_task_call else ""))
            # 使用线程池并发执行所有普通工具（不包括 over_task）
            if parsed_tools:
                tool_results = []
                max_workers = min(10, len(parsed_tools))  # 最多10个线程

                # 定义工具执行函数
                def execute_tool(index, tool_call, tool_name, tool_args):
                    try:
                        # print(f"[THREAD START] 开始执行工具 #{index}: {tool_name}", flush=True)
                        result = _invoke_tool_function(tool_name, tool_args)
                        ret = _format_tool_result(result)
                        # print(f"[THREAD SUCCESS] 工具 #{index} {tool_name} 执行成功", flush=True)
                        return index, tool_call, tool_name, tool_args, ret, None
                    except Exception as exc:
                        ret = f"工具执行失败：{exc}"
                        print(f"[THREAD ERROR] 工具 #{index} {tool_name} 执行失败: {exc}", flush=True)
                        import traceback
                        traceback.print_exc()
                        return index, tool_call, tool_name, tool_args, ret, exc

                # 在线程池中执行所有普通工具
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(execute_tool, idx, tc, tn, ta): (idx, tc, tn, ta)
                        for idx, tc, tn, ta in parsed_tools
                    }
                    # 收集所有结果
                    completed_count = 0
                    for future in as_completed(futures):
                        try:
                            index, tool_call, tool_name, tool_args, ret, error = future.result()
                            # status = "✅" if not error else "❌"
                            # print(f"[COLLECT {completed_count + 1}/{len(futures)}] {status} 工具 #{index} {tool_name}", flush=True)
                            tool_results.append({
                                'index': index,
                                'tool_call': tool_call,
                                'tool_name': tool_name,
                                'tool_args': tool_args,
                                'result': ret,
                                'error': error
                            })
                            completed_count += 1
                        except Exception as e:
                            # 处理未来对象本身的异常
                            idx, tc, tn, ta = futures[future]
                            print(f"[FUTURE ERROR] 获取工具 #{idx} {tn} 结果时异常: {e}", flush=True)
                            import traceback
                            traceback.print_exc()
                            tool_results.append({
                                'index': idx,
                                'tool_call': tc,
                                'tool_name': tn,
                                'tool_args': ta,
                                'result': f"工具执行异常：{e}",
                                'error': e
                            })
                    print(f"[INFO] 有 {completed_count} 个工具执行完成", flush=True)
                # 按索引排序结果
                tool_results.sort(key=lambda x: x['index'])
                # 统一添加所有工具结果到 messages 和历史记录（使用 session_id 隔离）
                # 获取当前会话的记忆管理器
                for tool_result in tool_results:
                    tool_name = tool_result['tool_name']
                    tool_args = tool_result['tool_args']
                    ret = _format_tool_result(tool_result['result'])
                    await session_chat_memory.add_chat_history(
                        input_text={
                            "role": "assistant",
                            "tool_return": {"function": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False), "result": ret}
                        }
                    )
                    # print(f"\n\n函数{tool_name}参数(字典表示)为{tool_args}执行结果：")
                    # print(ret, end="\n\n\n")
                    # 发送工具调用结果
                    tool_ret = {
                        "tool_return": {
                            "function_name": tool_name,
                            "arguments": tool_args,
                            "result": ret
                        }
                    }
                    yield f"data: {json.dumps(tool_ret)}\n\n"
                    # 添加 assistant 消息
                    assistant_tool_call = _tool_result_assistant_message(tool_name, tool_args, ret)
                    messages.append(assistant_tool_call)
                    # last_tool_ret = assistant_tool_call["content"]
            # 所有普通工具执行完成后，检查是否有 over_task 信号
            if over_task_call:
                idx, tool_call, tool_name, tool_args = over_task_call
                print(f"\n[INFO] 所有工具执行完成，检测到 over_task 信号，准备结束任务")
                # over_task 只是一个结束信号，不需要真正执行
                # 但为了保持一致性，仍然添加到历史记录和消息中
                ret = "任务结束信号已接收"
                await session_chat_memory.add_chat_history(
                    input_text={
                        "role": "assistant",
                        "tool_return": {"function": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False), "result": ret}
                    }
                )
                # print(f"\n\n函数{tool_name}参数(字典表示)为{tool_args}执行结果：")
                # print(ret, end="\n\n\n")
                # messages.append(_tool_result_assistant_message(tool_name, tool_args, ret))
                # 标记任务结束，退出循环
                # run_task = False
                session_chat_memory.run_task = False
                print("[INFO] over_task 信号已处理，任务结束")
        # 写入结束消息到文件
        await session_chat_memory.add_chat_history({"role": "assistant", "done": "[DONE]"})
        # 清理工具管理内存（使用 session_id 隔离）
        await cleanup_chat_memory_manager(session_id)
        await cleanup_file_memory_manager(session_id)
        yield "data: [DONE]\n\n"
    except Exception as ce:
        session_chat_memory.add_chat_history({"role": "assistant", "error": ce})
        raise ce


async def stop_chat_task(session_id):
    ''' 手动优雅停止当前会话的聊天任务 '''
    try:
        (await get_chat_memory_manager(session_id)).run_task = False
    except Exception as e:
        raise e
