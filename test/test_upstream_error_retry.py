"""Provider error envelopes share the HTTP/network retry budget, sync and async."""
import asyncio
import io
import json
import unittest
from unittest.mock import patch

from chat import chat_llm as llm
from chat.chat_llm import ChatLLM
from test_http_retry_and_stats_snapshot import _FakeSocket, _sse_http, _chunked_http, _request, _model_config


OK = _sse_http(b'data: {"id":"ok","choices":[{"delta":{"content":"finished"}}]}\n'
               b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
               b'data: [DONE]\n')


def error_response(error=None):
    return _sse_http(('data:' + json.dumps(error or {"error": {"message": "流式请求时出错"}},
                                          ensure_ascii=False) + '\n\n').encode())


class ProviderErrorRetryTests(unittest.TestCase):
    def run_responses(self, responses, asynchronous, maximum=3, stop_after_retry=False):
        sent, closed = [], []
        chunks = []
        load = lambda key, default=None: maximum if key == "NETWORK_RETRY_MAX_ATTEMPTS" else default
        with patch.object(llm, "load_var", side_effect=load), patch.object(llm, "_NETWORK_RETRY_INTERVAL_SECONDS", 0):
            if not asynchronous:
                sock = _FakeSocket(responses)
                with patch.object(llm.socket, "create_connection", return_value=sock):
                    chunks = list(ChatLLM.std_completions_sse(_request(), _model_config()))
                sent, closed = sock.sent, [None] * sock.closed
            else:
                class Reader:
                    def __init__(self, raw): self.stream = io.BytesIO(raw)
                    async def readline(self): return self.stream.readline()
                    async def readexactly(self, size): return self.stream.read(size)
                    async def read(self, size=-1): return self.stream.read(size)
                class Writer:
                    def write(self, data): sent.append(data)
                    async def drain(self): pass
                    def close(self): closed.append(None)
                    async def wait_closed(self): pass
                pending = list(responses)
                async def connect(*args, **kwargs):
                    return Reader(pending.pop(0)), Writer()
                async def collect():
                    stop = False
                    with patch.object(ChatLLM, "_open_connection", side_effect=connect):
                        async for chunk in ChatLLM.async_std_completions_sse(
                                _request(), model_config=_model_config(), stop_checker=lambda: stop):
                            chunks.append(chunk)
                            if stop_after_retry and '"retrying": true' in chunk:
                                stop = True
                asyncio.run(collect())
        frames = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith('data: {')]
        payloads = [json.loads(data.split(b'\r\n\r\n', 1)[1]) for data in sent]
        return frames, payloads, len(closed)

    def test_error_variants_retry_then_succeed(self):
        variants = [{"error": {"message": "流式请求时出错", "type": "server_error"}},
                    {"message": "流式请求时出错"}, {"message": "gateway unavailable", "status_code": 503},
                    {"message": "oops", "success": False}, {"type": "error", "message": "unknown"},
                    {"error": "error with choices", "choices": [{"delta": {"content": "ignored"}}]}]
        for asynchronous in (False, True):
            for variant in variants:
                with self.subTest(asynchronous=asynchronous, variant=variant):
                    frames, payloads, closed = self.run_responses([error_response(variant), OK], asynchronous)
                    errors = [frame for frame in frames if frame.get("error")]
                    self.assertEqual(len(errors), 1)
                    self.assertTrue(errors[0]["retrying"])
                    self.assertEqual(errors[0]["error_type"], "upstream_error_frame")
                    self.assertEqual(errors[0]["retry"], 1)
                    self.assertTrue(any(frame.get("content") == "finished" for frame in frames))
                    self.assertEqual(payloads[0], payloads[1])
                    self.assertEqual(closed, 2)

    def test_exhaustion_has_exactly_one_terminal_error(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                frames, payloads, closed = self.run_responses([error_response()] * 4, asynchronous)
                terminal = [frame for frame in frames if frame.get("retrying") is False]
                self.assertEqual(len(terminal), 1)
                self.assertEqual(terminal[0]["retry"], 4)
                self.assertEqual(terminal[0]["max_attempts"], 3)
                self.assertIn("流式请求时出错", terminal[0]["error_detail"])
                self.assertEqual(len(payloads), 4)
                self.assertEqual(closed, 4)

    def test_mixed_http_and_error_frames_share_one_counter(self):
        responses = [_chunked_http(b'503 Busy 200 connections', b'busy'), error_response(), error_response()]
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                frames, payloads, closed = self.run_responses(responses, asynchronous, maximum=2)
                self.assertEqual([frame["retry"] for frame in frames if frame.get("error")], [1, 2, 3])
                self.assertFalse(frames[-1]["retrying"])
                self.assertEqual(len(payloads), 3)

    def test_unlimited_and_user_stop(self):
        for asynchronous in (False, True):
            frames, payloads, closed = self.run_responses([error_response()] * 5 + [OK], asynchronous, maximum=0)
            self.assertEqual(len(payloads), 6)
            self.assertFalse(any(frame.get("retrying") is False for frame in frames))
        frames, payloads, closed = self.run_responses([error_response(), OK], True, maximum=0, stop_after_retry=True)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(closed, 1)
        frames, payloads, closed = self.run_responses([error_response(), OK], True, maximum=-1)
        self.assertIsNone(frames[0]["max_attempts"])
        self.assertTrue(frames[0]["retrying"])

    def test_partial_response_retries_with_prefix_instead_of_replaying(self):
        partial = _sse_http(b'data:{"choices":[{"delta":{"content":"prefix"}}]}\n'
                            b'data:{"error":{"message":"failed"}}\n')
        for asynchronous in (False, True):
            frames, payloads, closed = self.run_responses([partial, OK], asynchronous)
            retry = next(frame for frame in frames if frame.get("retrying"))
            self.assertTrue(retry["resume_from_partial"])
            self.assertTrue(retry["discard_tool_calls"])
            self.assertEqual(payloads[1]["messages"][-2], {"role": "assistant", "content": "prefix"})
            self.assertEqual([frame["content"] for frame in frames if frame.get("content")], ["prefix", "finished"])

    def test_partial_tool_arguments_are_discarded_on_retry(self):
        partial = _sse_http(b'data:{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"bad",'
                            b'"function":{"name":"read_file","arguments":"{broken"}}]}}]}\n'
                            b'data:{"error":"failed"}\n')
        for asynchronous in (False, True):
            frames, payloads, closed = self.run_responses([partial, OK], asynchronous)
            retry = next(frame for frame in frames if frame.get("retrying"))
            self.assertTrue(retry["discard_tool_calls"])
            self.assertNotIn("{broken", json.dumps(payloads[1]))

    def test_event_error_is_recognized_and_usage_heartbeat_is_not(self):
        response = _sse_http(b'event:error\ndata:{"message":"unknown"}\n\n')
        for asynchronous in (False, True):
            frames, payloads, closed = self.run_responses([response, OK], asynchronous)
            self.assertEqual(len(payloads), 2)
            self.assertEqual(frames[0]["error_type"], "upstream_error_frame")
        self.assertIsNone(llm._upstream_error({"id": "c1", "usage": {"total_tokens": 2}}))
        self.assertIsNone(llm._upstream_error({"message": "processing", "type": "heartbeat"}))


if __name__ == "__main__":
    unittest.main()
