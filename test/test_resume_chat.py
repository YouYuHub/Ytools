"""暂停/中断后继续（resume）后端测试：内部 nudge 开轮、末轮判定与接口校验。"""
import asyncio
import json
import os
import sys
from pathlib import Path

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

RESTART_PROBE = r'''
import asyncio, json, os, sys
sys.path.insert(0, os.getcwd())
from pathlib import Path
tmp = Path(sys.argv[1])
sessions = sys.argv[2].split(",")
from memory import chat_memory
from memory import permission_store
chat_memory.HISTORY_ROOT = tmp
permission_store.ROOT = tmp / "state"

async def collect_statuses():
    result = {}
    for session in sessions:
        manager = await chat_memory.get_chat_memory_manager(session)
        result[session] = manager.get_last_round_status()
    return result

statuses = asyncio.run(collect_statuses())

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import chat_router

async def fake_server(tool_request):
    yield "data: [DONE]\n\n"

chat_router.tool_chat_server = fake_server
app = FastAPI()
app.include_router(chat_router.api_chat_router)
client = TestClient(app)
out = {}
for session in sessions:
    st = client.get("/chat_stream/status", params={"session_id": session}).json()
    rs = client.post("/resume_chat", json={"session_id": session})
    out[session] = {
        "status": statuses[session],
        "can_resume": st["can_resume"],
        "running": st["running"],
        "resume_code": rs.status_code,
    }
print("PROBE-BEGIN")
print(json.dumps(out))
'''


def _run_restart_probe(tmp_path, sessions):
    import subprocess
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", RESTART_PROBE, str(tmp_path), ",".join(sessions)],
        cwd=root, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-2000:]}"
    marker = "PROBE-BEGIN\n"
    assert marker in proc.stdout, f"probe no output: {proc.stdout[-1000:]} {proc.stderr[-1000:]}"
    return json.loads(proc.stdout.split(marker, 1)[1].strip().splitlines()[0])


def test_restart_after_clean_stop_keeps_resumable(tmp_path, monkeypatch):
    """干净暂停（stopped 落盘、无检查点）→ 真重启（全新进程）→ 状态门与继续门全开。"""
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_restart"))
    asyncio.run(manager.add_chat_history({"role": "user", "content": "q"}))
    asyncio.run(manager.add_chat_history({"role": "assistant", "content": "partial"}))
    asyncio.run(manager.stop_current_round())
    assert manager.get_last_round_status() == "stopped"
    assert not manager._pending_checkpoint_path.exists(), "干净停止必须清除检查点"
    out = _run_restart_probe(tmp_path, ["resume_restart"])
    got = out["resume_restart"]
    assert got["status"] == "stopped"
    assert got["running"] is False
    assert got["can_resume"] is True
    assert got["resume_code"] == 200


def test_restart_after_crash_marks_interrupted_and_resumable(tmp_path, monkeypatch):
    """非干净终止（检查点残留、写者已死）→ 真重启 → 恢复为 interrupted 并可继续。"""
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_crash"))
    asyncio.run(manager.add_chat_history({"role": "user", "content": "q"}))
    asyncio.run(manager.add_chat_history({"role": "assistant", "content": "partial"}))
    checkpoint = manager._pending_checkpoint_path
    assert checkpoint.exists(), "进行中轮次应有检查点"
    # 模拟写者进程已死（重启后旧 worker PID 不存在）
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["writer_pid"] = 2 ** 30
    checkpoint.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = _run_restart_probe(tmp_path, ["resume_crash"])
    got = out["resume_crash"]
    assert got["status"] == "interrupted"
    assert got["running"] is False
    assert got["can_resume"] is True
    assert got["resume_code"] == 200
    assert not checkpoint.exists(), "恢复后应清除检查点"

def _isolate_roots(monkeypatch, tmp_path):
    from memory import permission_store
    monkeypatch.setattr(permission_store, "ROOT", tmp_path / "state")
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    return permission_store


def _write_done_round(tmp_path, session):
    path = tmp_path / f"{session}_chat.jsonl"
    path.write_text(json.dumps({"_meta": {}}) + "\n", encoding="utf-8")
    _write_round(path, "done", [{"role": "user", "content": "q"}], question="q")
    return path


def _make_main_task(store, session, *, owner, state="running"):
    created = store.create_task(session, {"request": {}, "mode": "auto"})
    with store.database() as db:
        db.execute("UPDATE tasks SET owner=?, state=? WHERE id=?", (owner, state, created["id"]))
    store.checkpoint(created["id"], "main", {
        "pending_round": {
            "event": "chat_round",
            "question": "stale-q",
            "started_at": "2026-10-29 13:00:00",
            "events": [
                {"role": "user", "content": "stale-q"},
                {"role": "assistant", "content": "p", "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
            ],
        },
        "messages": [{"role": "user", "content": "q"}],
    })
    return created["id"]


def test_stale_restorable_round_is_resumable_but_live_is_not(tmp_path, monkeypatch):
    """写者已死的可恢复任务视为可继续；owner 存活（待审批/真运行）绝不放行。"""
    store = _isolate_roots(monkeypatch, tmp_path)
    _write_done_round(tmp_path, "resume_stale")
    _write_done_round(tmp_path, "resume_live")
    _make_main_task(store, "resume_stale", owner=2 ** 30)
    _make_main_task(store, "resume_live", owner=os.getpid())
    assert store.has_stale_restorable_round("resume_stale") is True
    assert store.has_stale_restorable_round("resume_live") is False
    # has_restorable_round 本身两种都命中（门禁必须用 stale 变体区分）
    assert store.has_restorable_round("resume_stale") is True
    assert store.has_restorable_round("resume_live") is True
    client, chat_router = _resume_client(monkeypatch)

    async def fake_server(tool_request):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_router, "tool_chat_server", fake_server)
    stale_status = client.get("/chat_stream/status", params={"session_id": "resume_stale"}).json()
    assert stale_status["running"] is False
    assert stale_status["can_resume"] is True
    assert stale_status["last_round_status"] == "done"
    assert client.post("/resume_chat", json={"session_id": "resume_stale"}).status_code == 200
    live_status = client.get("/chat_stream/status", params={"session_id": "resume_live"}).json()
    assert live_status["can_resume"] is False
    assert client.post("/resume_chat", json={"session_id": "resume_live"}).status_code == 409


def test_retire_stale_task_finalizes_interrupted_round(tmp_path, monkeypatch):
    """新消息/继续到达时接续死亡任务：历史补 interrupted 轮，任务 cancelled。"""
    store = _isolate_roots(monkeypatch, tmp_path)
    _write_done_round(tmp_path, "resume_retire")
    task_id = _make_main_task(store, "resume_retire", owner=2 ** 30)
    from factory import chat_factory as cf
    retired = asyncio.run(cf._retire_interrupted_tasks("resume_retire"))
    assert retired == 1
    assert store.task(task_id)["state"] == "cancelled"
    manager = asyncio.run(chat_memory.get_chat_memory_manager("resume_retire"))
    assert manager.get_last_round_status() == "interrupted"

def test_restart_with_stale_permission_task_keeps_resumable(tmp_path, monkeypatch):
    """非干净终止 + 工具轮（SQLite 遗留 running 任务）→ 真重启 → ▶ 可用且继续放行。"""
    store = _isolate_roots(monkeypatch, tmp_path)
    _write_done_round(tmp_path, "resume_staletask")
    _make_main_task(store, "resume_staletask", owner=2 ** 30)
    out = _run_restart_probe(tmp_path, ["resume_staletask"])
    got = out["resume_staletask"]
    assert got["running"] is False
    assert got["can_resume"] is True
    assert got["resume_code"] == 200
