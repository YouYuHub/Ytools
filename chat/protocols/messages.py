"""Anthropic Messages request conversion and complete-response stream assembly."""
import copy
import json
import re
from urllib.parse import urlsplit, urlunsplit

from . import native_store
from .errors import MessagesProtocolError, MessagesUpstreamError
from .messages_content import content_blocks, tool_result_content, validate_payload
from util.media_limits import limit_media_messages


def endpoint(url):
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        raise MessagesProtocolError("Messages 模型地址不能指向 /chat/completions")
    if not path and parts.hostname == "api.anthropic.com":
        path = "/v1"
    if not path.endswith("/messages"):
        path += "/messages"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def headers(config):
    values = {"content-type": ("Content-Type", "application/json"),
              "anthropic-version": ("anthropic-version", "2023-06-01")}
    api_key = str(config.get("apiKey") or "")
    if api_key:
        # Anthropic Messages and Anthropic-compatible gateways (including
        # OpenCode Zen/Go) use x-api-key for API-key authentication. Callers
        # that require bearer auth can still override this via custom headers.
        values["x-api-key"] = ("x-api-key", api_key)
    explicit_auth = {}
    for item in config.get("_custom_headers") or []:
        if not isinstance(item, dict):
            continue
        name, value = str(item.get("name") or "").strip(), str(item.get("value") or "")
        key = name.casefold()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(c in name + value for c in "\r\n") or key in {"host", "content-length", "connection", "transfer-encoding"}:
            continue
        if key in {"authorization", "x-api-key"}:
            explicit_auth[key] = (name, value)
        else:
            values[key] = (name, value)
    if len(explicit_auth) > 1:
        raise MessagesProtocolError("请只配置 Authorization 或 x-api-key 中的一种鉴权头")
    if explicit_auth:
        values.pop("authorization", None)
        values.pop("x-api-key", None)
        values.update(explicit_auth)
    if urlsplit(config.get("url", "")).hostname == "api.anthropic.com" and not (
        "authorization" in values or "x-api-key" in values
    ):
        raise MessagesProtocolError("Anthropic API 缺少 API Key")
    return dict(values.values())


def _dump(value):
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


def text_blocks(content):
    return content_blocks(content, role="system")


def _append(messages, role, blocks):
    if not blocks:
        return
    # Do not merge native replay turns: that changes the signature-bound prefix.
    messages.append({"role": role, "content": blocks})


def convert_messages(source, *, as_facts=False, session_id=None):
    system, messages = [], []
    index = 0
    while index < len(source) and source[index].get("role") == "system":
        system.extend(text_blocks(source[index].get("content")))
        index += 1
    while index < len(source):
        message = source[index]
        role = message.get("role")
        if role == "system":
            raise MessagesProtocolError("通用 Messages 模式仅支持开头的 system 消息")
        if role == "tool":
            if as_facts:
                result = tool_result_content(message.get("content"), session_id=session_id)
                facts = [{"type": "text", "text": "已记录工具结果："}]
                facts.extend(result if isinstance(result, list) else [{"type": "text", "text": result or "（空）"}])
                _append(messages, "user", facts)
                index += 1
                continue
            raise MessagesProtocolError("工具结果没有紧邻的 assistant 工具调用")
        if role not in {"assistant", "user"}:
            raise MessagesProtocolError("Messages 不支持消息角色 " + str(role))
        blocks = content_blocks(message.get("content"), role=role, session_id=session_id)
        calls = message.get("tool_calls") or []
        if calls and as_facts:
            blocks.append({"type": "text", "text": "历史工具调用（事实记录，勿重复执行）：\n" + _text(calls)})
        elif calls:
            ids = []
            for call in calls:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in ids:
                    raise MessagesProtocolError("工具调用 ID 缺失或重复")
                function = call.get("function") or {}
                arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError as exc:
                    raise MessagesProtocolError("工具参数不是完整 JSON") from exc
                if not isinstance(arguments, dict) or not function.get("name"):
                    raise MessagesProtocolError("工具调用必须包含名称和 JSON 对象参数")
                ids.append(call_id)
                blocks.append({"type": "tool_use", "id": call_id, "name": function["name"], "input": arguments})
            _append(messages, role, blocks)
            results = {}
            index += 1
            while index < len(source) and source[index].get("role") == "tool":
                tool = source[index]
                call_id = tool.get("tool_call_id")
                if call_id not in ids or call_id in results:
                    raise MessagesProtocolError("工具结果 ID 未声明或重复")
                results[call_id] = {"type": "tool_result", "tool_use_id": call_id,
                                    "content": tool_result_content(tool.get("content"), session_id=session_id)}
                if tool.get("tool_is_error"):
                    results[call_id]["is_error"] = True
                index += 1
            if set(results) != set(ids):
                raise MessagesProtocolError("工具批次尚未生成全部结果")
            _append(messages, "user", [results[call_id] for call_id in ids])
            continue
        _append(messages, role, blocks)
        index += 1
    if not messages:
        raise MessagesProtocolError("Messages 请求缺少有效消息")
    return system, messages


def _compatible_parameters(payload, extra):
    """Translate common Chat extras without leaking Chat-only wire fields."""
    if 'stop_sequences' not in payload and extra.get('stop') is not None:
        stop = extra['stop']
        payload['stop_sequences'] = [stop] if isinstance(stop, str) else copy.deepcopy(stop)
    stops = payload.get('stop_sequences')
    if stops is not None and (not isinstance(stops, list) or any(not isinstance(stop, str) or not stop for stop in stops)):
        raise MessagesProtocolError('stop_sequences 必须为非空字符串数组')
    output = payload.get('output_config')
    if output is not None and not isinstance(output, dict):
        raise MessagesProtocolError('output_config 必须为 JSON 对象')
    response_format = extra.get('response_format')
    if response_format is None or (output or {}).get('format'):
        return
    if not isinstance(response_format, dict):
        raise MessagesProtocolError('response_format 必须为 JSON 对象')
    if response_format.get('type') == 'text':
        return
    if response_format.get('type') != 'json_schema':
        raise MessagesProtocolError('Messages 不支持该 response_format；请使用 output_config.format 的 JSON Schema')
    definition = response_format.get('json_schema') or {}
    schema = definition.get('schema') if isinstance(definition, dict) else None
    if not isinstance(schema, dict):
        raise MessagesProtocolError('response_format.json_schema 缺少 schema 对象')
    payload.setdefault('output_config', {})['format'] = {'type': 'json_schema', 'schema': copy.deepcopy(schema)}


def _tool_choice(request, tools):
    choice = request.tool_choice or 'auto'
    if isinstance(choice, str):
        choice = {'type': {'required': 'any'}.get(choice, choice)}
    elif choice.get('type') == 'function':
        function = choice.get('function') or {}
        choice = {'type': 'tool', 'name': function.get('name') if isinstance(function, dict) else None}
    else:
        choice = copy.deepcopy(choice)
    kind = choice.get('type')
    if kind not in {'auto', 'none', 'any', 'tool'}:
        raise MessagesProtocolError('Messages tool_choice 无效')
    if kind == 'tool' and choice.get('name') not in {tool['name'] for tool in tools}:
        raise MessagesProtocolError('tool_choice 指定的工具未在本次 tools 中声明')
    if kind == 'none':
        # ToolChoiceNone has only type; Chat's parallel flag is irrelevant here.
        return {'type': 'none'}
    if request.parallel_tool_calls is False:
        choice['disable_parallel_tool_use'] = True
    return choice


def build_payload(request, config, stream):
    source = []
    for message in request.messages:
        data = _dump(message)
        if getattr(message, "tool_is_error", None):
            data["tool_is_error"] = True
        source.append(data)
    thinking_requested = request.thinking or (request.extra_body or {}).get("thinking") or {}
    if not isinstance(thinking_requested, dict):
        raise MessagesProtocolError("thinking 必须为 JSON 对象")
    system, messages = convert_messages(source, as_facts=any(
        getattr(message, "provider_state", None) for message in request.messages)
        or thinking_requested.get("type") in {"enabled", "adaptive"}, session_id=request.session_id)
    tools = []
    for tool in request.tools or []:
        function = _dump(tool)["function"]
        schema = copy.deepcopy(function.get("parameters") or {"type": "object", "properties": {}})
        description = function.get("description") or ""
        if function["name"] == "read_media":
            description = "读取图片：media:// 引用、本地路径或 HTTP(S) 直链。每轮一次、最多 5 个来源；GIF 仅读取首帧。不支持音频、视频或时间区间；图片仅供下一次模型请求，quality 可选。"
            properties = schema.get("properties") or {}
            for field in ("start_time", "end_time"):
                properties.pop(field, None)
            if isinstance(properties.get("references"), dict):
                properties["references"]["description"] = "图片来源数组，支持会话 media:// 引用、本地路径和 http(s) 图片 URL。"
            if isinstance(schema.get("required"), list):
                schema["required"] = [field for field in schema["required"] if field not in {"start_time", "end_time"}]
        tools.append({"name": function["name"], "description": description, "input_schema": schema,
                      **({'strict': function['strict']} if function.get('strict') is not None else {})})
    payload = {"model": config["selected_model_id"], "messages": messages,
               "max_tokens": request.max_tokens, "stream": stream}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = _tool_choice(request, tools)
    if request.max_tokens <= 0:
        raise MessagesProtocolError("max_tokens 必须为正数")
    limit = config.get("maxOutputTokens") or config.get("max_output_tokens")
    if limit:
        if int(limit) <= 0:
            raise MessagesProtocolError("模型 maxOutputTokens 必须为正数")
        payload["max_tokens"] = min(payload["max_tokens"], int(limit))
    explicit = getattr(request, "model_fields_set", set())
    for key in ("temperature", "top_p", "thinking", "output_config", "stop_sequences"):
        value = getattr(request, key, None)
        if key in explicit and value is not None:
            payload[key] = copy.deepcopy(value)
    reserved = {"model", "messages", "system", "stream", "tools", "tool_choice", "parallel_tool_calls", "max_tokens",
                "presence_penalty", "reasoning_effort", "enable_thinking", "stream_options", "stop", "response_format"}
    for key, value in (request.extra_body or {}).items():
        if key not in reserved and key not in payload:
            payload[key] = copy.deepcopy(value)
    _compatible_parameters(payload, request.extra_body or {})
    # Older role/session parameter buckets can contain fields that a particular
    # Messages-compatible model rejects (for example, temperature on models that
    # have deprecated sampling controls). Enforce model metadata at the final
    # wire boundary so stale settings cannot bypass the UI.
    capabilities = config.get("parameterCapabilities") or config.get("parameter_capabilities") or {}
    if isinstance(capabilities, dict):
        for key, supported in capabilities.items():
            if supported is False:
                payload.pop(key, None)
        thinking = payload.get("thinking")
        if capabilities.get("thinking_budget") is False and isinstance(thinking, dict) and thinking.get("type") == "enabled":
            payload.pop("thinking", None)
    parent, appended = None, None
    # Only continue a currently pending native tool exchange. Completed prior turns
    # use fact projections, so history edits cannot revive stale signatures.
    for index in range(len(request.messages) - 1, -1, -1):
        reference = getattr(request.messages[index], "provider_state", None)
        if not reference:
            continue
        record = native_store.load_record(request.session_id, reference)
        if reference.get("reset") or not record or record["source_key"] != native_store.source_key(config):
            break
        if record["stop_reason"] != "tool_use":
            break
        blocks = [block for block in record["content"] if block["type"] == "tool_use"]
        results = {}
        suffix = source[index + 1:]
        if any(message.get("role") == "assistant" for message in suffix):
            # A later projection-only answer (e.g. another protocol or an aborted
            # continuation) starts a fresh epoch, rather than reviving this turn.
            break
        for tool in suffix:
            if tool.get("role") == "tool":
                call_id = tool.get("tool_call_id")
                if call_id in results:
                    raise MessagesProtocolError("原生工具结果重复")
                results[call_id] = {"type": "tool_result", "tool_use_id": call_id,
                                    "content": tool_result_content(tool.get("content"), session_id=request.session_id)}
                if tool.get("tool_is_error"):
                    results[call_id]["is_error"] = True
        for block in blocks:
            call_id = block["id"]
            status = record.get("tool_status", {}).get(call_id, {})
            if status.get("status") == "completed":
                # History display may truncate results; journalled model input
                # is authoritative for resuming this pending exchange.
                results[call_id] = {"type": "tool_result", "tool_use_id": call_id,
                                    "content": tool_result_content(status.get("content"), session_id=request.session_id), "is_error": status.get("is_error", False)}
            if call_id not in results:
                raise MessagesProtocolError("原生工具结果缺失或执行状态未知，不能自动重复执行；请重建任务上下文")
        if set(results) != {block["id"] for block in blocks}:
            raise MessagesProtocolError("原生工具结果包含未知调用 ID")
        appended = [{"role": "user", "content": [results[block["id"]] for block in blocks]}]
        extras = [message for message in suffix if message.get("role") != "tool"]
        if extras:
            _, converted = convert_messages(extras, as_facts=True, session_id=request.session_id)
            appended.extend(converted)
        payload["messages"] = native_store.replay_messages(request.session_id, record) + appended
        for key in ("system", "tools", "thinking", "output_config"):
            payload.pop(key, None)
            if record.get(key):
                payload[key] = copy.deepcopy(record[key])
        parent = record
        break
    if parent is None:
        limited, dropped = limit_media_messages(payload['messages'], config)
        if dropped:
            payload['messages'] = limited
    thinking = payload.get("thinking") or {}
    if thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1024 or budget >= payload["max_tokens"]:
            raise MessagesProtocolError("manual thinking budget_tokens 必须至少 1024 且小于 max_tokens")
        if payload.get("tool_choice", {}).get("type") in {"any", "tool"}:
            raise MessagesProtocolError("manual thinking 不支持强制工具选择")
    _validate_thinking_sampling(payload)
    validate_payload(payload, config)
    return payload, parent, appended


def _validate_thinking_sampling(payload):
    """thinking 开启（adaptive 或 manual）时，旧版 Claude 模型只接受默认 temperature 与 0.95–1 之间的 top_p。

    不静默改写用户设置：不满足时在发送前明确报错，由用户在参数面板调整。
    """
    thinking = payload.get("thinking") or {}
    if not isinstance(thinking, dict) or thinking.get("type") not in {"enabled", "adaptive"}:
        return
    temperature = payload.get("temperature")
    if temperature is not None and temperature != 1:
        raise MessagesProtocolError("开启 thinking 时 temperature 须为 1 或不设置（供应商限制），请调回 1 或关闭 thinking")
    top_p = payload.get("top_p")
    if top_p is not None and not (isinstance(top_p, (int, float)) and 0.95 <= top_p <= 1):
        raise MessagesProtocolError("开启 thinking 时 top_p 须在 0.95 到 1 之间或不设置（供应商限制），请调整或关闭 thinking")


def normalize_usage(usage):
    usage = copy.deepcopy(usage or {})
    prompt = sum(usage.get(key, 0) or 0 for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    completion = usage.get("output_tokens", 0) or 0
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "messages_usage": usage}


def normalize_response(response):
    reason = response.get("stop_reason")
    mapping = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
               "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "stop"}
    if reason not in mapping:
        raise MessagesProtocolError("不支持的 Messages stop_reason: " + str(reason))
    text, reasoning, calls, citations = [], [], [], []
    signed_thinking = True
    text_offset = 0
    for index, block in enumerate(response.get("content") or []):
        kind = block.get("type")
        if kind == "text":
            text.append(block.get("text") or "")
            for citation in block.get("citations") or []:
                citations.append({"content_block_index": index, "text_start": text_offset,
                                  "text_end": text_offset + len(text[-1]), "citation": copy.deepcopy(citation)})
            text_offset += len(text[-1])
        elif kind == "thinking":
            if not isinstance(block.get("signature"), str) or not block["signature"]:
                # Some Messages-compatible gateways expose reasoning without
                # Anthropic's opaque signature. It is displayable, but must never
                # be persisted as native replay or echoed back as a thinking block.
                signed_thinking = False
            reasoning.append(block.get("thinking") or "")
        elif kind == "redacted_thinking":
            if not isinstance(block.get("data"), str):
                raise MessagesProtocolError("redacted thinking 数据无效")
        elif kind == "tool_use":
            if not block.get("id") or not block.get("name") or not isinstance(block.get("input"), dict):
                raise MessagesProtocolError("tool_use 不是完整调用")
            calls.append({"index": len(calls), "id": block["id"], "type": "function",
                          "function": {"name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False)}})
        else:
            raise MessagesProtocolError("当前运行时不支持 Messages 响应内容块：" + str(kind))
    if len({call["id"] for call in calls}) != len(calls) or (reason == "tool_use" and not calls):
        raise MessagesProtocolError("Messages 工具调用缺失或 ID 重复")
    return {"id": response.get("id"), "content": "".join(text), "reasoning_content": "".join(reasoning),
            **({"citations": citations} if citations else {}),
            "tool_calls": calls if reason == "tool_use" else [],
            "finish_reason": mapping[reason], "stop_reason": reason,
            "stop_sequence": response.get("stop_sequence"),
            "usage": normalize_usage(response.get("usage")), "response_complete": True,
            "replay_eligible": signed_thinking and (not calls or reason == "tool_use"),
            "context_window_exceeded": reason == "model_context_window_exceeded",
            "refusal": reason == "refusal"}


class SSEFrames:
    def __init__(self):
        self.event, self.data = "", []

    def feed(self, line):
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        line = line.rstrip("\r\n")
        if not line:
            if not self.data:
                self.event = ""
                return None
            frame = (self.event, json.loads("\n".join(self.data)))
            self.event, self.data = "", []
            return frame
        if line.startswith("event:"):
            self.event = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            self.data.append(line[5:].lstrip(" "))
        return None


class MessagesStream:
    def __init__(self):
        self.response = None
        self.blocks, self.open_blocks, self.arguments = {}, set(), {}
        self.complete = False

    def feed(self, event, data):
        if not isinstance(data, dict):
            raise MessagesProtocolError("SSE 数据必须是 JSON 对象")
        kind = data.get("type") or event
        if kind == "error":
            error = data.get("error") or data
            raise MessagesUpstreamError(_text(error), error.get("type", "api_error") if isinstance(error, dict) else "api_error")
        if kind == "message_start":
            if self.response is not None:
                raise MessagesProtocolError("重复 message_start")
            self.response = copy.deepcopy(data["message"])
            self.response.setdefault("usage", {})
            return []
        if kind == "ping":
            return []
        if kind not in {"content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"}:
            return []
        if self.response is None:
            raise MessagesProtocolError("Messages 流缺少 message_start")
        if kind == "content_block_start":
            index = data["index"]
            if not isinstance(index, int) or index < 0:
                raise MessagesProtocolError("无效 content block index")
            if index in self.blocks:
                raise MessagesProtocolError("重复 content block index")
            block = copy.deepcopy(data["content_block"])
            if block.get("type") not in {"text", "thinking", "redacted_thinking", "tool_use"}:
                raise MessagesProtocolError("当前运行时不支持 Messages 响应内容块：" + str(block.get("type")))
            self.blocks[index] = block
            self.open_blocks.add(index)
            if block["type"] == "tool_use":
                self.arguments[index] = ""
                return [{"tool_call_preview": {"name": block["name"]}}]
            if block["type"] == "text" and block.get("text"):
                return [{"content": block["text"]}]
            if block["type"] == "thinking" and block.get("thinking"):
                return [{"reasoning_content": block["thinking"]}]
        elif kind == "content_block_delta":
            index = data["index"]
            if index not in self.open_blocks:
                raise MessagesProtocolError("delta 引用了未开启的 content block")
            block, delta = self.blocks[index], data["delta"]
            subtype = delta.get("type")
            if subtype == "text_delta" and block["type"] == "text":
                block["text"] = block.get("text", "") + delta["text"]
                return [{"content": delta["text"]}]
            if subtype == "thinking_delta" and block["type"] == "thinking":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
                return [{"reasoning_content": delta["thinking"]}]
            if subtype == "signature_delta" and block["type"] == "thinking":
                block["signature"] = block.get("signature", "") + delta["signature"]
                return [{"upstream_activity": True}]
            if subtype == "input_json_delta" and block["type"] == "tool_use":
                self.arguments[index] += delta.get("partial_json", "")
                return [{"upstream_activity": True}]
            if subtype == "citations_delta" and block["type"] == "text":
                citation = delta.get("citation")
                if not isinstance(citation, dict):
                    raise MessagesProtocolError("citations_delta 缺少 citation 对象")
                if block.get("citations") is None:
                    block["citations"] = []
                block["citations"].append(copy.deepcopy(citation))
                return []
            if subtype in {"text_delta", "thinking_delta", "signature_delta", "input_json_delta", "citations_delta"}:
                raise MessagesProtocolError("内容块与 delta 类型不匹配：" + str(subtype))
            # The API may add new delta types. Ignore opaque additions, but do
            # not commit tools whose argument stream could have been affected.
            if block["type"] in {"tool_use", "thinking"}:
                raise MessagesProtocolError("工具或签名块包含无法安全回放的新 delta：" + str(subtype))
            return []
        elif kind == "content_block_stop":
            index = data["index"]
            if index not in self.open_blocks:
                raise MessagesProtocolError("关闭了未开启的 content block")
            self.open_blocks.remove(index)
            if index in self.arguments and self.arguments[index]:
                try:
                    self.blocks[index]["input"] = json.loads(self.arguments[index])
                except ValueError:
                    # A token-limit stop can leave tool JSON incomplete. Never execute it.
                    self.blocks[index]["input"] = None
        elif kind == "message_delta":
            self.response.update(data.get("delta") or {})
            self.response["usage"].update(data.get("usage") or {})
        elif kind == "message_stop":
            if self.open_blocks:
                raise MessagesProtocolError("message_stop 前仍有未闭合内容块")
            self.response["content"] = [self.blocks[index] for index in sorted(self.blocks)]
            if self.response.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
                # Preserve visible text/thinking only; aborted tool blocks are diagnostics.
                self.response["content"] = [block for block in self.response["content"] if block["type"] != "tool_use"]
            normalized = normalize_response(self.response)
            self.complete = True
            return [normalized]
        return []
