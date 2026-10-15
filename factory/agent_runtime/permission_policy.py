"""Pure tool permission planning. Paths are resolved before, and checked at, use."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

FILE_FIELDS = {"read_file": "full_file_name", "write_file": "full_file_name", "edit_file": "full_file_name", "search_files": "dir_path"}
LOCAL_TOOLS = {*FILE_FIELDS, "read_media", "read_document", "run_command", "poll_command", "stop_command", "todo_write", "ask_user", "sub_agent", "over_task"}
APP_ROOT = Path(__file__).resolve().parents[2]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def server_fingerprint(server):
    if not server or server == "__builtin__":
        return None
    from util.mcp_client import _load_mcp_servers_config
    return fingerprint(_load_mcp_servers_config().get(server))


def inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_path(value, root):
    path = Path(value or ".").expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    return str(path.resolve())


def protected(path):
    path = Path(path).resolve()
    from memory.permission_store import ROOT as control_root
    return (inside(path, control_root) or inside(path, APP_ROOT / "history_files" / "sidecars")
            or (inside(path, APP_ROOT) and (path.name.startswith("permission_") or "tool_approval" in path.name))
            or path in {APP_ROOT / ".env", APP_ROOT / "setting" / "mcp_servers.json",
                        APP_ROOT / "H5" / "js" / "app" / "permissions.js"})


def plan_tool(name, args, context, *, server=None, original_name=None):
    args = json.loads(json.dumps(args))
    is_mcp = bool(server and server != "__builtin__") or name not in LOCAL_TOOLS
    if not is_mcp and any(str(key).startswith("_permission") or key == "permission_mode" for key in args):
        raise ValueError("工具参数不能设置权限")
    root = context["root"]
    paths, sources = [], []
    reason = ""
    if is_mcp:
        reason = "mcp_call"
    elif name == "run_command" and str(args.get("action") or "run").strip().lower() == "run":
        reason = "command_execution"
        args["action"] = "run"
        args["work_dir"] = resolve_path(args.get("work_dir"), root)
        paths.append(args["work_dir"])
        from factory.agent_runtime.builtin_tools import _resolve_shell
        args["shell"] = _resolve_shell(args.get("shell") or "auto", os.name == "nt")[1]
    elif name in FILE_FIELDS:
        field = FILE_FIELDS[name]
        if not args.get(field) and name != "search_files":
            raise ValueError(field + " 不能为空")
        args[field] = resolve_path(args.get(field), root)
        paths.append(args[field])
    elif name == "read_media":
        from factory.agent_runtime.builtin_tools import normalize_read_media_args
        refs, _, error, _, _ = normalize_read_media_args(args)
        if error:
            raise ValueError(error)
        normalized = []
        for ref in refs:
            if not isinstance(ref, str):
                raise ValueError("媒体来源必须是字符串")
            if ref.startswith("media://"):
                normalized.append(ref)
            elif urlsplit(ref).scheme in {"http", "https"}:
                sources.append(ref)
                normalized.append(ref)
            else:
                value = resolve_path(ref, root)
                paths.append(value)
                normalized.append(value)
        args["references"] = normalized
    elif name == "read_document":
        filename = str(args.get("filename") or args.get("file_name") or "").strip()
        if filename:
            args["filename"] = filename
            # A bare name can refer to a current-session uploaded document.
            from memory import file_memory
            uploaded = file_memory.find_file_memory_record(context["session"], filename) is not None
            if not uploaded:
                args["filename"] = resolve_path(filename, root)
                paths.append(args["filename"])
    if any(not inside(path, root) for path in paths):
        reason = reason or "outside_workspace"
    if any(protected(path) for path in paths):
        reason = reason or "protected_control_resource"
    if sources:
        reason = reason or "external_resource"
    value = {"tool": name, "args": args, "paths": paths, "sources": sources, "root": root,
             "reason": reason, "server": server, "original_name": original_name or name}
    value["server_fingerprint"] = server_fingerprint(server)
    value["digest"] = fingerprint(value)
    value["needs_approval"] = context["mode"] == "ask" and bool(reason)
    return value
