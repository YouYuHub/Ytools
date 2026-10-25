import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from memory.identity_store import IdentityStore
from security.local_credentials import private_file
from security.settings import SecuritySettings
from scripts.manage_accounts import bootstrap_temporary_admin


def test_private_credential_is_exclusive_and_cleaned_on_failure(tmp_path):
    path = tmp_path / "credential.txt"
    with private_file(path) as (stream, _):
        stream.write("fixture only")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        with private_file(path):
            pass
    assert path.read_text() == "fixture only"
    failed = tmp_path / "failed.txt"
    with pytest.raises(RuntimeError):
        with private_file(failed) as (stream, _):
            stream.write("fixture only")
            raise RuntimeError("failure")
    assert not failed.exists()


def test_temporary_bootstrap_does_not_print_password_and_requires_change(tmp_path, capsys):
    store = IdentityStore(SecuritySettings(tmp_path / "identity", require_mfa=False))
    store.prepare()
    credential = tmp_path / "initial.txt"
    user = bootstrap_temporary_admin(store, "admin", credential)
    password = next(line.split(": ", 1)[1] for line in credential.read_text().splitlines() if line.startswith("Temporary password: "))
    assert len(password) >= 24 and password not in json.dumps(user)
    assert password not in capsys.readouterr().out
    principal = store.authenticate(store.login("admin", password))
    assert principal and principal.password_change_required
    with pytest.raises(ValueError):
        bootstrap_temporary_admin(store, "another", tmp_path / "another.txt")
    assert not (tmp_path / "another.txt").exists()


def test_anonymous_real_entry_points_always_reach_login(tmp_path):
    app = create_app(security_settings=SecuritySettings(tmp_path / "identity", require_mfa=False))
    client = TestClient(app, base_url="http://127.0.0.1:48621")
    for entry in ("/ui", "/ui/", "/ui/index.html", "/index", "/index.html", "/ui/account.html"):
        response = client.get(entry)
        assert response.status_code == 200 and response.url.path == "/ui/login.html", entry
        assert "loginForm" in response.text
    assert client.get("/chat_history/summaries").status_code == 401
    assert client.get("/index.html?session=existing", follow_redirects=False).headers["location"] == "/ui/?session=existing"
