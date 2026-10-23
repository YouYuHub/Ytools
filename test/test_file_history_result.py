import asyncio
import json
from unittest.mock import patch

import pytest

from factory.agent_runtime.file_history_result import record_result
from memory import file_history


def result(path, old="old", new="new"):
    return {"success": True, "_file_history": {"path": str(path), "old_text": old, "new_text": new},
            "_model_result": {"success": True}, "_file_diff": {"path": str(path)}}


@pytest.mark.parametrize("outcome, expected", [
    ({"recorded": True}, {"history_recorded": True}),
    ({"recorded": False, "skipped": "unchanged"},
     {"history_recorded": False, "history_reason": "unchanged"}),
    ({"recorded": False, "skipped": "file_too_large"},
     {"history_recorded": False, "history_reason": "file_too_large"}),
])
def test_outcome_reaches_model_and_ui(tmp_path, outcome, expected):
    raw = result(tmp_path / "file.py")
    with patch.object(file_history, "record_change", return_value=outcome):
        record_result(raw, "s", "edit_file", 1)
    from factory.chat_factory import _format_tool_result
    model = json.loads(_format_tool_result(raw))
    for key, value in expected.items():
        assert raw[key] == model[key] == raw["_file_diff"][key] == value
    assert "_file_history" not in model


def test_record_failure_does_not_turn_write_into_failure(tmp_path):
    raw = result(tmp_path / "f")
    with patch.object(file_history, "record_change", side_effect=OSError("disk full")):
        record_result(raw, "s", "write_file", 1)
    assert raw["success"] is True
    assert raw["history_recorded"] is False
    assert raw["history_reason"] == "record_failed"


def test_actual_size_limit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", tmp_path / "history")
    monkeypatch.setattr(file_history.file_snapshots,'MAX_BYTES',128)
    raw = result(tmp_path / "f", new="a" * 129)
    from factory.chat_factory import _record_file_history
    asyncio.run(_record_file_history("s", "edit_file", raw, 1))
    assert raw["history_reason"] == "file_too_large"


def test_non_file_result_is_unchanged():
    raw = {"success": True}
    record_result(raw, "s", "read_file", 1)
    assert raw == {"success": True}


def test_patch_per_file_recording_outcomes_reach_batch_display_and_model(tmp_path):
    files=[{'path':str(tmp_path/name)} for name in ['a','b']]
    raw={'success':True,'files':files,'_file_diff':{'files':[dict(f) for f in files]},
         '_file_history':[{'path':f['path'],'old_text':'old','new_text':'new'} for f in files]}
    with patch.object(file_history,'record_change',side_effect=[{'recorded':True},{'recorded':False,'skipped':'history_quota_exceeded'}]):
        record_result(raw,'s','apply_patch',1)
    assert raw['success'] is True and raw['history_recorded'] is False
    assert raw['_file_diff']['history_recorded'] is False
    assert raw['files'][0]['history_recorded'] is True
    assert raw['_file_diff']['files'][1]['history_reason']=='history_quota_exceeded'
