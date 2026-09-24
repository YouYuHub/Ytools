# from email.mime import base
# from tkinter.filedialog import Open
# 标准库
# from typing import Generator, Any
import json
# import urllib.request
# import urllib.error
import re
import time
import urllib.parse
import socket
import ssl
# import uuid
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
    from env_manager import ChatModelConfigurationError, load_var, require_default_chat_config
except ImportError:
    ChatModelConfigurationError = RuntimeError

    def load_var(name, default=None):  # type: ignore[misc]
        return default

    require_default_chat_config = None
# 导入配置模型
from config import (
    ChatLLMRequest,
    DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS,
    DEFAULT_REQUEST_HARD_LIMIT_TOKENS,
)


def _resolve_network_retry_max_attempts() -> int:
    """解析网络请求连续失败重试上限（聊天设置可配，持久化在 .env）。

    返回值语义：0 或负数 = 不限制（保持旧行为，一直重试直到手动停止）；
    正数 = 同一轮流式请求连续失败达到该次数后不再重试，直接报错。
    """
    raw = load_var("NETWORK_RETRY_MAX_ATTEMPTS", DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS


# 网络重试固定间隔（秒）：重试之间等待，避免对供应商网关形成无间隔轰炸。
# 按需求固定 1 秒、不做配置项（详见 docs/api_docs.md 重试语义）。
_NETWORK_RETRY_INTERVAL_SECONDS = 1.0

# 供应商请求硬限制的安全余量与最低输出预算：
# 估算口径与真实 tokenizer 有偏差，预留余量；max_tokens 降到最低仍超限才判定不可发送。
_HARD_LIMIT_SAFETY_MARGIN_TOKENS = 512
_HARD_LIMIT_MIN_MAX_TOKENS = 1024

# 多模态部件固定占位（与 chat_runtime.MULTIMODAL_PART_PLACEHOLDER_TOKENS 同口径）：
# image_url / input_audio 的 base64 数据按字符估算会虚高数万倍，按单图 ~1k token 计
_MULTIMODAL_PART_PLACEHOLDER_TOKENS = 1024


def _resolve_request_hard_limit_tokens() -> int:
    """解析供应商请求硬限制（input_tokens + max_tokens 之和上限）。

    实测 opencode.ai 网关（orcarouter 上游）：input + max_tokens > 1048576（2^20）
    时返回 HTTP 400 且响应体为空（Content-Type: text/event-stream），属确定性
    失败，重试同一 payload 无意义。默认 1048576；0 或负数 = 禁用预检
    （其他供应商无此限制时可关闭）。可用 .env 的 REQUEST_HARD_LIMIT_TOKENS 覆盖。
    """
    raw = load_var("REQUEST_HARD_LIMIT_TOKENS", DEFAULT_REQUEST_HARD_LIMIT_TOKENS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_HARD_LIMIT_TOKENS


def _estimate_payload_input_tokens(payload: dict[str, Any]) -> int:
    """估算请求 payload 的输入 tokens（发送前硬限制预检用）。

    口径与项目统一估算一致（ASCII//4 + 非 ASCII 逐字），宁可略高估以拦截
    确定性失败；多模态部件（image_url / input_audio）按固定占位计费，
    避免 base64/data URL 字符数虚高估算。
    """
    def _text_tokens(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                text = str(value)
        else:
            text = str(value)
        if not text:
            return 0
        ascii_chars = sum(1 for char in text if ord(char) < 128)
        non_ascii_chars = len(text) - ascii_chars
        return max(1, ascii_chars // 4 + non_ascii_chars)

    total = 0
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "input_audio"):
                    total += _MULTIMODAL_PART_PLACEHOLDER_TOKENS
                elif part.get("type") == "text":
                    total += _text_tokens(part.get("text"))
                else:
                    total += _text_tokens(part)
        else:
            total += _text_tokens(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += _text_tokens(tool_calls)
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str):
            total += _text_tokens(reasoning_content)
    tools = payload.get("tools")
    if tools:
        total += _text_tokens(tools)
    return total


def _build_custom_header_lines(chat_config: dict[str, Any]) -> str:
    """把模型配置中的自定义请求头（`_custom_headers`）拼为原始 HTTP 头行。

    配置来源（随模型配置注入）：会话/全局 model_selection.<role>.headers，
    由 env_manager.inject_custom_headers_into_config 写入配置副本。非法条目
    （空名/含冒号或空白）在规范化阶段已丢弃，此处仅作防御性过滤。
    """
    raw = (chat_config or {}).get("_custom_headers")
    if not isinstance(raw, list):
        return ""
    lines = ""
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = "" if item.get("value") is None else str(item.get("value"))
        if not name or re.search(r"[\r\n]", name + value):
            continue
        lines += f"{name}: {value}\r\n"
    return lines


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

    # @staticmethod
    # def should_include_reasoning_effort(model_name: str) -> bool:
    #     """
    #     判断是否应该在请求中包含 reasoning_effort 参数
    #     该参数仅适用于 OpenAI o系列、GPT-5系列和部分DeepSeek模型
    #     参数:
    #         model_name: 模型名称
    #     返回:
    #         True 如果应该包含 reasoning_effort 参数，否则 False
    #     """
    #     if not model_name:
    #         return False
    #     model_lower = model_name.lower()
    #     # GPT-5系列（所有以 gpt-5 开头的模型都支持）
    #     if model_lower.startswith("gpt-5"):
    #         return True
    #     # o系列（所有以 o 开头的模型都支持，如 o1, o3, o4-mini 等）
    #     if model_lower.startswith("gpt-o"):
    #         return True
    #     # DeepSeek 支持 reasoning_effort 的模型
    #     deepseek_reasoning_models = set((
    #         "deepseek-v4-flash",
    #         "deepseek-v4-pro",
    #         # "deepseek-coder".0
    #     ))
    #     # if any(dm in model_lower for dm in deepseek_reasoning_models):
    #     if model_lower in deepseek_reasoning_models:
    #         return True
    #     # # 开源 GPT-OSS 模型
    #     # if "gpt-oss" in model_lower:
    #     #     return True
    #     return False

    @staticmethod
    def _resolve_chat_config(model_config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        """校验当前或调用方指定的 Chat Completions 模型配置。"""
        if model_config is None:
            if require_default_chat_config is None:
                raise ChatModelConfigurationError("无法加载当前聊天模型配置")
            chat_config = require_default_chat_config()
        elif isinstance(model_config, dict):
            chat_config = model_config
        else:
            raise ChatModelConfigurationError("指定的聊天模型配置必须是字典")
        api_url = chat_config.get("url")
        model_name = chat_config.get("selected_model_id") or chat_config.get("selected_model_name")
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
    def _resolve_active_chat_config() -> dict[str, Any]:
        """解析 .env 当前选中的聊天模型。"""
        return ChatLLM._resolve_chat_config()

    @staticmethod
    def confirm_completions_url(api_url: str) -> str:
        base = api_url.rstrip('/')
        if base.endswith('/chat/completions'):
            return base
        # if base.endswith('/v1'):
        #     return f"{base}/chat/completions"
        return f"{base}/chat/completions"

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
        # if request.reasoning_effort and ChatLLM.should_include_reasoning_effort(model_name):
        #     payload["reasoning_effort"] = request.reasoning_effort
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
    def _precheck_hard_limit(payload: dict[str, Any]) -> str | None:
        """发送前硬限制预检（input + max_tokens 不得超过供应商上限）。

        返回 None = 通过（必要时已就地降级 payload["max_tokens"]，日志提示）；
        返回字符串 = 不可挽救的确定性失败说明（调用方应直接失败、跳过网络重试）。
        每次尝试（含重试）都会调用：payload 每次重建，降级结果不跨尝试保留。
        """
        hard_limit = _resolve_request_hard_limit_tokens()
        if hard_limit <= 0:
            return None
        try:
            estimated_input = _estimate_payload_input_tokens(payload)
        except Exception:
            return None  # 估算失败不阻断发送（保守放行）
        raw_max_tokens = payload.get("max_tokens")
        try:
            max_tokens_value = int(raw_max_tokens) if raw_max_tokens is not None else 0
        except (TypeError, ValueError):
            max_tokens_value = 0
        if estimated_input + max_tokens_value <= hard_limit:
            return None
        # 输入本身在限额内：自动降级 max_tokens 保发送（保留最低输出预算）
        headroom = hard_limit - estimated_input - _HARD_LIMIT_SAFETY_MARGIN_TOKENS
        if headroom >= _HARD_LIMIT_MIN_MAX_TOKENS:
            payload["max_tokens"] = headroom
            print(
                f"[WARN] 请求规模预检：估算输入 {estimated_input} + max_tokens "
                f"{max_tokens_value} 超过供应商硬限制 {hard_limit}，"
                f"已自动降 max_tokens 至 {headroom}"
            )
            return None
        return (
            f"请求规模超过供应商硬限制：估算输入 {estimated_input} tokens + "
            f"max_tokens {max_tokens_value} > {hard_limit}（2^20）；"
            f"max_tokens 已无压缩空间，无法发送（确定性失败，跳过网络重试）。"
            f"请压缩上下文或缩减工具结果后重试"
        )

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
    def _read_full_response_body(f, is_chunked: bool) -> bytes:
        """同步读取完整 HTTP 响应体，支持 chunked 传输编码。

        非流式 LLM 响应对大 body 普遍使用 Transfer-Encoding: chunked；
        直接 f.read() 会拿到 `<hex-size>\\r\\n<data>\\r\\n...0\\r\\n\\r\\n`
        的原始分块格式，导致 json.loads 报
        'Expecting value: line 1 column 1 (char 0)'。
        """
        if not is_chunked:
            return f.read()
        buffer = b""
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
        return buffer

    @staticmethod
    async def _aread_full_response_body(
        reader,
        is_chunked: bool,
        timeout_read: float,
        stop_checker: Optional[Any] = None,
    ) -> bytes:
        """异步读取完整 HTTP 响应体，支持 chunked 传输编码（见同步版说明）。"""
        if not is_chunked:
            return await ChatLLM._wait_with_stop(reader.read(), timeout_read, stop_checker)
        buffer = b""
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
                break
            data = await ChatLLM._wait_with_stop(
                reader.readexactly(size), timeout_read, stop_checker
            )
            if not data:
                break
            await ChatLLM._wait_with_stop(
                reader.readexactly(2), timeout_read, stop_checker
            )  # chunk 数据末尾的 CRLF
            buffer += data
        return buffer

    @staticmethod
    def std_completions_sse(
        request: ChatLLMRequest = None,
        model_config: Optional[Dict[str, Any]] = None,
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
        retry_max_attempts = _resolve_network_retry_max_attempts()
        while True:
            # 网络重试固定间隔：第 2 次及以后尝试前等待，避免无间隔轰炸
            # 供应商网关（重试均为同一 payload，间隔不改变请求内容）
            if retry_count > 0:
                time.sleep(_NETWORK_RETRY_INTERVAL_SECONDS)
            try:
                chat_config = ChatLLM._resolve_chat_config(model_config)
                url = ChatLLM.confirm_completions_url(chat_config["url"])
                api_key = chat_config.get("apiKey") or "not-needed"
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=True,
                    model_name=chat_config["selected_model_id"],
                )
                parsed = urllib.parse.urlparse(url)
                host = parsed.hostname
                port = parsed.port
                path = parsed.path or "/chat/completions"
                use_ssl = parsed.scheme == "https"
                if port is None:
                    port = 443 if use_ssl else 80
                # 每次尝试都预检：payload 每次重试都会重建（max_tokens 回到
                # 配置原值），若仅首次预检，降级结果会在重试时丢失导致再次超限
                hard_limit_error = ChatLLM._precheck_hard_limit(payload)
                if hard_limit_error is not None:
                    print(f"[ERROR] {hard_limit_error}")
                    yield f"data: {json.dumps({'error': hard_limit_error, 'error_type': 'hard_limit', 'step': 'precheck', 'retry': 0, 'max_attempts': None, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
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
                        # 模型配置的自定义请求头（model_selection.<role>.headers）
                        f"{_build_custom_header_lines(chat_config)}"
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
                        # 网关偶发 4xx/5xx（如瞬时 400 Bad Request）与网络错误同源：
                        # 纳入 NETWORK_RETRY_MAX_ATTEMPTS 重试（语义见异步版注释）
                        last_step = "read_error_body"
                        error_body = ChatLLM._read_full_response_body(f, is_chunked)
                        error_text = error_body.decode("utf-8", errors="replace")
                        # 400 空响应体（网关规模超限/过载等）补充响应头诊断信息：
                        # x-opencode-log-id / x-opencode-endpoint-id 供供应商侧追查
                        if not error_text:
                            error_text = (
                                "[空响应体] log-id=%s endpoint-id=%s"
                                % (
                                    resp_headers.get("x-opencode-log-id", "-"),
                                    resp_headers.get("x-opencode-endpoint-id", "-"),
                                )
                            )
                        http_error = f"HTTP Error: {status_str}: {error_text}"
                        retry_count += 1
                        if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                            print(f"[ERROR] API 返回 {status_str}，连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                            yield f"data: {json.dumps({'error': f'HTTP错误(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {http_error}', 'error_type': 'http', 'step': last_step, 'error_detail': http_error, 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {json.dumps({'error': f'HTTP错误(步骤:{last_step}): {http_error}', 'error_type': 'http', 'step': last_step, 'error_detail': http_error, 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                        print(f"[WARN] API 返回 {status_str}，进入重试 #{retry_count}"
                              + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                        continue  # finally 先关闭当前连接，随后重连重发同一 payload
                    last_step = "read_sse_stream"
                    saw_finish_reason = False
                    saw_done = False
                    # 零输出检测：任一正文/思考/工具调用增量出现即置位。
                    # HTTP 200 但整条流没有任何有效输出（网关吞错返回空流、
                    # 供应商过载、内容被上游静默丢弃）与连接失败同源且瞬时
                    # 概率高，纳入 NETWORK_RETRY_MAX_ATTEMPTS 重连重发
                    saw_any_output = False
                    for line in ChatLLM._iter_sse_body_lines(f, is_chunked):
                        line = line.strip()
                        if not line or not line.startswith(b"data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == b"[DONE]":
                            saw_done = True
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            # 供应商错误帧（如 data: {"error": {...}}）不再静默吞掉：
                            # 提取真实错误信息上报并终止，避免仅表现为"空响应"无从定位
                            error_frame = chunk.get("error")
                            if error_frame is not None:
                                try:
                                    error_frame_text = json.dumps(error_frame, ensure_ascii=False)
                                except Exception:
                                    error_frame_text = str(error_frame)
                                yield f"data: {json.dumps({'error': '供应商错误帧: ' + error_frame_text, 'error_type': 'upstream_error_frame', 'retrying': False}, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
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
                        if content or reasoning_content or tool_calls_delta:
                            saw_any_output = True
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
                            saw_finish_reason = True
                            finish_payload = {"finish_reason": finish_reason}
                            if chunk_id is not None:
                                finish_payload["id"] = chunk_id
                            if usage is not None:
                                finish_payload["usage"] = usage
                            yield f"data: {json.dumps(finish_payload, ensure_ascii=False)}\n\n"
                    if not saw_any_output:
                        # 200 + 正常收尾（或空收尾标记）但零输出：视同连接失败
                        # 纳入网络重试，重连重发同一 payload（messages 未变，
                        # 已 yield 的增量帧为空所以无重复内容风险）。达到上限
                        # 才发终止性错误帧（retrying=False），由调用方落盘报错
                        retry_count += 1
                        empty_error = (
                            f"API 返回 {status_str} 但整个响应流没有任何模型输出"
                            f"（步骤:{last_step}）"
                        )
                        if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                            print(f"[ERROR] API 200 空响应，连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                            yield f"data: {json.dumps({'error': f'{empty_error}，已重试 {retry_max_attempts} 次仍为空', 'error_type': 'empty_response', 'step': last_step, 'error_detail': empty_error, 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {json.dumps({'error': f'{empty_error}', 'error_type': 'empty_response', 'step': last_step, 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                        print(f"[WARN] API 200 空响应，进入重试 #{retry_count}"
                              + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                        continue  # finally 先关闭当前连接，随后重连重发同一 payload
                    if not saw_done and not saw_finish_reason:
                        # 上游连接在流式响应完成前被关闭（EOF 且未收到
                        # finish_reason）：内容不完整。补发标记帧告知调用方
                        # （由调用方决定重试策略）；仍按惯例合成 [DONE] 收尾
                        yield f"data: {json.dumps({'stream_truncated': True}, ensure_ascii=False)}\n\n"
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
                if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                    print(f"[ERROR] API 请求连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                    yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {str(e)}', 'error_type': 'connect', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step}): {str(e)}', 'error_type': 'connect', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求失败，进入重试 #{retry_count}"
                      + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                continue
            except Exception as e:
                retry_count += 1
                if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                    print(f"[ERROR] API 请求连续异常 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                    yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {str(e)}', 'error_type': 'request', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step}): {str(e)}', 'error_type': 'request', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求异常，进入重试 #{retry_count}"
                      + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                continue

    @staticmethod
    async def async_std_completions_sse(
        request: ChatLLMRequest = None,
        stop_checker: Optional[Any] = None,
        model_config: Optional[Dict[str, Any]] = None,
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
        retry_max_attempts = _resolve_network_retry_max_attempts()
        while True:
            if ChatLLM._should_stop(stop_checker):
                yield "data: [DONE]\n\n"
                return
            # 网络重试固定间隔：第 2 次及以后尝试前等待，避免无间隔轰炸
            # 供应商网关（重试均为同一 payload，间隔不改变请求内容）
            if retry_count > 0:
                await asyncio.sleep(_NETWORK_RETRY_INTERVAL_SECONDS)
            try:
                chat_config = ChatLLM._resolve_chat_config(model_config)
                url = ChatLLM.confirm_completions_url(chat_config["url"])
                api_key = chat_config.get("apiKey") or "not-needed"
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=True,
                    model_name=chat_config["selected_model_id"],
                )
                parsed = urllib.parse.urlparse(url)
                host = parsed.hostname
                port = parsed.port
                path = parsed.path or "/chat/completions"
                use_ssl = parsed.scheme == "https"
                if port is None:
                    port = 443 if use_ssl else 80
                # 每次尝试都预检：payload 每次重试都会重建（max_tokens 回到
                # 配置原值），若仅首次预检，降级结果会在重试时丢失导致再次超限
                hard_limit_error = ChatLLM._precheck_hard_limit(payload)
                if hard_limit_error is not None:
                    print(f"[ERROR] {hard_limit_error}")
                    yield f"data: {json.dumps({'error': hard_limit_error, 'error_type': 'hard_limit', 'step': 'precheck', 'retry': 0, 'max_attempts': None, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
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
                        # 模型配置的自定义请求头（model_selection.<role>.headers，
                        # 取代原先硬编码的 x-opencode-session   ——   opencode 等供应商
                        # 需要的会话头在参数面板按角色自行配置）
                        f"{_build_custom_header_lines(chat_config)}"
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
                        # 网关偶发 4xx/5xx（如瞬时 400 Bad Request）与网络错误同源：
                        # 纳入 NETWORK_RETRY_MAX_ATTEMPTS 重试。未达上限发
                        # retrying=True 重试帧（仅推送前端提示，不落盘）后重连重发；
                        # 达到上限才发 retrying=False 终止帧（由调用方落盘）。
                        last_step = "read_error_body"
                        error_body = await ChatLLM._aread_full_response_body(
                            reader, is_chunked, timeout_read, stop_checker
                        )
                        error_text = error_body.decode("utf-8", errors="replace")
                        # 400 空响应体（网关规模超限/过载等）补充响应头诊断信息
                        if not error_text:
                            error_text = (
                                "[空响应体] log-id=%s endpoint-id=%s"
                                % (
                                    resp_headers.get("x-opencode-log-id", "-"),
                                    resp_headers.get("x-opencode-endpoint-id", "-"),
                                )
                            )
                        http_error = f"HTTP Error: {status_str}: {error_text}"
                        retry_count += 1
                        if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                            print(f"[ERROR] API 返回 {status_str}，连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                            yield f"data: {json.dumps({'error': f'HTTP错误(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {http_error}', 'error_type': 'http', 'step': last_step, 'error_detail': http_error, 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {json.dumps({'error': f'HTTP错误(步骤:{last_step}): {http_error}', 'error_type': 'http', 'step': last_step, 'error_detail': http_error, 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                        print(f"[WARN] API 返回 {status_str}，进入重试 #{retry_count}"
                              + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                        continue  # finally 先关闭当前连接，随后重连重发同一 payload
                    last_step = "read_sse_stream"
                    saw_finish_reason = False
                    saw_done = False
                    # 零输出检测（语义见同步版注释）：HTTP 200 但整条流没有
                    # 任何有效输出时纳入 NETWORK_RETRY_MAX_ATTEMPTS 重连重发
                    saw_any_output = False
                    async for line in ChatLLM._aiter_sse_body_lines(reader, is_chunked, timeout_read, stop_checker):
                        line = line.strip()
                        if not line or not line.startswith(b"data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == b"[DONE]":
                            saw_done = True
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            # 供应商错误帧（如 data: {"error": {...}}）不再静默吞掉：
                            # 提取真实错误信息上报并终止，避免仅表现为"空响应"无从定位
                            error_frame = chunk.get("error")
                            if error_frame is not None:
                                try:
                                    error_frame_text = json.dumps(error_frame, ensure_ascii=False)
                                except Exception:
                                    error_frame_text = str(error_frame)
                                yield f"data: {json.dumps({'error': '供应商错误帧: ' + error_frame_text, 'error_type': 'upstream_error_frame', 'retrying': False}, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
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
                        if content or reasoning_content or tool_calls_delta:
                            saw_any_output = True
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
                            saw_finish_reason = True
                            finish_payload = {"finish_reason": finish_reason}
                            if chunk_id is not None:
                                finish_payload["id"] = chunk_id
                            if usage is not None:
                                finish_payload["usage"] = usage
                            yield f"data: {json.dumps(finish_payload, ensure_ascii=False)}\n\n"
                    if not saw_any_output:
                        # 200 + 正常收尾（或空收尾标记）但零输出：视同连接失败
                        # 纳入网络重试（语义见同步版注释）
                        retry_count += 1
                        empty_error = (
                            f"API 返回 {status_str} 但整个响应流没有任何模型输出"
                            f"（步骤:{last_step}）"
                        )
                        if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                            print(f"[ERROR] API 200 空响应，连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                            yield f"data: {json.dumps({'error': f'{empty_error}，已重试 {retry_max_attempts} 次仍为空', 'error_type': 'empty_response', 'step': last_step, 'error_detail': empty_error, 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {json.dumps({'error': f'{empty_error}', 'error_type': 'empty_response', 'step': last_step, 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                        print(f"[WARN] API 200 空响应，进入重试 #{retry_count}"
                              + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                        continue  # finally 先关闭当前连接，随后重连重发同一 payload
                    if not saw_done and not saw_finish_reason:
                        # 上游连接在流式响应完成前被关闭（EOF 且未收到
                        # finish_reason）：内容不完整。补发标记帧告知调用方
                        # （由调用方决定重试策略）；仍按惯例合成 [DONE] 收尾
                        yield f"data: {json.dumps({'stream_truncated': True}, ensure_ascii=False)}\n\n"
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
                if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                    # 连续失败达到配置上限：发终止性错误帧（retrying=False），
                    # 上游据此结束任务并落盘最终失败记录
                    print(f"[ERROR] API 请求连续失败 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                    yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {str(e)}', 'error_type': 'connect', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps({'error': f'连接失败(步骤:{last_step}): {str(e)}', 'error_type': 'connect', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求失败，进入重试 #{retry_count}"
                      + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                continue
            except asyncio.CancelledError:
                yield "data: [DONE]\n\n"
                return
            except Exception as e:
                retry_count += 1
                if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                    print(f"[ERROR] API 请求连续异常 {retry_count - 1} 次后达到重试上限 {retry_max_attempts}，停止重试")
                    yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step})，已重试 {retry_max_attempts} 次仍失败: {str(e)}', 'error_type': 'request', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts, 'retrying': False}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps({'error': f'请求异常(步骤:{last_step}): {str(e)}', 'error_type': 'request', 'step': last_step, 'error_detail': str(e), 'retry': retry_count, 'max_attempts': retry_max_attempts or None, 'retrying': True}, ensure_ascii=False)}\n\n"
                print(f"[WARN] API 请求异常，进入重试 #{retry_count}"
                      + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到用户手动停止）"))
                continue

    @staticmethod
    def chat_completions(
        request: ChatLLMRequest = None,
        stream: Optional[bool] = None,
        stop_checker: Optional[Any] = None,
        model_config: Optional[Dict[str, Any]] = None,
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
                    model_config=model_config,
                ):
                    yield chunk

            return _stream_generator()
        chat_config = ChatLLM._resolve_chat_config(model_config)
        url = ChatLLM.confirm_completions_url(chat_config["url"])
        api_key = chat_config.get("apiKey") or "not-needed"
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port
        path = parsed.path or "/chat/completions"
        use_ssl = parsed.scheme == "https"
        if port is None:
            port = 443 if use_ssl else 80
        # 非流式零输出重试：HTTP 200 但响应无任何模型输出（空 body/空 choices/
        # 空 message 字段）与连接失败同源，纳入 NETWORK_RETRY_MAX_ATTEMPTS
        # 重连重发；达到上限抛 RuntimeError（调用方按既有异常语义兜底）
        last_step = "none"
        retry_count = 0
        retry_max_attempts = _resolve_network_retry_max_attempts()
        while True:
            # 网络重试固定间隔：第 2 次及以后尝试前等待（语义同流式版）
            if retry_count > 0:
                time.sleep(_NETWORK_RETRY_INTERVAL_SECONDS)
            payload = ChatLLM._build_request_payload(
                request,
                stream=False,
                model_name=chat_config["selected_model_id"],
            )
            # 每次尝试都预检（语义见流式版注释）
            hard_limit_error = ChatLLM._precheck_hard_limit(payload)
            if hard_limit_error is not None:
                print(f"[ERROR] {hard_limit_error}")
                raise RuntimeError(hard_limit_error)
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
                    # 模型配置的自定义请求头（model_selection.<role>.headers）
                    f"{_build_custom_header_lines(chat_config)}"
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
                is_chunked = "chunked" in resp_headers.get("transfer-encoding", "").lower()
                if "200" not in status_str and "201" not in status_str:
                    last_step = "read_error_body"
                    error_body = ChatLLM._read_full_response_body(f, is_chunked)
                    error_text = error_body.decode("utf-8", errors="replace")
                    # 400 空响应体补充响应头诊断信息（语义同流式版）
                    if not error_text:
                        error_text = (
                            "[空响应体] log-id=%s endpoint-id=%s"
                            % (
                                resp_headers.get("x-opencode-log-id", "-"),
                                resp_headers.get("x-opencode-endpoint-id", "-"),
                            )
                        )
                    raise RuntimeError(f"HTTP Error: {status_str}: {error_text}")
                last_step = "read_body"
                response_body = ChatLLM._read_full_response_body(f, is_chunked)
                response_text = response_body.decode("utf-8", errors="replace")
                try:
                    response_json = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError as exc:
                    # 带上下文抛出，便于诊断网关返回非 JSON（HTML 错误页/空体/分块未解码等）
                    raise RuntimeError(
                        f"压缩/非流式响应不是有效 JSON（{status_str}）：{exc}；"
                        f"body 前 300 字符：{response_text[:300]!r}"
                    ) from exc
                choices = response_json.get("choices", [])
                message = (choices[0].get("message", {}) or {}) if choices else {}
                content = message.get("content")
                reasoning_content = message.get("reasoning_content")
                tool_calls = message.get("tool_calls")
                has_output = bool(
                    (isinstance(content, str) and content.strip())
                    or (isinstance(reasoning_content, str) and reasoning_content.strip())
                    or tool_calls
                )
                if not has_output:
                    # 200 但零输出：纳入网络重试重发同一 payload
                    retry_count += 1
                    empty_error = (
                        f"API 返回 {status_str} 但响应没有模型输出"
                        f"（步骤:{last_step}，body 前 200 字符：{response_text[:200]!r}）"
                    )
                    if retry_max_attempts > 0 and retry_count > retry_max_attempts:
                        raise RuntimeError(
                            f"API 200 空响应（步骤:{last_step}），已重试 {retry_max_attempts} 次仍为空: {empty_error}"
                        )
                    print(
                        f"[WARN] API 200 空响应，进入重试 #{retry_count}"
                        + (f"（上限 {retry_max_attempts}）" if retry_max_attempts > 0 else "（直到手动停止）")
                    )
                    continue  # finally 先关闭当前连接，随后重连重发同一 payload
                finish_reason = choices[0].get("finish_reason")
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
