"""Already-enrolled accounts add/replace independent authenticators safely."""
import json
import time

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory.identity_store import IdentityStore
from security import crypto
from security.app import install_security
from security.settings import SecuritySettings

PASSWORD = 'disposable authenticator fixture password'


@pytest.fixture
def account(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    app = FastAPI(); store = install_security(app, SecuritySettings(tmp_path / 'identity'))
    owner = store.create_user('owner', PASSWORD, 'admin', bootstrap=True)
    other = store.create_user('other', PASSWORD, 'admin', actor=owner['id'])
    secrets = {user['id']: pyotp.random_base32() for user in (owner, other)}
    with store.database(True) as db:
        for user in (owner, other):
            db.execute('UPDATE users SET mfa_secret=?,must_change=0 WHERE id=?', (store.encrypt(secrets[user['id']]), user['id']))
            db.execute('INSERT INTO recovery_codes VALUES(?,?)', (user['id'], crypto.digest('a' * 24)))
    def advance(seconds=60): clock[0] += seconds
    def login(user=owner, secret=None):
        client = TestClient(app, base_url=store.settings.origin)
        response = client.post('/auth/login', json={'username': user['username'], 'password': PASSWORD}, headers={'Origin': store.settings.origin})
        assert response.status_code == 200
        client.headers.update({'Origin': store.settings.origin, 'X-CSRF-Token': response.json()['csrf']})
        if secret:
            response = client.post('/auth/mfa/verify', json={'code': pyotp.TOTP(secret).at(clock[0])})
            if response.status_code == 200: client.headers['X-CSRF-Token'] = response.json()['csrf']
            return client, response
        return client
    client, result = login(secret=secrets[owner['id']]); assert result.status_code == 200
    def begin(mode='add', target=client, secret=None):
        advance()
        response = target.post('/auth/mfa/manage/start', json={'password': PASSWORD,
            'code': pyotp.TOTP(secret or secrets[owner['id']]).at(clock[0]), 'mode': mode, 'label': 'Google Authenticator'})
        if response.status_code == 200: target.headers['X-CSRF-Token'] = response.json()['csrf']
        return response
    def confirm(binding, target=client):
        response = target.post('/auth/mfa/manage/confirm', json={'operation_id': binding['operation_id'], 'code': pyotp.TOTP(binding['secret']).at(clock[0])})
        if response.status_code == 200: target.headers['X-CSRF-Token'] = response.json()['csrf']
        return response
    return store, owner, other, secrets, clock, client, advance, login, begin, confirm


def test_add_preserves_old_factor_and_activates_independent_key_only_after_confirmation(account):
    store, owner, _, secrets, _, client, advance, login, begin, confirm = account
    response = begin(); assert response.status_code == 200
    binding = response.json()['binding']; assert binding['secret'] != secrets[owner['id']]
    _, pending_login = login(secret=binding['secret']); assert pending_login.status_code == 403
    with store.database() as db:
        encrypted = db.execute('SELECT secret FROM mfa_changes').fetchone()[0]
        assert binding['secret'] not in encrypted
    result = confirm(binding); assert result.status_code == 200
    assert len(result.json()['devices']) == 2 and result.json()['recovery_codes'] == []
    assert 'secret' not in json.dumps(client.get('/auth/mfa/devices').json())
    advance(); _, old = login(secret=secrets[owner['id']]); assert old.status_code == 200
    _, new = login(secret=binding['secret']); assert new.status_code == 200
    with store.database() as db:
        assert db.execute('SELECT count(*) FROM recovery_codes WHERE user_id=?', (owner['id'],)).fetchone()[0] == 1


def test_replace_is_atomic_revokes_old_sessions_and_refreshes_recovery_codes(account):
    store, owner, _, secrets, clock, client, advance, login, begin, confirm = account
    store.register('private-chat', owner['id'])
    advance(); other_session, response = login(secret=secrets[owner['id']]); assert response.status_code == 200
    binding = begin('replace').json()['binding']
    with store.database() as db:
        old_secret = db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0]
    accepted = {pyotp.TOTP(binding['secret']).at(clock[0] + delta) for delta in (-30, 0, 30)}
    wrong = next(str(value).zfill(6) for value in range(10) if str(value).zfill(6) not in accepted)
    bad = client.post('/auth/mfa/manage/confirm', json={'operation_id': binding['operation_id'], 'code': wrong})
    assert bad.status_code == 403
    with store.database() as db:
        assert db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0] == old_secret
    assert other_session.get('/auth/me').status_code == 200
    result = confirm(binding); assert result.status_code == 200
    assert len(result.json()['devices']) == 1 and len(result.json()['recovery_codes']) == 10
    assert other_session.get('/auth/me').status_code == 401
    assert client.get('/auth/me').status_code == 200
    advance(); _, old = login(secret=secrets[owner['id']]); assert old.status_code == 403
    _, new = login(secret=binding['secret']); assert new.status_code == 200
    assert store.owner('private-chat') == owner['id']
    with store.database() as db:
        assert not db.execute('SELECT 1 FROM recovery_codes WHERE user_id=? AND hash=?', (owner['id'], crypto.digest('a' * 24))).fetchone()


def test_cancel_expiry_and_cross_account_confirmation_do_not_change_active_devices(account):
    store, owner, other, secrets, _, client, advance, login, begin, confirm = account
    binding = begin().json()['binding']
    foreign, response = login(other, secrets[other['id']]); assert response.status_code == 200
    assert confirm(binding, foreign).status_code == 403
    assert foreign.post('/auth/mfa/manage/cancel', json={'operation_id': binding['operation_id']}).status_code == 200
    assert client.post('/auth/mfa/manage/cancel', json={'operation_id': binding['operation_id']}).status_code == 200
    assert confirm(binding).status_code == 403
    binding = begin().json()['binding']; advance(301)
    assert confirm(binding).status_code == 403
    assert len(client.get('/auth/mfa/devices').json()['devices']) == 1
    with store.database() as db:
        assert store.decrypt(db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0]) == secrets[owner['id']]


def test_remove_original_promotes_confirmed_device_and_cannot_remove_last(account):
    _, owner, _, secrets, clock, client, advance, login, begin, confirm = account
    binding = begin().json()['binding']; assert confirm(binding).status_code == 200
    advance()
    response = client.request('DELETE', '/auth/mfa/devices/primary', json={'password': PASSWORD, 'code': pyotp.TOTP(binding['secret']).at(clock[0])})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    assert response.json()['devices'] == [{'id': 'primary', 'label': 'Google Authenticator', 'primary': True}]
    advance()
    assert client.request('DELETE', '/auth/mfa/devices/primary', json={'password': PASSWORD, 'code': pyotp.TOTP(binding['secret']).at(clock[0])}).status_code == 403
    _, old = login(secret=secrets[owner['id']]); assert old.status_code == 403
    _, new = login(secret=binding['secret']); assert new.status_code == 200


def test_management_requires_current_password_and_factor_and_blocks_pending_login(account):
    store, owner, _, secrets, clock, client, advance, login, begin, _ = account
    pending = login(); assert pending.get('/auth/mfa/devices').status_code == 403
    advance()
    assert client.post('/auth/mfa/manage/start', json={'password': 'wrong', 'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])}).status_code == 403
    assert client.get('/auth/me').status_code == 200
    assert client.post('/auth/mfa/manage/start', json={'password': PASSWORD, 'code': ''}).status_code == 403
    assert begin().status_code == 200


def test_device_cap_and_offline_reset_clear_all_keys_and_pending_changes(account):
    store, owner, _, _, _, client, _, _, begin, confirm = account
    for _ in range(4):
        result = begin(); assert result.status_code == 200
        assert confirm(result.json()['binding']).status_code == 200
    assert len(client.get('/auth/mfa/devices').json()['devices']) == 5
    assert begin().status_code == 403
    binding = begin('replace').json()['binding']
    assert binding
    store.local_recover('owner', reset_mfa=True)
    with store.database() as db:
        for table in ('mfa_devices', 'mfa_state', 'mfa_changes', 'recovery_codes', 'login_sessions'):
            assert db.execute('SELECT count(*) FROM ' + table + ' WHERE user_id=?', (owner['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0] is None


def test_version_two_upgrade_preserves_original_binding_and_login(account):
    store, owner, _, _, _, client, *_ = account
    with store.database(True) as db:
        original = tuple(db.execute('SELECT mfa_secret,mfa_counter FROM users WHERE id=?', (owner['id'],)).fetchone())
        db.execute('DELETE FROM schema_version WHERE version>=3')
        for table in ('mfa_devices', 'mfa_state', 'mfa_changes'): db.execute('DROP TABLE ' + table)
    IdentityStore(store.settings).prepare()
    with store.database() as db:
        assert tuple(db.execute('SELECT mfa_secret,mfa_counter FROM users WHERE id=?', (owner['id'],)).fetchone()) == original
    assert client.get('/auth/me').status_code == 200
    assert len(client.get('/auth/mfa/devices').json()['devices']) == 1


def test_foreign_device_ids_and_extra_owner_fields_cannot_manage_another_account(account):
    _, owner, other, secrets, clock, client, advance, login, begin, confirm = account
    own = begin().json()['binding']; assert confirm(own).status_code == 200
    foreign, response = login(other, secrets[other['id']]); assert response.status_code == 200
    foreign_binding = begin(target=foreign, secret=secrets[other['id']]).json()['binding']
    assert confirm(foreign_binding, foreign).status_code == 200
    advance()
    response = client.request('DELETE', '/auth/mfa/devices/' + foreign_binding['operation_id'], json={
        'password': PASSWORD, 'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])})
    assert response.status_code == 403
    assert len(client.get('/auth/mfa/devices').json()['devices']) == 2
    assert len(foreign.get('/auth/mfa/devices').json()['devices']) == 2
    assert client.post('/auth/mfa/manage/start', json={'password': PASSWORD, 'code': '123456', 'user_id': other['id']}).status_code == 422


def test_revoke_cancels_staged_changes_and_audit_contains_no_secrets(account):
    store, owner, _, secrets, _, client, _, _, begin, _ = account
    binding = begin().json()['binding']
    audit = json.dumps(store.audit_entries())
    assert binding['secret'] not in audit and secrets[owner['id']] not in audit and PASSWORD not in audit
    store.revoke(owner['id'])
    assert client.post('/auth/mfa/manage/confirm', json={'operation_id': binding['operation_id'], 'code': '123456'}).status_code == 401
    with store.database() as db:
        assert db.execute('SELECT count(*) FROM mfa_changes WHERE user_id=?', (owner['id'],)).fetchone()[0] == 0


def test_management_requests_keep_origin_and_csrf_protection(account):
    _, _, _, _, _, client, _, _, begin, _ = account
    binding = begin().json()['binding']
    operations = [
        ('POST', '/auth/mfa/manage/start', {'password': PASSWORD, 'code': '123456'}),
        ('POST', '/auth/mfa/manage/confirm', {'operation_id': binding['operation_id'], 'code': '123456'}),
        ('POST', '/auth/mfa/manage/cancel', {'operation_id': binding['operation_id']}),
        ('DELETE', '/auth/mfa/devices/primary', {'password': PASSWORD, 'code': '123456'}),
    ]
    for method, path, payload in operations:
        assert client.request(method, path, json=payload, headers={'X-CSRF-Token': ''}).status_code == 403
        assert client.request(method, path, json=payload, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.get('/auth/me').status_code == 200
    assert client.post('/auth/mfa/manage/cancel', json={'operation_id': binding['operation_id']}).status_code == 200


def test_confirmed_change_invalidates_pending_changes_in_other_logins(account):
    _, owner, _, secrets, _, client, advance, login, begin, confirm = account
    binding = begin().json()['binding']
    advance(); other, response = login(secret=secrets[owner['id']]); assert response.status_code == 200
    competing = begin(target=other).json()['binding']
    assert confirm(binding).status_code == 200
    assert confirm(competing, other).status_code == 403
    assert len(other.get('/auth/mfa/devices').json()['devices']) == 2


def test_wrong_mfa_keeps_password_challenge_but_expired_login_requires_password(account):
    _, owner, _, secrets, clock, _, advance, login, _, _ = account
    pending = login()
    assert pending.get('/auth/status').json()['login_mfa_management'] is True
    # The fixture consumed this code during the initial login: replay is invalid.
    code = pyotp.TOTP(secrets[owner['id']]).at(clock[0])
    assert pending.post('/auth/mfa/verify', json={'code': code}).status_code == 403
    assert pending.get('/auth/me').json()['mfa_verified'] is False
    assert pending.get('/auth/mfa/devices').status_code == 403
    advance()
    result = pending.post('/auth/mfa/verify', json={'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])})
    assert result.status_code == 200 and result.json()['mfa_verified']
    expired = login(); advance(301)
    assert expired.post('/auth/mfa/verify', json={'code': '000000'}).status_code == 401


@pytest.mark.parametrize('mode', ['add', 'replace'])
@pytest.mark.parametrize('recovery', [False, True])
def test_login_can_prepare_qr_with_current_factor_without_resending_password(account, mode, recovery):
    store, owner, _, secrets, clock, _, advance, login, _, confirm = account
    pending = login(); advance()
    proof = 'a' * 24 if recovery else pyotp.TOTP(secrets[owner['id']]).at(clock[0])
    result = pending.post('/auth/mfa/manage/login_start', json={'mode': mode, 'label': 'Google Authenticator', 'code': proof})
    assert result.status_code == 200 and result.json()['mfa_verified']
    pending.headers['X-CSRF-Token'] = result.json()['csrf']
    binding = result.json()['binding']
    assert binding['secret'] != secrets[owner['id']]
    with store.database() as db:
        assert store.decrypt(db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0]) == secrets[owner['id']]
    assert pending.post('/auth/mfa/manage/login_start', json={'code': proof}).status_code == 403
    confirmed = confirm(binding, pending)
    assert confirmed.status_code == 200
    assert len(confirmed.json()['devices']) == (2 if mode == 'add' else 1)
    assert len(confirmed.json()['recovery_codes']) == (0 if mode == 'add' else 10)


def test_login_qr_needs_live_pending_login_valid_factor_origin_and_csrf(account):
    store, owner, _, secrets, clock, client, advance, login, _, _ = account
    pending = login(); payload = {'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])}
    assert pending.post('/auth/mfa/manage/login_start', json=payload).status_code == 403
    assert pending.get('/auth/me').json()['mfa_verified'] is False
    with store.database() as db:
        assert db.execute('SELECT count(*) FROM mfa_changes').fetchone()[0] == 0
    advance(); payload['code'] = pyotp.TOTP(secrets[owner['id']]).at(clock[0])
    assert pending.post('/auth/mfa/manage/login_start', json=payload, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert pending.post('/auth/mfa/manage/login_start', json=payload, headers={'X-CSRF-Token': ''}).status_code == 403
    assert pending.post('/auth/mfa/manage/login_start', json={**payload, 'user_id': owner['id']}).status_code == 422
    assert client.post('/auth/mfa/manage/login_start', json=payload).status_code == 403
    anonymous = TestClient(pending.app, base_url=store.settings.origin)
    assert anonymous.post('/auth/mfa/manage/login_start', json=payload, headers={'Origin': store.settings.origin}).status_code == 401
    advance(301)
    assert pending.post('/auth/mfa/manage/login_start', json=payload).status_code == 401


def test_login_qr_does_not_bypass_required_password_change(account):
    store, owner, _, secrets, clock, _, advance, login, _, _ = account
    with store.database(True) as db:
        db.execute('UPDATE users SET must_change=1 WHERE id=?', (owner['id'],))
    pending = login(); advance()
    payload = {'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])}
    response = pending.post('/auth/mfa/manage/login_start', json=payload)
    assert response.status_code == 403 and '初始密码' in response.json()['detail']
    assert pending.post('/auth/mfa/verify', json=payload).status_code == 200


def test_first_enrollment_errors_preserve_the_pending_login_and_binding(account):
    store, owner, _, _, clock, _, _, login, _, _ = account
    with store.database(True) as db:
        db.execute('UPDATE users SET mfa_secret=NULL,mfa_counter=-1 WHERE id=?', (owner['id'],))
        db.execute('DELETE FROM recovery_codes WHERE user_id=?', (owner['id'],))
    pending = login()
    assert pending.post('/auth/mfa/start', json={'password': 'wrong'}).status_code == 403
    binding = pending.post('/auth/mfa/start', json={'password': PASSWORD}).json()
    assert pending.post('/auth/mfa/confirm', json={'code': 'invalid'}).status_code == 403
    assert pending.get('/auth/me').json()['mfa_verified'] is False
    result = pending.post('/auth/mfa/confirm', json={'code': pyotp.TOTP(binding['secret']).at(clock[0])})
    assert result.status_code == 200 and result.json()['mfa_verified']


@pytest.mark.parametrize('role', ['member', 'operator', 'admin'])
def test_offline_lost_device_recovery_requires_new_enrollment_for_every_role(account, role):
    from dataclasses import replace
    store, owner, _, _, clock, _, _, login, _, _ = account
    store.settings = replace(store.settings, require_mfa=False)
    user = store.create_user('recover', PASSWORD, role, actor=owner['id'])
    old_secret = pyotp.random_base32()
    with store.database(True) as db:
        db.execute('UPDATE users SET must_change=0,mfa_secret=? WHERE id=?', (store.encrypt(old_secret), user['id']))
        original = tuple(db.execute('SELECT password_hash,role FROM users WHERE id=?', (user['id'],)).fetchone())
    store.register('preserved-recovery-chat', user['id'])
    old, response = login(user, old_secret); assert response.status_code == 200
    recovered = store.local_recover('recover', reset_mfa=True)
    assert recovered['mfa_setup_required'] and not recovered['mfa_enabled']
    assert old.get('/auth/me').status_code == 401
    pending = login(user)
    assert pending.get('/auth/me').json()['mfa_verified'] is False
    assert pending.get('/auth/sessions').status_code == 403
    binding = pending.post('/auth/mfa/start', json={'password': PASSWORD}).json()
    response = pending.post('/auth/mfa/confirm', json={'code': pyotp.TOTP(binding['secret']).at(clock[0])})
    assert response.status_code == 200 and response.json()['mfa_verified']
    assert not response.json()['user']['mfa_setup_required']
    assert store.owner('preserved-recovery-chat') == user['id']
    with store.database() as db:
        assert tuple(db.execute('SELECT password_hash,role FROM users WHERE id=?', (user['id'],)).fetchone()) == original
        assert db.execute('SELECT count(*) FROM mfa_recovery_required WHERE user_id=?', (user['id'],)).fetchone()[0] == 0


def test_schema_three_upgrade_only_adds_recovery_requirement_table(account):
    store, *_ = account
    with store.database(True) as db:
        before = db.execute('SELECT * FROM users ORDER BY id').fetchall()
        db.execute('DELETE FROM schema_version WHERE version>=4')
        db.execute('DROP TABLE mfa_recovery_required')
    IdentityStore(store.settings).prepare()
    with store.database() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM users ORDER BY id')] == [tuple(row) for row in before]
        assert db.execute('SELECT max(version) FROM schema_version').fetchone()[0] == 7
        assert db.execute('SELECT count(*) FROM mfa_recovery_required').fetchone()[0] == 0


def test_regenerate_backup_revokes_old_codes_and_other_logins_but_preserves_authenticators(account):
    store, owner, _, secrets, clock, client, advance, login, _, _ = account
    advance(); other, response = login(secret=secrets[owner['id']]); assert response.status_code == 200
    with store.database() as db:
        binding = db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0]
    advance()
    result = client.post('/auth/mfa/recovery/regenerate', json={'password': PASSWORD, 'code': pyotp.TOTP(secrets[owner['id']]).at(clock[0])})
    assert result.status_code == 200
    codes = result.json()['recovery_codes']; assert len(codes) == len(set(codes)) == 10
    client.headers['X-CSRF-Token'] = result.json()['csrf']
    assert client.get('/auth/mfa/devices').json()['recovery_codes_remaining'] == 10
    assert other.get('/auth/me').status_code == 401
    with store.database() as db:
        assert db.execute('SELECT mfa_secret FROM users WHERE id=?', (owner['id'],)).fetchone()[0] == binding
        assert {row[0] for row in db.execute('SELECT hash FROM recovery_codes WHERE user_id=?', (owner['id'],))} == {crypto.digest(code) for code in codes}
    obsolete = login(); assert obsolete.post('/auth/mfa/verify', json={'code': 'a'*24}).status_code == 403
    restored = login()
    formatted = '-'.join(codes[0][index:index+6].upper() for index in range(0,24,6))
    assert restored.post('/auth/mfa/verify', json={'code': '  '+formatted+'  '}).status_code == 200
    replay = login(); assert replay.post('/auth/mfa/verify', json={'code': codes[0]}).status_code == 403
    audit = json.dumps(store.audit_entries())
    assert all(code not in audit for code in codes) and PASSWORD not in audit


def test_backup_regeneration_requires_password_factor_origin_csrf_and_verified_login(account):
    store, owner, _, secrets, clock, client, advance, login, _, _ = account
    pending = login(); payload = {'password': PASSWORD, 'code': 'a'*24}
    assert pending.post('/auth/mfa/recovery/regenerate', json=payload).status_code == 403
    advance(); payload['code'] = pyotp.TOTP(secrets[owner['id']]).at(clock[0])
    assert client.post('/auth/mfa/recovery/regenerate', json=payload, headers={'Origin':'https://untrusted.example'}).status_code == 403
    assert client.post('/auth/mfa/recovery/regenerate', json=payload, headers={'X-CSRF-Token':''}).status_code == 403
    assert client.post('/auth/mfa/recovery/regenerate', json={**payload,'password':'wrong'}).status_code == 403
    with store.database() as db:
        assert db.execute('SELECT count(*) FROM recovery_codes WHERE user_id=?', (owner['id'],)).fetchone()[0] == 1
    assert client.post('/auth/mfa/recovery/regenerate', json={**payload,'user_id':owner['id']}).status_code == 422
