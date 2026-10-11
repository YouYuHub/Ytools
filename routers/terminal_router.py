"""Session-scoped background command management; jobs are created by run_command."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from factory.agent_runtime import command_jobs

api_terminal_router = APIRouter(prefix="/terminal")


@api_terminal_router.get("/jobs")
def list_jobs(session_id: str = Query(..., min_length=1)):
    return {"jobs": command_jobs.list_jobs(session_id, active_only=True)}


@api_terminal_router.get("/jobs/{job_id}")
def get_job(job_id: str, session_id: str = Query(..., min_length=1)):
    try:
        return command_jobs.action(job_id, "poll", session_id, tail_lines=2000, max_chars=131072)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@api_terminal_router.post("/jobs/{job_id}/{operation}")
def operate_job(job_id: str, operation: str, session_id: str = Query(..., min_length=1)):
    if operation not in ("stop", "close"):
        raise HTTPException(400, "仅支持 stop/close")
    try:
        return command_jobs.action(job_id, operation, session_id)
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api_terminal_router.get("/jobs/{job_id}/log/download")
def download_log(job_id: str, session_id: str = Query(..., min_length=1)):
    try:
        _, meta = command_jobs.read_job(job_id, session_id)
        from pathlib import Path
        path = Path(meta["output_path"])
        if not path.is_file():
            raise ValueError("日志文件尚未创建")
        return FileResponse(path, media_type="application/octet-stream", filename=f"{job_id}.log")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
