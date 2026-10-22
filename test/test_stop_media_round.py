"""Stopping media preparation must leave a real, editable history round."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from config import ChatLLMRequest
from factory import chat_factory as cf
from factory.session_worker import SessionWorkerProxy
from memory import chat_memory
from test_permission_generation import environment, Stream


def rounds(manager):
    return [row for row in chat_memory._load_meta_and_entries(manager._file_path, manager.session_id)[1]
            if row.get("event") == "chat_round"]


VIDEO = {"type": "video_url", "video_url": {"url": "media://clip.mp4"}}


def test_video_base64_is_placeholder_not_text_tokens():
    from factory.agent_runtime.chat_runtime import estimate_message_tokens, MULTIMODAL_PART_PLACEHOLDER_TOKENS
    reference = estimate_message_tokens({"role": "user", "content": [VIDEO]})
    encoded = estimate_message_tokens({"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + "A" * 1000000}}]})
    assert reference == encoded
    assert MULTIMODAL_PART_PLACEHOLDER_TOKENS <= reference < MULTIMODAL_PART_PLACEHOLDER_TOKENS + 10


@pytest.mark.parametrize("api,next_api", [("chat-completions", "messages"), ("messages", "responses"), ("responses", "chat-completions")])
@pytest.mark.parametrize("media_only", [False, True])
def test_stop_during_media_preparation_then_change_model_and_resend(environment, monkeypatch, api, next_api, media_only):
    config = {"apiType": api, "vision": True, "maxInputTokens": 1000000,
              "selected_provider_name": "test", "selected_model_name": api}
    monkeypatch.setattr(cf, "require_default_chat_config", lambda: dict(config))
    monkeypatch.setattr(cf, "_generation_use_worker", lambda: False)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def prepare(session, messages, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError("test media thread was not released")
        return []
    monkeypatch.setattr(cf, "resolve_message_media_refs", prepare)
    async def model(**kwargs):
        calls.append(config["apiType"])
        yield 'data: {"content":"完成检查","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)
    async def run():
        sid = "stop-video"
        content = [VIDEO] if media_only else [{"type": "text", "text": "检查视频问题"}, VIDEO]
        req = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": content}], tool_names=[], use_backend_history=False)
        stream = Stream()
        monkeypatch.setattr(cf, "_get_session_stream", lambda _: stream)
        task = asyncio.create_task(cf._run_chat_generation(req, stream))
        stream.task = task
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert any(e.get("round_started", {}).get("round") == 1 for e in stream.events)
            # Stop must finish while media preparation is still blocked in a thread.
            await asyncio.wait_for(cf.stop_chat_task(sid), 3)
            assert not release.is_set()
            assert not calls
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        manager = await chat_memory.get_chat_memory_manager(sid)
        saved = rounds(manager)
        assert len(saved) == 1 and saved[0]["status"] == "stopped"
        assert saved[0]["events"][0]["content"] == content
        assert not manager._pending_checkpoint_path.exists()
        assert (await manager.delete_rounds(1, dry_run=True))["total_rounds"] == 1
        config.update(apiType=next_api, selected_model_name=next_api)
        monkeypatch.setattr(cf, "resolve_message_media_refs", lambda *args, **kwargs: [])
        req.target_round = 1
        await cf._run_chat_generation(req, Stream())
        manager = await chat_memory.get_chat_memory_manager(sid)
        assert len(rounds(manager)) == 1
        assert rounds(manager)[0]["status"] == "done"
        assert calls == [next_api]
        await chat_memory.cleanup_chat_memory_manager(sid)
    asyncio.run(run())


@pytest.mark.parametrize("position", ["append", "replace", "insert"])
@pytest.mark.parametrize("restart", [False, True])
def test_cached_manager_and_restart_recover_original_round_position(environment, monkeypatch, position, restart):
    async def run():
        sid = "stop-checkpoint"
        main = chat_memory.ChatMemoryManager(sid)
        for text in ("first", "second", "third"):
            await main.add_chat_history({"role": "user", "content": text})
            await main.add_chat_history({"role": "assistant", "done": "[DONE]"})
        worker = chat_memory.ChatMemoryManager(sid)
        if position == "replace":
            worker.set_target_round(2)
        elif position == "insert":
            worker.set_insert_round(1)
        await worker.add_chat_history({"role": "user", "content": [{"type": "text", "text": "video question"}, VIDEO]})
        checkpoint = worker._pending_checkpoint_path.read_text(encoding="utf-8")
        assert main._round_store.pending_round is None
        assert len(rounds(main)) == 3
        if restart:
            monkeypatch.setattr(chat_memory, "_pid_alive", lambda _: False)
            main = chat_memory.ChatMemoryManager(sid)
        else:
            proxy = SimpleNamespace(request_stop=lambda **kwargs: True, is_generation_running=lambda: False)
            monkeypatch.setattr(cf, "_generation_use_worker", lambda: True)
            monkeypatch.setattr(cf, "peek_worker_proxy", lambda _: proxy)
            monkeypatch.setattr(cf, "_get_session_stream", lambda _: None)
            async def get_manager(_): return main
            monkeypatch.setattr(cf, "get_chat_memory_manager", get_manager)
            await cf.stop_chat_task(sid)
        saved = rounds(main)
        expected = ["first", "video question", "third"] if position == "replace" else (
            ["first", "video question", "second", "third"] if position == "insert" else
            ["first", "second", "third", "video question"])
        assert [row["question"] for row in saved] == expected
        index = expected.index("video question")
        assert saved[index]["status"] == ("interrupted" if restart else "stopped")
        assert not main._pending_checkpoint_path.exists()
        assert "writer_pid" not in saved[index] and "target_round" not in saved[index]
        assert (await main.delete_rounds(index + 1, dry_run=True))["planned_rounds"][0]["round"] == index + 1
        # A leftover sidecar after an atomic history commit must not duplicate it.
        main._pending_checkpoint_path.write_text(checkpoint, encoding="utf-8")
        await main.stop_current_round(recover_checkpoint=True)
        assert len(rounds(main)) == len(expected)
    asyncio.run(run())


def test_checkpoint_survives_history_write_failure_and_can_retry(environment, monkeypatch):
    async def run():
        manager = chat_memory.ChatMemoryManager("write-failed")
        await manager.add_chat_history({"role": "user", "content": "keep video"})
        original = chat_memory._write_meta_and_entries
        def fail(*args, **kwargs): raise OSError("simulated unavailable file")
        monkeypatch.setattr(chat_memory, "_write_meta_and_entries", fail)
        with pytest.raises(OSError):
            await manager.stop_current_round()
        assert manager._pending_checkpoint_path.exists()
        monkeypatch.setattr(chat_memory, "_write_meta_and_entries", original)
        await manager.stop_current_round(recover_checkpoint=True)
        assert len(rounds(manager)) == 1 and rounds(manager)[0]["status"] == "stopped"
    asyncio.run(run())


@pytest.mark.parametrize("position", ["replace", "insert"])
def test_stopping_saved_permission_round_keeps_position_without_sidecar(environment, monkeypatch, position):
    from memory import permission_store
    async def run():
        sid = "saved-video-position"
        main = chat_memory.ChatMemoryManager(sid)
        for text in ("first", "second", "third"):
            await main.add_chat_history({"role": "user", "content": text})
            await main.add_chat_history({"role": "assistant", "done": "[DONE]"})
        await main.update_context_summary({"summary": "已确认的历史事实", "source_round_count": 3})
        worker = chat_memory.ChatMemoryManager(sid)
        await worker.add_chat_history({"role": "user", "content": "saved video question"})
        pending = worker._round_store.snapshot_pending_round()
        worker._pending_checkpoint_path.unlink()
        original = {"target_round": 2} if position == "replace" else {"insert_round": 1}
        task = permission_store.create_task(sid, {"request": original, "root": str(environment), "mode": "ask"})
        permission_store.checkpoint(task["id"], "main", {"pending_round": pending})
        permission_store.set_task_state(task["id"], "suspended")
        monkeypatch.setattr(cf, "_generation_use_worker", lambda: False)
        monkeypatch.setattr(cf, "_get_session_stream", lambda _: None)
        async def get_manager(_): return main
        monkeypatch.setattr(cf, "get_chat_memory_manager", get_manager)
        await cf.stop_chat_task(sid)
        saved = rounds(main)
        expected = ["first", "saved video question", "third"] if position == "replace" else ["first", "saved video question", "second", "third"]
        assert [row["question"] for row in saved] == expected
        assert saved[1]["status"] == "stopped"
        meta, _ = chat_memory._load_meta_and_entries(main._file_path, sid)
        assert meta.get("context_summary") is not None
    asyncio.run(run())


def test_stop_during_precompression_has_durable_question(environment, monkeypatch):
    async def run():
        entered = asyncio.Event()
        async def compact(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        monkeypatch.setattr(cf, "compact_session_history_if_needed", compact)
        stream = Stream()
        req = ChatLLMRequest(session_id="stop-before-model", messages=[{"role": "user", "content": "question"}], tool_names=[], use_backend_history=True)
        running = asyncio.create_task(cf._run_chat_generation(req, stream))
        await asyncio.wait_for(entered.wait(), 3)
        assert any(e.get("round_started", {}).get("round") == 1 for e in stream.events)
        stream.user_stop_requested = True
        running.cancel()
        with pytest.raises(asyncio.CancelledError): await running
        manager = await chat_memory.get_chat_memory_manager(req.session_id)
        assert rounds(manager)[0]["question"] == "question"
        assert rounds(manager)[0]["status"] == "stopped"
    asyncio.run(run())


def test_precompression_failure_finishes_accepted_question(environment, monkeypatch):
    async def fail(*args, **kwargs): raise cf.ContextCompactionError("simulated summary failure")
    monkeypatch.setattr(cf, "compact_session_history_if_needed", fail)
    async def run():
        req = ChatLLMRequest(session_id="prepare-failure", messages=[{"role": "user", "content": "question"}], tool_names=[], use_backend_history=True)
        await cf._run_chat_generation(req, Stream())
        manager = await chat_memory.get_chat_memory_manager(req.session_id)
        assert rounds(manager)[0]["status"] == "error"
        assert not manager._pending_checkpoint_path.exists()
    asyncio.run(run())


@pytest.mark.parametrize("sent", [True, False])
def test_worker_queued_generate_is_running_before_task_started(monkeypatch, sent):
    proxy = SessionWorkerProxy("queued")
    monkeypatch.setattr(proxy, "_ensure_process", lambda: None)
    def send(payload):
        assert proxy.is_generation_running()
        return sent
    monkeypatch.setattr(proxy, "_send", send)
    assert proxy.start_generation({"messages": []}) is sent
    assert proxy.is_generation_running() is sent
    proxy._dispatch({"type": "task_done"})
    assert not proxy.is_generation_running()
