"""Resolve templates to normal, isolated session configuration. No LLM/tool calls."""
import copy
import hashlib
import json
import ntpath
from pathlib import Path

import env_manager
from chat.protocols import normalize_api_type
from config import get_current_dir, get_global_tool_inputs, get_persisted_work_dir
from factory.agent_runtime import tool_registry
from factory.agent_runtime.builtin_tools import BUILTIN_TOOL_SERVER_KEY, SELECTABLE_BUILTIN_TOOL_NAMES
from memory import chat_memory as memory, task_cost_store as costs
from util.file_lock import cross_process_lock
from util.timestamp_utils import now_str

ROLES = ("chat_model", "compaction_model", "title_model", "sub_agent_model")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def tool_catalog():
    tools, servers, originals, _ = tool_registry.get_tool_registry_snapshot()
    result = [{"server": BUILTIN_TOOL_SERVER_KEY, "name": name, "call_name": name, "description": "内置工具"}
              for name in SELECTABLE_BUILTIN_TOOL_NAMES]
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        server = servers.get(name)
        if name and server and server != BUILTIN_TOOL_SERVER_KEY:
            result.append({"server": server, "name": originals.get(name, name), "call_name": name,
                           "description": str(tool.get("function", {}).get("description") or "")[:300]})
    return result


def base_directory(source):
    if source:
        path, warning = memory.resolve_session_work_dir(source)
    else:
        path, warning = get_persisted_work_dir(), None
    return str(Path(path or get_current_dir()).expanduser().resolve()), warning


def selected_tools(source):
    if source:
        selection, warning = memory.resolve_session_tool_selection(source)
        if warning:
            raise ValueError(warning)
        return selection or {}
    selection, _servers, error = get_global_tool_inputs()
    if error:
        raise ValueError("默认工具配置读取失败，请先检查 MCP 配置")
    return selection


def canonical_tools(selection, catalog):
    """Saved templates keep (server, original name), independent of model aliases."""
    result = {}
    for server, names in selection.items():
        for name in names:
            found = next((t for t in catalog if t["server"] == server and name in {t["name"], t["call_name"]}), None)
            canonical = found["name"] if found else name
            if canonical not in result.setdefault(server, []):
                result[server].append(canonical)
    return result


def options(source=None):
    catalog = tool_catalog()
    selection, warnings = memory.resolve_session_model_selection(source) if source else (env_manager.get_global_model_selection(), [])
    directory, warning = base_directory(source)
    limits = costs.config(source)
    tools = canonical_tools(selected_tools(source), catalog)
    return {"models": [{"provider": m["provider_name"], "model": m["model_name"], "api_type": normalize_api_type(m.get("api_type"))}
                       for m in env_manager.list_available_models() if normalize_api_type(m.get("api_type")) in {"chat_completions", "messages", "responses"}],
            "tools": catalog, "currency": limits["pricing"]["currency"], "warnings": list(warnings or []) + ([warning] if warning else []),
            "capture": {"models": {role: {"provider": entry["ownership_name"], "model": entry["model_name"]}
                                   for role, entry in selection.items() if entry.get("ownership_name") and entry.get("model_name")},
                        "tools": tools, "budget": limits["limits"], "budget_currency": limits["pricing"]["currency"],
                        "work_dir": {"mode": "fixed", "path": directory}}}


def prepare(definition, source=None):
    global_models = env_manager.get_global_model_selection()
    models = copy.deepcopy(global_models)
    for role, target in definition["models"].items():
        defaults = global_models.get(role) or {}
        models[role] = env_manager.build_model_selection_entry(target["provider"], target["model"], role,
                            inherit_parameter=defaults.get("parameter"), inherit_headers=defaults.get("headers"))
    chat = models.get("chat_model") or {}
    if not chat.get("ownership_name") or not chat.get("model_name"):
        raise ValueError("请为模板选择聊天模型，或先配置全局默认聊天模型")
    for role, entry in models.items():
        if entry.get("ownership_name") and entry.get("model_name") and env_manager.get_model_config(entry["ownership_name"], entry["model_name"]) is None:
            raise ValueError("模型已移除或不可用：" + role + " / " + entry["ownership_name"] + " / " + entry["model_name"])
    catalog = tool_catalog()
    _configured, servers, error = get_global_tool_inputs()
    if error:
        raise ValueError("MCP 工具配置读取失败，请修正后重试")
    selection = definition["tools"] if definition["tools"] is not None else selected_tools(source)
    selection = canonical_tools(selection, catalog)
    resolved, visible = {}, []
    for server, names in selection.items():
        if names and server != BUILTIN_TOOL_SERVER_KEY and server not in servers:
            raise ValueError("MCP 服务已移除：" + server)
        for name in names:
            found = next((t for t in catalog if t["server"] == server and name in {t["name"], t["call_name"]}), None)
            if not found:
                raise ValueError("工具未发现或已移除：" + server + " / " + name + "；请刷新工具清单或修改模板")
            resolved.setdefault(server, []).append(found["call_name"])
            visible.append({"server": server, "name": found["name"]})
    if "read_media" in resolved.get(BUILTIN_TOOL_SERVER_KEY, []) and not (env_manager.get_model_config(chat["ownership_name"], chat["model_name"]) or {}).get("vision", False):
        raise ValueError("聊天模型不支持视觉，不能启用 read_media；请更换模型或取消该工具")
    current = costs.config(source)
    limits = costs.validate_limits(definition["budget"] if definition["budget"] is not None else current["limits"])
    currency = current["pricing"]["currency"]
    if definition["budget"] is not None and limits["cost"] > 0 and definition["budget_currency"] != currency:
        raise ValueError("模板的费用预算币种与当前计价币种不一致，请更新费用预算后再应用")
    base, warning = base_directory(source)
    rule = definition["work_dir"]
    if rule["mode"] == "inherit":
        directory = Path(base)
    elif rule["mode"] == "fixed":
        if not Path(rule["path"]).expanduser().is_absolute():
            raise ValueError("固定工作路径必须为绝对路径")
        directory = Path(rule["path"]).expanduser().resolve()
    else:
        relative = rule["path"].replace("\\", "/")
        if Path(relative).is_absolute() or ntpath.splitdrive(relative)[0] or ".." in Path(relative).parts:
            raise ValueError("相对子目录不能包含绝对路径或上级目录 ..")
        directory = (Path(base) / relative).resolve()
        if not directory.is_relative_to(Path(base).resolve()):
            raise ValueError("相对子目录不能指向当前工作目录以外的位置")
    if not directory.is_dir():
        raise ValueError("工作目录不存在，请先创建目录或修改路径：" + str(directory))
    parts = [definition["prompt"].strip()]
    if definition["output_format"].strip():
        parts.append("## 输出格式\n" + definition["output_format"].strip())
    if definition["acceptance"].strip():
        parts.append("## 验收要求\n" + definition["acceptance"].strip())
    prompt = "\n\n".join(part for part in parts if part)
    if not prompt:
        raise ValueError("请填写任务正文、输出格式或验收要求")
    summary = {"name": definition["name"], "models": {role: {"provider": value.get("ownership_name"), "model": value.get("model_name"), "api_type": value.get("api_type")}
                for role, value in models.items() if value.get("ownership_name") and value.get("model_name")},
               "tools": visible, "budget": limits, "currency": currency, "work_dir": str(directory), "prompt": prompt,
               "warnings": [warning] if warning else []}
    settings = {"model_selection": models, "tool_selection": resolved or {memory.EMPTY_TOOL_SELECTION_KEY: []}, "work_dir": str(directory)}
    return {"summary": summary, "fingerprint": digest({"settings": settings, "budget": limits, "currency": currency, "prompt": prompt})}, settings


def start(definition, source, request_id, expected_fingerprint):
    """Only creates a fresh UUID session; never mutates an existing conversation."""
    session = "task-" + request_id.hex
    from security.principal import current
    principal = current()
    if principal:
        session = "task-" + digest({"user": principal.user_id, "request": request_id.hex})[:32]
        from security.authorization import session as authorize_session
        authorize_session(session, create=True)
    signature = digest({"template": definition, "source": source})
    filename = memory.HISTORY_ROOT / (session + "_chat.jsonl")
    with cross_process_lock(memory.LOCK_ROOT / (filename.name + ".lock")):
        if filename.exists():
            previous = memory._read_meta_first_line(filename) or {}
            applied = previous.get("task_template") or {}
            if applied.get("request_signature") != signature:
                raise ValueError("新建请求标识已被其他模板使用，请重新预览")
            # Recover a crash between publishing the JSONL and committing its budget.
            with costs.database() as db:
                if db.execute("SELECT 1 FROM settings WHERE scope=?", ("template:" + session,)).fetchone() is None:
                    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("session:" + session, json.dumps(applied["summary"]["budget"])))
                    db.execute("INSERT INTO settings VALUES(?,?)", ("template:" + session, json.dumps(signature)))
            return {"session_id": session, "summary": applied["summary"], "reused": True}
        with costs.database() as db:
            if db.execute("SELECT 1 FROM settings WHERE scope=?", ("template:" + session,)).fetchone():
                raise ValueError("该请求创建的会话已删除，请重新预览后新建")
        plan, settings = prepare(definition, source)
        if plan["fingerprint"] != expected_fingerprint:
            raise ValueError("配置在预览后已变化，请重新预览后再确认")
        meta = memory._default_meta(session)
        meta.update(settings)
        meta.update({"title": definition["name"], "config_snapshot_at": now_str(),
                     "task_template": {"name": definition["name"], "request_signature": signature,
                                       "initial_prompt": plan["summary"]["prompt"], "summary": plan["summary"]}})
        memory.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            with costs.database() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("session:" + session, json.dumps(plan["summary"]["budget"])))
                db.execute("INSERT INTO settings VALUES(?,?)", ("template:" + session, json.dumps(signature)))
                memory._write_meta_and_entries(filename, meta, [])
        except Exception:
            # UUID is unpublished and guarded; a failed cross-store commit must not expose a half-configured session.
            filename.unlink(missing_ok=True)
            raise
    return {"session_id": session, "summary": plan["summary"], "reused": False}
