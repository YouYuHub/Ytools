"""聊天运行时的无状态辅助函数。

这里不包含会话编排或 MCP 调用，只负责请求参数、消息和 token 的轻量估算，
供聊天工厂、上下文压缩器与 sub_agent 子任务循环共同使用。

自 V1 sub_agent 起这里同时承载 SSE 解析 / tool_calls 增量合并 / 思考回传
契约等纯函数（原 chat_factory 私有实现迁入）：父循环与子任务循环共用同一
批基建函数，避免两个 Agent Loop 各自演化（docs/sub_agent_v1.md §6.2）。
"""
import json
import uuid
from typing import Any, Dict, List, Optional

try:
    import env_manager
except ImportError:
    env_manager = None

from config import (
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_TOOL_CALL_STREAM_TIMEOUT_SECONDS,
    Message,
)


def parse_bool_like(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def parse_int_like(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def parse_return_length(value: Any, default: int) -> int:
    """解析可配置的“回传长度”：允许任意整数（0=不回传，负数=全部回传）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def frontend_provides_full_history(messages: List[dict]) -> bool:
    user_count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            user_count += 1
        if role in {"assistant", "tool"}:
            return True
    return user_count > 1


def resolve_config_max_input_tokens(
    model_config: dict[str, Any] | None,
    default: int = 8192,
) -> int:
    """从已解析的模型配置中读取最大输入上下文长度。"""
    if not isinstance(model_config, dict):
        return default
    candidates = [model_config, model_config.get("selected_model")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("maxInputTokens", "max_input_tokens", "contextLength", "context_length"):
            try:
                parsed = int(candidate.get(key))
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return default


def resolve_model_max_input_tokens(default: int = 8192) -> int:
    if env_manager is None:
        return default
    return resolve_config_max_input_tokens(env_manager.get_default_chat_config(), default)


def _serialize_for_estimate(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    return value


def estimate_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    value = _serialize_for_estimate(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars)


# 多模态部件按固定占位计费：base64/data URL 数据按字符估算会虚高数万倍，
# 会误触发上下文预算检查；主流多模态模型单图成本约 1k token 量级，取固定占位值
MULTIMODAL_PART_PLACEHOLDER_TOKENS = 1024


def _estimate_content_value_tokens(content: Any) -> int:
    """content 支持 str 与多部件列表：媒体部件按固定占位计费，文本部件按字符估算。"""
    if not isinstance(content, list):
        return estimate_text_tokens(content)
    total = 0
    for part in content:
        if isinstance(part, str):
            total += estimate_text_tokens(part)
            continue
        if not isinstance(part, dict):
            continue
        if str(part.get("type") or "") == "text":
            total += estimate_text_tokens(part.get("text"))
            continue
        # 媒体部件：data URL / base64 / media:// 引用一律按固定占位计费
        total += MULTIMODAL_PART_PLACEHOLDER_TOKENS
    return total


def estimate_message_tokens(message: dict[str, Any]) -> int:
    if not isinstance(message, dict):
        return estimate_text_tokens(message)
    total = 4
    total += _estimate_content_value_tokens(message.get("content"))
    for key in ("role", "name", "reasoning_content", "tool_call_id", "finish_reason"):
        total += estimate_text_tokens(message.get(key))
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        total += estimate_text_tokens(tool_calls)
    return total


def estimate_messages_tokens(messages: List[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages if isinstance(message, dict))


def estimate_tool_definition_tokens(tools: List[Any] | None) -> int:
    if not tools:
        return 0
    return sum(estimate_text_tokens(tool) for tool in tools if tool is not None)


def estimate_request_context_tokens(
    messages: List[dict[str, Any]],
    tools: List[Any] | None = None,
) -> int:
    """估算一次 Chat Completions 请求的输入上下文（消息 + 工具 schema）。"""
    return estimate_messages_tokens(messages) + estimate_tool_definition_tokens(tools)


def build_request_messages(messages: List[dict]) -> list[Message]:
    return [
        Message(**({k: v for k, v in msg.items() if not str(k).startswith("_")}))
        if isinstance(msg, dict) else msg
        for msg in messages
    ]


class UsageAccumulator:
    def __init__(self) -> None:
        self._usage_by_id: Dict[str, dict] = {}
        self._fingerprints: set[str] = set()

    def collect(self, event_payload: dict[str, Any]) -> None:
        usage = event_payload.get("usage")
        if not isinstance(usage, dict):
            return
        usage_fingerprint = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        if usage_fingerprint in self._fingerprints:
            return
        completion_id = event_payload.get("id")
        if completion_id is not None and str(completion_id).strip():
            lookup_id = str(completion_id)
            if lookup_id in self._usage_by_id:
                return
            self._usage_by_id[lookup_id] = usage
            self._fingerprints.add(usage_fingerprint)
            return
        generated_id = f"no_id_{len(self._usage_by_id) + 1}"
        self._usage_by_id[generated_id] = usage
        self._fingerprints.add(usage_fingerprint)

    @property
    def count(self) -> int:
        return len(self._usage_by_id)

    def merge_to(self, target: dict[str, Any], merge_func) -> None:
        for usage in self._usage_by_id.values():
            merge_func(target, usage)


# ---------------------------------------------------------------------------
# SSE 解析与流式 tool_calls 增量合并（原 factory/chat_factory.py 私有实现迁入；
# chat_factory 保留同名 re-export，父/子 Agent 循环共用）
# ---------------------------------------------------------------------------

def parse_sse_event(sse_chunk: str) -> dict[str, Any] | None:
    if not isinstance(sse_chunk, str):
        return None
    chunk = sse_chunk.strip()
    if not chunk.startswith("data:"):
        return None
    payload_text = chunk[len("data:"):].strip()
    if payload_text == "[DONE]":
        return {"done": True}
    if not payload_text:
        return None
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        return None


def filter_tool_calls_fields(event: dict[str, Any]) -> dict[str, Any]:
    """过滤 tool_calls 中的 id 和 type 字段
    只处理 tool_calls 字段, 其他字段直接引用原对象以避免不必要的拷贝
    """
    if not isinstance(event, dict):
        return event
    # 如果没有 tool_calls 字段,直接返回原对象
    if "tool_calls" not in event:
        return event
    tool_calls = event.get("tool_calls")
    if not isinstance(tool_calls, list):
        return event
    # 只处理 tool_calls 字段,其他字段保持原样
    filtered_event = event.copy()
    filtered_tool_calls = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            # 创建新的工具调用对象,排除 id 和 type 字段
            filtered_tc = {k: v for k, v in tc.items() if k not in ("id", "type")}
            filtered_tool_calls.append(filtered_tc)
        else:
            filtered_tool_calls.append(tc)
    filtered_event["tool_calls"] = filtered_tool_calls
    return filtered_event


def merge_function_call_delta(
    accumulated: List[dict],
    function_call_delta: dict
) -> None:
    """合并流式 function_call 增量数据到累积列表中（兼容老模型）
    规则：
    - function_call 是单个对象，不是列表
    - 累积 name 和 arguments 字符串
    - 将 function_call 转换为与 tool_calls 相同的格式，index 固定为 0
    """
    if not isinstance(function_call_delta, dict):
        return
    # 查找是否已存在 function_call 的记录（index=0）
    existing_fc = None
    for acc_tc in accumulated:
        if acc_tc.get("index") == 0 and acc_tc.get("_is_function_call", False):
            existing_fc = acc_tc
            break
    # 如果不存在，创建新记录
    if existing_fc is None:
        existing_fc = {
            "index": 0,
            "_is_function_call": True,  # 标记这是 function_call 而非 tool_calls
            "function": {
                "name": "",
                "arguments": ""
            }
        }
        accumulated.append(existing_fc)
    # 累加 function.name
    fc_name = function_call_delta.get("name")
    if fc_name:
        existing_fc["function"]["name"] += fc_name
    # 累加 function.arguments
    fc_arguments = function_call_delta.get("arguments")
    if fc_arguments:
        existing_fc["function"]["arguments"] += fc_arguments


def merge_tool_call_delta(
    accumulated: List[dict],
    tool_calls_delta: list[dict]
) -> None:
    """合并流式工具调用增量数据到累积列表中
    规则：
    - accumulated 包含所有出现过的 index 对应的工具调用
    - 如果 delta 的 index 已存在，则累加该 index 的内容（name, arguments 等）
    - 如果 delta 的 index 不存在，则新增一条记录
    - index 本身是标识符，直接替换而非累加
    """
    if not isinstance(tool_calls_delta, list):
        return
    for delta in tool_calls_delta:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index")
        if index is None:
            continue
        # 查找是否已存在该 index 的记录
        existing_tc = None
        for acc_tc in accumulated:
            if acc_tc.get("index") == index:
                existing_tc = acc_tc
                break
        # 如果不存在，创建新记录
        if existing_tc is None:
            existing_tc = {
                "index": index,
                "id": delta.get("id") or f"call_{uuid.uuid4().hex}",
                "function": {
                    "name": "",
                    "arguments": ""
                },
                "type": "function"
            }
            accumulated.append(existing_tc)
        delta_function = delta.get("function", {})
        if delta.get("id"):
            existing_tc["id"] = delta["id"]
        if delta_function.get("name"):
            existing_tc["function"]["name"] += delta_function["name"]
        if delta_function.get("arguments"):
            existing_tc["function"]["arguments"] += delta_function["arguments"]
        if delta.get("type"):
            existing_tc["type"] = delta["type"]
    # 遵守 ai 给的 index 字段排序
    accumulated.sort(key=lambda x: x["index"])


# ---------------------------------------------------------------------------
# 思考过程（reasoning_content）回传契约（原 factory/chat_factory.py 迁入）
# ---------------------------------------------------------------------------

# 思考缺失时的占位符：仅用于"最新工具轮"的回传兜底——最新一次 API 调用
# 没有输出思考、上下文中也回溯不到更早的真实思考（或回传长度配置为 0）
# 时，带 tool_calls 的最新 assistant 消息仍需携带 reasoning_content 字段
# （GLM / DeepSeek 等严格上游缺失即 400）；占位符可通过校验，token 开销
# 可忽略。历史工具轮的思考不回传（请求副本中直接剥离字段，见
# _copy_for_request）。
REASONING_PLACEHOLDER = "..."


def load_reasoning_return_max_length(default: int = DEFAULT_REASONING_RETURN_MAX_LENGTH) -> int:
    if env_manager is None:
        return default
    return parse_return_length(env_manager.load_var("REASONING_RETURN_MAX_LENGTH", default), default)


def load_tool_call_stream_timeout(
    default: float = DEFAULT_TOOL_CALL_STREAM_TIMEOUT_SECONDS,
) -> float:
    """工具调用流式阶段（SSE 输出 tool_calls 期间）无输出超时秒数。

    0 或负数表示不限制。该超时约束的是"模型 SSE 事件间隔"：部分服务商
    在输出 tool_calls 期间会无限卡住（连接不断、永无后续事件），HTTP 读
    超时（默认 1800s）既兜不住也不能终止任务；超时后由运行时放弃本次工具
    调用、以失败结果反馈模型并继续任务。
    """
    if env_manager is None:
        return float(default)
    raw = env_manager.load_var("TOOL_CALL_STREAM_TIMEOUT_SECONDS", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else 0.0


def latest_reasoning_content(messages: List[dict[str, Any]]) -> str:
    """取上下文中最近一条非空 assistant 思考（工具轮回传兜底用；跳过占位符）。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip() and reasoning.strip() != REASONING_PLACEHOLDER:
            return reasoning
    return ""


def reasoning_content_for_tool_call(source: str, limit: int) -> str:
    """按回传长度策略生成工具轮 assistant 消息的 reasoning_content。

    思考模式下的工具调用链要求每条 assistant（tool_calls）消息都回传
    reasoning_content（严格上游缺失即 400），因此这里永远不返回空值：
    - limit < 0：全量回传；
    - limit > 0：只保留末尾 N 字符；
    - limit == 0：只回传占位符（"不回传"语义退化为最小占位，仅满足上游
      字段校验，避免请求被严格上游拒绝）；
    来源缺失（模型本轮未输出思考）时同样回退占位符。
    """
    if limit == 0:
        return REASONING_PLACEHOLDER
    text = source if isinstance(source, str) else ""
    if not text.strip():
        return REASONING_PLACEHOLDER
    return text if limit < 0 else text[-limit:]


def retain_latest_reasoning(messages: List[dict[str, Any]], *, limit: int | None = None) -> None:
    """只保留最近一条 assistant 真实思考，避免思考过程跨轮累积。

    严格上游（GLM / DeepSeek 等）要求工具调用链中每条 assistant（tool_calls）
    消息都回传 reasoning_content，缺失即 400；因此旧工具轮不再直接剥离字段，
    而是替换为 "..." 占位符（字段必须存在，占位可通过校验）。最近一条保持
    真实思考；意外缺失时回退历史最近思考，再兜底占位符。

    limit 缺省时按 REASONING_RETURN_MAX_LENGTH 配置读取；父循环
    （chat_factory）会显式传入其命名空间解析出的 limit（测试可打桩）。
    """
    assistant_indexes = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if not assistant_indexes:
        return
    latest_index = assistant_indexes[-1]
    # 先收集兜底思考：旧轮随后会被覆盖为占位符，之后再也取不到真实值
    fallback = ""
    for index in reversed(assistant_indexes[:-1]):
        reasoning = messages[index].get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip() and reasoning.strip() != REASONING_PLACEHOLDER:
            fallback = reasoning
            break
    if limit is None:
        limit = load_reasoning_return_max_length()
    for index in assistant_indexes:
        message = messages[index]
        if index == latest_index:
            if not message.get("tool_calls"):
                continue  # 非工具轮不强制回传思考
            current = message.get("reasoning_content")
            if isinstance(current, str) and current.strip():
                continue  # 最近一条已有思考，保持原样
            message["reasoning_content"] = reasoning_content_for_tool_call(fallback, limit)
            continue
        if message.get("tool_calls"):
            # 旧工具轮：字段必须存在；值用占位符避免真实思考跨轮累积
            message["reasoning_content"] = REASONING_PLACEHOLDER
        else:
            message.pop("reasoning_content", None)


def copy_for_request(
    messages: List[dict[str, Any]],
    *,
    limit: int | None = None,
) -> List[dict[str, Any]]:
    """构造发往上游的请求副本：思考过程只在此处做"回传"整形。

    运行时 messages 始终保持模型原始输出（每轮思考均为原始全文），落盘
    （record_message 深拷贝）也以真实值为源——历史工具轮的思考全部完整
    保留，绝不替换为 "..."。本函数只在副本上做回传侧语义：请求里最多只
    保留一条真实思考（最近一次 API 调用输出的那条），其余一律占位/剥离，
    既满足严格上游（GLM/DeepSeek）"reasoning_content 必须存在"的校验，
    又避免真实思考跨轮累积发给上游：
    - 旧工具轮 / 旧非工具 assistant：剥离 reasoning_content（历史思考
      一律不回传，只保留运行时/落盘里的原始值）；
    - 最新工具轮：只回传最近一次 API 调用输出的思考，按
      REASONING_RETURN_MAX_LENGTH 裁剪；本轮没有输出思考时向前回溯，
      取上下文中最近一次真实思考按同一规则回传；完全找不到真实思考或
      limit==0 时回传占位符（字段仅为通过上游校验而存在）；
    - 最新非工具轮：思考非必须，已有则按长度裁剪，limit==0 直接剥离。

    只对"需要修改"的 assistant 消息创建新 dict（浅拷贝顶层键），其余消息
    直接复用引用——避免对整包 messages（可能含 MB 级 base64 多模态部件）
    做深拷贝的每轮开销。
    """
    assistant_indexes = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if not assistant_indexes:
        return list(messages)
    latest_index = assistant_indexes[-1]
    # 回退思考：最新一次 API 调用没有输出思考时，向前找最近一次真实思考
    # （运行时 messages 每轮都保存原始全文，历史值可直接回溯；跳过占位符）
    fallback = ""
    for index in reversed(assistant_indexes[:-1]):
        reasoning = messages[index].get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip() and reasoning.strip() != REASONING_PLACEHOLDER:
            fallback = reasoning
            break
    if limit is None:
        limit = load_reasoning_return_max_length()

    def _mutated(message: dict[str, Any], reasoning: Any, drop: bool) -> dict[str, Any]:
        clone = dict(message)  # 顶层浅拷贝：字符串字段共享不可变值，安全
        if drop:
            clone.pop("reasoning_content", None)
        else:
            clone["reasoning_content"] = reasoning
        return clone

    copied: List[dict[str, Any]] = []
    assistant_set = set(assistant_indexes)
    for index, message in enumerate(messages):
        if index not in assistant_set or not isinstance(message, dict):
            copied.append(message)
            continue
        if index == latest_index:
            if not message.get("tool_calls"):
                # 非工具轮不强制回传思考：已有思考时按回传长度裁剪
                # （运行时保存的是原始全文），limit==0 时直接剥离字段
                reasoning = message.get("reasoning_content")
                if not (isinstance(reasoning, str) and reasoning.strip()) or limit < 0:
                    copied.append(message)  # 无思考或全量回传：原样引用
                elif limit == 0:
                    copied.append(_mutated(message, None, drop=True))
                else:
                    copied.append(_mutated(message, reasoning[-limit:], drop=False))
                continue
            current = message.get("reasoning_content")
            # 只回传一条思考：优先本轮（最近一次 API 调用）输出的思考；
            # 本轮没有时向前回溯最近一次真实思考（不产生第二条真实思考）；
            # 都没有或 limit==0 时回传占位符（字段存在即可通过严格上游校验）
            source = current if isinstance(current, str) and current.strip() else fallback
            reasoning = reasoning_content_for_tool_call(source, limit)
            if isinstance(current, str) and reasoning == current:
                copied.append(message)  # 回传值与原值一致：复用原对象
            else:
                copied.append(_mutated(message, reasoning, drop=False))
            continue
        # 旧 assistant（工具轮/非工具轮）：历史思考一律不回传，剥离字段
        if "reasoning_content" not in message:
            copied.append(message)
            continue
        copied.append(_mutated(message, None, drop=True))
    return copied