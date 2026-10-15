"""Coordinate readable local summaries with a separately retained native window."""
import asyncio
import inspect

from chat.protocols import normalize_api_type, responses_store
from chat.responses_client import compact, remote_compaction_needed
from config import ChatLLMRequest


async def coordinate(local_call, request, config, *, emitter=None, manual=False, manager=None):
    scope = (request.responses_context or {}).get("scope") or "chat_model"
    if normalize_api_type(config.get("apiType")) != "responses":
        return await local_call(emitter)
    state = responses_store.load_state(request.session_id, scope)
    if not state.get("head_response_id"):
        return await local_call(emitter)  # Existing history initialization, no remote chain yet.
    frozen = request.model_copy(deep=True)
    from .responses_context import pending_native_input
    pending_items, pending_ids = pending_native_input(frozen) if frozen.messages else ([], [])
    remote_pressure = remote_compaction_needed(frozen, config, state, pending_items)
    remote_task = None
    local_error = None
    remote_error = None
    result = None
    lease_acquired = False

    async def emit_remote(phase, error=None):
        if emitter:
            value = emitter({"event": "responses_compaction", "scope": "session",
                             "phase": phase, "error": str(error) if error else None,
                             "message": "官方压缩窗口已生成，待下次请求启用" if phase == "done" else "官方上下文压缩"})
            if inspect.isawaitable(value):
                await value

    async def remote():
        nonlocal lease_acquired
        responses_store.mark_compacting(request.session_id, scope, True)
        lease_acquired = True
        await emit_remote("start")
        reused = bool(responses_store.reusable_compaction_ref(state, has_pending_input=bool(pending_items)))
        reference = await compact(frozen, config, scope=scope,
                                  stop=(request.responses_context or {}).get("stop_checker"))
        if manager is not None:
            updated = responses_store.load_state(request.session_id, scope)
            native_usage = (updated.get("last_compaction") or {}).get("usage") or {}
            if native_usage and (updated.get("last_compaction") or {}).get("record_ref") == reference:
                usage = {"prompt_tokens": native_usage.get("input_tokens", 0),
                         "completion_tokens": native_usage.get("output_tokens", 0),
                         "total_tokens": native_usage.get("total_tokens", 0),
                         "completion_tokens_details": native_usage.get("output_tokens_details") or {},
                         "prompt_tokens_details": native_usage.get("input_tokens_details") or {}}
                # Record once, including when only the local half is retried.
                if not reused:
                    await manager.add_history_compression_usage(usage)
        await emit_remote("done")
        return reference

    async def emit(payload):
        nonlocal remote_task
        if payload.get("phase") == "start" and remote_task is None:
            remote_task = asyncio.create_task(remote())
        if emitter:
            value = emitter(payload)
            if inspect.isawaitable(value):
                await value

    try:
        result = await local_call(emit)
    except asyncio.CancelledError:
        if remote_task:
            remote_task.cancel()
            await asyncio.gather(remote_task, return_exceptions=True)
        if lease_acquired:
            responses_store.mark_compacting(request.session_id, scope, False)
        raise
    except Exception as exc:
        local_error = exc
    if remote_task is None and (manual or remote_pressure) and not local_error:
        remote_task = asyncio.create_task(remote())
    if remote_task is not None:
        try:
            try:
                reference = await remote_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                remote_error = exc
                await emit_remote("error", exc)
            responses_store.set_compaction_local_status(
                request.session_id, scope, "error" if local_error else "done", local_error)
            if request.responses_context is not None:
                if remote_error:
                    # A readable summary may have replaced unsent messages.
                    request.responses_context["deferred_items"] = pending_items
                    request.responses_context["deferred_ids"] = pending_ids
                else:
                    from .responses_context import committed
                    covered_ids = responses_store.compaction_input_ids(request.session_id, reference)
                    committed(request, covered_ids)
                    # Reusing a staged window cannot consume later inputs.
                    remaining_items, remaining_ids = pending_native_input(frozen, exclude_ids=covered_ids)
                    if remaining_ids:
                        request.responses_context["deferred_items"] = remaining_items
                        request.responses_context["deferred_ids"] = remaining_ids
        finally:
            if lease_acquired:
                responses_store.mark_compacting(request.session_id, scope, False)
    if local_error:
        raise local_error
    if remote_error:
        # The readable summary already exists; retain it and report partial success.
        # The old head remains effective until an explicit recovery/new native window.
        if emitter:
            value = emitter({"warning": {"code": "RESPONSES_COMPACTION_PARTIAL",
                "message": "本地摘要已保留，官方压缩失败：" + str(remote_error)}})
            if inspect.isawaitable(value):
                await value
    return result
