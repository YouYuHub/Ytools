"""The executor facade stays compatible while pure helpers stay independent."""
import copy
import subprocess
import sys

from factory.agent_runtime import builtin_tools as bt, tool_inputs


def test_pure_helpers_do_not_import_executors_or_mcp():
    result = subprocess.run([sys.executable, '-c',
        "import sys; from factory.agent_runtime import tool_inputs, file_diff_result; "
        "assert 'factory.agent_runtime.builtin_tools' not in sys.modules; "
        "assert 'util.mcp_client' not in sys.modules"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_legacy_policy_overrides_still_apply_through_executor(monkeypatch):
    monkeypatch.setattr(bt, '_ASK_MAX_QUESTIONS', 1)
    assert bt.normalize_ask_questions([{'question': 'one'}, {'question': 'two'}]) is None
    monkeypatch.setattr(bt, '_SUB_AGENT_TASK_MAX_CHARS', 3)
    value, error = bt.normalize_sub_agent_task('four')
    assert value is None and '4 > 3' in error
    monkeypatch.setattr(bt, '_FILE_DIFF_MAX_DIFF_CHARS', 20)
    value = bt._build_file_diff('old\n' * 10, 'new\n' * 10, 'fixture.txt')
    assert value['diff_truncated'] and value['lines_added'] == value['lines_removed'] == 10


def test_input_normalization_is_repeatable_and_does_not_mutate_callers():
    raw = [{'id': 'a', 'content': 'task', 'status': 'pending'}, {'id': 'a', 'content': 'next', 'status': 'in_progress'}]
    previous = [{'id': 'a', 'content': 'task', 'status': 'pending'}]
    before = copy.deepcopy((raw, previous))
    assert bt.normalize_todo_items(raw, previous) == bt.normalize_todo_items(raw, previous)
    assert (raw, previous) == before
    assert tool_inputs.normalize_todo_items(raw, previous) == bt.normalize_todo_items(raw, previous)
