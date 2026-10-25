"""Offline managed-data maintenance. See docs/account_storage.md."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["reconcile", "backup", "verify", "restore", "migrate", "rollback-journal"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import env_manager
    env_manager.init_path(args.root)
    from security.settings import SecuritySettings
    from memory.identity_store import IdentityStore
    from memory import storage_backup as backup
    settings = SecuritySettings.from_env()
    import os
    from dataclasses import replace
    if not os.environ.get("YTOOLS_SECURITY_DIR", env_manager.load_var("YTOOLS_SECURITY_DIR", None)):
        settings = replace(settings, directory=args.root.resolve() / "history_files/state/security")
    if args.command in {"verify", "restore"}:
        if not args.archive or (args.command == "restore" and not args.target):
            parser.error("--archive required; restore also needs --target")
        result = backup.verify(args.archive) if args.command == "verify" else backup.restore(args.archive, args.target)
    else:
        with backup.maintenance(args.root, settings.origin):
            if args.command == "backup":
                if not args.archive:
                    parser.error("backup needs --archive outside history_files")
                result = backup.backup(args.root, settings.directory, args.archive)
            else:
                if args.command == "migrate" and args.apply:
                    if not args.archive:
                        parser.error("migrate --apply requires a verified full --archive")
                    backup.validate_migration_archive(args.archive, args.root, settings.directory)
                store = IdentityStore(settings)
                store.prepare()
                from memory.storage_layout import system_access
                with system_access(store, reason="offline " + args.command):
                    if args.command == "reconcile":
                        from memory.session_catalog import reconcile
                        result = reconcile(store, args.root / "history_files", apply=args.apply)
                    elif args.command == "migrate":
                        from memory.storage_migration import migrate
                        result = migrate(store, args.root / "history_files", apply=args.apply)
                    else:
                        if not args.journal:
                            parser.error("rollback-journal needs --journal")
                        from memory.storage_migration import rollback
                        rollback(store, args.root / "history_files", args.journal)
                        result = {"rolled_back": True}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
