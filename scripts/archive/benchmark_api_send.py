"""Offline send-preparation comparison; never contacts a provider.

The reference reconstructs the former character-count/validation flow. Timings
exclude imports, model inference and real network/DB latency. History writes use
a temporary directory only. Run from the project root with Python.
"""
import asyncio
import json
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat import chat_llm, messages_client
from chat.chat_llm import ChatLLM
from chat.protocols.messages import build_payload
from chat.protocols.messages_content import validate_payload
from config import ChatLLMRequest
from factory import chat_factory
from factory.agent_runtime import chat_runtime as runtime
from memory import chat_memory
from util.token_estimate import text_token_estimate


def reference_count(text):
    if not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    return max(1, ascii_count // 4 + len(text) - ascii_count)


def median_ms(invoke, repeats=5):
    invoke()
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        invoke()
        values.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(values), 3)


def messages(count):
    result = [{"role": "user", "content": "Continue with synthetic tool results"}]
    text = ("0123456789abcdef 中文结果\n" * 1100)[:20000]
    for i in range(count):
        result.extend([
            {"role": "assistant", "content": "", "reasoning_content": "synthetic reasoning " * 125,
             "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "work", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": text},
        ])
    return result


def common_prepare(source, legacy):
    runtime.estimate_send_context_tokens(source, [])
    runtime.estimate_send_context_tokens(source, [])
    if legacy:
        chat_factory._messages_debug_summary(source)
    request = runtime.build_request_messages(runtime.copy_for_request(source))
    plain = [message.model_dump(exclude_none=True) for message in request]
    runtime.estimate_request_context_tokens(plain, [])
    payload = {"messages": plain, "max_tokens": 4096, "model": "offline"}
    chat_llm._estimate_payload_input_tokens(payload)
    return json.dumps(payload, ensure_ascii=False).encode()


def legacy_precheck(payload, config):
    error = ChatLLM._precheck_hard_limit(payload)
    if error:
        raise ValueError(error)
    estimated = chat_llm._estimate_payload_input_tokens(payload)
    if estimated >= config["maxInputTokens"]:
        raise ValueError("synthetic fixture too large")
    payload["max_tokens"] = min(payload["max_tokens"], config["maxInputTokens"] - estimated)


def adapter_prepare(request, config, legacy):
    if legacy:
        payload, _, _ = build_payload(request, config, True)
        legacy_precheck(payload, config)
        legacy_precheck(payload, config)
        validate_payload(payload, config)
        return json.dumps(payload, ensure_ascii=False).encode()
    payload, _, _ = messages_client._prepare(ChatLLM, request, config, True)
    messages_client._precheck(ChatLLM, payload, config)
    return messages_client._wire(config, payload)[2].split(b"\r\n\r\n", 1)[1]


def with_reference():
    stack = ExitStack()
    stack.enter_context(patch.object(runtime, "text_token_estimate", reference_count))
    stack.enter_context(patch.object(chat_llm, "text_token_estimate", reference_count))
    return stack


def history_probe():
    with tempfile.TemporaryDirectory(prefix="api-send-benchmark-") as directory:
        root = Path(directory)
        with patch.object(chat_memory, "HISTORY_ROOT", root), patch.object(chat_memory, "LOCK_ROOT", root / "locks"):
            path = chat_memory._get_chat_history_file("synthetic")
            entries = [{"event": "chat_round", "question": f"synthetic-{i}", "status": "completed",
                        "events": [{"role": "tool", "content": "text " * 8000}]} for i in range(200)]
            chat_memory._write_meta_and_entries(path, chat_memory._default_meta("synthetic"), entries)
            counter = [0]
            def write(legacy):
                counter[0] += 1
                state = {"status": "request_pending", "pending_request_ref": str(counter[0])}
                if legacy:
                    meta, loaded = chat_memory._load_meta_and_entries(path, "synthetic")
                    meta["responses_state"] = state
                    chat_memory._write_meta_and_entries(path, meta, loaded)
                else:
                    chat_memory.update_responses_session_meta("synthetic", state)
            before = median_ms(lambda: write(True), 3)
            raw = path.read_bytes()
            after = median_ms(lambda: write(False), 3)
            assert path.read_bytes() == raw
            return {"history_mib": round(len(raw) / 1048576, 3), "reference_full_rewrite_ms": before,
                    "optimized_sidecar_ms": after, "history_bytes_unchanged": True}


async def first_frame_probe():
    async def measure(legacy):
        first, last = b"data: first\n\n", b"data: last\n\n"
        reader = asyncio.StreamReader()
        reader.feed_data(f"{len(first + last):x}\r\n".encode() + first)
        async def finish():
            await asyncio.sleep(0.15)
            reader.feed_data(last + b"\r\n0\r\n\r\n")
            reader.feed_eof()
        task = asyncio.create_task(finish())
        started = time.perf_counter()
        if legacy:
            size = int((await reader.readline()).strip(), 16)
            await reader.readexactly(size)
        else:
            lines = ChatLLM._aiter_sse_body_lines(reader, True, 1)
            assert await anext(lines) == b"data: first"
            await lines.aclose()
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        await task
        return elapsed
    return {"remaining_chunk_delay_ms": 150, "reference_ms": await measure(True), "optimized_ms": await measure(False)}


def main():
    config = {"url": "http://offline.invalid/v1", "apiType": "messages", "selected_model_id": "offline",
              "maxInputTokens": 1048576}
    result = {"scope": "offline reference-flow comparison; no provider/network; medians of 5 runs", "cases": []}
    with patch.object(chat_llm, "_resolve_request_hard_limit_tokens", lambda: 1048576):
        for count in (10, 60):
            source = messages(count)
            request = ChatLLMRequest(messages=source, max_tokens=4096)
            with with_reference():
                reference = common_prepare(source, True)
                before = median_ms(lambda: common_prepare(source, True))
                adapter_reference = adapter_prepare(request, config, True)
                adapter_before = median_ms(lambda: adapter_prepare(request, config, True))
            assert common_prepare(source, False) == reference
            assert adapter_prepare(request, config, False) == adapter_reference
            result["cases"].append({"tool_exchanges": count, "estimated_tokens": runtime.estimate_send_context_tokens(source, []),
                "reference_common_ms": before, "optimized_common_ms": median_ms(lambda: common_prepare(source, False)),
                "reference_messages_adapter_ms": adapter_before,
                "optimized_messages_adapter_ms": median_ms(lambda: adapter_prepare(request, config, False)),
                "identical_body": True})
    result["history"] = history_probe()
    result["first_frame"] = asyncio.run(first_frame_probe())
    output = ROOT / "tmp" / "api-send-perf-after.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
