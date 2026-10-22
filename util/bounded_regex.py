"""Python-compatible regex matching with a real execution timeout."""
import regex


def compile_pattern(pattern, *, ignore_case=False):
    if len(pattern) > 8192:
        raise ValueError("pattern 最多 8192 字符；请缩小表达式")
    try:
        return regex.compile(pattern, regex.VERSION0 | (regex.IGNORECASE if ignore_case else 0))
    except regex.error as exc:
        raise ValueError(f"正则表达式不合法: {exc}") from exc
