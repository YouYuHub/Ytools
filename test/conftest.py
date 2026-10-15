"""Permission persistence must never use the developer's real control database."""
import pytest
from memory import permission_store


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
