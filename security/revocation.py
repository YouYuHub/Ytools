"""Best-effort shutdown of known jobs after explicit account disable/demotion.

Only recorded process identities may be signalled; never guess a PID. Previously
completed external effects and independently detached processes cannot be undone.
"""
import asyncio
from security.principal import CURRENT, Principal


async def stop_known_jobs(store, identifier):
    user = await asyncio.to_thread(store.user, identifier)
    if not user:
        return {"stopped": 0, "needs_attention": 0}
    token = CURRENT.set(Principal(identifier, user["username"], user["role"], "", 0, True, "", store))
    stopped = attention = 0
    try:
        from routers.chat_router import _session_generation_busy
        from factory.chat_factory import stop_chat_task
        for session in await asyncio.to_thread(store.owned, "session", identifier):
            if _session_generation_busy(session):
                try:
                    await asyncio.wait_for(stop_chat_task(session), timeout=10)
                    stopped += 1
                except (Exception, asyncio.TimeoutError):
                    attention += 1
        from factory.agent_runtime import command_jobs
        for job in await asyncio.to_thread(command_jobs.list_jobs, active_only=True):
            try:
                await asyncio.to_thread(command_jobs.action, job["job_id"], "stop", job["session_id"])
                stopped += 1
            except Exception:
                attention += 1
    finally:
        CURRENT.reset(token)
    await asyncio.to_thread(store.audit, "account-service", "account.jobs_stop", identifier,
                            stopped=stopped, needs_attention=attention)
    return {"stopped": stopped, "needs_attention": attention}
