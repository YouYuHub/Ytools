"""Validated single-process server entry point for a local TLS reverse proxy."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from env_manager import init_path
    init_path(ROOT)
    from security.settings import SecuritySettings
    from config import DEFAULT_SERVICE_HOST, DEFAULT_SERVICE_PORT
    settings = SecuritySettings.from_env()
    proxies = os.environ.get("YTOOLS_PROXY_IPS", "127.0.0.1,::1")
    if "*" in proxies:
        raise ValueError("不能信任任意来源的代理请求头，请明确列出本机代理 IP")
    if settings.production and DEFAULT_SERVICE_HOST not in {"127.0.0.1", "::1"}:
        raise ValueError("此部署入口要求后端仅监听回环地址，通过本机 HTTPS 代理访问")
    import uvicorn
    uvicorn.run("main:app", host=DEFAULT_SERVICE_HOST, port=DEFAULT_SERVICE_PORT,
                workers=1, proxy_headers=settings.production,
                forwarded_allow_ips=proxies if settings.production else "",
                access_log=False, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
