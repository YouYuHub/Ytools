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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import (
    get_model_config,
    get_role_parameter,
    get_role_selection,
    load_var,
)
from util.timestamp_utils import now_str

from factory.agent_runtime.builtin_tools import (
    ASK_USER_TOOL_NAME,
    CHECK_TOOL_EXISTS_NAME,
    SUB_AGENT_TOOL_NAME,
    TODO_TOOL_NAME,
    execute_ask_user_placeholder,
    execute_builtin_tool,
    normalize_todo_items,
    try_execute_builtin_file_tool,
)
from factory.agent_runtime.chat_runtime import (
    UsageAccumulator,
    build_request_messages,
    copy_for_request,
    estimate_request_context_tokens,
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
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


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

    async def _emit_start(self) -> None:
        await self._emit(
            "start",
            task=self.context.task,
            todo=self.todo or None,
            tools=[name for name in self.context.tool_servers if name != CHECK_TOOL_EXISTS_NAME],
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
                return chat_config, parameter
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

    def _build_request(self, parameter: dict[str, Any]) -> ChatLLMRequest:
        """构造子任务请求：参数填充优先级 sub_agent_model.parameter > 内置默认。

        ChatLLMRequest 内置默认（temperature 0.7 / top_p 1.0 / presence_penalty
        2.0 / reasoning_effort medium / max_tokens 8192）与父级入口一致；角色
        parameter（分桶后的生效参数）按字段覆盖。非标准参数（enable_thinking
        等）走 extra_body。
        """
        request = ChatLLMRequest(
            messages=build_request_messages(copy_for_request(self.messages)),
            tools=list(self.context.tools) or None,
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
                    await self._emit("delta", content_delta=content)
                if reasoning_content:
                    full_reasoning += reasoning_content
                    await self._emit("delta", reasoning_delta=reasoning_content)
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

        builtin_results: list[tuple[int, dict, str, dict, Any]] = []
        external_tools: list[tuple[int, dict, str, dict]] = []
        for idx, tc, tn, ta in parsed_tools:
            if tn == TODO_TOOL_NAME:
                items, notices, error_reason = normalize_todo_items(
                    ta.get("todos") if isinstance(ta, dict) else None,
                    prev_items=list(self.todo),
                )
                if items is None:
                    todo_result: dict[str, Any] = {"error": error_reason or "todos 参数无效"}
                else:
                    self.todo = items
                    await self._emit("todo", todos=items)
                    done_count = sum(1 for item in items if item["status"] == "done")
                    todo_result = {
                        "message": f"子任务计划已更新（共 {len(items)} 项，已完成 {done_count} 项）",
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
        # 预检查：task + 工具定义 vs 模型窗口（超窗 → error 收尾，不走父级降级链）
        model_config, _parameter = self._resolve_request_model_config()
        window = (
            resolve_config_max_input_tokens(model_config, default=8192)
            if model_config is not None
            else resolve_model_max_input_tokens(default=8192)
        )
        estimated = estimate_request_context_tokens(self.messages, ctx.tools)
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
                detail = f"子任务模型流式响应出错：{call['stream_error']}"
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

            # 构建带 tool_calls 的 assistant 消息（只保留已知工具）
            assistant_message: dict[str, Any] = {"role": "assistant"}
            if full_response:
                assistant_message["content"] = full_response
            elif full_reasoning:
                assistant_message["content"] = f"...{full_reasoning[-100:]}"
            if full_reasoning:
                assistant_message["reasoning_content"] = full_reasoning
            formatted_tool_calls = []
            for tc in tool_calls:
                function_info = tc.get("function") or {}
                tool_name = function_info.get("name")
                if tool_name not in ctx.tool_servers:
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
                final_reply = full_response.strip()
                if not final_reply and full_reasoning:
                    final_reply = f"（无正文输出，思考摘要）{full_reasoning[-300:]}"
                if not final_reply:
                    final_reply = "（子智能体未输出有效内容）"
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

            # 工具执行
            tool_results = await self._execute_tool_round(tool_calls)
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
                await self._emit(
                    "tool_result",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments_text,
                    result=ret,
                    oversized=False,
                    seq=self._seq,
                )
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": ret,
                    "_tool_name": tool_name,
                })
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
