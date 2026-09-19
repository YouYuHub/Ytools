"""round_started 事件验证：普通发送推送本轮最终轮次号，target_round 不重复推送。

前端依据该事件给本轮提问气泡就地补挂编辑/复制/删除入口（无需等收尾重载），
停止重发编排（编辑重发 while running）依赖轮次号精确删除/替换目标轮。
"""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

import factory.chat_factory as cf
from config import ChatLLMRequest
from memory.chat_memory import ChatMemoryManager, cleanup_chat_memory_manager


class FakeLLM:
    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        yield "data: " + json.dumps({"content": "回答内容"}) + "\n\n"
        yield "data: " + json.dumps({"finish_reason": "stop", "id": "x", "usage": {"total_tokens": 5}}) + "\n\n"
        yield "data: [DONE]\n\n"


def patch_env():
    # 生成循环默认跑在独立 worker 进程（无法继承本测试的 mock），切回 inline
    cf._CHAT_WORKER_MODE = "inline"
    cf.ChatLLM.chat_completions = FakeLLM.chat_completions
    cf.load_all_tools = lambda: asyncio.sleep(0)
    cf.tool_registry.ALL_TOOLS = []
    cf.tool_registry.TOOL_MCP_SERVERS = {}
    cf.require_default_chat_config = lambda: None
    cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)


async def collect_stream(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def parse_round_started(chunks):
    """从流中解析 round_started 事件（可能多个 chunk 拼接，逐帧解析）。"""
    rounds = []
    for chunk in chunks:
        for line in chunk.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if isinstance(payload, dict) and "round_started" in payload:
                rounds.append(payload["round_started"].get("round"))
    return rounds


async def main():
    patch_env()
    sid = f"mock_round_started_{uuid.uuid4().hex}"

    # 场景1: 首轮普通发送 → round_started round=1
    req1 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "第一问"}])
    full1 = await collect_stream(cf.tool_chat_server(req1))
    assert any("[DONE]" in c for c in full1), "首轮应正常结束"
    rounds1 = parse_round_started(full1)
    assert rounds1 == [1], f"首轮应推送一次 round_started=1，实际 {rounds1}"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)

    # 场景2: 第二轮普通发送 → round_started round=2
    req2 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "第二问"}])
    full2 = await collect_stream(cf.tool_chat_server(req2))
    assert any("[DONE]" in c for c in full2), "第二轮应正常结束"
    rounds2 = parse_round_started(full2)
    assert rounds2 == [2], f"第二轮应推送一次 round_started=2，实际 {rounds2}"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)

    # 场景3: 编辑重发（target_round=1）→ 不推 round_started（已有 target_round 帧）
    req3 = ChatLLMRequest(
        session_id=sid,
        messages=[{"role": "user", "content": "编辑后的第一问"}],
        target_round=1,
    )
    full3 = await collect_stream(cf.tool_chat_server(req3))
    assert any("[DONE]" in c for c in full3), "编辑重发应正常结束"
    rounds3 = parse_round_started(full3)
    assert rounds3 == [], f"target_round 路径不应推 round_started，实际 {rounds3}"
    assert any('"target_round"' in c for c in full3), "应推送 target_round 帧"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)
    await asyncio.sleep(0.2)

    ChatMemoryManager.delete_chat_session_file(sid)
    print("ALL PASS: round_started 推送/轮次递增/target_round 不重复")


if __name__ == "__main__":
    asyncio.run(main())
