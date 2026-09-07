"""HTTP 状态码错误重试与 token 统计进行中轮次快照测试：

- 流式请求收到非 2xx（如瞬时 400 Bad Request）时纳入 NETWORK_RETRY_MAX_ATTEMPTS
  重试：未达上限发 retrying=True 帧并重连重发；达到上限发 retrying=False 终止帧；
- 非流式路径仍直接抛 RuntimeError（压缩调用方依赖异常语义）；
- get_context_token_stats 在主进程（worker 模式）下读取 .pending 检查点
  快照，把进行中轮次纳入统计；
- 检查点写者已死亡（崩溃遗留）时快照不计入统计。
运行：python test\test_http_retry_and_stats_snapshot.py
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat import chat_llm as chat_llm_mod  # noqa: E402
from chat.chat_llm import ChatLLM  # noqa: E402
from config import ChatLLMRequest  # noqa: E402


def _sse_http(body: bytes) -> bytes:
    """把 SSE 正文（以 \n 分隔的完整行）编码为 chunked HTTP 响应。

    注意：必须整段作为一个 chunk 发送（行尾的 \n 保留在 chunk 内），
    与真实网关行为一致；逐行拆 chunk 会丢行尾换行，SSE 解析器
    按行切分时会把所有帧粘在一起。
    """
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    )


def _chunked_http(status: bytes, payload: bytes) -> bytes:
    return (
        b"HTTP/1.1 " + status + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + hex(len(payload))[2:].encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
    )


class _FakeSocket:
    """顺序返回多个预置响应的 socket 替身（每次 makefile 消耗一个）。"""

    def __init__(self, responses: list[bytes]):
        self._streams = [io.BytesIO(r) for r in responses]
        self.sent: list[bytes] = []
        self.closed = 0

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode):
        return self._streams.pop(0)

    def close(self):
        self.closed += 1


async def _nop_await(value):
    """把同步返回值包装成可 await 的结果（mock async 函数用）。"""
    return value


def _async_return(value):
    """返回一个立即完成的 awaitable（不产生协程警告）。"""
    future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future


def _model_config() -> dict:
    return {
        "url": "http://upstream.test/v1",
        "apiKey": "test-key",
        "apiType": "chat-completions",
        "selected_model_id": "test-model",
        "selected_model_name": "Test Model",
    }


def _request() -> ChatLLMRequest:
    return ChatLLMRequest(messages=[{"role": "user", "content": "你好"}], max_tokens=64)


class HttpRetryStreamTests(unittest.TestCase):
    def setUp(self):
        chat_llm_mod.load_var = lambda name, default=None: (
            3 if name == "NETWORK_RETRY_MAX_ATTEMPTS" else default
        )

    def tearDown(self):
        import env_manager

        chat_llm_mod.load_var = env_manager.load_var

    def test_http_400_retries_then_succeeds(self):
        ok_body = (
            b'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n'
            b"data: [DONE]\n"
        )
        responses = [
            _chunked_http(b"400 Bad Request", b'{"error":"bad request"}'),
            _sse_http(ok_body),
        ]
        fake_socket = _FakeSocket(responses)
        with patch("chat.chat_llm.socket.create_connection", return_value=fake_socket):
            chunks = list(ChatLLM.std_completions_sse(
                request=_request(), model_config=_model_config()
            ))
        retry_frames = [json.loads(c[6:]) for c in chunks if c.startswith("data: {") and '"retrying": true' in c]
        self.assertEqual(len(retry_frames), 1)
        self.assertTrue(retry_frames[0]["retrying"])
        self.assertEqual(retry_frames[0]["error_type"], "http")
        self.assertIn("400", retry_frames[0]["error"])
        self.assertIn("hi", "".join(chunks))  # 重试后内容正常返回
        self.assertEqual(fake_socket.closed, 2)  # 第一次连接被关闭后重连
        self.assertNotIn("已重试 3 次仍失败", "".join(chunks))

    def test_http_400_gives_up_after_max_attempts(self):
        responses = [
            _chunked_http(b"400 Bad Request", b'{"error":"bad request"}')
            for _ in range(4)  # 首次 + 3 次重试全部失败
        ]
        fake_socket = _FakeSocket(responses)
        with patch("chat.chat_llm.socket.create_connection", return_value=fake_socket):
            chunks = list(ChatLLM.std_completions_sse(
                request=_request(), model_config=_model_config()
            ))
        frames = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
        terminal = [f for f in frames if f.get("retrying") is False]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["error_type"], "http")
        self.assertEqual(terminal[0]["retry"], 4)
        self.assertEqual(terminal[0]["max_attempts"], 3)
        self.assertEqual(fake_socket.closed, 4)

    def test_async_http_400_retries_then_succeeds(self):
        ok_body = (
            b'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n'
            b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            b"data: [DONE]\n"
        )

        async def run_case():
            responses = [
                _chunked_http(b"400 Bad Request", b'{"error":"bad request"}'),
                _sse_http(ok_body),
            ]

            class _FakeWriter:
                def write(self, data):
                    pass

                async def drain(self):
                    pass

                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            class _FakeStreamReader:
                """单次连接的 reader 替身：只消费一个预置响应流。"""

                def __init__(self, stream: io.BytesIO):
                    self._stream = stream

                async def readline(self):
                    return self._stream.readline()

                async def readexactly(self, n):
                    return self._stream.read(n)

                async def read(self, n=-1):
                    return self._stream.read(n)

            # 真实行为：每次尝试（含重试）都新建连接 → 每次返回全新 reader
            readers = [_FakeStreamReader(io.BytesIO(r)) for r in responses]
            open_calls = {"n": 0}

            async def fake_open_connection(*args, **kwargs):
                i = min(open_calls["n"], len(readers) - 1)
                open_calls["n"] += 1
                return readers[i], _FakeWriter()

            with patch(
                "chat.chat_llm.ChatLLM._open_connection",
                side_effect=fake_open_connection,
            ):
                chunks = []
                async for chunk in ChatLLM.async_std_completions_sse(
                    request=_request(), model_config=_model_config()
                ):
                    chunks.append(chunk)
                return chunks

        chunks = asyncio.run(run_case())
        frames = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
        retry_frames = [f for f in frames if f.get("retrying") is True]
        self.assertEqual(len(retry_frames), 1)
        self.assertEqual(retry_frames[0]["error_type"], "http")
        self.assertIn("hi", "".join(chunks))
        self.assertTrue(any(f.get("finish_reason") == "stop" for f in frames))


class NonStreamUnchangedTests(unittest.TestCase):
    """非流式路径（压缩用）保持异常语义：非 2xx 直接抛 RuntimeError。"""

    def test_non_stream_http_400_raises_runtime_error(self):
        payload = b'{"error":"bad request"}'
        chunked_http = _chunked_http(b"400 Bad Request", payload)
        fake_socket = _FakeSocket([chunked_http])
        with patch("chat.chat_llm.socket.create_connection", return_value=fake_socket):
            with self.assertRaises(RuntimeError) as ctx:
                ChatLLM.chat_completions(
                    request=_request(),
                    stream=False,
                    model_config=_model_config(),
                )
        self.assertIn("HTTP Error", str(ctx.exception))


class StatsPendingSnapshotTests(unittest.TestCase):
    """token 统计兜底读取检查点快照（worker 模式下任务进行中增量可见）。"""

    SESSION = "stats_snapshot_ut"

    def setUp(self):
        from memory import chat_memory

        self.cm = chat_memory
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)

    def tearDown(self):
        self.cm.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_token_stats_includes_live_pending_round_from_checkpoint(self):
        manager = self.cm.ChatMemoryManager(self.SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "进行中的一轮提问"}))
        # 本进程内 pending_round 已存在（不走检查点兜底），先清掉以模拟
        # 主进程视角（生成任务在 worker 内存里）
        manager._round_store.reset()
        stats = asyncio.run(manager.get_context_token_stats(max_rounds=10))
        # 本进程写者仍存活 → 检查点快照应计入统计
        self.assertEqual(stats["rounds"]["total"], 1)
        self.assertGreater(stats["request_context_tokens"], 0)

    def test_dead_writer_checkpoint_not_counted(self):
        manager = self.cm.ChatMemoryManager(self.SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "崩溃遗留轮"}))
        manager._round_store.reset()
        # 把检查点写者 PID 改成不存在的进程，模拟崩溃遗留
        sidecar = manager._pending_checkpoint_path
        checkpoint = json.loads(sidecar.read_text(encoding="utf-8"))
        checkpoint["writer_pid"] = -2147483000
        sidecar.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
        snapshot = manager._read_pending_round_snapshot()
        self.assertIsNone(snapshot)
        stats = asyncio.run(manager.get_context_token_stats(max_rounds=10))
        self.assertEqual(stats["rounds"]["total"], 0)
        self.assertEqual(stats["request_context_tokens"], 0)

    def test_snapshot_round_only_user_question_fallback(self):
        # 只有 user 事件时统计应回退用 question 计入消息（不产生空轮）
        manager = self.cm.ChatMemoryManager(self.SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "只有提问"}))
        manager._round_store.reset()
        stats = asyncio.run(manager.get_context_token_stats(max_rounds=10))
        self.assertEqual(len(stats["round_tokens"]), 1)
        self.assertGreater(stats["round_tokens"][0]["tokens"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
