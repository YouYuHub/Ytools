"""Private stateful Responses journal, atomic commits and recoverable checkpoints."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from util.file_lock import cross_process_lock
from util.process_identity import process_identity
from util.atomic_json import write_json
from .errors import ResponsesProtocolError

NATIVE_ROOT = Path(__file__).resolve().parents[2] / "history_files"


def _root():
    memory = sys.modules.get("memory.chat_memory")
    return Path(memory.HISTORY_ROOT) if memory is not None else NATIVE_ROOT


def session_directory(session_id):
    root = (_root() / "sidecars" / "responses").resolve()
    result = (root / hashlib.sha256(str(session_id).encode()).hexdigest()).resolve()
    if result.parent != root:
        raise ResponsesProtocolError("Responses 会话路径无效")
    return result


def _scope_key(scope):
    return hashlib.sha256(str(scope).encode()).hexdigest()


def _state_path(session_id, scope):
    return session_directory(session_id) / (_scope_key(scope) + ".state.json")


def _record_path(session_id, record_id):
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise ResponsesProtocolError("Responses 记录引用无效")
    return session_directory(session_id) / (record_id + ".json")


def _guard(session_id, scope):
    lock = _root() / "lock" / ("responses-" + hashlib.sha256((str(session_id) + ":" + str(scope)).encode()).hexdigest() + ".lock")
    return cross_process_lock(lock)


def _write(path, value):
    write_json(path, value)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ResponsesProtocolError("Responses 状态文件无法读取，请使用本地上下文重新同步") from exc


def source_key(config):
    value = [config.get("url"), config.get("selected_provider_name"), config.get("selected_model_id"),
             config.get("apiKey"), config.get("_custom_headers")]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_state(session_id, scope="chat_model"):
    state = _read(_state_path(session_id, scope))
    if state is None:
        return {"version": 1, "session_id": str(session_id), "scope": scope,
                "generation": 0, "status": "new", "head_response_id": None,
                "chain_epoch": uuid.uuid4().hex, "pending_request_ref": None}
    if state.get("version") != 1 or state.get("session_id") != str(session_id) or state.get("scope") != scope:
        raise ResponsesProtocolError("Responses 状态作用域不匹配")
    lease = state.get("compaction_lease")
    if lease and process_identity(int(lease.get("pid") or 0)) != lease.get("process_identity"):
        # Clear only a lease whose owner process has gone away. Pending requests
        # remain uncertain and still require recovery before another generation.
        state.pop("compaction_lease", None)
        state["status"] = ("recovery_required" if state.get("pending_request_ref") else
                           "ready_to_rebase" if state.get("pending_compaction_ref") else "ready")
    return state


def load_record(session_id, reference):
    if not isinstance(reference, dict) or reference.get("protocol") != "responses":
        return None
    record = _read(_record_path(session_id, reference.get("record_id")))
    if record and record.get("session_id") == str(session_id):
        return record
    return None


def current_window(session_id, state):
    if state.get("pending_compaction_ref"):
        record = _read(_record_path(session_id, state["pending_compaction_ref"]))
        if not record:
            raise ResponsesProtocolError("Responses 压缩窗口丢失")
        output = (record.get("response") or {}).get("output") if record.get("kind") == "response" else record.get("output")
        if not output:
            raise ResponsesProtocolError("Responses 压缩窗口丢失")
        return copy.deepcopy(output)
    record_id, chain, visited, window = state.get("head_record_id"), [], set(), []
    while record_id:
        if record_id in visited or len(visited) >= 10000:
            raise ResponsesProtocolError("Responses 恢复记录链无效")
        visited.add(record_id)
        record = _read(_record_path(session_id, record_id))
        if not record:
            raise ResponsesProtocolError("Responses 恢复窗口丢失")
        if "window" in record:  # Read older snapshots as well as delta journals.
            window = copy.deepcopy(record["window"])
            break
        if record.get("window_reset"):
            output = copy.deepcopy(record["response"].get("output") or [])
            position = max(i for i, item in enumerate(output) if item.get("type") == "compaction")
            window = output[position:]
            break
        chain.append(record)
        record_id = record.get("window_parent_ref")
    for record in reversed(chain):
        request = _read(_record_path(session_id, record["request_ref"]))
        if not request:
            raise ResponsesProtocolError("Responses 原始请求记录丢失")
        window.extend(copy.deepcopy(request["payload"].get("input") or []))
        window.extend(copy.deepcopy(record["response"].get("output") or []))
    return window


def _mirror(session_id, state):
    if state.get("scope") != "chat_model":
        return
    memory = sys.modules.get("memory.chat_memory")
    if memory is not None and hasattr(memory, "update_responses_session_meta"):
        memory.update_responses_session_meta(session_id, public_state(state, include_ids=True))


def public_state(state, *, include_ids=False):
    keys = ("version", "scope", "status", "generation", "chain_epoch", "created_at", "compact_strategy",
            "head_created_at", "chain_started_at", "last_compaction", "rebuild_source", "error", "responses_mode")
    result = {key: copy.deepcopy(state[key]) for key in keys if key in state}
    result["has_remote_head"] = bool(state.get("head_response_id")) and state.get("responses_mode") != "stateless"
    result["has_compacted_window"] = bool(state.get("pending_compaction_ref"))
    if include_ids:
        for key in ("head_response_id", "head_record_id", "pending_request_ref",
                    "pending_compaction_ref", "source_key", "covered_input_seq"):
            if key in state:
                result[key] = state[key]
    started = state.get("chain_started_at") or state.get("head_created_at")
    result["age_risk"] = bool(started and time.time() - started >= 20 * 86400) and state.get("responses_mode") != "stateless"
    return result


def invalidate(session_id, reason, scope="chat_model", *, source="local", mirror=True):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        state.update(generation=state["generation"] + 1, status="rebuild_pending",
                     head_response_id=None, head_record_id=None, pending_request_ref=None,
                     pending_compaction_ref=None, rebuild_source=source, error=reason,
                     chain_epoch=uuid.uuid4().hex)
        state.pop("compaction_lease", None)
        for key in ("source_key", "compact_strategy", "last_compaction", "chain_started_at", "head_created_at", "responses_mode", "omit_encrypted_include_with_previous"):
            state.pop(key, None)
        _write(_state_path(session_id, scope), state)
    if mirror:
        _mirror(session_id, state)
    return state


def begin_request(session_id, scope, payload, config, input_ids, context_revision=None, snapshot=None,
                  compaction=False):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        if state.get("compaction_lease") and not compaction:
            raise ResponsesProtocolError("会话正在压缩，请等待压缩完成")
        if snapshot is not None and (state["generation"] != snapshot["generation"]
                                    or state.get("head_response_id") != snapshot.get("head_response_id")):
            raise ResponsesProtocolError("Responses 上下文已改变，未发送旧请求")
        if state.get("pending_request_ref"):
            raise ResponsesProtocolError("Responses 有尚未确认的请求，请先恢复或重新同步")
        expected_head = payload.get("previous_response_id")
        if expected_head and expected_head != state.get("head_response_id"):
            raise ResponsesProtocolError("Responses 响应链已改变，请重试当前任务")
        record_id = uuid.uuid4().hex
        record = {"version": 1, "kind": "request", "session_id": str(session_id), "scope": scope,
                  "record_id": record_id, "created_at": time.time(), "generation": state["generation"],
                  "source_key": source_key(config), "context_revision": context_revision,
                  "input_ids": list(input_ids), "payload": copy.deepcopy(payload),
                  "parent_record_ref": state.get("head_record_id") if expected_head else None,
                  "response_id": None, "status": "planned"}
        _write(_record_path(session_id, record_id), record)
        state.update(pending_request_ref=record_id, status="request_pending",
                     responses_mode="stateless" if payload.get("store") is False else "stateful")
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)
    return record


def omit_encrypted_include_with_previous(session_id, scope):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        state["omit_encrypted_include_with_previous"] = True
        _write(_state_path(session_id, scope), state)
    return state


def note_response_id(session_id, record, response_id):
    if not response_id:
        return
    record.update(response_id=response_id, status="accepted")
    _write(_record_path(session_id, record["record_id"]), record)


def note_request_failure(session_id, record, operation, error, detail):
    """Keep the original stream failure when a later recovery GET also fails."""
    failure = {"operation": operation, "at": time.time(),
               "error_type": type(error).__name__, "detail": detail[:1800]}
    status = getattr(error, "status_code", None)
    if status is not None:
        failure["http_status"] = status
    record["failures"] = [*(record.get("failures") or []), failure][-10:]
    _write(_record_path(session_id, record["record_id"]), record)


def abandon_request(session_id, record, error, *, unknown=False):
    with _guard(session_id, record["scope"]):
        state = load_state(session_id, record["scope"])
        record.update(status="unknown" if unknown else "failed", error=str(error))
        _write(_record_path(session_id, record["record_id"]), record)
        if state.get("pending_request_ref") == record["record_id"]:
            state.update(status="recovery_required" if unknown else
                         "ready_to_rebase" if state.get("pending_compaction_ref") else "ready",
                         error=str(error))
            if not unknown:
                state["pending_request_ref"] = None
            _write(_state_path(session_id, record["scope"]), state)
    _mirror(session_id, state)


def commit_response(session_id, request_record, response):
    with _guard(session_id, request_record["scope"]):
        state = load_state(session_id, request_record["scope"])
        if state.get("head_response_id") == response["id"] and state.get("head_record_id"):
            return {"protocol": "responses", "record_id": state["head_record_id"], "scope": state["scope"]}
        if state["generation"] != request_record["generation"] or state.get("pending_request_ref") != request_record["record_id"]:
            raise ResponsesProtocolError("Responses 迟到结果已失效，未提交工具或响应链")
        payload = request_record["payload"]
        record_id = uuid.uuid4().hex
        tool_status = {item["call_id"]: {"status": "planned"}
                       for item in response.get("output") or [] if item.get("type") == "function_call"}
        record = {"version": 1, "kind": "response", "session_id": str(session_id), "scope": state["scope"],
                  "record_id": record_id, "request_ref": request_record["record_id"],
                  "response_id": response["id"], "previous_response_id": payload.get("previous_response_id"),
                  "response": copy.deepcopy(response), "tool_status": tool_status,
                  "window_parent_ref": request_record.get("parent_record_ref"),
                  "window_reset": any(item.get("type") == "compaction" for item in response.get("output") or [])}
        if "parent_window" in request_record:  # Recover an older in-flight request.
            record["window"] = [*request_record["parent_window"], *copy.deepcopy(payload.get("input") or []),
                                *copy.deepcopy(response.get("output") or [])]
        _write(_record_path(session_id, record_id), record)
        request_record.update(status="committed", response_ref=record_id)
        _write(_record_path(session_id, request_record["record_id"]), request_record)
        if not payload.get("previous_response_id"):
            state["chain_epoch"] = uuid.uuid4().hex
            state["chain_started_at"] = response.get("created_at") or time.time()
            if any(item.get("type") == "compaction" for item in payload.get("input") or []):
                last = state.setdefault("last_compaction", {})
                last.update(activation="active", activated_response_id=response["id"],
                            activated_at=time.time(), input_tokens_after=(response.get("usage") or {}).get("input_tokens"))
        state.update(status="ready", head_response_id=response["id"], head_record_id=record_id,
                     head_created_at=response.get("created_at") or time.time(),
                     source_key=request_record["source_key"], pending_request_ref=None,
                     pending_compaction_ref=None, error=None,
                     covered_input_seq=state.get("covered_input_seq", 0) + len(request_record["input_ids"]))
        if (any(item.get("type") == "compaction_trigger" for item in payload.get("input") or [])
                and any(item.get("type") == "compaction" and item.get("encrypted_content") for item in response.get("output") or [])):
            _stage_trigger_activation(state, record_id, response)
        _write(_state_path(session_id, state["scope"]), state)
    _mirror(session_id, state)
    return {"protocol": "responses", "record_id": record_id, "scope": state["scope"]}


def pending_request(session_id, scope):
    state = load_state(session_id, scope)
    return _read(_record_path(session_id, state["pending_request_ref"])) if state.get("pending_request_ref") else None


def require_recovery(session_id, scope, error):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        state.update(status="recovery_required", error=error)
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)


def tool_started(session_id, reference, call_id):
    if not reference or reference.get("protocol") != "responses":
        return
    with _guard(session_id, reference["scope"]):
        record = load_record(session_id, reference)
        if not record or call_id not in record["tool_status"]:
            raise ResponsesProtocolError("Responses 工具未被完整响应声明")
        status = record["tool_status"][call_id].get("status")
        if status == "completed":
            return
        if status in {"running", "unknown"}:
            raise ResponsesProtocolError("该工具上次执行状态未知，请核对实际结果后再继续：" + call_id)
        record["tool_status"][call_id] = {"status": "running", "started_at": time.time()}
        _write(_record_path(session_id, record["record_id"]), record)


def tool_completed(session_id, reference, call_id, content, is_error=False):
    if not reference or reference.get("protocol") != "responses":
        return
    with _guard(session_id, reference["scope"]):
        record = load_record(session_id, reference)
        if record and call_id in record["tool_status"]:
            record["tool_status"][call_id] = {"status": "completed", "content": copy.deepcopy(content),
                                            "is_error": bool(is_error)}
            _write(_record_path(session_id, record["record_id"]), record)


def reset_tool(session_id, reference, call_id):
    """Rewind a natively started slot the execution ledger proves never ran.

    Suspend preserves native running slots so a resume can continue them, but
    the replay re-runs tool_started, which refuses running/unknown slots. Only
    slots the ledger clears (no record, completed cache or replayable dispatch)
    may rewind to planned; 'executing' slots keep their unknown state so the
    replay still refuses them. Returns True when a slot was rewound.
    """
    if not reference or reference.get("protocol") != "responses":
        return False
    with _guard(session_id, reference["scope"]):
        record = load_record(session_id, reference)
        if not record or call_id not in record["tool_status"]:
            return False
        if record["tool_status"][call_id].get("status") not in {"running", "unknown"}:
            return False
        record["tool_status"][call_id] = {"status": "planned"}
        _write(_record_path(session_id, record["record_id"]), record)
        return True


def tool_result(session_id, reference, call_id):
    record = load_record(session_id, reference)
    value = (record or {}).get("tool_status", {}).get(call_id)
    return value if value and value.get("status") == "completed" else None


def reusable_compaction_ref(state, *, has_pending_input=False):
    """Reuse a staged window, or a trigger head whose inputs have not changed."""
    if state.get("pending_compaction_ref"):
        return state["pending_compaction_ref"]
    last = state.get("last_compaction") or {}
    if (not has_pending_input and last.get("phase") == "done" and last.get("mode") == "trigger"
            and state.get("head_record_id") and last.get("record_ref") == state["head_record_id"]
            and last.get("response_id") == state.get("head_response_id")):
        return state["head_record_id"]
    return None


def needs_compaction_activation(state):
    """Older trigger chains also need a one-time root from their native window."""
    last = state.get("last_compaction") or {}
    return (last.get("phase") == "done" and last.get("mode") == "trigger"
            and last.get("activation") != "active")


def compaction_input_ids(session_id, reference):
    record = _read(_record_path(session_id, reference))
    if not record or record.get("session_id") != str(session_id):
        raise ResponsesProtocolError("Responses 压缩边界记录丢失")
    if record.get("kind") == "response":
        record = _read(_record_path(session_id, record["request_ref"]))
        if not record:
            raise ResponsesProtocolError("Responses 压缩请求记录丢失")
        return record.get("input_ids") or []
    return record.get("boundary_ids") or []


def save_compaction(session_id, scope, compacted, expected_state, *, boundary_ids=()):
    output = compacted.get("output")
    if compacted.get("object") != "response.compaction" or not isinstance(output, list) or not output:
        raise ResponsesProtocolError("官方 compact 没有返回完整的压缩窗口")
    if not any(item.get("type") == "compaction" and item.get("encrypted_content") for item in output):
        raise ResponsesProtocolError("官方 compact 缺少完成的 compaction item")
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        if state["generation"] != expected_state["generation"] or state.get("head_response_id") != expected_state.get("head_response_id"):
            raise ResponsesProtocolError("压缩期间会话已改变，压缩结果未激活")
        record_id = uuid.uuid4().hex
        _write(_record_path(session_id, record_id), {
            "version": 1, "kind": "compaction", "session_id": str(session_id), "scope": scope,
            "output": copy.deepcopy(output), "response": copy.deepcopy(compacted),
            "boundary_ids": list(boundary_ids), "created_at": time.time()})
        state.update(pending_compaction_ref=record_id,
                     status="compacting" if state.get("compaction_lease") else "ready_to_rebase", error=None,
                     last_compaction={"phase": "done", "created_at": time.time(), "record_ref": record_id,
                                      "mode": "standalone", "activation": "pending_rebase",
                                      "usage": copy.deepcopy(compacted.get("usage") or {})})
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)
    return record_id


def compaction_failed(session_id, scope, error):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        state["last_compaction"] = {"phase": "error", "error": str(error), "created_at": time.time()}
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)


def set_compaction_local_status(session_id, scope, phase, error=None):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        state.setdefault("last_compaction", {})["local_phase"] = phase
        state["last_compaction"]["local_error"] = str(error) if error else None
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)


def stage_rebuild(session_id, items, *, source="local", scope="chat_model"):
    """Stage one root input window; no extra model call just to obtain an ID."""
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        if state.get("compaction_lease"):
            raise ResponsesProtocolError("会话正在压缩，请等待完成后重新同步")
        record_id = uuid.uuid4().hex
        _write(_record_path(session_id, record_id), {
            "version": 1, "kind": "rebuild", "session_id": str(session_id), "scope": scope,
            "output": copy.deepcopy(items), "boundary_ids": [], "created_at": time.time()})
        state.update(generation=state["generation"] + 1, head_response_id=None,
                     head_record_id=None, pending_request_ref=None,
                     pending_compaction_ref=record_id, rebuild_source=source,
                     status="rebuild_pending", error=None)
        if source != "native":
            state.pop("source_key", None)
            state.pop("last_compaction", None)
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)
    return public_state(state)


def mark_compacting(session_id, scope, active):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        if active:
            if state.get("compaction_lease") or state.get("pending_request_ref"):
                raise ResponsesProtocolError("会话已有生成或压缩任务")
            state["status"] = "compacting"
            state["compaction_lease"] = {"started_at": time.time(), "pid": os.getpid(),
                                         "process_identity": process_identity(os.getpid())}
        else:
            state.pop("compaction_lease", None)
            if state.get("status") in {"compacting", "ready"}:
                state["status"] = "ready_to_rebase" if state.get("pending_compaction_ref") else "ready"
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)


def _stage_trigger_activation(state, record_id, response):
    state["compact_strategy"] = "trigger"
    state["last_compaction"] = {
        "phase": "done", "mode": "trigger", "created_at": time.time(),
        "record_ref": record_id, "response_id": response["id"],
        "activation": "pending_rebase", "usage": copy.deepcopy(response.get("usage") or {})}
    # Receiving a compaction item does not prove a gateway pruned the chain.
    state.update(pending_compaction_ref=record_id,
                 status="compacting" if state.get("compaction_lease") else "ready_to_rebase")


def note_trigger_compaction(session_id, scope, reference, response):
    with _guard(session_id, scope):
        state = load_state(session_id, scope)
        _stage_trigger_activation(state, reference["record_id"], response)
        _write(_state_path(session_id, scope), state)
    _mirror(session_id, state)
