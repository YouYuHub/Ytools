"""Stable input identities for one generation; private remote state stays separate."""
import copy
import uuid

from chat.protocols import responses_store
from chat.protocols.responses import parameters, response_mode
from chat.protocols.responses_content import convert_messages, native_input_items
from chat.protocols.errors import ResponsesProtocolError


def assign_ids(messages):
    for message in messages:
        if isinstance(message, dict):
            message.setdefault("context_item_id", uuid.uuid4().hex)


def start_turn(messages, *, scope="chat_model", start_index=None):
    assign_ids(messages)
    if start_index is None:
        start_index = next((i for i in range(len(messages) - 1, -1, -1)
                            if messages[i].get("role") == "user"), 0)
    return {"scope": scope, "turn_uid": uuid.uuid4().hex,
            "seen_ids": [m["context_item_id"] for m in messages[:start_index]],
            "initialized": False}


def mark_baseline(context, messages):
    assign_ids(messages)
    context["seen_ids"] = list({*context.get("seen_ids", []),
                              *(m["context_item_id"] for m in messages)})


def _source(request):
    result = []
    for message in request.messages:
        raw = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else copy.deepcopy(message)
        reference = getattr(message, "provider_state", None) if hasattr(message, "model_dump") else raw.get("provider_state")
        if reference:
            raw["provider_state"] = copy.deepcopy(reference)
        context_id = getattr(message, "context_item_id", None) if hasattr(message, "model_dump") else raw.get("context_item_id")
        if not context_id:
            context_id = uuid.uuid4().hex
            if hasattr(message, "context_item_id"):
                message.context_item_id = context_id
            else:
                message["context_item_id"] = context_id
        raw["context_item_id"] = context_id
        result.append(raw)
    return result


def prepare(request, config, stream):
    source = _source(request)
    context = request.responses_context
    if context is None:
        context = start_turn(source)
        request.responses_context = context
    scope = context.get("scope") or "chat_model"
    state = responses_store.load_state(request.session_id, scope)
    mode = response_mode(request, config)
    identity = responses_store.source_key(config)
    if state.get("source_key") and state["source_key"] != identity:
        state = responses_store.invalidate(request.session_id, "模型、端点或账户配置已变化", scope)
    if state.get("responses_mode", "stateful") != mode:
        state = responses_store.invalidate(request.session_id, "Responses 上下文模式已切换", scope)
    if state.get("pending_request_ref"):
        raise ResponsesProtocolError("Responses 有未确认请求，请重新同步上下文后继续")
    payload = parameters(request, config, stream)
    instructions = []
    if source and source[0].get("role") == "system":
        content = source[0].get("content")
        instructions.append(content if isinstance(content, str) else
                            "\n".join(p.get("text", "") for p in (content or []) if isinstance(p, dict)))
        source = source[1:]
    if request.instructions:
        instructions.append(request.instructions)
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if mode == "stateless":
        payload["input"] = convert_messages(source, request.session_id, replay_config=config)
        if not payload["input"]:
            raise ResponsesProtocolError("Responses 请求没有有效的本地上下文")
        context["initialized"] = True
        return payload, [m["context_item_id"] for m in source], state
    seen = set(context.get("seen_ids") or [])
    deferred_ids = set(context.get("deferred_ids") or [])
    new_messages = [m for m in source if m["context_item_id"] not in seen | deferred_ids and m.get("role") != "assistant"]
    new_ids = [*deferred_ids, *(m["context_item_id"] for m in new_messages)]
    delta_items = [*copy.deepcopy(context.get("deferred_items") or []),
                   *convert_messages(new_messages, request.session_id, delta=True)]
    if state.get("pending_compaction_ref"):
        compacted = responses_store.current_window(request.session_id, state)
        covered = set(responses_store.compaction_input_ids(request.session_id, state["pending_compaction_ref"]))
        remaining, _ = pending_native_input(request, exclude_ids=covered)
        payload["input"] = [*native_input_items(compacted), *remaining]
    elif state.get("head_response_id"):
        payload["previous_response_id"] = state["head_response_id"]
        head = responses_store.load_record(request.session_id, {
            "protocol": "responses", "record_id": state["head_record_id"]})
        provided = {item["call_id"] for item in delta_items if item.get("type") == "function_call_output"}
        recovered_outputs = []
        for call_id, result in (head or {}).get("tool_status", {}).items():
            if call_id in provided:
                continue
            if result.get("status") != "completed":
                error = "上次工具轮没有确认的完整结果，请核对实际执行效果并重新同步上下文；旧工具不会自动执行"
                responses_store.require_recovery(request.session_id, scope, error)
                raise ResponsesProtocolError(error)
            recovered_outputs.append({"type": "function_call_output", "call_id": call_id,
                                      "output": copy.deepcopy(result.get("content") or "")})
        delta_items = [*recovered_outputs, *delta_items]
        payload["input"] = delta_items
        if responses_store.needs_compaction_activation(state):
            # Upgrade pre-activation chains without losing messages or cached
            # tool results that were committed after their compaction item.
            window = responses_store.current_window(request.session_id, state)
            if not any(item.get("type") == "compaction" and item.get("encrypted_content") for item in window):
                raise ResponsesProtocolError("Responses 压缩恢复窗口缺少密文，请重新同步本地上下文")
            window = [item for item in window if item.get("type") != "reasoning" or item.get("encrypted_content")]
            payload.pop("previous_response_id", None)
            payload["input"] = [*native_input_items(window), *delta_items]
    else:
        payload["input"] = convert_messages(source, request.session_id)
        new_ids = [m["context_item_id"] for m in source]
    if not payload.get("input"):
        raise ResponsesProtocolError("Responses 请求没有新的有效输入")
    if payload.get("previous_response_id") and state.get("omit_encrypted_include_with_previous"):
        payload["include"] = [item for item in payload.get("include", []) if item != "reasoning.encrypted_content"]
        if not payload["include"]:
            payload.pop("include")
    context["initialized"] = True
    return payload, new_ids, state


def committed(request, input_ids):
    context = request.responses_context
    if context is not None:
        context["seen_ids"] = list({*context.get("seen_ids", []), *input_ids})
        if set(context.get("deferred_ids") or []) <= set(input_ids):
            context.pop("deferred_ids", None)
            context.pop("deferred_items", None)


def pending_native_input(request, *, exclude_ids=()):
    source = _source(request)
    context = request.responses_context or {}
    excluded = set(exclude_ids)
    seen = set(context.get("seen_ids") or []) | excluded
    pending = [m for m in source if m["context_item_id"] not in seen and m.get("role") not in {"assistant", "system"}]
    queued_ids = set(context.get("deferred_ids") or [])
    pending = [m for m in pending if m["context_item_id"] not in queued_ids]
    queued_items = copy.deepcopy(context.get("deferred_items") or [])
    if queued_ids and queued_ids <= excluded:
        queued_items, queued_ids = [], set()
    elif queued_ids & excluded:
        raise ResponsesProtocolError("Responses 待回传批次与压缩边界不一致，请重新同步本地上下文")
    return ([*queued_items, *convert_messages(pending, request.session_id, delta=True)],
            [*queued_ids, *(m["context_item_id"] for m in pending)])
