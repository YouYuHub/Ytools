"""Read raw upload bodies without FastAPI's multipart parser."""

from tempfile import SpooledTemporaryFile

from fastapi import HTTPException, Request


def require_raw_upload(request: Request, filename: str) -> str:
    name = (filename or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件名缺失")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise HTTPException(status_code=415, detail="请使用 application/octet-stream 上传文件")
    return name


async def read_upload(request: Request, max_bytes: int, label: str) -> bytes:
    """Bound memory use while reading a small raw upload."""
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail=f"{label}大小超过限制（最大{max_bytes // (1024 * 1024)}MB）")
        chunks.append(chunk)
    if not size:
        raise HTTPException(status_code=400, detail=f"上传的{label}为空")
    return b"".join(chunks)


async def spool_upload(request: Request, max_bytes: int, label: str):
    """Spool video and GIF uploads to disk before handing them to storage."""
    file = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail=f"{label}大小超过限制（最大{max_bytes // (1024 * 1024)}MB）")
            file.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail=f"上传的{label}为空")
        file.seek(0)
        return file
    except Exception:
        file.close()
        raise
