"""后台续跑行为验证：mock ChatLLM，验证 断线后后台任务继续生成并落盘。"""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

import factory.chat_factory as cf
from config import ChatLLMRequest
from memory.chat_memory import (
    ChatMemoryManager,
    cleanup_chat_memory_manager,
    get_chat_memory_manager,
)


class FakeLLM:
    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        for i in range(8):
            if stop_checker is not None and stop_checker():
                break
            yield f"data: {json.dumps({'content': f'块{i}', 'id': f'c{i}'})}\n\n"
            await asyncio.sleep(0.02)
        yield "data: " + json.dumps({"finish_reason": "stop", "id": "x", "usage": {"total_tokens": 12}}) + "\n\n"
        yield "data: [DONE]\n\n"


def patch_env():
    cf.ChatLLM.chat_completions = FakeLLM.chat_completions
    cf.load_all_tools = lambda: asyncio.sleep(0)
    cf.tool_registry.ALL_TOOLS = []
    cf.tool_registry.TOOL_MCP_SERVERS = {}
    cf.require_default_chat_config = lambda: None
    cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)


async def collect_stream(gen, count=None):
    out, i = [], 0
    async for chunk in gen:
        out.append(chunk)
        i += 1
        if count is not None and i >= count:
            break
    return out


async def main():
    patch_env()
    sid = f"mock_race_{uuid.uuid4().hex}"
    req1 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "讲个故事"}])

    # 场景1: 正常消费完整流
    full = await collect_stream(cf.tool_chat_server(req1))
    assert any("[DONE]" in c for c in full), "完整流应以 [DONE] 结束"
    text = "".join(c for c in full)
    assert "块0" in text and "块7" in text, "应有全部 8 个内容块"
    await asyncio.sleep(0.1)
    meta = await (await get_chat_memory_manager(sid)).get_session_meta()
    print("场景1 记录数:", meta["record_count"])
    assert meta["record_count"] >= 1, "整轮落盘"
    await cleanup_chat_memory_manager(sid)

    # 场景2: 消费中途断开（模拟刷页面）
    req2 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "继续讲故事"}])
    partial = await collect_stream(cf.tool_chat_server(req2), count=3)
    assert len(partial) == 3 and not any("[DONE]" in c for c in partial)
    # 后台任务应继续运行直至结束
    for _ in range(50):
        if not cf.is_chat_stream_running(sid):
            break
        await asyncio.sleep(0.05)
    assert not cf.is_chat_stream_running(sid), "后台任务最终应完成"
    await asyncio.sleep(0.1)
    meta = await (await get_chat_memory_manager(sid)).get_session_meta()
    print("场景2 记录数:", meta["record_count"])
    assert meta["record_count"] >= 2, "断线后轮次仍完整落库"
    await cleanup_chat_memory_manager(sid)
    print("PASS: 断线后台续跑 + 落盘")

    # 场景3: 断流期间新消费者附接 → 收到 replay 标记 + 本轮回放 + [DONE]
    req3 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "再来一轮"}])
    first = await collect_stream(cf.tool_chat_server(req3), count=2)
    assert len(first) == 2
    attach_req = ChatLLMRequest(session_id=sid, messages=[])
    attached = await collect_stream(cf.tool_chat_server(attach_req))
    replays = [c for c in attached if '"replay"' in c]
    assert replays, "附接应收到回放标记"
    joined_text = "".join(attached)
    assert "块" in joined_text, "应有回放/后续内容块"
    assert any("[DONE]" in c for c in attached), "附接流应收到 [DONE]"
    print("场景3 附接回放:", "OK")
    await cleanup_chat_memory_manager(sid)

    # 场景4: 运行中带新用户消息 → 平滑打断旧任务并开始新一轮生成
    req4a = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "第一问"}])
    first = await collect_stream(cf.tool_chat_server(req4a), count=2)
    assert len(first) == 2, "首轮应开始流出"
    req4b = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "打断后新问题"}])
    second = await collect_stream(cf.tool_chat_server(req4b))
    joined = "".join(second)
    assert any("[DONE]" in c for c in second), "新轮应以 [DONE] 结束"
    assert "块" in joined, "新轮应真正生成内容（run_task 被重置）"
    await asyncio.sleep(0.1)
    meta = await (await get_chat_memory_manager(sid)).get_session_meta()
    print("场景4 记录数:", meta["record_count"])
    assert meta["record_count"] >= 3, "新旧两轮都应落盘"
    await cleanup_chat_memory_manager(sid)
    ChatMemoryManager.delete_chat_session_file(sid)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())