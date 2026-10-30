"""Responses wire protocol, context modes and typed SSE assembly."""
import copy
import json
from urllib.parse import urlsplit, urlunsplit

from .errors import ResponsesProtocolError, ResponsesUpstreamError
from .responses_content import convert_messages


def endpoint(url):
    parts = urlsplit(str(url))
    path = parts.path.rstrip("/")
    if path.endswith(("/messages", "/chat/completions", "/responses/compact")):
        raise ResponsesProtocolError("Responses 模型地址必须是 API 基础地址或 /responses")
    if not path and parts.hostname == "api.openai.com":
        path = "/v1"
    if not path.endswith("/responses"):
        path += "/responses"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def response_mode(request, config):
    mode = getattr(request, "responses_mode", None) or config.get("responsesMode") or "stateful"
    if mode not in {"stateful", "stateless"}:
        raise ResponsesProtocolError("Responses 模式必须为 stateful 或 stateless")
    return mode


def parameters(request, config, stream):
    result = {"model": config["selected_model_id"], "store": response_mode(request, config) == "stateful", "stream": stream,
              "truncation": "disabled", "include": ["reasoning.encrypted_content"]}
    explicit = getattr(request, "model_fields_set", set())
    budget = request.max_output_tokens if request.max_output_tokens is not None else request.max_tokens
    if not isinstance(budget, int) or budget <= 0:
        raise ResponsesProtocolError("max_output_tokens 必须为正整数")
    maximum = config.get("maxOutputTokens") or config.get("max_output_tokens")
    result["max_output_tokens"] = min(budget, int(maximum)) if maximum else budget
    caps = config.get("parameterCapabilities") or config.get("parameter_capabilities") or {}
    for key in ("temperature", "top_p", "reasoning", "text"):
        value = getattr(request, key, None)
        if key in explicit and value is not None and caps.get(key) is not False:
            result[key] = copy.deepcopy(value)
    if "reasoning_effort" in explicit and caps.get("reasoning_effort") is not False:
        result.setdefault("reasoning", {}).setdefault("effort", request.reasoning_effort)
    reserved = {"model", "store", "stream", "input", "instructions", "tools", "tool_choice",
                "previous_response_id", "conversation", "max_tokens", "max_output_tokens",
                "presence_penalty", "enable_thinking", "thinking", "output_config",
                "stop", "stop_sequences", "stream_options", "context_management", "background", "responses_mode"}
    include = (request.extra_body or {}).get("include")
    if isinstance(include, list):
        result["include"] = list(dict.fromkeys([*result["include"], *include]))
    for key, value in (request.extra_body or {}).items():
        if key not in reserved and caps.get(key) is not False and key not in result:
            result[key] = copy.deepcopy(value)
    tools = []
    for tool in request.tools or []:
        raw = tool.model_dump(exclude_none=True) if hasattr(tool, "model_dump") else tool
        function = raw.get("function") or {}
        function = copy.deepcopy(function)
        if function.get("name") == "read_media":
            function["description"] = ("读取图片或视频：本地路径、HTTP(S) 直链或 media:// 引用。每轮一次、"
                                       "最多 5 个来源；视频转为关键帧拼图（单图）查看，start_time/end_time "
                                       "可选区间；GIF 仅读首帧。不支持音频；图片仅供下一次模型请求。")
        tools.append({"type": "function", "name": function["name"],
                      "description": function.get("description") or "",
                      "parameters": copy.deepcopy(function.get("parameters") or {"type": "object", "properties": {}}),
                      "strict": bool(function.get("strict", False))})
    if tools:
        result["tools"] = tools
        choice = copy.deepcopy(request.tool_choice or "auto")
        if isinstance(choice, dict) and choice.get("type") == "function":
            choice = {"type": "function", "name": (choice.get("function") or {}).get("name") or choice.get("name")}
        result["tool_choice"] = choice
        result["parallel_tool_calls"] = request.parallel_tool_calls is not False
    for key in ("reasoning", "text"):
        if key in result and not isinstance(result[key], dict):
            raise ResponsesProtocolError(key + " 必须为 JSON 对象")
    return result


def normalize_response(response, *, require_stored=True):
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        raise ResponsesProtocolError("Responses 成功响应缺少 response.id")
    if require_stored and response.get("store") is False:
        raise ResponsesProtocolError("平台未保存响应，请在模型参数中选择无状态 Responses 兼容模式")
    status = response.get("status")
    if status != "completed":
        detail = response.get("error") or response.get("incomplete_details") or {"status": status}
        raise ResponsesUpstreamError("Responses 未完成：" + json.dumps(detail, ensure_ascii=False), response)
    texts, reasoning, calls, call_ids = [], [], [], set()
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text") or "")
                elif part.get("type") == "refusal":
                    texts.append(part.get("refusal") or "")
        elif kind == "reasoning":
            reasoning.extend(part.get("text") or "" for part in item.get("summary") or [] if part.get("type") == "summary_text")
        elif kind == "function_call":
            call_id, name, args = item.get("call_id"), item.get("name"), item.get("arguments")
            # A malformed tool item in an otherwise completed response is an upstream
            # data failure, not a local protocol violation: report precisely which
            # check failed and attach the raw response so the retry layer persists
            # it for forensics instead of failing the turn without evidence.
            label = "第 %d 个 function_call（call_id=%s，name=%s）" % (
                len(calls) + 1, call_id or "<缺失>", name or "<缺失>")
            if not call_id:
                raise ResponsesUpstreamError("Responses 工具调用缺少 call_id：" + label, response)
            if call_id in call_ids:
                raise ResponsesUpstreamError("Responses 工具调用 call_id 重复：" + label, response)
            if not name:
                raise ResponsesUpstreamError("Responses 工具调用缺少名称：" + label, response)
            if item.get("status") not in {None, "completed"}:
                raise ResponsesUpstreamError(
                    "Responses 工具调用完成状态无效：%s，status=%r" % (label, item.get("status")), response)
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError) as exc:
                raise ResponsesProtocolError("Responses 工具参数不是完整 JSON") from exc
            if not isinstance(parsed, dict):
                raise ResponsesProtocolError("Responses 工具参数必须是 JSON 对象")
            call_ids.add(call_id)
            calls.append({"index": len(calls), "id": call_id, "type": "function", "function": {"name": name, "arguments": args}})
        elif kind != "compaction":
            raise ResponsesProtocolError("Responses 返回未启用的 output item：" + str(kind))
    usage = response.get("usage") or {}
    result = {"id": response_id, "content": "".join(texts), "reasoning_content": "".join(reasoning),
              "tool_calls": calls, "finish_reason": "tool_calls" if calls else "stop",
              "response_status": status, "response_complete": True}
    if usage:
        result["usage"] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens_details": copy.deepcopy(usage.get("input_tokens_details") or {}),
            "completion_tokens_details": copy.deepcopy(usage.get("output_tokens_details") or {}),
        }
    return result


class ResponsesStream:
    def __init__(self, *, require_stored=True):
        self.require_stored = require_stored
        self.response = None
        self.response_id = None
        self.content = ""
        self.reasoning = ""
        self.complete = False
        self.last_sequence = -1

    def feed(self, event):
        sequence = event.get("sequence_number")
        if isinstance(sequence, int):
            if sequence <= self.last_sequence:
                return []
            self.last_sequence = sequence
        kind = event.get("type")
        if kind == "response.created":
            self.response_id = (event.get("response") or {}).get("id")
            return [{"upstream_activity": True}]
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            delta = event.get("delta") or ""
            self.content += delta
            return [{"id": self.response_id, "content": delta}]
        if kind == "response.reasoning_summary_text.delta":
            delta = event.get("delta") or ""
            self.reasoning += delta
            return [{"id": self.response_id, "reasoning_content": delta}]
        if kind == "response.function_call_arguments.delta":
            return [{"tool_call_preview": True, "upstream_activity": True}]
        if kind == "response.compaction.compacting":
            return [{"upstream_activity": True, "event": "responses_compaction", "phase": "progress"}]
        if kind in {"response.completed", "response.failed", "response.incomplete"}:
            self.response = copy.deepcopy(event.get("response") or {})
            self.response_id = self.response.get("id") or self.response_id
            normalized = normalize_response(self.response, require_stored=self.require_stored)
            events = []
            for field, received in (("content", self.content), ("reasoning_content", self.reasoning)):
                final = normalized.pop(field, "")
                if not final.startswith(received):
                    raise ResponsesProtocolError("Responses 最终内容与已接收增量不一致")
                if len(final) > len(received):
                    events.append({"id": self.response_id, field: final[len(received):]})
            self.complete = True
            return [*events, normalized]
        if kind == "error":
            raise ResponsesUpstreamError(str(event.get("message") or event.get("error") or "Responses 上游错误"))
        return [{"upstream_activity": True}]
