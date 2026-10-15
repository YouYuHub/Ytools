"""Shared model request limits for media and native documents, across input tools.

Parsed document text is not a native file part and does not consume these quotas.
Limits are configured or learned from explicit supplier errors.
"""
import copy
import re
import time
from collections import OrderedDict
from threading import Lock

_cache = OrderedDict()
_lock = Lock()
_TTL = 3600
_PART_KINDS = {'image_url': 'image', 'image': 'image', 'video_url': 'video', 'input_audio': 'audio', 'file': 'document', 'input_file': 'document', 'document': 'document'}


def _key(config):
    return (config.get('url'), config.get('selected_provider_name'), config.get('selected_model_id'),
            str(config.get('apiType') or 'chat_completions').strip().casefold().replace('-', '_'))


def get_media_limits(config):
    config = config or {}
    selected = config.get('selected_model') or {}
    if not isinstance(selected, dict):
        selected = {}
    configured = config.get('media_limits', config.get('mediaLimits', selected.get('mediaLimits', {})))
    limits = {k: v for k, v in configured.items() if k in {'total', *set(_PART_KINDS.values())}
              and isinstance(v, int) and not isinstance(v, bool) and v >= 0} if isinstance(configured, dict) else {}
    if str(config.get('apiType') or '').strip().casefold() == 'messages':
        limits.update(audio=0, video=0)
    with _lock:
        cached = _cache.get(_key(config))
        if cached and time.monotonic() - cached[0] < _TTL:
            for kind, limit in cached[1].items():
                limits[kind] = min(limits.get(kind, limit), limit)
    return limits


def limit_media_messages(messages, config):
    """Return request copy; preserve tools/text and prefer most recently read media.

    Omitted parts become text; aligned injection keys therefore remain re-readable.
    No data URLs are included in placeholders or notices.
    """
    limits = get_media_limits(config)
    if not limits:
        return messages, 0
    result = copy.deepcopy(messages)
    used = {}; total = 0; dropped = 0
    def limit_parts(content):
        nonlocal total, dropped
        # Retain the first item of the newest read batch, rather than its last item.
        for index, part in enumerate(content):
            kind = _PART_KINDS.get(part.get('type')) if isinstance(part, dict) else None
            if not isinstance(part, dict):
                continue
            if kind and (used.get(kind, 0) >= limits.get(kind, float('inf')) or total >= limits.get('total', float('inf'))):
                content[index] = {'type': 'text', 'text': f'[未发送 {kind}：当前模型的单次请求媒体配额已满或不支持该类型；需要时请单独重新读取，配额为 0 时需更换支持该类型的模型。]'}
                dropped += 1
                continue
            if kind:
                used[kind] = used.get(kind, 0) + 1
                total += 1
            if part.get('type') == 'tool_result' and isinstance(part.get('content'), list):
                limit_parts(part['content'])
            elif part.get('type') == 'document':
                source = part.get('source') or {}
                if source.get('type') == 'content' and isinstance(source.get('content'), list):
                    limit_parts(source['content'])

    for message in reversed(result):
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            limit_parts(content)
    # A cached quota must not alter text-only retries, or repeatedly append the
    # same notice to an already corrected request with partial response history.
    if not total and not dropped:
        return messages, 0
    note = '当前供应商/模型的单次请求媒体限制：' + '，'.join(f'{k}≤{v}' for k, v in limits.items())
    note += '。数量为 1 的类型请每次只读取一个；未发送内容不得当作已看过。新读取媒体可能替换旧媒体，请先记录已读取内容。'
    if not any(message.get('content') == note for message in result if isinstance(message, dict)):
        result.append({'role': 'user', 'content': note})
    return result, dropped


def learn_media_limit(error, messages, config):
    """Only retry deterministic media-count errors when the resulting request changes."""
    match = re.search(r'at most\s+(\d+)\s+(image|video|audio|document|file|media)(?:\(s\)|s)?\b', str(error), re.I)
    if not match:
        return False
    limit = int(match[1]); kind = match[2].lower()
    kind = {'media': 'total', 'file': 'document'}.get(kind, kind)
    current = get_media_limits(config)
    if kind in current and limit >= current[kind]:
        return False
    trial = dict(current, **{kind: limit})
    # Check against a configuration without cached limits for this model.
    trial_config = {'url': '__trial__', 'media_limits': trial}
    _, dropped = limit_media_messages(messages, trial_config)
    if not dropped:
        return False
    with _lock:
        _cache[_key(config)] = (time.monotonic(), trial)
        _cache.move_to_end(_key(config))
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return True
