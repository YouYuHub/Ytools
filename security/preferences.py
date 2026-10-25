"""Account-owned preferences. Only an explicit allowlist can override runtime defaults."""
import json
import math
import time

from memory.identity_store import Denied, Unavailable
from security.principal import current

UI_DEFAULTS = {"theme": "system", "accent": "blue", "task_status_visible": True,
               "sidebar_width": 240, "groups_expanded": False}
RUNTIME_KEYS = frozenset({
    "HISTORY_COMPACT_TRIGGER_RATIO", "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO", "HISTORY_COMPACT_TARGET_TOKENS",
    "HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR", "HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS",
    "REASONING_RETURN_MAX_LENGTH", "HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", "MCP_TOOL_CALL_TIMEOUT_SECONDS",
    "NETWORK_RETRY_MAX_ATTEMPTS", "COMPACTION_RETRY_MAX_ATTEMPTS", "VIDEO_MAX_READ_SECONDS",
    "ONE_TASK_MAX_WORKERS", "SUB_AGENT_MAX_CONCURRENT", "SUB_AGENT_MAX_ROUNDS", "SUB_AGENT_TIMEOUT_SECONDS",
    "SUB_AGENT_FINAL_REPLY_RETRY_MAX", "SUB_AGENT_STREAM_ERROR_RETRY_MAX", "SUB_AGENT_TODO_REMIND_MAX",
    "SPEECH_INPUT_LANGUAGE",
})
FLOAT_KEYS = {"HISTORY_COMPACT_TRIGGER_RATIO", "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO",
              "HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR", "MCP_TOOL_CALL_TIMEOUT_SECONDS", "SUB_AGENT_TIMEOUT_SECONDS"}
PERSONAL_PATHS = frozenset("/chat_config/" + name for name in (
    "history_compaction", "context_return", "mcp_tools", "network_retry", "compaction_retry", "video_read_limit",
    "tool_concurrency", "sub_agent_limits", "sub_agent_retry", "speech_input", "permissions"))


def prepare(db):
    db.execute("""CREATE TABLE IF NOT EXISTS user_preferences(
        user_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL)""")


def _decode(row):
    if row is None:
        return {"version": 0, "data": {}}
    try:
        data = json.loads(row["data"])
        if not isinstance(data, dict):
            raise ValueError()
        return {"version": row["version"], "data": data}
    except (ValueError, TypeError) as error:
        raise Unavailable("账号偏好配置损坏，请从备份恢复") from error


def read(principal=None):
    principal = principal or current()
    if principal is None:
        return {"version": 0, "data": {}}
    if "account_preferences" not in principal.resource_cache:
        with principal.store.database() as db:
            row = db.execute("SELECT version,data FROM user_preferences WHERE user_id=?", (principal.user_id,)).fetchone()
        principal.resource_cache["account_preferences"] = _decode(row)
    return principal.resource_cache["account_preferences"]


def snapshot(principal=None):
    saved = read(principal)
    return {"ui": dict(saved["data"].get("ui", {})), "version": saved["version"]}


def update(section, values, *, only_if_unset=False):
    principal = current()
    if principal is None:
        raise Denied("账号偏好修改需要登录身份")
    with principal.store.database(True) as db:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND enabled=1", (principal.user_id,)).fetchone():
            raise Denied("账号已停用")
        row = db.execute("SELECT version,data FROM user_preferences WHERE user_id=?", (principal.user_id,)).fetchone()
        record = _decode(row)
        stored = dict(record["data"].get(section, {}))
        for key, value in values.items():
            if not only_if_unset or key not in stored:
                stored[key] = value
        record["data"][section] = stored
        data = json.dumps(record["data"], ensure_ascii=False, allow_nan=False)
        if len(data.encode("utf-8")) > 32768:
            raise ValueError("账号偏好配置过大")
        db.execute("INSERT OR REPLACE INTO user_preferences VALUES(?,?,?,?)",
                   (principal.user_id, record["version"] + 1, data, time.time()))
    principal.resource_cache.pop("account_preferences", None)
    return snapshot(principal)


def validate_ui(values):
    if not isinstance(values, dict) or set(values) - set(UI_DEFAULTS):
        raise ValueError("不支持的界面偏好字段")
    for key, value in values.items():
        if key == "theme" and (not isinstance(value, str) or value not in {"system", "light", "dark"}):
            raise ValueError("主题无效")
        if key == "accent" and (not isinstance(value, str) or value not in {"blue", "green", "purple", "pink", "yellow", "none"}):
            raise ValueError("强调色无效")
        if key in {"task_status_visible", "groups_expanded"} and type(value) is not bool:
            raise ValueError("显示开关必须为布尔值")
        if key == "sidebar_width" and (type(value) is not int or not 220 <= value <= 840):
            raise ValueError("侧边栏宽度须为 220–840")
    return values


def runtime_value(name):
    if name not in RUNTIME_KEYS or current() is None:
        return None
    return read()["data"].get("runtime", {}).get(name)


def set_runtime(values, *, only_if_unset=False):
    if not values or set(values) - RUNTIME_KEYS:
        raise ValueError("不能通过个人偏好修改实例配置")
    result = {}
    for key, value in values.items():
        if key == "SPEECH_INPUT_LANGUAGE":
            from routers.speech_config_router import SUPPORTED_LANGUAGES
            if value not in SUPPORTED_LANGUAGES:
                raise ValueError("语音语言无效")
            result[key] = value
            continue
        if isinstance(value, bool):
            raise ValueError("聊天设置必须为有限数值")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("聊天设置必须为有限数值") from None
        if not math.isfinite(number) or abs(number) > 1e15:
            raise ValueError("聊天设置必须为有限数值")
        if key not in FLOAT_KEYS and int(number) != number:
            raise ValueError("该聊天设置必须为整数")
        if key in {"ONE_TASK_MAX_WORKERS", "SUB_AGENT_MAX_CONCURRENT", "HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS"} and number < 1:
            raise ValueError("并发和连续拒绝次数须至少为 1")
        if key in {"HISTORY_COMPACT_TARGET_TOKENS", "MCP_TOOL_CALL_TIMEOUT_SECONDS", "SUB_AGENT_MAX_ROUNDS", "SUB_AGENT_TIMEOUT_SECONDS"} and number < 0:
            raise ValueError("该聊天设置不能为负数")
        if key == "HISTORY_COMPACT_TRIGGER_RATIO" and not 0 < number <= .95:
            raise ValueError("压缩触发比例须在 0–0.95 之间")
        if key == "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO" and not 0 < number <= .5:
            raise ValueError("摘要预算比例须在 0–0.5 之间")
        if key == "HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR" and number < 0:
            raise ValueError("拒绝系数不能为负数")
        result[key] = str(float(number)) if key in FLOAT_KEYS else str(int(number))
    update("runtime", result, only_if_unset=only_if_unset)
    return result
