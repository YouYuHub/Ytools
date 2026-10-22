"""Diagnostics exercise isolated fake servers; never invoke the user's tools."""
import asyncio
import contextlib
import json
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from util import mcp_diagnostics as diag
from routers import tools_manage_router as router


SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}


class Content:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_kwargs):
        return self.data


@pytest.fixture
def sdk(monkeypatch):
    state = NS(calls=[], launches=[], closed=[], error=None, call_error=None, delay=0,
               result=NS(content=[Content(type="text", text="done")], is_error=False),
               tools=[NS(name="echo", description="Echo", input_schema=SCHEMA)], pages=None, stderr="")
    config = {"Fake": {"command": "fake-server", "env": {"API_KEY": "configured-secret-123"}}}
    monkeypatch.setattr(diag.client, "_load_mcp_servers_config", lambda: config)
    monkeypatch.setattr(diag.client, "build_stdio_server_parameters", lambda _: NS(env={"OTHER_TOKEN": "env-secret-789"}))

    @contextlib.asynccontextmanager
    async def stdio(params, errlog):
        state.launches.append(params)
        errlog.write(state.stderr)
        try:
            yield object(), object()
        finally:
            state.closed.append(True)

    class Session:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def initialize(self):
            if state.error:
                raise state.error
            await asyncio.sleep(state.delay)
            return NS(protocolVersion="2025-11-25", serverInfo={"name": "Fake", "version": "1"}, capabilities={"tools": {}})

        async def list_tools(self, cursor=None):
            if state.pages:
                return state.pages(cursor)
            return NS(tools=state.tools)

        async def call_tool(self, name, arguments):
            state.calls.append((name, arguments))
            if state.call_error:
                raise state.call_error
            return state.result

    monkeypatch.setattr(diag.client, "stdio_client", stdio)
    monkeypatch.setattr(diag.client, "ClientSession", Session)
    return state


def run(**kwargs):
    return asyncio.run(diag.diagnose("Fake", **kwargs))


def test_dual_target_diagnostics_use_one_server_and_native_tool_arguments(sdk, monkeypatch):
    config = {'command': 'node', 'args': ['browser/node_modules/@playwright/mcp/cli.js', '--isolated'],
              'profiles': {'isolated': {}, 'extension': {'args': ['browser/node_modules/@playwright/mcp/cli.js', '--extension']}}, 'default_profile': 'isolated', 'persistent': True}
    monkeypatch.setattr(diag.client, '_load_mcp_servers_config', lambda: {'Browser': config})
    targets = []
    def build(server, target=None):
        targets.append((server, target)); return NS(env={})
    monkeypatch.setattr(diag.client, 'build_stdio_server_parameters', build)
    report = asyncio.run(diag.diagnose('Browser', action='call', tool_name='echo', arguments={'text': 'hello'}, mcp_profile='extension'))
    assert report['ok'] and report['mcp_profile'] == 'extension'
    assert targets == [('Browser', 'extension')] and sdk.calls == [('echo', {'text': 'hello'})]
    assert report['warnings'] and len(diag.configured_servers()['servers']) == 1
    assert sdk.closed


def test_connection_checks_capabilities_schema_without_calling_tools(sdk):
    report = run()
    assert report["ok"] and report["tool_count"] == 1
    assert report["protocol_version"] == "2025-11-25"
    assert report["capabilities"] == {"tools": {}}
    assert report["tools"][0]["input_schema"] == SCHEMA
    assert report["tools"][0]["schema_valid"]
    assert not sdk.calls and len(sdk.closed) == 1


def test_manual_call_uses_original_name_and_fresh_connection(sdk):
    for _ in range(2):
        report = run(action="call", tool_name="echo", arguments={"text": "hello"})
        assert report["ok"] and report["result"]["content"][0]["text"] == "done"
    assert sdk.calls == [("echo", {"text": "hello"})] * 2
    assert len(sdk.launches) == len(sdk.closed) == 2


def test_extension_inventory_and_handshake_distinguish_browser_authorization(sdk, monkeypatch):
    config = {"command": "node", "args": ["browser/node_modules/@playwright/mcp/cli.js", "--extension"], "persistent": True,
              "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "PRIVATE_EXTENSION_TOKEN"}}
    monkeypatch.setattr(diag.client, "_load_mcp_servers_config", lambda: {"Fake": config})
    inventory = diag.configured_servers()
    assert inventory["servers"][0]["browser_mode"] == "extension"
    assert "PRIVATE_EXTENSION_TOKEN" not in json.dumps(inventory)
    assert not sdk.launches
    report = run()
    assert report["ok"] and not sdk.calls
    assert report["connection"]["persistent"] is True
    assert any("不代表已连接浏览器页面" in warning for warning in report["warnings"])


@pytest.mark.parametrize("name,arguments,code", [
    ("unknown", {}, "tool_not_found"), ("echo", {}, "invalid_arguments"),
    ("echo", {"text": 12}, "invalid_arguments"), ("echo", {"text": "a", "other": True}, "invalid_arguments"),
])
def test_invalid_arguments_never_call_the_tool(sdk, name, arguments, code):
    report = run(action="call", tool_name=name, arguments=arguments)
    assert report["error"]["code"] == code and not sdk.calls
    assert len(sdk.closed) == 1


@pytest.mark.parametrize("schema", [{"type": "invalid"}, {"$ref": "https://must-not-fetch.invalid/schema"}])
def test_bad_or_remote_schema_fails_before_execution(sdk, schema):
    sdk.tools[0].input_schema = schema
    report = run(action="call", tool_name="echo", arguments={})
    assert report["error"]["code"] == "schema_error" and not sdk.calls


def test_local_schema_reference_supported(sdk):
    sdk.tools[0].input_schema = {"type": "object", "properties": {"text": {"$ref": "#/$defs/text"}},
                                 "$defs": {"text": {"type": "string"}}, "required": ["text"]}
    assert run(action="call", tool_name="echo", arguments={"text": "works"})["ok"]


def test_tool_error_preserves_return_and_empty_success_is_valid(sdk):
    sdk.result = NS(content=[Content(type="text", text="business failure")], isError=True)
    report = run(action="call", tool_name="echo", arguments={"text": "a"})
    assert report["error"]["code"] == "tool_error"
    assert report["result"]["content"][0]["text"] == "business failure"
    sdk.result = NS(content=[], is_error=False)
    report = run(action="call", tool_name="echo", arguments={"text": "a"})
    assert report["ok"] and report["result"]["content"] == []


def test_secret_redaction_covers_echo_arguments_stderr_and_schema(sdk):
    sdk.tools[0].input_schema = {"type": "object", "properties": {"api_key": {"type": "string", "default": "danger"}, "text": {"type": "string"}}}
    sdk.stderr = 'token=stderr-private-123 C:\\Users\\Someone\\private.txt'
    sdk.result = NS(content=[Content(type="text", text='configured-secret-123 env-secret-789 argument-secret-456 '
                                     'Bearer opaque-auth-123 {"api_key":"unseen-secret-123"} '
                                     'https://username:secret@example.org/?token=query-secret-123')],
                    is_error=False, structuredContent={"password": "abc", "data": "configured-secret-123"})
    report = run(action="call", tool_name="echo", arguments={"api_key": "argument-secret-456", "text": "env-secret-789"})
    assert report["ok"]
    text = json.dumps(report, ensure_ascii=False)
    for secret in ("configured-secret-123", "env-secret-789", "argument-secret-456", "opaque-auth-123", "unseen-secret-123", "query-secret-123", "stderr-private-123", "Someone", "username:secret"):
        assert secret not in text
    assert report["arguments"]["api_key"] == "[REDACTED]"
    assert report["arguments"]["text"] == "[REDACTED]"
    assert report["tools"][0]["input_schema"]["properties"]["api_key"]["type"] == "string"
    assert report["tools"][0]["input_schema"]["properties"]["api_key"]["default"] == "[REDACTED]"


def test_binary_outputs_omitted_and_large_results_bounded(sdk):
    sdk.result = NS(content=[Content(type="image", mimeType="image/png", data="IMAGEDATA"),
                             Content(type="resource", resource={"uri": "test://x", "blob": "BLOBDATA"})] +
                             [Content(type="text", text="x" * 20000) for _ in range(100)], is_error=False)
    report = run(action="call", tool_name="echo", arguments={"text": "a"})
    text = json.dumps(report)
    assert "IMAGEDATA" not in text and "BLOBDATA" not in text
    assert len(text) < 150000 and any("省略" in part.get("text", "") for part in report["result"]["content"])


def test_paginated_discovery_and_repeated_cursor(sdk):
    sdk.pages = lambda cursor: NS(tools=sdk.tools if cursor is None else [], nextCursor="page2" if cursor is None else None)
    assert run()["tool_count"] == 1
    sdk.pages = lambda cursor: NS(tools=sdk.tools, nextCursor="loop")
    assert run()["error"]["code"] == "protocol_error"


@pytest.mark.parametrize("error,code", [(FileNotFoundError("missing"), "command_not_found"),
    (PermissionError("denied"), "permission_denied"), (EOFError("closed"), "transport_error"),
    (RuntimeError("401 Unauthorized"), "permission_denied"), (RuntimeError("bad JSON-RPC"), "protocol_error")])
def test_failures_are_classified_and_connections_closed(sdk, error, code):
    sdk.error = error
    report = run()
    assert report["error"]["code"] == code and report["error"]["phase"] == "initialize"
    assert len(sdk.closed) == 1


def test_grouped_tool_validation_error_keeps_its_classification(sdk):
    class ExceptionGroup(Exception):
        def __init__(self, children):
            self.exceptions = children
    sdk.call_error = ExceptionGroup([diag.DiagnosticFailure("invalid_arguments", "invalid")])
    assert run(action="call", tool_name="echo", arguments={"text": "a"})["error"]["code"] == "invalid_arguments"


def test_timeout_and_cancellation_close_connection_and_release_slots(sdk):
    sdk.delay = 10
    assert run(timeout_seconds=.01)["error"]["code"] == "timeout"

    async def cancel():
        task = asyncio.create_task(diag.diagnose("Fake"))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert len(sdk.closed) == 2
    sdk.delay = 0
    assert run()["ok"]


def test_unknown_service_and_configuration_change_never_launch(sdk, monkeypatch):
    assert asyncio.run(diag.diagnose("python"))["error"]["code"] == "invalid_config"
    monkeypatch.setattr(diag.client, "build_stdio_server_parameters", lambda _: NS(env={}))
    sequence = iter([{"Fake": {"command": "safe"}}, {}])
    monkeypatch.setattr(diag.client, "_load_mcp_servers_config", lambda: next(sequence))
    assert run()["error"]["code"] == "invalid_config"
    assert not sdk.launches


def test_busy_reports_without_launching(sdk, monkeypatch):
    import threading
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(diag, "_SLOTS", slot)
    slot.acquire()
    try:
        assert run()["error"]["code"] == "busy"
        assert not sdk.launches
    finally:
        slot.release()


def test_malformed_config_has_safe_error(monkeypatch):
    def broken():
        raise ValueError("SENSITIVE CONFIG BODY")
    monkeypatch.setattr(diag.client, "_load_mcp_servers_config", broken)
    report = run()
    assert report["error"]["code"] == "invalid_config" and "SENSITIVE" not in json.dumps(report)


def test_unsupported_transport_has_explicit_error(sdk, monkeypatch):
    monkeypatch.setattr(diag.client, "_load_mcp_servers_config", lambda: {"Fake": {"type": "http", "url": "https://example.com"}})
    assert run()["error"]["code"] == "unsupported_transport" and not sdk.launches


def test_stderr_tail_keeps_utf8_and_only_last_bytes(sdk):
    sdk.stderr = "old diagnostic line\n" + "中文日志" * 3000 + "\n最后的错误信息"
    report = run()
    assert "old diagnostic line" not in report["stderr"]
    assert report["stderr"].endswith("最后的错误信息")


@pytest.fixture
def api(sdk):
    app = FastAPI()
    app.include_router(router.api_tools_manage_router)
    with TestClient(app) as client:
        yield client


def test_api_inventory_does_not_connect_or_expose_configuration(api, sdk):
    response = api.get("/tools/diagnostics")
    assert response.status_code == 200
    assert response.json()["servers"][0]["id"] == "Fake"
    assert "fake-server" not in response.text and "secret" not in response.text and not sdk.launches


def test_api_manual_call_and_request_validation(api, sdk):
    payload = {"server_id": "Fake", "action": "call", "tool_name": "echo", "arguments": {"text": "hello"}}
    assert api.post("/tools/diagnostics", json=payload).json()["ok"]
    assert len(sdk.calls) == 1
    for extra in ({"timeout_seconds": 0}, {"timeout_seconds": 301}, {"arguments": []},
                  {"arguments": {"text": "x" * 65536}}, {"tool_name": ""}, {"command": "python"}, {"action": "unexpected"}):
        assert api.post("/tools/diagnostics", json={**payload, **extra}).status_code == 422
    assert api.post("/tools/diagnostics", json={"server_id": "python"}).status_code == 404
    assert len(sdk.calls) == 1


def test_client_disconnect_cancels_test(sdk, monkeypatch):
    stopped = []

    async def pending(**kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append(True)
    monkeypatch.setattr(diag, "diagnose", pending)
    request = NS(is_disconnected=AsyncMock(return_value=True))
    with pytest.raises(router.HTTPException) as error:
        asyncio.run(router.test_mcp_server(router.MCPDiagnosticRequest(server_id="Fake"), request))
    assert error.value.status_code == 499 and stopped


def test_real_stdio_handshake_and_manual_call(tmp_path, monkeypatch):
    """A local JSON-RPC fixture verifies actual subprocess + installed SDK integration."""
    server = tmp_path / "diagnostic_server.py"
    server.write_text('''import sys, json, time
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "Fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        if request["params"]["arguments"].get("text") == "__timeout__":
            time.sleep(20)
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")
    (tmp_path / "setting").mkdir()
    (tmp_path / "setting" / "mcp_servers.json").write_text(json.dumps({"servers": {"Fake": {"command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")
    monkeypatch.setattr(diag.client, "PROJECT_ROOT", tmp_path)
    report = run(action="call", tool_name="echo", arguments={"text": "real stdio works"}, timeout_seconds=15)
    assert report["ok"], report.get("error")
    assert report["result"]["content"][0]["text"] == "real stdio works"
    stalled = run(action="call", tool_name="echo", arguments={"text": "__timeout__"}, timeout_seconds=1)
    assert stalled["error"]["code"] == "timeout", stalled.get("error")
    assert stalled["elapsed_ms"] < 10000, "超时应关闭真实 stdio 子进程，而不是等待业务调用自行结束"
