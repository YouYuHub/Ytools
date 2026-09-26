"""子智能体（sub agent）运行时：SubAgentContext + SubAgentRunner。

设计依据：docs/sub_agent_v1.md。核心原则：

- 共享的是不可变配置与无状态纯函数，隔离的是可变执行态：
  SubAgentContext 在派发时刻固化工具快照/限制/回调/取消令牌；子任务私有的
  messages/todo/usage/rounds 全部收敛在 Runner 实例属性内，不写全局。
- 层级取消：父级停止（stop_checker）→ 取消全部子任务；单个子任务超时/
  轮次上限/流错误 → 只终止自己，不向父级传播。父级 wait_for 超时后由
  run_sub_agent_batch 兜底补写 done 事件（runner.done_emitted 防重复）。
- 子任务不直接持有 chat_memory：所有事件经 emit_event 回调进入父循环
  （父循环负责 timestamp 补齐 + SSE 推送 + JSONL 落盘，写入发生在事件循环
  的同步守卫块内，禁止工作线程写历史）。
- 复用父循环同款基建：parse_sse_event / merge_tool_call_delta /
  copy_for_request（思考回传契约）/ execute_tool_round（线程池 MCP 执行）/
  超大结果拒绝阈值与反馈文案，保证父/子循环行为一致、不发生漂移。
"""
# from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import (
    get_model_config,
    get_role_headers,
    get_role_parameter,
    get_role_selection,
    inject_custom_headers_into_config,
    load_var,
)
from util.timestamp_utils import now_str
from memory import file_history as _file_history
from memory.file_memory import retire_native_document_parts

from factory.agent_runtime.builtin_tools import (
    ASK_USER_TOOL_NAME,
    CHECK_TOOL_EXISTS_NAME,
    RUN_COMMAND_NAME,
    READ_MEDIA_NAME,
    READ_DOCUMENT_NAME,
    SUB_AGENT_TOOL_NAME,
    TODO_TOOL_NAME,
    execute_ask_user_placeholder,
    execute_builtin_tool,
    execute_read_media,
    execute_read_document,
    load_any_media_model_part,
    normalize_todo_items,
    roll_recent_media_parts,
    retire_prior_video_parts,
    cap_video_parts_in_batch,
    try_execute_builtin_command_tool,
    try_execute_builtin_file_tool,
)
from factory.agent_runtime.chat_runtime import (
    UsageAccumulator,
    build_request_messages,
    copy_for_request,
    estimate_request_context_tokens,
    estimate_send_context_tokens,
    estimate_text_tokens,
    load_tool_call_stream_timeout,
    merge_function_call_delta,
    merge_tool_call_delta,
    parse_sse_event,
    resolve_config_max_input_tokens,
    resolve_model_max_input_tokens,
)
from factory.agent_runtime.context_compaction import (
    build_oversized_tool_feedback,
    load_context_compaction_settings,
    make_oversized_result_preview,
    resolve_oversized_result_token_threshold,
)
from factory.agent_runtime.tool_executor import (
    execute_tool_round,
    normalize_tool_calls,
    prepare_tool_execution,
)


@dataclass
class SubAgentContext:
    """子任务派发配置（不可变；派发时刻固化，子任务内不随外部变化）。"""

    # 身份
    agent_id: str
    parent_agent_id: str          # V1 恒 "main"
    parent_tool_call_id: str
    parent_tool_index: int        # 父级 parsed_tools 枚举 index（tool_results 排序用）
    agent_index: int              # 本轮第几个子任务（0 起，展示用）
    session_id: str
    # 任务
    task: str
    initial_todo: list[dict[str, Any]] | None
    # 工具（父级本轮快照，已剔除 sub_agent；ask_user 替换为占位定义）
    tools: list[dict[str, Any]]
    tool_servers: Dict[str, str]
    configured_tool_names: set[str]
    configured_tool_servers: Dict[str, str]
    # 限制
    max_rounds: int
    timeout_seconds: float
    reply_max_chars: int
    # 回调（父循环注入）
    emit_event: Callable[[dict[str, Any]], Awaitable[None]]  # JSONL 追加 + SSE 推送
    stop_checker: Callable[[], bool]                          # 父级停止信号查询
    # 父级当轮聊天模型的视觉能力（派发时刻固化）：子任务 read_media 可用性
    # 判定优先用父级传入值；sub_agent_model 独立子模型配置可在执行时再覆盖判定
    parent_vision_enabled: bool | None = None
    # V2 文件版本链：父级会话轮次号（子任务内文件工具入链标注 round 用）
    parent_round_number: int = 0
    # 取消令牌：要求本子任务停止时 set()
    cancel_token: asyncio.Event = field(default_factory=asyncio.Event)

    def stop_requested(self) -> bool:
        """本子任务是否被要求停止（父级停止 或 取消令牌置位）。"""
        try:
            parent_stop = bool(self.stop_checker())
        except Exception:
            parent_stop = False
        return parent_stop or self.cancel_token.is_set()


@dataclass
class SubAgentResult:
    agent_id: str
    parent_tool_call_id: str
    status: str          # done|error|stopped|interrupted|timeout|max_rounds
    final_reply: str     # 返回给父级的文本（超时/中断时为"进展+未完成原因"说明）
    rounds: int
    usage_total: dict[str, Any]
    error: str | None


def new_agent_id(existing: set[str] | None = None) -> str:
    """生成 agent_<8hex> 子智能体实例 ID；提供 existing 时保证轮内唯一。"""
    used = existing or set()
    for _ in range(16):
        candidate = f"agent_{uuid.uuid4().hex[:8]}"
        if candidate not in used:
            return candidate
    # 16 次仍冲突（理论上不可能）：退化为完整 hex
    candidate = f"agent_{uuid.uuid4().hex}"
    while candidate in used:
        candidate = f"agent_{uuid.uuid4().hex}"
    return candidate


def load_sub_agent_limits() -> dict[str, Any]:
    """实时读取 sub_agent 配置（load_var 口径与全仓一致）。"""
    from config import (
        DEFAULT_SUB_AGENT_MAX_CONCURRENT,
        DEFAULT_SUB_AGENT_MAX_ROUNDS,
        DEFAULT_SUB_AGENT_REPLY_MAX_CHARS,
        DEFAULT_SUB_AGENT_TIMEOUT_SECONDS,
    )

    def _int(name: str, default: int, minimum: int) -> int:
        try:
            value = int(load_var(name, default))
        except (TypeError, ValueError):
            return int(default)
        return value if value >= minimum else int(default)

    max_rounds = _int("SUB_AGENT_MAX_ROUNDS", DEFAULT_SUB_AGENT_MAX_ROUNDS, 1)
    max_concurrent = _int("SUB_AGENT_MAX_CONCURRENT", DEFAULT_SUB_AGENT_MAX_CONCURRENT, 1)
    reply_max_chars = _int("SUB_AGENT_REPLY_MAX_CHARS", DEFAULT_SUB_AGENT_REPLY_MAX_CHARS, 256)
    try:
        timeout_seconds = float(load_var("SUB_AGENT_TIMEOUT_SECONDS", DEFAULT_SUB_AGENT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout_seconds = float(DEFAULT_SUB_AGENT_TIMEOUT_SECONDS)
    return {
        "max_rounds": max_rounds,
        "max_concurrent": max_concurrent,
        "timeout_seconds": timeout_seconds,
        "reply_max_chars": reply_max_chars,
    }


def load_sub_agent_retry_limits() -> dict[str, int]:
    """读取子智能体交付保障重试配置（.env 可配，前端聊天设置可改）。

    - final_reply_max_attempts：空收尾重试上限（0/负=不限制）——
      子任务收尾轮最终回复为空时注入内部消息让子模型再次交付；
    - stream_error_max_attempts：流式错误断点续跑上限（0/负=不限制）——
      模型调用出错后注入内部消息从已有进度继续（工具轨迹/todo 不丢失）；
    - todo_remind_max：todo 未完成提醒上限（0=关闭，负=不限制）——
      只对「模型创建/更新过的计划」生效（父级预置但从未被触碰的计划
      不拦截有效交付）；每次完整工具执行轮后额度重置。

    三项全部受 max_rounds/timeout 双重硬封顶，配置为不限制也不会失控。
    """
    from config import (
        DEFAULT_SUB_AGENT_FINAL_REPLY_RETRY_MAX,
        DEFAULT_SUB_AGENT_STREAM_ERROR_RETRY_MAX,
        DEFAULT_SUB_AGENT_TODO_REMIND_MAX,
    )

    def _int(name: str, default: int) -> int:
        try:
            return int(load_var(name, default))
        except (TypeError, ValueError):
            return int(default)

    return {
        "final_reply_max_attempts": _int(
            "SUB_AGENT_FINAL_REPLY_RETRY_MAX", DEFAULT_SUB_AGENT_FINAL_REPLY_RETRY_MAX
        ),
        "stream_error_max_attempts": _int(
            "SUB_AGENT_STREAM_ERROR_RETRY_MAX", DEFAULT_SUB_AGENT_STREAM_ERROR_RETRY_MAX
        ),
        "todo_remind_max": _int(
            "SUB_AGENT_TODO_REMIND_MAX", DEFAULT_SUB_AGENT_TODO_REMIND_MAX
        ),
    }


def _merge_usage_values(total: dict[str, Any], delta: dict[str, Any]) -> None:
    if not isinstance(delta, dict):
        return
    for key, value in delta.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            existed = total.get(key, 0)
            if not isinstance(existed, (int, float)) or isinstance(existed, bool):
                existed = 0
            total[key] = existed + value
        elif isinstance(value, dict):
            child = total.get(key)
            if not isinstance(child, dict):
                child = {}
                total[key] = child
            _merge_usage_values(child, value)


def _collect_usage(accumulator: UsageAccumulator) -> dict[str, Any]:
    total: dict[str, Any] = {}
    accumulator.merge_to(total, _merge_usage_values)
    return total


def _format_result_text(result: Any) -> str:
    if isinstance(result, dict):
        # 内置文件工具的展示用 diff 与版本链入链数据：只推送前端与落盘，不进入子模型上下文
        result = {
            k: v for k, v in result.items()
            if k not in ("_file_diff", "_file_history")
        }
        # 原生文档块（read_document 返回）：数据本体为 base64，绝不能进入
        # 模型文本上下文或 JSONL；只保留可读的注入说明
        native_block = result.pop("native", None)
        if isinstance(native_block, dict):
            filename = str(native_block.get("filename") or "")
            result["native_document"] = (
                f"{filename} 已作为原生文档注入后续请求" if filename
                else "原生文档已注入后续请求"
            )
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


# 子任务任务文本中的 media:// 引用提取（子任务 read_media 安全边界数据源）：
# 父模型派发任务时会把可回读的媒体引用写进 task 文本（如「读取 media://a.png」），
# 子模型只允许读取这些引用（与父级"当前任务引用集"口径一致——子任务上下文中
# 没有其他来源），非引用（本地路径/http 直链）不受此过滤限制
_MEDIA_REFERENCE_IN_TEXT_RE = re.compile(r"media://[^\s<>()\"'`。，！？；、）】》]+")

READ_MEDIA_REJECTION_MESSAGE = (
    "当前子任务模型不支持视觉（vision=false），read_media 工具不可用、"
    "不要再次调用；请在最终回复中说明无法查看媒体内容。"
)


def resolve_sub_agent_vision_enabled(parent_vision_enabled: bool | None) -> bool:
    """解析子任务的视觉能力（实际执行子任务的模型为准）。

    - sub_agent_model 已配置为独立模型且配置有效（存在、协议为
      chat-completions，与 _resolve_request_model_config 回退链一致）
      → 按该模型 vision 判定——子模型不支持视觉时，无论父级是否支持/
        是否启用 read_media，子任务都不应看到该工具；
    - 未配置/配置失效/协议不符 → 继承父级聊天模型口径（parent_vision_enabled；
      未传 None 时保守视为支持）。

    判定失败（异常/配置缺失）保守视为支持，避免误禁工具（与工厂层保守口径一致）。
    """
    try:
        selection = get_role_selection("sub_agent_model") or {}
        provider = selection.get("ownership_name")
        model = selection.get("model_name")
        if provider and model:
            chat_config = get_model_config(str(provider), str(model))
            if (
                isinstance(chat_config, dict)
                and str(chat_config.get("apiType") or "chat-completions").casefold()
                == "chat-completions"
            ):
                return bool(chat_config.get("vision", False))
    except Exception:
        pass
    return parent_vision_enabled if parent_vision_enabled is not None else True


def _collect_task_media_references(task: str) -> list[str]:
    """按出现顺序收集任务文本中的 media:// 引用（去重）。

    子任务的安全边界数据源：子任务上下文完全独立（只有 task 一条 user 消息），
    能看到的媒体引用只可能出现在任务文本里——父模型据此引用派发，子模型
    读取该集合之外的 media:// 引用没有意义（也防止任务文本被注入引用做越权探测）。
    """
    if not isinstance(task, str):
        return []
    refs: list[str] = []
    for match in _MEDIA_REFERENCE_IN_TEXT_RE.finditer(task):
        ref = match.group(0).rstrip("。，！？;；,)")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _format_initial_todo_hint(items: list[dict[str, Any]]) -> str:
    """把父级预置计划渲染为注入文案（随第二条 user 消息进入子模型上下文）。

    预置计划过去只存在于 Runner 内部状态：收尾时才被突击检查「计划未完成」，
    而子模型对此毫不知情（现场表现：模型困惑地补一次 todo_write 再重复交付）。
    注入后计划可见，与收尾处「未触碰的预置计划不拦截有效交付」规则配套。
    """
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("id") or "").strip()
        if not content:
            continue
        status = str(item.get("status") or "pending")
        marker = {"done": "[已完成]", "in_progress": "[进行中]"}.get(status, "[待办]")
        lines.append(f"{index}. {marker} {content}")
    if not lines:
        return ""
    return (
        "【预置计划】父智能体为本任务预置了以下参考计划（可参照推进，"
        "也可按实际情况调整；如需更新进度或调整计划，请调用 todo 工具"
        "全量提交列表）：\n"
        + "\n".join(lines)
        + "\n任务完成后直接给出最终回复即可。"
    )


def _tool_definition_name(tool_def: Any) -> str:
    """从工具定义 dict 取函数名（畸形定义返回空串）。"""
    if not isinstance(tool_def, dict):
        return ""
    function_info = tool_def.get("function")
    if isinstance(function_info, dict):
        return str(function_info.get("name") or "")
    return str(tool_def.get("name") or "")


class SubAgentRunner:
    """单个子任务的精简 Agent 循环（职责划分对齐 V2 的 AgentRunner 设计）。"""

    def __init__(self, context: SubAgentContext):
        self.context = context
        # 任务文本必须注入为首轮 user 消息：子任务上下文完全独立、无系统
        # 提示词兜底，不注入则首轮请求 messages 为空，模型只会闲聊收尾，
        # 且"未执行任何任务"会被误标为 done（回归防护）。
        self.messages: list[dict[str, Any]] = [
            {"role": "user", "content": context.task},
        ]
        self.todo: list[dict[str, Any]] = list(context.initial_todo or [])
        # 父级预置计划必须对子模型可见：过去它只存在于 Runner 内部状态，
        # 收尾时的「计划未完成」提醒对模型而言凭空出现——现场表现是模型
        # 困惑地补一次 todo_write 再重复交付一遍。注入为第二条 user 消息
        #（_internal 仅本地标记，构造请求前剥离；配合收尾处「未触碰的
        # 预置计划不拦截有效交付」规则，见 _run_inner 收尾分支）。
        if self.todo:
            todo_hint = _format_initial_todo_hint(self.todo)
            if todo_hint:
                self.messages.append({
                    "role": "user",
                    "content": todo_hint,
                    "_internal": True,
                })
        self.usage_accumulator = UsageAccumulator()
        self.rounds = 0
        self.started_at = now_str()
        self._started_monotonic = time.monotonic()
        self.done_emitted = False
        self._seq = 0
        self._status = "done"
        self._error: str | None = None
        self._final_reply = ""
        self._consecutive_oversized = 0
        # 交付保障重试计数（配置见 load_sub_agent_retry_limits）：
        # - 空收尾重试 / 流错误续跑：累计计数，耗尽按 error 收尾；
        # - todo 提醒：每次完整工具执行轮后重置（工具轮后又"忘记"可再次提醒）
        self._final_reply_retries_used = 0
        self._stream_error_retries_used = 0
        self._todo_remind_used = 0
        # 计划是否被模型触碰过（成功调用 todo_write 即置位）：收尾提醒只
        # 约束「模型知情且维护过」的计划；父级预置但从未被触碰的计划属隐藏
        # 状态，不拦截有效交付（否则模型完成工作后会被迫补计划再重复交付）
        self._todo_touched = False
        self._retry_limits = load_sub_agent_retry_limits()
        # read_media 数据注入坐标（子任务版，与父循环同语义）：
        # pending_parts 为 (reference, quality, start_time, end_time) 坐标，
        # 工具结果统一处理后现场加载 base64 部件注入为 user 消息（仅内存，
        # 不落盘）；视频区间读取的坐标含区间；injected_refs 防重复读取
        # （键带区间：同视频不同区间是新的读取）；滚动窗口只保留最近 5 个
        self.read_media_pending_parts: list[tuple[str, Any]] = []
        self.read_media_injected_refs: set[str] = set()
        # read_document 原生分支待注入部件（模型支持该文档类型时）
        self.native_doc_pending_parts: list[Any] = []
        # 任务文本中的 media:// 引用集合：子任务 read_media 的安全边界数据源
        #（父模型派发任务时把可回读引用写进 task，子模型只能读这些）
        self.task_media_references: set[str] = set(
            _collect_task_media_references(context.task)
        )
        # 子任务模型的视觉能力（派发时由父级传入；None=派发方未判定，执行时兜底解析）
        self._vision_enabled: bool | None = getattr(
            context, "parent_vision_enabled", None
        )

    def _visible_tool_definitions(self) -> list[dict[str, Any]]:
        """当前子任务可见的工具定义。

        vision=false（子任务模型不支持视觉）时剔除 read_media 定义——
        工具 schema 一旦出现在请求里，模型就会知道并可能调用；不看请求上下文
        直接拒绝的方式在这里行不通（拒绝也是模型「知道」的一种）。与父级
        「不支持视觉时不注入 read_media」同口径。
        """
        tools = list(self.context.tools)
        if self._vision_enabled is False:
            tools = [
                tool_def for tool_def in tools
                if _tool_definition_name(tool_def) != READ_MEDIA_NAME
            ]
        return tools

    # ----------------------------- 事件发射 -----------------------------
    async def _emit(self, phase: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "event": "sub_agent",
            "agent_id": self.context.agent_id,
            "parent_agent_id": self.context.parent_agent_id,
            "parent_tool_call_id": self.context.parent_tool_call_id,
            "agent_index": self.context.agent_index,
            "phase": phase,
            **fields,
        }
        try:
            await self.context.emit_event(payload)
        except Exception as exc:  # 事件发射失败不拖垮子任务
            print(
                f"[WARN] sub_agent 事件发射失败"
                f"（agent_id={self.context.agent_id}, phase={phase}）: {exc}"
            )

    def _record_file_history(self, raw_result: dict[str, Any]) -> None:
        """V2 版本链：子任务内文件工具变更入链（同父级消费点语义，失败静默）。"""
        payload = raw_result.get("_file_history")
        if not isinstance(payload, dict) or not payload.get("path"):
            return
        try:
            _file_history.record_change(
                self.context.session_id,
                path=str(payload["path"]),
                display_path=str(payload.get("display_path") or payload["path"]),
                old_text=payload.get("old_text"),
                new_text=payload.get("new_text"),
                tool="sub_agent." + (payload.get("tool") or "file_tool"),
                round_number=int(self.context.parent_round_number or 0),
                encoding=str(payload.get("encoding") or "utf-8"),
            )
        except Exception as record_error:
            print(f"[WARN] 子任务文件版本链记录失败（不影响子任务）: {record_error}")

    async def _emit_start(self) -> None:
        await self._emit(
            "start",
            task=self.context.task,
            todo=self.todo or None,
            tools=[
                name for name in self.context.tool_servers
                if name != CHECK_TOOL_EXISTS_NAME
                and not (self._vision_enabled is False and name == READ_MEDIA_NAME)
            ],
            rounds_limit=self.context.max_rounds,
            timeout_seconds=self.context.timeout_seconds,
        )

    async def _emit_done(self, status: str, final_reply: str, error: str | None) -> None:
        if self.done_emitted:
            return
        self.done_emitted = True
        usage_total: dict[str, Any] = {}
        self.usage_accumulator.merge_to(usage_total, _merge_usage_values)
        await self._emit(
            "done",
            status=status,
            final_reply=final_reply,
            rounds=self.rounds,
            usage_total=usage_total,
            error=error,
            ended_at=now_str(),
        )

    # ----------------------------- 模型调用 -----------------------------
    def _resolve_request_model_config(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """解析子任务模型配置（docs/sub_agent_v1.md §6.5 模型配置）。

        返回 (model_config | None, parameter)：
        - model_selection.sub_agent_model 已配置且模型仍存在、协议为
          chat-completions → (该角色配置, sub_agent_model 角色参数)；
        - 未配置/配置失效/协议不符 → (None, chat_model 角色参数)：
          model_config=None 使 ChatLLM 走父级聊天模型解析链（ambient 会话
          覆盖已由 asyncio 任务上下文自动继承），子任务与父级共用同一模型；
        - 前端可按会话覆盖该角色（_meta.model_selection.sub_agent_model），
          本方法实时读取，每次模型调用都取最新配置。
        """
        try:
            selection = get_role_selection("sub_agent_model")
        except Exception:
            selection = {}
        provider = selection.get("ownership_name")
        model = selection.get("model_name")
        if provider and model:
            chat_config = get_model_config(str(provider), str(model))
            if chat_config is not None and str(chat_config.get("apiType") or "chat-completions").casefold() == "chat-completions":
                try:
                    parameter = get_role_parameter("sub_agent_model") or {}
                except Exception:
                    parameter = {}
                # 子智能体角色的自定义请求头随配置注入（ChatLLM 构造 HTTP 时读取 _custom_headers）
                return inject_custom_headers_into_config(
                    chat_config, get_role_headers("sub_agent_model")
                ), parameter
            print(
                f"[WARN] 子智能体模型配置无效或协议不支持"
                f"（{provider} / {model}），子任务回退父级聊天模型"
            )
        # 未配置：继承父级聊天模型的生成参数（模型本身由 ambient 覆盖链解析）
        try:
            parameter = get_role_parameter("chat_model") or {}
        except Exception:
            parameter = {}
        return None, parameter

    def _native_doc_types(self) -> list[str]:
        """子任务生效模型的原生文档能力（supportDocTypes 归一化列表）。

        口径与子任务模型解析一致：sub_agent_model 角色配置有效时取该模型，
        否则继承父级聊天模型（ambient 会话覆盖已由 asyncio 任务上下文继承）。
        解析失败返回空列表（read_document 走纯文本分页口径）。
        """
        try:
            model_config, _parameter = self._resolve_request_model_config()
            if model_config is None:
                from env_manager import require_default_chat_config

                model_config = require_default_chat_config()
            if not isinstance(model_config, dict):
                return []
            support = model_config.get("support_doc_types")
            return [str(item) for item in support] if isinstance(support, list) else []
        except Exception as exc:
            print(f"[WARN] 子任务 supportDocTypes 解析失败（按不支持处理）: {exc}")
            return []

    def _build_request(self, parameter: dict[str, Any]) -> ChatLLMRequest:
        """构造子任务请求：参数填充优先级 sub_agent_model.parameter > 内置默认。

        ChatLLMRequest 内置默认（temperature 0.7 / top_p 1.0 / presence_penalty
        2.0 / reasoning_effort medium / max_tokens 8192）与父级入口一致；角色
        parameter（分桶后的生效参数）按字段覆盖。非标准参数（enable_thinking
        等）走 extra_body。
        """
        request = ChatLLMRequest(
            messages=build_request_messages(copy_for_request(self.messages)),
            # 按模型可见性过滤后的工具（vision=false 时不含 read_media，
            # 请求里不出现该 schema，模型就无从知道并调用）
            tools=self._visible_tool_definitions() or None,
            session_id=self.context.session_id,
        )
        if "temperature" in parameter:
            try:
                request.temperature = float(parameter["temperature"])
            except (TypeError, ValueError):
                pass
        if "top_p" in parameter:
            try:
                request.top_p = float(parameter["top_p"])
            except (TypeError, ValueError):
                pass
        if "presence_penalty" in parameter:
            try:
                request.presence_penalty = float(parameter["presence_penalty"])
            except (TypeError, ValueError):
                pass
        if "max_tokens" in parameter:
            try:
                request.max_tokens = int(parameter["max_tokens"])
            except (TypeError, ValueError):
                pass
        if "reasoning_effort" in parameter:
            request.reasoning_effort = str(parameter["reasoning_effort"] or request.reasoning_effort)
        extra_body = parameter.get("extra_body")
        if isinstance(extra_body, dict) and extra_body:
            request.extra_body = dict(extra_body)
        return request

    async def _model_call(self) -> dict[str, Any]:
        """一次流式模型调用；返回本轮聚合结果（正文/思考/工具调用/finish_reason）。"""
        ctx = self.context
        full_response = ""
        full_reasoning = ""
        tool_calls: list[dict] = []
        finish_reason: str | None = None
        stream_error: Any = None
        usage = self.usage_accumulator
        tool_call_stream_timeout = load_tool_call_stream_timeout()
        tool_phase_deadline: float | None = None
        tool_phase_timeout_hit = False
        # 上游提前断开检测（EOF 未收到 finish_reason，ChatLLM 补发标记帧）：
        # 截断续写重试——落盘部分内容 + 内部消息让子模型继续（与父循环同语义）
        stream_truncated = False
        model_config, parameter = self._resolve_request_model_config()
        request = self._build_request(parameter)
        sse_iter = ChatLLM.chat_completions(
            request=request,
            stream=True,
            stop_checker=lambda: ctx.stop_requested(),
            model_config=model_config,
        ).__aiter__()
        try:
            while True:
                if tool_phase_deadline is None or tool_call_stream_timeout <= 0:
                    try:
                        sse_chunk = await sse_iter.__anext__()
                    except StopAsyncIteration:
                        break
                else:
                    remaining = tool_phase_deadline - time.monotonic()
                    if remaining <= 0:
                        tool_phase_timeout_hit = finish_reason is None
                        break
                    try:
                        sse_chunk = await asyncio.wait_for(sse_iter.__anext__(), timeout=remaining)
                    except asyncio.TimeoutError:
                        tool_phase_timeout_hit = finish_reason is None
                        break
                    except StopAsyncIteration:
                        break
                if tool_phase_deadline is not None and tool_call_stream_timeout > 0:
                    tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
                event = parse_sse_event(sse_chunk)
                if event is None:
                    continue
                if event.get("stream_truncated"):
                    # 上游提前断开（EOF 未收到 finish_reason）：置标记后跳出，
                    # 走下方"截断续写重试"路径，不按正常收尾处理
                    stream_truncated = True
                    break
                if event.get("done") or ctx.stop_requested():
                    break
                if event.get("error") is not None:
                    # 重试帧只提示进度（子任务无法向用户提示），不终止
                    if event.get("retrying"):
                        continue
                    stream_error = event.get("error")
                    break
                usage.collect(event)
                if event.get("finish_reason") is not None:
                    finish_reason = event["finish_reason"]
                    event = event.copy()
                    event.pop("finish_reason", None)
                content = event.get("content")
                reasoning_content = event.get("reasoning_content")
                tool_calls_delta = event.get("tool_calls")
                function_call_delta = event.get("function_call")
                if content:
                    full_response += content
                    # seq 与本轮 model_call/tool_* 对齐：前端按 seq 归位轮次，
                    # 避免 delta 先于 model_call 到达时轮次错位/重复分区
                    await self._emit("delta", content_delta=content, seq=self._seq)
                if reasoning_content:
                    full_reasoning += reasoning_content
                    await self._emit("delta", reasoning_delta=reasoning_content, seq=self._seq)
                if tool_calls_delta:
                    merge_tool_call_delta(tool_calls, tool_calls_delta)
                    if tool_call_stream_timeout > 0:
                        tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
                elif function_call_delta:
                    merge_function_call_delta(tool_calls, function_call_delta)
                    if tool_call_stream_timeout > 0:
                        tool_phase_deadline = time.monotonic() + tool_call_stream_timeout
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await sse_iter.aclose()
            except Exception:
                pass
        return {
            "full_response": full_response,
            "full_reasoning": full_reasoning,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "stream_error": stream_error,
            "tool_phase_timeout_hit": tool_phase_timeout_hit,
            "stream_truncated": stream_truncated,
        }

    # ----------------------------- 工具执行 -----------------------------
    async def _execute_tool_round(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        """子任务自己的工具执行轮：内置拦截 + 文件工具 + MCP 线程池。

        返回与 chat_factory 统一结果流同构的 dict 列表
        （index/tool_call/tool_name/tool_args/result/error）。
        """
        ctx = self.context
        known_tool_names = list(ctx.tool_servers.keys())
        normalized = normalize_tool_calls(tool_calls, known_tool_names)
        plan = prepare_tool_execution(normalized, known_tool_names)
        parsed_tools = [item for item in plan.parsed_tools if item[2] in ctx.tool_servers]
        blocked_tools = [item for item in plan.parsed_tools if item[2] not in ctx.tool_servers]
        results: list[dict[str, Any]] = []
        # 未授权工具：立即拒绝（不推送 tool_start）
        for idx, tc, tn, ta in blocked_tools:
            results.append({
                "index": idx,
                "tool_call": tc,
                "tool_name": tn,
                "tool_args": ta,
                "result": f"工具 {tn} 未在子任务工具列表中，禁止调用（子智能体不支持再派发子任务）",
                "error": None,
            })
        # over_task 结束信号（prepare_tool_execution 会把它从 parsed_tools 分离，
        # 不进入执行）：执行侧不实际执行，但必须为声明生成配对结果——否则
        # assistant 的 tool_calls 悬空（无配对 tool 结果），回放上游时被严格
        # 校验拒绝（400 空响应体）
        if plan.over_task_call:
            ot_index, ot_tc, ot_tn, ot_ta = plan.over_task_call
            results.append({
                "index": ot_index,
                "tool_call": ot_tc,
                "tool_name": ot_tn,
                "tool_args": ot_ta,
                "result": "子任务已由模型宣布结束（over_task）。",
                "error": None,
            })
        builtin_results: list[tuple[int, dict, str, dict, Any]] = []
        external_tools: list[tuple[int, dict, str, dict]] = []
        for idx, tc, tn, ta in parsed_tools:
            if tn == TODO_TOOL_NAME:
                prev_snapshot = list(self.todo)
                items, notices, error_reason = normalize_todo_items(
                    ta.get("todos") if isinstance(ta, dict) else None,
                    prev_items=prev_snapshot,
                )
                if items is None:
                    todo_result: dict[str, Any] = {"error": error_reason or "todos 参数无效"}
                else:
                    self.todo = items
                    self._todo_touched = True
                    await self._emit("todo", todos=items)
                    done_count = sum(1 for item in items if item["status"] == "done")
                    in_progress_count = sum(1 for item in items if item["status"] == "in_progress")
                    pending_count = len(items) - done_count - in_progress_count
                    sub_plan_complete = bool(items) and done_count == len(items)
                    # 与父级 todo 结果同构：三态文案 + plan_complete 机器可读标记
                    if sub_plan_complete:
                        sub_message = (
                            f"子任务规划全部完成（{done_count}/{len(items)} 项）："
                            "请汇总执行结果作为最终回复返回父智能体；"
                            "如无新的计划，无需再调用本工具"
                        )
                    elif not prev_snapshot:
                        sub_message = (
                            f"子任务计划已创建（共 {len(items)} 项，进行中 {in_progress_count} 项，"
                            f"待办 {pending_count} 项）：开始执行某步骤前将其设为 in_progress"
                        )
                    else:
                        sub_message = (
                            f"子任务计划已更新（共 {len(items)} 项，已完成 {done_count} 项，"
                            f"进行中 {in_progress_count} 项，待办 {pending_count} 项）"
                        )
                    todo_result = {
                        "message": sub_message,
                        "plan_complete": sub_plan_complete,
                        "todos": items,
                    }
                    if notices:
                        todo_result["warnings"] = notices
                builtin_results.append((idx, tc, tn, ta, todo_result))
                continue
            if tn == ASK_USER_TOOL_NAME:
                builtin_results.append((idx, tc, tn, ta, execute_ask_user_placeholder(ta)))
                continue
            if tn == SUB_AGENT_TOOL_NAME:
                builtin_results.append((idx, tc, tn, ta, {
                    "error": "子智能体不支持再派发子任务（V1 禁止嵌套），请自行完成或分步执行",
                }))
                continue
            if tn == READ_MEDIA_NAME:
                # 读取媒体（子任务版，与父循环 execute_read_media 同接口）：
                # 引用白名单 = 任务文本中的 media:// 引用（子任务上下文独立，
                # 没有其他来源）；数据本体由 runner 在工具结果统一处理后注入
                if not self._vision_enabled:
                    read_media_result: dict[str, Any] = {
                        "ok": False,
                        "error": READ_MEDIA_REJECTION_MESSAGE,
                        "loaded": [],
                        "skipped": [],
                        "injected_references": [],
                    }
                elif not self.task_media_references and any(
                    isinstance(item, str) and item.strip().startswith("media://")
                    for item in (ta.get("references") if isinstance(ta, dict) else None) or []
                ):
                    # 任务文本没有任何 media:// 引用：media:// 调用一律越界拒绝
                    #（execute_read_media 对空边界集合不做过滤是父级口径，这里
                    # 显式收紧，防越权读取会话其他媒体；本地/网络来源不受影响）
                    read_media_result = {
                        "ok": False,
                        "error": (
                            "任务文本中没有可读取的 media:// 引用"
                            "（read_media 仅支持父级派发任务文本中出现的引用）"
                        ),
                        "loaded": [],
                        "skipped": [],
                        "injected_references": [],
                    }
                else:
                    read_media_result = execute_read_media(
                        ta,
                        ctx.session_id,
                        self.task_media_references,
                        already_injected=self.read_media_injected_refs,
                        vision_enabled=self._vision_enabled,
                    )
                if read_media_result.get("ok"):
                    self.read_media_injected_refs.update(
                        read_media_result.get("injected_references") or []
                    )
                    self.read_media_pending_parts.extend([
                        (
                            item["reference"],
                            item.get("quality"),
                            item.get("start_time"),
                            item.get("end_time"),
                        )
                        for item in (read_media_result.get("loaded") or [])
                        if item.get("reference")
                    ])
                    evicted_refs: set[str] = set()
                    self.read_media_pending_parts = roll_recent_media_parts(
                        self.read_media_pending_parts,
                        evicted=evicted_refs,
                    )
                    self.read_media_injected_refs.difference_update(evicted_refs)
                builtin_results.append((idx, tc, tn, ta, read_media_result))
                continue
            if tn == READ_DOCUMENT_NAME:
                # 读取用户上传文件：子任务生效模型声明支持该类型（supportDocTypes）
                # 时优先返回原生文档部件（与父循环 execute_read_document 同接口），
                # 由 runner 在工具结果统一处理后注入；未命中则纯文本分页口径
                read_document_result = execute_read_document(
                    ta, ctx.session_id, support_doc_types=self._native_doc_types(),
                )
                native_block = (
                    read_document_result.get("native")
                    if isinstance(read_document_result, dict) else None
                )
                if isinstance(native_block, dict) and native_block.get("part") is not None:
                    self.native_doc_pending_parts.append(native_block["part"])
                builtin_results.append((idx, tc, tn, ta, read_document_result))
                continue
            builtin_result = execute_builtin_tool(
                tn, ta, ctx.configured_tool_names, ctx.configured_tool_servers,
                enabled_tool_names=set(ctx.tool_servers),
            )
            if builtin_result is not None:
                builtin_results.append((idx, tc, tn, ta, builtin_result))
                continue
            file_tool_result = try_execute_builtin_file_tool(tn, ta)
            if file_tool_result is not None:
                builtin_results.append((idx, tc, tn, ta, file_tool_result))
                continue
            # 内置终端命令工具（run_command）：服务端本地执行；命令为同步阻塞
            # 调用（可能长达分钟级），经 asyncio.to_thread 放到工作线程执行，
            # 避免卡死子任务事件循环（与 MCP 工具走线程池的语义一致）
            if tn == RUN_COMMAND_NAME:
                command_result = await asyncio.to_thread(
                    try_execute_builtin_command_tool, tn, ta)
                builtin_results.append((idx, tc, tn, ta, command_result))
                continue
            external_tools.append((idx, tc, tn, ta))
        # tool_start 事件（内置 + 外部统一推送；被拦截的不推送）
        for idx, tc, tn, ta in parsed_tools:
            await self._emit(
                "tool_start",
                tool_call_id=tc.get("id", "") if isinstance(tc, dict) else "",
                function_name=tn,
                arguments=ta,
                seq=self._seq,
            )
        for idx, tc, tn, ta, result in builtin_results:
            results.append({
                "index": idx, "tool_call": tc, "tool_name": tn,
                "tool_args": ta, "result": result, "error": None,
            })
        if external_tools:
            max_workers = max(1, min(3, len(external_tools)))
            mcp_results = await asyncio.to_thread(
                execute_tool_round,
                parsed_tools=external_tools,
                tool_mcp_servers=ctx.tool_servers,
                max_workers=max_workers,
            )
            results.extend(mcp_results)
        results.sort(key=lambda item: item["index"])
        return results

    # ----------------------------- 主循环 -----------------------------
    async def run(self) -> SubAgentResult:
        try:
            return await self._run_inner()
        except asyncio.CancelledError:
            # 层级取消：父级停止 / 父级超时兜底 触发。状态判定：
            # 取消令牌显式置位 → timeout；父级停止信号 → stopped；
            # 两者都不是的取消（wait_for 硬兜底）按 timeout 处理。
            if self.context.cancel_token.is_set():
                status = "timeout"
            elif self.context.stop_requested():
                status = "stopped"
            else:
                status = "timeout"
            self._status = status
            self._final_reply = self._build_progress_reply(status)
            try:
                await self._emit_done(status, self._final_reply, None)
            except Exception:
                pass
            raise
        except Exception as exc:
            self._status = "error"
            self._error = str(exc)
            self._final_reply = self._build_progress_reply("error", error=str(exc))
            try:
                await self._emit_done("error", self._final_reply, str(exc))
            except Exception:
                pass
            return self._make_result()

    async def _run_inner(self) -> SubAgentResult:
        ctx = self.context
        # 子任务视觉能力：以实际执行子任务的模型为准无条件解析——
        # sub_agent_model 独立子模型配置 > 父级口径（parent_vision_enabled）
        # > 保守支持。父级传入值只作为独立子模型未配置时的兜底，不能覆盖
        # 独立子模型的判定（否则会出现"父级支持但子模型不支持却看得到
        # read_media"的漏洞）
        self._vision_enabled = resolve_sub_agent_vision_enabled(
            self._vision_enabled if isinstance(self._vision_enabled, bool) else None
        )
        visible_tools = self._visible_tool_definitions()
        # 预检查：task + 工具定义 vs 模型窗口（超窗 → error 收尾，不走父级降级链）
        # 按"模型实际可见工具"估算（vision=false 时 read_media 已被剔除）
        model_config, _parameter = self._resolve_request_model_config()
        window = (
            resolve_config_max_input_tokens(model_config, default=8192)
            if model_config is not None
            else resolve_model_max_input_tokens(default=8192)
        )
        estimated = estimate_send_context_tokens(self.messages, visible_tools)
        if window > 0 and estimated > window:
            detail = (
                f"子任务上下文预估 {estimated} tokens 超过模型窗口 {window}："
                "任务描述过长或注入工具过多，请父智能体精简 task 后重新派发"
            )
            self._status = "error"
            self._error = detail
            self._final_reply = detail
            await self._emit_start()
            await self._emit_done("error", detail, detail)
            return self._make_result()
        await self._emit_start()
        compaction_settings = load_context_compaction_settings()
        oversized_threshold = resolve_oversized_result_token_threshold(compaction_settings)
        max_oversized_rejections = compaction_settings.max_oversized_rejections
        while True:
            # ---- 停止条件检查（每轮顶部）----
            if ctx.cancel_token.is_set():
                self._status = "timeout"
                self._final_reply = self._build_progress_reply("timeout")
                await self._emit_done("timeout", self._final_reply, None)
                return self._make_result()
            if ctx.stop_requested():
                self._status = "stopped"
                self._final_reply = self._build_progress_reply("stopped")
                await self._emit_done("stopped", self._final_reply, None)
                return self._make_result()
            elapsed = time.monotonic() - self._started_monotonic
            if ctx.timeout_seconds > 0 and elapsed >= ctx.timeout_seconds:
                self._status = "timeout"
                self._final_reply = self._build_progress_reply("timeout")
                await self._emit_done("timeout", self._final_reply, None)
                return self._make_result()
            if self.rounds >= ctx.max_rounds:
                self._status = "max_rounds"
                self._final_reply = self._build_progress_reply("max_rounds")
                await self._emit_done("max_rounds", self._final_reply, None)
                return self._make_result()
            self.rounds += 1
            self._seq += 1
            call = await self._model_call()
            if call["stream_error"] is not None:
                stream_error_detail = f"子任务模型流式响应出错：{call['stream_error']}"
                # 断点续跑重试：已生成的部分内容进入 messages（内部消息不落盘），
                # 让子模型从已有进度继续——工具轨迹/todo 都在上下文中不丢失；
                # 0/负=不限制（仍受 max_rounds/timeout 硬封顶），耗尽才 error 收尾
                se_limit = self._retry_limits["stream_error_max_attempts"]
                if se_limit <= 0 or self._stream_error_retries_used < se_limit:
                    self._stream_error_retries_used += 1
                    se_label = str(se_limit) if se_limit > 0 else "∞"
                    print(
                        f"[WARN] 子任务模型调用失败，断点续跑重试 "
                        f"#{self._stream_error_retries_used}（上限 {se_label}）"
                    )
                    if call["full_response"]:
                        self.messages.append({"role": "assistant", "content": call["full_response"]})
                    elif call["full_reasoning"]:
                        self.messages.append({"role": "assistant", "content": f"...{call['full_reasoning'][-100:]}"})
                    self.messages.append({
                        "role": "user",
                        "content": (
                            f"刚才的模型调用失败（{stream_error_detail}），本次请求没有产生可用回复。"
                            "请基于已有进度继续任务；如任务已经完成，请直接给出最终回复。"
                        ),
                        "_internal": True,
                    })
                    await self._emit(
                        "notice",
                        message=(
                            f"模型调用失败，正在从断点续跑"
                            f"（第 {self._stream_error_retries_used} 次，上限 {se_label}）"
                        ),
                        retry_kind="stream_error",
                        seq=self._seq,
                    )
                    continue
                detail = stream_error_detail + (
                    f"（断点续跑重试 {self._stream_error_retries_used} 次后放弃）"
                    if self._stream_error_retries_used else ""
                )
                self._status = "error"
                self._error = detail
                self._final_reply = self._build_progress_reply("error", error=detail)
                await self._emit_done("error", self._final_reply, detail)
                return self._make_result()
            tool_calls = call["tool_calls"]
            full_response = call["full_response"]
            full_reasoning = call["full_reasoning"]
            # 工具调用流式阶段超时且无可用调用：以内部消息反馈子模型重试
            if call["tool_phase_timeout_hit"] and not tool_calls:
                self.messages.append({
                    "role": "user",
                    "content": (
                        f"刚才的工具调用因上游服务超过 {int(load_tool_call_stream_timeout())} 秒"
                        "无输出而失败（接口卡死，参数可能不完整）。请重新发起工具调用或直接给出结论。"
                    ),
                    "_internal": True,
                })
                continue
            # 归一化工具调用（流式拼接异常修复）：与执行侧共用同一份列表，
            # 保证下面 assistant 声明的 id 与 _execute_tool_round 产出的结果
            # 一一配对——上游对声明/结果严格校验，任何"结果无声明"的孤儿或
            # "声明无结果"的悬空都会触发 400 空响应体（父循环同款契约）。
            normalized_tool_calls = normalize_tool_calls(
                tool_calls, list(ctx.tool_servers.keys())
            )
            # 构建带 tool_calls 的 assistant 消息。
            # 未知/未授权工具同样保留在声明里（与父循环一致）：执行侧会为
            # blocked 调用生成拒绝结果，若此处剔除就会形成孤儿结果 → 400。
            # 仅剔除连函数名都没有的结构损坏调用（执行侧同样跳过、不产生结果）。
            assistant_message: dict[str, Any] = {"role": "assistant"}
            if full_response:
                assistant_message["content"] = full_response
            elif full_reasoning:
                assistant_message["content"] = f"...{full_reasoning[-100:]}"
            if full_reasoning:
                assistant_message["reasoning_content"] = full_reasoning
            formatted_tool_calls = []
            for tc in normalized_tool_calls:
                function_info = tc.get("function") or {}
                tool_name = function_info.get("name")
                if not tool_name:
                    continue
                formatted_tool_calls.append({
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": function_info.get("name", ""),
                        "arguments": function_info.get("arguments", "{}"),
                    },
                })
            # 无工具调用：任务收尾
            if not formatted_tool_calls:
                # 上游提前断开（EOF 未收到 finish_reason）：与 token 截断同语义，
                # 以内部消息让子模型继续（受 max_rounds 约束，不会无限重试）。
                # 此前该场景与正常收尾不可区分——空正文被包装为"思考摘要"静默
                # 结束（done），真实回答丢失
                if call["stream_truncated"]:
                    if full_response:
                        self.messages.append({"role": "assistant", "content": full_response})
                    elif full_reasoning:
                        self.messages.append({"role": "assistant", "content": f"...{full_reasoning[-100:]}"})
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "上一次回复因连接中断被截断，你已输出的内容可能不完整。"
                            "请从中断处继续完成回答（如结论已完整，请重述一次完整结论）。"
                        ),
                        "_internal": True,
                    })
                    stream_truncated = False
                    continue
                if call["finish_reason"] == "length":
                    # 截断续写：落盘已生成内容 + 内部"请继续"消息（同父循环语义）
                    if full_response:
                        self.messages.append({"role": "assistant", "content": full_response})
                    self.messages.append({
                        "role": "user",
                        "content": "你的回答因为长度限制被截断了，请继续完成。",
                        "_internal": True,
                    })
                    continue
                if call["tool_phase_timeout_hit"]:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "工具调用在输出过程中超时被中止（参数可能不完整），本次已放弃；"
                            "请重新发起工具调用或直接给出结论。"
                        ),
                        "_internal": True,
                    })
                    continue
                # ---- 交付保障（子任务限定；父级循环无此机制）----
                # 优先级：todo 未完成提醒 > 空收尾重试 > 接受收尾。
                # todo 提醒只对「模型创建/更新过的计划」生效（它知情、做出过
                # 承诺）；父级预置但从未被触碰的计划是隐藏状态，不拦截有效
                # 交付——否则模型完成工作后仍会被迫补计划再重复交付一遍。
                # 提醒额度在每次完整工具执行轮后重置；空收尾/提醒都以内部
                # 消息推进，消耗 rounds，受 max_rounds/timeout 双重硬封顶。
                pending_todo = [
                    item for item in self.todo
                    if isinstance(item, dict) and item.get("status") in ("pending", "in_progress")
                ]
                tr_limit = self._retry_limits["todo_remind_max"]
                if pending_todo and self._todo_touched and (
                    tr_limit < 0 or self._todo_remind_used < tr_limit
                ):
                    self._todo_remind_used += 1
                    tr_label = str(tr_limit) if tr_limit > 0 else "∞"
                    print(
                        f"[WARN] 子任务收尾但 todo 仍有 {len(pending_todo)} 项未完成，"
                        f"提醒继续 #{self._todo_remind_used}（上限 {tr_label}）"
                    )
                    if full_response:
                        self.messages.append({"role": "assistant", "content": full_response})
                    elif full_reasoning:
                        self.messages.append({"role": "assistant", "content": f"...{full_reasoning[-100:]}"})
                    self.messages.append({
                        "role": "user",
                        "content": (
                            f"你准备结束子任务，但计划中仍有 {len(pending_todo)} 项未完成"
                            "（pending/in_progress）。请继续调用工具完成它们；"
                            "如相关步骤实际已完成或无需执行，请先用 todo 工具更新计划"
                            "（标记 done 或说明原因），然后再给出最终回复。"
                        ),
                        "_internal": True,
                    })
                    await self._emit(
                        "notice",
                        message=(
                            f"todo 计划仍有 {len(pending_todo)} 项未完成，"
                            f"已提醒子模型继续（第 {self._todo_remind_used} 次，上限 {tr_label}）"
                        ),
                        retry_kind="todo_remind",
                        seq=self._seq,
                    )
                    continue
                final_reply = full_response.strip()
                if not final_reply:
                    # 空收尾重试：正文为空（含"仅思考输出"场景）不算有效交付——
                    # 注入内部消息让子模型再次交付；额度耗尽后按 error 收尾，
                    # 不再伪装成 done+占位文本（父级可据此重派或换路径）
                    fr_limit = self._retry_limits["final_reply_max_attempts"]
                    if fr_limit <= 0 or self._final_reply_retries_used < fr_limit:
                        self._final_reply_retries_used += 1
                        fr_label = str(fr_limit) if fr_limit > 0 else "∞"
                        print(
                            f"[WARN] 子任务最终回复为空，空收尾重试 "
                            f"#{self._final_reply_retries_used}（上限 {fr_label}）"
                        )
                        if full_reasoning:
                            self.messages.append({
                                "role": "assistant",
                                "content": f"...{full_reasoning[-100:]}",
                                "reasoning_content": full_reasoning,
                            })
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "你上一轮没有输出任何最终回复内容。请基于已完成的工作"
                                "直接输出最终回复（结论/结果/遗留问题）；"
                                "如任务确实无法完成，请说明原因。"
                            ),
                            "_internal": True,
                        })
                        await self._emit(
                            "notice",
                            message=(
                                "子任务最终回复为空，已要求重新交付"
                                f"（第 {self._final_reply_retries_used} 次，上限 {fr_label}）"
                            ),
                            retry_kind="final_reply",
                            seq=self._seq,
                        )
                        continue
                    detail = (
                        f"子任务最终回复为空（空收尾重试 {self._final_reply_retries_used} 次后放弃）"
                        if self._final_reply_retries_used
                        else "子任务最终回复为空（空收尾重试已配置为关闭）"
                    )
                    self._status = "error"
                    self._error = detail
                    self._final_reply = self._build_progress_reply("error", error=detail)
                    await self._emit_done("error", self._final_reply, detail)
                    return self._make_result()
                self.messages.append(
                    assistant_message if full_response else {"role": "assistant", "content": final_reply}
                )
                self._status = "done"
                self._final_reply = final_reply
                await self._emit_done("done", final_reply, None)
                return self._make_result()
            assistant_message["tool_calls"] = formatted_tool_calls
            self.messages.append(assistant_message)
            # 落盘本轮模型调用（思考/正文/工具调用轨迹，docs §7.1 model_call）
            await self._emit(
                "model_call",
                seq=self._seq,
                reasoning_content=full_reasoning or None,
                content=assistant_message.get("content"),
                tool_calls=formatted_tool_calls,
            )
            # 工具执行（传入与声明同一份归一化列表，保证 id 一一配对）
            tool_results = await self._execute_tool_round(normalized_tool_calls)
            if not tool_results:
                self.messages.append({
                    "role": "user",
                    "content": "上一轮没有可执行的工具调用，请重新发起或直接给出结论。",
                    "_internal": True,
                })
                continue
            for tool_result in tool_results:
                tool_call = tool_result["tool_call"]
                tool_call_id = tool_call.get("id", "")
                tool_name = tool_result["tool_name"]
                tool_args = tool_result["tool_args"]
                arguments_text = (
                    tool_args if isinstance(tool_args, str)
                    else json.dumps(tool_args, ensure_ascii=False)
                )
                ret = _format_result_text(tool_result["result"])
                # 超大结果拒绝：替换为反馈文案，引导子模型重新规划
                if (
                    oversized_threshold > 0
                    and estimate_text_tokens(ret) > oversized_threshold
                ):
                    self._consecutive_oversized += 1
                    estimated_tokens = estimate_text_tokens(ret)
                    feedback = build_oversized_tool_feedback(
                        tool_name, estimated_tokens, oversized_threshold, result_text=ret
                    )
                    await self._emit(
                        "tool_result",
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments=arguments_text,
                        result=feedback,
                        result_preview=make_oversized_result_preview(ret),
                        oversized=True,
                        oversized_estimated_tokens=estimated_tokens,
                        seq=self._seq,
                    )
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": feedback,
                        "_tool_name": tool_name,
                        "_oversized": True,
                    })
                    if self._consecutive_oversized >= max_oversized_rejections:
                        detail = (
                            f"子任务连续 {self._consecutive_oversized} 次返回超大工具结果，已终止"
                        )
                        self._status = "error"
                        self._error = detail
                        self._final_reply = self._build_progress_reply("error", error=detail)
                        await self._emit_done("error", self._final_reply, detail)
                        return self._make_result()
                    continue
                self._consecutive_oversized = 0
                # 内置文件工具的展示用 diff：随 tool_result 事件分发（JSONL+SSE），
                # 模型上下文中的剥离已在 _format_result_text 完成
                sub_raw_result = tool_result["result"]
                sub_file_diff = (
                    sub_raw_result.get("_file_diff") if isinstance(sub_raw_result, dict) else None
                )
                # V2 版本链：子任务内的文件工具变更同样入链（标注父轮次号）
                if isinstance(sub_raw_result, dict) and "_file_history" in sub_raw_result:
                    self._record_file_history(sub_raw_result)
                await self._emit(
                    "tool_result",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments_text,
                    result=ret,
                    oversized=False,
                    **({"file_diff": sub_file_diff} if sub_file_diff else {}),
                    seq=self._seq,
                )
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": ret,
                    "_tool_name": tool_name,
                })
            # 本轮完整工具执行结束：todo 提醒额度重置——模型再次收尾时若计划
            # 仍未完成可重新获得一次提醒（"调工具后忘记→再提醒"语义）
            self._todo_remind_used = 0
            # read_media 数据注入（子任务版）：按坐标现场加载媒体 base64 部件，
            # 作为 user 消息追加进 messages（仅内存，不落盘——任务文本里的
            # media:// 引用即子任务媒体台账）。加载失败的坐标以占位文本告知
            # 子模型，避免静默丢失
            if self.read_media_pending_parts:
                # 视频一次性消费：与父级注入同口径——先回收旧 video 部件，
                # 同批多视频仅第一个带画面数据（供应商单请求仅 1 个视频）
                retire_prior_video_parts(self.messages)
                media_parts = []
                for reference, quality, start_time, end_time in self.read_media_pending_parts:
                    info = load_any_media_model_part(
                        ctx.session_id, reference, quality=quality,
                        start_time=start_time, end_time=end_time,
                    )
                    if info is not None and info.get("part") is not None:
                        media_parts.append(info["part"])
                    elif isinstance(info, dict) and info.get("error"):
                        media_parts.append({
                            "type": "text",
                            "text": f"[媒体 {reference} 读取失败：{info['error']}]",
                        })
                    else:
                        media_parts.append({
                            "type": "text",
                            "text": f"[媒体 {reference} 未找到，可能已删除或读取失败]",
                        })
                media_parts = cap_video_parts_in_batch(media_parts)
                self.messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "以下是你刚才调用 read_media 读取的媒体内容"
                                "（按引用顺序排列）："
                            ),
                        },
                        *media_parts,
                    ],
                    "_internal": True,
                })
                self.read_media_pending_parts = []
            # read_document 原生文档注入（子任务版）：模型声明支持该类型时，
            # 工具返回的原生文档部件在此作为 user 消息追加（仅内存、不落盘）。
            # 注入前回收上一批原生文档数据（转文本占位），避免多批叠加
            if self.native_doc_pending_parts:
                retire_native_document_parts(self.messages)
                self.messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "以下是你刚才调用 read_document 读取的文档内容"
                                "（原生文档，按文件名顺序排列）："
                            ),
                        },
                        *self.native_doc_pending_parts,
                    ],
                    "_internal": True,
                })
                self.native_doc_pending_parts = []
            # 循环继续：下一轮模型调用（顶部已检查停止条件）

    # ----------------------------- 收尾辅助 -----------------------------
    def _build_progress_reply(self, status: str, error: str | None = None) -> str:
        """非正常收尾时返回给父级的"进展 + 未完成原因"说明。"""
        last_text = ""
        for message in reversed(self.messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    last_text = content.strip()
                    break
        reason_map = {
            "timeout": "子任务执行超时，被强制中止",
            "stopped": "父级任务已停止，子任务被中止",
            "max_rounds": f"子任务达到轮次上限（{self.context.max_rounds} 轮）仍未完成",
            "error": "子任务执行出错",
        }
        parts = [f"子任务未完成：{reason_map.get(status, status)}。"]
        if error:
            parts.append(f"错误信息：{error}")
        pending_todo = [
            item for item in self.todo
            if isinstance(item, dict) and item.get("status") in ("pending", "in_progress")
        ]
        if pending_todo:
            parts.append(
                f"计划进度：共 {len(self.todo)} 项，其中 {len(pending_todo)} 项未完成"
                "（pending/in_progress）；"
                + "；".join(str(item.get("content") or item.get("id") or "?") for item in pending_todo)
            )
        elif self.todo:
            parts.append(f"计划进度：{len(self.todo)} 项全部标记完成")
        if last_text:
            preview = last_text[:1500]
            parts.append(f"已完成进展（最后一段正文）：{preview}")
        parts.append("请父智能体基于以上进展决定：重派子任务、换路径，或直接向用户说明。")
        return "\n".join(parts)

    def _make_result(self) -> SubAgentResult:
        final_reply = self._final_reply or self._build_progress_reply(self._status, self._error)
        # 最终回复长度保护：截断 + 尾注（完整轨迹始终在 JSONL 块内）
        limit = self.context.reply_max_chars
        if limit > 0 and len(final_reply) > limit:
            final_reply = (
                final_reply[:limit]
                + "\n\n（最终回复过长已截断，完整轨迹见子任务事件块）"
            )
        usage_total: dict[str, Any] = {}
        self.usage_accumulator.merge_to(usage_total, _merge_usage_values)
        return SubAgentResult(
            agent_id=self.context.agent_id,
            parent_tool_call_id=self.context.parent_tool_call_id,
            status=self._status,
            final_reply=final_reply,
            rounds=self.rounds,
            usage_total=usage_total,
            error=self._error,
        )


# ---------------------------------------------------------------------------
# 并发编排（父循环入口）
# ---------------------------------------------------------------------------
async def run_sub_agent_batch(
    contexts: list[SubAgentContext],
) -> list[dict[str, Any]]:
    """并发执行一批子任务；返回可直接汇入父级 tool_results 的结果 dict 列表。

    - 每个子任务一个 asyncio.Task，受 SUB_AGENT_MAX_CONCURRENT 信号量限流；
    - 每个子任务整体包 wait_for(timeout)：超时 → 任务被取消 → Runner 的
      CancelledError 处理器补写 done(timeout)；Runner 卡死时由本函数兜底
      补写（runner.done_emitted 防重复）；
    - 子任务失败不影响兄弟任务与父任务：异常转错误结果文本；
    - 父级停止时本调用与父循环一起被取消：每个子任务的 run() 内部先补写
      done(stopped) 再上抛 CancelledError（except Exception 不捕获
      BaseException，不会吞掉取消信号）。
    """
    if not contexts:
        return []
    limits = load_sub_agent_limits()
    semaphore = asyncio.Semaphore(limits["max_concurrent"])
    timeout = limits["timeout_seconds"]

    async def _run_one(context: SubAgentContext) -> dict[str, Any]:
        runner = SubAgentRunner(context)
        tool_call = {
            "id": context.parent_tool_call_id,
            "type": "function",
            "function": {
                "name": SUB_AGENT_TOOL_NAME,
                "arguments": json.dumps({"task": context.task}, ensure_ascii=False),
            },
        }
        try:
            async with semaphore:
                if timeout > 0:
                    try:
                        result = await asyncio.wait_for(runner.run(), timeout=timeout)
                    except asyncio.TimeoutError:
                        # 硬兜底：Runner 卡死时其 CancelledError 处理器可能没能
                        # 完成 done 发射，这里补写（done_emitted 防重复）
                        context.cancel_token.set()
                        fallback_reply = (
                            "子任务执行超时，被强制中止；未产出可用最终回复，"
                            "请父智能体精简任务后重派或换路径。"
                        )
                        await runner._emit_done("timeout", fallback_reply, "sub_agent timeout")
                        result = SubAgentResult(
                            agent_id=context.agent_id,
                            parent_tool_call_id=context.parent_tool_call_id,
                            status="timeout",
                            final_reply=runner._final_reply or fallback_reply,
                            rounds=runner.rounds,
                            usage_total=_collect_usage(runner.usage_accumulator),
                            error="sub_agent timeout",
                        )
                else:
                    result = await runner.run()
            print(
                f"[INFO] 子任务完成（agent_id={result.agent_id}, status={result.status}, "
                f"rounds={result.rounds}）"
            )
            return {
                "index": context.parent_tool_index,
                "tool_call": tool_call,
                "tool_name": SUB_AGENT_TOOL_NAME,
                "tool_args": {"task": context.task},
                "result": result.final_reply,
                "error": None,
                "_sub_agent": {
                    "agent_id": result.agent_id,
                    "status": result.status,
                    "rounds": result.rounds,
                    "usage_total": result.usage_total,
                },
            }
        except Exception as exc:  # 子任务异常不外溢（CancelledError 为 BaseException，不在其中）
            print(f"[WARN] 子任务异常（agent_id={context.agent_id}）: {exc}")
            return {
                "index": context.parent_tool_index,
                "tool_call": tool_call,
                "tool_name": SUB_AGENT_TOOL_NAME,
                "tool_args": {"task": context.task},
                "result": f"子任务执行失败：{exc}",
                "error": exc,
                "_sub_agent": {"agent_id": context.agent_id, "status": "error"},
            }

    results = await asyncio.gather(*[_run_one(ctx) for ctx in contexts])
    return list(results)
