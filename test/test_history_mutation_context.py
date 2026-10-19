"""History mutations keep bounded summaries and never continue an obsolete remote chain."""
import asyncio
import copy
import json

import pytest

from config import ChatLLMRequest
from chat.protocols import responses_store
from factory.agent_runtime import context_compaction as compact, responses_context
from factory.agent_runtime.history_summary_repair import repair_history_summary_if_needed
from memory import chat_memory
from memory.chat_history_format import adjust_context_summary_for_deleted_rounds, render_context_summary
from test_permission_generation import environment, Stream


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(compact, "resolve_effective_summary_budget", lambda *args: 4096)
    return chat_memory.ChatMemoryManager("mutation")


async def add_round(manager, question, answer):
    await manager.add_chat_history({"role": "user", "content": question})
    await manager.add_chat_history({"role": "assistant", "content": answer})
    await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})


async def seed(manager):
    for i in range(1, 6):
        await add_round(manager, f"QUESTION_{i}", f"ANSWER_{i} " + "large original " * 5000)
    await manager.update_context_summary({"source_round_count": 4, "blocks": [
        {"summary": "VALID_FIRST_BLOCK", "round_start": 1, "round_end": 2},
        {"summary": "STALE_FACT_FROM_3_AND_4", "round_start": 3, "round_end": 4},
    ]})


def request(manager, mode="stateful", messages=None):
    return ChatLLMRequest(session_id=manager.session_id, responses_mode=mode, max_tokens=1024,
        messages=messages or [{"role": "user", "content": "continue"}])


def remote_head(manager):
    state = responses_store.load_state(manager.session_id)
    state.update(head_response_id="old_response", head_record_id="bad_old_record", status="ready")
    responses_store._write(responses_store._state_path(manager.session_id, "chat_model"), state)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("operation", ["delete", "truncate", "edit", "stop_edit"])
def test_mixed_block_rebuilt_without_stale_facts_or_raw_expansion(manager, monkeypatch, remote, operation):
    sources = []

    async def summarize(source, *args, **kwargs):
        sources.append(source)
        assert "STALE_FACT" not in source and "VALID_FIRST_BLOCK" not in source
        assert "ANSWER_3" not in source and "ANSWER_1" not in source
        assert kwargs["output_token_budget"] <= 4096
        return compact.SummaryBuildResult("REPAIRED_VALID_FACTS", False, False,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})

    monkeypatch.setattr(compact, "summarize_context_text", summarize)

    async def run():
        await seed(manager)
        if remote:
            remote_head(manager)  # Even without the optional metadata mirror.
        if operation in {"delete", "truncate"}:
            await manager.delete_rounds(3, mode="single" if operation == "delete" else "truncate")
        else:
            manager.set_target_round(3)
            await manager.add_chat_history({"role": "user", "content": "NEW_QUESTION"})
            await manager.add_chat_history({"role": "assistant", "content": "NEW_ANSWER"})
            if operation == "stop_edit":
                await manager.stop_current_round()
            else:
                await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        summary = await manager.get_context_summary()
        assert summary is not None
        assert summary["blocks"][0]["summary"] == "VALID_FIRST_BLOCK"
        assert "STALE_FACT" not in json.dumps(summary)
        assert not responses_store.load_state(manager.session_id).get("head_response_id")
        if operation == "truncate":
            # The entire affected block disappeared: no model call needed.
            assert not await repair_history_summary_if_needed(manager, request(manager))
            assert not sources
        else:
            with pytest.raises(RuntimeError, match="待重建"):
                await manager.get_context_messages()
            await repair_history_summary_if_needed(manager, request(manager))
            assert len(sources) == 1
        output = await manager.get_context_messages()
        # The only full raw history is the uncompressed tail.
        summary_messages = [m for m in output if m["role"] == "system"]
        assert "large original" not in str(summary_messages)
        assert len(str(summary_messages)) < 2000
        assert "ANSWER_3" not in str(output)
        assert not await repair_history_summary_if_needed(manager, request(manager))

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["edit", "insert", "stop_edit", "stop_insert"])
@pytest.mark.parametrize("mode", ["stateful", "stateless"])
def test_next_responses_request_contains_preserved_suffix(manager, operation, mode):
    async def run():
        for i in range(1, 4):
            await add_round(manager, f"Q{i}", f"A{i}")
        if "insert" in operation:
            manager.set_insert_round(1)
        else:
            manager.set_target_round(2)
        await manager.add_chat_history({"role": "user", "content": "NEW_QUESTION"})
        await manager.add_chat_history({"role": "assistant", "content": "NEW_ANSWER"})
        # Simulate the remote head established by the edited task's prefix.
        remote_head(manager)
        if operation.startswith("stop"):
            await manager.stop_current_round()
        else:
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        history = await manager.get_context_messages()
        req = request(manager, mode, [*history, {"role": "user", "content": "CONTINUE"}])
        req.responses_context = responses_context.start_turn(history, start_index=len(history))
        payload, _, _ = responses_context.prepare(req, {"selected_model_id": "test"}, False)
        assert "previous_response_id" not in payload
        assert all(text in json.dumps(payload) for text in ["A3", "NEW_ANSWER", "CONTINUE"])

    asyncio.run(run())


def test_failed_repair_is_retryable_and_does_not_send_raw_history(manager, monkeypatch):
    async def fail(*args, **kwargs):
        raise compact.ContextCompactionError("offline")
    monkeypatch.setattr(compact, "summarize_context_text", fail)

    async def run():
        await seed(manager)
        await manager.delete_rounds(3, mode="single")
        before = copy.deepcopy(await manager.get_context_summary())
        with pytest.raises(compact.ContextCompactionError, match="offline"):
            await repair_history_summary_if_needed(manager, request(manager))
        assert await manager.get_context_summary() == before
        with pytest.raises(RuntimeError, match="待重建"):
            await manager.get_context_messages()

    asyncio.run(run())


def test_concurrent_mutation_cannot_commit_stale_repair(manager, monkeypatch):
    async def summarize(*args, **kwargs):
        await manager.delete_rounds(3, mode="single")
        return compact.SummaryBuildResult("OUTDATED_REPAIR", False, False)
    monkeypatch.setattr(compact, "summarize_context_text", summarize)

    async def run():
        await seed(manager)
        await manager.delete_rounds(3, mode="single")
        with pytest.raises(compact.ContextCompactionError, match="历史已变化"):
            await repair_history_summary_if_needed(manager, request(manager))
        assert "OUTDATED_REPAIR" not in str(await manager.get_context_summary())
    asyncio.run(run())


def test_all_deleted_blocks_do_not_resurrect_top_level_projection():
    result = adjust_context_summary_for_deleted_rounds({
        "source_round_count": 2, "summary": "OLD_SECRET", "completed": ["OLD_SECRET"],
        "blocks": [{"summary": "OLD_SECRET", "round_start": 1, "round_end": 2}],
    }, [1, 2], total_rounds=3)
    assert not render_context_summary(result)


def test_legacy_summary_and_repeated_deletion_keep_valid_repair_bounds(manager):
    async def run():
        await seed(manager)
        await manager.update_context_summary("legacy summary with unknown coverage")
        await manager.delete_rounds(3, mode="single")
        await manager.delete_rounds(2, mode="single")
        summary = await manager.get_context_summary()
        assert summary["source_round_count"] == 3
        assert summary["blocks"][0]["round_start"] == 1
        assert summary["blocks"][0]["round_end"] == 3
        assert summary["blocks"][0]["repair_required"]
        assert "legacy summary" not in str(summary)
    asyncio.run(run())


@pytest.mark.parametrize("protocol,mode", [
    ("chat_completions", None), ("messages", None),
    ("responses", "stateful"), ("responses", "stateless"),
])
@pytest.mark.parametrize("repair_fails", [False, True])
def test_generation_repairs_before_sending_history(environment, monkeypatch, protocol, mode, repair_fails):
    from factory import chat_factory as cf
    cfg = {"selected_provider_name": "test", "selected_model_id": "test", "selected_model_name": "test",
        "apiType": protocol, "vision": False, "maxInputTokens": 628000}
    monkeypatch.setattr(cf, "require_default_chat_config", lambda: cfg)
    monkeypatch.setattr(cf, "resolve_model_max_input_tokens", lambda **kw: 628000)
    monkeypatch.setattr(compact, "resolve_effective_summary_budget", lambda *args: 4096)
    calls = []

    async def summarize(source, *args, **kwargs):
        assert "ANSWER_3" not in source and "STALE_FACT" not in source
        if repair_fails:
            raise compact.ContextCompactionError("repair unavailable")
        return compact.SummaryBuildResult("REPAIRED_VALID_FACTS", False, False)

    async def no_extra_compaction(*args, **kwargs):
        return 0

    async def model(request=None, **kwargs):
        text = str([m.content for m in request.messages])
        assert "REPAIRED_VALID_FACTS" in text and "VALID_FIRST_BLOCK" in text
        assert "STALE_FACT" not in text and "ANSWER_3" not in text
        assert "ANSWER_5" in text
        calls.append(request)
        yield 'data: {"content":"完成","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(compact, "summarize_context_text", summarize)
    monkeypatch.setattr(cf, "compact_session_history_if_needed", no_extra_compaction)
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)

    async def run():
        manager = await chat_memory.get_chat_memory_manager("mutation-generation")
        await seed(manager)
        await manager.delete_rounds(3, mode="single")
        stream = Stream()
        req = ChatLLMRequest(session_id=manager.session_id, tool_names=[], responses_mode=mode,
            messages=[{"role": "user", "content": "继续"}])
        await cf._run_chat_generation(req, stream)
        if repair_fails:
            assert not calls
            assert any("repair unavailable" in str(e.get("error")) for e in stream.events)
            return
        assert len(calls) == 1
        assert not any(e.get("error") for e in stream.events)
        assert any(e.get("trigger_reason") == "history_mutation" and e.get("phase") == "done"
            for e in stream.events)
    asyncio.run(run())


def test_insertion_while_repair_pending_shifts_range_and_includes_new_round(manager, monkeypatch):
    async def summarize(source, *args, **kwargs):
        assert "INSERTED_FACT" in source and "ANSWER_3" not in source
        assert "ANSWER_4" in source
        return compact.SummaryBuildResult("repaired", False, False)
    monkeypatch.setattr(compact, "summarize_context_text", summarize)

    async def run():
        await seed(manager)
        await manager.update_context_summary({"source_round_count": 4, "blocks": [
            {"summary": "old mixed summary", "round_start": 1, "round_end": 4}
        ]})
        await manager.delete_rounds(3, mode="single")
        manager.set_insert_round(1)
        await add_round(manager, "NEW", "INSERTED_FACT")
        pending = await manager.get_context_summary()
        assert pending["blocks"][0]["round_end"] == 4
        await repair_history_summary_if_needed(manager, request(manager))
    asyncio.run(run())


def test_content_replacement_disconnects_native_replay_and_repairs_summary(manager):
    async def run():
        await seed(manager)
        remote_head(manager)
        result = chat_memory.ChatMemoryManager.replace_content_in_chat_session(
            manager.session_id, "ANSWER_3", "UPDATED_FACT")
        assert result["state"] == "succeed"
        assert not responses_store.load_state(manager.session_id).get("head_response_id")
        assert "STALE_FACT" not in str(await manager.get_context_summary())
        assert manager.get_summary_repair_snapshot() is not None
    asyncio.run(run())


def test_cancelled_repair_preserves_retryable_summary(manager, monkeypatch):
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(compact, "summarize_context_text", cancel)

    async def run():
        await seed(manager)
        await manager.delete_rounds(3, mode="single")
        before = await manager.get_context_summary()
        with pytest.raises(asyncio.CancelledError):
            await repair_history_summary_if_needed(manager, request(manager))
        reloaded = chat_memory.ChatMemoryManager(manager.session_id)
        assert await reloaded.get_context_summary() == before
        assert reloaded.get_summary_repair_snapshot() is not None
    asyncio.run(run())
