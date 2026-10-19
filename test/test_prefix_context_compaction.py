"""历史重发/插入使用受位置约束的摘要，不能压完全文再因越界退回原文。"""
import asyncio
import copy

import pytest

from config import ChatLLMRequest
from factory import chat_factory as cf
from factory.agent_runtime import context_compaction as compact
from factory.agent_runtime.chat_runtime import estimate_send_context_tokens
from memory import chat_memory
from test_permission_generation import environment, Stream


@pytest.fixture(autouse=True)
def isolated_manager_cache(monkeypatch):
    monkeypatch.setattr(chat_memory, "_session_managers", {})


async def seed(manager, repeat=2000):
    for i in range(1, 4):
        await manager.add_chat_history({"role": "user", "content": f"问题{i}"})
        await manager.add_chat_history({"role": "assistant", "content": ("旧内容" * repeat) if i < 3 else "FUTURE_SENTINEL"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})


@pytest.mark.parametrize("protocol", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize("position", ["edit", "insert"])
def test_edit_reuses_valid_prefix_summary_without_forcing_compaction(environment, monkeypatch, protocol, position):
    config = {"selected_provider_name": "test", "selected_model_name": "test", "selected_model_id": "test",
              "apiType": protocol, "vision": False, "maxInputTokens": 628000}
    monkeypatch.setattr(cf, "require_default_chat_config", lambda: config)
    monkeypatch.setattr(cf, "resolve_model_max_input_tokens", lambda default=8192: 628000)
    requests, compactions = [], []

    async def unexpected_compaction(*args, **kwargs):
        compactions.append(kwargs)
        raise AssertionError("合法摘要已放入窗口，不应因 Responses 重发而压缩")

    async def model(request=None, **kwargs):
        requests.append(request.model_copy(deep=True))
        yield 'data: {"content":"新的回答","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(cf, "compact_session_history_if_needed", unexpected_compaction)
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)

    async def run():
        manager = await chat_memory.get_chat_memory_manager("prefix-edit")
        await seed(manager, repeat=120000)
        await manager.update_context_summary({"source_round_count": 2, "summary": "前两轮摘要"})
        stream = Stream()
        req = ChatLLMRequest(session_id="prefix-edit", target_round=3 if position == "edit" else None,
            insert_round=2 if position == "insert" else None, tool_names=[],
            messages=[{"role": "user", "content": "重写第三轮"}], responses_mode="stateless")
        await cf._run_chat_generation(req, stream)
        assert not compactions
        assert len(requests) == 1
        content = "\n".join(str(m.content or "") for m in requests[0].messages)
        assert "前两轮摘要" in content and "FUTURE_SENTINEL" not in content
        assert not any(e.get("error") for e in stream.events)

    asyncio.run(run())


@pytest.mark.parametrize("position", ["edit", "insert"])
@pytest.mark.parametrize("protocol", ["chat_completions", "messages", "responses"])
def test_forced_compaction_only_covers_visible_prefix_and_preserves_global_summary(environment, monkeypatch, position, protocol):
    sources, events = [], []
    settings = compact.ContextCompactionSettings(trigger_ratio=.8, summary_budget_ratio=.15,
        oversized_reject_factor=1.5, max_oversized_rejections=3, target_tokens=3000)
    monkeypatch.setattr(compact, "resolve_effective_window", lambda settings=None: 20000)
    monkeypatch.setattr(compact, "_resolve_compaction_source_budget", lambda *args: 18000)

    async def summarize(source, *args, **kwargs):
        sources.append(source)
        return compact.SummaryBuildResult("只包含前两轮的任务摘要", False, False)

    monkeypatch.setattr(compact, "summarize_context_text", summarize)

    async def run():
        manager = await chat_memory.get_chat_memory_manager("prefix-force")
        await seed(manager)
        original = {"source_round_count": 3, "summary": "全会话摘要 FUTURE_SENTINEL"}
        await manager.update_context_summary(original)
        original = copy.deepcopy(await manager.get_context_summary())
        if position == "edit": manager.set_target_round(3)
        else: manager.set_insert_round(2)
        before = await manager.get_context_messages()
        assert estimate_send_context_tokens(before) > 3000
        async def emit(payload): events.append(payload)
        req = ChatLLMRequest(session_id=manager.session_id, messages=[{"role":"user","content":"新问题"}],
                             wire_protocol=protocol, protocol_config={"apiType":protocol})
        await compact.compact_session_history_if_needed(manager, req, settings=settings,
            budget_tokens=3000, enforce=True, force_all=True, trigger_reason="first_call", event_emitter=emit)
        assert sources and all("FUTURE_SENTINEL" not in source for source in sources)
        after = await manager.get_context_messages()
        assert estimate_send_context_tokens(after) < 3000
        assert "只包含前两轮的任务摘要" in str(after)
        assert "FUTURE_SENTINEL" not in str(after)
        assert await manager.get_context_summary() == original
        stats = await manager.get_context_token_stats()
        assert stats["rounds"]["summarized"] == 2
        assert stats["messages_tokens"] < 3000
        assert all(e["history_prefix_rounds"] == 2 for e in events)
        source_count = len(sources)
        assert await compact.compact_session_history_if_needed(manager, req, settings=settings,
            budget_tokens=3000, enforce=True, force_all=True, trigger_reason="first_call") == 0
        assert len(sources) == source_count, "相同前缀已压缩后不能重新压缩"
        manager.set_target_round(None); manager.set_insert_round(None)
        assert "FUTURE_SENTINEL" in str(await manager.get_context_messages())

    asyncio.run(run())


def test_prefix_summary_absorbs_edited_raw_overrides_without_reintroducing_them(environment, monkeypatch):
    async def run():
        manager = await chat_memory.get_chat_memory_manager("prefix-edited-raw")
        await seed(manager)
        await manager.update_context_summary({"source_round_count":2,"summary":"尚未包含最新编辑的摘要"})
        original_load = manager._load_for_read
        def load_with_override():
            meta, entries = original_load()
            meta["context_summary_raw_rounds"] = [1]
            return meta, entries
        monkeypatch.setattr(manager, "_load_for_read", load_with_override)
        manager.set_target_round(3)
        assert estimate_send_context_tokens(await manager.get_context_messages()) > 3000
        view = manager.get_context_compaction_view()
        assert await view.get_context_summary() is None
        await view.update_context_summary({"source_round_count":2,"summary":"已包含编辑内容的前缀摘要"})
        assert estimate_send_context_tokens(await manager.get_context_messages()) < 3000
        assert (await manager.get_context_token_stats())["messages_tokens"] < 3000
        assert (await manager.get_context_summary())["summary"] == "尚未包含最新编辑的摘要"
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize("position", ["edit", "insert"])
def test_first_call_rebuild_uses_the_new_prefix_summary_across_protocols(environment, monkeypatch, protocol, position):
    config = {"selected_provider_name":"test", "selected_model_name":"test", "selected_model_id":"test",
              "apiType":protocol, "vision":False, "maxInputTokens":628000}
    settings = compact.ContextCompactionSettings(trigger_ratio=.8, summary_budget_ratio=.15,
        oversized_reject_factor=1.5, max_oversized_rejections=3, target_tokens=60000)
    monkeypatch.setattr(cf, "require_default_chat_config", lambda: config)
    monkeypatch.setattr(cf, "resolve_model_max_input_tokens", lambda default=8192:628000)
    monkeypatch.setattr(cf, "load_context_compaction_settings", lambda:settings)
    monkeypatch.setattr(compact, "load_context_compaction_settings", lambda:settings)
    monkeypatch.setattr(compact, "resolve_effective_window", lambda settings=None:628000)
    monkeypatch.setattr(compact, "_resolve_compaction_source_budget", lambda *args:565200)
    monkeypatch.setattr(cf, "compact_session_history_if_needed", compact.compact_session_history_if_needed)
    calls, sources = [], []

    async def summarize(source, *args, **kwargs):
        sources.append(source)
        return compact.SummaryBuildResult("临时前缀摘要", False, False)

    async def model(request=None, **kwargs):
        calls.append(request.model_copy(deep=True))
        yield 'data: {"content":"完成","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(compact, "summarize_context_text", summarize)
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)

    async def run():
        manager = await chat_memory.get_chat_memory_manager("prefix-large")
        await seed(manager, repeat=120000)
        await manager.update_context_summary({"source_round_count":3,"summary":"全会话摘要 FUTURE_SENTINEL"})
        req = ChatLLMRequest(session_id="prefix-large", target_round=3 if position == "edit" else None,
            insert_round=2 if position == "insert" else None, tool_names=[],
            messages=[{"role":"user","content":"改写第三轮"}], responses_mode="stateless")
        stream = Stream()
        await cf._run_chat_generation(req, stream)
        assert sources and all("FUTURE_SENTINEL" not in text for text in sources)
        assert len(calls) == 1
        sent = "\n".join(str(message.content or "") for message in calls[0].messages)
        assert "临时前缀摘要" in sent and "FUTURE_SENTINEL" not in sent
        assert not any(e.get("error") for e in stream.events)
        start = next(e for e in stream.events if e.get("event") == "context_compaction" and e.get("phase") == "start")
        assert start["trigger_reason"] == "first_call"
        assert start["trigger_context_tokens"] > start["trigger_threshold"] == 628000
        assert start["history_prefix_rounds"] == 2

    asyncio.run(run())
