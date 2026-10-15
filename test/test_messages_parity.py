"""Parity regressions for existing Chat callers, retries and native tool replay."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

import env_manager
from chat.chat_llm import ChatLLM
from chat.protocols import native_store
from chat.protocols.messages import MessagesProtocolError, build_payload
from util.media_limits import get_media_limits, limit_media_messages
from test_messages_api import (
    CONFIG, SIGNED, TOOLS, Writer, decode, frames, http, isolated, request,
    response, sockets, sse,
)  # noqa: F401


def attachments():
    return [{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/a.png'}},
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/b.png'}}]


def run_transport(monkeypatch, req, responses, mode, config=CONFIG):
    if mode != 'async':
        sent = sockets(monkeypatch, responses)
        if mode == 'sync':
            result = decode(list(ChatLLM.std_completions_sse(req, model_config=config)))
        else:
            result = ChatLLM.chat_completions(req, stream=False, model_config=config)
        return result, [sock.payload() for sock in sent if sock.sent]

    async def run():
        scripts = iter(responses)
        writers = []
        async def connect(*args):
            reader = asyncio.StreamReader()
            reader.feed_data(next(scripts))
            reader.feed_eof()
            writers.append(Writer())
            return reader, writers[-1]
        monkeypatch.setattr(ChatLLM, '_open_connection', connect)
        result = decode([chunk async for chunk in ChatLLM.chat_completions(req, stream=True, model_config=config)])
        assert all(writer.closed for writer in writers)
        return result, [json.loads(writer.sent.split(b'\r\n\r\n', 1)[1]) for writer in writers]
    return asyncio.run(run())


def success(mode, content=None, reason='end_turn'):
    return http(json.dumps(response(content, reason)).encode()) if mode == 'nonstream' else http(sse(frames(content, reason)))


@pytest.mark.parametrize('mode', ['sync', 'async', 'nonstream'])
@pytest.mark.parametrize('envelope', ['http', 'sse'])
def test_supplier_media_count_error_corrects_payload_and_remembers_limit(monkeypatch, mode, envelope):
    if mode == 'nonstream' and envelope == 'sse':
        # A JSON error envelope is the nonstream equivalent of an SSE error.
        body = json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'At most 1 image(s)'}}).encode()
        error = http(body)
    else:
        body = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'At most 1 image(s)'}}
        error = http(json.dumps(body).encode(), status=400) if envelope == 'http' else http(sse([body]))
    req = request(messages=[{'role': 'user', 'content': attachments()}])
    original = copy.deepcopy(req.messages[0].content)
    result, sent = run_transport(monkeypatch, req, [error, success(mode)], mode)
    assert len(sent) == 2
    assert [part['type'] for part in sent[0]['messages'][0]['content']] == ['image', 'image']
    assert [part['type'] for part in sent[1]['messages'][0]['content']] == ['image', 'text']
    assert '未发送 image' in sent[1]['messages'][0]['content'][1]['text']
    assert req.messages[0].content == original
    assert get_media_limits(CONFIG)['image'] == 1
    cached, _, _ = build_payload(req, CONFIG, True)
    assert cached['messages'] == sent[1]['messages']
    if mode == 'nonstream':
        assert result['content'] == '你好'
    else:
        assert result[0]['warning']['code'] == 'MEDIA_LIMIT_DOWNGRADE'
        assert result[-1]['finish_reason'] == 'stop'


def test_nested_tool_result_quota_preserves_tool_pairing_and_text():
    content = [{'type': 'tool_result', 'tool_use_id': 'a', 'content': attachments()},
               {'type': 'tool_result', 'tool_use_id': 'b', 'is_error': True, 'content': 'failed'}]
    original = copy.deepcopy(content)
    limited, dropped = limit_media_messages([{'role': 'user', 'content': content}], dict(CONFIG, mediaLimits={'image': 1}))
    assert dropped == 1
    assert [part['tool_use_id'] for part in limited[0]['content']] == ['a', 'b']
    assert limited[0]['content'][1] == content[1]
    assert [part['type'] for part in limited[0]['content'][0]['content']] == ['image', 'text']
    assert content == original


@pytest.mark.parametrize('mode', ['sync', 'async', 'nonstream'])
def test_media_downgrade_never_edits_pending_native_prefix(monkeypatch, mode):
    req = request(messages=[{'role': 'user', 'content': attachments()}], tools=TOOLS)
    payload, _, _ = build_payload(req, CONFIG, True)
    ref = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, 'tool_use'))
    for call_id in ('tool_a', 'tool_b'):
        native_store.tool_completed(req.session_id, ref, call_id, 'already completed')
    pending = request(messages=[{'role': 'user', 'content': 'projection'},
                                {'role': 'assistant', 'provider_state': ref}], tools=TOOLS)
    error = http(b'{"error":"At most 1 image(s)"}', status=400)
    if mode == 'nonstream':
        with pytest.raises(RuntimeError, match='不能修改签名'):
            run_transport(monkeypatch, pending, [error], mode)
    else:
        result, sent = run_transport(monkeypatch, pending, [error], mode)
        assert len(sent) == 1 and result[-1]['non_retryable']
        assert '不能修改签名' in result[-1]['error']
    record = native_store.load_record(req.session_id, ref)
    assert record['messages'] == payload['messages'] and record['content'] == SIGNED
    assert all(status['status'] == 'completed' for status in record['tool_status'].values())


@pytest.mark.parametrize('mode', ['sync', 'async', 'nonstream'])
def test_completed_empty_response_retries_before_saving_native_record(monkeypatch, mode, tmp_path):
    result, sent = run_transport(monkeypatch, request(), [success(mode, []), success(mode)], mode)
    assert len(sent) == 2
    assert sent[1]['messages'][:-1] == sent[0]['messages']
    assert '上一轮没有返回正文' in sent[1]['messages'][-1]['content'][0]['text']
    assert len(list((tmp_path / 'native').rglob('*.json'))) == 1
    if mode == 'nonstream':
        assert result['content'] == '你好'
    else:
        assert result[0]['error_type'] == 'empty_response' and result[0]['retrying']
        assert result[-1]['finish_reason'] == 'stop'


@pytest.mark.parametrize('mode', ['sync', 'async', 'nonstream'])
def test_completed_empty_response_retry_limit_is_bounded(monkeypatch, mode, tmp_path):
    if mode == 'nonstream':
        with pytest.raises(RuntimeError, match='没有任何模型输出'):
            run_transport(monkeypatch, request(), [success(mode, []), success(mode, [])], mode)
    else:
        result, sent = run_transport(monkeypatch, request(), [success(mode, []), success(mode, [])], mode)
        assert len(sent) == 2
        assert [event['retrying'] for event in result] == [True, False]
    assert not list((tmp_path / 'native').rglob('*.json'))


@pytest.mark.parametrize('reason', ['refusal', 'max_tokens', 'model_context_window_exceeded', 'stop_sequence'])
def test_empty_special_stop_is_not_treated_as_transport_failure(monkeypatch, reason):
    result, sent = run_transport(monkeypatch, request(), [success('sync', [], reason)], 'sync')
    assert len(sent) == 1
    assert result[-1]['stop_reason'] == reason and not any(event.get('error') for event in result)


@pytest.mark.parametrize('runtime', ['main', 'sub_agent'])
def test_empty_tool_continuation_does_not_repeat_tools_and_preserves_prefix(monkeypatch, runtime):
    import test_todo_no_tools_regression as harness
    monkeypatch.setattr(harness._MemoryStub, 'update_current_round_usage', AsyncMock(), raising=False)
    from factory.agent_runtime.sub_agent import SubAgentRunner
    from test_sub_agent import _make_context
    async def run():
        native = copy.deepcopy(SIGNED)
        native[-2]['name'] = native[-1]['name'] = 'todo_write'
        native[-2]['input'] = {'todos': [{'id': '1', 'content': '步骤', 'status': 'done'}]}
        native[-1]['input'] = {'todos': [{'id': '1', 'content': '步骤', 'status': 'done'}]}
        scripts = iter([frames(native, 'tool_use'), frames([]), frames()])
        writers = []
        async def connect(*args):
            reader = asyncio.StreamReader()
            reader.feed_data(http(sse(next(scripts))))
            reader.feed_eof()
            writers.append(Writer())
            return reader, writers[-1]
        monkeypatch.setattr(ChatLLM, '_open_connection', connect)
        if runtime == 'main':
            stream = await harness._run_case(['todo_write'], llm=ChatLLM, resolve_config=lambda: dict(CONFIG, maxInputTokens=128000))
            records = harness._last_memory.records
            assert sum('tool_return' in chunk for chunk in stream.chunks) == 2
            assert sum(record.get('role') == 'tool' for record in records if isinstance(record, dict)) == 2
        else:
            context, emitted, _ = _make_context()
            runner = SubAgentRunner(context)
            monkeypatch.setattr(runner, '_resolve_request_model_config', lambda: (CONFIG, {}))
            result = await runner.run()
            assert result.status == 'done'
            assert sum(message.get('role') == 'tool' for message in runner.messages) == 2
        payloads = [json.loads(writer.sent.split(b'\r\n\r\n', 1)[1]) for writer in writers]
        assert len(payloads) == 3
        assert payloads[-1]['messages'][:-1] == payloads[-2]['messages']
        assert payloads[-1]['messages'][-3]['content'] == native
        assert '已完成的工具无需重复执行' in payloads[-1]['messages'][-1]['content'][0]['text']
        records = [json.loads(path.read_text(encoding='utf-8')) for path in native_store._root().rglob('*.json')]
        last = next(record for record in records if record.get('parent_id'))
        assert native_store.replay_messages(last['session_id'], last)[:-1] == payloads[-1]['messages']
    asyncio.run(run())


@pytest.mark.parametrize('stop', ['END', ['END', 'DONE']])
def test_chat_stop_and_json_schema_extras_convert_to_messages(stop):
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    extra = {'stop': stop, 'response_format': {'type': 'json_schema', 'json_schema': {'name': 'result', 'strict': True, 'schema': schema}}}
    req = request(extra_body=extra, output_config={'effort': 'medium'})
    payload, _, _ = build_payload(req, CONFIG, True)
    assert payload['stop_sequences'] == ([stop] if isinstance(stop, str) else stop)
    assert payload['output_config'] == {'effort': 'medium', 'format': {'type': 'json_schema', 'schema': schema}}
    assert 'stop' not in payload and 'response_format' not in payload
    assert req.extra_body == extra
    chat = ChatLLM._build_request_payload(req, True, 'chat-model')
    assert chat['stop'] == stop and chat['response_format'] == extra['response_format']


def test_native_parameters_win_over_chat_aliases_and_keep_cache_control():
    native_format = {'type': 'json_schema', 'schema': {'type': 'string'}}
    req = request(stop_sequences=['NATIVE'], output_config={'format': native_format},
                  extra_body={'stop': 42, 'response_format': {'type': 'json_object'},
                              'cache_control': {'type': 'ephemeral', 'ttl': '1h'}})
    payload, _, _ = build_payload(req, CONFIG, True)
    assert payload['stop_sequences'] == ['NATIVE']
    assert payload['output_config']['format'] == native_format
    assert payload['cache_control'] == {'type': 'ephemeral', 'ttl': '1h'}


@pytest.mark.parametrize('extra', [{'stop': 1}, {'stop': ['']}, {'response_format': {'type': 'json_object'}},
                                  {'response_format': {'type': 'json_schema'}}])
def test_invalid_chat_aliases_fail_without_network_retry(extra):
    result = decode(list(ChatLLM.std_completions_sse(request(extra_body=extra), model_config=CONFIG)))
    assert len(result) == 1 and result[0]['error_type'] == 'protocol' and result[0]['non_retryable']


def test_tool_choice_none_omits_parallel_control_and_strict_is_preserved():
    tools = copy.deepcopy(TOOLS)
    tools[0]['function']['strict'] = True
    req = request(tools=tools, tool_choice='none', parallel_tool_calls=False)
    payload, _, _ = build_payload(req, CONFIG, True)
    assert payload['tool_choice'] == {'type': 'none'} and payload['tools'][0]['strict'] is True
    assert ChatLLM._build_request_payload(req, True, 'chat-model')['tools'][0]['function']['strict'] is True
    with pytest.raises(MessagesProtocolError, match='未在本次 tools'):
        build_payload(request(tools=tools, tool_choice={'type': 'function', 'function': {'name': 'unknown'}}), CONFIG, True)


def test_extra_parameters_reach_title_compaction_and_sub_agent(monkeypatch):
    from factory.agent_runtime import title_generator, context_compaction
    from factory.agent_runtime.sub_agent import SubAgentRunner
    from test_sub_agent import _make_context
    params = {'extra_body': {'cache_control': {'type': 'ephemeral'}, 'stop': 'END'}}
    monkeypatch.setattr(title_generator, 'get_role_parameter', lambda role: params)
    monkeypatch.setattr(title_generator, 'resolve_title_model_config', lambda: (CONFIG, ''))
    monkeypatch.setattr(context_compaction, 'get_role_parameter', lambda role: params)
    monkeypatch.setattr(env_manager, 'model_selection', {'chat_model': {'api_type': 'messages', 'parameter': {'messages': params}}})
    context, _, _ = _make_context()
    runner = SubAgentRunner(context)
    requests = [title_generator._build_title_request('topic', 'messages_test'),
                context_compaction._build_summary_request('rules', 'context', 1024, 'messages_test'),
                runner._build_request(params), request(**env_manager.apply_role_parameter_defaults({}))]
    for req in requests:
        payload, _, _ = build_payload(req, CONFIG, True)
        assert payload['cache_control'] == {'type': 'ephemeral'} and payload['stop_sequences'] == ['END']


@pytest.mark.parametrize('source_api', ['messages', 'chat-completions'])
def test_compaction_fallback_resolves_parameters_for_actual_protocol(monkeypatch, source_api):
    from factory.agent_runtime import context_compaction as compaction
    target_api = 'chat-completions' if source_api == 'messages' else 'messages'
    primary = dict(CONFIG, apiType=source_api, selected_provider_name='Primary', selected_model_name='Primary',
                   selected_model_id='primary', maxInputTokens=128000)
    fallback = dict(CONFIG, apiType=target_api, selected_provider_name='Fallback', selected_model_name='Fallback',
                    selected_model_id='fallback', maxInputTokens=128000)
    native = {'thinking': {'type': 'adaptive'}, 'extra_body': {'cache_control': {'type': 'ephemeral'}}}
    chat = {'temperature': 0.3, 'extra_body': {'enable_thinking': False, 'custom_chat': True}}
    params = {'compaction_model': native if source_api == 'messages' else chat,
              'chat_model': native if target_api == 'messages' else chat}
    monkeypatch.setattr(compaction, 'resolve_context_compaction_model_config', lambda settings: primary)
    monkeypatch.setattr(compaction, 'require_default_chat_config', lambda: fallback)
    monkeypatch.setattr(compaction, 'get_role_selection', lambda role: {'ownership_name': 'Primary', 'model_name': 'Primary'})
    monkeypatch.setattr(compaction, 'get_role_parameter', lambda role: params[role])
    monkeypatch.setattr(compaction, 'resolve_compaction_retry_max_attempts', lambda: 1)
    calls = []
    def invoke(*, request, stream, model_config):
        calls.append((request, model_config))
        if model_config['selected_model_id'] == 'primary':
            raise RuntimeError('primary provider unavailable')
        return {'content': '完整摘要', 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}
    monkeypatch.setattr(compaction.ChatLLM, 'chat_completions', invoke)
    result = asyncio.run(compaction.summarize_context_text('需要压缩的历史', request(), scope='history'))
    assert result.text == '完整摘要' and result.used_fallback and len(calls) == 2
    req, config = calls[-1]
    if target_api == 'messages':
        payload, _, _ = build_payload(req, config, False)
        assert payload['thinking'] == {'type': 'adaptive'}
        assert payload['cache_control'] == {'type': 'ephemeral'}
        assert 'temperature' not in payload and 'enable_thinking' not in payload and 'custom_chat' not in payload
    else:
        payload = ChatLLM._build_request_payload(req, False, config['selected_model_id'])
        assert payload['temperature'] == 0.3 and payload['custom_chat'] is True
        assert 'cache_control' not in payload and req.thinking is None and req.output_config is None
