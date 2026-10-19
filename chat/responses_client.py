"""Responses orchestration; HTTP transport uses the standard library."""
import asyncio
import copy
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
from datetime import timezone

from .protocols import responses_store
from .protocols.responses import endpoint, normalize_response, ResponsesStream, response_mode
from .protocols.responses_content import native_input_items
from .protocols.errors import ResponsesProtocolError, ResponsesUpstreamError
from .responses_transport import AsyncClient, SyncClient, Stopped


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _sse(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


def _client(config, request, *, asynchronous=False):
    parts = urlsplit(endpoint(config["url"]))
    headers = {"Authorization": "Bearer " + str(config.get("apiKey") or "not-needed")}
    for item in config.get("_custom_headers") or []:
        name, value = str(item.get("name") or "").strip(), str(item.get("value") or "")
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) and not any(c in name + value for c in "\r\n"):
            if name.casefold() not in {"host", "content-length", "connection", "transfer-encoding"}:
                for previous in list(headers):
                    if previous.casefold() == name.casefold():
                        headers.pop(previous)
                headers[name] = value
    if parts.hostname == "opencode.ai" and parts.path.startswith("/zen/go/"):
        if not any(key.casefold() == "x-opencode-session" for key in headers):
            headers["x-opencode-session"] = hashlib.sha256(str(request.session_id).encode()).hexdigest()
        if not any(key.casefold() == "user-agent" for key in headers):
            headers["User-Agent"] = "agent-tool-sse/1.0"
    cls = AsyncClient if asynchronous else SyncClient
    return cls(endpoint(config["url"]), headers, request), {}


def _kwargs(method, payload, query):
    return {**payload, **({"extra_query": query} if query else {})}


def _stop_client(client, stop):
    if hasattr(client, "set_stop"):
        client.set_stop(stop)


def _maximum():
    from env_manager import load_var
    try:
        return int(load_var("NETWORK_RETRY_MAX_ATTEMPTS", 3))
    except (TypeError, ValueError):
        return 3


def _retry_delay(exc, attempt):
    headers = getattr(exc, "response_headers", {}) or {}
    hint = headers.get("retry-after")
    if hint:
        try:
            delay = float(hint)
        except ValueError:
            try:
                date = parsedate_to_datetime(hint)
                delay = date.replace(tzinfo=timezone.utc).timestamp() - time.time() if date.tzinfo is None else date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = None
        if delay is not None:
            return min(30, max(0, delay))
    return min(2 ** (attempt - 1), 10)


def _failure(exc, attempt, maximum, retrying=False):
    status = getattr(exc, "status_code", None)
    detail = error_detail(exc)
    return {"error": detail, "error_type": "protocol" if isinstance(exc, ResponsesProtocolError) else
            "upstream_error_frame" if isinstance(exc, ResponsesUpstreamError) else "http" if status else "connect",
            "error_detail": detail, "step": "responses", "retry": attempt,
            "max_attempts": maximum if maximum > 0 else None, "retrying": retrying,
            "non_retryable": not retrying, "discard_tool_calls": True}


def error_detail(exc):
    detail = str(exc) or ("Responses 请求等待超时" if isinstance(exc, TimeoutError) else type(exc).__name__)
    status = getattr(exc, "status_code", None)
    if "<html" in detail.lower() or "<!doctype" in detail.lower():
        return f"HTTP {status or '错误'}：平台返回网页，未提供兼容的 Responses API 端点"
    return detail[:1800]


def _retrieval_unavailable(session_id, record, exc):
    responses_store.note_request_failure(session_id, record, "retrieve", exc, error_detail(exc))
    previous = next((failure["detail"] for failure in reversed(record.get("failures") or [])
                     if failure["operation"] == "stream"), None)
    detail = f"查询接口 HTTP {exc.status_code}"
    if previous:
        detail += "；此前断流：" + previous
    return ResponsesProtocolError(
        f"平台无法查询断流后的响应（{detail}），请在模型参数中重新同步上下文后继续；未确认工具不会执行")


def _retryable(exc):
    if isinstance(exc, ResponsesProtocolError):
        return False
    if isinstance(exc, ResponsesUpstreamError):
        return _upstream_retryable(exc)
    status = getattr(exc, "status_code", None)
    return status in {408, 409, 425, 429} or (status is not None and status >= 500) or status is None


# Deterministic upstream terminations: retrying the same frozen payload only
# reproduces them, so they must not consume the network retry budget. Anything
# else (server_error, rate limits, bare error frames) is transient like an
# HTTP 5xx and shares NETWORK_RETRY_MAX_ATTEMPTS with the transport errors.
_NON_RETRYABLE_UPSTREAM_CODES = frozenset({
    "invalid_request", "invalid_request_error",
    "authentication_error", "permission_error", "not_found_error",
})

_NON_RETRYABLE_UPSTREAM_HINTS = ("invalid", "auth", "permission", "not_found", "not found")


def _upstream_retryable(exc):
    response = exc.response or {}
    if not response:
        # Bare "error" event frame without a terminal response: retry by default
        # like the chat_completions provider error frame, except for request or
        # credential failures identifiable from the message text.
        detail = str(exc).lower()
        if re.search(r"\b40[134]\b", detail):
            return False
        return not any(hint in detail for hint in _NON_RETRYABLE_UPSTREAM_HINTS)
    if response.get("status") == "completed":
        # Only the malformed-tool-call check raises with a completed response
        # (normalize raises anything else non-completed as failed/incomplete).
        # Nothing was executed, so recreating the frozen request within the
        # network budget lets a one-off gateway glitch self-heal; a systematic
        # mismatch still terminates once the budget is exhausted, now with the
        # raw response persisted for forensics.
        return True
    if response.get("status") != "failed":
        # incomplete (max_output_tokens/content_filter) and cancelled are
        # deterministic model-side terminations, not transient faults.
        return False
    code = str(((response.get("error") or {}).get("code")) or "").lower()
    if not code:
        return True
    if code in _NON_RETRYABLE_UPSTREAM_CODES:
        return False
    return not any(hint in code for hint in _NON_RETRYABLE_UPSTREAM_HINTS)


def _restart_failed_request(request, record, exc, *, stateless):
    """Abandon a terminally failed request so the retry recreates it.

    Retrieving the same response ID only replays the terminal failure. The
    failed record never committed, so the chain head still points at the last
    success and the next loop iteration can begin a fresh request from the
    frozen payload. The caller must drop its record reference.
    """
    responses_store.abandon_request(request.session_id, record, exc)
    return ResponsesStream(require_stored=not stateless)


def _parent_missing(exc):
    status = getattr(exc, "status_code", None)
    detail = str(exc).lower()
    return status in {400, 404} and ("previous_response" in detail or "previous response" in detail or "referenced response" in detail) and any(
        text in detail for text in ("not found", "not_found", "does not exist", "expired", "不存在"))


def _include_conflict(payload, exc):
    detail = str(exc).lower()
    return (getattr(exc, "status_code", None) == 400 and payload.get("previous_response_id")
            and "reasoning.encrypted_content" in payload.get("include", [])
            and "reasoning.encrypted_content" in detail and "previous_response_id" in detail
            and any(text in detail for text in ("cannot be used", "not supported", "不能同时")))


def _adjust_include(request, payload, record, exc):
    responses_store.abandon_request(request.session_id, record, exc)
    state = responses_store.omit_encrypted_include_with_previous(request.session_id, record["scope"])
    payload = copy.deepcopy(payload)
    payload["include"] = [value for value in payload.get("include", []) if value != "reasoning.encrypted_content"]
    if not payload["include"]:
        payload.pop("include")
    return payload, state


def _stateless_interrupted(exc):
    return ResponsesProtocolError("无状态 Responses 响应中断，平台无法通过响应 ID 查询恢复；未完成响应中的工具不会执行，可再次发送继续。详情：" + error_detail(exc))


def _rebase(payload, session_id, state):
    window = responses_store.current_window(session_id, state)
    if not window:
        raise ResponsesProtocolError("远端响应已失效且缺少原生恢复窗口，请重新同步本地上下文")
    rebuilt = copy.deepcopy(payload)
    rebuilt.pop("previous_response_id", None)
    # An item ID without encrypted content cannot resurrect expired hidden
    # reasoning. Keep the visible/tool facts if a gateway omitted the cipher.
    window = [item for item in window if item.get("type") != "reasoning" or item.get("encrypted_content")]
    rebuilt["input"] = [*native_input_items(window), *payload["input"]]
    return rebuilt


def _commit(request, record, response):
    normalized = normalize_response(response, require_stored=record["payload"].get("store") is not False)
    reference = responses_store.commit_response(request.session_id, record, response)
    from factory.agent_runtime.responses_context import committed
    committed(request, record["input_ids"])
    normalized["provider_state"] = reference
    normalized["response_record"] = {
        "event": "model_response", "protocol": "responses", "scope": record["scope"],
        "request_uid": record["record_id"], "response_id": response["id"],
        "previous_response_id": record["payload"].get("previous_response_id"),
        "status": "completed", "record_ref": reference["record_id"], "committed": True}
    return normalized


def _precheck(payload, state, config):
    from factory.agent_runtime.chat_runtime import estimate_text_tokens
    maximum = config.get("maxInputTokens") or config.get("max_input_tokens")
    if not maximum:
        return
    # For a chained request, input alone is not the rendered context.
    estimated = None
    if payload.get("previous_response_id") and state.get("head_record_id"):
        head = responses_store.load_record(state["session_id"], {
            "protocol": "responses", "record_id": state["head_record_id"]})
        raw = (head or {}).get("response") or {}
        if not any(item.get("type") == "compaction" for item in raw.get("output") or []):
            usage = raw.get("usage") or {}
            if usage.get("input_tokens") is not None:
                # Remote usage includes hidden reasoning. Ciphertext length does
                # not measure its rendered context; never tokenize opaque bytes.
                estimated = int(usage["input_tokens"]) + int(usage.get("output_tokens") or 0)
                estimated += estimate_text_tokens([payload.get("input"), payload.get("instructions"), payload.get("tools")])
    else:
        window = payload.get("input") or []
        if not any(item.get("type") in {"reasoning", "compaction"} for item in window):
            estimated = estimate_text_tokens([window, payload.get("instructions"), payload.get("tools")])
    if estimated is not None:
        if estimated >= int(maximum):
            raise ResponsesProtocolError("Responses 远端上下文预估超出模型窗口，请压缩或重新同步")
        payload["max_output_tokens"] = min(payload["max_output_tokens"], int(maximum) - estimated)


def _prepare(request, config, stream):
    from factory.agent_runtime.responses_context import prepare
    payload, ids, state = prepare(request, config, stream)
    _precheck(payload, state, config)
    return payload, ids, state


def remote_compaction_needed(request, config, state, pending):
    """Remote chains can outgrow a small, already-summarized local context."""
    maximum = config.get("maxInputTokens") or config.get("max_input_tokens")
    if (not maximum or not state.get("head_record_id") or state.get("pending_compaction_ref")
            or responses_store.needs_compaction_activation(state) or state.get("pending_request_ref")):
        return False
    head = responses_store.load_record(request.session_id, {"protocol": "responses", "record_id": state["head_record_id"]})
    raw = (head or {}).get("response") or {}
    usage = raw.get("usage") or {}
    if usage.get("input_tokens") is None or any(item.get("type") == "compaction" for item in raw.get("output") or []):
        return False
    from factory.agent_runtime.chat_runtime import estimate_text_tokens
    from factory.agent_runtime.context_compaction import load_context_compaction_settings
    estimated = int(usage["input_tokens"]) + int(usage.get("output_tokens") or 0) + estimate_text_tokens(pending)
    ratio = load_context_compaction_settings().trigger_ratio
    return estimated >= int(maximum) * ratio


async def _recover_pending(transport, client, query, request, config, stop):
    scope = (request.responses_context or {}).get("scope") or "chat_model"
    pending = responses_store.pending_request(request.session_id, scope)
    if pending is None:
        return None
    if response_mode(request, config) == "stateless" or (pending.get("payload") or {}).get("store") is False:
        responses_store.abandon_request(request.session_id, pending, "无状态请求无法查询恢复")
        responses_store.invalidate(request.session_id, "无状态请求无法查询恢复，使用已确认的本地上下文", scope)
        return "上次未完成的响应不会执行工具，将从本地上下文继续"
    if pending.get("source_key") != responses_store.source_key(config):
        responses_store.invalidate(request.session_id, "账户或模型已变化", scope)
        return "已按当前模型准备重新建立会话"
    if not pending.get("response_id"):
        responses_store.abandon_request(request.session_id, pending, "上次请求未收到可查询 ID")
        responses_store.invalidate(request.session_id, "上次请求接受状态未知，按本地事实建立新链", scope)
        return "上次请求没有可查询 ID，将从本地上下文继续"
    try:
        response = _dump(await transport._wait_with_stop(
            client.responses.retrieve(pending["response_id"], **({"extra_query": query} if query else {})),
            request.timeout_read, stop))
    except Exception as exc:
        if getattr(exc, "status_code", None) in {404, 405}:
            raise _retrieval_unavailable(request.session_id, pending, exc) from exc
        raise
    return _finish_recovery(request, pending, response, scope)


def _finish_recovery(request, pending, response, scope):
    if response.get("status") in {"queued", "in_progress"}:
        raise ResponsesProtocolError("上次 Responses 请求仍在生成，请稍后重试或重新同步")
    if response.get("status") == "completed":
        normalized = _commit(request, pending, response)
        if normalized.get("tool_calls"):
            # A previous, interrupted generation must not start its old actions
            # just because a new user question has arrived.
            responses_store.invalidate(request.session_id, "上次未执行工具保留为历史，按当前问题重新规划", scope)
        return "已确认上次远端响应，恢复记录已保存"
    responses_store.abandon_request(request.session_id, pending, "上次远端请求未完成")
    responses_store.invalidate(request.session_id, "上次远端请求未完成，按本地事实恢复", scope)
    return "上次响应未完成，将从本地上下文继续"


async def async_stream(transport, request, config, stop=None):
    client, query = _client(config, request, asynchronous=True)
    _stop_client(client, stop)
    payload, ids, state, record = None, None, None, None
    stateless = response_mode(request, config) == "stateless"
    assembly = ResponsesStream(require_stored=not stateless)
    maximum, attempt, rebuilt = _maximum(), 0, False
    try:
        recovered = await _recover_pending(transport, client, query, request, config, stop)
        if recovered:
            yield _sse({"warning": {"code": "RESPONSES_RECOVERED", "message": recovered}})
        payload, ids, state = _prepare(request, config, True)
        while not transport._should_stop(stop):
            stream = None
            operation = "retrieve" if assembly.response_id else "create"
            try:
                if operation == "create":
                    from factory.agent_runtime.task_costs import network_attempt
                    await network_attempt()
                if record is None:
                    record = responses_store.begin_request(request.session_id, state["scope"], payload, config, ids, snapshot=state)
                if assembly.response_id:
                    try:
                        response = _dump(await transport._wait_with_stop(
                            client.responses.retrieve(assembly.response_id, **({"extra_query": query} if query else {})),
                            request.timeout_read, stop))
                    except Exception as exc:
                        if getattr(exc, "status_code", None) in {404, 405}:
                            raise _retrieval_unavailable(request.session_id, record, exc) from exc
                        raise
                    if response.get("status") in {"queued", "in_progress"}:
                        raise TimeoutError("Responses 响应仍在生成，稍后查询恢复")
                    events = assembly.feed({"type": "response.completed", "response": response})
                    for event in events:
                        if event.get("response_complete"):
                            final = _commit(request, record, response)
                            final.pop("content", None); final.pop("reasoning_content", None)
                            yield _sse(final)
                        else:
                            yield _sse(event)
                    yield "data: [DONE]\n\n"
                    return
                stream = await transport._wait_with_stop(
                    client.responses.create(**_kwargs(client.responses.create, payload, query)),
                    request.timeout_read, stop)
                operation = "stream"
                iterator = stream.__aiter__()
                while not transport._should_stop(stop):
                    try:
                        raw = await transport._wait_with_stop(iterator.__anext__(), request.timeout_read, stop)
                    except StopAsyncIteration:
                        break
                    wire = _dump(raw)
                    if wire.get("type") == "response.created":
                        response_id = (wire.get("response") or {}).get("id")
                        responses_store.note_response_id(request.session_id, record, response_id)
                    for event in assembly.feed(wire):
                        if event.get("response_complete"):
                            final = _commit(request, record, assembly.response)
                            final.pop("content", None); final.pop("reasoning_content", None)
                            yield _sse(final)
                        else:
                            yield _sse(event)
                    if assembly.complete:
                        yield "data: [DONE]\n\n"
                        return
                if transport._should_stop(stop):
                    raise asyncio.CancelledError()
                raise ConnectionError("Responses 流提前结束，正在确认远端响应")
            except asyncio.CancelledError:
                if record:
                    responses_store.abandon_request(request.session_id, record, "任务已停止", unknown=bool(assembly.response_id) and not stateless)
                raise
            except Exception as exc:
                if record and operation != "retrieve":
                    responses_store.note_request_failure(request.session_id, record, operation, exc, error_detail(exc))
                if not assembly.response_id and record and _include_conflict(payload, exc):
                    payload, state = _adjust_include(request, payload, record, exc)
                    record, assembly = None, ResponsesStream(require_stored=not stateless)
                    yield _sse({"warning": {"code": "RESPONSES_INCLUDE_COMPAT", "message": "有状态兼容：已移除平台拒绝的推理密文返回选项，保留响应 ID 继续；未切换为无状态"}})
                    continue
                if not rebuilt and payload.get("previous_response_id") and _parent_missing(exc):
                    responses_store.abandon_request(request.session_id, record, exc)
                    payload = _rebase(payload, request.session_id, state)
                    record, rebuilt, assembly = None, True, ResponsesStream()
                    yield _sse({"warning": {"code": "RESPONSES_REBUILT", "message": "有状态续接失败：本次回传本地原生上下文重建响应链，仍为有状态模式；若反复出现，请选择“无状态兼容”"}})
                    continue
                attempt += 1
                if (stateless and (assembly.response_id or assembly.content or assembly.reasoning)
                        and _retryable(exc) and not isinstance(exc, ResponsesUpstreamError)):
                    exc = _stateless_interrupted(exc)
                retrying = _retryable(exc) and (maximum <= 0 or attempt < maximum)
                if isinstance(exc, ResponsesUpstreamError) and exc.response:
                    record["terminal_response"] = exc.response
                if retrying and isinstance(exc, ResponsesUpstreamError):
                    assembly = _restart_failed_request(request, record, exc, stateless=stateless)
                    record = None
                elif not retrying and record:
                    responses_store.abandon_request(request.session_id, record, exc,
                                                    unknown=bool(assembly.response_id) and not stateless and not isinstance(exc, ResponsesUpstreamError))
                yield _sse(_failure(exc, attempt, maximum, retrying))
                if not retrying:
                    yield "data: [DONE]\n\n"
                    return
                # No complete response ID: retry the frozen request from its known
                # parent, never a modified partial prefix. Tools are not executed.
                await transport._wait_with_stop(asyncio.sleep(_retry_delay(exc, attempt)), None, stop)
            finally:
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass
    except asyncio.CancelledError:
        if not transport._should_stop(stop):
            raise
    except Exception as exc:
        yield _sse(_failure(exc, 1, maximum))
    finally:
        try:
            await client.close()
        except Exception:
            pass
    yield "data: [DONE]\n\n"


def _sync_call(request, config, *, stream, stop=None, transport=None):
    client, query = _client(config, request)
    _stop_client(client, stop)
    record = None
    stateless = response_mode(request, config) == "stateless"
    assembly = ResponsesStream(require_stored=not stateless)
    maximum, attempt, rebuilt = _maximum(), 0, False
    try:
        scope = (request.responses_context or {}).get("scope") or "chat_model"
        pending = responses_store.pending_request(request.session_id, scope)
        if pending:
            if stateless or (pending.get("payload") or {}).get("store") is False or pending.get("source_key") != responses_store.source_key(config) or not pending.get("response_id"):
                if stateless or (pending.get("payload") or {}).get("store") is False:
                    responses_store.abandon_request(request.session_id, pending, "无状态请求无法查询恢复")
                responses_store.invalidate(request.session_id, "上次请求无法查询，按当前本地事实恢复", scope)
            else:
                try:
                    recovered = _dump(client.responses.retrieve(pending["response_id"], **({"extra_query": query} if query else {})))
                except Exception as exc:
                    if getattr(exc, "status_code", None) in {404, 405}:
                        raise _retrieval_unavailable(request.session_id, pending, exc) from exc
                    raise
                _finish_recovery(request, pending, recovered, scope)
        payload, ids, state = _prepare(request, config, stream)
        while not (stop and stop()):
            upstream = None
            operation = "retrieve" if assembly.response_id else "create"
            try:
                if operation == "create":
                    from factory.agent_runtime.task_costs import network_attempt_sync
                    network_attempt_sync()
                if record is None:
                    record = responses_store.begin_request(request.session_id, state["scope"], payload, config, ids, snapshot=state)
                if assembly.response_id:
                    try:
                        response = _dump(client.responses.retrieve(assembly.response_id, **({"extra_query": query} if query else {})))
                    except Exception as exc:
                        if getattr(exc, "status_code", None) in {404, 405}:
                            raise _retrieval_unavailable(request.session_id, record, exc) from exc
                        raise
                    if response.get("status") in {"queued", "in_progress"}:
                        raise TimeoutError("Responses 响应仍在生成")
                elif stream:
                    upstream = client.responses.create(**_kwargs(client.responses.create, payload, query))
                    operation = "stream"
                    for raw in upstream:
                        if stop and stop():
                            responses_store.abandon_request(request.session_id, record, "任务已停止", unknown=bool(assembly.response_id) and not stateless)
                            return
                        wire = _dump(raw)
                        if wire.get("type") == "response.created":
                            responses_store.note_response_id(request.session_id, record, (wire.get("response") or {}).get("id"))
                        for event in assembly.feed(wire):
                            if not event.get("response_complete"):
                                yield event
                        if assembly.complete:
                            break
                    if not assembly.complete:
                        raise ConnectionError("Responses 流提前结束")
                    response = assembly.response
                else:
                    response = _dump(client.responses.create(**_kwargs(client.responses.create, payload, query)))
                    responses_store.note_response_id(request.session_id, record, response.get("id"))
                if stop and stop():
                    responses_store.abandon_request(request.session_id, record, "任务已停止", unknown=not stateless)
                    return
                recovery_events = []
                if stream:
                    if not assembly.complete:
                        recovery_events = assembly.feed({"type": "response.completed", "response": response})
                normalized = _commit(request, record, response)
                if stream:
                    for event in recovery_events:
                        if not event.get("response_complete"):
                            yield event
                    normalized.pop("content", None); normalized.pop("reasoning_content", None)
                yield normalized
                return
            except Stopped:
                if record:
                    responses_store.abandon_request(request.session_id, record, "任务已停止", unknown=not stateless)
                return
            except Exception as exc:
                if record and operation != "retrieve":
                    responses_store.note_request_failure(request.session_id, record, operation, exc, error_detail(exc))
                if not assembly.response_id and record and _include_conflict(payload, exc):
                    payload, state = _adjust_include(request, payload, record, exc)
                    record, assembly = None, ResponsesStream(require_stored=not stateless)
                    continue
                if not rebuilt and payload.get("previous_response_id") and _parent_missing(exc):
                    responses_store.abandon_request(request.session_id, record, exc)
                    payload = _rebase(payload, request.session_id, state)
                    record, rebuilt, assembly = None, True, ResponsesStream()
                    continue
                attempt += 1
                if (stateless and (assembly.response_id or assembly.content or assembly.reasoning)
                        and _retryable(exc) and not isinstance(exc, ResponsesUpstreamError)):
                    exc = _stateless_interrupted(exc)
                retrying = _retryable(exc) and (maximum <= 0 or attempt < maximum)
                if isinstance(exc, ResponsesUpstreamError) and exc.response:
                    record["terminal_response"] = exc.response
                if retrying and isinstance(exc, ResponsesUpstreamError):
                    assembly = _restart_failed_request(request, record, exc, stateless=stateless)
                    record = None
                elif not retrying and record:
                    responses_store.abandon_request(request.session_id, record, exc,
                                                    unknown=bool(assembly.response_id) and not stateless and not isinstance(exc, ResponsesUpstreamError))
                yield _failure(exc, attempt, maximum, retrying)
                if not retrying:
                    return
                deadline = time.monotonic() + _retry_delay(exc, attempt)
                while time.monotonic() < deadline:
                    if stop and stop():
                        return
                    time.sleep(0.1)
            finally:
                if upstream is not None:
                    try:
                        upstream.close()
                    except Exception:
                        pass
    except Exception as exc:
        yield _failure(exc, 1, maximum)
    finally:
        try:
            client.close()
        except Exception:
            pass


def sync_stream(transport, request, config, stop=None):
    for event in _sync_call(request, config, stream=True, stop=stop, transport=transport):
        yield _sse(event)
    yield "data: [DONE]\n\n"


def nonstream(transport, request, config, stop=None):
    result = None
    for event in _sync_call(request, config, stream=False, stop=stop, transport=transport):
        result = event
    return result or {"error": "Responses 请求已停止"}


async def compact(request, config, *, scope="chat_model", stop=None):
    if response_mode(request, config) == "stateless":
        raise ResponsesProtocolError("无状态 Responses 使用本地上下文压缩，无需压缩远端响应链")
    from chat.chat_llm import ChatLLM
    state = responses_store.load_state(request.session_id, scope)
    if state.get("pending_request_ref"):
        raise ResponsesProtocolError("请求尚未确认，不能压缩 Responses 状态")
    from factory.agent_runtime.responses_context import pending_native_input
    pending, boundary_ids = pending_native_input(request) if request.messages else ([], [])
    reusable = responses_store.reusable_compaction_ref(state, has_pending_input=bool(pending))
    if reusable:
        return reusable
    window = responses_store.current_window(request.session_id, state)
    if not window:
        raise ResponsesProtocolError("尚无 Responses 原生会话，请先进行一次对话")
    window.extend(pending)
    client, query = _client(config, request, asynchronous=True)
    _stop_client(client, stop)
    try:
        if state.get("compact_strategy") == "trigger":
            return await _compact_trigger(client, query, request, config, state, pending, boundary_ids, stop)
        try:
            instructions = _compaction_instructions(request, state)
            from factory.agent_runtime.task_costs import network_attempt
            await network_attempt()
            response = await ChatLLM._wait_with_stop(client.responses.compact(
                model=config["selected_model_id"], input=native_input_items(window),
                **({"instructions": instructions} if instructions else {}),
                **({"extra_query": query} if query else {})), request.timeout_read, stop)
        except Exception as exc:
            if getattr(exc, "status_code", None) not in {404, 405}:
                raise
            return await _compact_trigger(client, query, request, config, state, pending, boundary_ids, stop)
        if stop and stop():
            raise asyncio.CancelledError()
        return responses_store.save_compaction(request.session_id, scope, _dump(response), state,
                                              boundary_ids=boundary_ids)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        detail = error_detail(exc)
        responses_store.compaction_failed(request.session_id, scope, detail)
        raise ResponsesProtocolError(detail) from exc
    finally:
        await client.close()


async def _compact_trigger(client, query, request, config, state, pending, ids, stop):
    """Official manual server-side compaction for gateways lacking /compact."""
    scope = state["scope"]
    maximum = config.get("maxOutputTokens") or config.get("max_output_tokens")
    # The tested gateway/upstream requires a 20k budget for this control item;
    # it is a ceiling, not the amount of tokens necessarily generated.
    if maximum and int(maximum) < 20000:
        raise ResponsesProtocolError("该模型输出上限不足以使用 compaction_trigger，请使用独立 compact 或本地重新同步")
    payload = {"model": config["selected_model_id"], "store": True,
               "previous_response_id": state["head_response_id"],
               "input": [*pending, {"type": "compaction_trigger"}],
               "max_output_tokens": 20000, "stream": False}
    if request.reasoning:
        payload["reasoning"] = copy.deepcopy(request.reasoning)
    instructions = _compaction_instructions(request, state)
    if instructions:
        payload["instructions"] = instructions
    record = responses_store.begin_request(request.session_id, scope, payload, config, ids,
                                          snapshot=state, compaction=True)
    try:
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.task_costs import network_attempt
        await network_attempt()
        raw = _dump(await ChatLLM._wait_with_stop(
            client.responses.create(**_kwargs(client.responses.create, payload, query)), request.timeout_read, stop))
        responses_store.note_response_id(request.session_id, record, raw.get("id"))
        if stop and stop():
            raise asyncio.CancelledError()
        normalized = normalize_response(raw)
        if normalized.get("tool_calls") or not any(item.get("type") == "compaction" and item.get("encrypted_content") for item in raw.get("output") or []):
            raise ResponsesProtocolError("平台没有执行 compaction_trigger，原会话未替换")
        reference = responses_store.commit_response(request.session_id, record, raw)
        from factory.agent_runtime.responses_context import committed
        committed(request, ids)
        return reference["record_id"]
    except asyncio.CancelledError:
        responses_store.abandon_request(request.session_id, record, "压缩已停止", unknown=True)
        raise
    except Exception as exc:
        responses_store.abandon_request(request.session_id, record, error_detail(exc),
                                        unknown=bool(record.get("response_id")) and not isinstance(exc, ResponsesProtocolError))
        raise


def _compaction_instructions(request, state):
    values = []
    if request.messages and request.messages[0].role == "system":
        content = request.messages[0].content
        values.append(content if isinstance(content, str) else
                      "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict)))
    if request.instructions:
        values.append(request.instructions)
    if not values and state.get("head_record_id"):
        head = responses_store.load_record(request.session_id, {
            "protocol": "responses", "record_id": state["head_record_id"]})
        previous = responses_store._read(responses_store._record_path(request.session_id, head["request_ref"])) if head else None
        return ((previous or {}).get("payload") or {}).get("instructions")
    return "\n\n".join(values)
