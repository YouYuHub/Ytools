"""Explicit server security settings; never infer trust from a proxy's IP."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SecuritySettings:
    directory: Path
    origin: str = "http://127.0.0.1:48621"
    production: bool = False
    require_mfa: bool = True
    idle_seconds: int = 1800
    absolute_seconds: int = 43200
    body_bytes: int = 8 * 1024 * 1024
    upload_bytes: int = 600 * 1024 * 1024
    trusted_operators: bool = False
    allowed_media_origins: tuple = ()

    @classmethod
    def from_env(cls):
        from config import PROJECT_ROOT
        from env_manager import load_var
        def value(name, default):
            return os.environ.get(name, load_var(name, default))
        production = str(value("YTOOLS_DEPLOYMENT", "local")).lower() == "production"
        origin = str(value("YTOOLS_PUBLIC_ORIGIN", "http://127.0.0.1:48621")).rstrip("/")
        parts = urlsplit(origin)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment):
            raise ValueError("YTOOLS_PUBLIC_ORIGIN 必须是完整的 http(s) origin")
        if production and parts.scheme != "https":
            raise ValueError("生产部署的 YTOOLS_PUBLIC_ORIGIN 必须使用 HTTPS")
        if not production and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程访问必须设置 YTOOLS_DEPLOYMENT=production 并配置 HTTPS origin")
        directory = Path(str(value("YTOOLS_SECURITY_DIR", PROJECT_ROOT / "history_files" / "state" / "security"))).expanduser().resolve()
        allowed = tuple(item.strip().rstrip("/") for item in str(value("YTOOLS_ALLOWED_MEDIA_ORIGINS", "")).split(",") if item.strip())
        for item in allowed:
            entry = urlsplit(item)
            if entry.scheme not in {"http", "https"} or not entry.hostname or entry.username or entry.password or entry.path or entry.query or entry.fragment:
                raise ValueError("YTOOLS_ALLOWED_MEDIA_ORIGINS 只能列出精确 http(s) origin")
        return cls(directory=directory, origin=origin, production=production, allowed_media_origins=allowed,
                   trusted_operators=str(value("YTOOLS_TRUSTED_OPERATORS", "false")).lower() == "true")

    @property
    def cookie_name(self):
        return "__Host-ytools_session" if self.production else "ytools_local_session"

    @property
    def host(self):
        return urlsplit(self.origin).hostname
