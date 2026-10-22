"""Extension-mode wiring without touching the user's browser or opening tabs."""
import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from config import FunctionDefinition
from factory.agent_runtime import tool_executor, tool_registry
from util import mcp_client
from util.mcp_server_info import describe_mcp_server, validate_browser_connection
from util.mcp_profiles import model_envelope


def browser_config(*flags):
    return {"command": "node", "args": ["browser/node_modules/@playwright/mcp/cli.js", *flags], "persistent": True}


def test_single_browser_server_offers_two_runtime_targets():
    data = json.loads((mcp_client.PROJECT_ROOT / "setting/mcp_servers.json").read_text(encoding="utf-8"))
    assert "BrowserExisting" not in data["servers"] and "BrowserExisting" not in data["inputs"]
    config = data["servers"]["Browser"]
    assert config["persistent"] is True and list(config["profiles"]) == ['isolated', 'extension']
    params = mcp_client.build_stdio_server_parameters("Browser", 'extension')
    isolated = mcp_client.build_stdio_server_parameters("Browser", 'isolated')
    assert '--extension' in params.args and '--isolated' not in params.args and '--headless' not in params.args
    assert '--isolated' in isolated.args and '--headless' in isolated.args and '--extension' not in isolated.args
    assert Path(params.args[0]).is_absolute()
    assert Path(params.env["PLAYWRIGHT_MCP_OUTPUT_DIR"]) == Path(tempfile.gettempdir()) / "ytools_playwright_artifacts" / 'extension'
    assert '--headless' in config['profiles']['isolated']['args'], 'building a target must not mutate stored configuration'


@pytest.mark.parametrize("flags,mode", [(('--extension',), 'extension'), (('--isolated',), 'isolated'),
    (('--cdp-endpoint', 'http://127.0.0.1:9222'), 'cdp'), (('--endpoint=ws://private.invalid/key',), 'remote'), ((), 'managed')])
def test_connection_metadata_identifies_modes_without_exposing_private_configuration(flags, mode):
    config = browser_config(*flags)
    config["env"] = {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "PRIVATE_TOKEN", "WORK_DIR": "C:/Users/Private"}
    info = describe_mcp_server(config)
    assert info["browser_mode"] == mode
    assert not any(value in json.dumps(info) for value in ['PRIVATE_TOKEN', 'Private', '9222', 'private.invalid'])


def test_edge_and_environment_flags_are_detected_without_changing_configuration():
    config = browser_config('--extension', '--browser', 'msedge')
    assert describe_mcp_server(config)["browser_channel"] == 'Edge'
    config = browser_config()
    config['env'] = {'PLAYWRIGHT_MCP_EXTENSION': 'true', 'PLAYWRIGHT_MCP_BROWSER': 'chrome'}
    assert describe_mcp_server(config)["browser_mode"] == 'extension'
    assert '--extension' not in config['args']
    config['persistent'] = False
    assert 'persistent' in describe_mcp_server(config)['connection_hint']
    assert describe_mcp_server({'command': 'custom-server', 'args': ['--extension']}) == {'persistent': False}


@pytest.mark.parametrize('other', [('--isolated',), ('--cdp-endpoint=http://localhost:9222',), ('--endpoint', 'ws://localhost:9000')])
def test_conflicting_extension_flags_fail_before_a_process_is_built(monkeypatch, other):
    config = browser_config('--extension', *other)
    monkeypatch.setattr(mcp_client, '_load_mcp_servers_config', lambda: {'DailyBrowser': config})
    def forbidden(*_args):
        pytest.fail('invalid configuration must not build or start a process')
    monkeypatch.setattr(mcp_client, '_build_stdio_parameters', forbidden)
    with pytest.raises(ValueError, match='连接方式'):
        mcp_client.build_stdio_server_parameters('DailyBrowser')
    # CDP may deliberately use a fresh isolated context; do not reject this.
    validate_browser_connection(browser_config('--cdp-endpoint=http://localhost:9222', '--isolated'))


def test_same_named_browser_tools_are_identifiable_and_route_to_the_selected_service(monkeypatch):
    for name in ('ALL_TOOLS', 'TOOL_MCP_SERVERS', 'TOOL_ORIGINAL_NAMES', 'TOOL_NAME_INDEX', '_TOOLS_CACHE', '_TOOLS_CACHE_MONO'):
        monkeypatch.setattr(tool_registry, name, getattr(tool_registry, name))
    configs = {'Browser': browser_config('--isolated'), 'CustomExtension': browser_config('--extension')}
    monkeypatch.setattr(tool_registry, '_load_mcp_servers', lambda _cwd='': configs)
    async def tools(_server):
        return [FunctionDefinition(name='browser_tabs', description='List or select tabs', parameters={'type': 'object'})]
    monkeypatch.setattr(tool_registry, 'get_mcp_tools', tools)
    report = asyncio.run(tool_registry.refresh_tools_from_mcp('.'))
    items = {tool['server_id']: tool for tool in report['tools']}
    assert items['Browser']['function']['name'] != items['CustomExtension']['function']['name']
    for server, tool in items.items():
        assert server in tool['function']['description']
        assert 'browser_tabs' in tool['function']['description']
    assert '现有浏览器' in items['CustomExtension']['function']['description']
    assert '先列出标签页' in items['CustomExtension']['function']['description']
    assert '独立隔离' in items['Browser']['function']['description']
    alias = items['CustomExtension']['function']['name']
    assert tool_registry.resolve_tool_selection({'CustomExtension': ['browser_tabs']}) == [alias]
    assert tool_registry.resolve_tool_selection({'Browser': ['browser_tabs']}) == [items['Browser']['function']['name']]
    calls = []
    async def call(**kwargs):
        calls.append(kwargs); return 'authorized tabs'
    monkeypatch.setattr(tool_executor, 'call_mcp_tool', call)
    assert tool_executor._invoke_tool_function(alias, {'action': 'list'}, tool_registry.TOOL_MCP_SERVERS, tool_registry.TOOL_ORIGINAL_NAMES) == 'authorized tabs'
    assert calls == [{'function_name': 'browser_tabs', 'arguments': {'action': 'list'}, 'mcp_service': 'CustomExtension', 'session_scope': 'main'}]


def test_one_service_reuses_both_target_processes_and_separates_child_scopes(monkeypatch):
    launched = []
    calls = []
    config = browser_config('--isolated', '--headless')
    config['profiles'] = {'isolated': {}, 'extension': {}}
    config['default_profile'] = 'isolated'
    @contextlib.asynccontextmanager
    async def stdio(params):
        launched.append(params.target); yield object(), object()
    class Session:
        def __init__(self, *_args): self.marker = len(launched)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def initialize(self): pass
        async def list_tools(self): return NS(tools=[NS(name='browser_tabs'), NS(name='browser_snapshot')])
        async def call_tool(self, name, args):
            calls.append((name, args.copy()))
            if args.get('stall'):
                await asyncio.sleep(60)
            return NS(is_error=False, content=[NS(text=str(self.marker))])
    monkeypatch.setattr(mcp_client, '_load_mcp_servers_config', lambda: {'Browser': config})
    monkeypatch.setattr(mcp_client, 'build_stdio_server_parameters', lambda _, target=None: NS(target=target))
    monkeypatch.setattr(mcp_client, 'stdio_client', stdio)
    monkeypatch.setattr(mcp_client, 'ClientSession', Session)
    monkeypatch.setattr(mcp_client, '_PERSISTENT_CLIENTS', {})
    try:
        first = asyncio.run(mcp_client.call_mcp_tool('browser_tabs', {'arguments': {'action': 'list'}}, 'Browser'))
        arguments = {'mcp_profile': 'extension', 'arguments': {'action': 'list'}}
        extension = asyncio.run(mcp_client.call_mcp_tool('browser_tabs', arguments, 'Browser'))
        assert 'profile: extension' in extension and extension != first
        assert arguments['mcp_profile'] == 'extension' and calls[-1] == ('browser_tabs', {'action': 'list'})
        assert asyncio.run(mcp_client.call_mcp_tool('browser_snapshot', {'mcp_profile': 'isolated'}, 'Browser')) == first
        assert asyncio.run(mcp_client.call_mcp_tool('browser_snapshot', {'mcp_profile': 'extension'}, 'Browser')) == extension
        assert launched == ['isolated', 'extension'], 'one process per target, not one per tool call'
        asyncio.run(mcp_client.call_mcp_tool('browser_tabs', {'mcp_profile': 'extension'}, 'Browser', 'sub_agent:child'))
        mcp_client.close_persistent_mcp_clients('sub_agent:child')
        assert ('Browser', 'main', 'isolated') in mcp_client._PERSISTENT_CLIENTS
        assert ('Browser', 'main', 'extension') in mcp_client._PERSISTENT_CLIENTS
        assert not any(key[1] == 'sub_agent:child' for key in mcp_client._PERSISTENT_CLIENTS)
        assert len(launched) == 3
        monkeypatch.setattr(mcp_client, '_mcp_call_timeout_seconds', lambda: .05)
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(mcp_client.call_mcp_tool('browser_snapshot', {'mcp_profile': 'extension', 'arguments': {'stall': True}}, 'Browser'))
        assert ('Browser', 'main', 'extension') not in mcp_client._PERSISTENT_CLIENTS
        assert asyncio.run(mcp_client.call_mcp_tool('browser_snapshot', {}, 'Browser')) == first
        for invalid in ('invalid', '', None, False, 0):
            with pytest.raises(ValueError, match='mcp_profile'):
                asyncio.run(mcp_client.call_mcp_tool('browser_tabs', {'mcp_profile': invalid}, 'Browser'))
    finally:
        mcp_client.close_persistent_mcp_clients()


def test_single_catalog_does_not_accept_removed_service_or_old_tool_aliases(monkeypatch):
    for name in ('ALL_TOOLS', 'TOOL_MCP_SERVERS', 'TOOL_ORIGINAL_NAMES', 'TOOL_NAME_INDEX', '_TOOLS_CACHE', '_TOOLS_CACHE_MONO'):
        monkeypatch.setattr(tool_registry, name, getattr(tool_registry, name))
    config = browser_config('--isolated')
    config['profiles'] = {'isolated': {}, 'extension': {}}
    config['default_profile'] = 'isolated'
    monkeypatch.setattr(tool_registry, '_load_mcp_servers', lambda _cwd='': {'Browser': config})
    async def tools(_server):
        return mcp_client.MCPToolCatalog([FunctionDefinition(name='browser_tabs', description='Tabs', parameters=model_envelope({'isolated': {'type': 'object'}, 'extension': {'type': 'object'}}))], {'browser_tabs': ['isolated', 'extension']}, {'browser_tabs': 1}, [])
    monkeypatch.setattr(tool_registry, 'get_mcp_tools', tools)
    report = asyncio.run(tool_registry.refresh_tools_from_mcp('.'))
    assert report['servers'] == ['Browser'] and report['total'] == 1
    assert 'server_aliases' not in report
    tool = report['tools'][0]
    assert tool['function']['name'] == 'browser_tabs'
    assert tool['function']['parameters']['properties']['mcp_profile']['enum'] == ['isolated', 'extension']
    assert tool['function']['parameters']['additionalProperties'] is False
    old = tool_registry._qualified_tool_name('BrowserExisting', 'browser_tabs', set(), set())
    other = tool_registry._qualified_tool_name('Browser', 'browser_tabs', set(), set())
    assert 'legacy_names' not in tool
    assert old not in tool_registry.TOOL_NAME_INDEX and other not in tool_registry.TOOL_NAME_INDEX
    assert tool_registry.resolve_tool_selection({'BrowserExisting': ['browser_tabs', old]}) == []
    assert tool_registry.resolve_tool_selection({'Browser': ['browser_tabs']}) == ['browser_tabs']
    assert 'browser_tabs' not in tool_registry.canonicalize_requested_names([old, other])[0]
    with pytest.raises(ValueError, match='未在MCP服务器中注册'):
        tool_executor._invoke_tool_function(old, {}, tool_registry.TOOL_MCP_SERVERS, tool_registry.TOOL_ORIGINAL_NAMES)
    monkeypatch.setattr(mcp_client, '_load_mcp_servers_config', lambda: {'Browser': config})
    assert mcp_client._get_configured_server_command('BrowserExisting') is None
    assert mcp_client._persistent_server_signature('BrowserExisting') is None
    from factory.task_templates import canonical_tools
    assert canonical_tools({'BrowserExisting': [old]}, [{'server': 'Browser', 'name': 'browser_tabs', 'call_name': 'browser_tabs'}]) == {'BrowserExisting': [old]}
