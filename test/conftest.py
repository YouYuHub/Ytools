"""Permission persistence must never use the developer's real control database."""
import copy

import pytest
from memory import permission_store


_ENV_GLOBAL_NAMES = (
    "env_file",
    "env_vars",
    "setting_file",
    "models_config",
    "mcp_config",
    "model_selection",
    "setting_vars",
)


@pytest.fixture(autouse=True, scope="module")
def _isolate_env_manager_globals():
    """Snapshot env globals per module; never leak into later modules.

    Several suites redirect env_manager at runtime (init_path to temp dirs and
    back to the real repo). Restoring by reloading real files leaks
    machine-local configuration (e.g. a Responses chat model) into later
    modules: compaction tests then take the Responses branch and tag messages
    with context_item_id. Module scope (not function) preserves modules that
    legitimately build on their own import-time init.
    """
    import env_manager

    snapshot = {
        name: copy.deepcopy(getattr(env_manager, name, None))
        for name in _ENV_GLOBAL_NAMES
    }
    yield
    for name, value in snapshot.items():
        setattr(env_manager, name, value)


@pytest.fixture(autouse=True)
def permission_database(tmp_path, monkeypatch, request):
    monkeypatch.setattr(permission_store, "ROOT", tmp_path / "permissions")
    # Legacy tool/protocol regression tests exercise the former unrestricted
    # behavior. Permission-specific tests use the production default (ask).
    name = request.node.path.stem
    if not name.startswith("test_permission") and name != "test_tool_permissions":
        permission_store.set_policy("full_control")


@pytest.fixture(autouse=True)
def isolated_crash_history(tmp_path, monkeypatch, request):
    # Crash synthesis writes history asynchronously; do not leave its synthetic
    # session in the user's conversation list.
    if request.node.path.stem == "test_session_worker_crash_visibility":
        from memory import chat_memory, file_memory
        monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
        monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
        monkeypatch.setattr(chat_memory, "_session_managers", {})
        monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "uploads")
