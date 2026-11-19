"""Transient full-task SSE replay; hot frames remain in the bounded stream deque."""
import json
import tempfile

# 紧凑快照：单合并帧载荷上限（字符数）。正常分片远小于此值，上限只在
# 极端长文本时把一个 run 切成几帧，避免合成病态巨帧。
COMPACT_MERGED_FRAME_CHARS = 256 * 1024

_MERGEABLE_TEXT_KEYS = ("content", "reasoning_content")

# 子任务 delta 帧可合并的键集合：超出此集合（如 usage/approval）视为结构帧。
_SUB_DELTA_ALLOWED_KEYS = frozenset({
    "event", "phase", "agent_id", "seq", "parent_tool_call_id", "round",
    "timestamp", "content_delta", "reasoning_delta",
})
_SUB_DELTA_CHANNELS = ("content_delta", "reasoning_delta")


def _parse_data_chunk(chunk):
    """解析 `data: {...}` 帧载荷；非 data 行/解析失败返回 None（调用方原样透传）。"""
    if not isinstance(chunk, str):
        return None
    text = chunk.strip()
    if not text.startswith("data:"):
        return None
    try:
        payload = json.loads(text[len("data:"):].strip())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _mergeable_channel(payload):
    """判定分片帧是否可合并：键集合 ⊆ {id, 通道} 且通道值非空。

    id 是上游 completion 标识（内容帧上仅具信息意义，用量去重只看 usage
    帧自带 id），合并时保留 run 内最后一个 id。任何其他键（usage、
    tool_start/return、round_started、compaction、sub_agent、ask_user、
    warning、error、finish_reason……）都视为结构帧，打断合并。
    返回 (channel, value, frame_id) 或 None。
    """
    if not isinstance(payload, dict):
        return None
    channel = None
    for key in payload:
        if key == "id":
            continue
        if key in _MERGEABLE_TEXT_KEYS or key == "tool_calls":
            if channel is not None:
                return None
            channel = key
        else:
            return None
    if channel is None:
        return None
    value = payload[channel]
    if channel == "tool_calls":
        if not isinstance(value, list):
            return None
    elif not isinstance(value, str):
        return None
    # 空值（空字符串/空数组）由调用方直接丢弃：前后端本来就忽略它们，
    # 且不打断 run（语义上是纯 no-op）。
    return channel, value, payload.get("id")


def _mergeable_sub_delta(payload):
    """判定子任务 delta 帧是否可合并：返回 (run_key, channel, value) 或 None。

    长任务里子任务思考/正文分片是帧数大头（每个子任务每轮几十帧），此前作为
    结构帧 1:1 透传。同一 agent_id + seq + 单通道的连续 delta 按文本拼接合并，
    与前端 appendLiveDelta 逐帧追加的结果完全一致（轮归属/通道/顺序不变）。
    双通道同帧、缺 agent_id、空增量一律不合并（原样透传，保留 autoOpen 等副作用）。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("event") != "sub_agent" or payload.get("phase") != "delta":
        return None
    if not isinstance(payload.get("agent_id"), str):
        return None
    for key in payload:
        if key not in _SUB_DELTA_ALLOWED_KEYS:
            return None
    channel = None
    for name in _SUB_DELTA_CHANNELS:
        value = payload.get(name)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or channel is not None:
            return None
        channel = name
    if channel is None:
        return None
    return (("sub", payload["agent_id"], payload.get("seq"), channel), channel, payload[channel])


def compact_replay_chunks(chunks):
    """附接回放紧凑快照：连续同通道分片合并为少帧（纯函数）。

    语义保证：
    - 只合并连续 run；结构帧打断 run 并原字节透传——管线分块边界
      （seal 点）与帧顺序完全保留，前端渲染结果与逐帧回放一致；
    - 单帧 run 原字节透传（零漂移），多帧 run 重序列化为紧凑帧；
    - 空分片帧（单通道空字符串/空数组）直接丢弃——前后端本来就忽略它们；
    - 子任务 delta 按 (agent_id, seq, 通道) 合并，等价于前端逐帧追加；
    - 不可解析帧原样透传，绝不因紧凑丢数据。
    """
    from .chat_runtime import merge_tool_call_delta
    out = []
    run_key = None
    run_channel = None
    run_items = []  # (original_chunk, value, payload)
    run_ids = []
    run_len = 0

    def flush():
        nonlocal run_key, run_channel, run_len
        if not run_items:
            run_key = run_channel = None
            return
        if len(run_items) == 1:
            out.append(run_items[0][0])
        elif run_channel == "tool_calls":
            merged = []
            for _, batch, _payload in run_items:
                merge_tool_call_delta(merged, batch)
            out.append("data: " + json.dumps({"tool_calls": merged}, ensure_ascii=False) + "\n\n")
        elif run_key is not None and run_key[0] == "sub":
            # 以 run 内最后一帧为底（保留 agent_id/seq/phase/timestamp 等），
            # 仅把该通道替换为拼接文本——与前端 appendLiveDelta 累积结果一致。
            merged_payload = dict(run_items[-1][2])
            merged_payload[run_channel] = "".join(value for _, value, _p in run_items)
            out.append("data: " + json.dumps(merged_payload, ensure_ascii=False) + "\n\n")
        else:
            merged_text = "".join(value for _, value, _p in run_items)
            merged_payload = {"content": merged_text} if run_channel == "content" else {"reasoning_content": merged_text}
            if run_ids and run_ids[-1] is not None:
                merged_payload = {"id": run_ids[-1], **merged_payload}
            out.append("data: " + json.dumps(merged_payload, ensure_ascii=False) + "\n\n")
        run_key = run_channel = None
        run_items.clear()
        run_ids.clear()
        run_len = 0

    def value_len(channel, value):
        if channel == "tool_calls":
            return sum(len(json.dumps(item, ensure_ascii=False)) for item in value)
        return len(value)

    for chunk in chunks:
        payload = _parse_data_chunk(chunk)
        parsed = _mergeable_channel(payload)
        is_sub = False
        if parsed is not None:
            channel, value, frame_id = parsed
            key = ("text", channel)
        else:
            sub = _mergeable_sub_delta(payload)
            if sub is None:
                flush()
                out.append(chunk)
                continue
            key, channel, value = sub
            frame_id = None
            is_sub = True
        if not value and not is_sub:
            continue  # 空分片：语义 no-op，直接丢弃且不打断 run
        added = value_len(channel, value)
        if run_key is not None and key != run_key:
            flush()
        if run_items and run_len + added > COMPACT_MERGED_FRAME_CHARS:
            flush()
        run_key = key
        run_channel = channel
        run_items.append((chunk, value, payload))
        run_ids.append(frame_id)
        run_len += added
    flush()
    return out


class StreamReplayJournal:
    def __init__(self):
        # Small tasks stay in memory; large ones spill to an automatically
        # removed private temp file instead of growing the hot deque forever.
        self.file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        self.end = 0

    def append(self, chunk: str) -> int:
        data = chunk.encode("utf-8")
        self.file.seek(self.end)
        self.file.write(len(data).to_bytes(8, "big") + data)
        self.end += 8 + len(data)
        return self.end

    def read(self, offset: int) -> tuple[str, int]:
        self.file.seek(offset)
        header = self.file.read(8)
        if len(header) != 8:
            raise OSError("任务回放记录不完整")
        size = int.from_bytes(header, "big")
        data = self.file.read(size)
        if len(data) != size:
            raise OSError("任务回放内容不完整")
        return data.decode("utf-8"), offset + 8 + size

    def close(self):
        try:
            self.file.close()
        except OSError:
            # A failed flush must not fail the real generation or its teardown.
            pass
