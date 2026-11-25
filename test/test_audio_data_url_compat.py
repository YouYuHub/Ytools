"""input_audio 双格式兼容回归：OpenAI 纯 base64 vs DashScope data URL。

背景（用户实测 opencode 网关转发 qwen4-max）：DashScope compatible-mode
把 input_audio.data 的纯 base64 当 URL 校验，报「The provided URL does not
appear to be valid」并进入 20 次网络重试死循环。修复两条防线：
1. normalize_input_audio_for_endpoint：dashscope host 发送前主动转 data URL；
2. upgrade_audio_on_url_error：任意端点命中确定性签名时经 retry_messages
   通道转换重发一次（不计网络重试），与 NATIVE_DOC_TEXT_DOWNGRADE 同模式。
"""
import copy
import json
import unittest
from unittest.mock import patch

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from util import media_limits as m
from test_http_retry_and_stats_snapshot import _FakeSocket, _sse_http, _chunked_http, _model_config

URL_ERROR = ('{"error":{"message":"The provided URL does not appear to be valid. '
             'Ensure it is correctly formatted.","type":"invalid_request_error",'
             '"param":null,"code":"invalid_parameter_error"}}')
OK = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n' + b'data: [DONE]\n'


def audio_messages(data='UklGRg==', fmt='wav'):
    return [{'role': 'user', 'content': [
        {'type': 'text', 'text': '识别音频'},
        {'type': 'input_audio', 'input_audio': {'data': data, 'format': fmt}},
    ]}]


class AudioDataUrlUnitTests(unittest.TestCase):
    def test_convert_pure_base64_to_data_url_with_mime(self):
        messages = audio_messages()
        original = copy.deepcopy(messages)
        result = m._convert_audio_parts(messages)
        self.assertIsNotNone(result)
        self.assertEqual(messages, original)
        self.assertEqual(result[0]['content'][1]['input_audio']['data'], 'data:audio/wav;base64,UklGRg==')
        self.assertEqual(result[0]['content'][1]['input_audio']['format'], 'wav')
        # 幂等：已是 data URL 不再转换
        self.assertIsNone(m._convert_audio_parts(result))

    def test_unknown_format_uses_empty_mime(self):
        result = m._convert_audio_parts(audio_messages(fmt='xyz'))
        self.assertEqual(result[0]['content'][1]['input_audio']['data'], 'data:;base64,UklGRg==')

    def test_url_and_reference_data_never_converted(self):
        for data in ('https://h/a.wav', 'http://h/a.wav', 'data:audio/wav;base64,X', 'media://a.wav'):
            self.assertIsNone(m._convert_audio_parts(audio_messages(data=data)), data)
        self.assertIsNone(m._convert_audio_parts([{'role': 'user', 'content': 'hi'}]))
        self.assertIsNone(m._convert_audio_parts([]))

    def test_normalize_only_targets_dashscope_host(self):
        messages = audio_messages()
        dashscope = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        converted = m.normalize_input_audio_for_endpoint(messages, dashscope)
        self.assertTrue(converted[0]['content'][1]['input_audio']['data'].startswith('data:audio/wav;base64,'))
        # OpenAI / opencode 等其它端点保持纯 base64 原样（同一对象）
        for url in ('https://api.openai.com/v1/chat/completions', 'https://opencode.example/v1', '', None):
            self.assertIs(m.normalize_input_audio_for_endpoint(messages, url), messages)
        # 伪造相似域名不命中
        self.assertIs(m.normalize_input_audio_for_endpoint(
            messages, 'https://dashscope.aliyuncs.com.evil.io/v1'), messages)

    def test_signature_detection_is_specific(self):
        self.assertTrue(m.audio_base64_url_error(URL_ERROR))
        self.assertFalse(m.audio_base64_url_error('HTTP 400 invalid tool_call_id'))
        self.assertFalse(m.audio_base64_url_error('At most 0 video(s) may be provided'))
        self.assertFalse(m.audio_base64_url_error(''))
        self.assertFalse(m.audio_base64_url_error(None))

    def test_upgrade_gate_requires_signature_and_convertible_part(self):
        self.assertIsNone(m.upgrade_audio_on_url_error('偶发 400', audio_messages()))
        self.assertIsNone(m.upgrade_audio_on_url_error(URL_ERROR, [{'role': 'user', 'content': 'hi'}]))
        upgraded = m.upgrade_audio_on_url_error(URL_ERROR, audio_messages())
        self.assertIsNotNone(upgraded)
        # 二次调用无部件可转 → None（无循环风险）
        self.assertIsNone(m.upgrade_audio_on_url_error(URL_ERROR, upgraded))


class AudioDataUrlRetryTests(unittest.TestCase):
    def setUp(self):
        m._cache.clear()

    def request(self):
        return ChatLLMRequest(messages=audio_messages(), max_tokens=32)

    def test_sync_sse_http400_upgrades_and_retries_without_network_count(self):
        sock = _FakeSocket([_chunked_http(b'400 Bad Request', URL_ERROR.encode()), _sse_http(OK)])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            output = ''.join(ChatLLM.std_completions_sse(self.request(), _model_config()))
        self.assertIn('AUDIO_DATA_URL_UPGRADE', output)
        self.assertIn('"content": "ok"', output)
        payloads = [json.loads(data.split(b'\r\n\r\n', 1)[1]) for data in sock.sent]
        self.assertEqual(len(payloads), 2)
        first = payloads[0]['messages'][0]['content'][1]['input_audio']['data']
        second = payloads[1]['messages'][0]['content'][1]['input_audio']['data']
        self.assertEqual(first, 'UklGRg==')
        self.assertEqual(second, 'data:audio/wav;base64,UklGRg==')

    def test_sync_sse_error_frame_upgrades(self):
        body = ('data: ' + URL_ERROR + '\n').encode()
        sock = _FakeSocket([_sse_http(body), _sse_http(OK)])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            output = ''.join(ChatLLM.std_completions_sse(self.request(), _model_config()))
        self.assertIn('AUDIO_DATA_URL_UPGRADE', output)
        self.assertIn('"content": "ok"', output)
        self.assertEqual(sock.closed, 2)

    def test_nonstream_http400_upgrades(self):
        good = _chunked_http(b'200 OK', b'{"choices":[{"message":{"content":"ok"}}]}')
        sock = _FakeSocket([_chunked_http(b'400 Bad Request', URL_ERROR.encode()), good])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            result = ChatLLM.chat_completions(self.request(), stream=False, model_config=_model_config())
        self.assertEqual(result['content'], 'ok')
        payload = json.loads(sock.sent[1].split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload['messages'][0]['content'][1]['input_audio']['data'],
                         'data:audio/wav;base64,UklGRg==')

    def test_dashscope_endpoint_converts_proactively_single_attempt(self):
        # 用 http scheme：_FakeSocket 不做 TLS 握手；normalize 只看 host
        config = dict(_model_config(), url='http://dashscope.aliyuncs.com/compatible-mode/v1')
        sock = _FakeSocket([_sse_http(OK)])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            output = ''.join(ChatLLM.std_completions_sse(self.request(), config))
        self.assertIn('"content": "ok"', output)
        self.assertNotIn('AUDIO_DATA_URL_UPGRADE', output)  # 无需失败重试
        payload = json.loads(sock.sent[0].split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload['messages'][0]['content'][1]['input_audio']['data'],
                         'data:audio/wav;base64,UklGRg==')

    def test_async_sse_http400_upgrades(self):
        import asyncio
        async def run():
            responses = [_chunked_http(b'400 Bad Request', URL_ERROR.encode()), _sse_http(OK)]
            sent = []
            class Reader:
                def __init__(self, raw): self.stream = __import__('io').BytesIO(raw)
                async def readline(self): return self.stream.readline()
                async def readexactly(self, count): return self.stream.read(count)
                async def read(self, count=-1): return self.stream.read(count)
            class Writer:
                def write(self, data): sent.append(data)
                async def drain(self): pass
                def close(self): pass
                async def wait_closed(self): pass
            async def connect(*args, **kwargs): return Reader(responses.pop(0)), Writer()
            with patch.object(ChatLLM, '_open_connection', side_effect=connect):
                result = ''.join([chunk async for chunk in ChatLLM.async_std_completions_sse(
                    self.request(), model_config=_model_config())])
            self.assertIn('AUDIO_DATA_URL_UPGRADE', result)
            self.assertIn('"content": "ok"', result)
            self.assertEqual(len(sent), 2)
            self.assertIn('data:audio/wav;base64,', sent[1].decode())
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
