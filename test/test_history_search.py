import asyncio
import json
import threading
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import history_search as search


def history(root, sid="sample", events=None, title="原始标题", rounds=None):
    path = root / (sid + "_chat.jsonl")
    records = [{"_meta": {"title": title, "updated_at": "2026-10-14 12:00:00"}}]
    records.extend(rounds if rounds is not None else [{"event": "chat_round", "events": events or [
        {"role": "user", "content": "用户问题"}, {"role": "assistant", "content": "Hello 搜索测试"},
    ]}])
    # ensure_ascii=True intentionally: grep over the raw file misses the Chinese.
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def test_search_text_title_filename_and_multiple_events(tmp_path):
    path = history(tmp_path, "中文文件", events=[
        {"role": "user", "content": "需要查找中文内容"},
        {"role": "assistant", "content": "中文内容答复", "reasoning_content": "中文内容思考"},
        {"role": "tool", "tool_name": "read_file", "result": {"text": "中文内容工具"}},
    ], title="中文内容标题")
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = search.search_history("中文内容", root=tmp_path, include_internal=True)
    assert result["total"] == 5
    assert result["session_count"] == 1
    assert {row["field"] for row in result["results"]} == {"title", "content", "reasoning_content", "result"}
    assert [row["event_index"] for row in result["results"] if row["field"] == "content"] == [0, 1]
    assert all(row["round_no"] == 1 for row in result["results"] if row["role"] != "session")
    assert search.search_history("中文文件_chat.jsonl", root=tmp_path)["total"] == 0
    assert search.search_history("中文文件_chat.jsonl", root=tmp_path, include_filenames=True)["total"] == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("query", ["a", "文", "中文", "Hello", "STRASSE", "a_%", '["x"]', '"""', "🦄ab", "a\nb"])
def test_literal_unicode_and_highlight_are_consistent(tmp_path, query):
    text = 'Hello Straße中文 a_% ["x"] """ 🦄ab a\nb'
    history(tmp_path, events=[{"role": "user", "content": text}])
    result = search.search_history(query, root=tmp_path)
    assert any(row["role"] == "user" for row in result["results"])
    snippet = next(row["snippets"][0] for row in result["results"] if row["role"] == "user")
    assert snippet["text"][snippet["start"]:snippet["end"]].casefold() == query.casefold()


def test_index_changes_and_snapshot_pagination(tmp_path):
    events = [{"role": "user", "content": f"hit {i}"} for i in range(7)]
    history(tmp_path, events=events)
    first = search.search_history("hit", root=tmp_path, limit=3)
    second = search.search_history("hit", root=tmp_path, limit=3, offset=3, revision=first["revision"])
    assert first["total"] == 7 and first["next_offset"] == 3
    assert len(second["results"]) == 3 and second["next_offset"] == 6
    assert not {row["id"] for row in first["results"]} & {row["id"] for row in second["results"]}
    with patch.object(search, "_read_file", side_effect=AssertionError("Unchanged file was reparsed")):
        assert search.search_history("hit", root=tmp_path)["total"] == 7
    history(tmp_path, events=[{"role": "user", "content": "changed"}], title="已重命名")
    with pytest.raises(search.SearchSnapshotChanged):
        search.search_history("hit", root=tmp_path, offset=3, revision=first["revision"])
    assert search.search_history("hit", root=tmp_path)["total"] == 0
    assert search.search_history("已重命名", root=tmp_path)["total"] == 1
    (tmp_path / "sample_chat.jsonl").unlink()
    assert search.search_history("changed", root=tmp_path)["total"] == 0


def test_full_record_types_but_no_private_protocol_or_media_bytes(tmp_path):
    history(tmp_path, events=[
        {"role": "user", "content": [{"type": "text", "text": "textneedle"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secretneedle"}},
            {"type": "file", "file": {"filename": "reportneedle.pdf", "file_data": "secretneedle"}}],
            "quotes": [{"text": "quoteneedle"}]},
        {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path":"argneedle"}'}}]},
        {"event": "sub_agent", "phase": "done", "agent_id": "agent-2", "final_reply": "agentneedle"},
        {"event": "context_compaction", "phase": "done", "summary_text": "summaryneedle"},
        {"role": "assistant", "error": "errorneedle"},
        {"event": "model_response", "payload": {"api_key": "secretneedle"}, "content": "secretneedle"},
        {"role": "system", "content": "secretneedle"},
    ])
    for query in ("textneedle", "reportneedle", "quoteneedle", "argneedle", "agentneedle", "summaryneedle", "errorneedle"):
        assert search.search_history(query, root=tmp_path, include_internal=True)["total"] == 1
    assert search.search_history("secretneedle", root=tmp_path, include_internal=True)["total"] == 0
    assert search.search_history("agentneedle", root=tmp_path, include_internal=True)["results"][0]["agent_id"] == "agent-2"


def test_corrupt_lines_skipped_rounds_numbered_and_sidecars_not_scanned(tmp_path):
    path = history(tmp_path, rounds=[{"event": "chat_round", "events": []},
        {"event": "chat_round", "events": [{"role": "user", "content": "needle"}]}])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{unfinished\n')
    (tmp_path / "sidecars").mkdir()
    (tmp_path / "sidecars" / "hidden_chat.jsonl").write_text(path.read_text(), encoding="utf-8")
    path.with_suffix(".jsonl.bak").write_text(path.read_text(), encoding="utf-8")
    result = search.search_history("needle", root=tmp_path)
    assert result["total"] == 1 and result["results"][0]["round_no"] == 2
    assert result["skipped_lines"] == 1


def test_cancelled_search_and_waiting_search_release_lock(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(search.SearchCancelled):
        search.search_history("test", root=tmp_path, cancel=cancel)
    history(tmp_path)
    assert search.search_history("Hello", root=tmp_path)["total"] == 1
    search._lock.acquire()
    try:
        with pytest.raises(search.SearchCancelled):
            search.search_history("test", root=tmp_path, cancel=cancel)
    finally:
        search._lock.release()


def test_tool_data_field_is_searchable_but_native_base64_is_not(tmp_path):
    history(tmp_path, events=[{"role": "tool", "result": {"data": "plainneedle", "image": {
        "type": "base64", "data": "binaryneedle"}}}])
    assert search.search_history("plainneedle", root=tmp_path, include_internal=True)["total"] == 1
    assert search.search_history("binaryneedle", root=tmp_path, include_internal=True)["total"] == 0


def test_invalid_nested_shapes_do_not_prevent_other_records_being_searched(tmp_path):
    history(tmp_path, events=[{"role": "assistant", "tool_calls": [{"function": None}, {"function": "bad"}]},
        {"role": "user", "content": [{"type": {}, "text": "needle"}]}])
    assert search.search_history("needle", root=tmp_path)["total"] == 1


def test_unreadable_file_does_not_return_stale_matches(tmp_path):
    path = history(tmp_path)
    assert search.search_history("Hello", root=tmp_path)["total"] == 1
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with patch.object(search, "_read_file", side_effect=PermissionError):
        result = search.search_history("Hello", root=tmp_path)
    assert result["total"] == 0 and result["skipped_files"] == 1
    assert search.search_history("Hello", root=tmp_path)["total"] == 1


@pytest.mark.parametrize("q", ["", "  ", "a" * 201, "a\x00b"])
def test_query_validation(tmp_path, q):
    with pytest.raises(ValueError):
        search.search_history(q, root=tmp_path)


def test_route_search_pagination_validation_and_revision(tmp_path):
    from memory import chat_memory
    from routers.chat_router import api_chat_router
    history(tmp_path, events=[{"role": "user", "content": "hit"}, {"role": "assistant", "content": "hit"}, {"role": "tool", "result": "hit"}])
    app = FastAPI()
    app.include_router(api_chat_router)
    with patch.object(chat_memory, "HISTORY_ROOT", tmp_path), TestClient(app) as client:
        response = client.get("/chat_history/search", params={"q": "hit", "limit": 1})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2 and len(body["results"]) == 1
        assert client.get("/chat_history/search", params={"q": "hit", "offset": 1, "revision": body["revision"]}).status_code == 200
        assert client.get("/chat_history/search", params={"q": "hit", "offset": 1, "revision": "stale"}).status_code == 409
        assert client.get("/chat_history/search", params={"q": "hit", "include_internal": True}).json()["total"] == 3
        assert client.get("/chat_history/search", params={"q": "sample_chat.jsonl"}).json()["total"] == 0
        assert client.get("/chat_history/search", params={"q": "sample_chat.jsonl", "include_filenames": True}).json()["total"] == 1
        assert client.get("/chat_history/search", params={"q": "hit", "offset": 1, "revision": body["revision"], "include_internal": True}).status_code == 409
        assert client.get("/chat_history/search", params={"q": "   "}).status_code == 400
        for params in ({"q": "hit", "limit": 0}, {"q": "hit", "offset": -1}, {"q": "x" * 201}, {"q": "hit", "include_internal": "bad"}):
            assert client.get("/chat_history/search", params=params).status_code == 422


def test_route_disconnect_cancels_worker():
    from routers.chat_router import search_chat_history
    stopped = threading.Event()
    def worker(*args, cancel, **kwargs):
        if cancel.wait(2):
            stopped.set()
            raise search.SearchCancelled()
        raise AssertionError("Client disconnect did not cancel worker")
    class Request:
        async def is_disconnected(self):
            return True
    async def run():
        with patch.object(search, "search_history", worker):
            response = await search_chat_history(Request(), "hit", 40, 0, None)
            assert response.status_code == 499
            await asyncio.sleep(0.05)
    asyncio.run(run())
    assert stopped.is_set()


def test_scope_filters_before_counting_and_paging_and_reuses_index(tmp_path):
    history(tmp_path, "needle-file", title="needle title", events=[
        {"role": "user", "content": "needle question", "quotes": [{"text": "needle quote"}]},
        {"role": "assistant", "content": "needle answer", "reasoning_content": "needle reasoning"},
        {"role": "tool", "result": "needle tool result"},
        {"event": "sub_agent", "phase": "done", "agent_id": "a", "final_reply": "needle agent reply"},
        {"event": "context_compaction", "summary_text": "needle summary"},
    ])
    history(tmp_path, "tool-only", events=[{"role": "tool", "result": "needle"}])
    first = search.search_history("needle", root=tmp_path, limit=1)
    assert first["total"] == 3 and first["session_count"] == 1
    assert first["include_internal"] is False and first["include_filenames"] is False
    pages = [first]
    while pages[-1]["next_offset"] is not None:
        pages.append(search.search_history("needle", root=tmp_path, limit=1,
                                          offset=pages[-1]["next_offset"], revision=first["revision"]))
    rows = [row for page in pages for row in page["results"]]
    assert {(row["role"], row["field"]) for row in rows} == {("user", "content"), ("assistant", "content"), ("session", "title")}
    assert all(row["filename"].endswith("_chat.jsonl") for row in rows)
    with patch.object(search, "_read_file", side_effect=AssertionError("Scope switch must reuse index")):
        expanded = search.search_history("needle", root=tmp_path, include_internal=True)
        assert expanded["total"] == 9 and expanded["session_count"] == 2
        assert search.search_history("needle", root=tmp_path, include_filenames=True)["total"] == 4
        assert search.search_history(".jsonl", root=tmp_path, include_internal=True)["total"] == 0
        assert search.search_history(".jsonl", root=tmp_path, include_filenames=True)["total"] == 2
        for scope in ({"include_internal": True}, {"include_filenames": True}):
            with pytest.raises(search.SearchSnapshotChanged):
                search.search_history("needle", root=tmp_path, offset=1, revision=first["revision"], **scope)
