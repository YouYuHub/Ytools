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
    if str(config.get('apiType') or '').strip().casefold() in {'messages', 'responses'}:
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


def native_doc_format_error(error):
    """网关是否拒绝了 dict 形态的 file 部件（原生文档格式不兼容）。

    形如 OpenCode 网关的 ``MMGPT3Item.file Input should be a valid string``
    校验错误：网关只接受 string 形态的 file，与 OpenAI 官方
    ``{"type": "file", "file": {"filename", "file_data"}}`` 格式不兼容。
    必须同时命中格式特征，避免把偶发 400 误判为确定性格式错误。
    """
    text = str(error or '')
    if 'MMGPT3Item' in text and 'Input should be a valid string' in text:
        return True
    return ('content.list' in text and 'Input should be a valid string' in text
            and ("'file'" in text or '"file"' in text))


def _file_part_payload(part):
    if not isinstance(part, dict) or part.get('type') != 'file':
        return None
    info = part.get('file')
    return info if isinstance(info, dict) else None


def downgrade_native_doc_to_text(messages):
    """把 dict 形态 file 部件替换为解析文本部件（返回新列表，原列表不动）。

    无可降级部件返回 None。解析失败/超长时转为明确占位或截断文本，
    绝不把 data URL 带入文本。调用方（chat_llm 400 分支）拿到非 None 后
    经 retry_messages 通道重发一次。
    """
    import base64

    from config import DEFAULT_FILE_TEXT_MAX_CHARS
    from factory.file_factory import extract_text_from_bytes

    def needs_downgrade(content):
        return isinstance(content, list) and any(_file_part_payload(p) is not None for p in content)

    if not any(isinstance(m, dict) and needs_downgrade(m.get('content')) for m in (messages or [])):
        return None
    result = copy.deepcopy(messages)
    for message in result:
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            info = _file_part_payload(part)
            if info is None:
                continue
            filename = str(info.get('filename') or '文档')
            text = ''
            try:
                data_url = str(info.get('file_data') or '')
                b64 = data_url.split(',', 1)[1] if ',' in data_url else data_url
                text = extract_text_from_bytes(filename, base64.b64decode(b64)) or ''
            except Exception as exc:
                text = ''
                parse_error = str(exc)
            else:
                parse_error = ''
            if text.strip():
                truncated = len(text) > DEFAULT_FILE_TEXT_MAX_CHARS
                body = text[:DEFAULT_FILE_TEXT_MAX_CHARS] if truncated else text
                content[index] = {'type': 'text', 'text': (
                    f'[文档 {filename} 原生格式不受当前网关支持，已自动转为文本'
                    f'（共 {len(text)} 字符' + ('，已截断' if truncated else '') + '）：]\n' + body)}
            else:
                content[index] = {'type': 'text', 'text': (
                    f'[文档 {filename} 原生格式不受当前网关支持，且文本解析失败'
                    f'（{parse_error or "无文本内容"}）：请改用 read_document 文本模式分页读取。]')}
    return result


def downgrade_native_doc_on_format_error(error, messages):
    """网关 file 部件格式错误时返回降级后的 messages（转解析文本），否则 None。

    供 chat_llm 400 分支调用：命中时经 retry_messages 通道重发一次（不计
    网络重试），与 MEDIA_LIMIT_DOWNGRADE 同模式。降级后请求里不再有 dict
    形态 file 部件，不会重复触发，无循环风险。
    """
    if not native_doc_format_error(error):
        return None
    return downgrade_native_doc_to_text(messages)
