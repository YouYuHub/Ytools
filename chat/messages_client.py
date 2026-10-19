"""Messages transport using ChatLLM's existing socket, cancellation and body readers."""
import asyncio
import copy
import hashlib
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from util.media_limits import learn_media_limit, limit_media_messages

from .protocols import native_store
from .protocols.messages import (
    MessagesProtocolError, MessagesUpstreamError, MessagesStream, SSEFrames,
    build_payload, endpoint, headers, normalize_response,
)
from .protocols.messages_content import validate_payload


class HTTPFailure(RuntimeError):
    def __init__(self, status, detail, response_headers):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.response_headers = response_headers


class EmptyResponse(RuntimeError):
    pass


class EmptyTurn(EmptyResponse):
    """An end_turn without output needs a new continuation prompt."""


def _recover_media_limit(exc, prepared, config, *, partial=False):
    """Correct explicit count errors only before output and outside native replay."""
    if partial or prepared is None:
        return None
    if not ((isinstance(exc, HTTPFailure) and exc.status == 400)
            or (isinstance(exc, MessagesUpstreamError) and exc.error_type not in {
                'authentication_error', 'permission_error', 'not_found_error'})):
        return None
    payload, parent, _ = prepared
    if not learn_media_limit(str(exc), payload['messages'], config):
        return None
    if parent is not None or any(
        block.get('type') in {'thinking', 'redacted_thinking'}
        for message in payload['messages'] for block in message.get('content', [])
        if isinstance(block, dict)
    ):
        raise MessagesProtocolError('供应商媒体配额低于当前原生回放的附件数量；不能修改签名绑定的历史，请压缩历史或重建上下文后重试')
    corrected = copy.deepcopy(payload)
    corrected['messages'], dropped = limit_media_messages(payload['messages'], config)
    return (corrected, None, None) if dropped else None


def _media_warning():
    return _sse({'warning': {'code': 'MEDIA_LIMIT_DOWNGRADE', 'message':
        '供应商限制媒体类型或数量，已按限制降级并重新请求；未发送内容会明确告知模型。'}})


def _wire(config, payload, session_id=None):
    # Also check resumed requests, which include partial text added after errors.
    validate_payload(payload, config)
    url = urlsplit(endpoint(config["url"]))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    port = url.port or (443 if url.scheme == "https" else 80)
    host_header = url.netloc
    path = url.path + ("?" + url.query if url.query else "")
    request_headers = headers(config)
    # OpenCode Go asks API clients to identify themselves and provide a stable
    # per-conversation ID for routing and prompt caching. Keep custom values
    # from the model configuration authoritative when present.
    if (url.hostname or "").casefold() == "opencode.ai" and url.path.startswith("/zen/go/"):
        lower_names = {name.casefold() for name in request_headers}
        if "user-agent" not in lower_names:
            request_headers["User-Agent"] = "agent-tool-sse/1.0"
        if session_id and "x-opencode-session" not in lower_names:
            stable_id = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()
            request_headers["x-opencode-session"] = stable_id
    lines = [f"POST {path} HTTP/1.1", f"Host: {host_header}"]
    lines.extend(f"{key}: {value}" for key, value in request_headers.items())
    lines.extend([f"Content-Length: {len(body)}", "Connection: close", "", ""])
    return url, port, "\r\n".join(lines).encode("utf-8") + body


def _retryable(exc):
    if isinstance(exc, MessagesProtocolError):
        return False
    if isinstance(exc, HTTPFailure):
        return exc.status in {408, 409, 425, 429} or exc.status >= 500
    if isinstance(exc, MessagesUpstreamError):
        return exc.error_type not in {"invalid_request_error", "authentication_error", "permission_error", "not_found_error"}
    return isinstance(exc, (OSError, TimeoutError, EmptyResponse, RuntimeError))


def _retry_delay(exc):
    if isinstance(exc, HTTPFailure):
        try:
            return min(60.0, max(1.0, float(exc.response_headers.get("retry-after", 1))))
        except (TypeError, ValueError):
            pass
    return 1.0


def _failure(exc, attempt, maximum, step, partial=False):
    retrying = _retryable(exc) and (maximum <= 0 or attempt <= maximum)
    error_type = "request"
    if isinstance(exc, HTTPFailure):
        error_type = "http"
    elif isinstance(exc, MessagesUpstreamError):
        error_type = "upstream_error_frame"
    elif isinstance(exc, EmptyResponse):
        error_type = "empty_response"
    elif isinstance(exc, TimeoutError):
        error_type = "timeout"
    elif isinstance(exc, OSError):
        error_type = "connect"
    elif isinstance(exc, MessagesProtocolError):
        error_type = "protocol"
    detail = str(exc) or (f"Messages 请求在 {step} 阶段超时" if isinstance(exc, TimeoutError) else type(exc).__name__)
    return {"error": detail, "error_type": error_type, "step": step, "error_detail": detail,
            "retry": attempt, "max_attempts": maximum if maximum > 0 else None,
            "retrying": retrying, "non_retryable": not _retryable(exc),
            "discard_tool_calls": True, "resume_from_partial": partial}


def _sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _continue_payload(payload, content):
    resumed = copy.deepcopy(payload)
    if content:
        resumed["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "".join(content)}]})
    resumed["messages"].append({"role": "user", "content": [{"type": "text", "text":
        "上一次响应因网络或供应商流式错误中断。请从已输出正文处继续，避免重复；未完成的工具调用没有执行，请重新生成完整调用。"}]})
    return resumed


def _continue_empty_turn(prepared):
    payload, parent, appended = prepared
    resumed = copy.deepcopy(payload)
    prompt = {'role': 'user', 'content': [{'type': 'text', 'text':
        '上一轮没有返回正文。请继续完成用户请求，基于已有工具结果给出答复；已完成的工具无需重复执行。'}]}
    resumed['messages'].append(prompt)
    # Keep the exact continuation in the sidecar if another native tool turn follows.
    return resumed, parent, (copy.deepcopy(appended or []) + [prompt] if parent else None)


def _commit(request, config, payload, state, parent, appended):
    normalized = normalize_response(state)
    if state.get('stop_reason') == 'end_turn' and not any(
        normalized.get(key) for key in ('content', 'reasoning_content', 'tool_calls')
    ):
        raise EmptyTurn('API 200 响应正常结束但没有任何模型输出')
    if normalized["replay_eligible"]:
        try:
            normalized["provider_state"] = native_store.save_record(
                request.session_id, config, payload, state, parent=parent, appended=appended)
        except OSError as exc:
            raise MessagesProtocolError(f"无法保存 Messages 原生回放记录，本轮工具调用未提交：{exc}") from exc
    return normalized


def _prepare(transport, request, config, stream):
    payload, parent, appended = build_payload(request, config, stream)
    _precheck(transport, payload, config)
    return payload, parent, appended


def _precheck(transport, payload, config=None):
    error = transport._precheck_hard_limit(payload)
    if error:
        raise MessagesProtocolError(error)
    window = (config or {}).get("maxInputTokens") or (config or {}).get("max_input_tokens")
    if window:
        from .chat_llm import _estimate_payload_input_tokens
        estimated = _estimate_payload_input_tokens(payload)
        if estimated >= int(window):
            raise MessagesProtocolError(f"Messages 实际回放上下文约 {estimated} tokens，超过模型窗口 {window}，请压缩历史后重试")
        payload["max_tokens"] = min(payload["max_tokens"], int(window) - estimated)
    thinking = payload.get("thinking") or {}
    if thinking.get("type") == "enabled" and thinking.get("budget_tokens", 0) >= payload["max_tokens"]:
        raise MessagesProtocolError("请求预算降额后不足以容纳 thinking budget_tokens；请缩减上下文或思考预算")


def _sync_head(file, transport):
    from .chat_llm import _http_status_code
    while True:
        status_line = file.readline()
        if not status_line:
            raise OSError("连接在 HTTP 状态行前关闭")
        status = _http_status_code(
            status_line.decode("utf-8", errors="replace").strip())
        values = {}
        while True:
            line = file.readline()
            if not line:
                raise OSError("连接在 HTTP 响应头中途关闭")
            if not line.strip():
                break
            name, separator, value = line.decode("utf-8", errors="replace").partition(":")
            if separator:
                values[name.strip().lower()] = value.strip()
        if not 100 <= status < 200:
            return status, values


async def _async_head(reader, transport, timeout, stop):
    from .chat_llm import _http_status_code
    while True:
        status_line = await transport._wait_with_stop(reader.readline(), timeout, stop)
        if not status_line:
            raise OSError("连接在 HTTP 状态行前关闭")
        status = _http_status_code(
            status_line.decode("utf-8", errors="replace").strip())
        values = {}
        while True:
            line = await transport._wait_with_stop(reader.readline(), timeout, stop)
            if not line:
                raise OSError("连接在 HTTP 响应头中途关闭")
            if not line.strip():
                break
            name, separator, value = line.decode("utf-8", errors="replace").partition(":")
            if separator:
                values[name.strip().lower()] = value.strip()
        if not 100 <= status < 200:
            return status, values


def _retry_limit():
    from .chat_llm import _resolve_network_retry_max_attempts
    return _resolve_network_retry_max_attempts()


def sync_stream(transport, request, config, stop=None):
    connect, read, drain = transport._resolve_timeouts(request, 300, 1800, 120)
    maximum, attempt, delay = _retry_limit(), 0, 1.0
    prepared = None
    while not transport._should_stop(stop):
        content, reasoning, output = [], [], False
        state, step = MessagesStream(), "precheck"
        try:
            if prepared is None:
                prepared = _prepare(transport, request, config, True)
            payload, parent, appended = prepared
            _precheck(transport, payload, config)
            url, port, wire = _wire(config, payload, request.session_id)
            step = "connect"
            from factory.agent_runtime.task_costs import network_attempt_sync
            network_attempt_sync()
            sock = socket.create_connection((url.hostname, port), timeout=connect)
            try:
                if url.scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=url.hostname)
                sock.settimeout(drain)
                step = "send_request"
                sock.sendall(wire)
                sock.settimeout(read)
                with sock.makefile("rb") as file:
                    step = "read_status"
                    status, response_headers = _sync_head(file, transport)
                    chunked = "chunked" in response_headers.get("transfer-encoding", "").lower()
                    if not 200 <= status < 300:
                        body = transport._read_full_response_body(file, chunked).decode("utf-8", errors="replace")
                        raise HTTPFailure(status, body or "[空响应体]", response_headers)
                    step = "read_sse_stream"
                    frames = SSEFrames()
                    for line in transport._iter_sse_body_lines(file, chunked):
                        if transport._should_stop(stop):
                            yield "data: [DONE]\n\n"
                            return
                        frame = frames.feed(line)
                        if frame is None:
                            continue
                        for event in state.feed(*frame):
                            if event.get("response_complete"):
                                final = _commit(request, config, payload, state.response, parent, appended)
                                final.pop("content", None)
                                final.pop("reasoning_content", None)
                                yield _sse(final)
                            else:
                                if event.get("content"):
                                    content.append(event["content"]); output = True
                                if event.get("reasoning_content"):
                                    reasoning.append(event["reasoning_content"]); output = True
                                if event.get("tool_call_preview"):
                                    output = True
                                event["id"] = state.response.get("id")
                                yield _sse(event)
                        if state.complete:
                            yield "data: [DONE]\n\n"
                            return
                    if output:
                        yield _sse({"stream_truncated": True, "discard_tool_calls": True})
                        yield "data: [DONE]\n\n"
                        return
                    raise EmptyResponse("API 200 空响应或流未完成")
            finally:
                sock.close()
        except Exception as exc:
            try:
                corrected = _recover_media_limit(exc, prepared, config, partial=output)
            except MessagesProtocolError as repair_error:
                exc, corrected = repair_error, None
            if corrected is not None:
                prepared = corrected
                yield _media_warning()
                continue
            attempt += 1
            failure = _failure(exc, attempt, maximum, step, output)
            yield _sse(failure)
            if not failure["retrying"]:
                break
            if isinstance(exc, EmptyTurn):
                prepared = _continue_empty_turn(prepared)
            elif output:
                prepared = (_continue_payload(payload, content), None, None)
            delay = _retry_delay(exc)
            end = time.monotonic() + delay
            while time.monotonic() < end and not transport._should_stop(stop):
                time.sleep(min(.1, max(0, end - time.monotonic())))
    yield "data: [DONE]\n\n"


async def async_stream(transport, request, config, stop=None):
    connect, read, drain = transport._resolve_timeouts(request, 300, 1800, 120)
    maximum, attempt = _retry_limit(), 0
    prepared = None
    while not transport._should_stop(stop):
        content, output = [], False
        state, step, writer = MessagesStream(), "precheck", None
        try:
            if prepared is None:
                prepared = _prepare(transport, request, config, True)
            payload, parent, appended = prepared
            _precheck(transport, payload, config)
            url, port, wire = _wire(config, payload, request.session_id)
            step = "connect"
            reader, writer = await transport._open_connection(url.hostname, port, url.scheme == "https", connect, stop)
            step = "send_request"
            writer.write(wire)
            await transport._wait_with_stop(writer.drain(), drain, stop)
            step = "read_status"
            status, response_headers = await _async_head(reader, transport, read, stop)
            chunked = "chunked" in response_headers.get("transfer-encoding", "").lower()
            if not 200 <= status < 300:
                body = await transport._aread_full_response_body(reader, chunked, read, stop)
                raise HTTPFailure(status, body.decode("utf-8", errors="replace") or "[空响应体]", response_headers)
            step = "read_sse_stream"
            frames = SSEFrames()
            async for line in transport._aiter_sse_body_lines(reader, chunked, read, stop):
                frame = frames.feed(line)
                if frame is None:
                    continue
                for event in state.feed(*frame):
                    if event.get("response_complete"):
                        final = _commit(request, config, payload, state.response, parent, appended)
                        final.pop("content", None)
                        final.pop("reasoning_content", None)
                        yield _sse(final)
                    else:
                        if event.get("content"):
                            content.append(event["content"]); output = True
                        if event.get("reasoning_content") or event.get("tool_call_preview"):
                            output = True
                        event["id"] = state.response.get("id")
                        yield _sse(event)
                if state.complete:
                    yield "data: [DONE]\n\n"
                    return
            if output:
                yield _sse({"stream_truncated": True, "discard_tool_calls": True})
                yield "data: [DONE]\n\n"
                return
            raise EmptyResponse("API 200 空响应或流未完成")
        except asyncio.CancelledError:
            if not transport._should_stop(stop):
                # asyncio.wait_for uses cancellation to enforce the tool stream
                # watchdog. Let it observe a timeout rather than a fake DONE.
                raise
            yield "data: [DONE]\n\n"
            return
        except Exception as exc:
            try:
                corrected = _recover_media_limit(exc, prepared, config, partial=output)
            except MessagesProtocolError as repair_error:
                exc, corrected = repair_error, None
            if corrected is not None:
                prepared = corrected
                yield _media_warning()
                continue
            attempt += 1
            failure = _failure(exc, attempt, maximum, step, output)
            yield _sse(failure)
            if not failure["retrying"]:
                break
            if isinstance(exc, EmptyTurn):
                prepared = _continue_empty_turn(prepared)
            elif output:
                prepared = (_continue_payload(payload, content), None, None)
            # Cancellation polling is shared with the existing Chat transport.
            try:
                await transport._wait_with_stop(asyncio.sleep(_retry_delay(exc)), None, stop)
            except asyncio.CancelledError:
                if not transport._should_stop(stop):
                    raise
                yield "data: [DONE]\n\n"
                return
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
    yield "data: [DONE]\n\n"


def nonstream(transport, request, config, stop=None):
    connect, read, drain = transport._resolve_timeouts(request, 300, 1800, 120)
    maximum, attempt = _retry_limit(), 0
    prepared = None
    while not transport._should_stop(stop):
        step = "precheck"
        try:
            if prepared is None:
                prepared = _prepare(transport, request, config, False)
            payload, parent, appended = prepared
            _precheck(transport, payload, config)
            url, port, wire = _wire(config, payload, request.session_id)
            step = "connect"
            from factory.agent_runtime.task_costs import network_attempt_sync
            network_attempt_sync()
            sock = socket.create_connection((url.hostname, port), timeout=connect)
            try:
                if url.scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=url.hostname)
                step = "send_request"
                sock.settimeout(drain); sock.sendall(wire); sock.settimeout(read)
                with sock.makefile("rb") as file:
                    step = "read_status"
                    status, response_headers = _sync_head(file, transport)
                    chunked = "chunked" in response_headers.get("transfer-encoding", "").lower()
                    step = "read_body"
                    raw = transport._read_full_response_body(file, chunked).decode("utf-8", errors="replace")
                    if not 200 <= status < 300:
                        raise HTTPFailure(status, raw or "[空响应体]", response_headers)
                    if not raw:
                        raise EmptyResponse("API 200 空响应")
                    try:
                        response = json.loads(raw)
                    except ValueError as exc:
                        raise MessagesUpstreamError("供应商返回非 JSON 响应") from exc
                    if response.get("type") == "error" or response.get("error"):
                        error = response.get("error") or response
                        raise MessagesUpstreamError(str(error), error.get("type", "api_error"))
                    if not response.get("stop_reason"):
                        raise EmptyResponse("Messages 响应缺少结束状态")
                    if response.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
                        response["content"] = [block for block in response.get("content", []) if block["type"] != "tool_use"]
                    return _commit(request, config, payload, response, parent, appended)
            finally:
                sock.close()
        except Exception as exc:
            try:
                corrected = _recover_media_limit(exc, prepared, config)
            except MessagesProtocolError as repair_error:
                exc, corrected = repair_error, None
            if corrected is not None:
                prepared = corrected
                continue
            attempt += 1
            failure = _failure(exc, attempt, maximum, step)
            if not failure["retrying"]:
                raise RuntimeError(failure["error"]) from exc
            if isinstance(exc, EmptyTurn):
                prepared = _continue_empty_turn(prepared)
            end = time.monotonic() + _retry_delay(exc)
            while time.monotonic() < end and not transport._should_stop(stop):
                time.sleep(min(.1, max(0, end - time.monotonic())))
    raise RuntimeError("模型调用已停止")
