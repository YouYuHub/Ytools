import pytest

from security.settings import SecuritySettings


@pytest.fixture
def entrypoint(tmp_path, monkeypatch):
    from scripts import serve
    import env_manager, config, uvicorn
    monkeypatch.setattr(env_manager, "init_path", lambda _: None)
    monkeypatch.setattr(config, "DEFAULT_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "DEFAULT_SERVICE_PORT", 48621)
    settings = SecuritySettings(tmp_path, production=True, origin="https://chat.example.test")
    monkeypatch.setattr(SecuritySettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.delenv("YTOOLS_PROXY_IPS", raising=False)
    called = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: called.append((args, kwargs)))
    return serve, config, called


def test_production_entrypoint_rejects_public_backend_and_wildcard_proxy(entrypoint, monkeypatch):
    serve, config, called = entrypoint
    monkeypatch.setattr(config, "DEFAULT_SERVICE_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="回环"):
        serve.main()
    monkeypatch.setattr(config, "DEFAULT_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("YTOOLS_PROXY_IPS", "*")
    with pytest.raises(ValueError, match="任意来源"):
        serve.main()
    assert not called


def test_production_entrypoint_keeps_sse_single_process_and_avoids_ticket_logs(entrypoint):
    serve, _, called = entrypoint
    serve.main()
    config = called[0][1]
    assert config["host"] == "127.0.0.1" and config["workers"] == 1
    assert config["proxy_headers"] and config["forwarded_allow_ips"] == "127.0.0.1,::1"
    assert config["access_log"] is False


def test_environment_pins_work_directory_over_mutable_project_setting(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "load_var", lambda *args: "/mutable/project")
    monkeypatch.setenv("DEFAULT_CHAT_WORK_DIR", str(tmp_path))
    assert config.get_persisted_work_dir() == str(tmp_path)
