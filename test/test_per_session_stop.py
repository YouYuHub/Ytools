"""按会话隔离的停止/状态链路验证：mock ChatLLM，验证 stop_chat_task 只停止目标会话任务，
其他会话的后台生成任务不受影响（is_chat_stream_running 按 session_id 各自独立）。"""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

import factory.chat_factory as cf
from config import ChatLLMRequest
from memory.chat_memory import ChatMemoryManager, cleanup_chat_memory_manager


class StoppableLLM:
    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        for i in range(200):
            if stop_checker is not None and stop_checker():
                break
            yield f"data: {json.dumps({'content': f'块{i}', 'id': f'c{i}'})}\n\n"
            await asyncio.sleep(0.02)
        yield "data: " + json.dumps({"finish_reason": "stop", "id": "x", "usage": {"total_tokens": 12}}) + "\n\n"
        yield "data: [DONE]\n\n"


def patch_env():
    cf.ChatLLM.chat_completions = StoppableLLM.chat_completions
    cf.load_all_tools = lambda: asyncio.sleep(0)
    cf.tool_registry.ALL_TOOLS = []
    cf.tool_registry.TOOL_MCP_SERVERS = {}
    cf.require_default_chat_config = lambda: None
    cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)


async def collect_stream(gen_obj, count=None):
    out, i = [], 0
    async for chunk in gen_obj:
        out.append(chunk)
        i += 1
        if count is not None and i >= count:
            break
    return out


async def main():
    patch_env()
    sid_a = f"sess_stop_a_{uuid.uuid4().hex}"
    sid_b = f"sess_stop_b_{uuid.uuid4().hex}"
    req_a = ChatLLMRequest(session_id=sid_a, messages=[{"role": "user", "content": "任务A"}])
    req_b = ChatLLMRequest(session_id=sid_b, messages=[{"role": "user", "content": "任务B"}])

    gen_a = cf.tool_chat_server(req_a)
    gen_b = cf.tool_chat_server(req_b)
    await collect_stream(gen_a, count=2)
    await collect_stream(gen_b, count=2)

    assert cf.is_chat_stream_running(sid_a), "A 应在后台生成"
    assert cf.is_chat_stream_running(sid_b), "B 应在后台生成"

    # 只停止 A：B 必须不受影响
    await cf.stop_chat_task(sid_a)
    for _ in range(100):
        if not cf.is_chat_stream_running(sid_a):
            break
        await asyncio.sleep(0.02)
    assert not cf.is_chat_stream_running(sid_a), "A 应被优雅停止"
    assert cf.is_chat_stream_running(sid_b), "B 不应被 A 的停止影响"

    # B 仍可完整消费到 [DONE]
    tail_b = await collect_stream(gen_b)
    assert any("[DONE]" in c for c in tail_b), "B 应正常生成完毕"

    await cleanup_chat_memory_manager(sid_a)
    await cleanup_chat_memory_manager(sid_b)
    ChatMemoryManager.delete_chat_session_file(sid_a)
    ChatMemoryManager.delete_chat_session_file(sid_b)
    print("按会话停止隔离验证通过: A 已停止, B 完整生成完毕")


if __name__ == "__main__":
    asyncio.run(main())