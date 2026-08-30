"""会话历史与单轮工具轨迹的上下文压缩。

压缩模型被要求输出普通文本，不依赖 JSON mode 或结构化输出能力。原始工具结果仍会
完整推送给前端并写入 JSONL；仅下一次发送给模型的工作上下文会被替换为摘要。
"""
# from __future__ import annotations
# 标准库
import asyncio
import inspect
import json
from dataclasses import dataclass
from functools import partial
from typing import Any
# 自定义模块
from chat.chat_llm import ChatLLM
from config import (
    ChatLLMRequest,
    DEFAULT_CONTEXT_HISTORY_ROUNDS,
    DEFAULT_HISTORY_TRIGGER_RATIO,
    DEFAULT_SUMMARY_BUDGET_RATIO,
    DEFAULT_OVERSIZED_REJECT_FACTOR,
    DEFAULT_MAX_OVERSIZED_REJECTIONS,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
)
from env_manager import (
    ChatModelConfigurationError,
    get_model_config,
    get_role_parameter,
    get_role_selection,
    load_var,
    require_default_chat_config,
)
from memory.chat_history_format import (
    format_tool_call_history_line,
    parse_summary_sections,
    render_context_summary,
    round_entry_to_compaction_text,
    round_entry_to_context_messages,
    split_context_window,
)
from memory.chat_round_store import merge_usage_dict as _merge_usage_dict
# 相对导入
from .chat_runtime import (
    estimate_messages_tokens,
    estimate_request_context_tokens,
    estimate_text_tokens,
    parse_return_length,
    resolve_config_max_input_tokens,
    resolve_model_max_input_tokens,
)


# 上下文压缩阈值 = min(聊天窗口, 压缩窗口) × trigger_ratio
# 摘要输出上限 = min(模型 maxOutputTokens, 8k, 摘要预算)
_SUMMARY_SEGMENT_MAX_TOKENS = 8192
_OVERSIZED_PREVIEW_TOKEN_BUDGET = 2048
_OVERSIZED_FEEDBACK_EXCERPT_TOKEN_BUDGET = 600
_COMPACTION_EVENT_EXCERPT_TOKEN_BUDGET = 800
_ROUND_SUMMARY_MARKER = "_context_compaction_scope"
_ROUND_SUMMARY_INDEX_MARKER = "_context_compaction_index"
_ROUND_SUMMARY_SCOPE = "active_round"


def get_context_compaction_defaults() -> dict[str, int | float]:
    """返回前后端共用的历史压缩默认值，供配置接口和恢复默认功能使用。"""
    return {
        "keep_rounds": DEFAULT_CONTEXT_HISTORY_ROUNDS,
        "trigger_ratio": DEFAULT_HISTORY_TRIGGER_RATIO,
        "summary_budget_ratio": DEFAULT_SUMMARY_BUDGET_RATIO,
        "oversized_reject_factor": DEFAULT_OVERSIZED_REJECT_FACTOR,
        "max_oversized_rejections": DEFAULT_MAX_OVERSIZED_REJECTIONS,
    }


class ContextCompactionError(RuntimeError):
    """压缩彻底失败：压缩模型与聊天模型均不可用——调用方应终止当前任务。

    与 ChatModelConfigurationError（配置问题）不同，此异常表示实际调用链路
    已按"压缩模型 -> 聊天模型重试"降级过仍失败，不再做节选降级。
    """


@dataclass(frozen=True)
class ContextCompactionSettings:
    """从环境配置加载的上下文压缩策略。

    压缩模型选择不在此处读取：统一由 models.json 顶层 `model_selection.compaction_model`
    管理（见 resolve_context_compaction_model_config），未配置时跟随当前聊天模型。
    """
    keep_rounds: int
    trigger_ratio: float
    # 累计摘要总预算占聊天模型窗口的比例
    summary_budget_ratio: float
    oversized_reject_factor: float
    max_oversized_rejections: int


@dataclass(frozen=True)
class SummaryBuildResult:
    text: str
    source_was_truncated: bool
    used_fallback: bool
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class RoundContextCompactionResult:
    messages: list[dict[str, Any]]
    triggered: bool
    before_tokens: int
    after_tokens: int
    used_fallback: bool = False
    summary_text: str | None = None
    summary_blocks: list[dict[str, Any]] | None = None
    compress_index: int = 0
    usage: dict[str, Any] | None = None


def _compaction_model_from_selection() -> tuple[str | None, str | None, str | None]:
    """从 models.json 顶层 model_selection.compaction_model 读取压缩模型选择（含 api_type）。"""
    selection = get_role_selection("compaction_model")
    return (
        _normalize_optional_name(selection.get("ownership_name")),
        _normalize_optional_name(selection.get("model_name")),
        _normalize_optional_name(selection.get("api_type")),
    )


def _parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_non_negative_int(value: Any, default: int) -> int:
    """解析 >=0 的整数；负数非法回退默认值。

    与 _parse_positive_int 的区别：0 是合法值（keep_rounds 的"无限窗口"
    语义，见 config.HistoryCompactionConfig），不能回落为默认值。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _parse_trigger_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    # 保留系统提示词、当前输入和工具定义的余量，避免压缩触发得太晚。
    return min(parsed, 0.95)


def _parse_summary_budget_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    # 摘要预算不应超过单轮阈值的一半，防止摘要本身挤占工具轨迹空间。
    return min(parsed, 0.5)


def _parse_oversized_reject_factor(value: Any, default: float) -> float:
    """超大结果拒绝系数；<=0 表示关闭该功能，上限 10 防止误配置。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return 0.0
    return min(parsed, 10.0)


def _normalize_optional_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value).strip()


def _truncate_text_to_token_budget(text: str, token_budget: int) -> str:
    """保留文本头尾，按轻量估算裁剪到指定 token 预算内。"""
    normalized = _to_text(text)
    if not normalized or token_budget <= 0:
        return ""
    total_tokens = estimate_text_tokens(normalized)
    if total_tokens <= token_budget:
        return normalized

    marker = "\n…【中间内容因上下文预算已省略】…\n"
    marker_tokens = estimate_text_tokens(marker)
    if token_budget <= marker_tokens + 8:
        # 预算极小时宁可保留开头的关键标识，也不制造超过预算的大摘要。
        char_count = max(1, int(len(normalized) * token_budget / max(total_tokens, 1)))
        return normalized[:char_count]

    available_budget = max(1, token_budget - marker_tokens)
    # 估算公式只是字符密度的粗估（ASCII÷4 + 非ASCII），对密集 ASCII / 代码 / JSON
    # 场景可能低估真实 token；若按 100% 预算裁剪，估算偏差会直接传导到上游模型输入，
    # 存在撑爆窗口的风险（且迭代收紧只按自身估算测量，无法修正系统性低估）。
    # 因此头尾合计只使用预算的 80%（头部 45% + 尾部 35%），预留约 20% 余量
    # 吸收估算误差；尾部占比相对提高（35/80=43.75%），
    # 因为工具输出/日志的结论与错误通常出现在末尾。
    retained_fraction = 0.45 + 0.35
    estimated_chars = max(
        2,
        int(len(normalized) * available_budget * retained_fraction / max(total_tokens, 1)),
    )
    head_chars = max(1, int(estimated_chars * (0.45 / retained_fraction)))
    tail_chars = max(1, estimated_chars - head_chars)
    candidate = normalized[:head_chars] + marker + normalized[-tail_chars:]

    # 估算公式会受中英文比例变化影响，少量迭代收紧以防超出模型输入预算。
    for _ in range(4):
        candidate_tokens = estimate_text_tokens(candidate)
        if candidate_tokens <= token_budget:
            return candidate
        scale = max(0.1, token_budget / max(candidate_tokens, 1))
        head_chars = max(1, int(head_chars * scale))
        tail_chars = max(1, int(tail_chars * scale))
        candidate = normalized[:head_chars] + marker + normalized[-tail_chars:]
    return candidate


def _summary_output_token_limit(
    tool_request: ChatLLMRequest,
    model_config: dict[str, Any],
    *,
    output_token_budget: int | None = None,
) -> int:
    """单段摘要输出上限 = min(模型 maxOutputTokens, 8k, 摘要预算)。

    `output_token_budget` 为调用方传入的摘要预算（如单轮摘要总预算的剩余份额）；
    未传时只受模型输出能力与 8k 分段上限约束（跨轮 chunk 摘要场景）。
    """
    model_limit = _parse_positive_int(model_config.get("maxOutputTokens"), _SUMMARY_SEGMENT_MAX_TOKENS)
    limit = min(_SUMMARY_SEGMENT_MAX_TOKENS, model_limit)
    if output_token_budget is not None and output_token_budget > 0:
        limit = min(limit, output_token_budget)
    return max(64, limit)


def _clean_summary_text(value: Any) -> str:
    text = _to_text(value)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text



def load_context_compaction_settings() -> ContextCompactionSettings:
    """读取环境变量并做运行时安全规整。"""
    return ContextCompactionSettings(
        keep_rounds=_parse_non_negative_int(
            load_var("HISTORY_COMPACT_KEEP_ROUNDS", DEFAULT_CONTEXT_HISTORY_ROUNDS),
            DEFAULT_CONTEXT_HISTORY_ROUNDS,
        ),
        trigger_ratio=_parse_trigger_ratio(
            load_var("HISTORY_COMPACT_TRIGGER_RATIO", DEFAULT_HISTORY_TRIGGER_RATIO),
            DEFAULT_HISTORY_TRIGGER_RATIO,
        ),
        summary_budget_ratio=_parse_summary_budget_ratio(
            load_var("HISTORY_COMPACT_SUMMARY_BUDGET_RATIO", DEFAULT_SUMMARY_BUDGET_RATIO),
            DEFAULT_SUMMARY_BUDGET_RATIO,
        ),
        oversized_reject_factor=_parse_oversized_reject_factor(
            load_var("HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR", DEFAULT_OVERSIZED_REJECT_FACTOR),
            DEFAULT_OVERSIZED_REJECT_FACTOR,
        ),
        max_oversized_rejections=_parse_positive_int(
            load_var("HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS", DEFAULT_MAX_OVERSIZED_REJECTIONS),
            DEFAULT_MAX_OVERSIZED_REJECTIONS,
        ),
    )


def resolve_context_compaction_model_config(
    settings: ContextCompactionSettings | None = None,
) -> dict[str, Any]:
    """解析压缩模型配置；未配置或配置不可用时使用当前聊天模型。

    压缩模型统一由 models.json 顶层 `model_selection.compaction_model` 管理，
    不再从 .env 读取或写入（HISTORY_COMPACT_MODEL_PROVIDER/NAME 已弃用）。
    配置的压缩模型在 models.json 中不存在、或协议不是 chat-completions 时，
    回退为当前聊天模型（运行时仅支持 Chat Completions 协议）。
    """
    provider, model, _ = _compaction_model_from_selection()
    config = None
    if provider:
        candidate = get_model_config(provider, model)
        if candidate is not None and str(candidate.get("apiType") or "chat-completions").strip().casefold() == "chat-completions":
            config = candidate
        else:
            print(
                f"[WARN] 压缩模型不可用（{provider} / {model}），回退为当前聊天模型执行压缩"
            )
    if config is None:
        config = require_default_chat_config()
    return config


def get_context_compaction_model_status(
    settings: ContextCompactionSettings | None = None,
) -> dict[str, Any]:
    """返回压缩模型选择及其有效解析状态，供配置接口展示。"""
    provider, model, api_type = _compaction_model_from_selection()
    configured = None
    if provider or model:
        configured = {
            "provider": provider,
            "model": model,
            "api_type": api_type,
            "parameter": get_role_selection("compaction_model").get("parameter") or {},
        }
    status: dict[str, Any] = {
        "configured": configured,
        "uses_active_chat_model": configured is None,
        "effective": None,
        "valid": False,
        "error": None,
    }
    try:
        config = resolve_context_compaction_model_config(settings)
    except ChatModelConfigurationError as exc:
        status["error"] = str(exc)
        return status

    status["valid"] = True
    status["effective"] = {
        "provider": config.get("selected_provider_name"),
        "model": config.get("selected_model_name"),
        "model_id": config.get("selected_model_id"),
        "api_type": config.get("apiType") or "chat-completions",
        "max_input_tokens": resolve_config_max_input_tokens(config),
    }
    return status


def resolve_context_compaction_threshold(
    settings: ContextCompactionSettings | None = None,
) -> int:
    """上下文压缩触发阈值（跨轮历史与单轮工具轨迹共用）。

    阈值 = min(聊天模型窗口, 压缩模型窗口) × trigger_ratio；
    以两者较小窗口为基数，保证压缩产物能同时放进聊天与压缩模型的上下文。
    返回 0 表示功能关闭（trigger_ratio <= 0，正常配置解析不会出现）。
    """
    settings = settings or load_context_compaction_settings()
    if settings.trigger_ratio <= 0:
        return 0
    chat_window = resolve_model_max_input_tokens(default=8192)
    try:
        compaction_window = resolve_config_max_input_tokens(
            resolve_context_compaction_model_config(settings),
            default=8192,
        )
    except ChatModelConfigurationError:
        compaction_window = chat_window
    window = min(chat_window, compaction_window)
    return max(1024, int(window * settings.trigger_ratio))


def resolve_oversized_result_token_threshold(
    settings: ContextCompactionSettings | None = None,
) -> int:
    """超大工具结果的拒绝阈值。

    阈值 = min(聊天模型窗口, 压缩模型窗口) × oversized_reject_factor；
    返回 0 表示功能关闭。该阈值保证"先于压缩"拦截：超过它的大结果即使走
    head/tail 截断压缩，中段信息丢失也已不可接受。
    """
    settings = settings or load_context_compaction_settings()
    if settings.oversized_reject_factor <= 0:
        return 0
    chat_window = resolve_model_max_input_tokens(default=8192)
    try:
        compaction_window = resolve_config_max_input_tokens(
            resolve_context_compaction_model_config(settings),
            default=8192,
        )
    except ChatModelConfigurationError:
        compaction_window = chat_window
    window = min(chat_window, compaction_window)
    return max(1024, int(window * settings.oversized_reject_factor))


def resolve_summary_total_budget(
    settings: ContextCompactionSettings | None = None,
) -> int:
    """摘要总预算 = 聊天窗口 × summary_budget_ratio（用户可配，上限 50%）。

    以窗口为基数（而非单轮阈值）：100k 窗口 × 20% = 20k 总摘要预算。
    """
    settings = settings or load_context_compaction_settings()
    window = resolve_model_max_input_tokens(default=8192)
    return max(1024, int(window * settings.summary_budget_ratio))


def make_oversized_result_preview(
    result_text: str,
    token_budget: int = _OVERSIZED_PREVIEW_TOKEN_BUDGET,
) -> str:
    """把被拒绝的超大工具结果裁剪成头尾节选，仅用于 JSONL 记录，不进入模型上下文。"""
    return _truncate_text_to_token_budget(result_text, token_budget)


def build_oversized_tool_feedback(
    tool_name: str,
    estimated_tokens: int,
    threshold: int,
    result_text: str | None = None,
    excerpt_token_budget: int = _OVERSIZED_FEEDBACK_EXCERPT_TOKEN_BUDGET,
) -> str:
    """构造给模型的超长反馈文案：告知结果过大并引导重新考虑工具使用。

    `result_text` 非空时，文案附带该结果的头尾节选（复用 head 45% + tail 35%
    截断比例与省略标记），给模型提供缩小查询范围的具体线索；不传则保持纯提示。
    """
    estimate_k = round(estimated_tokens / 1000, 1)
    threshold_k = round(threshold / 1000, 1)
    feedback = (
        f"工具 {tool_name} 的返回结果过大：估算约 {estimate_k}k token，"
        f"超过 {threshold_k}k 的上下文预算，结果无法纳入模型上下文。"
        "请重新考虑工具使用方式：例如缩小查询范围、添加过滤/限制参数、分批获取，"
        "或改用能返回精炼结果的工具。请不要原样重试同样的调用。"
    )
    if result_text:
        excerpt = _truncate_text_to_token_budget(result_text, excerpt_token_budget)
        if excerpt:
            feedback += (
                "\n\n为帮助你重新规划，以下是该结果的头尾节选（中间部分已省略）：\n"
                f"{excerpt}"
            )
    return feedback


def _sse_frame_payload(chunk: Any) -> dict[str, Any] | None:
    """解析 ChatLLM 流式输出的单条 SSE 文本帧为 JSON dict。

    与 chat_factory._parse_sse_event 同规则（此处独立实现以避免反向依赖）：
    - 非 data: 行 / 空行 / 无法解析 -> None
    - "data: [DONE]" -> {"done": True}
    """
    if not isinstance(chunk, str):
        return None
    line = chunk.strip()
    if not line.startswith("data:"):
        return None
    payload_text = line[len("data:"):].strip()
    if payload_text == "[DONE]":
        return {"done": True}
    if not payload_text:
        return None
    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def summarize_context_text(
    source_text: str,
    tool_request: ChatLLMRequest,
    *,
    scope: str,
    settings: ContextCompactionSettings | None = None,
    output_token_budget: int | None = None,
    delta_emitter: Any | None = None,
) -> SummaryBuildResult:
    """用配置的压缩模型将内容变成普通文本摘要。

    调用链：压缩模型 ->（失败或返回空时）当前聊天模型重试一次 ->
    （仍失败）抛 ContextCompactionError 终止任务，不再做节选降级。
    `output_token_budget` 用于把单段摘要约束在摘要预算之内（单轮场景）。

    `delta_emitter` 为可选异步回调：提供时压缩请求走**流式接口**，模型每产生
    一段思考/正文增量即回调一次 `{"reasoning_content"?: chunk, "content"?: chunk}`
    （与聊天 SSE 字段同名），供调用方实时转发给前端渲染；未提供时保持原有
    非流式调用。两种模式的最终摘要文本、usage 口径完全一致。
    """
    settings = settings or load_context_compaction_settings()
    try:
        model_config = resolve_context_compaction_model_config(settings)
    except ChatModelConfigurationError as exc:
        raise ContextCompactionError(f"压缩模型与聊天模型配置均无效：{exc}") from exc

    output_tokens = _summary_output_token_limit(
        tool_request, model_config, output_token_budget=output_token_budget
    )
    prompt = (
        f"你是{scope}压缩器。请把用户任务、已确认事实、关键数值、路径、错误信息、"
        "工具执行结论以及仍需继续的事项压缩为后续模型可直接使用的中文备忘。\n"
        "输出时尽量按以下标题分段组织，没有对应内容的段落整体省略，标题必须原样使用：\n"
        "【任务目标】- 当前正在完成的任务目标\n"
        "【已完成工作】- 已完成的关键步骤与结果\n"
        "【关键设计决定】- 关键决策及选择原因（例如选定某方案/路径/工具的原因）\n"
        "【未解决问题】- 仍需继续、受阻或下一步要做的事项\n"
        "【重要文件】- 涉及的关键文件/路径及其作用\n"
        "段落内使用短项目符号。只输出普通文本，不要输出 JSON、XML、代码围栏或任何固定数据结构。\n"
        "工具原始输出很长时，只保留会影响后续决策的结果、证据、错误和下一步。"
    )
    if scope == "累计历史摘要":
        prompt += (
            "这是已有累计摘要与新增历史的合并任务：必须继承已有摘要中仍有效的事实、"
            "设计决定、文件路径和未解决事项，同时吸收新增内容；不要只输出新增部分。"
        )
    max_input_tokens = resolve_config_max_input_tokens(model_config)
    source_budget = max(
        256,
        max_input_tokens - estimate_text_tokens(prompt) - output_tokens - 256,
    )
    bounded_source = _truncate_text_to_token_budget(source_text, source_budget)
    source_was_truncated = bounded_source != _to_text(source_text)
    summary_request = _build_summary_request(prompt, bounded_source, output_tokens, tool_request.session_id)

    async def _invoke_non_stream(request: ChatLLMRequest, config: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        result = await asyncio.to_thread(
            partial(
                ChatLLM.chat_completions,
                request=request,
                stream=False,
                model_config=config,
            )
        )
        if inspect.isawaitable(result):
            result = await result
        text = ""
        if isinstance(result, dict):
            text = _clean_summary_text(result.get("content") or result.get("reasoning_content"))
        if not text:
            raise RuntimeError("压缩模型返回了空内容")
        # 防止少数服务端忽略 max_tokens，反过来放大下一轮的上下文。
        text = _truncate_text_to_token_budget(text, max(128, output_tokens * 2))
        usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else None
        return text, usage

    async def _invoke_stream(request: ChatLLMRequest, config: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """流式压缩：逐帧解析 SSE，把思考/正文增量实时回调给 delta_emitter。

        - reasoning_content / content 增量分别累积，最终摘要只取正文；
        - 带 retry 标记的错误帧是客户端内部重连提示，忽略；
        - 其余错误帧记录 last_error，若整条流没有产出正文则作为失败原因抛出，
          交给上层走"压缩模型 -> 聊天模型重试"降级链。
        """
        generator = ChatLLM.chat_completions(
            request=request,
            stream=True,
            model_config=config,
        )
        if inspect.isawaitable(generator):
            generator = await generator
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] | None = None
        last_error = ""
        async for chunk in generator:
            frame = _sse_frame_payload(chunk)
            if frame is None:
                continue
            if frame.get("done"):
                break
            error_text = frame.get("error")
            if error_text:
                if not frame.get("retry"):
                    last_error = str(error_text)
                continue
            reasoning_chunk = frame.get("reasoning_content")
            content_chunk = frame.get("content")
            has_reasoning = isinstance(reasoning_chunk, str) and bool(reasoning_chunk)
            has_content = isinstance(content_chunk, str) and bool(content_chunk)
            if has_reasoning:
                reasoning_parts.append(reasoning_chunk)
            if has_content:
                content_parts.append(content_chunk)
            if (has_reasoning or has_content) and delta_emitter is not None:
                delta_payload: dict[str, Any] = {}
                if has_reasoning:
                    delta_payload["reasoning_content"] = reasoning_chunk
                if has_content:
                    delta_payload["content"] = content_chunk
                try:
                    await delta_emitter(delta_payload)
                except Exception as exc:
                    print(f"[WARN] 压缩增量事件推送失败：{exc}")
            if isinstance(frame.get("usage"), dict):
                usage = frame["usage"]
        text = "".join(content_parts).strip()
        if not text:
            hint = f"（服务端错误：{last_error}）" if last_error else ""
            raise RuntimeError(f"压缩模型返回了空内容{hint}")
        text = _clean_summary_text(text)
        # 防止少数服务端忽略 max_tokens，反过来放大下一轮的上下文。
        text = _truncate_text_to_token_budget(text, max(128, output_tokens * 2))
        return text, usage

    async def _invoke(request: ChatLLMRequest, config: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        if delta_emitter is None:
            return await _invoke_non_stream(request, config)
        return await _invoke_stream(request, config)

    try:
        text, usage = await _invoke(summary_request, model_config)
    except asyncio.CancelledError:
        raise
    except ContextCompactionError:
        raise
    except Exception as first_error:
        # 压缩模型失败：换当前聊天模型重试一次；同一模型则不重复尝试
        try:
            chat_config = require_default_chat_config()
        except ChatModelConfigurationError as exc:
            raise ContextCompactionError(
                f"压缩失败且聊天模型不可用：{exc}（首次错误：{first_error}）"
            ) from first_error
        same_model = (
            str(chat_config.get("selected_provider_name") or "") == str(model_config.get("selected_provider_name") or "")
            and str(chat_config.get("selected_model_id") or "") == str(model_config.get("selected_model_id") or "")
        )
        if same_model:
            raise ContextCompactionError(f"压缩模型调用失败：{first_error}") from first_error
        print(f"[WARN] 压缩模型失败（{first_error}），改用当前聊天模型重试压缩")
        retry_limit = _summary_output_token_limit(
            tool_request, chat_config, output_token_budget=output_token_budget
        )
        retry_max_input = resolve_config_max_input_tokens(chat_config)
        retry_source_budget = max(
            256,
            retry_max_input - estimate_text_tokens(prompt) - retry_limit - 256,
        )
        retry_source = _truncate_text_to_token_budget(source_text, retry_source_budget)
        retry_request = _build_summary_request(prompt, retry_source, retry_limit, tool_request.session_id)
        try:
            text, usage = await _invoke(retry_request, chat_config)
        except asyncio.CancelledError:
            raise
        except Exception as retry_error:
            raise ContextCompactionError(
                f"聊天模型重试压缩仍失败，任务终止：{retry_error}（首次错误：{first_error}）"
            ) from retry_error

    return SummaryBuildResult(
        text=text,
        source_was_truncated=source_was_truncated,
        used_fallback=False,
        usage=usage,
    )


def _build_summary_request(
    prompt: str,
    source: str,
    output_tokens: int,
    session_id: str | None,
) -> ChatLLMRequest:
    """构造压缩摘要请求；模型选择配置的 parameter 覆盖内置默认参数。

    参数优先级：model_selection.compaction_model.parameter（按压缩模型 api_type 取桶）
    > 压缩内置默认值。
    """
    parameter = get_role_parameter("compaction_model")

    def _as_float(key: str, default: float) -> float:
        try:
            return float(parameter[key])
        except (KeyError, TypeError, ValueError):
            return default

    def _as_int(key: str, default: int) -> int:
        try:
            return int(parameter[key])
        except (KeyError, TypeError, ValueError):
            return default

    extra_body = parameter.get("extra_body")
    request = ChatLLMRequest(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": source},
        ],
        max_tokens=_as_int("max_tokens", output_tokens),
        temperature=_as_float("temperature", 0.2),
        top_p=_as_float("top_p", 1.0),
        presence_penalty=_as_float("presence_penalty", 0.0),
        stream=False,
        reasoning_effort=str(parameter.get("reasoning_effort") or "low"),
        tool_choice=None,
        parallel_tool_calls=None,
        session_id=session_id,
        use_backend_history=False,
        backend_history_rounds=0,
    )
    if isinstance(extra_body, dict) and extra_body:
        request.extra_body = dict(extra_body)
    return request


def _summary_source_round_count(summary_state: Any) -> int:
    if not isinstance(summary_state, dict):
        return 0
    try:
        return max(0, int(summary_state.get("source_round_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _resolve_compaction_source_budget(
    tool_request: ChatLLMRequest,
    settings: ContextCompactionSettings,
) -> int:
    """为每批压缩源预留提示词和输出空间，避免静默丢掉中间轮次。"""
    try:
        model_config = resolve_context_compaction_model_config(settings)
        model_limit = resolve_config_max_input_tokens(model_config)
        output_limit = _summary_output_token_limit(
            tool_request,
            model_config,
            output_token_budget=resolve_summary_total_budget(settings),
        )
    except ChatModelConfigurationError:
        model_limit = resolve_model_max_input_tokens(default=8192)
        output_limit = min(_SUMMARY_SEGMENT_MAX_TOKENS, max(64, model_limit // 8))
    reserved = output_limit + 4096
    return max(2048, min(int(model_limit * 0.75), model_limit - reserved))


def _select_compaction_count(
    round_entries: list[dict[str, Any]],
    source_budget: int,
) -> int:
    """按压缩模型输入预算选择最早的一批轮次，至少推进一轮。"""
    total = 0
    selected = 0
    for round_entry in round_entries:
        round_tokens = max(1, estimate_text_tokens(round_entry_to_compaction_text(round_entry)))
        if selected and total + round_tokens > source_budget:
            break
        selected += 1
        total += round_tokens
    return max(1, selected) if round_entries else 0


async def _build_cumulative_summary(
    previous_summary: Any,
    new_summary: SummaryBuildResult,
    tool_request: ChatLLMRequest,
    settings: ContextCompactionSettings,
) -> tuple[str, dict[str, Any]]:
    """把新摘要并入一个有界累计摘要，避免旧摘要块只取最近几块。"""
    previous_text = render_context_summary(previous_summary) or ""
    usage: dict[str, Any] = {}
    if isinstance(new_summary.usage, dict):
        _merge_usage_dict(usage, new_summary.usage)
    if not previous_text:
        return new_summary.text, usage

    merge_result = await summarize_context_text(
        (
            "【已有累计摘要】\n"
            f"{previous_text}\n\n"
            "【新增历史摘要】\n"
            f"{new_summary.text}"
        ),
        tool_request,
        scope="累计历史摘要",
        settings=settings,
        output_token_budget=resolve_summary_total_budget(settings),
    )
    if isinstance(merge_result.usage, dict):
        _merge_usage_dict(usage, merge_result.usage)
    return merge_result.text, usage


async def _collapse_existing_summary(
    summary_state: Any,
    tool_request: ChatLLMRequest,
    settings: ContextCompactionSettings,
) -> tuple[str, dict[str, Any]]:
    """把旧版多摘要块迁移为累计摘要，避免历史块只取最近几块造成丢失。"""
    previous_text = render_context_summary(summary_state) or ""
    if not previous_text:
        return "", {}
    result = await summarize_context_text(
        "【已有历史摘要】\n" + previous_text,
        tool_request,
        scope="累计历史摘要",
        settings=settings,
        output_token_budget=resolve_summary_total_budget(settings),
    )
    usage = dict(result.usage) if isinstance(result.usage, dict) else {}
    return result.text, usage


def _make_cumulative_summary_state(
    summary_text: str,
    source_round_count: int,
    recent_questions: list[str],
) -> dict[str, Any]:
    """构造新的单块累计摘要状态；原始分块事件仍保存在 JSONL 中。"""
    sections = parse_summary_sections(summary_text)
    cumulative_block = {
        **sections,
        "round_start": 1,
        "round_end": max(0, source_round_count),
    }
    return {
        **sections,
        "blocks": [cumulative_block],
        "return_blocks": 1,
        "source_round_count": max(0, source_round_count),
        "recent_questions": recent_questions,
        "recent_questions_scope": "all_history",
    }


async def compact_session_history_if_needed(
    session_chat_memory,
    tool_request: ChatLLMRequest,
    *,
    settings: ContextCompactionSettings | None = None,
    event_emitter: Any | None = None,
    budget_tokens: int | None = None,
    enforce: bool = False,
    force_all: bool = False,
) -> int:
    """按统一的跨轮流程压缩已完成的旧会话轮次。

    返回本次新压缩的轮次数。`context_summary` 仍使用字典存储元数据，
    但其中的摘要正文不再要求模型输出 JSON。

    `event_emitter` 为可选的异步回调（payload: dict -> None）：
    每个 chunk 压缩开始时推送 {"role": "assistant", "context_summary": ...}，
    完成后推送 {"role": "assistant", "summary_usage": {...}}（累计摘要随 done 返回）。

    `budget_tokens` 覆盖默认预算（聊天窗口 × trigger_ratio）。手动和摘要模式使用
    摘要总预算，自动切换到更小窗口模型时也可按实际可用空间计算预算。

    `enforce` 放宽 keep_rounds 保护；`force_all` 在此基础上要求本次把所有未覆盖
    的历史轮次都纳入累计摘要，即使它们当前尚未超过预算。模型上下文按
    HISTORY_COMPACT_KEEP_ROUNDS 总轮次窗口回传：未压缩轮次完整对话占窗口，
    已压缩轮次问题按剩余窗口保真（见 memory.chat_history_format.split_context_window）。
    """
    settings = settings or load_context_compaction_settings()
    if budget_tokens is not None and budget_tokens > 0:
        budget_limit = max(1024, int(budget_tokens))
    else:
        budget_limit = resolve_context_compaction_threshold(settings)
    # keep_rounds <= 0 表示无限窗口：不设保留下限，仅按预算阈值压缩
    keep_floor = 0 if (enforce or force_all or settings.keep_rounds <= 0) else settings.keep_rounds
    # 与 ChatMemoryManager.get_context_messages 保持一致：估算时按同样配置回传工具结果
    history_tool_result_max_length = parse_return_length(
            load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
            DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    )
    summary_state = await session_chat_memory.get_context_summary()
    history_entries = await session_chat_memory.get_chat_history()
    round_entries = [
        entry for entry in history_entries
        if isinstance(entry, dict) and entry.get("event") == "chat_round"
    ]
    stored_source_count = _summary_source_round_count(summary_state)
    if stored_source_count > len(round_entries):
        # 摘要游标不能指向不存在的轮次；丢弃旧摘要并从完整原始历史重建。
        print("[WARN] 历史摘要游标超过当前轮次数，已重置累计摘要")
        summary_state = None
        stored_source_count = 0
    summarized_count = min(stored_source_count, len(round_entries))
    raw_rounds = round_entries[summarized_count:]
    # 保真问题索引按总轮次窗口切分：只覆盖已压缩轮次（未压缩轮次以完整对话
    # 回传，其问题不重复进入索引）；编号与累计摘要 round_start/round_end 同一体系
    recent_question_items, _raw_in_window = split_context_window(
        round_entries, summarized_count, settings.keep_rounds
    )
    recent_questions = [question for _number, question in recent_question_items]

    async def sync_recent_questions() -> None:
        if not isinstance(summary_state, dict):
            return
        if (
            summary_state.get("recent_questions") == recent_questions
            and summary_state.get("recent_question_numbers") == [
                number for number, _question in recent_question_items
            ]
            and summary_state.get("recent_questions_scope") == "all_history"
        ):
            return
        updated_state = dict(summary_state)
        updated_state["recent_questions"] = recent_questions
        updated_state["recent_question_numbers"] = [
            number for number, _question in recent_question_items
        ]
        updated_state["recent_questions_scope"] = "all_history"
        await session_chat_memory.update_context_summary(updated_state)

    if not raw_rounds:
        legacy_blocks = summary_state.get("blocks") if isinstance(summary_state, dict) else None
        if isinstance(legacy_blocks, list) and len(legacy_blocks) > 1:
            collapsed_text, collapsed_usage = await _collapse_existing_summary(
                summary_state,
                tool_request,
                settings,
            )
            if collapsed_text:
                summary_state = _make_cumulative_summary_state(
                    collapsed_text,
                    summarized_count,
                    recent_questions,
                )
                await session_chat_memory.update_context_summary(summary_state)
                update_history_usage = getattr(session_chat_memory, "add_history_compression_usage", None)
                if collapsed_usage and callable(update_history_usage):
                    try:
                        await update_history_usage(collapsed_usage)
                    except Exception as exc:
                        print(f"[WARN] 累计摘要迁移 usage 落盘失败：{exc}")
        await sync_recent_questions()
        return 0
    source_budget = _resolve_compaction_source_budget(tool_request, settings)
    compacted_rounds = 0
    while raw_rounds:
        if not force_all and len(raw_rounds) <= keep_floor:
            break
        summary_message = render_context_summary(summary_state)
        context_messages: list[dict[str, Any]] = []
        if summary_message:
            context_messages.append({"role": "system", "content": summary_message})
        for round_entry in raw_rounds:
            context_messages.extend(
                round_entry_to_context_messages(round_entry, history_tool_result_max_length)
            )
        within_budget = estimate_messages_tokens(context_messages) <= budget_limit
        if not force_all and within_budget:
            break
        tail_rounds: list[dict[str, Any]] = []
        tail_tokens = estimate_messages_tokens(context_messages[:1]) if summary_message else 0
        for round_entry in reversed(raw_rounds):
            round_tokens = estimate_messages_tokens(
                round_entry_to_context_messages(round_entry, history_tool_result_max_length)
            )
            if tail_rounds and tail_tokens + round_tokens > budget_limit:
                break
            tail_rounds.append(round_entry)
            tail_tokens += round_tokens
        tail_rounds.reverse()
        if len(tail_rounds) >= len(raw_rounds):
            if not force_all:
                break
            summarize_count = _select_compaction_count(raw_rounds, source_budget)
        else:
            summarize_count = len(raw_rounds) - len(tail_rounds)
        # 每批压缩量同时受历史目标预算和压缩模型输入预算约束；force_all 模式
        # 最终会覆盖全部未摘要轮次，不会因为历史当前尚未超预算而提前停止。
        if not force_all:
            summarize_count = min(summarize_count, len(raw_rounds) - keep_floor)
        summarize_count = min(summarize_count, _select_compaction_count(raw_rounds, source_budget))
        if summarize_count <= 0:
            break
        chunk_rounds = raw_rounds[:summarize_count]
        parts: list[str] = []
        for index, round_entry in enumerate(chunk_rounds, start=1):
            parts.append(f"【待压缩历史轮次 {index}】\n{round_entry_to_compaction_text(round_entry)}")
        chunk_source = "\n\n".join(parts)
        # 压缩模型思考/正文增量 -> phase="delta" 事件（仅实时推送，不落盘）
        session_delta_emitter = None
        if event_emitter is not None:

            async def _emit_session_delta(delta: dict[str, Any]) -> None:
                await event_emitter({
                    "event": "context_compaction",
                    "scope": "session",
                    "phase": "delta",
                    **delta,
                })

            session_delta_emitter = _emit_session_delta
        if event_emitter is not None:
            try:
                await event_emitter({
                    "event": "context_compaction",
                    "scope": "session",
                    "phase": "start",
                    "role": "assistant",
                    "context_summary": (
                        f"【上下文摘要】（即将压缩 {len(chunk_rounds)} 个旧轮次）\n"
                        f"{_truncate_text_to_token_budget(chunk_source, _COMPACTION_EVENT_EXCERPT_TOKEN_BUDGET)}"
                    ),
                })
            except Exception as exc:
                print(f"[WARN] 跨轮压缩开始事件推送失败：{exc}")
        summary_result = await summarize_context_text(
            chunk_source,
            tool_request,
            scope="跨轮会话历史",
            settings=settings,
            output_token_budget=resolve_summary_total_budget(settings),
            delta_emitter=session_delta_emitter,
        )
        previous_count = _summary_source_round_count(summary_state)
        cumulative_text, cumulative_usage = await _build_cumulative_summary(
            summary_state,
            summary_result,
            tool_request,
            settings,
        )
        summary_state = _make_cumulative_summary_state(
            cumulative_text,
            previous_count + len(chunk_rounds),
            recent_questions,
        )
        await session_chat_memory.update_context_summary(summary_state)
        if cumulative_usage:
            update_history_usage = getattr(session_chat_memory, "add_history_compression_usage", None)
            if callable(update_history_usage):
                try:
                    await update_history_usage(cumulative_usage)
                except Exception as exc:
                    print(f"[WARN] 跨轮压缩 usage 落盘失败：{exc}")
        if event_emitter is not None:
            try:
                chunk_usage: dict[str, Any] = {}
                if cumulative_usage:
                    chunk_usage.update(cumulative_usage)
                chunk_usage.update({
                    "compressed_rounds": len(chunk_rounds),
                    "fallback": summary_result.used_fallback,
                    "cumulative": True,
                })
                await event_emitter({
                    "event": "context_compaction",
                    "scope": "session",
                    "phase": "done",
                    "role": "assistant",
                    # 累计摘要全文随 done 事件推送并落盘，刷新后仍可完整回放
                    "summary_text": cumulative_text,
                    "summary_usage": chunk_usage,
                })
            except Exception as exc:
                print(f"[WARN] 跨轮压缩完成事件推送失败：{exc}")
        raw_rounds = raw_rounds[summarize_count:]
        compacted_rounds += summarize_count
        # 游标推进后按新游标重算保真问题索引（只覆盖已压缩轮次，
        # 未压缩轮次的问题不重复进入索引）
        recent_question_items, _raw_in_window = split_context_window(
            round_entries, _summary_source_round_count(summary_state), settings.keep_rounds
        )
        recent_questions = [question for _number, question in recent_question_items]
        print(
            f"[INFO] 已压缩 {len(chunk_rounds)} 个旧会话轮次"
            f"（累计 {summary_state['source_round_count']}，累计摘要 1 块，"
            f"降级={summary_result.used_fallback}）"
        )
    await sync_recent_questions()
    return compacted_rounds


def _find_current_round_start(messages: list[dict[str, Any]]) -> int | None:
    # 内部“继续”提示不应取代原始用户问题，优先定位最后一条非内部 user 消息。
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if not message.get("_internal"):
            return index
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return index
    return None


def _has_tool_trace(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(message, dict)
        and (message.get("role") == "tool" or bool(message.get("tool_calls")))
        for message in messages
    )


def _count_tool_result_messages(messages: list[dict[str, Any]]) -> int:
    """统计消息中的工具结果数量，用作单轮压缩的逻辑游标。"""
    return sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    )


def _split_previous_round_summaries(
    prefix: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从消息前缀中拆出本轮已生成的摘要块与其余消息。

    返回 (retained, blocks)：blocks 每项为 {"content": 摘要文本, "index": 覆盖游标}，
    按时间顺序排列（最老在前）；提取时去掉“【本轮已执行工具摘要】”渲染前缀，
    只保留纯摘要正文。旧格式标记消息没有游标字段时 index 记 0。
    """
    retained: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    for message in prefix:
        if (
            isinstance(message, dict)
            and message.get(_ROUND_SUMMARY_MARKER) == _ROUND_SUMMARY_SCOPE
        ):
            text = _to_text(message.get("content"))
            if text.startswith("【本轮已执行工具摘要】"):
                text = text[len("【本轮已执行工具摘要】"):].lstrip("\n").strip()
            if not text:
                continue
            try:
                block_index = int(message.get(_ROUND_SUMMARY_INDEX_MARKER, 0) or 0)
            except (TypeError, ValueError):
                block_index = 0
            blocks.append({"content": text, "index": max(0, block_index)})
            continue
        retained.append(message)
    return retained, blocks


def _round_messages_to_compaction_text(
    previous_summaries: list[str],
    messages: list[dict[str, Any]],
) -> str:
    tool_names_by_call_id: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = _to_text(tool_call.get("id"))
            function_info = tool_call.get("function")
            tool_name = function_info.get("name") if isinstance(function_info, dict) else ""
            if call_id and tool_name:
                tool_names_by_call_id[call_id] = str(tool_name)
    lines: list[str] = []
    if previous_summaries:
        lines.append("【此前本轮摘要】")
        lines.extend(previous_summaries)
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = _to_text(message.get("content"))
        if role == "user" and content:
            lines.append(f"【用户】{content}")
        elif role == "assistant":
            if content:
                lines.append(f"【助手】{content}")
            for tool_call in message.get("tool_calls") or []:
                tool_line = format_tool_call_history_line(tool_call)
                if tool_line:
                    lines.append(tool_line)
        elif role == "tool":
            call_id = _to_text(message.get("tool_call_id"))
            tool_name = (
                tool_names_by_call_id.get(call_id)
                or _to_text(message.get("_tool_name"))
                or _to_text(message.get("name"))
                or "未知工具"
            )
            if content:
                lines.append(f"【工具结果：{tool_name}】\n{content}")
    return "\n\n".join(lines) or "本轮未能提取到可压缩的文本。"


async def compact_active_round_context_if_needed(
    messages: list[dict[str, Any]],
    tool_request: ChatLLMRequest,
    *,
    settings: ContextCompactionSettings | None = None,
    compression_index: int | None = None,
    event_emitter: Any | None = None,
) -> RoundContextCompactionResult:
    """在下一次模型调用前压缩当前任务已执行的工具轨迹。

    仅在至少包含一次工具调用/工具结果时替换轨迹，避免破坏正常聊天消息顺序。
    当前用户问题会被保留；本轮摘要按段累积为一个累计摘要块（每段先压缩新轨迹，
    再与旧摘要合并）。返回的累计摘要文本与 `compress_index` 会随 done 事件写入当前
    `chat_round.events`，供后续历史重建跳过已覆盖轨迹。

    `event_emitter` 为可选的异步回调（payload: dict -> None）：
    压缩开始时推送 {"role": "assistant", "compress_context": ...}，
    压缩完成后推送 {"role": "assistant", "compress_usage": {...}}，
    累计摘要合并不单独推送事件，合并后的摘要随 done 事件返回。
    """
    settings = settings or load_context_compaction_settings()
    original_messages = list(messages)
    before_tokens = estimate_request_context_tokens(original_messages, tool_request.tools)
    token_limit = resolve_context_compaction_threshold(settings)
    if token_limit <= 0 or before_tokens <= token_limit:
        return RoundContextCompactionResult(
            messages=original_messages,
            triggered=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
    round_start = _find_current_round_start(original_messages)
    if round_start is None:
        print("[WARN] 当前请求没有 user 消息，跳过单轮上下文压缩")
        return RoundContextCompactionResult(
            messages=original_messages,
            triggered=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
    active_messages = original_messages[round_start:]
    if not _has_tool_trace(active_messages):
        return RoundContextCompactionResult(
            messages=original_messages,
            triggered=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
    # 防空转：before_tokens 是全量上下文（含历史）。若超窗主要来自历史而非本轮轨迹
    # （本轮轨迹不足阈值一半），本轮压缩压不掉历史，只会空转消耗压缩模型调用；
    # 此类场景留给跨轮压缩（按轮数 > keep_rounds 触发）与首调用预算检查处理。
    active_tokens = estimate_messages_tokens(active_messages)
    if active_tokens <= max(256, token_limit // 2):
        print(
            f"[INFO] 单轮上下文压缩跳过：本轮轨迹 {active_tokens} tokens 未达阈值一半"
            f"（{token_limit}），超窗主要来自历史上下文"
        )
        return RoundContextCompactionResult(
            messages=original_messages,
            triggered=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
    retained_prefix, previous_blocks = _split_previous_round_summaries(
        original_messages[:round_start]
    )
    # 旧格式可能包含多个本轮摘要块；先合并为一个输入，之后统一保存为累计块。
    previous_summary_text = "\n\n".join(
        _to_text(block.get("content")) for block in previous_blocks if _to_text(block.get("content"))
    )
    previous_summary = {"summary": previous_summary_text} if previous_summary_text else None
    blocks: list[dict[str, Any]] = []
    summary_budget = resolve_summary_total_budget(settings)
    merged_usage: dict[str, Any] = {}
    compaction_source = _round_messages_to_compaction_text([], active_messages)
    # 压缩模型思考/正文增量 -> phase="delta" 事件（仅实时推送，不落盘）
    round_delta_emitter = None
    if event_emitter is not None:

        async def _emit_round_delta(delta: dict[str, Any]) -> None:
            await event_emitter({
                "event": "context_compaction",
                "scope": "round",
                "phase": "delta",
                **delta,
            })

        round_delta_emitter = _emit_round_delta
    if event_emitter is not None:
        try:
            await event_emitter({
                "event": "context_compaction",
                "scope": "round",
                "phase": "start",
                "role": "assistant",
                "compress_context": (
                    "【上下文摘要】\n"
                    f"{_truncate_text_to_token_budget(compaction_source, _COMPACTION_EVENT_EXCERPT_TOKEN_BUDGET)}"
                ),
            })
        except Exception as exc:
            print(f"[WARN] 单轮压缩开始事件推送失败：{exc}")
    summary_result = await summarize_context_text(
        compaction_source,
        tool_request,
        scope="单轮工具执行上下文",
        settings=settings,
        output_token_budget=summary_budget,
        delta_emitter=round_delta_emitter,
    )
    preserved_user_messages = [
        message for message in active_messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    if not preserved_user_messages:
        return RoundContextCompactionResult(
            messages=original_messages,
            triggered=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
    try:
        resolved_compression_index = int(compression_index)
    except (TypeError, ValueError):
        resolved_compression_index = 0
    if resolved_compression_index <= 0:
        resolved_compression_index = _count_tool_result_messages(active_messages)
    previous_max_index = max((int(block.get("index") or 0) for block in previous_blocks), default=0)
    resolved_compression_index = max(resolved_compression_index, previous_max_index)
    cumulative_text, cumulative_usage = await _build_cumulative_summary(
        previous_summary,
        summary_result,
        tool_request,
        settings,
    )
    blocks = [{
        "content": cumulative_text,
        "index": resolved_compression_index,
    }]
    summary_messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": f"【本轮已执行工具摘要】\n{cumulative_text}",
        _ROUND_SUMMARY_MARKER: _ROUND_SUMMARY_SCOPE,
        _ROUND_SUMMARY_INDEX_MARKER: resolved_compression_index,
    }]
    # 兼容部分只接受“system 消息位于开头”的 Chat Completions 实现：
    # 将临时摘要插入所有前导 system 消息之后，而不是插在旧 assistant 与当前 user 之间。
    leading_system_count = 0
    for message in retained_prefix:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        leading_system_count += 1
    compacted_messages = (
        retained_prefix[:leading_system_count]
        + summary_messages
        + retained_prefix[leading_system_count:]
        + preserved_user_messages
    )
    after_tokens = estimate_request_context_tokens(compacted_messages, tool_request.tools)
    merged_usage.update(cumulative_usage)
    summary_text = cumulative_text
    print(
        f"[INFO] 单轮工具上下文已压缩：{before_tokens} -> {after_tokens} tokens"
        f"（阈值 {token_limit}，摘要预算 {summary_budget}，"
        f"累计块 1，降级={summary_result.used_fallback}）"
    )
    if event_emitter is not None:
        try:
            usage_payload: dict[str, Any] = {}
            if cumulative_usage:
                usage_payload.update(cumulative_usage)
            usage_payload.update({
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "fallback": summary_result.used_fallback,
                "block_count": 1,
                "cumulative": True,
            })
            # 摘要全文随 done 事件推送并落盘（同一 payload），前端刷新后仍可在
            # 压缩块中回放摘要正文；思考过程只在 delta 阶段实时展示、不落盘。
            await event_emitter({
                "event": "context_compaction",
                "scope": "round",
                "phase": "done",
                "role": "assistant",
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "compress_index": resolved_compression_index,
                "block_count": 1,
                "summary_text": cumulative_text,
                "compress_usage": usage_payload,
            })
        except Exception as exc:
            print(f"[WARN] 单轮压缩完成事件推送失败：{exc}")
    return RoundContextCompactionResult(
        messages=compacted_messages,
        triggered=True,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        used_fallback=summary_result.used_fallback,
        summary_text=summary_text,
        summary_blocks=blocks,
        compress_index=resolved_compression_index,
        usage=merged_usage or summary_result.usage,
    )
