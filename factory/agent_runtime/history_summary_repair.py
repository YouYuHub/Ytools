"""Repair only summary blocks affected by history edits, before any model replay."""
import asyncio
import copy


async def repair_history_summary_if_needed(memory, request, *, settings=None, event_emitter=None):
    from . import context_compaction as compact
    from memory.chat_history_format import (
        normalize_context_summary, round_entry_to_compaction_text, parse_summary_sections,
    )

    snapshot = getattr(memory, "get_summary_repair_snapshot", None)
    snapshot = snapshot() if callable(snapshot) else None
    if snapshot is None:
        return False
    original, rounds = snapshot
    settings = settings or compact.load_context_compaction_settings()
    total_budget = compact.resolve_effective_summary_budget(settings)
    repaired = copy.deepcopy(original)
    usage = {}

    async def emit(phase, **payload):
        if event_emitter is not None:
            await event_emitter({"event": "context_compaction", "scope": "session",
                "role": "assistant", "phase": phase, "trigger_reason": "history_mutation",
                **payload})

    await emit("start", context_summary="历史已编辑或删除，正在重建受影响的摘要；其余摘要保留。")
    try:
        for index, block in enumerate(original["blocks"]):
            if not block.get("repair_required"):
                continue
            start, end = block["round_start"], block["round_end"]
            if not 1 <= start <= end <= len(rounds):
                raise compact.ContextCompactionError("待重建摘要范围与现有历史不一致")
            source = "\n\n".join(
                f"【保留历史第 {n} 轮】\n{round_entry_to_compaction_text(rounds[n - 1])}"
                for n in range(start, end + 1)
            )
            # 原摘要预算约束每个修复块，长来源由公共压缩器完整分段处理。
            # 禁止用旧摘要作为输入，避免已删除事实或旧编辑内容重新混入。
            budget = min(total_budget, max(1024, int(block.get("repair_token_budget", 1024))))

            async def delta(payload):
                await emit("delta", **payload)

            result = await compact.summarize_context_text(
                source, request, scope="历史修改后的摘要重建", settings=settings,
                output_token_budget=budget, segment_output_limit=budget,
                delta_emitter=delta if event_emitter else None,
            )
            compact._merge_usage_dict(usage, result.usage or {})
            if result.usage:
                await memory.add_history_compression_usage(result.usage)
            if not result.text.strip() or result.source_was_truncated:
                raise compact.ContextCompactionError("摘要重建未完整处理保留历史，请重试")
            repaired["blocks"][index] = {
                **parse_summary_sections(result.text), "round_start": start, "round_end": end,
            }
        repaired = normalize_context_summary(repaired)
        await memory.commit_summary_repair(original, rounds, repaired)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await emit("aborted", reason="summary_repair_failed", error=str(exc)[:500])
        raise compact.ContextCompactionError(f"历史摘要重建失败，未发送旧摘要或完整原文：{exc}") from exc
    await emit("done", summary=repaired,
        summary_text=compact.render_context_summary(repaired),
        summary_usage=usage, after_tokens=compact.estimate_text_tokens(compact.render_context_summary(repaired) or ""))
    return True
