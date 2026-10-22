"""Explicit MCP connection/tool tests. Reports contain bounded, redacted data."""
import asyncio
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone

from util import mcp_client as client
from util.mcp_server_info import describe_mcp_server
from util.mcp_profiles import connection_config, select_profile, tool_allowed, schema_validator

_SLOTS = threading.BoundedSemaphore(3)
_SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|cookie|credential|api.?key|private.?key", re.I)
_MAX_TOOLS = 200


def _plain(value):
    return value.model_dump(mode="json", by_alias=False, exclude_none=True) if hasattr(value, "model_dump") else value


class Redactor:
    def __init__(self, *sources):
        self.secrets = set()
        for source in sources:
            self.collect(source)

    def collect(self, value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                self.collect(item, str(name))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if index and _SECRET_KEY.search(str(value[index - 1])) and isinstance(item, str):
                    self.secrets.add(item)
                self.collect(item, key)
        elif isinstance(value, str):
            if _SECRET_KEY.search(key):
                for item in (value, os.path.expandvars(value)):
                    if len(item) >= 4:
                        self.secrets.add(item)
            for match in re.finditer(r'''(?i)(?:api[_-]?key|token|password|secret|authorization|cookie)["']?\s*[=:]\s*["']?([^\s&,;"'}]+)''', value):
                if len(match[1]) >= 4:
                    self.secrets.add(match[1])

    def text(self, value, limit=12000):
        text = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[\w.+/=-]+", r"\1[REDACTED]", text)
        text = re.sub(r'''(?i)((?:api[_-]?key|token|password|secret|authorization|cookie)["']?\s*[=:]\s*["']?)[^\s&,;"'}]+''', r"\1[REDACTED]", text)
        text = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
        text = re.sub(r"(?i)[a-z]:[\\/][^\r\n\"'<>|]*|/(?:home|Users|tmp|private)/[^\s\"'<>]*", "[LOCAL_PATH]", text)
        return text if len(text) <= limit else text[:limit] + "\n[内容已截断]"

    def clean(self, value, depth=0, *, schema=False, _budget=None):
        budget = _budget if _budget is not None else [64000, 2000]
        value = _plain(value)
        budget[1] -= 1
        if depth > 12 or budget[0] <= 0 or budget[1] <= 0:
            return "[嵌套内容已省略]"
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                if len(output) >= 200 or budget[0] <= 0 or budget[1] <= 0:
                    output["[其余条目已省略]"] = True
                    break
                safe_key = self.text(key, 200)
                budget[0] -= len(safe_key)
                output[safe_key] = "[REDACTED]" if ((_SECRET_KEY.search(str(key)) and not schema) or (schema and key in {"default", "examples"})) else self.clean(item, depth + 1, schema=schema, _budget=budget)
            return output
        if isinstance(value, (list, tuple)):
            items = []
            for item in value[:200]:
                if budget[0] <= 0 or budget[1] <= 0:
                    break
                items.append(self.clean(item, depth + 1, schema=schema, _budget=budget))
            if len(value) > len(items):
                items.append("[其余条目已省略]")
            return items
        if isinstance(value, str):
            safe = self.text(value, min(12000, max(0, budget[0])))
            budget[0] -= len(safe)
            return safe
        return value


def load_servers():
    try:
        return client._load_mcp_servers_config()
    except (OSError, ValueError) as exc:
        raise DiagnosticFailure("invalid_config", "无法读取 MCP 配置，请检查 setting/mcp_servers.json 的格式和读取权限") from exc


def configured_servers():
    output = []
    for name, config in load_servers().items():
        try:
            info = describe_mcp_server(config)
        except ValueError:
            info = {"configuration_error": True, "connection_hint": "连接配置无效，请发起连接测试查看原因。"}
        output.append({"id": name, "transport": config.get("type", "stdio") if isinstance(config, dict) else "unknown",
                       "configured": isinstance(config, dict), **info})
    return {"servers": output}


def _validator(schema):
    return schema_validator(schema)


class DiagnosticFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error_code(exc, phase):
    if isinstance(exc, DiagnosticFailure):
        return exc.code
    leaf = client._unwrap_exception_group(exc)
    if isinstance(leaf, DiagnosticFailure):
        return leaf.code
    if isinstance(leaf, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(leaf, FileNotFoundError):
        return "command_not_found"
    if isinstance(leaf, PermissionError) or re.search(r"401|403|unauthori|forbidden", str(leaf), re.I):
        return "permission_denied"
    if isinstance(leaf, (BrokenPipeError, EOFError, ConnectionError)) or re.search(r"closed|disconnect|broken pipe|endofstream", str(leaf), re.I):
        return "transport_error"
    return "invalid_config" if phase == "configuration" else "tool_error" if phase == "call_tool" else "protocol_error"


def _content(result, redactor):
    output, size = [], 0
    for item in (getattr(result, "content", None) or [])[:100]:
        part = _plain(item)
        if not isinstance(part, dict):
            part = {"type": getattr(item, "type", "text"), "text": getattr(item, "text", "")}
        if part.get("type") in {"image", "audio"}:
            part = {"type": part["type"], "mime_type": part.get("mime_type") or part.get("mimeType"),
                    "note": "二进制内容不写入诊断报告", "encoded_chars": len(part.get("data") or "")}
        if isinstance(part.get("resource"), dict) and "blob" in part["resource"]:
            part["resource"]["blob"] = "[二进制内容已省略]"
        safe = redactor.clean(part)
        size += len(json.dumps(safe, ensure_ascii=False))
        if size > 131072:
            output.append({"type": "text", "text": "[其余输出超过报告大小上限，已省略]"})
            break
        output.append(safe)
    return {"content": output, "structured_content": redactor.clean(client._sdk_attr(result, "structured_content", "structuredContent")),
            "is_error": bool(client._sdk_attr(result, "is_error", "isError", False))}


async def diagnose(server_id, *, action="connect", tool_name=None, arguments=None, timeout_seconds=30, mcp_profile=None):
    started = time.monotonic()
    phase = "configuration"
    config, config_error = None, None
    try:
        config = load_servers().get(server_id)
    except DiagnosticFailure as exc:
        config_error = exc
    redactor = Redactor(config, arguments)
    report = {"version": 1, "server_id": server_id, "transport": "stdio", "action": action,
              "tool_name": tool_name, "arguments": redactor.clean(arguments) if action == "call" else None,
              "timeout_seconds": timeout_seconds, "captured_at": datetime.now(timezone.utc).isoformat(),
              "ok": False, "steps": [], "tools": [], "warnings": [], "redacted": True}
    try:
        mcp_profile = select_profile(config, mcp_profile)
        effective_config = connection_config(config, mcp_profile) if isinstance(config, dict) else config
    except ValueError as exc:
        effective_config, config_error = None, DiagnosticFailure("unsupported_transport" if "仅支持 stdio" in str(exc) else "invalid_config", str(exc))
    connection = describe_mcp_server(effective_config)
    if mcp_profile:
        report["mcp_profile"] = mcp_profile
    if connection.get("browser_mode"):
        report["connection"] = connection
        if connection["browser_mode"] == "extension":
            report["warnings"].append("扩展模式：MCP 握手成功不代表已连接浏览器页面。实际调用需在浏览器授权、选择标签页；诊断结束会断开测试连接，聊天中需另行授权其常驻连接。")
    if not _SLOTS.acquire(blocking=False):
        return {**report, "server_id": redactor.text(server_id, 512), "tool_name": redactor.text(tool_name, 256) if tool_name else None,
                "elapsed_ms": round((time.monotonic() - started) * 1000), "error": {"code": "busy", "phase": "queued", "message": "已有多个 MCP 测试进行中，请稍后重试"}}
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
            async def run():
                nonlocal phase
                if config_error:
                    raise config_error
                if isinstance(effective_config, dict) and effective_config.get("type", "stdio") != "stdio":
                    raise DiagnosticFailure("unsupported_transport", "当前项目的 MCP 客户端仅支持 stdio 服务")
                if not isinstance(effective_config, dict) or not isinstance(effective_config.get("command"), str) or not effective_config["command"].strip():
                    raise DiagnosticFailure("invalid_config", "请选择 setting/mcp_servers.json 中有 command 的已配置服务")
                params = client.build_stdio_server_parameters(server_id, mcp_profile) if mcp_profile else client.build_stdio_server_parameters(server_id)
                if load_servers().get(server_id) != config:
                    raise DiagnosticFailure("invalid_config", "测试期间服务配置已变化，请重新选择服务")
                redactor.collect(params.env or {})
                report["steps"].append({"phase": phase, "ok": True})
                phase = "connect"
                async with client.stdio_client(params, errlog=stderr) as (reader, writer):
                    report["steps"].append({"phase": phase, "ok": True})
                    async with client.ClientSession(reader, writer) as session:
                        phase = "initialize"
                        initialized = await session.initialize()
                        report["protocol_version"] = redactor.text(client._sdk_attr(initialized, "protocol_version", "protocolVersion", "未知"))
                        report["server_info"] = redactor.clean(client._sdk_attr(initialized, "server_info", "serverInfo", {}))
                        capabilities = _plain(getattr(initialized, "capabilities", None)) or {}
                        report["capabilities"] = redactor.clean(capabilities)
                        report["steps"].append({"phase": phase, "ok": True})
                        await client._upgrade_modern_protocol(session)
                        phase = "list_tools"
                        found, cursor, seen = [], None, set()
                        for _ in range(30):
                            page = await session.list_tools(**({"cursor": cursor} if cursor else {}))
                            found.extend(page.tools)
                            cursor = client._sdk_attr(page, "next_cursor", "nextCursor")
                            if len(found) > 2000:
                                raise DiagnosticFailure("protocol_error", "工具清单超过诊断上限 2000 项")
                            if not cursor:
                                break
                            if cursor in seen:
                                raise DiagnosticFailure("protocol_error", "工具分页游标重复，服务未能完成清单返回")
                            seen.add(cursor)
                        else:
                            raise DiagnosticFailure("protocol_error", "工具清单分页超过诊断上限")
                        found = [tool for tool in found if tool_allowed(effective_config, tool.name)]
                        report["tool_count"] = len(found)
                        report["steps"].append({"phase": phase, "ok": True, "count": len(found)})
                        for tool in found[:_MAX_TOOLS]:
                            schema = client._sdk_attr(tool, "input_schema", "inputSchema", {})
                            try:
                                _validator(schema)
                                schema_valid = True
                            except Exception:
                                schema_valid = False
                            safe_schema = redactor.clean(schema, schema=True)
                            truncated = len(json.dumps(safe_schema, ensure_ascii=False)) > 6000
                            report["tools"].append({"name": redactor.text(tool.name, 256), "description": redactor.text(getattr(tool, "description", "") or "", 1200),
                                "input_schema": "[Schema 过长，请参考服务文档]" if truncated else safe_schema,
                                "schema_valid": schema_valid, "schema_truncated": truncated})
                        if len(found) > _MAX_TOOLS:
                            report["warnings"].append("仅展示前 200 项工具；仍可输入完整工具名进行测试")
                        if not found:
                            report["warnings"].append("连接成功，但服务没有提供可调用工具")
                        if any(not t["schema_valid"] for t in report["tools"]):
                            report["warnings"].append("部分工具的参数 Schema 不合法，调用前会明确报错")
                        if action == "call":
                            phase = "validate_arguments"
                            tool = next((item for item in found if item.name == tool_name), None)
                            if tool is None:
                                raise DiagnosticFailure("tool_not_found", "服务未提供该工具，请使用工具原名")
                            try:
                                checker = _validator(client._sdk_attr(tool, "input_schema", "inputSchema", {}))
                                error = next(checker.iter_errors(arguments or {}), None)
                            except Exception as exc:
                                raise DiagnosticFailure("schema_error", "参数 Schema 无法验证（仅支持本地引用）：" + str(exc)) from exc
                            if error:
                                path = ".".join(str(p) for p in error.absolute_path) or "$"
                                raise DiagnosticFailure("invalid_arguments", path + ": " + error.message)
                            report["steps"].append({"phase": phase, "ok": True})
                            phase = "call_tool"
                            result = await session.call_tool(tool_name, arguments or {})
                            report["result"] = _content(result, redactor)
                            if report["result"]["is_error"]:
                                raise DiagnosticFailure("tool_error", "服务已返回工具执行错误，详见结果内容")
                            report["steps"].append({"phase": phase, "ok": True})
                report["ok"] = True
            try:
                await asyncio.wait_for(run(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report["error"] = {"code": _error_code(exc, phase), "phase": phase,
                                   "message": redactor.text(client._format_mcp_error(exc))}
            finally:
                stderr.flush()
                stderr.buffer.seek(0, os.SEEK_END)
                size = stderr.buffer.tell()
                stderr.buffer.seek(max(0, size - 12000))
                # 从字节边界截取时可能切开 UTF-8 字符，仅替换该字符，不丢失整段日志。
                report["stderr"] = redactor.text(stderr.buffer.read(12000).decode("utf-8", errors="replace"))
    finally:
        _SLOTS.release()
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    report["server_id"] = redactor.text(server_id, 512)
    report["tool_name"] = redactor.text(tool_name, 256) if tool_name else None
    # 环境变量中的密钥在构建启动参数后才获知，最终再次处理调用参数。
    report["arguments"] = redactor.clean(arguments) if action == "call" else None
    return report
