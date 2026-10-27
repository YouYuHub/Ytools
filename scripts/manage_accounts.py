"""Offline account bootstrap and recovery."""
import argparse
import getpass
import json
import os
import secrets
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




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-admin", "create-user", "list", "reset-password", "reset-mfa"])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=["admin", "operator", "member"], default="member")
    parser.add_argument("--trusted", action="store_true", help="确认管理员/操作员属于可信实例操作者")
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
        parser.error("未知操作")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print("操作未完成：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
