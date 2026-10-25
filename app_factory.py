"""Build HTTP routes without starting watchers, MCP processes or recovery jobs."""
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def create_app(*, security_settings=None):
    from config import PROJECT_ROOT
    from routers.chat_router import api_chat_router
    from routers.chat_config_router import api_chat_config_router
    from routers.task_cost_router import api_task_cost_router
    from routers.task_template_router import api_task_template_router
    from routers.tools_manage_router import api_tools_manage_router
    from routers.file_router import api_file_router
    from routers.prompt_router import api_prompt_router
    from routers.export_router import api_export_router
    from routers.file_history_router import api_file_history_router
    from routers.user_profile_router import api_user_profile_router
    from routers.speech_config_router import api_speech_config_router
    from routers.terminal_router import api_terminal_router
    from routers.tool_approval_router import api_permission_router
    from security.app import install_security
    from security.static import AccountStaticFiles
    from util.ui_cache import ui_cache_headers

    app = FastAPI(title="Ytools", description="账号保护的智能体工作区", version="0.2")
    app.middleware("http")(ui_cache_headers)
    for router, tag in (
        (api_chat_router, "ChatLLM"), (api_task_cost_router, "TaskCosts"),
        (api_task_template_router, "TaskTemplates"), (api_chat_config_router, "ChatConfig"),
        (api_tools_manage_router, "ToolsManage"), (api_file_router, "Files"),
        (api_prompt_router, "Skills"), (api_export_router, "Export"),
        (api_file_history_router, "FileDiffHistory"), (api_user_profile_router, "UserProfile"),
        (api_speech_config_router, "ChatConfig"), (api_terminal_router, "Terminal"),
        (api_permission_router, "Permissions"),
    ):
        app.include_router(router, tags=[tag])

    @app.get("/")
    async def root():
        return {"service": "Ytools", "ui": "/ui/", "authentication": "required"}

    app.mount("/ui", AccountStaticFiles(directory=str(PROJECT_ROOT / "H5"), html=True), name="ui")
    install_security(app, security_settings)

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            def patch(value):
                if isinstance(value, dict):
                    if value.get("type") == "string" and value.get("contentMediaType"):
                        value.setdefault("format", "binary")
                    for child in value.values():
                        patch(child)
                elif isinstance(value, list):
                    for child in value:
                        patch(child)
            patch(schema)
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = openapi
    return app
