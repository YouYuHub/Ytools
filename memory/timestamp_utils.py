"""统一的会话时间戳格式化工具。

之前 `now_str`/`_now_str` 在 `memory/chat_round_store.py` 与
`memory/chat_memory.py` 各定义一份，另外 `memory/file_memory.py`
与 `mcp_server/sys_server.py` 又直接写 `strftime` 字面量。
集中到本模块后，全仓库的写入格式只有一处定义。
"""
from __future__ import annotations

from datetime import datetime

# 整个仓库历史文件统一的时间戳格式串（jsonl / 上传记录 / MCP 状态都用这个）
DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    """获取当前本地时间的格式化字符串，格式由 DEFAULT_TIMESTAMP_FORMAT 决定。"""
    return datetime.now().strftime(DEFAULT_TIMESTAMP_FORMAT)


def format_timestamp(value: datetime | None = None) -> str:
    """格式化任意时间（默认当前时间）为标准字符串。"""
    target = value or datetime.now()
    return target.strftime(DEFAULT_TIMESTAMP_FORMAT)