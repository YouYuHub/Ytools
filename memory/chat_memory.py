""" 记录当前轮次对话所有工具调用的历史 - 支持多会话文件持久化 """
# from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import copy
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# fastapi
from fastapi import HTTPException
from fastapi.responses import FileResponse

from factory.agent_runtime.builtin_tools import ASK_ANSWER_PREFIX, ASK_USER_TOOL_NAME
from memory.chat_round_store import (
    ChatRoundStore,
    compression_usage_values,
    merge_compression_usage,
    merge_usage_dict,
    parse_round_entry,
)
from util.file_lock import cross_process_lock
from util.timestamp_utils import now_str
from config import (
    DEFAULT_CONTEXT_HISTORY_ROUNDS,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    get_global_tool_inputs,
    get_persisted_work_dir,
    normalize_tool_inputs,
    resolve_work_dir,
)

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)

# 会话上传文件的根目录（与 memory.file_memory.HISTORY_ROOT 保持一致）
UPLOAD_HISTORY_ROOT = HISTORY_ROOT / "upload"

# 跨进程写锁文件的统一存放目录：避免 *.lock 散落在历史文件根目录
# （与 <session>_chat.jsonl、<session>_chat.jsonl.pending 混在一起）
LOCK_ROOT = HISTORY_ROOT / "lock"


def _migrate_legacy_lock_files() -> None:
    """把旧版散落在 history_files 根目录的 *.lock 迁移到 lock/ 子目录。

    锁文件内容为空、只承载句柄互斥语义，迁移后新旧路径不要求互通：
    服务重启（部署本改动）后所有进程统一使用新路径。个别文件正被
    其他进程持有时 Windows 会拒绝移动，静默跳过留待下次启动再迁。
    """
    try:
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        for legacy in HISTORY_ROOT.glob("*.lock"):
            try:
                legacy.replace(LOCK_ROOT / legacy.name)
            except OSError:
                pass
    except OSError:
        pass


_migrate_legacy_lock_files()



def _safe_session_id(session_id: str) -> str:
    """把 session_id 规整为安全文件名片段。

    会话标识以文件名为准，因此必须无损保留合法的文件名字符：
    - 保留 Unicode 字母/数字（含中文）、空格、下划线、点、横线
    - 路径分隔符等其余字符替换为下划线
    - 拦截 `..` 防止路径穿越
    """
    value = re.sub(r"[^\w .-]+", "_", session_id)
    value = value.replace("..", "_")
    return value.strip(" ._-")


def _get_chat_history_file(session_id: str) -> Path:
    safe_id = _safe_session_id(session_id)
    return HISTORY_ROOT / f"{safe_id}_chat.jsonl"


def _pid_alive(pid: Any) -> bool:
    """判断进程是否存活（Windows/POSIX 通用，尽力而为）。

    检查点写者存活校验用：写者进程仍活着时不能把检查点恢复成历史轮次。
    psutil 不可用时 Windows 用 OpenProcess 探测（非破坏性，不向目标进程
    发信号）；POSIX 用 os.kill(pid, 0) 存在性检查。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # 打开失败但错误码为拒绝访问 → 进程存在（通常是系统保护进程）
            return kernel32.GetLastError() == ERROR_ACCESS_DENIED
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# def _write_jsonline(file_path: Path, data: dict[str, Any]) -> None:
#     with file_path.open("a+", encoding="utf-8") as fp:
#         fp.write(json.dumps(data, ensure_ascii=False) + "\n")


def _read_jsonlines(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _default_title(session_id: str) -> str:
    return f"session_{session_id}"


def _default_meta(session_id: str) -> dict[str, Any]:
    now = now_str()
    return {
        "title": _default_title(session_id),
        "user_questions": [],
        "todo": [],
        "usage": {},
        "created_at": now,
        "updated_at": now,
        "record_count": 0,
        "completion_count": 0,
        "context_summary": None,
        "compress_usage": {},
        # 会话独立工作目录：None 表示未覆盖，跟随全局默认 DEFAULT_CHAT_WORK_DIR。
        # 惰性写入——只有用户显式设置过才落盘，新会话不预填
        "work_dir": None,
        # 会话独立工具选择：None 表示未覆盖，跟随全局默认（mcp_servers.json 的 inputs 键）。
        # 结构与 inputs 一致：{服务名: [工具名]}；空选择视为未覆盖（清空即恢复跟随全局）
        "tool_selection": None,
        # 会话独立模型选择：None 表示未覆盖，跟随全局默认（models.json 顶层 model_selection）。
        # 结构为 {角色: {ownership_name, model_name, parameter, api_type}}，仅存已覆盖的角色；
        # 空选择/非法条目视为未覆盖（清空即恢复跟随全局）
        "model_selection": None,
    }


def normalize_session_id(raw: Any) -> str:
    """把任意输入规整为合法 session_id。

    会话标识以文件名为准（`history_files/<session_id>_chat.jsonl`），
    因此这里会：
    - 兼容直接传入文件名（去掉 `_chat.jsonl` / `.jsonl` 后缀）
    - 无损保留合法的文件名字符（中文等 Unicode 字符、空格、`_ . -`），
      仅把路径分隔符等危险字符替换为下划线（与 _safe_session_id 一致）
    - 空值回退为 "default"
    """
    if raw is None:
        return "default"
    value = str(raw).strip()
    # 兼容直接传入文件名（如 web-xxx_chat.jsonl / web-xxx.jsonl）
    value = re.sub(r"_chat\.jsonl$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.jsonl$", "", value, flags=re.IGNORECASE)
    value = _safe_session_id(value).strip(" ._-")
    return value or "default"


def _is_meta_record(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("_meta"), dict)


def read_session_meta_value(session_id: str, key: str) -> Any:
    """只读读取会话首行 _meta 的指定字段（不创建文件/目录，不触发重算落盘）。

    供解析会话工作目录等轻量场景使用；文件不存在或首行非 _meta 返回 None。
    """
    try:
        file_path = _get_chat_history_file(normalize_session_id(session_id))
    except Exception:
        return None
    if not file_path.exists():
        return None
    rows = _read_jsonlines(file_path)
    if rows and _is_meta_record(rows[0]):
        meta = rows[0].get("_meta")
        if isinstance(meta, dict):
            return meta.get(key)
    return None


def resolve_session_work_dir(session_id: str) -> tuple[str | None, str | None]:
    """解析会话生效工作目录（惰性：不落盘、不 chdir）。

    解析顺序（会话覆盖优先，全局默认兜底）：
    1. `_meta.work_dir` 存在且目录有效 → 直接生效；
    2. `_meta.work_dir` 存在但已失效 → 记录警告，回退全局默认；
    3. `.env` 的 DEFAULT_CHAT_WORK_DIR 有效 → 生效（新会话的初始目录）；
    4. 都没有 → (None, None)，调用方保持当前进程 cwd。

    Returns:
        (生效目录绝对路径或 None, 警告消息或 None)
    """
    warning: str | None = None
    override = read_session_meta_value(session_id, "work_dir")
    if isinstance(override, str) and override.strip():
        resolved = resolve_work_dir(override)
        if resolved is not None:
            return str(resolved), None
        warning = f"会话工作目录已失效（{override}），已回退默认工作目录"
    persisted = get_persisted_work_dir()
    if isinstance(persisted, str) and persisted.strip():
        resolved = resolve_work_dir(persisted)
        if resolved is not None:
            return str(resolved), warning
        warning = warning or f"默认工作目录已失效（{persisted}），保持当前目录"
    return None, warning


def resolve_session_tool_selection(
    session_id: str,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """解析会话生效工具选择（惰性：只读，不落盘）。

    解析顺序（会话覆盖优先，全局默认兜底）：
    1. `_meta.tool_selection` 为非空 {服务名: [工具名]} → 直接生效；
       请求未携带 tool_names 时由生成流程用它作为本轮工具；
    2. 未覆盖/空选择/非法 → 回退全局默认（mcp_servers.json 的 inputs 键）；
    3. 全局配置读取失败 → 记录警告，返回 (None, 警告)，调用方按无默认工具处理。

    Returns:
        (生效工具选择或 None, 警告消息或 None)
    """
    override = read_session_meta_value(session_id, "tool_selection")
    normalized_override = normalize_tool_inputs(override) if override is not None else None
    if normalized_override:
        return normalized_override, None
    inputs, _servers, error = get_global_tool_inputs()
    if error:
        return None, f"全局默认工具配置读取失败（{error}），本轮按未配置默认工具处理"
    return (inputs or None), None


def resolve_session_model_selection(
    session_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """解析会话生效模型选择（惰性：只读，不落盘；返回全部角色的合并结果）。

    解析顺序（按角色独立覆盖，全局默认兜底）：
    1. `_meta.model_selection` 中该角色的覆盖条目存在且模型仍在 models.json 目录中 → 生效；
    2. 覆盖条目引用的模型已不存在（如 models.json 被手工编辑）→ 记录警告，该角色回退全局默认；
    3. 未覆盖的角色直接使用全局默认（models.json 顶层 model_selection）。

    Returns:
        ({role: {ownership_name, model_name, parameter, api_type}} 全角色合并结果, 警告消息列表)
    """
    effective = _get_global_model_selection()
    warnings: list[str] = []
    override = read_session_meta_value(session_id, "model_selection")
    normalized_override = _normalize_model_selection(override) if override is not None else {}
    for role in _MODEL_SELECTION_ROLES:
        entry = normalized_override.get(role) if isinstance(normalized_override, dict) else None
        if not (isinstance(entry, dict) and entry.get("ownership_name") and entry.get("model_name")):
            continue
        provider = entry.get("ownership_name")
        model = entry.get("model_name")
        if _get_model_config(provider, model) is None:
            warnings.append(
                f"会话独立{role} 已失效（{provider} / {model} 不在 models.json 中），已回退全局默认"
            )
            continue
        effective[role] = entry
    return effective, warnings


# 摘要/历史格式化已迁移到 memory.chat_history_format，本文件仅保留调用入口
from memory.chat_history_format import (
    normalize_context_summary as _normalize_context_summary,
    render_context_summary as _render_context_summary,
    render_recent_questions_message as _render_recent_questions_message,
    extract_recent_questions as _extract_recent_questions,
    extract_recent_question_items as _extract_recent_question_items,
    split_context_window as _split_context_window,
    RECENT_QUESTIONS_TOKEN_BUDGET,
    round_entry_to_context_messages as _round_entry_to_context_messages,
    round_entry_compression_usage as _round_entry_compression_usage,
    format_tool_call_history_line as _format_tool_call_history_line,
    _round_question as _extract_round_question,
)
from env_manager import load_var as _load_var
from env_manager import normalize_model_selection as _normalize_model_selection
from env_manager import get_model_config as _get_model_config
from env_manager import get_global_model_selection as _get_global_model_selection
from env_manager import MODEL_SELECTION_ROLES as _MODEL_SELECTION_ROLES
from factory.agent_runtime.chat_runtime import (
    estimate_messages_tokens as _estimate_messages_tokens,
    estimate_request_context_tokens as _estimate_request_context_tokens,
    estimate_text_tokens as _estimate_text_tokens,
    estimate_tool_definition_tokens as _estimate_tool_definition_tokens,
    parse_return_length as _parse_return_length,
    resolve_model_max_input_tokens as _resolve_model_max_input_tokens,
)


def _default_context_history_rounds() -> int:
    """读取当前历史压缩设置，作为上下文 API 省略轮数时的默认值。

    0 表示无限轮次窗口（仅按阈值压缩），原样返回。
    """
    try:
        value = int(_load_var("HISTORY_COMPACT_KEEP_ROUNDS", DEFAULT_CONTEXT_HISTORY_ROUNDS) or 0)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_HISTORY_ROUNDS
    return value if value >= 0 else DEFAULT_CONTEXT_HISTORY_ROUNDS


def _normalize_usage_record_usage(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    usage_total = {}
    completion_count = 0
    if entry.get("event") == "chat_round":
        round_total = entry.get("usage_total")
        if isinstance(round_total, dict):
            usage_total = round_total
        if isinstance(entry.get("completion_count"), int):
            completion_count = entry.get("completion_count", 0)
    return usage_total, completion_count


# 上述格式化函数已迁移至 memory/chat_history_format.py（统一入口）
# 为保留向后兼容，旧名字仍然指向新实现，供其他模块使用。


def _recompute_meta_from_entries(
    session_id: str,
    base_meta: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    bump_updated_at: bool = True,
) -> dict[str, Any]:
    now = now_str()
    meta = dict(base_meta) if isinstance(base_meta, dict) else _default_meta(session_id)
    # 会话标识以文件名为准，不再写入 _meta；每次重算都要清理，
    # 因为上传的 jsonl（如 web-xxx_chat.jsonl）其 _meta 里仍带 session_id，
    # 不是“只清理一次”就能完成的
    meta.pop("session_id", None)
    if not meta.get("created_at"):
        meta["created_at"] = now
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        meta["title"] = _default_title(session_id)
    questions: list[str] = []
    usage_total: dict[str, Any] = {}
    compress_usage_total: dict[str, Any] = {}
    completion_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "chat_round":
            # question 为空时回退从事件提取（多模态消息的文本部件），
            # 兼容修复前已落盘的空 question 轮次，重建 user_questions / 会话标题
            question = _extract_round_question(entry)
            if question:
                questions.append(question)
        entry_usage_total, entry_completion_count = _normalize_usage_record_usage(entry)
        if entry_usage_total:
            merge_usage_dict(usage_total, entry_usage_total)
        if entry.get("event") == "chat_round":
            merge_compression_usage(
                compress_usage_total,
                _round_entry_compression_usage(entry),
            )
        completion_count += entry_completion_count
    history_compress_usage = meta.get("_history_compress_usage")
    if merge_compression_usage(compress_usage_total, history_compress_usage):
        merge_usage_dict(usage_total, compression_usage_values(history_compress_usage))
    active_round_compaction = meta.get("_active_round_compaction")
    if isinstance(active_round_compaction, dict):
        active_usage = _round_entry_compression_usage({
            "events": active_round_compaction.get("events", []),
        })
        if not active_usage:
            active_usage = active_round_compaction.get("compress_usage")
        if merge_compression_usage(compress_usage_total, active_usage):
            merge_usage_dict(usage_total, compression_usage_values(active_usage))
    meta["user_questions"] = questions
    meta["usage"] = usage_total
    meta["compress_usage"] = compress_usage_total
    meta["record_count"] = len(entries)
    meta["completion_count"] = completion_count
    # 会话工作目录：显式 None 表示未覆盖（保留）；非字符串/空白视为非法清除。
    # 目录是否存在不在重算时校验（失效由 resolve_session_work_dir 回退处理），
    # 避免目录临时被占用（如重命名）时静默丢失用户配置
    work_dir = meta.get("work_dir")
    if work_dir is not None and (not isinstance(work_dir, str) or not work_dir.strip()):
        meta.pop("work_dir", None)
    # 会话工具选择：None 表示未覆盖（保留）；非 dict 或规整后为空（无有效工具条目）
    # 视为未覆盖清除，恢复跟随全局默认 inputs
    tool_selection = meta.get("tool_selection")
    if tool_selection is not None:
        normalized_selection = normalize_tool_inputs(tool_selection)
        if normalized_selection:
            meta["tool_selection"] = normalized_selection
        else:
            meta.pop("tool_selection", None)
    # 会话模型选择：None 表示未覆盖（保留）；仅保留配置完整的角色条目
    # （模型是否仍存在不在重算时校验，失效由 resolve_session_model_selection 回退全局处理），
    # 非法结构或空选择视为未覆盖清除
    model_selection_override = meta.get("model_selection")
    if model_selection_override is not None:
        normalized_models = _normalize_model_selection(model_selection_override)
        valid_models = {
            role: entry for role, entry in normalized_models.items()
            if entry.get("ownership_name") and entry.get("model_name")
        }
        if valid_models:
            meta["model_selection"] = valid_models
        else:
            meta.pop("model_selection", None)
    # 只有真实写入（追加轮次 / 改标题 / 删除记录 / 导入）才刷新 updated_at；
    # 纯读取（如打开会话列表时逐个拉 meta）不能改动它，
    # 否则“按最近更新排序”会退化为“按最后一次 meta 读取顺序排序”，顺序失真
    if bump_updated_at:
        meta["updated_at"] = now
    elif not meta.get("updated_at"):
        meta["updated_at"] = now
    meta["context_summary"] = _normalize_context_summary(meta.get("context_summary"))
    if (
        meta.get("title") == _default_title(session_id)
        and questions
    ):
        first_q = questions[0]
        meta["title"] = first_q[:40] if first_q else _default_title(session_id)
    return meta


def _load_meta_and_entries(
    file_path: Path,
    session_id: str,
    rows: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if rows is None:
        rows = _read_jsonlines(file_path)
    if rows and _is_meta_record(rows[0]):
        base_meta = rows[0].get("_meta") or _default_meta(session_id)
        entries = [row for row in rows[1:] if isinstance(row, dict)]
    else:
        base_meta = _default_meta(session_id)
        entries = [row for row in rows if isinstance(row, dict)]
    meta = _recompute_meta_from_entries(session_id, base_meta, entries, bump_updated_at=False)
    return meta, entries


def _read_meta_first_line(file_path: Path) -> dict[str, Any] | None:
    """只读 jsonl 首行解析 _meta；首行缺失/损坏/非 _meta 记录时返回 None。

    写路径（追加轮次/改标题/删除/导入等）总是把重算后的完整 _meta 落在
    首行，因此正常文件单行读取即可拿到与全量重算一致的元数据；
    返回 None 时调用方应回退 _load_meta_and_entries 全量路径兜底。
    """
    try:
        with file_path.open("r", encoding="utf-8") as fp:
            first_line = fp.readline().strip()
    except OSError:
        return None
    if not first_line:
        return None
    try:
        row = json.loads(first_line)
    except ValueError:
        return None
    if not _is_meta_record(row):
        return None
    meta = row.get("_meta")
    return meta if isinstance(meta, dict) else None


# 原子写重试退避序列（秒）：Windows 上刚写入的文件可能被搜索索引、杀软或
# 其他进程短暂占用（open / os.replace 偶发 WinError 5），指数退避把重试窗口
# 拉长到约 2.6s 基本覆盖占用时长；仍失败由调用方决定中止还是降级。
_ATOMIC_WRITE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0)


def _write_meta_and_entries(file_path: Path, meta: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    # 先写临时文件再原子替换：流式期间其他请求（如 /chat_history/file 读取）
    # 不会读到写了一半的文件，避免并发竞态导致记录丢失。
    # Windows 上临时文件与目标文件都可能被搜索索引/杀软短暂占用，临时文件
    # 的 open 与 os.replace 偶发 WinError 5，指数退避重试规避；全部失败则抛出。
    tmp_path = file_path.with_name(file_path.name + ".tmp")
    last_error: OSError | None = None
    for delay in _ATOMIC_WRITE_RETRY_DELAYS:
        try:
            with tmp_path.open("w", encoding="utf-8") as fp:
                fp.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
                for entry in entries:
                    fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, file_path)
            return
        except OSError as err:
            last_error = err
            time.sleep(delay)
    raise last_error


def _delete_path_with_retry(path: Path, *, recursive: bool = False, attempts: int = 6) -> None:
    """删除文件或目录，带重试。

    与 _write_meta_and_entries 同理：Windows 上文件/目录可能被搜索索引、杀软
    或刚关闭的句柄短暂占用，首次删除偶发 WinError 5；重试几次规避，
    仍失败则抛出 OSError 交由上层处理。
    """
    for attempt in range(attempts):
        try:
            if recursive:
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


class ChatMemoryManager:
    """工具调用历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._run_task = True
        self._lock = threading.Lock()
        self._file_path = _get_chat_history_file(session_id)
        self._round_store = ChatRoundStore(session_id)
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        # 仅在文件缺失时 touch：touch 对已存在的文件也会刷新 mtime，
        # 服务重启后逐会话实例化会平白搅动所有历史文件的修改时间；
        # exist_ok=True 兜住"检查后被他进程抢先创建"的竞态窗口
        if not self._file_path.exists():
            self._file_path.touch(exist_ok=True)
        with self._write_guard():
            rows = _read_jsonlines(self._file_path)
            stored_meta = rows[0].get("_meta") if (rows and _is_meta_record(rows[0])) else None
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id, rows=rows)
            # 首行 _meta 与重算结果一致（写路径总是落盘重算后的 _meta）时跳过重写：
            # 服务重启后每个会话首次实例化都会走到这里，无变化的全量重写
            # （临时文件 + os.replace）会成批拖慢重启后的首个首屏加载；
            # 仅新文件/旧格式/内容漂移时才物化 _meta 首行
            if stored_meta != meta:
                _write_meta_and_entries(self._file_path, meta, entries)
        self._recover_pending_round_checkpoint()

    @property
    def _meta_lock_path(self) -> Path:
        """跨进程写锁文件：统一放 history_files/lock/ 下（只创建不删除），
        避免与 JSONL、.pending 检查点混在同一目录。"""
        return LOCK_ROOT / (self._file_path.name + ".lock")

    @contextmanager
    def _write_guard(self):
        """写路径统一守卫：跨进程文件锁（OS 级，崩溃自动释放）+ 进程内线程锁。

        生成任务在独立 worker 进程写轮次/压缩/usage，主进程写标题/删除/导入/
        上传记录，双方都是「读全量→改→原子替换」，必须互斥，否则后写者会
        用自己的旧快照覆盖对方的更新。
        """
        with cross_process_lock(self._meta_lock_path):
            with self._lock:
                yield

    def _recover_pending_round_checkpoint(self) -> None:
        """进程重启后恢复上次未收尾的轮次（工具调用/思考/工具结果不丢）。

        仅在管理器新建时执行：检查点由活跃轮次逐事件写入，而活跃写进程
        一定持有缓存的管理器实例——走到新建，即说明写入它的进程已结束，
        不会误伤仍在进行中的轮次。恢复为 interrupted 状态的历史轮次，
        user_questions/标题/回放照常可用。

        写者存活校验：生成任务在每会话独立 worker 进程中执行（与主进程
        内存隔离），主进程因读接口新建管理器时不能假设"写进程已结束"。
        检查点记录写入方 PID，恢复前确认该进程已不存在（重启后 PID 复用
        的窗口极小，且必须恰好是本主进程/另一 worker 才可能误判）；写者
        仍存活时说明轮次仍在生成，保留检查点交由真正的收尾逻辑处理。
        """
        with self._write_guard():
            recovered = None
            try:
                if self._pending_checkpoint_path.exists():
                    recovered = json.loads(self._pending_checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                recovered = None
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                return
            # 写者进程仍存活：轮次还在生成中（多进程架构下主进程读接口
            # 新建管理器会走到这里），绝不能恢复成 interrupted 历史
            writer_pid = recovered.get("writer_pid")
            if isinstance(writer_pid, int) and _pid_alive(writer_pid):
                return
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            legacy_checkpoint = meta.pop("_pending_round_checkpoint", None)
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                recovered = legacy_checkpoint
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                return
            normalized = dict(recovered)
            if normalized.get("status") == "running":
                normalized["status"] = "interrupted"
            normalized.setdefault("ended_at", normalized.get("started_at"))
            # 幂等去重：同轮（started_at + 首个用户事件一致）已存在于历史时
            # 不再追加，只清理残留检查点。避免恢复逻辑异常路径下产生重复轮次
            if not self._round_already_recorded(entries, normalized):
                entries.append(normalized)
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        self._clear_pending_checkpoint()

    @staticmethod
    def _round_already_recorded(entries: list[dict[str, Any]], recovered_round: dict[str, Any]) -> bool:
        """判断恢复轮次是否已完整落盘过（按 started_at + 首个用户事件匹配）。

        正常收尾的轮次 events 应为恢复快照的超集（恢复后收尾只会继续追加）。
        """
        started_at = recovered_round.get("started_at")
        if not started_at:
            return False
        recovered_user = next(
            (
                event for event in (recovered_round.get("events") or [])
                if isinstance(event, dict) and event.get("role") == "user"
            ),
            None,
        )
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("event") != "chat_round":
                continue
            if entry.get("started_at") != started_at:
                continue
            if recovered_user is None:
                return True
            existing_user = next(
                (
                    event for event in (entry.get("events") or [])
                    if isinstance(event, dict) and event.get("role") == "user"
                ),
                None,
            )
            if existing_user is not None and existing_user == recovered_user:
                return True
        return False

    @property
    def run_task(self) -> bool:
        return self._run_task

    @run_task.setter
    def run_task(self, value: bool) -> None:
        """
        优雅控制对话启停功能
        """
        self._run_task = value

    async def get_file_text(self) -> str | None:
        """锁内读取完整 jsonl 文本（文件不存在返回 None），
        避免与并发写入交替读写读到不完整内容。"""
        with self._lock:
            if not self._file_path.exists():
                return None
            return self._file_path.read_text(encoding="utf-8")

    async def add_chat_history(self, input_text: Any) -> str:
        """
        追加工具调用记录到 JSONL 文件中
        Args:
            input_text: 要记录的内容，可以是字符串或字典
        Returns:
            操作结果字符串
        """
        record: dict[str, Any] = {
            "timestamp": now_str()
        }
        if isinstance(input_text, str):
            record["content"] = input_text
        elif isinstance(input_text, dict):
            record.update(input_text)
        else:
            record["content"] = str(input_text)
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            had_pending_round = self._round_store.pending_round is not None
            completed_round = self._round_store.record_message(record)
            started_new_round = (
                not had_pending_round
                and self._round_store.pending_round is not None
            )
            meta_changed = False
            # 上次进程异常退出后可能留下活动轮次检查点；新轮次开始时清掉，
            # 避免把上一轮的压缩状态误认成当前任务状态。
            if started_new_round and "_active_round_compaction" in meta:
                meta.pop("_active_round_compaction", None)
                meta_changed = True
            if completed_round is not None:
                meta.pop("_active_round_compaction", None)
                # 轮次已收尾：检查点完成使命，连同最终事件一起落盘
                self._clear_pending_checkpoint()
                entries.append(completed_round)
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
            else:
                # 收尾前的轮次事件只存在内存，重启/崩溃即丢失（工具调用、
                # 思考过程、工具结果都是不可再生的数据）。追加后把 pending
                # 轮次快照写入侧车文件（仅当前轮事件，体量小、原子替换）；
                # 不做全历史重写——大会话逐事件重写 JSONL 会卡住整个请求。
                checkpoint = self._round_store.snapshot_pending_round()
                if checkpoint is not None:
                    self._write_pending_checkpoint(checkpoint)
                elif meta_changed:
                    meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                    _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    @property
    def _pending_checkpoint_path(self) -> Path:
        return self._file_path.with_name(self._file_path.name + ".pending")

    def _read_pending_round_snapshot(self) -> dict[str, Any] | None:
        """读取侧车检查点中的进行中轮次快照（只读，供主进程 token 统计兜底）。

        生成任务在会话 worker 进程中执行时，pending_round 只存在于 worker
        内存，主进程读 JSONL 只能看到已完成轮次——任务过程中上下文统计
        永远不增长。检查点由 worker 逐事件原子覆盖写入，这里只读该文件，
        把进行中轮次纳入统计（绝不修改文件，不参与崩溃恢复）。
        """
        try:
            if not self._pending_checkpoint_path.exists():
                return None
            raw = json.loads(self._pending_checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("event") != "chat_round":
            return None
        if raw.get("status") != "running":
            return None
        writer_pid = raw.get("writer_pid")
        # 写者已死亡仍残留的检查点是崩溃遗留（等恢复流程清理），不计入统计；
        # _pid_alive 对无效/负数/不存在 PID 一律返回 False，无需额外判断
        if isinstance(writer_pid, int) and not _pid_alive(writer_pid):
            return None
        snapshot = dict(raw)
        snapshot.pop("writer_pid", None)
        return snapshot

    def _write_pending_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """侧车检查点：只含当前轮快照，写临时文件后原子替换。

        快照附带 writer_pid（当前写进程 PID）：恢复方据此判断写者是否仍
        存活——多进程架构下主进程读接口新建管理器时，不能把仍被 worker
        写入中的轮次误恢复成 interrupted 历史。

        Windows 上刚写入的文件可能被搜索索引/杀软短暂占用导致 open 与
        os.replace 偶发 WinError 5，按 _ATOMIC_WRITE_RETRY_DELAYS 指数退避重试。
        检查点只服务崩溃恢复，重试耗尽不抛错：保留旧检查点并打警告继续本轮
        （丢的只是最近一条事件的恢复点，下一条事件写入会重新覆盖）；若抛错
        会经 add_chat_history 打断整个生成轮次，把偶发文件占用放大成任务失败。
        """
        payload_checkpoint = dict(checkpoint)
        if payload_checkpoint.get("event") == "chat_round":
            payload_checkpoint.setdefault("writer_pid", os.getpid())
        tmp_path = self._pending_checkpoint_path.with_name(self._pending_checkpoint_path.name + ".tmp")
        payload = json.dumps(payload_checkpoint, ensure_ascii=False)
        last_error: OSError | None = None
        for delay in _ATOMIC_WRITE_RETRY_DELAYS:
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, self._pending_checkpoint_path)
                return
            except OSError as err:
                last_error = err
                time.sleep(delay)
        print(f"[WARN] 会话 {self.session_id} 轮次检查点写入失败（保留旧检查点，本轮继续）：{last_error}")

    def _clear_pending_checkpoint(self) -> None:
        try:
            self._pending_checkpoint_path.unlink()
        except OSError:
            pass

    async def stop_current_round(self) -> str:
        """用户手动停止任务：直接以 stopped 状态收尾当前轮次，不写入"停止任务"假消息。"""
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            completed_round = self._round_store.finalize_round("stopped")
            if completed_round is not None:
                meta.pop("_active_round_compaction", None)
                self._clear_pending_checkpoint()
                entries.append(completed_round)
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def truncate_rounds_for_reanswer(self) -> int:
        """覆盖式重新回答：删除最近 ask_user 提问轮之后的所有轮次。

        场景：最新提问已经回答过（其后存在旧的回答轮），用户再次回答同一
        提问时应覆盖而不是追加，保证一个问题只有一个答案轮次。

        保护条件：仅当提问轮之后的第一轮以回答格式（ASK_ANSWER_PREFIX 前缀）
        开始时才截断——提问后若已开启普通新任务，则不动历史。
        返回删除的轮次数；无需截断（无提问轮 / 首次回答 / 保护命中）返回 0。
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            # 从后往前找最近一个包含 ask_user 工具调用的轮次
            ask_idx = None
            for idx in range(len(entries) - 1, -1, -1):
                entry = entries[idx]
                if not isinstance(entry, dict) or entry.get("event") != "chat_round":
                    continue
                has_ask_call = any(
                    isinstance(event, dict)
                    and event.get("role") == "assistant"
                    and any(
                        isinstance(tool_call, dict)
                        and isinstance(tool_call.get("function"), dict)
                        and tool_call["function"].get("name") == ASK_USER_TOOL_NAME
                        for tool_call in (event.get("tool_calls") or [])
                    )
                    for event in (entry.get("events") or [])
                )
                if has_ask_call:
                    ask_idx = idx
                    break
            if ask_idx is None:
                return 0
            removed = entries[ask_idx + 1:]
            if not removed:
                return 0  # 首次回答：提问轮之后还没有任何轮次
            # 保护：提问轮之后的第一轮必须是回答轮，否则不截断
            first_question = removed[0].get("question") if isinstance(removed[0], dict) else ""
            if not str(first_question or "").startswith(ASK_ANSWER_PREFIX):
                return 0
            entries = entries[:ask_idx + 1]
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
            return len(removed)

    async def update_current_round_usage(
        self,
        usage_total: dict[str, Any],
        completion_count: int = 0,
    ) -> str:
        """将当前轮次的 usage 汇总挂到内存中的 pending round。"""
        with self._lock:
            return self._round_store.add_usage(usage_total, completion_count)

    async def get_current_round_tool_result_count(self) -> int:
        """返回当前任务已经记录的工具结果数量，作为单轮压缩游标。"""
        with self._lock:
            return self._round_store.current_tool_result_count()

    async def update_current_round_compaction(
        self,
        compress_content: str,
        compress_index: int,
        compress_usage: dict[str, Any] | None = None,
        compress_blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """兼容旧调用方：把当前轮累计压缩摘要写成 round done 事件。

        pending round 最终收尾时只会把事件写入 `chat_round.events`；活动期间
        同步写入 `_meta._active_round_compaction`，避免任务中断时压缩状态完全丢失。
        """
        with self._write_guard():
            if not self._round_store.update_compaction(
                compress_content,
                compress_index,
                compress_blocks=compress_blocks,
                compress_usage=compress_usage,
            ):
                return "忽略无有效压缩状态"
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            pending_round = self._round_store.pending_round or {}
            meta["_active_round_compaction"] = {
                "question": pending_round.get("question", ""),
                "events": [
                    event for event in pending_round.get("events", [])
                    if isinstance(event, dict) and event.get("event") == "context_compaction"
                ],
                "started_at": pending_round.get("started_at"),
            }
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def add_context_compaction_event(self, payload: dict[str, Any]) -> str:
        """追加压缩过程事件。

        round scope 写入当前 pending `chat_round.events`，session scope 仍写入独立
        JSONL 事件行。timestamp 由本方法补充。
        """
        if not isinstance(payload, dict) or payload.get("event") != "context_compaction":
            return "忽略非法压缩事件"
        record = dict(payload)
        record.setdefault("timestamp", now_str())
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if record.get("scope") == "round":
                if not self._round_store.record_compaction_event(record):
                    return "忽略无进行中轮次或非法压缩事件"
                pending_round = self._round_store.pending_round or {}
                meta["_active_round_compaction"] = {
                    "question": pending_round.get("question", ""),
                    "events": [
                        event for event in pending_round.get("events", [])
                        if isinstance(event, dict) and event.get("event") == "context_compaction"
                    ],
                    "started_at": pending_round.get("started_at"),
                }
            else:
                entries.append(record)
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def add_sub_agent_event(self, payload: dict[str, Any]) -> str:
        """追加 sub_agent 子任务事件到当前 pending `chat_round.events`。

        调用契约（docs/sub_agent_v1.md §6.3/§7.1）：子任务 Runner 不直接持有
        chat_memory，事件一律经父循环提供的 emit 回调进入本方法——保证写入
        发生在事件循环的同步 `_write_guard()` 块内（跨进程文件锁不可重入，
        禁止工作线程写历史）。timestamp 由本方法补充；追加后同步刷新 `.pending`
        侧车检查点快照（子任务事件随快照落盘，崩溃恢复不丢轨迹）。
        """
        if not isinstance(payload, dict) or payload.get("event") != "sub_agent":
            return "忽略非法 sub_agent 事件"
        record = dict(payload)
        record.setdefault("timestamp", now_str())
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if not self._round_store.record_sub_agent_event(record):
                return "忽略无进行中轮次或非法 sub_agent 事件"
            # 检查点快照（与 add_chat_history 相同模式）：仅覆盖侧车文件，
            # 不做全历史重写；meta 重算开销为零（子事件不带 role，不影响聚合）
            checkpoint = self._round_store.snapshot_pending_round()
            if checkpoint is not None:
                self._write_pending_checkpoint(checkpoint)
        return "记录成功"

    async def find_orphan_compaction_events(self) -> list[dict[str, Any]]:
        """返回未完成的压缩 start 事件（任务中断遗留：有 start、无对应 done/aborted）。

        压缩结果采用"完成后一次性写入"（context_summary / pending round 的
        compress 字段），中断时不会留下半成品数据；孤儿 start 仅代表展示层
        有未闭合的压缩过程条目，由调用方标记 aborted 并在下一次请求按需重新压缩。
        """
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
        open_starts: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("event") != "context_compaction":
                continue
            scope = str(entry.get("scope") or "unknown")
            phase = entry.get("phase")
            if phase == "start":
                open_starts[scope] = entry
            elif phase in ("done", "aborted"):
                open_starts.pop(scope, None)
        active_checkpoint = meta.get("_active_round_compaction")
        if isinstance(active_checkpoint, dict):
            for event in active_checkpoint.get("events", []):
                if not isinstance(event, dict):
                    continue
                phase = event.get("phase")
                if phase == "start":
                    open_starts["round"] = event
                elif phase in ("done", "aborted"):
                    open_starts.pop("round", None)
        return list(open_starts.values())

    async def mark_compaction_aborted(self, orphan: dict[str, Any]) -> str:
        """把一个未完成的压缩 start 事件标记为 aborted（前端据此失效对应条目）。"""
        scope = orphan.get("scope")
        if scope == "round":
            with self._write_guard():
                meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
                checkpoint = meta.get("_active_round_compaction")
                if not isinstance(checkpoint, dict):
                    return "忽略无活动压缩"
                events = checkpoint.get("events")
                if not isinstance(events, list):
                    return "忽略无活动压缩"
                events.append({
                    "event": "context_compaction",
                    "scope": "round",
                    "phase": "aborted",
                    "role": "assistant",
                    "reason": "task_interrupted",
                    "timestamp": now_str(),
                })
                meta["_active_round_compaction"] = {**checkpoint, "events": events}
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
            return "记录成功"
        return await self.add_context_compaction_event({
            "event": "context_compaction",
            "scope": scope,
            "phase": "aborted",
            "role": "assistant",
            "reason": "task_interrupted",
        })

    async def add_history_compression_usage(self, usage: dict[str, Any] | None) -> str:
        if not usage:
            return "忽略空压缩 usage"
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            history_usage = meta.get("_history_compress_usage")
            if not isinstance(history_usage, dict):
                history_usage = {}
                meta["_history_compress_usage"] = history_usage
            if not merge_compression_usage(history_usage, usage):
                return "忽略空压缩 usage"
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def get_meta(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_session_meta(self) -> dict[str, Any]:
        """读取会话元数据（热点只读路径：优先只读首行，不回写文件）。

        旧实现每次「读全量 → 重算 → 整文件重写回盘」，而 GET /chat_history/meta
        是首屏按会话并发调用的热点（每个会话一次），全量重写在会话多时既拖慢
        首屏又产生大量无谓磁盘写。派生字段（标题/提问列表/usage 等）已由写路径
        随重算结果落盘，正常文件单行读取的响应与全量重算一致；仅当首行不可靠
        （缺失/损坏/标题仍是默认值/缺 updated_at 或 user_questions）时回退全量
        重算兜底，两种路径都只读不写。
        """
        with self._lock:
            meta = _read_meta_first_line(self._file_path)
            if (
                meta is None
                or meta.get("title") == _default_title(self.session_id)
                or not meta.get("updated_at")
                or not meta.get("user_questions")
            ):
                meta, _entries = _load_meta_and_entries(self._file_path, self.session_id)
        return meta

    async def get_session_metadata(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_session_todo(self) -> list[dict[str, Any]]:
        """读取当前任务计划（_meta.todo）；无记录返回空列表。"""
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
        todo = meta.get("todo")
        return todo if isinstance(todo, list) else []

    async def update_session_todo(self, todos: list[dict[str, Any]]) -> str:
        """写入当前任务计划（_meta.todo），供前端展示与跨轮系统提示注入。"""
        if not isinstance(todos, list):
            raise ValueError("todos 必须是列表")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["todo"] = todos
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def update_session_title(self, title: str) -> dict[str, Any]:
        """
        更新当前会话首行 _meta 的 title 字段
        Args:
            title: 新的会话标题
        Returns:
            更新后的元数据字典
        """
        new_title = (title or "").strip()
        if not new_title:
            raise ValueError("title 不能为空")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["title"] = new_title
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def update_session_upload_id(self, upload_id: str) -> dict[str, Any]:
        """
        把本会话上传文件所在目录名写入首行 _meta 的 upload_id 字段。

        上传目录按上传时前端传入的 session_id 命名（history_files/upload/
        下的文件夹名），可能与会话文件名不一致；记录后可保证删除会话时
        能连带清理上传目录（见 delete_chat_session_file）。
        Args:
            upload_id: 上传目录名（history_files/upload/ 下的文件夹名）
        Returns:
            更新后的元数据字典
        """
        upload_id = (upload_id or "").strip()
        if not upload_id:
            raise ValueError("upload_id 不能为空")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["upload_id"] = upload_id
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_chat_history(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近工具调用历史列表
        Args:
            number: 获取最后几条数量，-1 表示所有
        Returns:
            历史记录列表
        """
        if not hasattr(number, "__int__"):
            # 抛出类型不匹配错误
            raise TypeError("number 参数必须为整数")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return entries[-number:] if number > 0 else entries

    async def get_context_summary(self) -> dict[str, Any] | None:
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return _normalize_context_summary(meta.get("context_summary"))

    async def update_context_summary(self, summary: dict[str, Any] | str | None) -> str:
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["context_summary"] = _normalize_context_summary(summary)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    def _load_for_read(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """只读路径的"读全量 + 按需物化"：meta 与首行一致时不再全量重写。

        get_context_messages / get_context_token_stats 等只读查询过去每次都
        无条件 `_write_meta_and_entries`（临时文件 + os.replace 全文件重写），
        大会话下轮询/加载会明显变慢；实际上写路径总是把重算后的 _meta 落在
        首行，仅当读取到的首行 _meta 与重算结果漂移时才需要物化（与
        __init__ 的惰性物化同口径）。
        """
        with self._write_guard():
            rows = _read_jsonlines(self._file_path)
            stored_meta = rows[0].get("_meta") if (rows and _is_meta_record(rows[0])) else None
            meta, entries = _load_meta_and_entries(
                self._file_path, self.session_id, rows=rows
            )
            if stored_meta != meta:
                _write_meta_and_entries(self._file_path, meta, entries)
        return meta, entries

    async def get_context_messages(
        self,
        max_rounds: int | None = None,
        max_tool_result_length: int | None = None,
        minimal: bool = False,
        recent_questions_token_budget: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取用于模型上下文的历史视图（按 HISTORY_COMPACT_KEEP_ROUNDS 总轮次窗口）。

        窗口语义（keep_rounds = 模型上下文保留的总轮次窗口）：
        - 未压缩轮次优先占窗口，以完整对话回传（含助手回答、工具调用等），
          其问题不重复进入保真索引；
        - 剩余窗口从最新往回分配给已压缩轮次的原始用户问题（保真索引，
          受 10k token 预算限制，超出从最旧丢弃）；
        - keep_rounds <= 0 表示无限窗口（仅按阈值压缩）。

        Args:
            max_rounds: 总轮次窗口；省略时跟随 HISTORY_COMPACT_KEEP_ROUNDS，
                <=0 表示无限窗口
            max_tool_result_length: 窗口内完整轮次的工具结果回传长度
            minimal: 兼容参数，不再切换已完成历史表示
            recent_questions_token_budget: 保真问题索引预算；省略时为 10k
        Returns:
            符合 ChatCompletion messages 结构的累计摘要/问题索引/完整轮次消息列表
        """
        if max_rounds is None:
            max_rounds = _default_context_history_rounds()
        if not hasattr(max_rounds, "__int__"):
            raise TypeError("max_rounds 参数必须为整数")
        max_rounds = int(max_rounds)
        if max_tool_result_length is None:
            max_tool_result_length = _parse_return_length(
                _load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
                DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
            )
        meta, entries = self._load_for_read()
        context_summary = _normalize_context_summary(meta.get("context_summary"))
        summary_message = _render_context_summary(context_summary)
        round_entries = [
            row for row in entries
            if isinstance(row, dict) and row.get("event") == "chat_round"
        ]
        summarized_count = 0
        if context_summary is not None:
            try:
                summarized_count = max(0, int(context_summary.get("source_round_count", 0) or 0))
            except (TypeError, ValueError):
                summarized_count = 0
            if summarized_count > len(round_entries):
                # 游标失效（历史被删等）：视为无摘要，下次压缩前从原始轮次重建
                context_summary = None
                summary_message = None
                summarized_count = 0
        if recent_questions_token_budget is None:
            question_budget = RECENT_QUESTIONS_TOKEN_BUDGET
        else:
            try:
                question_budget = max(256, int(recent_questions_token_budget))
            except (TypeError, ValueError):
                question_budget = 10_000

        messages: list[dict[str, Any]] = []
        if summary_message:
            messages.append({"role": "system", "content": summary_message})
        if context_summary is not None:
            # 摘要模式：未压缩轮次完整对话占窗口，已压缩轮次问题按剩余窗口保真
            # （未压缩轮次的问题不重复进入索引）
            question_items, raw_rounds = _split_context_window(
                round_entries, summarized_count, max_rounds, question_budget
            )
            recent_questions_message = _render_recent_questions_message(question_items)
            if recent_questions_message:
                messages.append(recent_questions_message)
        else:
            # 未触发过压缩：按窗口回传最近 N 轮完整对话（最旧的超出窗口舍弃），
            # 此时没有已压缩轮次，无需问题索引
            question_items = []
            raw_rounds = round_entries[-max_rounds:] if max_rounds > 0 else round_entries
        for round_entry in raw_rounds:
            messages.extend(
                _round_entry_to_context_messages(round_entry, max_tool_result_length)
            )
        return messages

    async def get_context_token_stats(
        self,
        max_rounds: int | None = None,
        max_tool_result_length: int | None = None,
        tools: list[Any] | None = None,
        system_prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """返回模型上下文构成的 token 统计，供调试/前端展示压缩效果。

        进入摘要模式后，统计的历史部分与 `get_context_messages` 一致：
        累计摘要 + 已压缩轮次保真问题索引 + 窗口内未压缩轮次完整对话；
        没有摘要的旧会话暂按原始轮次估算。

        `system_prompt_text` 为运行时系统提示附加文本（工作路径+系统提示），
        真实请求会追加到首条 system 消息；传入后单独统计并计入请求上下文总额。
        """
        if max_rounds is None:
            max_rounds = _default_context_history_rounds()
        if not hasattr(max_rounds, "__int__"):
            raise TypeError("max_rounds 参数必须为整数")
        max_rounds = int(max_rounds)
        if max_tool_result_length is None:
            max_tool_result_length = _parse_return_length(
                _load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
                DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
            )
        meta, entries = self._load_for_read()
        # 流式任务尚未收到 done 时，当前轮仍只存在于 ChatRoundStore.pending_round；
        # 复制快照供 token_stats 纳入统计，避免轮询只能看到上一轮的旧数据。
        pending_round = copy.deepcopy(self._round_store.pending_round)
        if pending_round is None:
            # 生成任务跑在会话 worker 进程时，pending_round 在 worker 内存，
            # 主进程实例里恒为 None；兜底读取 worker 逐事件写入的检查点
            # 侧车快照，让任务进行中（无 usage/压缩事件的静默间隔）统计
            # 也能实时反映当前轮增量。
            pending_round = self._read_pending_round_snapshot()
        context_summary = _normalize_context_summary(meta.get("context_summary"))
        summary_message = (
            _render_context_summary(context_summary)
            if context_summary is not None else None
        )
        round_entries = [
            row for row in entries
            if isinstance(row, dict) and row.get("event") == "chat_round"
        ]
        summarized_round_count = 0
        if context_summary is not None:
            try:
                stored_source_count = max(0, int(context_summary.get("source_round_count", 0) or 0))
            except (TypeError, ValueError):
                stored_source_count = 0
            if stored_source_count > len(round_entries):
                # token_stats 不能展示一个游标已失效的摘要；下次聊天前会重建它。
                context_summary = None
                summary_message = None
            else:
                summarized_round_count = stored_source_count
        # 有摘要后按总轮次窗口切分：未压缩轮次完整回传（占窗口），
        # 已压缩轮次问题按剩余窗口进入保真索引；没有摘要的旧会话
        # 暂时保留原始估算，供前端展示并触发首次压缩。
        summary_only = context_summary is not None
        if summary_only:
            question_items, raw_rounds = _split_context_window(
                round_entries, summarized_round_count, max_rounds
            )
            retained_rounds: list[dict[str, Any]] = list(raw_rounds)
        else:
            question_items = []
            retained_rounds = round_entries
            if max_rounds > 0:
                retained_rounds = retained_rounds[-max_rounds:]
        if (
            isinstance(pending_round, dict)
            and isinstance(pending_round.get("events"), list)
            and pending_round.get("events")
        ):
            retained_rounds.append(pending_round)

        messages: list[dict[str, Any]] = []
        if summary_message:
            messages.append({"role": "system", "content": summary_message})
        recent_questions_message = (
            _render_recent_questions_message(question_items)
            if summary_only else None
        )
        if recent_questions_message:
            messages.append(recent_questions_message)
        round_tokens: list[dict[str, Any]] = []
        for round_entry in retained_rounds:
            round_messages = _round_entry_to_context_messages(
                round_entry, max_tool_result_length
            )
            # 当前轮只有 user 事件时，通用历史格式化器会等待 assistant 内容；
            # 实时统计仍应计入这条已经送出的用户消息。
            if (
                not round_messages
                and round_entry is pending_round
                and isinstance(round_entry.get("question"), str)
                and round_entry.get("question", "").strip()
            ):
                round_messages = [{
                    "role": "user",
                    "content": round_entry["question"].strip(),
                }]
            compress_usage = _round_entry_compression_usage(round_entry)
            round_tokens.append({
                "question": round_entry.get("question", ""),
                "status": round_entry.get("status", ""),
                "messages": len(round_messages),
                "tokens": _estimate_messages_tokens(round_messages),
                "compress_count": (
                    compress_usage.get("compression_count", 0)
                    if isinstance(compress_usage, dict) else 0
                ),
            })
            messages.extend(round_messages)

        messages_tokens = _estimate_messages_tokens(messages)
        tool_definition_tokens = _estimate_tool_definition_tokens(tools)
        # 运行时系统提示词（工作路径+系统提示）：真实请求追加在首条 system 消息，
        # 单独统计并计入请求上下文总额，避免低估
        system_prompt_tokens = (
            _estimate_text_tokens(system_prompt_text)
            if isinstance(system_prompt_text, str) and system_prompt_text.strip()
            else 0
        )
        request_context_tokens = (
            messages_tokens + system_prompt_tokens + tool_definition_tokens
        )
        context_token_limit = _resolve_model_max_input_tokens(default=8192)

        compress_usage_total = meta.get("compress_usage")
        if not isinstance(compress_usage_total, dict):
            compress_usage_total = {}
        history_compress_usage = meta.get("_history_compress_usage")
        history_compress_count = 0
        if isinstance(history_compress_usage, dict):
            try:
                history_compress_count = max(
                    0, int(history_compress_usage.get("compression_count", 0) or 0)
                )
            except (TypeError, ValueError):
                history_compress_count = 0

        return {
            "context_token_limit": context_token_limit,
            "messages_tokens": messages_tokens,
            "system_prompt_tokens": system_prompt_tokens,
            "tool_definition_tokens": tool_definition_tokens,
            "request_context_tokens": request_context_tokens,
            "estimated_budget_ratio": round(
                request_context_tokens / context_token_limit, 4
            ) if context_token_limit > 0 else 0.0,
            "rounds": {
                "total": len(round_entries) + (1 if pending_round else 0),
                "summarized": summarized_round_count,
                "retained": len(retained_rounds),
                "max_rounds": max_rounds,
            },
            "history_context_mode": "summary_only" if summary_only else "legacy_raw",
            "raw_history_rounds_sent": (
                max(0, len(retained_rounds) - (1 if pending_round else 0))
            ),
            "has_context_summary": bool(summary_message),
            "summary_text_length": len(summary_message) if summary_message else 0,
            "recent_questions_length": (
                len(recent_questions_message.get("content", ""))
                if recent_questions_message else 0
            ),
            "recent_questions_count": len(question_items) if summary_only else 0,
            "context_compress_count": compress_usage_total.get("compression_count", 0),
            "history_compress_count": history_compress_count,
            "round_tokens": round_tokens,
        }

    async def clear_chat_history(self) -> str:
        """
        清空当前会话的所有工具调用历史
        Returns:
            操作结果字符串
        """
        with self._write_guard():
            self._round_store.reset()
            meta = _default_meta(self.session_id)
            # 清空历史只重置对话内容；工作目录/工具选择/模型选择是用户的持久配置，应予保留
            old_meta, _ = _load_meta_and_entries(self._file_path, self.session_id)
            old_work_dir = old_meta.get("work_dir")
            if isinstance(old_work_dir, str) and old_work_dir.strip():
                meta["work_dir"] = old_work_dir
            old_tool_selection = normalize_tool_inputs(old_meta.get("tool_selection"))
            if old_tool_selection:
                meta["tool_selection"] = old_tool_selection
            old_model_selection = _normalize_model_selection(old_meta.get("model_selection"))
            old_model_selection = {
                role: entry for role, entry in old_model_selection.items()
                if entry.get("ownership_name") and entry.get("model_name")
            }
            if old_model_selection:
                meta["model_selection"] = old_model_selection
            _write_meta_and_entries(self._file_path, meta, [])
        return "清空成功"

    async def get_session_work_dir(self) -> str | None:
        """读取会话独立工作目录（_meta.work_dir）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "work_dir")
        if isinstance(value, str) and value.strip():
            return value
        return None

    async def update_session_work_dir(self, work_dir: str | None) -> dict[str, Any]:
        """写入/清除会话独立工作目录（_meta.work_dir）。

        Args:
            work_dir: 目录路径；空串/None 表示清除覆盖，恢复跟随全局默认。
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: 目录不存在或不可访问
        """
        raw = (work_dir or "").strip()
        if raw:
            resolved = resolve_work_dir(raw)
            if resolved is None:
                raise ValueError(f"工作目录不存在或不可访问: {raw}")
            new_value: str | None = str(resolved)
        else:
            new_value = None
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if new_value is None:
                meta.pop("work_dir", None)
            else:
                meta["work_dir"] = new_value
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_session_tool_selection(self) -> dict[str, list[str]] | None:
        """读取会话独立工具选择（_meta.tool_selection）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "tool_selection")
        if value is None:
            return None
        return normalize_tool_inputs(value) or None

    async def update_session_tool_selection(
        self, tool_selection: dict[str, list[Any]] | None
    ) -> dict[str, Any]:
        """写入/清除会话独立工具选择（_meta.tool_selection）。

        Args:
            tool_selection: {服务名: [工具名]}；None/空 dict 表示清除覆盖，
                恢复跟随全局默认（mcp_servers.json 的 inputs 键）。
                非法条目（非字符串/空串/重复工具名）由服务端规整时丢弃。
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: tool_selection 不是字典
        """
        if tool_selection is not None and not isinstance(tool_selection, dict):
            raise ValueError("tool_selection 必须是 {服务名: [工具名]} 形式的字典")
        normalized = normalize_tool_inputs(tool_selection)
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if normalized:
                meta["tool_selection"] = normalized
            else:
                meta.pop("tool_selection", None)
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_session_model_selection(self) -> dict[str, dict[str, Any]] | None:
        """读取会话独立模型选择（_meta.model_selection）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "model_selection")
        if value is None:
            return None
        valid = {
            role: entry for role, entry in _normalize_model_selection(value).items()
            if entry.get("ownership_name") and entry.get("model_name")
        }
        return valid or None

    async def update_session_model_selection(
        self, role: str, entry: dict[str, Any] | None
    ) -> dict[str, Any]:
        """写入/清除会话独立模型选择的单个角色（_meta.model_selection）。

        Args:
            role: 模型角色（chat_model / compaction_model / title_model，随版本扩展）
            entry: {ownership_name, model_name, parameter, api_type} 覆盖条目；
                None 表示清除该角色覆盖（恢复跟随全局默认）
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: role 未知或 entry 结构非法
        """
        if role not in _MODEL_SELECTION_ROLES:
            raise ValueError(f"未知模型角色: {role!r}；可选: {'/'.join(_MODEL_SELECTION_ROLES)}")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            stored = dict(meta.get("model_selection")) if isinstance(meta.get("model_selection"), dict) else {}
            if entry is None:
                stored.pop(role, None)
            else:
                if not isinstance(entry, dict):
                    raise ValueError("model_selection 覆盖条目必须是字典")
                normalized = _normalize_model_selection({role: entry}).get(role) or {}
                if not (normalized.get("ownership_name") and normalized.get("model_name")):
                    raise ValueError("覆盖条目必须包含 ownership_name 与 model_name")
                stored[role] = normalized
            if stored:
                meta["model_selection"] = stored
            else:
                meta.pop("model_selection", None)
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def ensure_session_config_snapshot(self) -> tuple[bool, dict[str, Any]]:
        """会话配置快照：首次正式开始任务时把当前全局默认配置固化为会话独立配置。

        幂等：`_meta.config_snapshot_at` 已存在时直接返回（快照一次后不再重写，
        用户后续的手动会话级修改不受影响，清除单键的「恢复跟随全局」语义也不变）。

        固化内容（仅在会话尚无对应覆盖时写入，已有覆盖键保持不动）：
          - work_dir        ← 全局默认工作目录（.env DEFAULT_CHAT_WORK_DIR，目录有效才写入）
          - tool_selection  ← mcp_servers.json 的 inputs（规整后，非空才写入）
          - model_selection ← models.json 顶层 model_selection（全角色有效条目）
        单项读取失败只跳过该项，不阻断快照与聊天。

        Returns:
            (本次是否真正写入快照, 快照后的元数据字典)
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if meta.get("config_snapshot_at"):
                return False, meta
            # 全局默认工作目录
            try:
                persisted = get_persisted_work_dir()
                existing_work_dir = meta.get("work_dir")
                if (
                    isinstance(persisted, str) and persisted.strip()
                    and not (isinstance(existing_work_dir, str) and existing_work_dir.strip())
                ):
                    resolved = resolve_work_dir(persisted)
                    if resolved is not None:
                        meta["work_dir"] = str(resolved)
            except Exception as work_error:
                print(f"[WARN] 会话配置快照（work_dir）失败，已跳过该项: {work_error}")
            # 全局默认工具选择
            try:
                inputs, _servers, tool_error = get_global_tool_inputs()
                if tool_error is None:
                    normalized_inputs = normalize_tool_inputs(inputs)
                    if normalized_inputs and meta.get("tool_selection") is None:
                        meta["tool_selection"] = normalized_inputs
            except Exception as tool_error:
                print(f"[WARN] 会话配置快照（tool_selection）失败，已跳过该项: {tool_error}")
            # 全局默认模型选择（全部角色）
            try:
                global_selection = _get_global_model_selection()
                valid_selection = {
                    role: entry for role, entry in (global_selection or {}).items()
                    if isinstance(entry, dict) and entry.get("ownership_name") and entry.get("model_name")
                }
                if valid_selection and not isinstance(meta.get("model_selection"), dict):
                    meta["model_selection"] = valid_selection
            except Exception as model_error:
                print(f"[WARN] 会话配置快照（model_selection）失败，已跳过该项: {model_error}")
            meta["config_snapshot_at"] = now_str()
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return True, meta

    @staticmethod
    def list_chat_sessions():
        """
        列出 HISTORY_ROOT 目录下的所有 jsonl 文件
        :return: 一个包含所有 jsonl 文件路径的列表
        """
        jsonl_files = list(HISTORY_ROOT.glob('*.jsonl'))
        return jsonl_files

    @staticmethod
    def get_chat_session_file(session_id: str):
        """
        返回指定 session_id 的 jsonl 文件响应
        :param session_id: 会话ID
        :return: 文件响应
        """
        chat_history_file = _get_chat_history_file(session_id)
        if chat_history_file.exists():
            return FileResponse(chat_history_file, filename=chat_history_file.name)
        else:
            raise HTTPException(status_code=404, detail="Chat session file not found")

    @staticmethod
    def get_chat_session_meta(session_id: str) -> dict[str, Any]:
        """
        获取指定 session_id 的聊天历史元数据（首行 _meta）
        :param session_id: 会话ID
        :return: 元数据字典
        """
        chat_history_file = _get_chat_history_file(session_id)
        if not chat_history_file.exists():
            raise HTTPException(status_code=404, detail="Chat session file not found")
        manager = ChatMemoryManager(session_id)
        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            _write_meta_and_entries(manager._file_path, meta, entries)
        return meta

    @staticmethod
    def delete_chat_session_file(session_id: str) -> dict:
        """
        删除指定 session_id 的 jsonl 文件，并连同该会话上传文件所在目录一并删除。

        - 上传目录名优先取 jsonl 首行 _meta 记录的 upload_id；
          旧记录无该字段时回退为按 session_id 推导的目录名；
        - jsonl 与上传目录互不依赖：任一侧存在都会被删除
          （可顺带清理有上传目录但无聊天文件的孤儿会话）；
        - 任一删除步骤失败（如 Windows 上文件被占用）都会抛出 OSError，
          由路由层转换为 HTTP 异常返回给前端。

        :param session_id: 会话 ID
        :return: 操作结果字典
        :raises OSError: 删除失败时抛出
        """
        chat_history_file = _get_chat_history_file(session_id)
        recorded_upload_id: Any = None
        if chat_history_file.exists():
            # 先读 _meta 里记录的 upload_id（jsonl 即将删除，需先取出）
            try:
                meta, _ = _load_meta_and_entries(chat_history_file, session_id)
                recorded_upload_id = meta.get("upload_id")
            except OSError:
                recorded_upload_id = None
        deleted_parts: list[str] = []
        # 1) 删除 jsonl 历史文件与其 pending 检查点侧车
        if chat_history_file.exists():
            _delete_path_with_retry(chat_history_file)
            deleted_parts.append(f"会话文件 {chat_history_file.name}")
        pending_sidecar = chat_history_file.with_name(chat_history_file.name + ".pending")
        if pending_sidecar.exists():
            _delete_path_with_retry(pending_sidecar)
            deleted_parts.append(f"检查点 {pending_sidecar.name}")
        # 2) 删除上传目录：优先用记录的 upload_id（需重新清洗，避免畸形值逃逸），
        #    否则回退为按 session_id 推导的目录名
        upload_dir_name = ""
        if recorded_upload_id:
            upload_dir_name = _safe_session_id(str(recorded_upload_id))
        if not upload_dir_name:
            upload_dir_name = _safe_session_id(session_id)
        if upload_dir_name:
            upload_dir = UPLOAD_HISTORY_ROOT / upload_dir_name
            if upload_dir.exists():
                _delete_path_with_retry(upload_dir, recursive=True)
                deleted_parts.append(f"上传目录 upload/{upload_dir_name}")
        if not deleted_parts:
            return {
                "state": "succeed",
                "describe": f"会话文件 {session_id}_chat.jsonl 及其上传目录均不存在",
            }
        return {
            "state": "succeed",
            "describe": "已删除：" + "、".join(deleted_parts),
        }

    @staticmethod
    def delete_chat_session_file_line(session_id: str, startline: int, endline: int) -> dict[str, Any]:
        """
        删除指定 session_id 的 jsonl 文件指定行范围
        :param session_id: 会话 ID
        :param startline: 要删除的起始行号(从1开始)
        :param endline: 要删除的结束行号(包含,从1开始)
        :return: 操作结果字典
        """
        # 验证参数
        if startline is None or endline is None:
            raise ValueError("startline 和 endline 参数不能为空")
        try:
            startline = int(startline)
            endline = int(endline)
        except (TypeError, ValueError):
            raise TypeError("startline 和 endline 必须是整数")
        if startline < 1 or endline < 1:
            raise ValueError("行号必须是大于等于 1 的整数")
        if startline > endline:
            raise ValueError(f"起始行号({startline})不能大于结束行号({endline})")
        manager = ChatMemoryManager(session_id)
        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            total_lines = len(entries)
            if total_lines == 0:
                return {
                    "state": "failed",
                    "describe": "当前会话历史文件为空，无可删除行"
                }
            # 这里的行号以“业务记录行”计数（不包含第一行 _meta）
            valid_start = max(1, startline)
            valid_end = min(endline, total_lines)
            if valid_start > valid_end:
                return {
                    "state": "failed",
                    "describe": f"指定的行号范围超出文件范围,当前总行数为 {total_lines}"
                }
            remaining_entries = [
                entry for index, entry in enumerate(entries, start=1)
                if index < valid_start or index > valid_end
            ]
            deleted_count = valid_end - valid_start + 1
            # 删除会改变轮次顺序/数量，旧摘要游标不再可靠；下次聊天前由统一
            # 跨轮压缩流程从剩余原始轮次重建累计摘要和问题索引。
            meta["context_summary"] = None
            meta = _recompute_meta_from_entries(manager.session_id, meta, remaining_entries)
            _write_meta_and_entries(manager._file_path, meta, remaining_entries)
        return {
            "state": "succeed",
            "describe": f"已删除第 {valid_start} 到 {valid_end} 行,共 {deleted_count} 行。原始记录数 {total_lines},剩余记录数 {len(remaining_entries)}",
            "meta_after": meta,
            "usage_after": meta.get("usage", {})
        }

    @staticmethod
    def replace_content_in_chat_session(
        session_id: str,
        find_text: str,
        replace_text: str,
    ) -> dict[str, Any]:
        """在会话 JSONL 全部业务记录中把 find_text 替换为 replace_text。

        供前端媒体伪标签删除使用：用户删除消息中渲染的媒体控件后，把历史
        记录里对应的标签原文替换为占位说明（如"用户已删除/文件不存在"）。
        递归遍历每条记录的所有字符串字段（events[].content 等）；未命中时
        不重写文件；轮次数量与顺序不变，摘要游标保持有效，无需重置。
        """
        if not isinstance(find_text, str) or not find_text.strip():
            raise ValueError("find_text 不能为空")
        if not isinstance(replace_text, str):
            replace_text = ""
        manager = ChatMemoryManager(session_id)
        replaced_total = 0
        touched_entries = 0

        def _walk(value: Any) -> Any:
            nonlocal replaced_total
            if isinstance(value, str):
                count = value.count(find_text)
                if count:
                    replaced_total += count
                    return value.replace(find_text, replace_text)
                return value
            if isinstance(value, list):
                return [_walk(item) for item in value]
            if isinstance(value, dict):
                return {key: _walk(item) for key, item in value.items()}
            return value

        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            new_entries: list[Any] = []
            for entry in entries:
                new_entry = _walk(entry)
                if new_entry is not entry:
                    touched_entries += 1
                new_entries.append(new_entry)
            if replaced_total <= 0:
                return {
                    "state": "failed",
                    "describe": "未在会话历史中找到对应内容",
                    "replaced": 0,
                }
            meta = _recompute_meta_from_entries(manager.session_id, meta, new_entries)
            _write_meta_and_entries(manager._file_path, meta, new_entries)
        return {
            "state": "succeed",
            "describe": f"已替换 {replaced_total} 处（涉及 {touched_entries} 条记录）",
            "replaced": replaced_total,
        }

    @staticmethod
    def import_jsonl_chat_history(
        session_id: str,
        raw_bytes: bytes,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """导入上传的 jsonl 聊天历史文件并写入 history_files 目录。

        流程：
        - 解析上传字节流；首行若是 `{"_meta": ...}` 则作为基础 meta（`session_id` 字段会被移除）；
        - 逐行通过 `parse_round_entry` 验证；不合法行直接丢弃；
        - 目标文件（`<session_id>_chat.jsonl`）已存在且 `overwrite=False`（默认）时，
          自动追加时间戳另存为 `<session_id>_<时间戳>_chat.jsonl`，避免覆盖旧会话；
          `overwrite=True` 时强制覆盖目标文件；
        - 写入后通过 `_recompute_meta_from_entries` 重新计算 user_questions / usage / record_count / completion_count。

        Args:
            session_id: 目标会话 ID（会先经 normalize_session_id 规整）。
            raw_bytes: 上传的 jsonl 字节流。
            overwrite: 是否强制覆盖同名历史文件（默认 False，重名自动另存）。

        Returns:
            包含 state / session_id（实际写入）/ filename / total_lines / imported_rounds /
            skipped_lines / collision / meta 等字段的字典。
        """
        return _import_jsonl_payload(session_id, raw_bytes, overwrite=overwrite)


def _resolve_import_target(
    session_id: str,
    overwrite: bool,
) -> tuple[str, Path, bool]:
    """解析导入目标文件，处理同名冲突。

    Returns:
        (实际 session_id, 目标文件路径, 是否发生冲突另存)
    - overwrite=True：强制写入 `<session_id>_chat.jsonl`（覆盖），collision=False
    - overwrite=False：目标不存在则直接使用；已存在则追加时间戳
      另存为 `<session_id>_<时间戳>_chat.jsonl`，collision=True
    """
    base_id = normalize_session_id(session_id)
    target_file = _get_chat_history_file(base_id)
    if overwrite:
        return base_id, target_file, False
    if not target_file.exists():
        return base_id, target_file, False
    # 同名冲突：追加时间戳（同一秒再次冲突时递增序号）
    counter = 0
    while True:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        if counter:
            suffix = f"{suffix}_{counter}"
        candidate_id = f"{base_id}_{suffix}"
        candidate_file = _get_chat_history_file(candidate_id)
        if not candidate_file.exists():
            return candidate_id, candidate_file, True
        counter += 1


def _import_jsonl_payload(
    session_id: str,
    raw_bytes: bytes,
    overwrite: bool = False,
) -> dict[str, Any]:
    """将上传的 jsonl 字节流解析并写入历史文件。

    解析规则：
    - 跳过空行
    - 第 1 行若为 `{"_meta": {...}}` 结构，作为基础元数据（其中 session_id 字段会被移除）；
      否则忽略元数据并以默认 meta 起算
    - 其余行尝试用 `parse_round_entry` 还原为标准 `chat_round` 条目；
      无法解析的行直接丢弃并计入 skipped_lines
    - 目标文件同名且 overwrite=False（默认）时自动追加时间戳另存，避免覆盖旧会话
    - 全部解析完成后通过 `_recompute_meta_from_entries` 重新聚合 user_questions / usage / record_count / completion_count

    Returns:
        包含 session_id（实际写入）/ filename / total_lines / imported_rounds /
        skipped_lines / collision / meta 的字典
    """
    if raw_bytes is None:
        raise ValueError("raw_bytes 不能为空")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # 兼容 GBK/UTF-8-sig 等常见中文编码
        text = raw_bytes.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    total_lines = len(lines)
    base_meta: dict[str, Any] | None = None
    candidate_entries: list[dict[str, Any]] = []
    if lines and _is_meta_record(_safe_parse_jsonl_line(lines[0])):
        try:
            first_row = json.loads(lines[0])
            base_meta = first_row.get("_meta") if isinstance(first_row, dict) else None
        except json.JSONDecodeError:
            base_meta = None
        lines = lines[1:]
    skipped_lines = 0
    for line in lines:
        parsed = _safe_parse_jsonl_line(line)
        if parsed is None:
            skipped_lines += 1
            continue
        round_entry = parse_round_entry(parsed)
        if round_entry is None:
            skipped_lines += 1
            continue
        candidate_entries.append(round_entry)
    actual_session_id, file_path, collision = _resolve_import_target(session_id, overwrite)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    manager = ChatMemoryManager(actual_session_id)
    with manager._write_guard():
        final_entries = list(candidate_entries)
        merged_meta = _recompute_meta_from_entries(
            actual_session_id,
            base_meta,
            final_entries,
        )
        if isinstance(base_meta, dict) and isinstance(base_meta.get("title"), str) and base_meta["title"].strip():
            merged_meta["title"] = base_meta["title"].strip()
        imported_summary = (
            _normalize_context_summary(base_meta.get("context_summary"))
            if isinstance(base_meta, dict) else None
        )
        imported_source_count = 0
        if imported_summary is not None:
            try:
                imported_source_count = int(imported_summary.get("source_round_count", 0) or 0)
            except (TypeError, ValueError):
                imported_source_count = 0
        if imported_summary is not None and 0 <= imported_source_count <= len(candidate_entries):
            # 保真索引只覆盖已压缩轮次（游标之前），未压缩轮次以完整对话回传
            imported_question_items, _raw = _split_context_window(
                candidate_entries, imported_source_count, _default_context_history_rounds()
            )
            imported_summary["recent_questions"] = [
                question for _number, question in imported_question_items
            ]
            imported_summary["recent_question_numbers"] = [
                number for number, _question in imported_question_items
            ]
            imported_summary["recent_questions_scope"] = "all_history"
            merged_meta["context_summary"] = imported_summary
        else:
            # 不能证明摘要游标与导入轮次对应时，宁可下次重建，也不要静默跳过历史。
            merged_meta["context_summary"] = None
        _write_meta_and_entries(file_path, merged_meta, final_entries)
    return {
        "state": "succeed",
        "session_id": actual_session_id,
        "filename": file_path.name,
        "total_lines": total_lines,
        "imported_rounds": len(candidate_entries),
        "skipped_lines": skipped_lines,
        "record_count": merged_meta.get("record_count", len(final_entries)),
        "completion_count": merged_meta.get("completion_count", 0),
        "title": merged_meta.get("title"),
        "overwrite": bool(overwrite),
        "collision": bool(collision),
        "meta": merged_meta,
    }


def _safe_parse_jsonl_line(line: str) -> Any:
    """把单行 jsonl 安全解析为对象；解析失败返回 None。"""
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


# 会话管理器注册表（用于缓存不同 session_id 的管理器实例）
_session_managers: dict[str, ChatMemoryManager] = {}
_session_lock = threading.Lock()


async def get_chat_memory_manager(session_id: str) -> ChatMemoryManager:
    """
    根据 session_id 获取或创建工具记忆管理器实例
    Args:
        session_id: 会话ID
    Returns:
        ChatMemoryManager实例
    """
    with _session_lock:
        if session_id not in _session_managers:
            _session_managers[session_id] = ChatMemoryManager(session_id)
        return _session_managers[session_id]


def chat_memory_file_exists(session_id: str) -> bool:
    """判断会话历史文件是否已存在（只读检查，绝不创建文件/目录）。

    供查询类接口（如 /chat_context/token_stats）在实例化管理器前使用：
    ChatMemoryManager.__init__ 会 touch 文件并写入 _meta，若查询接口直接
    get_chat_memory_manager，会让"仅查询"的会话凭空产生空历史文件。
    """
    try:
        return _get_chat_history_file(normalize_session_id(session_id)).exists()
    except Exception:
        return False


async def cleanup_chat_memory_manager(session_id: str) -> None:
    """
    清理指定会话的记忆管理器（释放实例引用）
    Args:
        session_id: 会话ID
    """
    with _session_lock:
        if session_id in _session_managers:
            del _session_managers[session_id]


if __name__ == "__main__":
    print("=" * 60)
    print("ChatMemoryManager 并发安全测试")
    print("=" * 60)

    # 测试1：基本功能
    print("\n【测试1】基本功能测试")

    # 为不同session创建独立的管理器
    manager_1 = get_chat_memory_manager("session_1")
    manager_2 = get_chat_memory_manager("session_2")

    # 测试会话1
    manager_1.add_chat_history("工具调用1-1")
    manager_1.add_chat_history("工具调用1-2")
    manager_1.add_chat_history("工具调用1-3")
    print(f"Session 1 历史: {manager_1.get_chat_history()}")

    # 测试会话2
    manager_2.add_chat_history("工具调用2-1")
    manager_2.add_chat_history("工具调用2-2")
    print(f"Session 2 历史: {manager_2.get_chat_history()}")

    # 验证隔离
    print(f"\n验证隔离 - Session 1: {manager_1.get_chat_history()}")
    print(f"验证隔离 - Session 2: {manager_2.get_chat_history()}")

    # 测试2：数量限制
    print("\n【测试2】数量限制测试（最多10条）")
    manager_limit = get_chat_memory_manager("session_limit")
    for i in range(12):
        manager_limit.add_chat_history(f"调用{i+1}")
    history = manager_limit.get_chat_history(10)
    print(f"添加12条后保留: {len(history)} 条")
    print(f"历史记录: {history}")

    # 测试3：文本摘要
    print("\n【测试3】文本摘要测试")
    manager_text = get_chat_memory_manager("session_text")
    manager_text.add_chat_history("第一条很长的工具调用记录" * 10)
    manager_text.add_chat_history("第二条记录")
    manager_text.add_chat_history("第三条记录")
    # text_summary = manager_text.get_chat_history_text(max_total_chars=50)
    # print(f"文本摘要（限制50字符）:\n{text_summary}")

    # 测试4：清理功能
    print("\n【测试4】清理功能测试")
    print(f"清理前 Session 1: {len(manager_1.get_chat_history())} 条")
    manager_1.clear_chat_history()
    print(f"清理后 Session 1: {len(manager_1.get_chat_history())} 条")
    print(f"Session 2 未受影响: {len(manager_2.get_chat_history())} 条")

    # 测试5：并发安全测试
    print("\n【测试5】并发安全测试")
    import time

    errors = []

    def worker(session_id, num_calls):
        try:
            manager = get_chat_memory_manager(session_id)
            for i in range(num_calls):
                manager.add_chat_history(f"Thread-{session_id}-Call-{i}")
                time.sleep(0.001)  # 模拟一些工作
        except Exception as e:
            errors.append(str(e))

    # 创建多个线程同时操作不同的session
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(f"concurrent_session_{i}", 20))
        threads.append(t)
        t.start()

    # 等待所有线程完成
    for t in threads:
        t.join()

    print(f"并发测试完成，错误数: {len(errors)}")
    if errors:
        print(f"错误详情: {errors[:3]}")

    # 验证每个会话的数据完整性
    for i in range(5):
        session_id = f"concurrent_session_{i}"
        manager = get_chat_memory_manager(session_id)
        history = manager.get_chat_history(10)
        print(f"  {session_id}: {len(history)} 条记录")

    # 清理测试数据
    for i in range(5):
        cleanup_chat_memory_manager(f"concurrent_session_{i}")
    cleanup_chat_memory_manager("session_1")
    cleanup_chat_memory_manager("session_2")
    cleanup_chat_memory_manager("session_limit")
    cleanup_chat_memory_manager("session_text")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)
