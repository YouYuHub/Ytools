from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

# 加载 env 环境变量
from config import apply_persisted_work_dir, PROJECT_ROOT
from env_manager import init_path
init_path(PROJECT_ROOT)
apply_persisted_work_dir()

# 引入路由，需要在 init_path() 之后
from routers.chat_router import api_chat_router
from routers.chat_config_router import api_chat_config_router
from routers.tools_manage_router import api_tools_manage_router
from routers.file_router import api_file_router
from routers.prompt_router import api_prompt_router


# FastAPI 实例化
app = FastAPI(
    title="智能体工具使用测试",
    description="工具类测试",
    version="v0.1"
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    
    def patch_binary_format(schema_part):
        if isinstance(schema_part, dict):
            if schema_part.get("type") == "string" and schema_part.get("contentMediaType"):
                schema_part.setdefault("format", "binary")
            for value in schema_part.values():
                patch_binary_format(value)
        elif isinstance(schema_part, list):
            for item in schema_part:
                patch_binary_format(item)
                
    patch_binary_format(openapi_schema)
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
# 跨域设置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 允许所有来源
    allow_credentials=True, # 允许携带 cookie
    allow_methods=["*"], # 允许所有方法
    allow_headers=["*"], # 允许所有头部
)


@app.get("/", tags=["Root"])
async def root():
    """ 根路由 """
    return {"message": "欢迎使用大模型智能体工具接口"}


# 包含路由
app.include_router(api_chat_router, tags=["ChatLLM"])
app.include_router(api_chat_config_router, tags=["ChatConfig"])
app.include_router(api_tools_manage_router, tags=["ToolsManage"])
app.include_router(api_file_router, tags=["FileUpload"])
app.include_router(api_prompt_router, tags=["Skills"])


# ---------- 配置文件热重载（全局轮询线程，不依赖 uvicorn --reload） ----------
def _start_config_hot_reload() -> None:
    from util import config_watcher

    def _reload_mcp_servers(data: dict, meta: dict) -> None:
        # inputs / servers 变化都同步工具选择内存快照（服务集合可能增删）
        try:
            from routers import chat_config_router
            chat_config_router.sync_tool_selection_memory(data)
        except Exception as exc:
            print(f"[config-watch] 同步工具选择内存失败: {exc}")
        # 仅 servers 键变化才重新探测 MCP 工具（inputs 保存不应触发昂贵的工具发现）。
        # 工具发现要拉起 MCP 子进程，极端情况下可能卡住（如子进程握手挂起），
        # 放到临时线程执行并限时：超时放弃本次重探（临时线程为 daemon，随进程退出），
        # 保证热重载轮询线程本身永不冻结
        if meta.get("servers_changed"):
            import asyncio
            import threading
            from config import get_current_dir
            from factory.agent_runtime import tool_registry

            def _rediscover() -> None:
                try:
                    asyncio.run(tool_registry.refresh_tools_from_mcp(get_current_dir()))
                except Exception as exc:
                    print(f"[config-watch] mcp_servers.json 变更后重新发现工具失败: {exc}")

            rediscover_thread = threading.Thread(
                target=_rediscover, name="config-watch-mcp-refresh", daemon=True
            )
            rediscover_thread.start()
            rediscover_thread.join(timeout=60.0)
            if rediscover_thread.is_alive():
                print("[config-watch] MCP 工具重探超过 60s 未完成，已放弃等待（后台线程继续）")

    def _reload_models(_data: dict, _meta: dict) -> None:
        import env_manager as _env_manager
        if _env_manager.reload_models_config():
            selection = _env_manager.get_current_model_selection()
            print(
                f"[config-watch] models.json 内存已同步："
                f"chat_model={selection.get('provider')}/{selection.get('model')}"
            )

    config_watcher.register_reload_callback("mcp_servers", "tool_selection_memory", _reload_mcp_servers)
    config_watcher.register_reload_callback("models", "models_config", _reload_models)
    config_watcher.start_config_watcher()


_start_config_hot_reload()


# ---------- 工具注册表预热：启动即后台探测一次，首个页面加载/首条消息直接命中缓存 ----------
def _prewarm_tool_registry() -> None:
    import asyncio
    import threading
    from config import get_current_dir
    from factory.agent_runtime import tool_registry

    def _prewarm() -> None:
        try:
            asyncio.run(tool_registry.refresh_tools_from_mcp(get_current_dir()))
        except Exception as exc:
            print(f"[prewarm] MCP 工具预热失败（首个请求会现场重探）: {exc}")

    # 预热要拉起 MCP 子进程，可能耗时数秒，放后台线程避免阻塞服务启动；
    # 失败不致命——缓存缺失时 /tools/list 与发送路径会现场重探
    threading.Thread(target=_prewarm, name="tool-registry-prewarm", daemon=True).start()


_prewarm_tool_registry()



if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app='main:app',
        host="0.0.0.0", port=48621,
        reload=True, reload_dirs=[str(PROJECT_ROOT)],
        reload_excludes=[
            # "mcp_server/*",     # 排除 generated 子目录
            "mcp_server/**",
            "test/**",          # 递归排除 test 下所有层级
            "setting/**",
            "history_files/**",
            "docs/**",
            "H5/**",
            # "*.pyc",            # 也可按模式排除文件
            "*.md",
            "*.txt",
        ],
    )
