import asyncio
import copy
import io
import json
import unittest
from unittest.mock import patch

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from util import media_limits as m
from factory.agent_runtime.builtin_tools import collect_injected_media_keys, execute_read_media
from test_http_retry_and_stats_snapshot import _FakeSocket, _sse_http, _chunked_http, _model_config


def media(kind, label='data:test'):
    key = {'image': 'image_url', 'video': 'video_url', 'audio': 'input_audio'}[kind]
    return {'type': key, key: {'url': label}}


def request():
    return ChatLLMRequest(messages=[{'role': 'user', 'content': [media('image'), media('image'), media('video')]}], max_tokens=32)


OK = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n' + b'data: [DONE]\n'
ERROR = 'Streaming response failed: [400] At most 0 video(s) may be provided in one prompt. (parameter=video)'


class MediaLimitTests(unittest.TestCase):
    def setUp(self):
        m._cache.clear()

    def test_supported_models_unchanged(self):
        messages = [{'role': 'user', 'content': [media('image'), media('image'), media('video')]}]
        result, dropped = m.limit_media_messages(messages, _model_config())
        self.assertEqual(result, messages)
        self.assertEqual(dropped, 0)

    def test_quota_notice_is_idempotent_and_text_only_retry_is_unchanged(self):
        config = dict(_model_config(), media_limits={'image': 1})
        text = [{'role': 'user', 'content': 'request'}, {'role': 'assistant', 'content': 'prefix'},
                {'role': 'user', 'content': 'resume'}]
        self.assertEqual(m.limit_media_messages(text, config), (text, 0))
        first, _ = m.limit_media_messages([{'role': 'user', 'content': [media('image')]}], config)
        second, _ = m.limit_media_messages(first, config)
        self.assertEqual(first, second)

    def test_learned_limits_are_isolated_by_protocol(self):
        chat_config = _model_config()
        self.assertTrue(m.learn_media_limit(ERROR, request().model_dump()['messages'], chat_config))
        messages_config = dict(chat_config, apiType='messages')
        self.assertEqual(m.get_media_limits(messages_config), {'audio': 0, 'video': 0})

    def test_zero_video_does_not_remove_supported_images(self):
        messages = [{'role': 'user', 'content': [media('image'), media('video')]}]
        self.assertTrue(m.learn_media_limit(ERROR, messages, _model_config()))
        result, dropped = m.limit_media_messages(messages, _model_config())
        self.assertEqual(dropped, 1)
        self.assertEqual(result[0]['content'][0]['type'], 'image_url')
        self.assertEqual(result[0]['content'][1]['type'], 'text')
        self.assertIn('video≤0', result[-1]['content'])
        self.assertFalse(m.learn_media_limit(ERROR, result, _model_config()))

    def test_total_limit_prefers_latest_and_dropped_keys_are_re_readable(self):
        messages = [{'_internal': True, '_media_injected_keys': ['old'], 'role': 'user',
                     'content': [{'type': 'text', 'text': 'old'}, media('image', 'OLD')]},
                    {'_internal': True, '_media_injected_keys': ['new', 'later'], 'role': 'user',
                     'content': [{'type': 'text', 'text': 'new'}, media('image', 'NEW'), media('image', 'LATER')]}]
        original = copy.deepcopy(messages)
        result, dropped = m.limit_media_messages(messages, {'media_limits': {'total': 1}})
        self.assertEqual(dropped, 2)
        self.assertEqual(collect_injected_media_keys(result), {'new'})
        self.assertEqual(messages, original)
        self.assertNotIn('OLD', result[0]['content'][1]['text'])

    def test_config_limits_and_endpoint_isolation(self):
        config = _model_config()
        config['selected_model'] = {'mediaLimits': {'total': 1, 'video': 0}}
        self.assertEqual(m.get_media_limits(config), {'total': 1, 'video': 0})
        self.assertTrue(m.learn_media_limit(ERROR, request().model_dump()['messages'], _model_config()))
        self.assertEqual(m.get_media_limits(dict(_model_config(), url='http://other')), {})

    def test_unrelated_error_never_discards_media(self):
        self.assertFalse(m.learn_media_limit('HTTP 400 invalid tool_call_id', [], _model_config()))
        self.assertEqual(m.get_media_limits(_model_config()), {})

    def test_document_zero_preserves_images_and_parsed_text(self):
        messages = [
            {'role': 'tool', 'tool_call_id': 'doc1', 'content': '文档解析正文'},
            {'role': 'user', 'content': [media('image'),
                {'type': 'file', 'file': {'filename': 'a.pdf', 'file_data': 'data:application/pdf;base64,TEST'}}]},
        ]
        original = copy.deepcopy(messages)
        result, dropped = m.limit_media_messages(messages, {'mediaLimits': {'document': 0}})
        self.assertEqual(dropped, 1)
        self.assertEqual(result[0], messages[0])
        self.assertEqual(result[1]['content'][0]['type'], 'image_url')
        self.assertEqual(result[1]['content'][1]['type'], 'text')
        self.assertNotIn('base64', result[1]['content'][1]['text'])
        self.assertEqual(messages, original)

    def test_total_is_shared_between_documents_and_images(self):
        messages = [{'role': 'user', 'content': [
            {'type': 'file', 'file': {'filename': 'pages.pdf', 'file_data': 'data:test'}},
            media('image'),
        ]}]
        result, dropped = m.limit_media_messages(messages, {'mediaLimits': {'total': 1, 'document': 1}})
        self.assertEqual(dropped, 1)
        self.assertEqual(result[0]['content'][0]['type'], 'file')
        self.assertEqual(result[0]['content'][1]['type'], 'text')

    def test_read_tool_returns_skipped_files_instead_of_claiming_injection(self):
        def load(_sid, reference, **kwargs):
            return {'kind': 'video' if reference.endswith('.gif') else 'image', 'source_type': 'local'}
        result = execute_read_media({'references': ['a.png', 'b.gif', 'c.png']}, 'test',
                                    load_media=load, media_limits={'total': 1, 'video': 0})
        self.assertEqual([item['reference'] for item in result['loaded']], ['a.png'])
        self.assertEqual(result['injected_references'], ['a.png'])
        self.assertEqual([item['reference'] for item in result['skipped']], ['b.gif', 'c.png'])
        self.assertTrue(all(item['reason'] == 'provider_media_limit' for item in result['skipped']))

    def test_image_limit_one_preserves_tools_and_text(self):
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'call1'}]},
                    {'role': 'tool', 'tool_call_id': 'call1', 'content': 'read result'},
                    {'role': 'user', 'content': [{'type': 'text', 'text': 'read these'}, media('image'), media('image')]}]
        config = _model_config()
        self.assertTrue(m.learn_media_limit('At most 1 image(s) may be provided', messages, config))
        result, dropped = m.limit_media_messages(messages, config)
        self.assertEqual(dropped, 1)
        self.assertEqual(result[:2], messages[:2])
        self.assertEqual(result[2]['content'][0]['text'], 'read these')

    def test_nonstream_http_downgrade(self):
        good = _chunked_http(b'200 OK', b'{"choices":[{"message":{"content":"ok"}}]}')
        sock = _FakeSocket([_chunked_http(b'400 Bad Request', json.dumps({'error': ERROR}).encode()), good])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            result = ChatLLM.chat_completions(request(), stream=False, model_config=_model_config())
        self.assertEqual(result['content'], 'ok')
        self.assertNotIn('video_url', sock.sent[1].decode())

    def test_http_and_sse_error_retry_with_corrected_payload(self):
        for error_response in [_chunked_http(b'400 Bad Request', json.dumps({'error': ERROR}).encode()),
                               _sse_http(('data: ' + json.dumps({'error': {'message': ERROR}}) + '\n').encode())]:
            with self.subTest(response=error_response[:30]):
                m._cache.clear()
                sock = _FakeSocket([error_response, _sse_http(OK)])
                with patch('chat.chat_llm.socket.create_connection', return_value=sock):
                    output = ''.join(ChatLLM.std_completions_sse(request(), _model_config()))
                self.assertIn('MEDIA_LIMIT_DOWNGRADE', output)
                self.assertIn('"content": "ok"', output)
                self.assertEqual(sock.closed, 2)
                payloads = [json.loads(data.split(b'\r\n\r\n', 1)[1]) for data in sock.sent]
                self.assertIn('video_url', json.dumps(payloads[0]))
                self.assertNotIn('video_url', json.dumps(payloads[1]))
                self.assertEqual(sum(p.get('type') == 'image_url' for p in payloads[1]['messages'][0]['content']), 2)

    def test_retry_after_partial_content_uses_continuation(self):
        body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
        body += ('data: ' + json.dumps({'error': {'message': ERROR}}) + '\n').encode()
        sock = _FakeSocket([_sse_http(body), _sse_http(OK)])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            output = ''.join(ChatLLM.std_completions_sse(request(), _model_config()))
        self.assertIn('upstream_error_frame', output)
        self.assertEqual(sock.closed, 2)
        resumed = json.loads(sock.sent[1].split(b'\r\n\r\n', 1)[1])
        self.assertEqual(resumed['messages'][-2]['content'], 'partial')
        self.assertIn('中断处继续', resumed['messages'][-1]['content'])

    def test_native_doc_format_error_detection(self):
        file_error = ('Invalid request format: 3 validation errors for GPT3Message\n'
                        'content.list[nullable[union[str,MMGPT3Item]]].1.MMGPT3Item.file\n'
                        "  Input should be a valid string [type=string_type, input_value={'type': 'file'}, input_type=dict]")
        self.assertTrue(m.native_doc_format_error(file_error))
        self.assertFalse(m.native_doc_format_error('[空响应体] log-id=- endpoint-id=-'))
        self.assertFalse(m.learn_media_limit('HTTP 400 invalid tool_call_id', [], _model_config()))
        self.assertFalse(m.native_doc_format_error('HTTP 400 invalid tool_call_id'))
        self.assertFalse(m.native_doc_format_error(''))
        self.assertFalse(m.native_doc_format_error(None))

    def test_downgrade_native_doc_to_text(self):
        import base64
        body = base64.b64encode('张三\n电话 13800000000'.encode('utf-8')).decode()
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': '请看简历'},
            {'type': 'file', 'file': {'filename': 'resume.txt', 'file_data': 'data:text/plain;base64,' + body}},
        ]}]
        original = copy.deepcopy(messages)
        result = m.downgrade_native_doc_to_text(messages)
        self.assertIsNotNone(result)
        self.assertEqual(messages, original)
        self.assertEqual(result[0]['content'][0], {'type': 'text', 'text': '请看简历'})
        downgraded = result[0]['content'][1]
        self.assertEqual(downgraded['type'], 'text')
        self.assertIn('resume.txt', downgraded['text'])
        self.assertIn('张三', downgraded['text'])
        self.assertIn('13800000000', downgraded['text'])
        self.assertNotIn('base64', downgraded['text'])
        # 无 file 部件 → None（不触发降级）
        self.assertIsNone(m.downgrade_native_doc_to_text([{'role': 'user', 'content': 'hi'}]))
        self.assertIsNone(m.downgrade_native_doc_to_text([]))
        # string 形态 file（网关原生格式）不动
        self.assertIsNone(m.downgrade_native_doc_to_text(
            [{'role': 'user', 'content': [{'type': 'file', 'file': 'file-abc123'}]}]))
        # 解析失败 → 明确占位而非抛错
        bad = [{'role': 'user', 'content': [{'type': 'file', 'file': {'filename': 'x.bin', 'file_data': '!!!not-base64!!!'}}]}]
        failed = m.downgrade_native_doc_to_text(bad)
        self.assertIn('解析失败', failed[0]['content'][0]['text'])

    def test_downgrade_combined_gate(self):
        file_error = 'MMGPT3Item.file Input should be a valid string'
        self.assertIsNone(m.downgrade_native_doc_on_format_error('偶发 400', [{'role': 'user', 'content': 'hi'}]))
        self.assertIsNone(m.downgrade_native_doc_on_format_error(file_error, [{'role': 'user', 'content': 'hi'}]))

    def test_sse_file_format_400_downgrades_to_text_and_retries(self):
        import base64
        body = base64.b64encode('李四简历正文'.encode('utf-8')).decode()
        file_error = ('Invalid request format: 3 validation errors for GPT3Message\n'
                        'content.list[nullable[union[str,MMGPT3Item]]].1.MMGPT3Item.file\n'
                        '  Input should be a valid string')
        req = ChatLLMRequest(messages=[{'role': 'user', 'content': [
            {'type': 'text', 'text': '请看附件'},
            {'type': 'file', 'file': {'filename': 'resume.txt', 'file_data': 'data:text/plain;base64,' + body}},
        ]}], max_tokens=32)
        sock = _FakeSocket([_chunked_http(b'400 Bad Request', json.dumps({'error': file_error}).encode()), _sse_http(OK)])
        with patch('chat.chat_llm.socket.create_connection', return_value=sock):
            output = ''.join(ChatLLM.std_completions_sse(req, _model_config()))
        self.assertIn('NATIVE_DOC_TEXT_DOWNGRADE', output)
        self.assertIn('"content": "ok"', output)
        payloads = [json.loads(data.split(b'\r\n\r\n', 1)[1]) for data in sock.sent]
        self.assertEqual(len(payloads), 2)
        self.assertIn('file_data', json.dumps(payloads[0]))
        second = json.dumps(payloads[1], ensure_ascii=False)
        self.assertNotIn('file_data', second)
        self.assertIn('李四简历正文', second)

    def test_async_sse_downgrade(self):
        async def run():
            responses = [_sse_http(('data: ' + json.dumps({'error': {'message': ERROR}}) + '\n').encode()), _sse_http(OK)]
            sent = []
            class Reader:
                def __init__(self, raw): self.stream = io.BytesIO(raw)
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
                result = ''.join([chunk async for chunk in ChatLLM.async_std_completions_sse(request(), model_config=_model_config())])
            self.assertIn('"content": "ok"', result)
            self.assertEqual(len(sent), 2)
            self.assertNotIn('video_url', sent[1].decode())
        asyncio.run(run())
