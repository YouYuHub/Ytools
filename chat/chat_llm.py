# from email.mime import base
# from tkinter.filedialog import Open
# 标准库
# from typing import Generator, Any
import json
# import urllib.request
# import urllib.error
import urllib.parse
import socket
import ssl
from dataclasses import dataclass #, field
from typing import(
    List,
    Dict,
    Generator,
    AsyncGenerator,
    Any,
    Optional,
    Union,
)
import asyncio
from contextlib import suppress



# 自己的模块
try:
    from env_manager import ChatModelConfigurationError, require_default_chat_config
except ImportError:
    ChatModelConfigurationError = RuntimeError
    require_default_chat_config = None
# 导入配置模型
from config import ChatLLMRequest


@dataclass
class StreamChunk:
    """流式响应的数据块"""
    content: Optional[str] = None  # 正常回复内容
    reasoning_content: Optional[str] = None  # 推理过程内容
    tool_calls: Optional[List[Dict[str, Any]]] = None  # 工具调用列表
    usage: Optional[Dict[str, Any]] = None  # token 使用统计
    
    def __bool__(self):
        """判断是否有有效内容"""
        return any((
            self.content is not None and self.content != "",
            self.reasoning_content is not None and self.reasoning_content != "",
            self.tool_calls is not None and len(self.tool_calls) > 0,
            self.usage is not None,
        ))


class ChatLLM:
    """聊天客户端，支持流式输出"""
    @staticmethod
    def _should_stop(stop_checker: Optional[Any]) -> bool:
        if stop_checker is None:
            return False
        try:
            return bool(stop_checker())
        except Exception:
            return False

    @staticmethod
    async def _wait_with_stop(
        awaitable,
        timeout: Optional[float],
        stop_checker: Optional[Any] = None,
        check_interval: float = 0.5,
    ):
        task = asyncio.create_task(awaitable)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            while True:
                if ChatLLM._should_stop(stop_checker):
                    raise asyncio.CancelledError("stopped by client")
                if deadline is None:
                    wait_timeout = check_interval
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    wait_timeout = min(check_interval, remaining)
                done, _ = await asyncio.wait({task}, timeout=wait_timeout)
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def should_include_reasoning_effort(model_name: str) -> bool:
        """
        判断是否应该在请求中包含 reasoning_effort 参数
        该参数仅适用于 OpenAI o系列、GPT-5系列和部分DeepSeek模型
        参数:
            model_name: 模型名称
        返回:
            True 如果应该包含 reasoning_effort 参数，否则 False
        """
        if not model_name:
            return False
        model_lower = model_name.lower()
        # GPT-5系列（所有以 gpt-5 开头的模型都支持）
        if model_lower.startswith("gpt-5"):
            return True
        # o系列（所有以 o 开头的模型都支持，如 o1, o3, o4-mini 等）
        if model_lower.startswith("gpt-o"):
            return True
        # DeepSeek 支持 reasoning_effort 的模型
        deepseek_reasoning_models = set((
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            # "deepseek-coder".0
        ))
        # if any(dm in model_lower for dm in deepseek_reasoning_models):
        if model_lower in deepseek_reasoning_models:
            return True
        # # 开源 GPT-OSS 模型
        # if "gpt-oss" in model_lower:
        #     return True
        return False

    @staticmethod
    def _resolve_active_chat_config() -> dict[str, Any]:
        if require_default_chat_config is None:
            raise ChatModelConfigurationError("无法加载当前聊天模型配置")
        chat_config = require_default_chat_config()
        api_url = chat_config.get("url")
        model_name = chat_config.get("selected_model_name")
        print(f"[INFO] model: {model_name}")
        api_type = str(chat_config.get("apiType") or "chat-completions").strip().casefold()
        if not isinstance(api_url, str) or not api_url.strip():
            raise ChatModelConfigurationError("已选择的模型缺少 url 配置")
        parsed_url = urllib.parse.urlparse(api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ChatModelConfigurationError("已选择的模型 url 必须是有效的 http 或 https 地址")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ChatModelConfigurationError("已选择的模型缺少 id 或 name 配置")
        if api_type != "chat-completions":
            raise ChatModelConfigurationError(
                f"当前聊天客户端仅支持 apiType=chat-completions，已选择模型配置为 {api_type!r}"
            )
        return chat_config

    @staticmethod
    def confirm_completions_url(api_url: str) -> str:
        base = api_url.rstrip('/')
        if base.endswith('/chat/completions'):
            return base
        if base.endswith('/v1'):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    @staticmethod
    def _serialize_messages(messages: Optional[List[Any]]) -> List[Dict[str, Any]]:
        serialized_messages: List[Dict[str, Any]] = []
        for msg in messages or []:
            if hasattr(msg, "model_dump"):
                data = msg.model_dump()
            elif hasattr(msg, "dict"):
                data = msg.dict()
            else:
                data = msg
            if isinstance(data, dict):
                serialized_messages.append({
                    k: v for k, v in data.items()
                    if v is not None or k == "content"
                })
            else:
                serialized_messages.append({"content": data})
        return serialized_messages

    @staticmethod
    def _build_request_payload(
        request: ChatLLMRequest,
        stream: bool,
        model_name: str,
    ) -> Dict[str, Any]:
        if request is None:
            raise ValueError("request 参数不能为 None")
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": ChatLLM._serialize_messages(request.messages),
            "stream": stream,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "presence_penalty": request.presence_penalty,
        }
        if request.reasoning_effort and ChatLLM.should_include_reasoning_effort(model_name):
            payload["reasoning_effort"] = request.reasoning_effort
        if request.tools is not None:
            serialized_tools: List[Dict[str, Any]] = []
            for tool in request.tools:
                if hasattr(tool, "model_dump"):
                    serialized_tools.append(tool.model_dump(exclude_none=True))
                elif hasattr(tool, "dict"):
                    serialized_tools.append(tool.dict(exclude_none=True))
                else:
                    serialized_tools.append(tool)
            payload["tools"] = serialized_tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.extra_body:
            reserved_keys = {"model", "messages", "stream", "tools", "tool_choice", "parallel_tool_calls"}
            payload.update({
                key: value
                for key, value in request.extra_body.items()
                if key not in reserved_keys
            })
        return payload

    @staticmethod
    def _resolve_timeouts(request: Optional[ChatLLMRequest], timeout_connect: int, timeout_read: int, timeout_drain: int):
        if request is None:
            return timeout_connect, timeout_read, timeout_drain
        return (
            request.timeout_connect if request.timeout_connect is not None else timeout_connect,
            request.timeout_read if request.timeout_read is not None else timeout_read,
            request.timeout_drain if request.timeout_drain is not None else timeout_drain,
        )

    @staticmethod
    async def _open_connection(
        host: str,
        port: int,
        use_ssl: bool,
        timeout_connect: float,
        stop_checker: Optional[Any] = None,
    ):
        connection_kwargs: dict[str, Any] = {}
        if use_ssl:
            connection_kwargs["ssl"] = ssl.create_default_context()
            connection_kwargs["server_hostname"] = host
            connection_kwargs["ssl_handshake_timeout"] = timeout_connect
        return await ChatLLM._wait_with_stop(
            asyncio.open_connection(host, port, **connection_kwargs),
            timeout_connect,
            stop_checker,
        )

    @staticmethod
    def _iter_sse_body_lines(f, is_chunked: bool):
        """同步生成器：从 HTTP 响应体中按完整行产出 bytes
        支持 chunked 传输编码：chunk 边界可能落在任意字节位置（甚至切断
        UTF-8 多字节字符），必须先重组为完整的 body 字节流再按 \\n 切行，
        否则 json.loads 会因半个汉字报 'utf-8' codec can't decode 错误"""
        buffer = b""
        if is_chunked:
            while True:
                size_line = f.readline()
                if not size_line:
                    break
                try:
                    size = int(size_line.strip().split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    break  # 最后一个 chunk，忽略 trailers
                data = f.read(size)
                if not data:
                    break
                f.read(2)  # chunk 数据末尾的 CRLF
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line
                if len(data) < size:
                    break  # 连接提前关闭
        else:
            while True:
                data = f.read1(65536) if hasattr(f, "read1") else f.read(65536)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line
        if buffer:
            yield buffer

    @staticmethod
    async def _aiter_sse_body_lines(
        reader,
        is_chunked: bool,
        timeout_read: float,
        stop_checker: Optional[Any] = None,
    ):
        """异步生成器：从 HTTP 响应体中按完整行产出 bytes
        支持 chunked 传输编码：chunk 边界可能落在任意字节位置（甚至切断
        UTF-8 多字节字符），必须先重组为完整的 body 字节流再按 \\n 切行，
        否则 json.loads 会因半个汉字报 'utf-8' codec can't decode 错误"""
        buffer = b""
        if is_chunked:
            while True:
                size_line = await ChatLLM._wait_with_stop(
                    reader.readline(), timeout_read, stop_checker
                )
                if not size_line:
                    break
                try:
                    size = int(size_line.strip().split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    break  # 最后一个 chunk，忽略 trailers
                data = await ChatLLM._wait_with_stop(
                    reader.readexactly(size), timeout_read, stop_checker
                )
                await ChatLLM._wait_with_stop(
                    reader.readexactly(2), timeout_read, stop_checker
                )  # chunk 数据末尾的 CRLF
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line
        else:
            while True:
                data = await ChatLLM._wait_with_stop(
                    reader.read(65536), timeout_read, stop_checker
                )
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line
        if buffer:
            yield buffer

    @staticmethod
    def std_completions_sse(
        request: ChatLLMRequest = None,
    ) -> Generator[str, None, None]:
        """标准库的 SSE 协议，不依赖 openai 库
        使用给定的对话历史进行聊天，返回 SSE 格式的流式数据
        专为 FastAPI 的 StreamingResponse 设计，直接配合 POST 接口使用
        参数:
            request: ChatLLMRequest 对象，包含所有配置参数（包括 messages）
            api_url: API 地址，可选，不传则使用默认配置（此参数不在 ChatLLMRequest 中）
            timeout_connect: 连接超时秒数，默认 300
            timeout_read:    读取响应超时秒数（含模型处理时间），默认 1800s，大上下文需增大
            timeout_drain:   发送数据冲刷超时秒数，默认 120s，大上下文需增大
        返回:
            生成器形式返回 SSE 格式数据，每条数据格式为：
            data: {"id": "cmpl-xxx", "content": "...", "reasoning_content": "...", "tool_calls": [...], "usage": {...}}\n\n
            结束时返回：data: [DONE]\n\n
            错误时返回：data: {"error": "错误信息"}\n\n
        """
        if request is None:
            raise ValueError("request 参数不能为 None")
        timeout_connect, timeout_read, timeout_drain = ChatLLM._resolve_timeouts(request, 300, 1800, 120)
        last_step = "none"
        retry_count = 0
        while True:
            try:
                chat_config = ChatLLM._resolve_active_chat_config()
                url = ChatLLM.confirm_completions_url(chat_config["url"])
                api_key = chat_config.get("apiKey") or "not-needed"
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=True,
                    model_name=chat_config["selected_model_name"],
                )
                parsed = urllib.parse.urlparse(url)
                host = parsed.hostname
                port = parsed.port
                path = parsed.path or "/v1/chat/completions"
                use_ssl = parsed.scheme == "https"
                if port is None:
                    port = 443 if use_ssl else 80
                body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                last_step = "connect"
                sock = socket.create_connection((host, port), timeout=timeout_connect)
                if use_ssl:
                    sock.settimeout(timeout_connect)
                    ctx = ssl.create_default_context()
                    sock = ctx.wrap_socket(sock, server_hostname=host)
                try:
                    request_line = f"POST {path} HTTP/1.1\r\n"
                    header_lines = (
                        f"Host: {host}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Authorization: Bearer {api_key}\r\n"
                        f"Content-Length: {len(body_bytes)}\r\n"
                        f"Connection: close\r\n"
                        f"\r\n"
                    )
                    last_step = "send_request"
                    sock.settimeout(timeout_drain)
                    sock.sendall((request_line + header_lines).encode("utf-8") + body_bytes)
                    last_step = "read_status"
                    sock.settimeout(timeout_read)
                    f = sock.makefile("rb")
                    status_line = f.readline()
                    status_str = status_line.decode("utf-8", errors="replace").strip()
                    while "100" in status_str or "Continue" in status_str:
                        while True:
                            header_line = f.readline()
                            header_str = header_line.decode("utf-8", errors="replace").strip()
                            if not header_str:
                                break
                        last_step = "read_status_after_100"
                        status_line = f.readline()
                        status_str = status_line.decode("utf-8", errors="replace").strip()
                    last_step = "read_headers"
                    resp_headers = {}
                    while True:
                        header_line = f.readline()
                        header_str = header_line.decode("utf-8", errors="replace").strip()
                        if not header_str:
                            break
                        if ":" in header_str:
                            h_key, h_val = header_str.split(":", 1)
                            resp_headers[h_key.strip().lower()] = h_val.strip()
                    is_chunked = "chunked" in resp_headers.get("transfer-encoding", "").lower()
                    if "200" not in status_str and "201" not in status_str:
                        last_step = "read_error_body"
                        error_body = f.read()
                        error_text = error_body.decode("utf-8", errors="replace")
                        yield f"data: {json.dumps({'error': f'HTTP Error: {status_str}: {error_text}'}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    last_step = "read_sse_stream"
                    for line in ChatLLM._iter_sse_body_lines(f, is_chunked):
                        line = line.strip()
                        if not line or not line.startswith(b"data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == b"[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            usage_only = chunk.get("usage")
                            chunk_id_only = chunk.get("id")
                            if usage_only is not None or chunk_id_only is not None:
                                tail_data = {}
                                if chunk_id_only is not None:
                                    tail_data["id"] = chunk_id_only
                                if usage_only is not None:
                                    tail_data["usage"] = usage_only
                                yield f"data: {json.dumps(tail_data, ensure_ascii=False)}\n\n"
                            continue
                        delta = choices[0].get("delta", {})
                        finish_reason = choices[0].get("finish_reason")
                        usage = chunk.get("usage")
                        chunk_id = chunk.get("id")
                        content = delta.get("content")
                        reasoning_content = delta.get("reasoning_content")
                        tool_calls_delta = delta.get("tool_calls")
                        sse_data = {}
                        if chunk_id is not None:
                            sse_data["id"] = chunk_id
                        if content is not None:
                            sse_data["content"] = content
                        if reasoning_content is not None:
                            sse_data["reasoning_content"] = reasoning_content
                        if tool_calls_delta is not None:
                            sse_data["tool_calls"] = tool_calls_delta
                        if usage is not None:
                            sse_data["usage"] = usage
                        if sse_data:
                            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"
                        if finish_reason is not None:
                            finish_payload = {"finish_reason": finish_reason}
                            if chunk_id is not None:
                                finish_payload["id"] = chunk_id
                            if usage is not None:
                                finish_payload["usage"] = usage
                            yield f"data: {json.dumps(finish_payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass
            except ChatModelConfigurationError as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            except (socket.timeout, OSError) as e:
                retry_count += 1
                yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step}): {str(e)}', 'retry': retry_count}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求失败，进入重试 #{retry_count}（直到用户手动停止）")
                continue
            except Exception as e:
                retry_count += 1
                yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step}): {str(e)}', 'retry': retry_count}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求异常，进入重试 #{retry_count}（直到用户手动停止）")
                continue

    @staticmethod
    async def async_std_completions_sse(
        request: ChatLLMRequest = None,
        stop_checker: Optional[Any] = None,
    ) -> AsyncGenerator[str, None]:
        """标准库 SSE 协议异步版本，不依赖 aiohttp/openai 库
        使用给定的对话历史进行聊天，返回 SSE 格式的流式数据
        专为 FastAPI 的 StreamingResponse 设计，直接配合 POST 接口使用
        参数:
            request: ChatLLMRequest 对象，包含所有配置参数（包括 messages）
            api_url: API 地址，可选，不传则使用默认配置（此参数不在 ChatLLMRequest 中）
            timeout_connect: 连接超时秒数，默认 300
            timeout_read:    读取响应超时秒数（含模型处理时间），默认 1800s，大上下文需增大
            timeout_drain:   发送数据冲刷超时秒数，默认 120s，大上下文需增大
        返回:
            异步生成器形式返回 SSE 格式数据，每条数据格式为：
            data: {"id": "cmpl-xxx", "content": "...", "reasoning_content": "...", "tool_calls": [...], "usage": {...}}\n\n
            结束时返回：data: [DONE]\n\n
            错误时返回：data: {"error": "错误信息"}\n\n
        """
        if request is None:
            raise ValueError("request 参数不能为 None")
        timeout_connect, timeout_read, timeout_drain = ChatLLM._resolve_timeouts(request, 300, 1800, 120)
        last_step = "none"
        retry_count = 0
        while True:
            if ChatLLM._should_stop(stop_checker):
                yield "data: [DONE]\n\n"
                return
            try:
                chat_config = ChatLLM._resolve_active_chat_config()
                url = ChatLLM.confirm_completions_url(chat_config["url"])
                api_key = chat_config.get("apiKey") or "not-needed"
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=True,
                    model_name=chat_config["selected_model_name"],
                )
                parsed = urllib.parse.urlparse(url)
                host = parsed.hostname
                port = parsed.port
                path = parsed.path or "/v1/chat/completions"
                use_ssl = parsed.scheme == "https"
                if port is None:
                    port = 443 if use_ssl else 80
                body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                last_step = "connect"
                reader, writer = await ChatLLM._open_connection(
                    host,
                    port,
                    use_ssl,
                    timeout_connect,
                    stop_checker,
                )
                try:
                    request_line = f"POST {path} HTTP/1.1\r\n"
                    header_lines = (
                        f"Host: {host}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Authorization: Bearer {api_key}\r\n"
                        f"Content-Length: {len(body_bytes)}\r\n"
                        f"Connection: close\r\n"
                        f"\r\n"
                    )
                    last_step = "send_request"
                    writer.write((request_line + header_lines).encode("utf-8") + body_bytes)
                    await ChatLLM._wait_with_stop(writer.drain(), timeout_drain, stop_checker)
                    last_step = "read_status"
                    status_line = await ChatLLM._wait_with_stop(reader.readline(), timeout_read, stop_checker)
                    status_str = status_line.decode("utf-8", errors="replace").strip()
                    while "100" in status_str or "Continue" in status_str:
                        while True:
                            header_line = await ChatLLM._wait_with_stop(reader.readline(), timeout_read, stop_checker)
                            header_str = header_line.decode("utf-8", errors="replace").strip()
                            if not header_str:
                                break
                        last_step = "read_status_after_100"
                        status_line = await ChatLLM._wait_with_stop(reader.readline(), timeout_read, stop_checker)
                        status_str = status_line.decode("utf-8", errors="replace").strip()
                    last_step = "read_headers"
                    resp_headers = {}
                    while True:
                        header_line = await ChatLLM._wait_with_stop(reader.readline(), timeout_read, stop_checker)
                        header_str = header_line.decode("utf-8", errors="replace").strip()
                        if not header_str:
                            break
                        if ":" in header_str:
                            h_key, h_val = header_str.split(":", 1)
                            resp_headers[h_key.strip().lower()] = h_val.strip()
                    is_chunked = "chunked" in resp_headers.get("transfer-encoding", "").lower()
                    if "200" not in status_str and "201" not in status_str:
                        last_step = "read_error_body"
                        error_body = await ChatLLM._wait_with_stop(reader.read(), timeout_read, stop_checker)
                        error_text = error_body.decode("utf-8", errors="replace")
                        yield f"data: {json.dumps({'error': f'HTTP Error: {status_str}: {error_text}'}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    last_step = "read_sse_stream"
                    async for line in ChatLLM._aiter_sse_body_lines(reader, is_chunked, timeout_read, stop_checker):
                        line = line.strip()
                        if not line or not line.startswith(b"data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == b"[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            usage_only = chunk.get("usage")
                            chunk_id_only = chunk.get("id")
                            if usage_only is not None or chunk_id_only is not None:
                                tail_data = {}
                                if chunk_id_only is not None:
                                    tail_data["id"] = chunk_id_only
                                if usage_only is not None:
                                    tail_data["usage"] = usage_only
                                yield f"data: {json.dumps(tail_data, ensure_ascii=False)}\n\n"
                            continue
                        delta = choices[0].get("delta", {})
                        finish_reason = choices[0].get("finish_reason")
                        usage = chunk.get("usage")
                        chunk_id = chunk.get("id")
                        content = delta.get("content")
                        reasoning_content = delta.get("reasoning_content")
                        tool_calls_delta = delta.get("tool_calls")
                        sse_data = {}
                        if chunk_id is not None:
                            sse_data["id"] = chunk_id
                        if content is not None:
                            sse_data["content"] = content
                        if reasoning_content is not None:
                            sse_data["reasoning_content"] = reasoning_content
                        if tool_calls_delta is not None:
                            sse_data["tool_calls"] = tool_calls_delta
                        if usage is not None:
                            sse_data["usage"] = usage
                        if sse_data:
                            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"
                        if finish_reason is not None:
                            finish_payload = {"finish_reason": finish_reason}
                            if chunk_id is not None:
                                finish_payload["id"] = chunk_id
                            if usage is not None:
                                finish_payload["usage"] = usage
                            yield f"data: {json.dumps(finish_payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                finally:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
            except ChatModelConfigurationError as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            except (asyncio.TimeoutError, OSError) as e:
                retry_count += 1
                yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step}): {str(e)}', 'retry': retry_count}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求失败，进入重试 #{retry_count}（直到用户手动停止）")
                continue
            except asyncio.CancelledError:
                yield "data: [DONE]\n\n"
                return
            except Exception as e:
                retry_count += 1
                yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step}): {str(e)}', 'retry': retry_count}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求异常，进入重试 #{retry_count}（直到用户手动停止）")
                continue
            
    @staticmethod
    def chat_completions(
        request: ChatLLMRequest = None,
        stream: Optional[bool] = None,
        stop_checker: Optional[Any] = None,
    ) -> Union[AsyncGenerator[str, Any], dict[str, Any]]:
        """统一聊天入口，支持流式和非流式响应。

        返回值说明：
        - 当 stream=True 时，返回一个异步生成器，逐块产出 SSE 文本。
        - 当 stream=False 时，返回一个包含 id / content / reasoning_content / tool_calls / finish_reason / usage 的字典。
        """
        if request is None:
            raise ValueError("request 参数不能为 None")
        if stream is None:
            stream = request.stream
        timeout_connect, timeout_read, timeout_drain = ChatLLM._resolve_timeouts(
            request,
            300,
            1800,
            120,
        )
        if stream:
            async def _stream_generator():
                async for chunk in ChatLLM.async_std_completions_sse(
                    request=request,
                    stop_checker=stop_checker,
                ):
                    yield chunk
            return _stream_generator()
        chat_config = ChatLLM._resolve_active_chat_config()
        url = ChatLLM.confirm_completions_url(chat_config["url"])
        api_key = chat_config.get("apiKey") or "not-needed"
        payload = ChatLLM._build_request_payload(
            request,
            stream=False,
            model_name=chat_config["selected_model_name"],
        )
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port
        path = parsed.path or "/v1/chat/completions"
        use_ssl = parsed.scheme == "https"
        if port is None:
            port = 443 if use_ssl else 80
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sock = socket.create_connection((host, port), timeout=timeout_connect)
        if use_ssl:
            sock.settimeout(timeout_connect)
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        try:
            request_line = f"POST {path} HTTP/1.1\r\n"
            header_lines = (
                f"Host: {host}\r\n"
                f"Content-Type: application/json\r\n"
                f"Authorization: Bearer {api_key}\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            sock.settimeout(timeout_drain)
            sock.sendall((request_line + header_lines).encode("utf-8") + body_bytes)
            sock.settimeout(timeout_read)
            f = sock.makefile("rb")
            status_line = f.readline()
            status_str = status_line.decode("utf-8", errors="replace").strip()
            while "100" in status_str or "Continue" in status_str:
                while True:
                    header_line = f.readline()
                    header_str = header_line.decode("utf-8", errors="replace").strip()
                    if not header_str:
                        break
                status_line = f.readline()
                status_str = status_line.decode("utf-8", errors="replace").strip()
            resp_headers = {}
            while True:
                header_line = f.readline()
                header_str = header_line.decode("utf-8", errors="replace").strip()
                if not header_str:
                    break
                if ":" in header_str:
                    h_key, h_val = header_str.split(":", 1)
                    resp_headers[h_key.strip().lower()] = h_val.strip()
            if "200" not in status_str and "201" not in status_str:
                error_body = f.read()
                raise RuntimeError(f"HTTP Error: {status_str}: {error_body.decode('utf-8', errors='replace')}")
            response_body = f.read()
            response_text = response_body.decode("utf-8", errors="replace")
            response_json = json.loads(response_text) if response_text else {}
            choices = response_json.get("choices", [])
            if not choices:
                return {
                    "id": response_json.get("id"),
                    "content": "",
                    "reasoning_content": "",
                    "tool_calls": [],
                    "finish_reason": None,
                    "usage": response_json.get("usage"),
                    "raw": response_json,
                }
            choice = choices[0]
            message = choice.get("message", {}) or {}
            content = message.get("content")
            reasoning_content = message.get("reasoning_content")
            tool_calls = message.get("tool_calls")
            finish_reason = choice.get("finish_reason")
            usage = response_json.get("usage")
            return {
                "id": response_json.get("id"),
                "content": content,
                "reasoning_content": reasoning_content,
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
                "usage": usage,
                "raw": response_json,
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass


if __name__ == "__main__":
    # 测试 should_include_reasoning_effort 方法
    
    test_models = [
        # GPT-5系列模型（应该返回True）
        ("gpt-5", True),
        ("gpt-5-mini", True),
        ("gpt-5-nano", True),
        ("gpt-5.1", True),
        ("gpt-5.2", True),
        ("gpt-5.3", True),
        ("gpt-5.4", True),
        ("gpt-5.5", True),
        
        # OpenAI o系列模型（应该返回True）
        ("o3", True),
        ("o3-mini", True),
        ("o4-mini", True),
        
        # DeepSeek模型（应该返回True）
        ("deepseek-reasoner", True),
        ("deepseek-chat", True),
        ("deepseek-v4-flash", True),
        ("deepseek-v4-pro", True),
        
        # GPT-OSS开源模型（应该返回True）
        ("gpt-oss", True),
        
        # 不支持的模型（应该返回False）
        ("gpt-4o", False),          # GPT-4系列不支持
        ("gpt-4o-mini", False),     # GPT-4系列不支持
        ("gpt-4", False),           # GPT-4系列不支持
        ("gpt-3.5-turbo", False),   # GPT-3.5系列不支持
        ("Qwen3.5-2B", False),
        ("qwen-turbo", False),
        ("claude-3-opus", False),
        ("llama-3-70b", False),
        ("glm-4", False),
        ("", False),
        (None, False),
    ]
    
    ...
