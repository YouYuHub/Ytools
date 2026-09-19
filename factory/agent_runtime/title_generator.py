"""会话标题自动生成：首个提问收尾后由标题模型生成 ≤30 字标题。

设计（与上下文压缩的辅助模型调用同范式）：
- 标题模型由 models.json 顶层 `model_selection.title_model` 或会话级
  `_meta.model_selection.title_model` 配置（前端"参数"面板"标题模型"tab）。
  HTTP 触发路径内 ambient 覆盖已由 chat_factory 任务启动时设置（FastAPI
  请求在任务上下文之外，本模块的 get_role_selection 读取全局选择——
  会话级覆盖标题模型的场景由 chat_config_router 在请求处理内临时设置
  ambient 后再调用生成函数）。
- **未配置标题模型时保持旧机制**：首个用户问题前 40 字
  （memory/chat_memory._recompute_meta_from_entries 兜底填充），
  本模块不写盘、零行为变化；配置无效（不存在/协议不符）同样回退旧机制。
- 输出预览采集：**前端收集 SSE 数据流**（思考/正文 delta），累计达到
  TITLE_SOURCE_PREVIEW_CHARS（100 字符）时调 POST /chat_config/generate_title
  发起标题请求（问题文本 + 输出预览透传）——标题生成与聊天主链路解耦，
  聊天循环不再感知标题任务，前端收到响应即替换侧栏标题、零轮询。
  流内不足 100 字符即 [DONE] 时，前端以实际全文触发（语义与旧机制一致）。
- 多模态标题：首问含图片/视频/音频时，前端把首问原始 content 部件一并
  传给触发接口；标题模型支持视觉（models.json 该模型 vision=true）则
  媒体解析为 base64 一并送入（大图自动缩略），不支持则按聊天主链路
  vision=false 同口径转为带引用的文本占位。
- 失败处理：失败仅打印 WARN 并写 _title_state.attempted 标记（防持续
  不可用的上游被每轮任务重复请求），标题保持旧机制值。
- 防重复：会话首行 _meta._title_state 标记；生成成功后 recompute 的
  首问 40 字兜底不再覆盖（title != default_title），用户手动重命名
  （update_session_title）同样不会被后续任务覆盖；「每条消息重新标题」
  开启时跳过标记检查每轮重新生成。
"""

from __future__ import annotations

import asyncio
import inspect
from functools import partial
from typing import Any

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import (
    get_model_config,
    get_role_headers,
    get_role_parameter,
    get_role_selection,
    inject_custom_headers_into_config,
)
from util.timestamp_utils import now_str

# 首轮模型输出预览采集长度（字符）：思考/正文均计入，任一达到即停止采集
TITLE_SOURCE_PREVIEW_CHARS = 100

# 标题输出硬截断上限（字符）：提示词要求 ≤30 字，留冗余防个别模型失控
_TITLE_MAX_CHARS = 40

# 提问文本在标题请求中的截断长度（避免超长问题浪费标题请求 token）
_TITLE_QUESTION_MAX_CHARS = 300

_TITLE_SYSTEM_PROMPT = (
    "根据提供的会话开头内容为该会话生成一个简短的标题。\n"
    "要求：\n"
    "1. 不超过30个字，通常12-20字最合适；\n"
    "2. 概括用户的核心意图或任务主题，具体不空泛；\n"
    "3. 使用与用户问题相同的语言；\n"
    "4. 只输出标题文本本身：不要引号、句号、\"标题：\"之类的前缀，"
    "不要任何解释或多余内容。\n"
)


class TitleSourceCollector:
    """采集首轮模型输出前 N 字符（思考/正文，取较长者）。

    chat_factory 在流式 delta 聚合处喂入（feed_*），任一来源达到
    TITLE_SOURCE_PREVIEW_CHARS 后内部短路，后续调用零成本；
    任务收尾时由 schedule_title_generation 取快照。
    """

    def __init__(self) -> None:
        self._reasoning = ""
        self._content = ""

    def feed_reasoning(self, delta: Any) -> None:
        if self._is_full():
            return
        if isinstance(delta, str) and delta:
            self._reasoning += delta

    def feed_content(self, delta: Any) -> None:
        if self._is_full():
            return
        if isinstance(delta, str) and delta:
            self._content += delta

    def _is_full(self) -> bool:
        return (
            len(self._reasoning) >= TITLE_SOURCE_PREVIEW_CHARS
            or len(self._content) >= TITLE_SOURCE_PREVIEW_CHARS
        )

    def snapshot(self) -> str:
        """当前预览：思考与正文取较长者（正文语义更相关），截到 100 字符。"""
        source = (
            self._content if len(self._content) >= len(self._reasoning) else self._reasoning
        )
        return source[:TITLE_SOURCE_PREVIEW_CHARS].strip()


def build_title_content(
    question_text: str,
    model_preview: str,
    question_parts: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
    vision_enabled: bool | None = None,
) -> list[dict[str, Any]] | str:
    """组装标题请求的 user content（支持多模态）。

    - 纯文本（无媒体部件 / 无 session）：字符串，【用户问题】+
      【模型输出内容前100字符】（空预览省略该节）；
    - 首问含图片/视频/音频且标题模型支持视觉（vision_enabled 非 False）：
      OpenAI 兼容部件列表，媒体部件经 resolve_media_content_parts 解析为
      base64（大图自动降采样缩略，与聊天主链路同口径），并附带说明文本部件；
    - 标题模型不支持视觉（vision_enabled=False）：媒体部件按聊天主链路
      同口径转为「[图片 media://x.png]」文本占位（模型至少知道首问带图，
      图片本身不发 base64）——返回纯文本部件列表，不中断标题生成。

    Args:
        question_parts: 首问的原始 content 部件列表（媒体为 media:// 引用
            形态，chat_factory 在收尾触发点透传）；字符串/None 视为纯文本。
    解析失败的引用回退为带引用的文本占位，不中断标题生成。
    """
    question_text_value = str(question_text or "").strip()[:_TITLE_QUESTION_MAX_CHARS]
    preview = str(model_preview or "").strip()[:TITLE_SOURCE_PREVIEW_CHARS]
    text_lines = ["【用户问题】", question_text_value or "（无文本，可能为多模态消息）"]
    if preview:
        text_lines.append("【模型输出内容前100字符】")
        text_lines.append(preview)
    plain_text = "\n".join(text_lines)

    has_media = isinstance(question_parts, list) and any(
        isinstance(part, dict) and str(part.get("type") or "") != "text"
        for part in question_parts
    )
    if not session_id or not has_media:
        return plain_text

    # 多模态标题：用户问题媒体部件（解析为 base64 或按视觉能力转文本占位）
    # + 资料文本部件（保证资料结构在多模态消息里依然清晰）
    from memory.file_memory import resolve_media_content_parts

    try:
        resolved, unresolved = resolve_media_content_parts(
            session_id, question_parts, vision_enabled=vision_enabled
        )
    except Exception as exc:
        print(f"[WARN] 标题请求媒体解析失败（回退纯文本标题）: {exc}")
        return plain_text
    if unresolved:
        print(f"[WARN] 标题请求 {len(unresolved)} 个媒体引用解析失败（按文本占位发送）")
    parts: list[dict[str, Any]] = [{"type": "text", "text": plain_text}]
    for part in resolved:
        if isinstance(part, dict):
            parts.append(part)
    return parts


def _clean_title_text(raw: Any) -> str:
    """清洗标题模型输出：取首个非空行、剥包裹引号与常见前缀、硬截断。"""
    text = str(raw or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    text = text.strip("\"'“”‘’「」『』《》").strip()
    for prefix in ("标题：", "标题:", "Title:", "title:", "TITLE:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text[:_TITLE_MAX_CHARS]


def resolve_title_model_config() -> tuple[dict[str, Any] | None, str]:
    """解析标题模型配置；未配置/无效返回 (None, 原因)——调用方保持旧机制标题。

    会话级生效：生成任务内 ambient 覆盖已设置（chat_factory 任务启动时），
    get_role_selection("title_model") 即该会话生效选择（会话覆盖 → 全局默认）。
    """
    try:
        selection = get_role_selection("title_model")
    except Exception as exc:
        return None, f"读取标题模型选择失败：{exc}"
    provider = str(selection.get("ownership_name") or "").strip()
    model = str(selection.get("model_name") or "").strip()
    if not provider or not model:
        return None, "未配置标题模型"
    try:
        config = get_model_config(provider, model)
    except Exception as exc:
        return None, f"读取标题模型配置失败：{exc}"
    if config is None:
        return None, f"标题模型配置不存在（{provider} / {model}）"
    if str(config.get("apiType") or "chat-completions").strip().casefold() != "chat-completions":
        return None, f"标题模型必须为 chat-completions 协议（{model}）"
    # 标题角色的自定义请求头随配置注入（ChatLLM 构造 HTTP 时读取 _custom_headers）
    return inject_custom_headers_into_config(config, get_role_headers("title_model")), ""


def _build_title_request(source_content: Any, session_id: str | None) -> ChatLLMRequest:
    """构造标题请求；标题模型的 parameter（分桶生效值）覆盖内置默认。

    source_content 为字符串（纯文本）或 OpenAI 兼容部件列表（多模态，
    媒体已由 build_title_content 解析为 base64 / 文本占位）。

    内置默认（temperature 0.3 / top_p 1.0 / presence_penalty 0.0 /
    max_tokens 64 / reasoning_effort low）面向"30 字以内标题"的小任务。
    """
    try:
        parameter = get_role_parameter("title_model") or {}
    except Exception:
        parameter = {}

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
            {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": source_content},
        ],
        max_tokens=_as_int("max_tokens", 64),
        temperature=_as_float("temperature", 0.3),
        top_p=_as_float("top_p", 1.0),
        presence_penalty=_as_float("presence_penalty", 0.0),
        stream=False,
        reasoning_effort=str(parameter.get("reasoning_effort") or "low"),
        tool_choice=None,
        parallel_tool_calls=None,
        session_id=session_id or "default",
        use_backend_history=False,
        backend_history_rounds=0,
    )
    if isinstance(extra_body, dict) and extra_body:
        request.extra_body = dict(extra_body)
    return request


async def maybe_generate_session_title(
    session_id: str,
    question_text: str,
    model_preview: str,
    question_parts: list[dict[str, Any]] | None = None,
    retitle_each_message: bool = False,
) -> str | None:
    """为会话生成一次标题；返回标题文本或 None（未配置/失败/已生成过）。

    retitle_each_message=True（会话开启「每条消息重新标题」）时跳过已生成
    标记检查，每轮收尾都覆盖式生成新标题（覆盖语义与手动重命名相同——
    用户重命名后再次发送仍会被覆盖，属该开关的显式语义）。
    """
    from memory.chat_memory import read_session_meta_value

    if retitle_each_message:
        # 每条消息重新标题：每轮收尾都生成（本轮失败也继续，不留 attempted 障碍）
        print("[INFO] 会话开启每条消息重新标题，本轮收尾将重新生成标题")
    else:
        # 标记检查：已生成过（或用户已手动重命名）→ 不再覆盖；
        # 已尝试过但未成功（attempts 非空）→ 同样不再自动重试（防止上游
        # 持续不可用时每轮任务都白发一次标题请求）；需要重试时可改用
        # 「每条消息重新标题」开关或手动重命名会话。
        state = read_session_meta_value(session_id, "_title_state")
        if isinstance(state, dict) and state.get("title_generated"):
            return None
        if isinstance(state, dict) and state.get("attempted"):
            return None

    config, reason = resolve_title_model_config()
    if config is None:
        # 未配置/配置无效：静默保持旧机制标题（仅日志，不打扰用户）
        print(f"[INFO] 会话标题保持旧机制（{reason}）")
        return None

    # 标题模型支持视觉（models.json 该模型 vision=true）且首问含媒体部件时，
    # 把媒体一并送给标题模型（首问"根据截图优化页面"没有图就无法起准标题）；
    # 不支持视觉时媒体部件转文本占位（与聊天主链路 vision=false 同口径）
    vision_enabled: bool | None = None
    if isinstance(question_parts, list):
        vision_enabled = bool(config.get("vision", False))
    source_content = build_title_content(
        question_text,
        model_preview,
        question_parts=question_parts,
        session_id=session_id,
        vision_enabled=vision_enabled,
    )
    request = _build_title_request(source_content, session_id)
    try:
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
    except Exception as exc:
        # 失败原因完整保留（含上游 403/区域限制等响应体）：所选标题模型在
        # 其网关侧可能不可用（如 OpenCode 网关按工作区对部分模型做区域门控，
        # 同 provider 下聊天模型可用不代表标题角色可用），提示用户更换
        print(f"[WARN] 标题模型调用失败（保持旧机制标题，可尝试更换标题模型）: {exc}")
        return None
    title = ""
    if isinstance(result, dict):
        title = _clean_title_text(result.get("content") or result.get("reasoning_content"))
    if not title:
        print("[WARN] 标题模型返回空内容（保持旧机制标题）")
        return None
    return title


async def apply_title_to_session(
    session_id: str,
    title: str,
    model_name: str = "",
) -> dict[str, Any] | None:
    """把标题写入会话首行 _meta（title + _title_state 标记），返回新 meta。"""
    from memory.chat_memory import get_chat_memory_manager

    manager = await get_chat_memory_manager(session_id)
    return await manager.apply_generated_title(
        title,
        {
            "source": "title_model",
            "model": str(model_name or ""),
            "applied_at": now_str(),
        },
    )


async def _generate_and_apply_title(
    session_id: str,
    question_text: str,
    preview: str,
    question_parts: list[dict[str, Any]] | None = None,
    retitle_each_message: bool = False,
) -> None:
    """后台标题任务主体：生成 → 写盘；两步各自失败静默（标题保持旧机制值）。

    非 retitle 模式下调用失败（上游 4xx/5xx、空输出等）写 attempted 标记：
    之后轮次不再自动重试（防止持续不可用的上游被每轮任务重复请求）；
    开启「每条消息重新标题」时不写该标记（每轮重试是开关的显式语义）。
    """
    from memory.chat_memory import read_session_meta_value, mark_title_attempted

    title = await maybe_generate_session_title(
        session_id, question_text, preview,
        question_parts=question_parts,
        retitle_each_message=retitle_each_message,
    )
    if not title:
        if not retitle_each_message:
            # 已有 title_generated（生成后写盘前会话被删等边缘）时不覆盖标记
            state = read_session_meta_value(session_id, "_title_state")
            if not (isinstance(state, dict) and state.get("title_generated")):
                # mark_title_attempted 为同步写盘函数（内部自互斥），直接调用
                mark_title_attempted(session_id)
        return
    config, _ = resolve_title_model_config()
    model_name = ""
    if isinstance(config, dict):
        model_name = str(config.get("selected_model_id") or config.get("selected_model_name") or "")
    meta = await apply_title_to_session(session_id, title, model_name)
    if meta is not None:
        print(f"[INFO] 会话 [{session_id}] 标题已由标题模型更新：{title}")


def _log_title_task_error(task: "asyncio.Task") -> None:
    """标题后台任务的兜底异常日志（生成/写盘异常不应无声消失）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[WARN] 会话标题任务异常终止（保持旧机制标题）: {exc}")


# 前端触发生成时的同会话防抖窗口（秒）：一轮任务只会触发一次请求，
# 防御前端异常重发/并发双击导致短时间重复请求标题模型
_FRONTEND_TRIGGER_DEBOUNCE_SECONDS = 2.0
_frontend_trigger_at: dict[str, float] = {}


async def generate_title_for_frontend(
    session_id: str,
    question_text: str,
    model_preview: str,
    question_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """前端触发入口：流内采集到预览后由前端调用一次，生成即写盘并返回标题。

    与收尾触发（_generate_and_apply_title）的差别：
    - 由前端显式发起（HTTP 请求而非后台任务），生成结果同步返回给前端，
      前端收到后直接替换侧栏标题——不再轮询 meta；
    - 已有 title_generated（首轮已生成过/用户手动重命名过）时跳过生成，
      返回现标题（前端无需替换）；「每条消息重新标题」开关不受该限制；
    - 防重复：同会话防抖窗口内的重复请求直接返回现有状态，不调标题模型。

    返回 {state, title, replaced, skipped, reason?}：
    - replaced=true：本轮新生成了标题（title 为新标题，已写盘）；
    - skipped=true：未生成（已生成过/失败/未配置），title 为当前盘上标题。
    """
    import time as _time
    from memory.chat_memory import read_session_meta_value

    session_id = str(session_id or "").strip()
    if not session_id:
        return {"state": "failed", "title": "", "replaced": False, "skipped": True, "reason": "缺少会话 ID"}

    # 同会话防抖：窗口内的重复请求不调标题模型（返回当前标题）
    now = _time.monotonic()
    last = _frontend_trigger_at.get(session_id)
    _frontend_trigger_at[session_id] = now
    if last is not None and (now - last) < _FRONTEND_TRIGGER_DEBOUNCE_SECONDS:
        existing_title = read_session_meta_value(session_id, "title") or ""
        return {
            "state": "succeed", "title": str(existing_title),
            "replaced": False, "skipped": True, "reason": "防抖窗口内的重复请求",
        }

    retitle_each = False
    try:
        retitle_each = bool(read_session_meta_value(session_id, "retitle_each_message"))
    except Exception:
        retitle_each = False
    # 已生成过（首轮生成/手动重命名）：不再覆盖——直接回显当前标题
    if not retitle_each:
        state = read_session_meta_value(session_id, "_title_state")
        if isinstance(state, dict) and state.get("title_generated"):
            existing_title = read_session_meta_value(session_id, "title") or ""
            return {
                "state": "succeed", "title": str(existing_title),
                "replaced": False, "skipped": True, "reason": "标题已生成过",
            }

    title = await maybe_generate_session_title(
        session_id, question_text, model_preview,
        question_parts=question_parts,
        retitle_each_message=retitle_each,
    )
    if not title:
        # 失败（未配置/调用失败/空输出）：收尾触发路径同款 attempted 标记，
        # 防止持续不可用的上游被每轮任务重复请求
        from memory.chat_memory import mark_title_attempted

        current_state = read_session_meta_value(session_id, "_title_state")
        if not (isinstance(current_state, dict) and current_state.get("title_generated")):
            mark_title_attempted(session_id)
        existing_title = read_session_meta_value(session_id, "title") or ""
        return {
            "state": "succeed", "title": str(existing_title),
            "replaced": False, "skipped": True, "reason": "标题模型未返回标题（保持旧机制标题）",
        }

    config, _ = resolve_title_model_config()
    model_name = ""
    if isinstance(config, dict):
        model_name = str(config.get("selected_model_id") or config.get("selected_model_name") or "")
    meta = await apply_title_to_session(session_id, title, model_name)
    if meta is None:
        existing_title = read_session_meta_value(session_id, "title") or ""
        return {
            "state": "succeed", "title": str(existing_title),
            "replaced": False, "skipped": True, "reason": "标题写盘失败",
        }
    print(f"[INFO] 会话 [{session_id}] 标题已由标题模型更新（前端触发）：{title}")
    return {"state": "succeed", "title": title, "replaced": True, "skipped": False}


def schedule_title_generation(
    session_id: str,
    question_text: str,
    collector: TitleSourceCollector | None = None,
    question_parts: list[dict[str, Any]] | None = None,
) -> None:
    """任务收尾触发点（遗留入口，当前主链路已不再调用）。

    聊天主链路的标题触发已解耦为前端驱动：前端收集 SSE 流内输出达 100
    字符后调 POST /chat_config/generate_title（generate_title_for_frontend），
    聊天循环不再感知标题任务。本函数保留：内部范式（后台任务/防重复/
    attempted 标记）与 generate_title_for_frontend 共用，且仍被单元测试
    覆盖；未来若需要服务端自主兜底触发可复用。

    在生成任务的事件循环内 create_task：ambient 会话级模型覆盖随上下文
    复制，标题任务内的角色配置读取与主任务一致（会话覆盖 → 全局默认）。
    question_parts 为首个提问的原始 content 部件列表（多模态标题用）。

    「每条消息重新标题」为会话独立配置（_meta.retitle_each_message，
    前端聊天设置弹窗开关）：开启时每轮收尾都重新生成标题（question_parts
    仅首轮提供，后续轮次纯文本）；未开启时仅首轮任务收尾尝试一次。
    注意：worker 进程模式下任务在该 worker 内执行（写盘靠跨进程文件锁
    互斥）；worker 空闲自动退出极端情况下可能中断标题任务。
    """
    try:
        preview = collector.snapshot() if collector is not None else ""
        retitle_each = False
        try:
            from memory.chat_memory import read_session_meta_value
            raw_flag = read_session_meta_value(session_id, "retitle_each_message")
            retitle_each = bool(raw_flag)
        except Exception:
            retitle_each = False
        task = asyncio.create_task(
            _generate_and_apply_title(
                session_id, question_text, preview,
                question_parts=question_parts,
                retitle_each_message=retitle_each,
            )
        )
        task.add_done_callback(_log_title_task_error)
    except Exception as exc:
        print(f"[WARN] 会话标题任务启动失败（保持旧机制标题）: {exc}")
