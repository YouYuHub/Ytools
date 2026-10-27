"""Tool contracts: no unintended writes, bounded work, and accurate feedback."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt
from factory.agent_runtime.tool_control import run_sync_tool
from factory.agent_runtime.tool_validation import argument_error, prepare_builtin_calls
from mcp_server import web_tools_server as web


def call(index, name, args):
    return index, {"id": f"audit_{index}", "function": {"name": name}}, name, args


def test_missing_write_mode_never_changes_or_creates_file(tmp_path):
    path = tmp_path / "existing.txt"
    path.write_text("keep", encoding="utf-8")
    for target in (path, tmp_path / "new.txt"):
        with pytest.raises(ValueError, match="mode 必填"):
            bt.execute_write_file({"path": str(target), "content": "lost"})
    assert path.read_text() == "keep"
    assert not (tmp_path / "new.txt").exists()


def test_edit_expansion_and_encoded_size_leave_original_intact(tmp_path, monkeypatch):
    path = tmp_path / "expand.txt"
    path.write_text("a" * 32)
    monkeypatch.setattr(bt, "_EDIT_FILE_MAX_BYTES", 128)
    with pytest.raises(ValueError, match="结果过大"):
        bt.execute_edit_file({"path": str(path), "old_string": "a", "new_string": "b" * 32,
                              "replace_all": True})
    assert path.read_text() == "a" * 32
    with pytest.raises(ValueError, match="MiB 上限"):
        bt.execute_edit_file({"path": str(path), "old_string": "a" * 32, "new_string": "汉" * 60})
    assert path.read_text() == "a" * 32


def test_edit_preserves_untouched_mixed_line_endings(tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"a\r\nb\nc\rd\r\n")
    bt.execute_edit_file({"path": str(path), "old_string": "b", "new_string": "B"})
    assert path.read_bytes() == b"a\r\nB\nc\rd\r\n"


def test_external_edit_before_commit_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "race.txt"
    path.write_text("before")
    commit = bt._atomic_write_text
    def concurrent_commit(target, content, encoding, **kwargs):
        target.write_text("external update")
        return commit(target, content, encoding, **kwargs)
    monkeypatch.setattr(bt, "_atomic_write_text", concurrent_commit)
    with pytest.raises(ValueError, match="FILE_MODIFIED_EXTERNALLY"):
        bt.execute_edit_file({"path": str(path), "old_string": "before", "new_string": "mine"})
    assert path.read_text() == "external update"


def test_small_file_paging_reuses_snapshot_and_invalidates_after_write(tmp_path):
    path = tmp_path / "page.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    with patch.object(bt, "_read_bytes_with_limit", wraps=bt._read_bytes_with_limit) as read:
        first = bt.execute_read_file({"path": str(path), "limit": 1})
        second = bt.execute_read_file({"path": str(path), "start_line": first["next_start_line"], "limit": 1})
        assert "two" in second["content"]
        assert read.call_count == 1
        path.write_text("changed\n", encoding="utf-8")
        third = bt.execute_read_file({"path": str(path)})
        assert "changed" in third["content"]
        assert third["content_hash"] != first["content_hash"]
        assert read.call_count == 2


def test_explicit_utf16_without_bom_is_readable_and_editable(tmp_path):
    path = tmp_path / "utf16.txt"
    path.write_bytes("hello\nworld".encode("utf-16-le"))
    read = bt.execute_read_file({"path": str(path), "encoding": "utf-16-le"})
    assert "world" in read["content"]
    bt.execute_edit_file({"path": str(path), "encoding": "utf-16-le", "old_string": "world", "new_string": "changed"})
    assert path.read_bytes().decode("utf-16-le") == "hello\nchanged"


@pytest.mark.parametrize("field,value", [("start_line", 1.5), ("limit", True), ("char_offset", -1)])
def test_read_rejects_invalid_offsets(tmp_path, field, value):
    path = tmp_path / "text.txt"
    path.write_text("content")
    with pytest.raises(ValueError):
        bt.execute_read_file({"path": str(path), field: value})


def test_empty_exclusions_do_not_scan_hidden_directories(tmp_path):
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("needle")
    args = {"dir_path": str(tmp_path), "pattern": "needle", "exclude_dirs": [], "no_default_excludes": False}
    assert bt.execute_search_files(args)["total_matches"] == 0
    assert bt.execute_search_files({**args, "no_default_excludes": True})["total_matches"] == 1


def test_slow_regex_returns_incomplete_result_within_budget(tmp_path):
    (tmp_path / "slow.txt").write_text("a" * 100000 + "!")
    started = time.monotonic()
    result = bt.execute_search_files({"dir_path": str(tmp_path), "pattern": "(a+)+$", "is_regex": True,
                                      "max_scan_seconds": 0.1})
    assert time.monotonic() - started < 2
    assert result["scan_truncated"]
    assert not result["scan_complete"]


def test_search_counts_matches_without_storing_all_match_blocks(tmp_path):
    (tmp_path / "many.txt").write_text("needle\n" * 5000)
    result = bt.execute_search_files({"dir_path": str(tmp_path), "pattern": "needle", "count_total": True})
    assert result["total_matches"] == 5000
    assert result["displayed_matches"] < 5000
    assert result["output_truncated"]


def test_pdf_page_failure_is_not_returned_as_full_text(monkeypatch):
    monkeypatch.setattr(bt.file_memory, "find_file_memory_record", lambda *_: None)
    monkeypatch.setattr(bt, "_load_local_document", lambda *_args, **_kwargs:
                        ({"filename": "doc.pdf", "content": "wrong full text", "type": "pdf"}, b"fake", None))
    monkeypatch.setattr(bt.file_memory, "document_supports_native", lambda *_: True)
    monkeypatch.setattr(bt.file_memory, "build_native_pdf_page_part_from_bytes", lambda *_args, **_kwargs:
                        {"error": "page_start 超出 PDF 总页数"})
    result = bt.execute_read_document({"filename": "doc.pdf", "page_start": 999, "page_count": 1,
                                       "_max_tokens": 2000}, "test", ["pdf"])
    assert "超出 PDF 总页数" in result["error"]
    assert "content" not in result


@pytest.mark.parametrize("value", [True, 2.5, -1, "3"])
def test_document_offsets_are_strict(value):
    result = bt.execute_read_document({"filename": "doc.pdf", "offset": value}, "test")
    assert "offset" in result["error"]


def test_network_media_range_rejected_without_download(monkeypatch):
    with patch.object(bt, "_fetch_url_media_bytes") as fetch:
        result = bt.load_any_media_model_part("test", "https://example.test/video.mp4", start_time=0, end_time=10)
    assert "不支持时间区间" in result["error"]
    fetch.assert_not_called()


def test_media_parts_reused_in_memory_and_not_in_result(monkeypatch):
    calls = []
    def load(_session, reference, **kwargs):
        calls.append(kwargs)
        if reference == "bad.png":
            raise OSError("unreadable")
        return {"part": {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}, "kind": "image"}
    monkeypatch.setattr(bt, "load_any_media_model_part", load)
    parts = {}
    result = bt.execute_read_media({"references": ["bad.png", "good.png"]}, "test", api_type="responses", loaded_parts=parts)
    assert result["ok"] and len(result["skipped"]) == 1
    assert calls[-1]["api_type"] == "responses"
    assert parts["good.png"]["part"]["type"] == "image_url"
    assert "base64" not in json.dumps(result)


@pytest.mark.parametrize("args", [{"quality": 85.9}, {"quality": True}, {"start_time": float("nan")},
                                  {"end_time": float("inf")}, {"start_time": True}])
def test_media_rejects_invalid_numbers(args):
    assert bt.normalize_read_media_args({"references": ["video.mp4"], **args})[2]


def test_ask_barrier_defers_every_other_tool_before_permission():
    calls = [call(1, "write_file", {"path": "file.txt", "content": "data", "mode": "create"}),
             call(2, "ask_user", {"questions": [{"question": "确认覆盖？"}]}),
             call(3, "external_tool", {})]
    allowed, rejected = prepare_builtin_calls(calls)
    assert [item[2] for item in allowed] == ["ask_user"]
    assert len(rejected) == 2
    assert all(item["result"]["code"] == "WAITING_USER" and not item["result"]["executed"] for item in rejected)


def test_question_total_and_boolean_are_validated():
    assert bt.normalize_ask_questions([{"question": "选哪个？", "multiple": "false"}]) is None
    calls = [call(1, "ask_user", {"questions": [{"question": str(i)} for i in range(3)]}),
             call(2, "ask_user", {"questions": [{"question": str(i)} for i in range(3)]})]
    allowed, rejected = prepare_builtin_calls(calls)
    assert len(allowed) == 1
    assert "最多 5" in rejected[0]["result"]["error"]


def test_unknown_parameters_return_short_errors():
    error = argument_error("run_command", {"command": "echo ok", "timeuot_seconds": 1})
    assert "timeuot_seconds" in error
    assert len(argument_error("write_file", {"path": "x", "mode": "create", "content": "x" * (bt._WRITE_EDIT_MAX_CHARS + 1)})) < 300
    assert bt.normalize_sub_agent_task(123)[1]
    for value in (float("nan"), float("inf"), True, "120"):
        assert argument_error("run_command", {"command": "echo ok", "timeout_seconds": value})
    assert argument_error("read_file", {"path": "x", "start_line": 1.0})


@pytest.mark.parametrize("arguments", ['{"broken":', '[]', 'null', '"text"', '{"value": NaN}'])
def test_invalid_json_arguments_never_become_empty_mcp_arguments(arguments):
    from factory.agent_runtime.tool_executor import prepare_tool_execution, _invoke_tool_function
    plan = prepare_tool_execution([{"id": "invalid", "function": {
        "name": "external_tool", "arguments": arguments}}], ["external_tool"])
    assert plan.has_parse_error
    allowed, rejected = prepare_builtin_calls(plan.parsed_tools)
    assert not allowed
    assert rejected[0]["result"]["code"] == "INVALID_ARGUMENTS"
    with patch("factory.agent_runtime.tool_executor.call_mcp_tool") as invoke:
        with pytest.raises(ValueError, match="JSON 对象"):
            _invoke_tool_function("external_tool", plan.parsed_tools[0][3], {"external_tool": "test"})
    invoke.assert_not_called()


def test_concatenated_tools_with_missing_arguments_are_not_invented():
    from factory.agent_runtime.tool_executor import normalize_tool_calls
    call = {"id": "bad_split", "function": {"name": "firstsecond", "arguments": "{}"}}
    assert normalize_tool_calls([call], ["first", "second"]) == [call]


def test_local_native_document_uses_independent_byte_budget(tmp_path, monkeypatch):
    path = tmp_path / "native.pdf"
    path.write_bytes(b"native document bytes")
    monkeypatch.setattr(bt, "_READ_DOCUMENT_LOCAL_MAX_BYTES", 3)
    monkeypatch.setattr(bt.file_memory, "find_file_memory_record", lambda *_: None)
    monkeypatch.setattr(bt.file_memory, "native_doc_max_bytes", lambda: 1024)
    result = bt.execute_read_document({"filename": str(path), "_max_tokens": 2000}, "native-budget", ["pdf"])
    assert result["mode"] == "native"
    assert "content" not in result
    monkeypatch.setattr(bt.file_memory, "native_doc_max_bytes", lambda: 4)
    limited = bt.execute_read_document({"filename": str(path), "_max_tokens": 2000}, "native-budget", ["pdf"])
    assert "超过读取上限" in limited["error"]


def test_auto_native_over_quota_can_still_fall_back_to_small_text(tmp_path, monkeypatch):
    from factory import file_factory
    path = tmp_path / "fallback.pdf"
    path.write_bytes(b"native document bytes")
    monkeypatch.setattr(bt.file_memory, "find_file_memory_record", lambda *_: None)
    monkeypatch.setattr(bt.file_memory, "native_doc_max_bytes", lambda: 4)
    monkeypatch.setattr(file_factory, "extract_text_from_bytes", lambda *_: "fallback text")
    result = bt.execute_read_document({"filename": str(path), "_max_tokens": 2000}, "native-fallback", ["pdf"])
    assert result["mode"] == "text"
    assert result["content"] == "fallback text"
    assert "回退" in result["message"]


def test_command_status_uses_actual_exit_code_not_directory_name(tmp_path, monkeypatch):
    directory = tmp_path / "exit=7"
    directory.mkdir()
    monkeypatch.setattr(bt, "_run_command_foreground", lambda *_: (0, "ok", "", False, 0.1, None))
    result = bt.execute_run_command({"command": "echo ok", "work_dir": str(directory), '_structured_output': True})
    assert result["status"] == "completed"
    assert result["exit_code"] == 0


def test_cancelled_foreground_command_terminates_child(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "_BG_LOG_DIR", tmp_path)
    monkeypatch.setattr(bt, "_run_command_foreground", bt._run_foreground_direct)
    code = "import os,time; print(os.getpid(),flush=True); time.sleep(30)"
    if os.name == "nt":
        shell, command = "powershell", f"& '{sys.executable}' -u -c '{code}'"
    else:
        import shlex
        shell, command = "sh", shlex.join([sys.executable, "-u", "-c", code])
    async def run():
        running = asyncio.create_task(run_sync_tool(bt.execute_run_command,
                                                    {"command": command, "shell": shell, "timeout_seconds": 30}))
        pid = None
        try:
            for _ in range(100):
                for output in tmp_path.glob("fg_*.out.txt"):
                    text = output.read_text(encoding="utf-8", errors="ignore").strip()
                    if text.isdigit():
                        pid = int(text)
                        break
                if pid is not None or running.done():
                    break
                await asyncio.sleep(0.05)
            assert pid is not None, "child must start before testing cancellation"
        finally:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        alive = bt._pid_alive_windows(pid) if os.name == "nt" else bt._pid_alive_posix(pid)
        assert not alive
    asyncio.run(run())


def test_unsafe_http_paging_does_not_repeat_request():
    with patch.object(web, "_http_request") as request:
        with pytest.raises(ValueError, match="GET/HEAD"):
            web._fetch_url_impl("https://example.test/api", "text", "", "", 8000, 30,
                                "", "POST", "{}", None, offset=200)
    request.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="PowerShell cmdlet exit status")
def test_powershell_cmdlet_failure_has_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "_BG_LOG_DIR", tmp_path)
    monkeypatch.setattr(bt, "_run_command_foreground", bt._run_foreground_direct)
    result = bt.execute_run_command({"command": "Write-Error 'audit expected failure'",
                                     "shell": "powershell", '_structured_output': True})
    assert result["exit_code"] != 0
    assert result["status"] == "failed"


def test_child_report_budget_keeps_conclusion_and_evidence():
    from types import SimpleNamespace
    from factory.agent_runtime.sub_agent import SubAgentRunner
    from factory.agent_runtime.chat_runtime import estimate_text_tokens
    runner = object.__new__(SubAgentRunner)
    original = "结论：已核对。\n" + "详细内容" * 3000 + "\n证据路径：C:/project/result.txt"
    runner._final_reply = original
    runner._status, runner._error, runner.rounds = "done", None, 2
    runner._file_conflicts = []
    runner.context = SimpleNamespace(reply_max_chars=30000, reply_token_budget=256,
                                     agent_id="child", parent_tool_call_id="call")
    runner.usage_accumulator = SimpleNamespace(merge_to=lambda *_: None)
    result = runner._make_result()
    assert estimate_text_tokens(result.final_reply) <= 256
    assert result.final_reply.startswith("结论")
    assert "证据路径" in result.final_reply
    assert runner._final_reply == original
