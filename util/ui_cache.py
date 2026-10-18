"""H5 静态资源缓存策略。"""
from fastapi import Request


async def ui_cache_headers(request: Request, call_next):
    """HTML/未版本化文件每次校验；内容哈希 URL 可长期复用。"""
    response = await call_next(request)
    if request.url.path.startswith("/ui/") and (
        request.url.path.endswith(("/", ".html", ".js", ".css"))
    ):
        version = request.query_params.get("v", "")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if len(version) == 12 and all(char in "0123456789abcdef" for char in version)
            else "no-cache"
        )
    return response
