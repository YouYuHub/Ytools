"""Character estimate shared by request guards; preserves the existing formula."""


def text_token_estimate(text: str) -> int:
    if not text:
        return 0
    # Encoding runs in C and also handles lone surrogates without changing counts.
    ascii_chars = len(text) if text.isascii() else len(text.encode("ascii", "ignore"))
    return max(1, ascii_chars // 4 + len(text) - ascii_chars)
