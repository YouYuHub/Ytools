"""Question continuation uses the same round identity and stream as normal chat."""
import asyncio
import json

import pytest

from config import ChatLLMRequest
from factory import chat_factory as cf
from memory import chat_memory
from test_permission_generation import environment  # noqa: F401


def frames(chunks):
    return [json.loads(line[5:]) for chunk in chunks for line in chunk.splitlines()
            if line.startswith('data:') and line[5:].strip() != '[DONE]']


@pytest.mark.parametrize('position,expected', [({}, 3), ({'target_round':2}, 2), ({'insert_round':1}, 2)])
def test_all_send_paths_confirm_live_round_after_question_saved(environment, monkeypatch, position, expected):
    async def model(**kwargs):
        yield 'data: {"content":"回答后实时继续","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf, '_CHAT_WORKER_MODE', 'inline')
    monkeypatch.setattr(cf, '_SESSION_STREAMS', {})
    monkeypatch.setattr(chat_memory, '_session_managers', {})
    monkeypatch.setattr(cf.ChatLLM, 'chat_completions', model)
    async def run():
        manager=await chat_memory.get_chat_memory_manager('ask-stream-test')
        for text in ('第一轮','第二轮'):
            await manager.add_chat_history({'role':'user','content':text})
            await manager.add_chat_history({'role':'assistant','content':'原回复'})
            await manager.add_chat_history({'role':'assistant','done':'[DONE]'})
        req=ChatLLMRequest(session_id='ask-stream-test',messages=[{'role':'user','content':'【回答模型提问】\n同意继续'}],use_backend_history=False,**position)
        received=frames([chunk async for chunk in cf.tool_chat_server(req)])
        assert {'round_started':{'round':expected}} in received
        assert any(frame.get('content')=='回答后实时继续' for frame in received)
    asyncio.run(run())


def test_creator_never_skips_metadata_if_worker_advances_before_subscription(monkeypatch):
    async def run():
        stream=cf._SessionStream('ask-creator-test')
        stream.emit('data: {"round_started":{"round":3}}\n\n')
        stream.emit('data: {"round_model":{"name":"test model"}}\n\n')
        stream.set_round_start()
        stream.emit('data: {"content":"immediate continuation"}\n\n')
        await stream.finish()
        async def prepare(request):
            return stream,True
        monkeypatch.setattr(cf,'_prepare_chat_stream',prepare)
        received=frames([chunk async for chunk in cf.tool_chat_server(ChatLLMRequest(session_id='ask-creator-test',messages=[{'role':'user','content':'answer'}]))])
        assert received==[{'round_started':{'round':3}},{'round_model':{'name':'test model'}},{'content':'immediate continuation'}]
    asyncio.run(run())
