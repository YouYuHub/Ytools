"""暂停/中断后继续（resume）后端测试：内部 nudge 开轮、末轮判定与接口校验。"""
import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import chat_memory
from memory import file_memory
from memory.chat_round_store import ChatRoundStore
from memory.chat_history_format import (
    _round_question,
    _resolve_round_user_content,
    round_entry_to_context_messages,
)


def _write_round(path, status, events, question=""):
    line = {
        "event": "chat_round",
        "question": question,
        "started_at": "2026-10-29 12:00:00",
        "events": events,
        "status": status,
    }
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_internal_nudge_opens_round_without_question():
    store = ChatRoundStore(session_id="resume_ut")
    store.record_message({"role": "user", "content": "上次任务被中断了，请继续。", "_internal": True})
    assert store.pending_round is not None
    assert store.pending_round["question"] == ""
    assert store.pending_round["events"][0].get("_internal") is True
    # 普通用户消息仍正常计入问题（回归）
    store2 = ChatRoundStore(session_id="resume_ut2")
    store2.record_message({"role": "user", "content": "正常问题"})
    assert store2.pending_round["question"] == "正常问题"


def test_internal_user_events_skipped_in_question_and_replay():
    entry = {
        "event": "chat_round",
        "question": "",
        "events": [{"role": "user", "content": "上次任务被中断了，请继续。", "_internal": True}],
        "status": "running",
    }
    assert _round_question(entry) is None
    assert _resolve_round_user_content(entry) is None
    # 无助手输出的纯 nudge 轮：回放为空（不把 nudge 发给模型）
    assert round_entry_to_context_messages(entry) == []


def test_last_round_status_reads_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    path = tmp_path / "resume_hist_chat.jsonl"
    path.write_text(json.dumps({"_meta": {"title": "t"}}) + "\n", encoding="utf-8")
    _write_round(path, "done", [{"role": "user", "content": "q1"}], question="q1")
    _write_round(path, "stopped", [{"role": "user", "content": "q2"}], question="q2")
    manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_hist"))
    assert manager.get_last_round_status() == "stopped"
    empty_path = tmp_path / "resume_empty_chat.jsonl"
    empty_path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    empty_manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_empty"))
    assert empty_manager.get_last_round_status() is None


def _resume_client(monkeypatch):
    from routers import chat_router
    from routers.chat_router import api_chat_router
    app = FastAPI()
    app.include_router(api_chat_router)
    return TestClient(app), chat_router


def test_resume_chat_rejects_missing_running_and_done(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    client, chat_router = _resume_client(monkeypatch)
    # 会话不存在 → 404，且不创建文件
    response = client.post("/resume_chat", json={"session_id": "nope"})
    assert response.status_code == 404
    assert not (tmp_path / "nope_chat.jsonl").exists()
    # 末轮已完成 → 409
    done_path = tmp_path / "resume_done_chat.jsonl"
    done_path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(done_path, "done", [{"role": "user", "content": "q"}], question="q")
    response = client.post("/resume_chat", json={"session_id": "resume_done"})
    assert response.status_code == 409
    # 运行中 → 409（末轮 stopped 也不行）
    stopped_path = tmp_path / "resume_busy_chat.jsonl"
    stopped_path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(stopped_path, "stopped", [{"role": "user", "content": "q"}], question="q")
    monkeypatch.setattr(chat_router, "is_chat_stream_running", lambda sid: True)
    response = client.post("/resume_chat", json={"session_id": "resume_busy"})
    assert response.status_code == 409


def test_resume_chat_starts_internal_round(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    client, chat_router = _resume_client(monkeypatch)
    stopped_path = tmp_path / "resume_go_chat.jsonl"
    stopped_path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(stopped_path, "interrupted", [{"role": "user", "content": "q"}], question="q")
    seen = {}

    async def fake_server(tool_request):
        seen["resume"] = tool_request.resume
        seen["messages"] = [m.model_dump() for m in tool_request.messages]
        seen["use_backend_history"] = tool_request.use_backend_history
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_router, "tool_chat_server", fake_server)
    response = client.post("/resume_chat", json={"session_id": "resume_go", "tool_names": ["read_file"]})
    assert response.status_code == 200
    assert seen["resume"] is True
    assert seen["use_backend_history"] is True
    assert len(seen["messages"]) == 1 and seen["messages"][0]["role"] == "user"
    assert "继续" in (seen["messages"][0]["content"] or "")


def test_stream_status_reports_can_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    client, _ = _resume_client(monkeypatch)
    stopped_path = tmp_path / "resume_flag_chat.jsonl"
    stopped_path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(stopped_path, "stopped", [{"role": "user", "content": "q"}], question="q")
    body = client.get("/chat_stream/status", params={"session_id": "resume_flag"}).json()
    assert body["running"] is False
    assert body["can_resume"] is True
    assert body["last_round_status"] == "stopped"
    body = client.get("/chat_stream/status", params={"session_id": "resume_absent"}).json()
    assert body["can_resume"] is False
    assert body["last_round_status"] is None


def test_resume_context_retains_stopped_round_media(tmp_path, monkeypatch):
    """续跑上下文保留被续轮次的完整媒体部件（可解析为 base64），而非文本占位。"""
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "session_files")
    saved = file_memory.save_session_media(
        "resume_media", "shot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64
    )
    path = tmp_path / "resume_media_chat.jsonl"
    path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(path, "done", [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ], question="q1")
    _write_round(path, "stopped", [
        {"role": "user", "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": saved["media_ref"]}},
        ]},
        {"role": "assistant", "content": "正在看"},
    ], question="看这张图")
    manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_media"))
    default_messages = asyncio.run(manager.get_context_messages())
    default_user_texts = [m["content"] for m in default_messages if m["role"] == "user"]
    assert any(isinstance(c, str) and "[图片 " in c for c in default_user_texts)
    retained = asyncio.run(manager.get_context_messages(retain_stopped_round_media=True))
    user_parts = [m["content"] for m in retained
                  if m["role"] == "user" and isinstance(m.get("content"), list)]
    assert len(user_parts) == 1
    assert user_parts[0][1] == {"type": "image_url", "image_url": {"url": saved["media_ref"]}}
    # 端到端：保留的引用可解析为 base64（与当前轮同口径），而非占位文本
    # （解析器替换 message["content"] 为新列表，需重新读取断言）
    assert file_memory.resolve_message_media_refs("resume_media", retained) == []
    resolved_parts = [m["content"] for m in retained
                      if m["role"] == "user" and isinstance(m.get("content"), list)]
    assert resolved_parts[0][1]["image_url"]["url"].startswith("data:image/png;base64,")
