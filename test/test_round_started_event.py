"""round_started / round_model 事件验证。

- 普通发送推送本轮最终轮次号（round_started.round），target_round 不重复推送；
- round_started 帧不再携带 model（该帧只在普通发送路径推送，编辑重发/回答
  插入会缺模型名）——本轮生效聊天模型统一由 round_model 帧下发，三条发送
  路径均推送，前端据此显示「本轮消耗 … · 模型 provider/name」。
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
    # 上下文窗口：测试环境不读 models.json（默认 8192），而真实 MCP 工具定义
    # 本身就占数千 token，会触发超窗降级链使任务提前终止——本测试只关注
    # round_started / round_model 帧，这里显式给出充足窗口
    cf.resolve_model_max_input_tokens = lambda default=8192: 1000000
    # 会话生效聊天模型（round_model 帧的数据源）：provider / name / id 三项
    cf.require_default_chat_config = lambda: {
        "selected_provider_name": "测试供应商",
        "selected_model_name": "测试模型",
        "selected_model_id": "test-model-id",
        "vision": True,
    }
    cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)


async def collect_stream(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def parse_frames(chunks):
    """从流中解析所有 JSON 帧（可能多个 chunk 拼接，逐帧解析）。"""
    frames = []
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
            if isinstance(payload, dict):
                frames.append(payload)
    return frames


def collect_round_started(frames):
    return [f["round_started"] for f in frames if "round_started" in f]


def collect_round_model(frames):
    return [f["round_model"] for f in frames if "round_model" in f]


async def main():
    patch_env()
    sid = f"mock_round_started_{uuid.uuid4().hex}"
    expected_model = {"provider": "测试供应商", "name": "测试模型", "id": "test-model-id"}

    # 场景1: 首轮普通发送 → round_started round=1（不含 model）+ 一次 round_model
    req1 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "第一问"}])
    full1 = await collect_stream(cf.tool_chat_server(req1))
    assert any("[DONE]" in c for c in full1), "首轮应正常结束"
    frames1 = parse_frames(full1)
    started1 = collect_round_started(frames1)
    assert [s.get("round") for s in started1] == [1], f"首轮应推送一次 round_started=1，实际 {started1}"
    # round_started 只带轮次号：model 字段已移除（双路下发的冗余源）
    assert all("model" not in s for s in started1), f"round_started 不应携带 model，实际 {started1}"
    models1 = collect_round_model(frames1)
    assert models1 == [expected_model], f"首轮应推送一次 round_model={expected_model}，实际 {models1}"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)

    # 场景2: 第二轮普通发送 → round_started round=2 + 一次 round_model
    req2 = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "第二问"}])
    full2 = await collect_stream(cf.tool_chat_server(req2))
    assert any("[DONE]" in c for c in full2), "第二轮应正常结束"
    frames2 = parse_frames(full2)
    started2 = collect_round_started(frames2)
    assert [s.get("round") for s in started2] == [2], f"第二轮应推送一次 round_started=2，实际 {started2}"
    assert all("model" not in s for s in started2), f"round_started 不应携带 model，实际 {started2}"
    assert collect_round_model(frames2) == [expected_model], "第二轮应推送一次 round_model"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)

    # 场景3: 编辑重发（target_round=1）→ 不推 round_started，但仍推 round_model
    # （这正是把模型名从 round_started 挪到 round_model 的原因：编辑重发路径
    #  此前拿不到模型名，前端只能显示上一轮的旧模型）
    req3 = ChatLLMRequest(
        session_id=sid,
        messages=[{"role": "user", "content": "编辑后的第一问"}],
        target_round=1,
    )
    full3 = await collect_stream(cf.tool_chat_server(req3))
    assert any("[DONE]" in c for c in full3), "编辑重发应正常结束"
    frames3 = parse_frames(full3)
    assert collect_round_started(frames3) == [], "target_round 路径不应推 round_started"
    assert any('"target_round"' in c for c in full3), "应推送 target_round 帧"
    assert collect_round_model(frames3) == [expected_model], \
        f"编辑重发路径同样应推送 round_model（实际 {collect_round_model(frames3)}）"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)
    await asyncio.sleep(0.2)

    # 场景4: 回答插入（insert_round）→ 不推 round_started，但仍推 round_model
    req4 = ChatLLMRequest(
        session_id=sid,
        messages=[{"role": "user", "content": "回答模型的提问"}],
        insert_round=1,
    )
    full4 = await collect_stream(cf.tool_chat_server(req4))
    assert any("[DONE]" in c for c in full4), "回答插入应正常结束"
    frames4 = parse_frames(full4)
    assert collect_round_started(frames4) == [], "insert_round 路径不应推 round_started"
    assert collect_round_model(frames4) == [expected_model], \
        f"回答插入路径同样应推送 round_model（实际 {collect_round_model(frames4)}）"
    await asyncio.sleep(0.1)
    await cleanup_chat_memory_manager(sid)

    ChatMemoryManager.delete_chat_session_file(sid)
    print("ALL PASS: round_started 仅带轮次号（无 model）/ round_model 三条路径均下发")


if __name__ == "__main__":
    asyncio.run(main())
