"""Profiles route connections independently of vendor and native tool names."""
import asyncio
import copy
import json
import sys

import pytest

from config import FunctionDefinition
from factory.agent_runtime import tool_registry
from util import mcp_client as client
from util.mcp_profiles import connection_config, model_envelope, select_profile, validate_arguments
from util.mcp_server_info import describe_mcp_server


SCHEMA = {"type": "object", "$defs": {"value": {"type": "string"}},
          "properties": {"text": {"$ref": "#/$defs/value"}}, "required": ["text"], "additionalProperties": False}


def config():
    return {"command": "echo", "args": ["base"], "env": {"SHARED": "yes", "OVERRIDE": "base"},
            "persistent": True, "default_profile": "blue", "profiles": {
                "blue": {"label": "测试环境", "env": {"OVERRIDE": "blue"}},
                "green": {"label": "生产环境", "command": "custom", "args": [], "persistent": False}}}


def test_effective_config_is_immutable_args_replace_and_env_merge():
    original = config()
    saved = copy.deepcopy(original)
    blue = connection_config(original)
    green = connection_config(original, "green")
    assert blue["args"] == ["base"] and blue["env"] == {"SHARED": "yes", "OVERRIDE": "blue"}
    assert green["command"] == "custom" and green["args"] == [] and not green["persistent"]
    assert "profiles" not in green and original == saved
    info = describe_mcp_server(original)
    assert [item["id"] for item in info["mcp_profiles"]] == ["blue", "green"]
    assert not any(value in json.dumps(info) for value in ("OVERRIDE", "custom"))


@pytest.mark.parametrize("change", [
    {"profiles": {}}, {"profiles": {"bad id": {}}}, {"default_profile": "missing"},
    {"profiles": {"blue": {"args": "wrong"}}}, {"profiles": {"blue": {"env": {"X": 1}}}},
    {"profiles": {"blue": {"tools": "echo"}}}, {"profiles": {"blue": {"browser_target": "extension"}}},
])
def test_invalid_configuration_fails_before_launch(change):
    with pytest.raises(ValueError):
        connection_config({**config(), **change})


def test_locked_profile_cannot_be_overridden():
    cfg = {**config(), "locked_profile": "green"}
    assert select_profile(cfg) == "green"
    with pytest.raises(ValueError, match="固定"):
        connection_config(cfg, "blue")


def test_envelope_preserves_local_refs_and_native_route_named_parameters():
    native = copy.deepcopy(SCHEMA)
    native["properties"]["mcp_profile"] = {"type": "integer"}
    envelope = model_envelope({"blue": native, "green": native})
    validate_arguments(envelope, {"mcp_profile": "green", "arguments": {"text": "hello", "mcp_profile": 7}})
    with pytest.raises(ValueError):
        validate_arguments(envelope, {"mcp_profile": "green", "arguments": {"text": 7}})
    assert envelope["properties"]["arguments"]["properties"]["text"]["$ref"] == "#/properties/arguments/$defs/value"
    different = model_envelope({"blue": native, "green": {"type": "object", "required": ["count"],
        "properties": {"count": {"type": "integer"}}}})
    validate_arguments(different, {"mcp_profile": "blue", "arguments": {"text": "local refs"}})
    assert "blue" in different["properties"]["arguments"]["anyOf"][0]["description"]


def test_schema_rebasing_keeps_literal_fields_and_isolates_same_named_anchors():
    native = {"type": "object", "$defs": {"item": {"$anchor": "value", "type": "string"}},
        "properties": {"$id": {"type": "string"}, "text": {"$ref": "#value"}},
        "examples": [{"$id": "business value", "$ref": "literal text"}], "required": ["text"]}
    other = copy.deepcopy(native)
    other["$defs"]["item"]["type"] = "integer"
    schema = model_envelope({"blue": native, "green": other})
    first = schema["properties"]["arguments"]["anyOf"][0]
    assert first["properties"]["$id"] == {"type": "string"}
    assert first["examples"] == native["examples"]
    validate_arguments(schema, {"mcp_profile": "green", "arguments": {"text": 3}})
    validate_arguments(schema, {"mcp_profile": "blue", "arguments": {"text": "hello", "$id": "literal"}})


def test_discovery_merges_capabilities_filters_allowlists_and_preserves_registry_schema(monkeypatch):
    cfg = config()
    cfg["profiles"]["blue"]["tools"] = ["echo"]
    monkeypatch.setattr(client, "_load_mcp_servers_config", lambda: {"Custom": cfg})
    async def discover(_server, profile):
        native = SCHEMA if profile == "blue" else {"type": "object", "properties": {"count": {"type": "integer"}}}
        return [FunctionDefinition(name=name, parameters=native) for name in ("echo", "green_only")]
    monkeypatch.setattr(client, "_discover_tools", discover)
    catalog = asyncio.run(client.get_mcp_tools("Custom"))
    assert len(catalog) == 2 and catalog.tool_profiles == {"echo": ["blue", "green"], "green_only": ["green"]}
    assert catalog.schema_variants["echo"] == 2
    for name in ("ALL_TOOLS", "TOOL_MCP_SERVERS", "TOOL_ORIGINAL_NAMES", "TOOL_NAME_INDEX", "_TOOLS_CACHE", "_TOOLS_CACHE_MONO"):
        monkeypatch.setattr(tool_registry, name, getattr(tool_registry, name))
    monkeypatch.setattr(tool_registry, "_load_mcp_servers", lambda _cwd="": {"Custom": cfg})
    async def get_tools(_server):
        return catalog
    monkeypatch.setattr(tool_registry, "get_mcp_tools", get_tools)
    payload = asyncio.run(tool_registry.refresh_tools_from_mcp("."))
    tool = next(t for t in payload["tools"] if t["original_name"] == "echo")
    assert tool["schema_variants"] == 2
    assert tool["function"]["parameters"] == catalog[0].parameters
    validate_arguments(tool["function"]["parameters"], {"mcp_profile": "blue", "arguments": {"text": "hello"}})
    assert "green=生产环境" in tool["function"]["description"]


def test_failed_profile_is_not_advertised_or_used_as_fallback(monkeypatch):
    cfg = config()
    monkeypatch.setattr(client, "_load_mcp_servers_config", lambda: {"Custom": cfg})
    async def discover(_server, profile):
        if profile == "blue":
            raise RuntimeError("private connection failure")
        return [FunctionDefinition(name="echo", parameters=SCHEMA)]
    monkeypatch.setattr(client, "_discover_tools", discover)
    catalog = asyncio.run(client.get_mcp_tools("Custom"))
    assert catalog[0].parameters["properties"]["mcp_profile"]["enum"] == ["green"]
    assert catalog.profile_status[0] == {"id": "blue", "ok": False, "tool_count": 0}
    cfg["locked_profile"] = "blue"
    with pytest.raises(RuntimeError, match="所有 profile"):
        asyncio.run(client.get_mcp_tools("Custom"))


def test_route_errors_and_allowlists_stop_before_client_creation(monkeypatch):
    cfg = config()
    cfg["profiles"]["blue"]["tools"] = []
    monkeypatch.setattr(client, "_load_mcp_servers_config", lambda: {"Custom": cfg})
    def forbidden(*_args):
        pytest.fail("invalid routing must not create a client")
    monkeypatch.setattr(client, "_get_persistent_client", forbidden)
    for envelope in ([], False, {"mcp_profile": "missing"}, {"mcp_profile": "blue"}, {"text": "flat is invalid"},
                     {"mcp_profile": "green", "arguments": []}):
        with pytest.raises(ValueError):
            asyncio.run(client.call_mcp_tool("echo", envelope, "Custom"))


def test_one_malformed_profile_does_not_erase_the_public_profile_metadata():
    cfg = config()
    cfg["profiles"]["green"]["args"] = "invalid"
    cfg["default_profile"] = "green"
    info = describe_mcp_server(cfg)
    assert info["configuration_error"]
    assert info["mcp_profiles"][0]["available"] is True
    assert info["mcp_profiles"][1]["configuration_error"]
    assert info["mcp_profiles"][1]["available"] is False


def test_three_model_api_tool_definitions_keep_the_same_profile_envelope():
    from chat.chat_llm import ChatLLM
    from chat.protocols.messages import build_payload
    from chat.protocols.responses import parameters
    from config import ChatLLMRequest
    envelope = model_envelope({"blue": SCHEMA, "green": SCHEMA})
    request = ChatLLMRequest(messages=[{"role": "user", "content": "test"}],
        tools=[{"type": "function", "function": {"name": "echo", "description": "Echo", "parameters": envelope}}])
    assert ChatLLM._build_request_payload(request, False, "fixture")["tools"][0]["function"]["parameters"] == envelope
    messages_payload, _, _ = build_payload(request, {"selected_model_id": "fixture"}, False)
    assert messages_payload["tools"][0]["input_schema"] == envelope
    assert parameters(request, {"selected_model_id": "fixture"}, False)["tools"][0]["parameters"] == envelope


def test_real_native_schema_validation_and_two_profile_processes(tmp_path, monkeypatch):
    """Real SDK + local JSON-RPC fixture; no real user service or browser."""
    fixture = tmp_path / "profiles_server.py"
    fixture.write_text('''import sys, json, os
kind = os.environ["PROFILE_KIND"]
schema = {"type": "object", "properties": {"value": {"type": "string" if kind == "blue" else "integer"}, "mcp_profile": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req: continue
    method = req["method"]
    if method == "initialize": result = {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "Fixture", "version": "1"}}
    elif method == "tools/list": result = {"tools": [{"name": "echo", "inputSchema": schema}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps({"pid": os.getpid(), "profile": kind, "args": req["params"]["arguments"]})}]}
    else: result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
''', encoding="utf-8")
    cfg = {"command": sys.executable, "args": ["-u", str(fixture)], "persistent": True,
           "default_profile": "blue", "profiles": {p: {"env": {"PROFILE_KIND": p}} for p in ("blue", "green")}}
    monkeypatch.setattr(client, "_load_mcp_servers_config", lambda: {"Generic": cfg})
    monkeypatch.setattr(client, "_PERSISTENT_CLIENTS", {})
    def call(profile, value):
        result = asyncio.run(client.call_mcp_tool("echo", {"mcp_profile": profile, "arguments": value}, "Generic"))
        return json.loads(result.split("\n", 1)[1])
    try:
        blue = call("blue", {"value": "hello", "mcp_profile": "native business field"})
        green = call("green", {"value": 42})
        assert blue["pid"] != green["pid"] and blue["profile"] == "blue" and green["profile"] == "green"
        assert blue["args"] == {"value": "hello", "mcp_profile": "native business field"}
        assert call("blue", {"value": "again"})["pid"] == blue["pid"]
        with pytest.raises(ValueError, match="Schema"):
            call("green", {"value": "string is not valid in green"})
        assert call("green", {"value": 43})["pid"] == green["pid"]
        cfg["profiles"]["blue"]["env"]["CHANGED"] = "yes"
        assert call("blue", {"value": "new config"})["pid"] != blue["pid"]
        assert call("green", {"value": 44})["pid"] == green["pid"]
    finally:
        client.close_persistent_mcp_clients()
