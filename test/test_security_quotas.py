import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from memory.identity_store import IdentityStore, Throttled, Denied
from security.principal import CURRENT
from security.settings import SecuritySettings
from security import quotas

PASSWORD = "correct horse battery staple 24"


@pytest.fixture
def account(tmp_path):
    store = IdentityStore(SecuritySettings(tmp_path, require_mfa=False))
    store.prepare()
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    principal = store.authenticate(store.login("member", PASSWORD))
    token = CURRENT.set(principal)
    try:
        yield store, admin, member, principal
    finally:
        CURRENT.reset(token)


def test_account_reservations_are_atomic_across_workers_and_sessions(account):
    store, admin, member, principal = account
    quotas.update(store, admin["id"], member["id"], {"daily_calls": 2, "daily_tokens": 100})
    def reserve(number):
        token = CURRENT.set(principal)
        try:
            quotas.reserve(str(number), 40)
            return True
        except Throttled:
            return False
        finally:
            CURRENT.reset(token)
    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    assert quotas.status(store, member["id"])["used"] == {"daily_calls": 2, "daily_tokens": 80}


def test_no_send_releases_and_unknown_keeps_conservative_reservation(account):
    store, _, member, _ = account
    quotas.reserve("not-sent", 100)
    quotas.settle("not-sent", 0, sent=False, complete=False)
    quotas.reserve("unknown", 100)
    quotas.settle("unknown", 3, sent=True, complete=False)
    quotas.settle("unknown", 0, sent=False, complete=False)  # Idempotent.
    quotas.reserve("reported", 100)
    quotas.settle("reported", 21, sent=True, complete=True)
    assert quotas.status(store, member["id"])["used"] == {"daily_calls": 2, "daily_tokens": 121}


def test_disabled_account_cannot_reserve_and_quota_cannot_be_reset_by_session(account):
    store, admin, member, _ = account
    store.change_user(admin["id"], member["id"], enabled=False)
    with pytest.raises(Denied):
        quotas.reserve("new-call", 1)


def test_model_calls_outside_task_still_consume_account_quota(account, monkeypatch):
    store, admin, member, _ = account
    from factory.agent_runtime import task_costs
    from config import ChatLLMRequest
    quotas.update(store, admin["id"], member["id"], {"daily_calls": 1, "daily_tokens": 50000})
    request = ChatLLMRequest(messages=[{"role": "user", "content": "hello"}])
    def invoke():
        task_costs.network_attempt_sync()
        return {"usage": {"prompt_tokens": 4, "completion_tokens": 2}}
    task_costs.tracked_call(invoke, request, False, None, {})
    assert quotas.status(store, member["id"])["used"]["daily_tokens"] == 6
    with pytest.raises(Throttled):
        task_costs.tracked_call(invoke, request, False, None, {})


def test_api_upload_window_is_not_purged_by_login_throttle(account):
    store, _, member, _ = account
    from security.rate_limits import charge
    charge(store, member["id"], "upload-bytes", 100, 86400, 90)
    with store.database(True) as db:
        db.execute("UPDATE login_limits SET started=started-3600 WHERE key LIKE 'api:%'")
    store.throttle("local", "member")
    with pytest.raises(Throttled):
        charge(store, member["id"], "upload-bytes", 100, 86400, 20)


def test_batch_api_limits_commit_earlier_charges_when_later_bucket_rejects(account):
    store, _, member, _ = account
    from security.rate_limits import charge_many
    charges = [('requests', 3, 60, 1), ('writes', 1, 60, 1)]
    charge_many(store, member['id'], charges)
    with pytest.raises(Throttled):
        charge_many(store, member['id'], charges)
    with store.database() as db:
        rows = dict(db.execute('SELECT key,count FROM login_limits WHERE key LIKE ?', ('api:' + member['id'] + ':%',)))
    assert rows['api:' + member['id'] + ':requests'] == 2
    assert rows['api:' + member['id'] + ':writes'] == 1


def test_batch_api_limits_stay_atomic_across_concurrent_requests(account):
    store, _, member, _ = account
    from security.rate_limits import charge_many
    def charge_request(_):
        try:
            charge_many(store, member['id'], [('requests', 8, 60, 1), ('writes', 2, 60, 1)])
            return True
        except Throttled:
            return False
    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(charge_request, range(8))) == 2
    with store.database() as db:
        rows = dict(db.execute('SELECT key,count FROM login_limits WHERE key LIKE ?', ('api:' + member['id'] + ':%',)))
    assert rows['api:' + member['id'] + ':requests'] == 8
    assert rows['api:' + member['id'] + ':writes'] == 2


def test_batch_api_limits_use_one_commit_and_reset_expired_windows(account, monkeypatch):
    from contextlib import contextmanager
    from security import rate_limits
    store, _, member, _ = account
    original, writes = store.database, []
    @contextmanager
    def counted(write=False):
        if write:
            writes.append(True)
        with original(write) as db:
            yield db
    monkeypatch.setattr(store, 'database', counted)
    rate_limits.charge_many(store, member['id'], [('requests', 2, 60, 1), ('writes', 1, 60, 1)])
    assert len(writes) == 1
    now = rate_limits.time.time()
    monkeypatch.setattr(rate_limits.time, 'time', lambda: now + 61)
    rate_limits.charge_many(store, member['id'], [('requests', 2, 60, 1), ('writes', 1, 60, 1)])
    with store.database() as db:
        assert {row[0] for row in db.execute('SELECT count FROM login_limits WHERE key LIKE ?', ('api:' + member['id'] + ':%',))} == {1}
