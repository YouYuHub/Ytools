"""Offline account bootstrap, recovery and explicit legacy ownership migration."""
import argparse
import contextlib
import getpass
import hashlib
import json
import os
import shutil
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def password():
    first = getpass.getpass("新密码（至少 15 字符）: ")
    if first != getpass.getpass("再次输入: "):
        raise ValueError("两次密码不一致")
    return first


def bootstrap_temporary_admin(store, name, password_file):
    from security.crypto import username
    from security.local_credentials import private_file
    name = username(name)
    if store.users():
        raise ValueError("账号已存在，不能再次初始化管理员")
    temporary = secrets.token_urlsafe(24)
    with private_file(password_file) as (stream, path):
        stream.write("Ytools initial administrator\nUsername: " + name + "\nTemporary password: " + temporary
                     + "\nLogin: " + store.settings.origin + "/ui/login.html\n"
                     + "Change this password at first login and bind your authenticator. Delete this file after changing it.\n")
        stream.flush()
        os.fsync(stream.fileno())
        user = store.create_user(name, temporary, "admin", bootstrap=True)
        with store.database(True) as db:
            db.execute("UPDATE users SET must_change=1 WHERE id=?", (user["id"],))
            store._audit(db, "local-cli", "user.bootstrap_temporary", user["id"])
    return {**store.user(user["id"]), "credential_file": str(path)}


def adopt_legacy(store, user, apply=False):
    """Only explicitly selected account can own unowned legacy data."""
    from memory import chat_memory
    from memory import task_template_store
    from prompt import prompt_manager
    sessions, groups, skipped = [], [], []
    for path in chat_memory.HISTORY_ROOT.glob("*_chat.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        identifier = chat_memory.normalize_session_id(path.name)
        owner = store.owner("session", identifier)
        if owner is None:
            sessions.append(identifier)
        else:
            skipped.append(identifier)
    for group in chat_memory._read_session_groups_raw():
        if store.owner("group", group["id"]) is None:
            groups.append(group["id"])
    template_source = task_template_store.ROOT / "templates.sqlite3"
    template_target = task_template_store.ROOT / user["id"] / "templates.sqlite3"
    prompts = [path for path in prompt_manager.MD_FILES_DIR.glob("*.md") if not path.is_symlink()]
    conflicts = []
    for source in prompts:
        target = prompt_manager.MD_FILES_DIR / user["id"] / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            conflicts.append(source.name)
    if template_source.exists() and template_target.exists():
        conflicts.append("private templates already exist")
    result = {"owner": user["username"], "sessions": sessions, "groups": groups, "already_owned": len(skipped),
              "prompts": len(prompts), "templates": template_source.exists(), "conflicts": conflicts, "applied": False}
    if not apply:
        return result
    if conflicts:
        raise ValueError("迁移存在冲突，请先整理私有提示词/模板；未修改归属")
    # Metadata adoption leaves JSONL/media/version/provider-state bytes in place.
    # Save a durable manifest before assigning anything. Unknown items stay denied
    # after interruption; repeating the command safely skips already-owned IDs.
    manifest = store.root / ("adoption-" + user["id"] + ".json")
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for kind, identifiers in (("session", sessions), ("group", groups)):
        for identifier in identifiers:
            store.register(kind, identifier, user["id"])
    if template_source.exists():
        template_target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(f"file:{template_source.as_posix()}?mode=ro", uri=True)) as source:
            destination = sqlite3.connect(template_target)
            try:
                source.backup(destination)
            finally:
                destination.close()
    for source in prompts:
        target = prompt_manager.MD_FILES_DIR / user["id"] / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
    store.audit("local-cli", "legacy.adopt", user["id"], sessions=len(sessions), groups=len(groups))
    result["applied"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-admin", "create-user", "list", "reset-password", "reset-mfa", "adopt-legacy"])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=["admin", "operator", "member"], default="member")
    parser.add_argument("--trusted", action="store_true", help="确认管理员/操作员属于可信实例操作者")
    parser.add_argument("--apply", action="store_true", help="实际执行旧数据归属迁移；默认只预览")
    parser.add_argument("--temporary-password-file", help="init-admin 专用：随机临时密码写入仅当前用户可读的新文件，首次登录强制改密")
    args = parser.parse_args()
    if args.temporary_password_file and args.action != "init-admin":
        parser.error("--temporary-password-file 仅用于首次初始化管理员")
    from env_manager import init_path
    init_path(ROOT)
    from memory.identity_store import IdentityStore
    from security.settings import SecuritySettings
    from security.crypto import username
    store = IdentityStore(SecuritySettings.from_env())
    store.prepare()
    if args.action == "list":
        print(json.dumps(store.users(), ensure_ascii=False, indent=2))
        return
    if not args.username:
        parser.error("该操作需要 --username")
    if args.action == "init-admin":
        result = (bootstrap_temporary_admin(store, args.username, args.temporary_password_file)
                  if args.temporary_password_file else store.create_user(args.username, password(), "admin", bootstrap=True))
    elif args.action == "create-user":
        if args.role in {"admin", "operator"} and not args.trusted:
            parser.error("创建可信执行角色需要 --trusted")
        admin = next((user for user in store.users() if user["enabled"] and user["role"] == "admin"), None)
        if admin is None:
            raise ValueError("请先创建管理员")
        result = store.create_user(args.username, password(), args.role, actor=admin["id"])
        store.audit("local-cli", "user.create.offline", result["id"])
    elif args.action == "reset-password":
        result = store.local_recover(args.username, password())
    elif args.action == "reset-mfa":
        result = store.local_recover(args.username, reset_mfa=True)
        result["next_step"] = "使用原密码登录，按页面重新扫码绑定验证器并保存恢复码；完成前不能进入工作区。"
    else:
        user = next((item for item in store.users() if item["username"] == username(args.username)), None)
        if user is None:
            raise ValueError("账号不存在")
        result = adopt_legacy(store, user, args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print("操作未完成：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
