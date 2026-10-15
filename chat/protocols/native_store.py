"""Private, atomic Messages replay records. Public history contains references only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from util.atomic_json import write_json

NATIVE_ROOT = Path(__file__).resolve().parents[2] / "history_files" / "sidecars" / "messages"


def _root():
    memory = sys.modules.get("memory.chat_memory")
    if memory is not None:
        return Path(memory.HISTORY_ROOT) / "sidecars" / "messages"
    return NATIVE_ROOT


def _path(session_id, record_id):
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise ValueError("Invalid native replay reference")
    session_key = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()
    return _root() / session_key / (record_id + ".json")


def session_directory(session_id):
    root = _root().resolve()
    directory = (root / hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()).resolve()
    if directory.parent != root:
        raise ValueError("Native session path escaped its storage root")
    return directory


def source_key(config):
    # Credentials are hashed, never persisted as part of a request snapshot.
    source = [config.get("url"), config.get("selected_provider_name"),
              config.get("selected_model_id"), config.get("apiKey"),
              config.get("_custom_headers")]
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _write(path, record):
    write_json(path, record)


def load_record(session_id, reference):
    if not isinstance(reference, dict) or reference.get("protocol") != "messages":
        return None
    try:
        record = json.loads(_path(session_id, reference.get("record_id")).read_text(encoding="utf-8"))
        return record if record.get("session_id") == str(session_id) and record.get("version") == 1 else None
    except (OSError, ValueError, TypeError):
        return None


def save_record(session_id, config, payload, response, *, parent=None, appended=None):
    record_id = uuid.uuid4().hex
    content = copy.deepcopy(response["content"])
    record = {
        "version": 1, "session_id": str(session_id), "record_id": record_id,
        "source_key": source_key(config), "response_id": response.get("id"),
        "stop_reason": response.get("stop_reason"), "content": content,
        "epoch": parent["epoch"] if parent else uuid.uuid4().hex,
        "parent_id": parent["record_id"] if parent else None,
        "tools": copy.deepcopy(payload.get("tools")),
        "system": copy.deepcopy(payload.get("system")),
        "thinking": copy.deepcopy(payload.get("thinking")),
        "output_config": copy.deepcopy(payload.get("output_config")),
        "tool_status": {block["id"]: {"status": "planned"} for block in content
                        if block.get("type") == "tool_use"},
    }
    if parent:
        record["appended"] = copy.deepcopy(appended or [])
    else:
        record["messages"] = copy.deepcopy(payload["messages"])
    _write(_path(session_id, record_id), record)
    return {"protocol": "messages", "record_id": record_id}


def replay_messages(session_id, record):
    chain = []
    seen = set()
    cursor = record
    while cursor is not None:
        if cursor["record_id"] in seen or len(chain) >= 1000:
            raise ValueError("Invalid native replay chain")
        seen.add(cursor["record_id"])
        chain.append(cursor)
        parent_id = cursor.get("parent_id")
        if not parent_id:
            break
        cursor = load_record(session_id, {"protocol": "messages", "record_id": parent_id})
        if cursor is None:
            raise ValueError("Native replay snapshot is missing; start a new context")
    messages = []
    for item in reversed(chain):
        if item.get("parent_id"):
            messages.extend(copy.deepcopy(item.get("appended", [])))
        else:
            messages = copy.deepcopy(item["messages"])
        messages.append({"role": "assistant", "content": copy.deepcopy(item["content"])})
    return messages


def tool_started(session_id, reference, call_id):
    if isinstance(reference, dict) and reference.get("protocol") == "responses":
        from .responses_store import tool_started as start
        return start(session_id, reference, call_id)
    record = load_record(session_id, reference)
    if record and call_id in record["tool_status"]:
        record["tool_status"][call_id] = {"status": "running"}
        _write(_path(session_id, record["record_id"]), record)


def tool_completed(session_id, reference, call_id, content, is_error=False):
    if isinstance(reference, dict) and reference.get("protocol") == "responses":
        from .responses_store import tool_completed as complete
        return complete(session_id, reference, call_id, content, is_error)
    record = load_record(session_id, reference)
    if record and call_id in record["tool_status"]:
        record["tool_status"][call_id] = {"status": "completed", "content": content, "is_error": bool(is_error)}
        _write(_path(session_id, record["record_id"]), record)


def reset_replay(messages):
    """Keep facts, permanently retire references when the client edits the prefix."""
    for message in messages:
        if isinstance(message, dict) and message.get("provider_state", {}).get("protocol") == "messages":
            message["provider_state"] = {"protocol": "messages", "reset": True}
