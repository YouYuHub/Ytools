from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

# 加载 env 环境变量
from load_env import init_path
init_path()

# 引入路由，需要在 init_path() 之后
from routers.chat_router import api_chat_router
from routers.tools_manage_router import api_tools_manage_router
from routers.file_router import api_file_router


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
app.include_router(api_chat_router, tags=["ChatTool"])
app.include_router(api_tools_manage_router, tags=["ToolsManage"])
app.include_router(api_file_router, tags=["FileUpload"])


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app='main:app', host="0.0.0.0", port=48621, reload=False)

