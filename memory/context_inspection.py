"""Bounded, read-only descriptions of the local context. No provider payloads."""
from datetime import datetime, timezone

from .chat_history_format import (
    normalize_context_summary, render_summary_block, _round_question,
    extract_recent_question_items,
)


def _excerpt(value, limit):
    text = str(value or "")
    return {"text": text[:limit], "truncated": len(text) > limit}


def inspect_history(meta, entries, pending, stats, *, offset=0, limit=40):
    rounds = [e for e in entries if isinstance(e, dict) and e.get("event") == "chat_round"]
    summary = normalize_context_summary(meta.get("context_summary"))
    covered = int(stats["rounds"]["summarized"])
    supplements = set(meta.get("context_summary_raw_rounds") or [])
    blocks = []
    all_blocks = (summary or {}).get("blocks", [])
    for index, block in enumerate(all_blocks[:50]):
        blocks.append({"index": index + 1, "round_start": block.get("round_start"),
            "round_end": block.get("round_end"), "repair_required": bool(block.get("repair_required")),
            "active": covered > 0 or bool(summary and not summary.get("source_round_count")),
            "preview": _excerpt(render_summary_block(block), 6000)})
    repair_ranges = [(b.get("round_start"), b.get("round_end")) for b in all_blocks if b.get("repair_required")]
    question_numbers = {number for number, _ in extract_recent_question_items(rounds[:covered])}
    rows = []
    total = len(rounds) + bool(pending)
    # Latest rounds first, page only small excerpts; never send tool traces.
    for position in range(total - 1 - offset, max(-1, total - 1 - offset - limit), -1):
        is_pending = position == len(rounds)
        entry = pending if is_pending else rounds[position]
        number = position + 1
        repair = any(isinstance(start, int) and isinstance(end, int) and start <= number <= end
            for start, end in repair_ranges)
        representation = ("active" if is_pending else "repair_pending" if repair else
            "supplement" if number in supplements and number <= covered else
            "summary" if number <= covered else "raw")
        rows.append({"round": number, "status": entry.get("status", ""),
            "representation": representation, "question_index": number in question_numbers and number not in supplements,
            "question": _excerpt(_round_question(entry), 240), "started_at": entry.get("started_at")})
    events = [e for e in entries if isinstance(e, dict) and
        e.get("event") in {"context_compaction", "responses_compaction"}]
    if pending:
        events.extend(e for e in pending.get("events", []) if isinstance(e, dict) and
            e.get("event") in {"context_compaction", "responses_compaction"})
    # Completed-round compaction events belong to that round too.
    for entry in rounds:
        events.extend(e for e in entry.get("events", []) if isinstance(e, dict) and
            e.get("event") in {"context_compaction", "responses_compaction"})
    events.sort(key=lambda e: str(e.get("timestamp") or e.get("created_at") or ""))
    allowed = ("event", "scope", "phase", "timestamp", "trigger_reason", "before_tokens", "after_tokens",
        "trigger_threshold", "trigger_context_tokens", "target_tokens", "token_limit", "budget_scope")
    recent = [{**{key: event[key] for key in allowed if key in event},
        **({"error": str(event["error"])[:500]} if event.get("error") else {})}
        for event in reversed(events[-12:])]
    return {"captured_at": datetime.now(timezone.utc).isoformat(),
        "basis": "next_request_local_preview", "pending_round": bool(pending),
        "repair_required": bool(repair_ranges), "repair_block_count": len(repair_ranges), "summary_blocks": blocks,
        "summary_block_count": len(all_blocks), "summary_blocks_truncated": len(all_blocks) > len(blocks),
        "round_page": {"offset": offset, "limit": limit, "total": total,
            "has_more": offset + len(rows) < total, "items": rows},
        "recent_compactions": recent,
        "notes": ["预览按当前已保存的模型配置和本地历史组装；输入框中尚未发送的内容、附件及运行中未落盘的增量不包含在内。",
            "已发送请求使用任务启动时的配置；修改配置影响后续任务。",
            "待重建摘要的当前体积不是修复后的请求体积；聊天请求会先完成摘要重建。"]}


def inspect_configuration(session_id, config, parameters):
    from chat.protocols import normalize_api_type, responses_store
    from factory.agent_runtime.context_compaction import (
        load_context_compaction_settings, resolve_effective_window,
        resolve_context_compaction_threshold, resolve_history_target_tokens,
        resolve_effective_summary_budget,
    )
    settings = load_context_compaction_settings()
    protocol = normalize_api_type(config.get("apiType"))
    mode = (parameters.get("responses_mode") or config.get("responsesMode") or "stateful") if protocol == "responses" else "stateless"
    result = {"model": {"provider": config.get("selected_provider_name"),
        "name": config.get("selected_model_name") or config.get("selected_model_id"),
        "protocol": protocol, "mode": mode},
        "policy": {"trigger_ratio": settings.trigger_ratio,
            "effective_window": resolve_effective_window(settings),
            "trigger_threshold": resolve_context_compaction_threshold(settings),
            "target_tokens": resolve_history_target_tokens(settings),
            "summary_budget": resolve_effective_summary_budget(settings)},
        "remote": {"applicable": protocol == "responses" and mode == "stateful"}}
    if not result["remote"]["applicable"]:
        return result
    try:
        state = responses_store.load_state(session_id)
        compatible = (not state.get("source_key") or state["source_key"] == responses_store.source_key(config)) and state.get("responses_mode", "stateful") == mode
        remote = result["remote"]
        remote.update(status=state.get("status", "new"), compatible=compatible,
            has_remote_head=bool(state.get("head_response_id")),
            has_compacted_window=bool(state.get("pending_compaction_ref")),
            age_risk=responses_store.public_state(state).get("age_risk", False))
        if state.get("error"):
            remote["message"] = str(state["error"])[:500]
        compacted = state.get("last_compaction") or {}
        remote["compaction"] = {key: compacted[key] for key in
            ("phase", "mode", "activation", "local_phase", "created_at", "input_tokens_after") if key in compacted}
        if state.get("head_record_id"):
            record = responses_store.load_record(session_id, {"protocol": "responses", "record_id": state["head_record_id"]}) or {}
            response = record.get("response") or {}
            usage = response.get("usage") or {}
            remote["last_usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                if isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool)}
            if (compatible and "input_tokens" in remote["last_usage"] and not state.get("pending_compaction_ref")
                    and not any(item.get("type") == "compaction" for item in response.get("output", []))):
                remote["chain_tokens_estimate"] = usage["input_tokens"] + usage.get("output_tokens", 0)
        # Opaque ciphertext is never tokenized or returned as the remote context size.
    except Exception:
        result["remote"]["error"] = "远端状态读取失败；本地预览仍可查看。"
    return result
