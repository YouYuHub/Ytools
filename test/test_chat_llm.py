import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import init_path


class ChatLLMConfigTests(unittest.TestCase):
    def _initialize_selected_model_config(self, temp_path: Path) -> None:
        (temp_path / "setting").mkdir(parents=True, exist_ok=True)
        (temp_path / ".env").write_text(
            "CHAT_OWNERSHIP_NANE=Selected Provider\nCHAT_MODEL_NAME=selected-model\n",
            encoding="utf-8",
        )
        (temp_path / "setting" / "models.json").write_text(
            json.dumps({
                "Selected Provider": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-api-key",
                    "apiType": "chat-completions",
                    "models": {
                        "selected-model": {
                            "id": "selected-model",
                            "url": "https://selected.example.test/v1",
                            "maxInputTokens": 123456,
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        init_path(str(temp_path))

    def test_selected_models_json_config_overrides_legacy_request_fields(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                self._initialize_selected_model_config(Path(temp_dir))
                request = ChatLLMRequest(
                    api_url="https://request.example.test/v1",
                    model="request-model",
                    messages=[{"role": "user", "content": "你好"}],
                    extra_body={"model": "extra-body-model", "custom_option": True},
                )

                chat_config = ChatLLM._resolve_active_chat_config()
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=False,
                    model_name=chat_config["selected_model_id"],
                )

                self.assertEqual(chat_config["url"], "https://selected.example.test/v1")
                self.assertEqual(payload["model"], "selected-model")
                self.assertTrue(payload["custom_option"])
                self.assertNotIn("api_url", request.model_dump())
                self.assertNotIn("model", request.model_dump())
            finally:
                init_path(str(repo_root))

    def test_async_https_connection_uses_request_connect_timeout_for_tls_handshake(self):
        async def run_case():
            open_connection = AsyncMock(return_value=(object(), object()))
            with patch("chat.chat_llm.asyncio.open_connection", new=open_connection):
                reader, writer = await ChatLLM._open_connection(
                    "example.test",
                    443,
                    use_ssl=True,
                    timeout_connect=300,
                )

            self.assertIsNotNone(reader)
            self.assertIsNotNone(writer)
            self.assertEqual(open_connection.await_args.args, ("example.test", 443))
            self.assertEqual(open_connection.await_args.kwargs["server_hostname"], "example.test")
            self.assertEqual(open_connection.await_args.kwargs["ssl_handshake_timeout"], 300)
            self.assertIsNotNone(open_connection.await_args.kwargs["ssl"])

        asyncio.run(run_case())

    def test_payload_omits_tool_controls_without_tools(self):
        request = ChatLLMRequest(
            messages=[{"role": "user", "content": "压缩这段上下文"}],
            tool_choice="none",
            parallel_tool_calls=False,
        )
        payload = ChatLLM._build_request_payload(
            request,
            stream=False,
            model_name="test-model",
        )

        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("parallel_tool_calls", payload)


class _FakeSocket:
    """最小 socket 替身：makefile 返回预置的响应字节流。"""

    def __init__(self, response: bytes):
        self._stream = __import__("io").BytesIO(response)
        self.sent = b""

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode):
        return self._stream

    def close(self):
        pass


class ChatLLMNonStreamResponseTests(unittest.TestCase):
    """非流式路径（压缩使用）必须正确解码 chunked 响应体。

    回归背景：LLM 网关对大响应普遍使用 Transfer-Encoding: chunked，
    直接 f.read() 会拿到 `<hex-size>\\r\\n{...}\\r\\n0\\r\\n\\r\\n` 的原始
    分块格式，json.loads 报 'Expecting value: line 1 column 1 (char 0)'。
    """

    def _model_config(self) -> dict:
        return {
            "url": "http://upstream.test/v1",
            "apiKey": "test-key",
            "apiType": "chat-completions",
            "selected_model_id": "test-model",
            "selected_model_name": "Test Model",
        }

    def _request(self) -> ChatLLMRequest:
        return ChatLLMRequest(messages=[{"role": "user", "content": "压缩"}], max_tokens=64)

    def test_read_full_response_body_decodes_chunked_fragments(self):
        import io
        body = (
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )
        result = ChatLLM._read_full_response_body(io.BytesIO(body), is_chunked=True)
        self.assertEqual(result, b"hello world")

    def test_read_full_response_body_passthrough_when_not_chunked(self):
        import io
        raw = b'{"id": "x"}'
        result = ChatLLM._read_full_response_body(io.BytesIO(raw), is_chunked=False)
        self.assertEqual(result, raw)

    def test_non_stream_chat_completions_parses_chunked_json_response(self):
        payload = json.dumps({
            "id": "cmpl-1",
            "choices": [{
                "message": {"role": "assistant", "content": "压缩完成"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }).encode("utf-8")
        chunked_http = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + hex(len(payload))[2:].encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
        )
        fake_socket = _FakeSocket(chunked_http)
        with patch("chat.chat_llm.socket.create_connection", return_value=fake_socket):
            result = ChatLLM.chat_completions(
                request=self._request(),
                stream=False,
                model_config=self._model_config(),
            )
        self.assertEqual(result["content"], "压缩完成")
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(result["usage"]["total_tokens"], 15)

    def test_non_stream_json_decode_error_includes_body_preview(self):
        bad_body = b"<html>Bad Gateway</html>"
        chunked_http = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + hex(len(bad_body))[2:].encode() + b"\r\n" + bad_body + b"\r\n0\r\n\r\n"
        )
        fake_socket = _FakeSocket(chunked_http)
        with patch("chat.chat_llm.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(RuntimeError) as ctx:
                ChatLLM.chat_completions(
                    request=self._request(),
                    stream=False,
                    model_config=self._model_config(),
                )
        message = str(ctx.exception)
        self.assertIn("不是有效 JSON", message)
        self.assertIn("<html>", message)


if __name__ == "__main__":
    unittest.main()
