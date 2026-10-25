"""Real browser + isolated HTTP app; never use the user's service or model API."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from app_factory import create_app
from security import crypto
from security.settings import SecuritySettings

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'temporary browser fixture password 2026'


@pytest.fixture
def browser_app(tmp_path, monkeypatch, request):
    import env_manager
    import config
    import pyotp
    from factory import chat_factory as cf
    from factory.agent_runtime import builtin_tools, tool_registry
    from memory import chat_memory, file_memory, file_history, task_template_store
    from prompt import prompt_manager

    settings_root = tmp_path / 'configuration'
    settings_root.mkdir()
    env_manager.init_path(settings_root)
    monkeypatch.setattr(config, 'get_mcp_servers_config_path', lambda: settings_root / 'setting' / 'mcp_servers.json')
    monkeypatch.setattr(config, 'read_mcp_servers_config', lambda *_, **kwargs: {})
    monkeypatch.setenv('DEFAULT_CHAT_WORK_DIR', str(tmp_path / 'workspace'))
    history = tmp_path / 'history'
    history.mkdir()
    monkeypatch.setattr(chat_memory, 'HISTORY_ROOT', history)
    monkeypatch.setattr(chat_memory, 'LOCK_ROOT', tmp_path / 'locks')
    monkeypatch.setattr(chat_memory, '_session_managers', {})
    monkeypatch.setattr(file_memory, 'HISTORY_ROOT', history / 'session_files')
    monkeypatch.setattr(file_history, 'HISTORY_DIFF_ROOT', history / 'session_files')
    monkeypatch.setattr(task_template_store, 'ROOT', tmp_path / 'templates')
    monkeypatch.setattr(prompt_manager, 'MD_FILES_DIR', tmp_path / 'prompts')
    monkeypatch.setattr(builtin_tools, '_BG_LOG_DIR', tmp_path / 'commands')
    monkeypatch.setattr(tool_registry, '_load_mcp_servers', lambda *_, **kwargs: {})
    monkeypatch.setattr(tool_registry, 'ALL_TOOLS', [])
    monkeypatch.setattr(tool_registry, 'TOOL_MCP_SERVERS', {})
    monkeypatch.setattr(tool_registry, 'TOOL_ORIGINAL_NAMES', {})
    monkeypatch.setattr(tool_registry, 'TOOL_NAME_INDEX', {})
    monkeypatch.setattr(tool_registry, '_TOOLS_CACHE', None)
    monkeypatch.setattr(tool_registry, '_TOOLS_CACHE_MONO', 0.0)
    monkeypatch.setattr(cf, '_CHAT_WORKER_MODE', 'inline')
    monkeypatch.setattr(cf, '_SESSION_STREAMS', {})
    monkeypatch.setattr(cf, 'load_all_tools', lambda: asyncio.sleep(0))
    monkeypatch.setattr(cf, 'resolve_model_max_input_tokens', lambda default=8192: 1000000)
    monkeypatch.setattr(cf, 'require_default_chat_config', lambda: {
        'selected_provider_name': 'fixture', 'selected_model_name': 'fixture',
        'selected_model_id': 'fixture-model', 'vision': True})
    monkeypatch.setattr(cf, 'compact_session_history_if_needed', lambda *a, **k: asyncio.sleep(0))

    chunk_count = 30 if request.node.callspec.params['flow'] == 'account_panel_stream' else 12
    async def fake_model(request=None, stream=False, stop_checker=None, **kwargs):
        for number in range(chunk_count):
            if stop_checker and stop_checker():
                break
            yield 'data: ' + json.dumps({'content': f'fixture-chunk-{number} ', 'id': str(number)}) + '\n\n'
            await asyncio.sleep(0.12)
        yield 'data: ' + json.dumps({'finish_reason': 'stop', 'id': 'end', 'usage': {'total_tokens': 12}}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM, 'chat_completions', fake_model)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    app = create_app(security_settings=SecuritySettings(tmp_path / 'identity', origin=origin, require_mfa=False))
    store = app.state.identity_store
    alice = store.create_user('alice', PASSWORD, 'admin', bootstrap=True)
    bob = store.create_user('bob', PASSWORD, 'admin' if request.node.callspec.params['flow']=='account_share_import' else 'member', actor=alice['id'])
    secret = pyotp.random_base32()
    recovery = [f'fixture-recovery-{number}' for number in range(6)]
    with store.database(True) as db:
        db.execute('UPDATE users SET must_change=0')
        db.execute('UPDATE users SET mfa_secret=? WHERE id=?', (store.encrypt(secret), alice['id']))
        db.executemany('INSERT INTO recovery_codes VALUES(?,?)', [(alice['id'], crypto.digest(code)) for code in recovery])
    if request.node.callspec.params['flow'] == 'lost_device_rebind':
        store.local_recover('alice', reset_mfa=True)
    for sid, user in [('alpha', alice), ('beta', bob)]:
        store.register('session', sid, user['id'])
        meta = chat_memory._default_meta(sid)
        meta['title'] = 'Fixture ' + sid
        chat_memory._write_meta_and_entries(history / (sid + '_chat.jsonl'), meta, [{
            'event': 'chat_round', 'question': 'Fixture ' + sid, 'status': 'done',
            'events': [{'role':'user','content':'Fixture '+sid},{'role': 'assistant', 'content': 'Fixture reply ' + sid}],
            'timestamp': '2026-10-25 00:00:00'}])
    if request.node.callspec.params['flow'] == 'account_panel_performance':
        entries = [{'event':'chat_round','question':f'Large question {number}','status':'completed',
                    'events':[{'role':'assistant','reasoning_content':'fixture thought ' * 50},
                              {'role':'assistant','content':f'Large reply {number}\n\n' + 'Long fixture paragraph. ' * 80}],
                    'timestamp':'2026-10-25 00:00:00'} for number in range(1,81)]
        chat_memory._write_meta_and_entries(history / 'alpha_chat.jsonl', chat_memory._default_meta('alpha'), entries)
    if request.node.callspec.params['flow'] == 'history_pages':
        meta=chat_memory._default_meta('alpha');meta['title']='Paged history'
        entries=[{'event':'chat_round','question':f'Page question {number}','status':'done','events':[
            {'role':'user','content':f'Page question {number}'},
            {'role':'assistant','reasoning_content':'fixture thinking '+('x'*300)},
            {'role':'assistant','tool_calls':[{'id':f'tool-{number}','function':{'name':'read_file','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':f'tool-{number}','tool_name':'read_file','result':'fixture result '+('x'*(1300000 if number==180 else 8000))},
            {'role':'assistant','content':f'Page answer {number}'}]} for number in range(1,181)]
        chat_memory._write_meta_and_entries(history / 'alpha_chat.jsonl',meta,entries)
    if request.node.callspec.params['flow'] == 'account_return':
        # Simulate a pre-account legacy default history, which cannot be claimed
        # by whichever account happens to open the homepage first.
        chat_memory._write_meta_and_entries(history / 'default_chat.jsonl', chat_memory._default_meta('default'), [{
            'event':'chat_round','question':'Legacy fixture','status':'completed',
            'events':[{'role':'assistant','content':'Legacy private fixture'}]}])
    target = tmp_path / 'workspace' / 'example.txt'
    target.parent.mkdir()
    target.write_bytes(b'changed\n')
    file_history.record_change('alpha', path=str(target), display_path='example.txt', old_text='original\n',
                               new_text='changed\n', tool='edit_file', round_number=1)
    runtime = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='critical',
                                          access_log=False, lifespan='off', timeout_keep_alive=1,
                                          timeout_graceful_shutdown=2))
    def run_server():
        loop = asyncio.SelectorEventLoop() if os.name == 'nt' else asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(runtime.serve(sockets=[sock]))
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not runtime.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert runtime.started, 'isolated browser test server failed to start'
        yield {'origin': origin, 'password': PASSWORD, 'secret': secret, 'recovery': recovery,
               'scroll_fixture': os.environ.get('YTOOLS_SCROLL_FIXTURE'),
               'alice': alice['id'], 'bob': bob['id'], 'file_key': file_history.file_key(str(target))}
    finally:
        runtime.should_exit = True
        thread.join(10)
        if thread.is_alive():
            runtime.force_exit = True
            thread.join(5)
        sock.close()
        assert not thread.is_alive(), 'isolated test server did not stop'


@pytest.mark.parametrize('flow', ['editor', 'mfa_rotation', 'account_switch', 'stream_reload', 'history_scroll',
                                'login_mfa_retry', 'login_mfa_add', 'login_mfa_replace',
                                'recovery_regenerate', 'lost_device_rebind', 'account_return', 'username_change', 'account_preferences',
                                'account_panel_performance', 'account_panel_stream', 'account_panel_logout', 'account_panel_username', 'account_share_import', 'history_pages'])
def test_browser_page_flows(browser_app, flow):
    node = shutil.which('node')
    if not node or not (ROOT / 'browser/node_modules/playwright').is_dir():
        if os.environ.get('YTOOLS_REQUIRE_BROWSER_TESTS') == '1':
            pytest.fail('required browser dependencies are missing')
        pytest.skip('optional browser regression requires Node and browser/npm ci')
    environment = dict(os.environ, YTOOLS_E2E_CONFIG=json.dumps(browser_app))
    result = subprocess.run([node, str(ROOT / 'test/web_page_flows.cjs'), flow], cwd=ROOT,
                            env=environment, capture_output=True, text=True, encoding='utf-8', timeout=120)
    if result.returncode == 77:
        if os.environ.get('YTOOLS_REQUIRE_BROWSER_TESTS') == '1':
            pytest.fail('required Playwright Chromium is missing')
        pytest.skip('optional browser regression requires installed Playwright Chromium')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS ' + flow in result.stdout
