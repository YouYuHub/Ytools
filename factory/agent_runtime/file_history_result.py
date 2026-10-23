"""Record file versions and expose the outcome without failing a successful write."""
from memory import file_history
import os
from pathlib import Path
from util.logger import get_logger


def record_result(result, session_id, tool, round_number):
    if not isinstance(result, dict):
        return
    payload = result.get("_file_history")
    if isinstance(payload, list):
        key = lambda path: os.path.normcase(str(Path(path).resolve()))
        reports = {key(item['path']): item for item in result.get('files', [])}
        batch = result.get('_file_diff')
        diff_reports = {key(item['path']): item for item in batch.get('files', [])} if isinstance(batch, dict) else {}
        outcomes = []
        for entry in payload:
            report = reports.get(key(entry['path']), {})
            child = {'_file_history': entry}
            record_result(child, session_id, tool, round_number)
            fields = {key: value for key, value in child.items() if key.startswith('history_')}
            report.update(fields)
            diff_reports.get(key(entry['path']), {}).update(fields)
            outcomes.append(fields)
        result['history_recorded'] = bool(outcomes) and all(item.get('history_recorded') for item in outcomes)
        if not result['history_recorded']:
            result['history_reason'] = 'see_files'
        fields = {name: value for name, value in result.items() if name.startswith('history_')}
        for target in (batch, result.get('_model_result')):
            if isinstance(target, dict):
                target.update(fields)
        return
    if not isinstance(payload, dict) or not payload.get("path"):
        return
    try:
        outcome = file_history.record_change(
            session_id, path=str(payload["path"]),
            display_path=str(payload.get("display_path") or payload["path"]),
            old_text=payload.get("old_text"), new_text=payload.get("new_text"),
            tool=tool, round_number=int(round_number or 0),
            encoding=str(payload.get("encoding") or "utf-8"),
            old_exists=payload.get('old_exists'), new_exists=payload.get('new_exists'),
            old_encoding=payload.get('old_encoding'),
        )
        fields = {"history_recorded": bool(outcome.get("recorded"))}
        if not fields["history_recorded"]:
            fields["history_reason"] = outcome.get("skipped") or "not_recorded"
    except Exception as exc:
        get_logger(__name__).warning(f"文件已修改，但版本记录失败: {exc}")
        fields = {"history_recorded": False, "history_reason": "record_failed"}
    result.update(fields)
    for key in ("_model_result", "_file_diff"):
        if isinstance(result.get(key), dict):
            result[key].update(fields)
