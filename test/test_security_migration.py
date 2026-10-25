import pytest

from memory.identity_store import IdentityStore
from security.principal import CURRENT
from security.settings import SecuritySettings


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    from memory import chat_memory, task_template_store
    from prompt import prompt_manager
    from scripts.manage_accounts import adopt_legacy
    assert CURRENT.get() is None
    history = tmp_path / "history"
    history.mkdir()
    raw = b'{"_meta":{"title":"legacy","responses_state":{"opaque":"keep"}}}\r\n{"event":"chat_round","content":"unaltered"}\n'
    (history / "legacy_chat.jsonl").write_bytes(raw)
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", history)
    monkeypatch.setattr(chat_memory, "_read_session_groups_raw", lambda: [{"id": "old-group"}])
    monkeypatch.setattr(task_template_store, "ROOT", tmp_path / "templates")
    monkeypatch.setattr(prompt_manager, "MD_FILES_DIR", tmp_path / "prompts")
    prompt_manager.MD_FILES_DIR.mkdir()
    (prompt_manager.MD_FILES_DIR / "old.md").write_text("legacy prompt", encoding="utf-8")
    store = IdentityStore(SecuritySettings(tmp_path / "identity", require_mfa=False))
    store.prepare()
    user = store.create_user("admin", "correct horse battery staple 24", "admin", bootstrap=True)
    return store, user, history, raw, prompt_manager, task_template_store, adopt_legacy


def test_explicit_adoption_preserves_bytes_and_copies_private_template(legacy):
    store, user, history, raw, prompts, templates, adopt = legacy
    old = templates.save({"name": "old", "description": "legacy", "prompt": "keep"})
    preview = adopt(store, user)
    assert not preview["applied"] and store.owner("session", "legacy") is None
    assert not (templates.ROOT / user["id"]).exists()
    assert adopt(store, user, apply=True)["applied"]
    assert store.owner("session", "legacy") == user["id"]
    assert store.owner("group", "old-group") == user["id"]
    assert (history / "legacy_chat.jsonl").read_bytes() == raw
    assert (prompts.MD_FILES_DIR / user["id"] / "old.md").read_text(encoding="utf-8") == "legacy prompt"
    token = CURRENT.set(store.authenticate(store.login("admin", "correct horse battery staple 24")))
    try:
        assert templates.read(old["id"])["template"] == old["template"]
    finally:
        CURRENT.reset(token)


def test_adoption_conflict_does_not_claim_or_rewrite_legacy(legacy):
    store, user, history, raw, prompts, _, adopt = legacy
    private = prompts.MD_FILES_DIR / user["id"]
    private.mkdir()
    (private / "old.md").write_text("new-private", encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        adopt(store, user, apply=True)
    assert store.owner("session", "legacy") is None
    assert (history / "legacy_chat.jsonl").read_bytes() == raw
    assert (private / "old.md").read_text(encoding="utf-8") == "new-private"
