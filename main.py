if __name__ == "__main__":
    # Import the ASGI module only once; validation runs before runtime watchers.
    from scripts.serve import main as serve_main
    serve_main()
    raise SystemExit(0)

import asyncio
import threading

# 加载 env 环境变量
from config import (
    apply_persisted_work_dir,
    get_current_dir,
    PROJECT_ROOT,
)
from util import config_watcher, ollama_model_watcher
from factory.agent_runtime import tool_registry
from env_manager import init_path

from util.logger import get_logger, setup_logging

logger = get_logger("main")

init_path(PROJECT_ROOT)
# 启动日志系统：init_path 之后才能读到 .env 的 LOG_LEVEL（后端启动日志等级配置项）
setup_logging(force=True)
apply_persisted_work_dir()
# 初始化 env 路径后才能正确加载到变量
from config import DEFAULT_SERVICE_HOST, DEFAULT_SERVICE_PORT

from routers.chat_config_router import sync_tool_selection_memory


from app_factory import create_app

app = create_app()


@app.on_event("startup")
async def permission_ui_startup():
    from memory.permission_store import prepare_database
    await asyncio.to_thread(prepare_database)
    from memory.session_listing import recover_pending_sessions

    def recover():
        from memory.storage_layout import system_access
        with system_access(app.state.identity_store, reason="startup checkpoint recovery"):
            recover_pending_sessions(app.state.history_recovery_stop)

    app.state.history_recovery_stop = threading.Event()
    app.state.history_recovery_thread = threading.Thread(
        target=recover,
        name="history-checkpoint-recovery", daemon=True,
    )
    app.state.history_recovery_thread.start()
    print(f"聊天页面：{app.state.identity_store.settings.origin}/ui/", flush=True)


@app.on_event("shutdown")
async def permission_ui_shutdown():
    recovery_stop = getattr(app.state, "history_recovery_stop", None)
    if recovery_stop is not None:
        recovery_stop.set()
        await asyncio.to_thread(app.state.history_recovery_thread.join, 1)
    from factory.session_worker import _WORKER_PROXIES
    from memory.permission_store import suspend_session
    for session, proxy in list(_WORKER_PROXIES.items()):
        suspend_session(session)
        await asyncio.to_thread(proxy.shutdown)
    from factory.chat_factory import _SESSION_STREAMS
    for session, stream in list(_SESSION_STREAMS.items()):
        if stream.task is not None and not stream.task.done():
            stream.permission_suspend = True
            suspend_session(session)
            stream.task.cancel()
            try:
                await stream.task
            except (asyncio.CancelledError, Exception):
                pass
    from chat.http_pool import close_idle_connections
    close_idle_connections()


# ---------- 配置文件热重载（全局轮询线程，不依赖 uvicorn --reload） ----------
def _start_config_hot_reload() -> None:
    import env_manager as _env_manager

    def _reload_mcp_servers(data: dict, meta: dict) -> None:
        # inputs / servers 变化都同步工具选择内存快照（服务集合可能增删）
        try:
            sync_tool_selection_memory(data)
        except Exception as exc:
            logger.warning(f"[config-watch] 同步工具选择内存失败: {exc}")

        def _rediscover() -> None:
            try:
                asyncio.run(tool_registry.refresh_tools_from_mcp(get_current_dir()))
            except Exception as exc:
                logger.warning(f"[config-watch] mcp_servers.json 变更后重新发现工具失败: {exc}")

        # 仅 servers 键变化才重新探测 MCP 工具（inputs 保存不应触发昂贵的工具发现）。
        # 工具发现要拉起 MCP 子进程，极端情况下可能卡住（如子进程握手挂起），
        # 放到临时线程执行并限时：超时放弃本次重探（临时线程为 daemon，随进程退出），
        # 保证热重载轮询线程本身永不冻结
        if meta.get("servers_changed"):
            rediscover_thread = threading.Thread(
                target=_rediscover, name="config-watch-mcp-refresh", daemon=True
            )
            rediscover_thread.start()
            rediscover_thread.join(timeout=60.0)
            if rediscover_thread.is_alive():
                logger.warning("[config-watch] MCP 工具重探超过 60s 未完成，已放弃等待（后台线程继续）")

    def _reload_models(_data: dict, _meta: dict) -> None:
        if _env_manager.reload_models_config():
            selection = _env_manager.get_current_model_selection()
            logger.info(f"[config-watch] models.json 内存已同步："
                f"chat_model={selection.get('provider')}/{selection.get('model')}")

    def _reload_env(_data: dict, _meta: dict) -> None:
        if _env_manager.reload_env_vars():
            logger.info("[config-watch] .env 内存已同步（load_var 类配置热生效，如 VIDEO_MAX_READ_SECONDS）")

    config_watcher.register_reload_callback("mcp_servers", "tool_selection_memory", _reload_mcp_servers)
    config_watcher.register_reload_callback("models", "models_config", _reload_models)
    config_watcher.register_reload_callback("env", "env_vars", _reload_env)
    config_watcher.start_config_watcher()


_start_config_hot_reload()

# ---------- Ollama 本地模型发现（启动立即检查，之后默认每 30 秒轮询） ----------
ollama_model_watcher.start_ollama_model_watcher()


# ---------- 工具注册表预热：启动即后台探测一次，首个页面加载/首条消息直接命中缓存 ----------
def _prewarm_tool_registry() -> None:
    from config import get_current_dir
    from factory.agent_runtime.tool_registry import refresh_tools_from_mcp

    def _prewarm() -> None:
        try:
            asyncio.run(refresh_tools_from_mcp(get_current_dir()))
        except Exception as exc:
            logger.warning(f"[prewarm] MCP 工具预热失败（首个请求会现场重探）: {exc}")

    # 预热要拉起 MCP 子进程，可能耗时数秒，放后台线程避免阻塞服务启动；
    # 失败不致命——缓存缺失时 /tools/list 与发送路径会现场重探
    threading.Thread(target=_prewarm, name="tool-registry-prewarm", daemon=True).start()

_prewarm_tool_registry()



if __name__ == '__main__':
    import uvicorn
    from pathlib import Path

    setup_file = Path(__file__).stem
    uvicorn.run(app=f'{setup_file}:app',
        host=DEFAULT_SERVICE_HOST, port=DEFAULT_SERVICE_PORT,
        timeout_graceful_shutdown=5,
    )
