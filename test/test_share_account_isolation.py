"""Two disposable accounts exercise real share/import and read boundaries."""
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from security.principal import CURRENT
from security.principal import Principal
from security.settings import SecuritySettings

PASSWORD = "isolated share fixture password 2026"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    import env_manager
    from memory import chat_memory as cm, file_memory as fm, file_history, task_template_store
    from prompt import prompt_manager
    configuration = tmp_path / "config"
    configuration.mkdir()
    env_manager.init_path(configuration)
    history = tmp_path / "history_files"
    history.mkdir()
    monkeypatch.setattr(cm, "HISTORY_ROOT", history)
    monkeypatch.setattr(cm, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(fm, "HISTORY_ROOT", history / "session_files")
    monkeypatch.setattr(fm, "_session_managers", {})
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", history / "session_files")
    monkeypatch.setattr(task_template_store, "ROOT", tmp_path / "templates")
    monkeypatch.setattr(prompt_manager, "MD_FILES_DIR", tmp_path / "prompts")
    app = create_app(security_settings=SecuritySettings(tmp_path / "identity", require_mfa=False))
    store = app.state.identity_store
    first = store.create_user("first", PASSWORD, "admin", bootstrap=True)
    second = store.create_user("second", PASSWORD, "admin", actor=first["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    clients = []
    for user in (first, second):
        client = TestClient(app, base_url=store.settings.origin)
        reply = client.post("/auth/login", json={"username":user["username"], "password":PASSWORD}, headers={"Origin":store.settings.origin})
        assert reply.status_code == 200
        client.headers.update({"Origin":store.settings.origin,"X-CSRF-Token":reply.json()["csrf"]})
        clients.append(client)
    return store, (first, second), clients, history


def payload(text="shared public fixture", **meta):
    return (json.dumps({"_meta":{"title":text, **meta}}) + "\n" + json.dumps({
        "event":"chat_round", "question":text, "status":"done",
        "events":[{"role":"user","content":text},{"role":"assistant","content":text}]} ) + "\n").encode()


def seed(store, root, sid, user_id, data):
    store.register(sid, user_id)
    directory = root / "users" / user_id / "sessions" / sid
    directory.mkdir(parents=True)
    path = directory / (sid + "_chat.jsonl")
    path.write_bytes(data)
    return path


def post_file(client, endpoint, name, data, **query):
    return client.post("/chat_history/" + endpoint, params={"filename":name, **query},
                       content=data, headers={"Content-Type":"application/octet-stream"})


@contextlib.contextmanager
def as_user(store, user):
    token=CURRENT.set(Principal(user["id"],user["username"],user["role"],"fixture",0,True,"",store))
    try:
        yield
    finally:
        CURRENT.reset(token)


@pytest.mark.parametrize("endpoint", ["import_package", "upload_chat_file"])
def test_new_jsonl_import_is_immediately_loadable_and_in_own_directory(accounts, endpoint):
    store, users, clients, root = accounts
    reply = post_file(clients[0], endpoint, "new-share_chat.jsonl", payload(), **({"session_id":"new-share"} if endpoint == "upload_chat_file" else {}))
    assert reply.status_code == 200, reply.text
    data = reply.json()
    sid = data["imported"][0]["session_id"] if endpoint == "import_package" else data["session_id"]
    result = data["imported"][0] if endpoint == "import_package" else data
    assert result["imported_rounds"]==1 and result["skipped_lines"]==0
    loaded = clients[0].get("/chat_history/file", params={"session_id":sid})
    assert loaded.status_code == 200, loaded.text
    assert "shared public fixture" in loaded.text
    assert clients[1].get("/chat_history/file", params={"session_id":sid}).status_code == 404
    assert (root / "users" / users[0]["id"] / "sessions" / sid / (sid + "_chat.jsonl")).exists()
    assert not (root / (sid + "_chat.jsonl")).exists()


def test_foreign_reserved_identifier_is_copied_without_claiming_it(accounts):
    store, users, (first, second), root = accounts
    assert first.post("/chat_history/ensure_session",json={"session_id":"reserved"}).status_code == 200
    imported = post_file(second,"import_package","reserved_chat.jsonl",payload())
    assert imported.status_code == 200, imported.text
    data=imported.json()
    assert data["imported"], data
    sid=data["imported"][0]["session_id"]
    assert sid!="reserved" and store.owner("reserved")==users[0]["id"]
    assert store.owner(sid)==users[1]["id"]
    assert second.get("/chat_history/file",params={"session_id":sid}).status_code==200


def test_foreign_share_preview_is_not_a_local_overwrite_conflict(accounts):
    store, users, (first, second), root = accounts
    source = seed(store, root, "source", users[0]["id"], payload("source shared content"))
    archive=first.get("/chat_history/export_zip?session_ids=source")
    assert archive.status_code==200
    preview=post_file(second,"import_preview","shared.zip",archive.content)
    assert preview.status_code==200,preview.text
    assert preview.json()["sessions"][0]["exists"] is False
    assert preview.json()["conflicts"]==[]
    result=post_file(second,"import_package","shared.zip",archive.content,conflict_strategy="overwrite")
    assert result.status_code==200,result.text
    assert result.json()["imported"] and not result.json()["failed"]
    sid=result.json()["imported"][0]["session_id"]
    assert sid!="source" and store.owner(sid)==users[1]["id"]
    assert source.read_bytes()==payload("source shared content")
    assert second.get("/chat_history/file?session_id=source").status_code==404
    assert second.get("/chat_history/file",params={"session_id":sid}).status_code==200


def test_untrusted_jsonl_cannot_rebind_a_legacy_session_to_foreign_attachments(accounts):
    store, users, (first, second), root=accounts
    for sid, user in (("victim",users[0]),("mine",users[1])):
        seed(store, root, sid, user["id"], payload(sid,upload_id=sid))
    secret=root/"users"/users[0]["id"]/"sessions/victim/files/files/private.txt"
    secret.parent.mkdir(parents=True);secret.write_text("other account attachment",encoding="utf-8")
    result=post_file(second,"import_package","mine_chat.jsonl",payload("imported",upload_id="victim"),conflict_strategy="overwrite")
    assert result.status_code==200,result.text
    assert second.get("/file/get_session_document?session_id=mine&name=private.txt").status_code!=200
    export=second.get("/chat_history/export_zip?session_ids=mine")
    assert export.status_code==200,export.text
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert b"other account attachment" not in b"".join(archive.read(name) for name in archive.namelist())


def test_conflicting_upload_headers_stay_in_separate_account_directories(accounts):
    store,users,(first,second),root=accounts
    for sid,user in (("victim",users[0]),("mine",users[1])):
        seed(store, root, sid, user["id"], payload(sid,upload_id="victim"))
    secret=root/"users"/users[0]["id"]/"sessions/victim/files/files/private.txt";secret.parent.mkdir(parents=True);secret.write_text("legacy private bytes")
    # mine 的 uploads/victim/ 与 victim 的 files/ 物理隔离，读不到对方附件。
    assert second.get("/file/get_session_document?session_id=mine&name=private.txt").status_code!=200
    export=second.get("/chat_history/export_zip?session_ids=mine")
    assert export.status_code==200,export.text
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert b"legacy private bytes" not in b"".join(archive.read(name) for name in archive.namelist())


def test_zip_copy_keeps_attachments_groups_and_permission_scoped_to_receiver(accounts):
    from memory import chat_memory as cm,permission_store
    store,users,(first,second),root=accounts
    seed(store, root, "source", users[0]["id"], payload("shared content",upload_id="source"))
    document=root/"users"/users[0]["id"]/"sessions/source/files/files/document.txt";document.parent.mkdir(parents=True);document.write_text("shared document")
    group=first.post("/chat_history/groups",json={"name":"shared group"}).json()["group"]
    assert first.put("/chat_history/group_assign",json={"session_id":"source","group_id":group["id"]}).status_code==200
    with as_user(store,users[0]):
        permission_store.set_policy("ask","source")
    archive=first.get("/chat_history/export_zip?session_ids=source").content
    result=post_file(second,"import_package","shared.zip",archive).json()
    assert not result["failed"] and len(result["imported"])==1,result
    imported=result["imported"][0];sid=imported["session_id"]
    assert sid!="source" and store.owner(sid)==users[1]["id"]
    assert imported["group_id"] in {g["id"] for g in second.get("/chat_history/groups").json()["groups"]}
    assert second.get("/file/get_session_document",params={"session_id":sid,"name":"document.txt"}).text=="shared document"
    document.write_text("sender later changed")
    assert second.get("/file/get_session_document",params={"session_id":sid,"name":"document.txt"}).text=="shared document"
    with as_user(store,users[1]):
        assert permission_store.policy(sid)["session_mode"]=="ask"
        with pytest.raises(Exception):
            cm.export_sessions_to_zip(["source"])
    assert first.get("/chat_history/file",params={"session_id":sid}).status_code==404


def test_concurrent_bare_imports_never_overwrite_each_other(accounts):
    from memory import chat_memory as cm
    store,users,clients,root=accounts
    def save(number):
        with as_user(store,users[1]):
            return cm._import_jsonl_payload("concurrent",payload("content-"+str(number)))["session_id"]
    with ThreadPoolExecutor(2) as workers:
        ids=list(workers.map(save,(1,2)))
    assert len(set(ids))==2
    for number,sid in enumerate(ids,1):
        assert "content-"+str(number) in clients[1].get("/chat_history/file",params={"session_id":sid}).text


def test_import_history_is_subject_to_account_storage_quota(accounts):
    from security import quotas
    store,users,clients,root=accounts
    quotas.update(store,users[0]["id"],users[1]["id"],{"storage_bytes":1024})
    result=post_file(clients[1],"import_package","large_chat.jsonl",payload("x"*5000))
    assert result.status_code in {400,429} and "存储" in result.text
    assert list(root.rglob("*_chat.jsonl"))==[]


def test_export_removes_model_credentials_inside_protocol_parameter_buckets(accounts):
    store,users,clients,root=accounts
    model={"chat_model":{"ownership_name":"fixture","model_name":"fixture","headers":[{"key":"Authorization","value":"TOP-PRIVATE"}],
        "parameter":{"chat_completions":{"temperature":0.2,"headers":{"Authorization":"NESTED-PRIVATE"},"api_key":"KEY-PRIVATE"}}}}
    seed(store, root, "source", users[0]["id"], payload(model_selection=model))
    response=clients[0].get("/chat_history/export_zip?session_ids=source")
    assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        history=archive.read("source_chat.jsonl")
    assert b"PRIVATE" not in history and b"temperature" in history


@pytest.mark.parametrize("package", [False, True], ids=["jsonl", "zip"])
def test_package_with_only_invalid_rounds_cannot_erase_existing_history(accounts, package):
    store,users,clients,root=accounts
    original=payload("keep this")
    mine_path=seed(store, root, "mine", users[0]["id"], original)
    data=b'{"_meta":{}}\ninvalid garbage\n'
    if package:
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,"w") as archive:
            archive.writestr("mine_chat.jsonl",data)
        data=buffer.getvalue()
    result=post_file(clients[0],"import_package","mine.zip" if package else "mine_chat.jsonl",data,conflict_strategy="overwrite")
    if package:
        assert result.status_code==200 and not result.json()["imported"] and result.json()["failed"]
    else:
        assert result.status_code==400
    assert mine_path.read_bytes()==original


@pytest.mark.skipif(os.name == "nt", reason="Windows session IDs are case insensitive")
def test_case_sensitive_exports_reject_portable_path_collision_without_omitting_sessions(accounts):
    store,users,clients,root=accounts
    for sid in ("lower", "LOWER"):
        seed(store, root, sid, users[0]["id"], payload(sid))
    combined=clients[0].get("/chat_history/export_zip",params={"session_ids":"lower,LOWER"})
    assert combined.status_code==400 and "分别分享" in combined.text
    for sid in ("lower", "LOWER"):
        assert clients[0].get("/chat_history/export_zip",params={"session_ids":sid}).status_code==200


def test_uppercase_unicode_package_and_attachment_paths_are_canonical_and_reexportable(accounts):
    from memory import chat_memory as cm,file_memory as fm
    store,users,(first,second),root=accounts
    source="UpperStraße";buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w") as archive:
        archive.writestr(source+"_chat.jsonl",payload(upload_id="source-upload"))
        archive.writestr("session_files/source-upload/files/document.txt",b"portable document")
        archive.writestr("manifest.json",json.dumps({"version":1,"sessions":[{"session_id":source,"filename":source+"_chat.jsonl","upload_id":"source-upload"}]}))
    result=post_file(first,"import_package","unicode.zip",buffer.getvalue()).json()
    assert result["imported"] and not result["failed"],result
    sid=result["imported"][0]["session_id"]
    assert sid==store.resource_key(source)
    assert first.get("/file/get_session_document",params={"session_id":source,"name":"document.txt"}).text=="portable document"
    with as_user(store,users[0]):
        assert asyncio.run(cm.get_chat_memory_manager(source)) is asyncio.run(cm.get_chat_memory_manager(sid))
        assert asyncio.run(fm.get_file_memory_manager(source)) is asyncio.run(fm.get_file_memory_manager(sid))
    with as_user(store,users[1]):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            asyncio.run(fm.get_file_memory_manager(sid))
    exported=first.get("/chat_history/export_zip",params={"session_ids":source})
    assert exported.status_code==200
    preview=post_file(second,"import_preview","unicode.zip",exported.content)
    assert preview.status_code==200,preview.text


def test_normal_member_reads_and_exports_only_own_history_and_does_not_gain_import_execution_rights(accounts):
    store,users,clients,root=accounts
    member=store.create_user("member",PASSWORD,actor=users[0]["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0 WHERE id=?",(member["id"],))
    seed(store, root, "member-session", member["id"], payload("member own content"))
    client=TestClient(clients[0].app,base_url=store.settings.origin)
    login=client.post("/auth/login",json={"username":"member","password":PASSWORD},headers={"Origin":store.settings.origin})
    client.headers.update({"Origin":store.settings.origin,"X-CSRF-Token":login.json()["csrf"]})
    assert client.get("/chat_history/file?session_id=member-session").status_code==200
    assert client.get("/chat_history/export_zip?session_ids=member-session").status_code==200
    result=post_file(client,"import_package","new_chat.jsonl",payload())
    assert result.status_code==403


def test_export_size_limit_is_checked_before_reading_large_attachments(accounts,monkeypatch):
    from memory import chat_memory as cm
    store,users,clients,root=accounts
    seed(store, root, "large", users[0]["id"], payload())
    directory=root/"users"/users[0]["id"]/"sessions/large/files/files";directory.mkdir(parents=True)
    first=directory/"one.bin";first.write_bytes(b"x"*100)
    monkeypatch.setattr(cm,"IMPORT_PACKAGE_MAX_BYTES",4*1024*1024+50)
    response=clients[0].get("/chat_history/export_zip?session_ids=large")
    assert response.status_code==400 and "上限" in response.text
