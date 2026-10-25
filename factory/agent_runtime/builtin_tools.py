"""由服务端本地执行的内置工具。"""
# from __future__ import annotations
from urllib.parse import urlsplit
import base64
import codecs
import copy
import difflib
import fnmatch
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from collections import OrderedDict
from functools import wraps
from pathlib import Path
from factory.agent_runtime.permission_runtime import guard_path, permission_urlopen
from threading import Lock
from typing import Any, Optional


from util.text_encoding import decode_text, unicode_bom, powershell_text_prelude
from util.tool_arguments import string_arg, int_arg, number_arg
from util.file_lock import cross_process_lock
from factory.agent_runtime.tool_control import (
    ToolCancelledError, check_tool_control, remaining_seconds, tool_deadline,
)
from memory import file_memory
from . import file_diff_result as _file_diff_result
from . import tool_inputs as _tool_inputs
from .builtin_tool_catalog import (
    TODO_TOOL_NAME,
    ASK_USER_TOOL_NAME,
    WRITE_FILE_NAME,
    EDIT_FILE_NAME,
    APPLY_PATCH_NAME,
    READ_FILE_NAME,
    SEARCH_FILES_NAME,
    LIST_FILES_NAME,
    READ_MEDIA_NAME,
    READ_DOCUMENT_NAME,
    SUB_AGENT_TOOL_NAME,
    RUN_COMMAND_NAME,
    POLL_COMMAND_NAME,
    STOP_COMMAND_NAME,
    WRITE_STDIN_NAME,
    COMMAND_TOOL_NAMES,
    BUILTIN_TOOL_SERVER_KEY,
    SELECTABLE_BUILTIN_TOOL_NAMES,
    TODO_TOOL_DEFINITION,
    ASK_USER_TOOL_DEFINITION,
    SUB_AGENT_TOOL_DEFINITION,
    WRITE_FILE_TOOL_DEFINITION,
    EDIT_FILE_TOOL_DEFINITION,
    APPLY_PATCH_TOOL_DEFINITION,
    READ_FILE_TOOL_DEFINITION,
    SEARCH_FILES_TOOL_DEFINITION,
    LIST_FILES_TOOL_DEFINITION,
    READ_DOCUMENT_TOOL_DEFINITION,
    RUN_COMMAND_TOOL_DEFINITION,
    POLL_COMMAND_TOOL_DEFINITION,
    STOP_COMMAND_TOOL_DEFINITION,
    WRITE_STDIN_TOOL_DEFINITION,
)


# 工具名称和 Schema 从 catalog 显式重导出，保留原有导入路径。
# 前端回答 ask_user 提问时的消息前缀（前端同名拼接，需保持一致）；
# 后端据此识别"覆盖式重新回答"：截断最新提问轮之后的旧回答轮
ASK_ANSWER_PREFIX = "【回答模型提问】"
def inject_builtin_tools(
    selected_tools: list[dict[str, Any]],
    selected_tool_servers: dict[str, str],
    include_todo: bool = False,
    include_ask_user: bool = False,
    include_write_file: bool = False,
    include_edit_file: bool = False,
    include_read_file: bool = False,
    include_search_files: bool = False,
    include_run_command: bool = False,
    include_read_media: bool = False,
    include_read_document: bool = False,
    include_sub_agent: bool = False,
    default_command_shell: str = "auto",
    include_apply_patch: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """注入用户选择的本地内置工具：todo_write、ask_user、
    write_file、edit_file、read_file、search_files、run_command、
    read_media、read_document 与 sub_agent（用户在工具选择中勾选时）。"""
    tools = list(selected_tools)
    servers = dict(selected_tool_servers)
    # todo_write 独立于外部工具：用户显式勾选即注入（即使没有选择任何 MCP 工具）
    if include_todo and TODO_TOOL_NAME not in servers:
        tools.append(TODO_TOOL_DEFINITION)
        servers[TODO_TOOL_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_ask_user and ASK_USER_TOOL_NAME not in servers:
        tools.append(ASK_USER_TOOL_DEFINITION)
        servers[ASK_USER_TOOL_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_write_file and WRITE_FILE_NAME not in servers:
        tools.append(WRITE_FILE_TOOL_DEFINITION)
        servers[WRITE_FILE_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_edit_file and EDIT_FILE_NAME not in servers:
        tools.append(EDIT_FILE_TOOL_DEFINITION)
        servers[EDIT_FILE_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_apply_patch and APPLY_PATCH_NAME not in servers:
        tools.append(APPLY_PATCH_TOOL_DEFINITION)
        servers[APPLY_PATCH_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_read_file and READ_FILE_NAME not in servers:
        tools.append(READ_FILE_TOOL_DEFINITION)
        servers[READ_FILE_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_search_files and SEARCH_FILES_NAME not in servers:
        tools.append(SEARCH_FILES_TOOL_DEFINITION)
        servers[SEARCH_FILES_NAME] = BUILTIN_TOOL_SERVER_KEY
        if LIST_FILES_NAME not in servers:
            tools.append(LIST_FILES_TOOL_DEFINITION)
            servers[LIST_FILES_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_run_command and RUN_COMMAND_NAME not in servers:
        command_definition = copy.deepcopy(RUN_COMMAND_TOOL_DEFINITION)
        command_definition["function"]["description"] += f" 本会话默认 shell={default_command_shell}。"
        command_definition["function"]["parameters"]["properties"]["shell"]["default"] = default_command_shell
        tools.append(command_definition)
        servers[RUN_COMMAND_NAME] = BUILTIN_TOOL_SERVER_KEY
        for definition, name in ((POLL_COMMAND_TOOL_DEFINITION, POLL_COMMAND_NAME),
                                 (STOP_COMMAND_TOOL_DEFINITION, STOP_COMMAND_NAME),
                                 (WRITE_STDIN_TOOL_DEFINITION, WRITE_STDIN_NAME)):
            if name not in servers:
                tools.append(definition)
                servers[name] = BUILTIN_TOOL_SERVER_KEY
    if include_read_media and READ_MEDIA_NAME not in servers:
        tools.append(build_read_media_tool_definition())
        servers[READ_MEDIA_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_read_document and READ_DOCUMENT_NAME not in servers:
        tools.append(READ_DOCUMENT_TOOL_DEFINITION)
        servers[READ_DOCUMENT_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_sub_agent and SUB_AGENT_TOOL_NAME not in servers:
        tools.append(SUB_AGENT_TOOL_DEFINITION)
        servers[SUB_AGENT_TOOL_NAME] = BUILTIN_TOOL_SERVER_KEY
    return tools, servers


def is_builtin_tool(tool_name: str) -> bool:
    return tool_name in (
        TODO_TOOL_NAME, ASK_USER_TOOL_NAME,
        WRITE_FILE_NAME, EDIT_FILE_NAME, APPLY_PATCH_NAME, READ_FILE_NAME, SEARCH_FILES_NAME, LIST_FILES_NAME,
        READ_MEDIA_NAME, READ_DOCUMENT_NAME, SUB_AGENT_TOOL_NAME,
        RUN_COMMAND_NAME, POLL_COMMAND_NAME, STOP_COMMAND_NAME, WRITE_STDIN_NAME,
    )


def model_tool_output_char_budget(model_config=None) -> int:
    """Reserve most of a small model window for the rest of the request."""
    from factory.agent_runtime.chat_runtime import resolve_model_max_input_tokens, resolve_config_max_input_tokens
    window = (resolve_config_max_input_tokens(model_config) if model_config is not None
              else resolve_model_max_input_tokens(default=8192))
    return max(512, min(12000, window // 2))


def document_output_token_budget(*, model_config=None, messages=None, tools=None, tool_count=1) -> int:
    """Budget the complete parsed result, sharing remaining input space across a tool batch.

    Use the effective model (including a child agent override), not its global default.
    Leave 25% safety margin for tokenizer differences and 512 tokens for request framing.
    The normal pre-send compaction check still handles other tools and native inputs.
    """
    from factory.agent_runtime.chat_runtime import (
        estimate_send_context_tokens, resolve_config_max_input_tokens, resolve_model_max_input_tokens,
    )
    window = (resolve_config_max_input_tokens(model_config) if model_config is not None
              else resolve_model_max_input_tokens(default=8192))
    remaining = window - 512
    if messages is not None:
        remaining -= estimate_send_context_tokens(messages, tools)
    shared = max(0, remaining) // max(1, tool_count)
    return max(0, int(min(window // 4, shared) * 0.75))


def resolve_default_command_shell(session_id: str | None = None) -> str:
    """统一使用平台默认，旧会话/环境中的默认 shell 覆盖不再生效。"""
    return _resolve_shell("auto", os.name == "nt")[1]



_TODO_VALID_STATUSES = _tool_inputs.TODO_VALID_STATUSES
_TODO_MAX_ITEMS = _tool_inputs.TODO_MAX_ITEMS
_TODO_MAX_CONTENT_CHARS = _tool_inputs.TODO_MAX_CONTENT_CHARS


def _next_todo_id(existing: set[str]) -> str:
    return _tool_inputs.next_todo_id(existing)


def normalize_todo_items(raw: Any, prev_items: list[dict[str, Any]] | None=None) -> tuple[list[dict[str, str]] | None, list[str] | None, str | None]:
    return _tool_inputs.normalize_todo_items(raw, prev_items, max_items=_TODO_MAX_ITEMS,
        max_content_chars=_TODO_MAX_CONTENT_CHARS, valid_statuses=_TODO_VALID_STATUSES, id_factory=_next_todo_id)



# --------------------------- ask_user：向用户提问 ---------------------------

_ASK_MAX_QUESTIONS = _tool_inputs.ASK_MAX_QUESTIONS
_ASK_MAX_QUESTION_CHARS = _tool_inputs.ASK_MAX_QUESTION_CHARS
_ASK_MAX_OPTIONS = _tool_inputs.ASK_MAX_OPTIONS
_ASK_MAX_OPTION_CHARS = _tool_inputs.ASK_MAX_OPTION_CHARS


def normalize_ask_questions(raw: Any) -> list[dict[str, Any]] | None:
    return _tool_inputs.normalize_ask_questions(raw, max_questions=_ASK_MAX_QUESTIONS,
        max_question_chars=_ASK_MAX_QUESTION_CHARS, max_options=_ASK_MAX_OPTIONS, max_option_chars=_ASK_MAX_OPTION_CHARS)


# --------------------------- sub_agent：子智能体派发 ---------------------------
# 派发参数长度上限（防超大参数直接进上下文）
_SUB_AGENT_TASK_MAX_CHARS = _tool_inputs.SUB_AGENT_TASK_MAX_CHARS


def normalize_sub_agent_task(raw: Any) -> tuple[str | None, str | None]:
    return _tool_inputs.normalize_sub_agent_task(raw, max_chars=_SUB_AGENT_TASK_MAX_CHARS)



# ------------------- write_file / edit_file：内置文件编辑工具 -------------------
# 与 MCP sys_tools_server 的同名工具保持语义一致（相对路径基于会话工作目录，
# worker 进程已 os.chdir），但结果为结构化 dict：除人类可读 message 外携带
# path/action/changed_bytes 等机器可读字段；edit/写改类结果另带 _file_diff
# 顶级键（unified diff + 行数统计，仅前端展示与 JSONL 落盘，不进模型上下文）
# 与 content_hash（read_file 亦可获取，edit_file 可用 expected_hash 校验）。

_WRITE_EDIT_MAX_CHARS = 2 * 1024 * 1024   # 单次写入/替换内容上限（防超大参数）
_EDIT_FILE_MAX_BYTES = 64 * 1024 * 1024  # 超大文件明确拒绝，不能截取前缀后覆盖原文件
_EDIT_BATCH_MAX_CHARS = 4 * 1024 * 1024




def _decode_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """按候选编码依次尝试严格解码，返回 (文本, 实际使用的编码)。
    显式指定 encoding 时严格解码，避免把损坏的替换字符交给模型编辑。"""
    try:
        return decode_text(data, encoding, candidates, strict=bool(encoding))
    except (UnicodeError, LookupError) as exc:
        raise ValueError(f"无法使用指定编码 {encoding} 解码；请检查文件编码") from exc



def _is_probably_binary(data: bytes, encoding: str = "") -> bool:
    """前 8KB 中出现空字节即视为二进制文件。"""
    explicit = encoding.lower().replace("_", "-")
    if explicit.startswith(("utf-16", "utf-32")):
        return False
    return not unicode_bom(data) and b"\x00" in data[:8192]


def _read_bytes_with_limit(path: Path, max_bytes: int) -> bytes:
    """按实际读取量限制文件，避免 stat 后文件增长绕过大小检查。"""
    data = bytearray()
    with path.open("rb") as handle:
        while True:
            check_tool_control()
            # read(max_bytes + 1) can allocate the entire ceiling even for a tiny file.
            chunk = handle.read(min(64 * 1024, max_bytes - len(data) + 1))
            if not chunk:
                break
            if len(data) + len(chunk) > max_bytes:
                raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)} MiB 读取上限")
            data.extend(chunk)
    return bytes(data)


def _display_path(path: Path) -> str:
    """统一用 / 作为路径分隔符展示，避免模型转义反斜杠出错。"""
    return str(path).replace("\\", "/")


# ------------------- 文件 diff 生成（edit_file / write_file 共用） -------------------
# diff 仅供前端展示：工具结果先带 _file_diff 顶级键，由 chat_factory / sub_agent
# 消费时摘取进 SSE 事件与 JSONL 落盘，不进入模型上下文（模型只看 message 中的
# +N/-M 统计），故 diff 文本可给到较大上限；仍设保险丝防止超大 diff 撑爆 JSONL。
_FILE_DIFF_MAX_TEXT_CHARS = 256 * 1024   # 新旧文本超过该大小时跳过逐行 diff
_FILE_DIFF_MAX_DIFF_CHARS = 20000        # diff 文本上限，超限截断
_FILE_DIFF_CONTEXT_LINES = 2             # hunk 上下文行数

# ------------------- V2 文件历史版本链（record_change 入链数据） -------------------
# 工具层只负责携带"写前/写后全文"，由 chat_factory / sub_agent 消费点调用
# memory.file_history.record_change 落版本链（docs/file_diff.md §9）；本模块
# 保持纯函数语义，不感知 session_id 与版本链存储。
_FILE_HISTORY_PAYLOAD_KEY = "_file_history"   # {path, display_path, old_text, new_text}


def _build_file_diff(old_text: str, new_text: str, path: Path) -> dict:
    return _file_diff_result.build_file_diff(old_text, new_text, path, display_path=_display_path(path),
        max_text_chars=_FILE_DIFF_MAX_TEXT_CHARS, max_diff_chars=_FILE_DIFF_MAX_DIFF_CHARS, context_lines=_FILE_DIFF_CONTEXT_LINES)


_EDIT_MODEL_DIFF_MAX_CHARS = 1200


def _build_edit_model_result(path: Path, edits: list[dict[str, Any]], replacements: int, file_diff: dict[str, Any], content_hash: str) -> dict[str, Any]:
    return _file_diff_result.build_edit_model_result(path, edits, replacements, file_diff, content_hash,
        display_path=_display_path(path), max_diff_chars=_EDIT_MODEL_DIFF_MAX_CHARS)


def _content_hash(text: str) -> str:
    """内容指纹：解码后全文的 sha256 前 16 位。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _decode_for_edit(data: bytes, encoding: str) -> tuple[str, str]:
    """编辑必须完整、无损解码；猜测失败时拒绝修改原文件。"""
    try:
        return decode_text(data, encoding, candidates=('gbk',), strict=True)
    except (UnicodeError, LookupError) as exc:
        raise ValueError("文件编码无法无损识别；请指定正确的 encoding，原文件未修改") from exc



_VERSION_UNSET = object()


def _file_version(path):
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None


def _assert_file_version(path, version):
    if _file_version(path) != version:
        raise ValueError("FILE_MODIFIED_EXTERNALLY：读取后文件已变化，请重新 read_file 后合并修改")


def _encoded_size(content, encoding, maximum):
    encoder = codecs.getincrementalencoder(encoding)()
    total = 0
    for offset in range(0, len(content), 65536):
        check_tool_control()
        total += len(encoder.encode(content[offset:offset + 65536]))
        if total > maximum:
            raise ValueError(f"写入结果超过 {maximum // (1024 * 1024)} MiB 上限；请拆分文件")
    total += len(encoder.encode("", final=True))
    if total > maximum:
        raise ValueError(f"写入结果超过 {maximum // (1024 * 1024)} MiB 上限；请拆分文件")
    return total


def _serialized_file_mutation(executor):
    @wraps(executor)
    def execute(args):
        path_text = _tool_path(args)
        if not path_text:
            return executor(args)
        path = Path(path_text).expanduser().resolve()
        guard_path(path)
        identity = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
        lock_path = Path(tempfile.gettempdir()) / "ytools-file-locks" / (identity + ".lock")
        with cross_process_lock(lock_path, timeout=remaining_seconds(10)):
            check_tool_control()
            return executor({**args, "path": str(path)})
    return execute


def _atomic_write_text(path: Path, content: str, encoding: str, *, expected_version=_VERSION_UNSET) -> None:
    """在目标目录完整写入临时文件，再替换目标文件。"""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temp_path)
        check_tool_control()
        if expected_version is not _VERSION_UNSET:
            _assert_file_version(path, expected_version)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_create_text(path: Path, content: str, encoding: str) -> None:
    """Create a complete file without replacing one created concurrently."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        check_tool_control()
        os.link(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _optional_bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须为布尔值 true 或 false")
    return value


def _tool_path(tool_args: dict[str, Any] | None) -> str:
    """读取文件路径参数：新名 path，兼容旧名 full_file_name（旧历史/旧客户端）。

    schema 只暴露 path；旧工具调用（JSONL 历史、重放）沿用 full_file_name，
    执行层与权限规划层统一经本函数读取，保证新旧调用都能执行。
    """
    args = tool_args or {}
    return string_arg(args, "path", args.get("full_file_name", "")).strip()


@_serialized_file_mutation
def execute_write_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 write_file 实现：语义对齐 MCP 同名工具，返回结构化结果。"""
    file_path = _tool_path(tool_args)
    if not file_path:
        raise ValueError("path 不能为空")
    content = tool_args.get("content")
    if not isinstance(content, str):
        raise ValueError("content 必须为字符串")
    if len(content) > _WRITE_EDIT_MAX_CHARS:
        raise ValueError("content 超过 2 Mi 字符上限；请分段写入")
    requested_encoding = string_arg(tool_args, "encoding").strip()
    encoding = requested_encoding or "utf-8"
    legacy_append = _optional_bool(tool_args, "append")
    legacy_create_only = _optional_bool(tool_args, "create_only")
    raw_mode = tool_args.get("mode")
    if raw_mode is not None:
        mode = str(raw_mode).strip().lower()
        if mode not in {"create", "overwrite", "append"}:
            raise ValueError("mode 仅支持 create、overwrite、append")
        if ("append" in tool_args and legacy_append != (mode == "append")) or ("create_only" in tool_args and legacy_create_only != (mode == "create")):
            raise ValueError("mode 与 append/create_only 参数冲突；请只使用 mode")
    else:
        if legacy_create_only or legacy_append:
            mode = "create" if legacy_create_only else "append"
        else:
            raise ValueError("mode 必填：create 新建、overwrite 覆盖、append 追加；文件未修改")
    append = mode == "append"
    create_only = mode == "create"
    expected_hash = string_arg(tool_args, "expected_hash").strip()
    if create_only and expected_hash:
        raise ValueError("create 模式不能同时指定 expected_hash")
    path = Path(file_path).expanduser().resolve()
    guard_path(path)
    existed = path.exists()
    version_before = _file_version(path)
    if create_only and existed:
        raise ValueError("FILE_ALREADY_EXISTS：create_only=true，原文件未修改")
    if expected_hash:
        if not existed or not path.is_file():
            raise ValueError("FILE_MODIFIED_EXTERNALLY：文件不存在，原文件未修改；请重新 read_file")
        guarded_data = _read_bytes_with_limit(path, _EDIT_FILE_MAX_BYTES)
        guarded_text, detected_encoding = _decode_for_edit(guarded_data, requested_encoding)
        if _content_hash(guarded_text) != expected_hash:
            raise ValueError("FILE_MODIFIED_EXTERNALLY：文件内容与 expected_hash 不一致，原文件未修改；请重新 read_file")
        if append and not requested_encoding:
            encoding = detected_encoding
    elif append and existed and not requested_encoding:
        existing_data = _read_bytes_with_limit(path, _EDIT_FILE_MAX_BYTES)
        _, encoding = _decode_for_edit(existing_data, "")
    dir_path = path.parent if str(path.parent).strip() else Path(".")
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
    size_before = path.stat().st_size if existed else 0
    # 旧内容仅用于 diff 生成（成功失败均不中断写入主流程）
    old_text: str | None = ""
    old_encoding = encoding
    if existed and size_before <= _EDIT_FILE_MAX_BYTES:
        try:
            old_data = _read_bytes_with_limit(path, _EDIT_FILE_MAX_BYTES)
            if not _is_probably_binary(old_data, encoding if append else ''):
                old_text, old_encoding = _decode_bytes_used(old_data, encoding if append else '')
            else:
                old_text = None
        except (OSError, ValueError):
            old_text = None
    elif existed:
        old_text = None
    encoded_size = _encoded_size(content, encoding, _EDIT_FILE_MAX_BYTES)
    if append and size_before + encoded_size > _EDIT_FILE_MAX_BYTES:
        raise ValueError("追加结果超过 64 MiB 上限；请写入新文件")
    check_tool_control()
    _assert_file_version(path, version_before)
    if create_only:
        try:
            _atomic_create_text(path, content, encoding)
        except FileExistsError as exc:
            raise ValueError("FILE_ALREADY_EXISTS：create_only=true，原文件未修改") from exc
    elif append:
        with open(path, "a", encoding=encoding, newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    else:
        _atomic_write_text(path, content, encoding, expected_version=version_before)
    action = "追加" if append else "新建" if create_only else "覆盖"
    # 追加模式的新文本 = 旧内容 + 新内容（diff 才能正确反映最终文件变化）
    new_text_for_diff = (old_text + content) if append and old_text is not None else content
    file_diff = _build_file_diff(old_text, new_text_for_diff, path) if old_text is not None else {
        "diff": "", "lines_added": 0, "lines_removed": 0,
        "diff_truncated": False, "diff_skipped": "file_too_large",
    }
    final_hash = _content_hash(new_text_for_diff) if (not append or old_text is not None) else None
    hash_note = f"，内容指纹 {final_hash}" if final_hash else ""
    return {
        "success": True,
        "message": (
            f"[write_file] 已{action} {len(content)} 字符 → {_display_path(path.resolve())}"
            f"{hash_note}"
            + (f"（diff +{file_diff['lines_added']} -{file_diff['lines_removed']} 行）"
               if (file_diff["lines_added"] or file_diff["lines_removed"]) and not file_diff["diff_skipped"] else "")
        ),
        "path": _display_path(path.resolve()),
        "action": mode,
        "created": not existed,
        "size_before": size_before,
        "size_after": path.stat().st_size,
        "chars_written": len(content),
        "encoding": encoding,
        "content_hash": final_hash,
        "_file_diff": file_diff,
        # V2 版本链入链数据：{path, display_path, old_text, new_text}
        # （消费点 chat_factory / sub_agent 摘取后调 record_change，再统一剥离）
        _FILE_HISTORY_PAYLOAD_KEY: {
            "path": str(path.resolve()),
            "display_path": _display_path(path.resolve()),
            "encoding": encoding,
            "old_text": old_text,
            "old_exists": existed, "new_exists": True,
            "old_encoding": old_encoding,
            # 追加模式的最终全文 = 旧内容 + 新写入内容
            "new_text": (
                new_text_for_diff
                if not append or old_text is not None
                else None
            ),
        },
    }


@_serialized_file_mutation
def execute_edit_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 edit_file 实现：语义对齐 MCP 同名工具，返回结构化结果。

    换行归一化匹配 + 原风格写回、0 匹配给最接近候选行提示等行为均与 MCP 版一致；
    额外返回 replacements/eol_style/encoding/content_hash 与 _file_diff（unified diff
    + 行数统计，chat_factory/sub_agent 摘取后仅推送前端与落盘，不进模型上下文）。
    """
    args = tool_args or {}
    if "edits" in args:
        if any(key in args for key in ("old_string", "new_string", "replace_all")):
            raise ValueError(
                "参数格式冲突：请只选一种。单次替换同时传 old_string 和 new_string（可选 replace_all）；"
                "多处替换只传 edits，并在每一项中提供 old_string/new_string/replace_all"
            )
        edits = args["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
            raise ValueError("edits 必须是包含 1-100 项的列表")
    else:
        if "old_string" not in args or "new_string" not in args:
            raise ValueError(
                "单次替换必须同时提供 old_string 和 new_string；多处替换请只提供 edits 列表"
            )
        edits = [args]
    batch_chars = 0
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("old_string"), str) or not edit["old_string"]:
            raise ValueError("old_string 不能为空；如需清空文件请使用 write_file 写入空内容")
        if not isinstance(edit.get("new_string"), str):
            raise ValueError("new_string 必须为字符串（可为空串表示删除）")
        if max(len(edit["old_string"]), len(edit["new_string"])) > _WRITE_EDIT_MAX_CHARS:
            raise ValueError("old_string/new_string 超过 2 Mi 字符上限")
        batch_chars += len(edit["old_string"]) + len(edit["new_string"])
        if batch_chars > _EDIT_BATCH_MAX_CHARS:
            raise ValueError("edits 文本合计超过 4 Mi 字符；请拆分调用")
        _optional_bool(edit, "replace_all")
    encoding = string_arg(args, "encoding").strip()
    file_path = _tool_path(args)
    if not file_path:
        raise ValueError("path 不能为空")
    path = Path(file_path).expanduser().resolve()
    guard_path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{path} 不存在或不是文件")
    size = path.stat().st_size
    version_before = _file_version(path)
    if size > _EDIT_FILE_MAX_BYTES:
        raise ValueError(f"文件超过 {_EDIT_FILE_MAX_BYTES // (1024 * 1024)} MiB 编辑上限，原文件未修改")
    data = _read_bytes_with_limit(path, _EDIT_FILE_MAX_BYTES)
    if _is_probably_binary(data, encoding):
        raise ValueError(f"{path} 疑似二进制文件，不支持文本编辑")
    original, used_encoding = _decode_for_edit(data, encoding)
    expected_hash = string_arg(args, "expected_hash").strip()
    current_hash = _content_hash(original)
    if expected_hash and expected_hash != current_hash:
        raise ValueError("FILE_MODIFIED_EXTERNALLY：文件内容与 expected_hash 不一致，原文件未修改；请重新 read_file")
    line_endings = set()
    eol = "\n"
    for ending in re.finditer(r"\r\n|\r|\n", original):
        if not line_endings:
            eol = ending.group()
        line_endings.add(ending.group())
        if len(line_endings) == 3:
            break
    written_text = original
    # 匹配在换行归一化文本上进行，写回时还原原文件的换行风格
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    original_text = text
    stripped_numbered_lines = False
    replaced = 0
    matched_lines = 0
    for index, edit in enumerate(edits, 1):
        check_tool_control()
        old_norm = edit["old_string"].replace("\r\n", "\n").replace("\r", "\n")
        new_norm = edit["new_string"].replace("\r\n", "\n").replace("\r", "\n")
        count = text.count(old_norm)
        if count == 0:
            # read_file 默认在 content 中加行号；模型复制连续完整行时自动去除。
            numbered = [re.fullmatch(r"\s*(\d+)\| (.*)", line) for line in old_norm.split("\n")]
            if numbered and all(numbered):
                numbers = [int(match.group(1)) for match in numbered]
                if numbers == list(range(numbers[0], numbers[0] + len(numbers))):
                    candidate = "\n".join(match.group(2) for match in numbered)
                    if candidate and candidate in text:
                        old_norm = candidate
                        count = text.count(old_norm)
                        stripped_numbered_lines = True
        if count == 0:
            hint = ""
            close = difflib.get_close_matches(
                old_norm.strip(), [line.strip() for line in text.split("\n")], n=2, cutoff=0.6
            )
            if close:
                shown = " / ".join(f"「{c[:120]}」" for c in close)
                hint = f"；文件中最相似的行：{shown}（注意空格/缩进/全角差异）"
            raise ValueError(
                f"第 {index} 项 old_string 在文件中 0 处匹配；请先用 read_file 核对内容（空格/缩进/换行必须完全一致）{hint}"
            )
        replace_all = _optional_bool(edit, "replace_all")
        if count > 1 and not replace_all:
            raise ValueError(
                f"第 {index} 项 old_string 在文件中匹配到 {count} 处，存在歧义；请提供更长的上下文使其唯一，或传 replace_all=true 全部替换"
            )
        occurrences = count if replace_all else 1
        projected = len(text) + occurrences * (len(new_norm) - len(old_norm))
        if projected > _EDIT_FILE_MAX_BYTES:
            raise ValueError(f"第 {index} 项替换结果过大（超过 64 Mi 字符）；请缩小替换范围")
        raw_pattern = re.compile(r"(?:\r\n|\r|\n)".join(re.escape(part) for part in old_norm.split("\n")))
        def replacement(match):
            local_eol = re.search(r"\r\n|\r|\n", match.group())
            return new_norm.replace("\n", local_eol.group() if local_eol else eol)
        written_text = raw_pattern.sub(replacement, written_text, count=0 if replace_all else 1)
        _encoded_size(written_text, used_encoding, _EDIT_FILE_MAX_BYTES)
        text = text.replace(old_norm, new_norm) if replace_all else text.replace(old_norm, new_norm, 1)
        replaced += count if replace_all else 1
        matched_lines += len(old_norm.split("\n"))
    updated = text
    file_diff = _build_file_diff(original_text, updated, path)
    if written_text != original:
        _atomic_write_text(path, written_text, used_encoding, expected_version=version_before)
    style = "mixed" if len(line_endings) > 1 else {"\r\n": "CRLF", "\r": "CR", "\n": "LF"}[eol]
    diff_note = (
        f"，diff +{file_diff['lines_added']} -{file_diff['lines_removed']} 行"
        if not file_diff["diff_skipped"] and (file_diff["lines_added"] or file_diff["lines_removed"])
        else ""
    )
    return {
        "success": True,
        "message": (
            f"[edit_file] 已在 {_display_path(path)} 中替换 {replaced} 处"
            f"（换行风格 {style}，编码 {used_encoding}{diff_note}"
            f"{'，已去除 read_file 行号' if stripped_numbered_lines else ''}），"
            f"内容指纹 {_content_hash(written_text)}"
        ),
        "path": _display_path(path),
        "action": "replace",
        "replacements": replaced,
        "matched_occurrences": replaced,
        "eol_style": style,
        "encoding": used_encoding,
        "matched_lines": matched_lines,
        "content_hash": _content_hash(written_text),
        "_file_diff": file_diff,
        "_model_result": _build_edit_model_result(
            path, edits, replaced, file_diff, _content_hash(written_text)
        ),
        # 快照使用精确换行文本，展示 diff 的预算不限制可回退内容。
        _FILE_HISTORY_PAYLOAD_KEY: {
            "path": str(path.resolve()),
            "display_path": _display_path(path.resolve()),
            "encoding": used_encoding,
            "old_text": original,
            "new_text": written_text,
            "old_exists": True, "new_exists": True,
        },
    }


def try_execute_builtin_file_tool(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any] | None:
    """执行内置文件编辑工具；非目标名称返回 None 交由其他执行器处理。

    异常统一转为 {"error": ...} 结构化结果（模型可见的错误反馈），
    与 todo/ask_user 的 builtin_results 错误通道保持同构。
    """
    if tool_name == APPLY_PATCH_NAME:
        from .context_patch import execute as executor
    elif tool_name == WRITE_FILE_NAME:
        executor = execute_write_file
    elif tool_name == EDIT_FILE_NAME:
        executor = execute_edit_file
    elif tool_name == READ_FILE_NAME:
        executor = execute_read_file
    elif tool_name == SEARCH_FILES_NAME:
        executor = execute_search_files
    elif tool_name == LIST_FILES_NAME:
        executor = execute_list_files
    else:
        return None
    try:
        return executor(tool_args if isinstance(tool_args, dict) else {})
    except Exception as exc:
        return {"success": False, "error": str(exc), "tool": tool_name}


# ------------------- read_file / search_files：内置读取与检索工具 -------------------
# 与 MCP sys_tools_server 的同名工具保持语义一致（相对路径基于会话工作目录）；
# read_file 返回结构化结果（含 path/total_lines/range 等字段），search_files
# 返回纯文本（与 MCP 版相同的区块格式，便于模型直接阅读）。

_READ_FILE_MAX_LINES = 2000
_READ_FILE_MAX_CHARS = 60000
_READ_FILE_MAX_BYTES = 64 * 1024 * 1024
_SEARCH_MAX_RESULTS_LIMIT = 200
_SEARCH_CONTEXT_MAX = 5
_SEARCH_DEFAULT_FILE_MB = 2.0
_SEARCH_HARD_FILE_MAX_BYTES = 16 * 1024 * 1024
# 模型未传 max_depth 时的默认递归深度（与 schema 描述一致）。
# 注意：内置工具走 dict 参数分发，schema 的 default 不会被框架自动应用，
# 缺省值必须在这里显式兜底，否则会退化成 0（只扫当前目录一层）。
_SEARCH_DEFAULT_MAX_DEPTH = 6
_SEARCH_DEFAULT_MAX_SCANNED_FILES = 3000
_SEARCH_DEFAULT_MAX_SCAN_SECONDS = 10.0
_read_snapshot_cache = OrderedDict()
_read_snapshot_lock = Lock()
_READ_SNAPSHOT_CACHE_WEIGHT = 4 * 1024 * 1024
# 跨文件搜索/遍历时默认跳过的目录名（含各语言依赖与构建产物目录）
_SKIP_DIR_NAMES = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build",
    "history_files", "tmp", "vendor",
}






def _path_matches(path, root, pattern):
    """A basename glob, or an anchored relative glob; **/ also matches zero directories."""
    if not pattern:
        return True
    pattern = pattern.replace('\\', '/')
    if pattern.startswith('./'):
        pattern = pattern[2:]
    if '/' not in pattern:
        return fnmatch.fnmatch(path.name, pattern)
    expressions = []
    parts = pattern.split('/')
    for i, part in enumerate(parts):
        if part == '**':
            expressions.append('(?:[^/]+/)*' if i < len(parts) - 1 else '.*')
        else:
            expressions.append(fnmatch.translate(part)[4:-3] + ('/' if i < len(parts) - 1 else ''))
    flags = re.IGNORECASE if os.name == 'nt' else 0
    return re.fullmatch(''.join(expressions), path.relative_to(root).as_posix(), flags) is not None


def execute_list_files(tool_args):
    from . import query_pages
    args = tool_args or {}
    resumed = query_pages.resume(args, files=True)
    if resumed is not None:
        return resumed
    root = _resolve_dir_path(args.get('dir_path') or '.')
    guard_path(root)
    pattern = string_arg(args, 'pattern', '*', required=True, max_length=4096)
    maximum = int_arg(args, 'max_scanned_files', 3000, minimum=1, maximum=20000)
    duration = number_arg(args, 'max_scan_seconds', 10, minimum=0.1, maximum=120)
    depth = int_arg(args, 'max_depth', 6, maximum=128)
    exclusions = args.get('exclude_dirs')
    if exclusions is not None and (not isinstance(exclusions, list) or any(not isinstance(x, str) or not x for x in exclusions)):
        raise ValueError('exclude_dirs 必须为目录名字符串数组')
    errors, denied = [], []
    versions = {root: _file_version(root)}
    paths, scanned, size = [], 0, 0
    complete = True
    deadline = time.monotonic() + duration
    for path, kind in _walk_entries(root, depth, exclusions, errors.append, denied.append,
                                    _optional_bool(args, 'no_default_excludes'),
                                    _optional_bool(args, 'respect_gitignore'), versions):
        check_tool_control()
        if scanned >= maximum or len(versions) >= 20000 or time.monotonic() >= deadline:
            complete = False
            break
        if not guard_path(path, search=True):
            denied.append(path)
            continue
        try:
            versions.setdefault(path, _file_version(path))
        except OSError:
            errors.append(path)
            continue
        if kind != 'file':
            continue
        scanned += 1
        if not _optional_bool(args, 'no_default_excludes') and path.name.startswith('.'):
            continue
        if _path_matches(path, root, pattern):
            shown = _display_path(path)
            if size + len(shown) > query_pages.MAX_SNAPSHOT_CHARS:
                complete = False
                break
            paths.append(shown)
            size += len(shown)
    scan_truncated = not complete
    complete = complete and not errors and not denied
    return query_pages.create(args, paths, {
        'success': True, 'path': _display_path(root), 'files_checked': scanned,
        'scan_complete': complete, 'scan_truncated': scan_truncated,
        'skipped_unreadable': len(errors), 'skipped_permission': len(denied),
        '_prefix': '[list_files] ' + ('扫描完整' if complete else '扫描未完成；未列出的文件不能判定不存在'),
    }, versions, files=True)


def _resolve_dir_path(dir_path: Any) -> Path:
    """解析目录参数：空串回退当前目录（生成任务中即会话工作目录）。"""
    text = str(dir_path).strip() if dir_path else ""
    candidate = Path(text).expanduser() if text else Path(".")
    if not candidate.exists():
        raise FileNotFoundError(f"路径不存在: {candidate}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"不是目录: {candidate}")
    return candidate.resolve()


def _walk_entries(root: Path, max_depth: int, exclude_dirs=None, onerror=None, onpermission=None,
                 no_default_excludes: bool = False, respect_gitignore=False, versions=None):
    """遍历目录树，yield (路径, "file"|"dir")。
    exclude_dirs 追加到默认排除（隐藏目录与 _SKIP_DIR_NAMES）之后；
    no_default_excludes=True 时关闭默认排除，只应用 exclude_dirs。
    max_depth 为相对 root 的深度上限（0=仅当前目录）。"""
    root_depth = len(root.parts)
    user_rules = list(exclude_dirs) if exclude_dirs else []
    if no_default_excludes:
        exclusions = user_rules
    else:
        exclusions = list(_SKIP_DIR_NAMES) + [".*"] + user_rules
    if respect_gitignore:
        from .search_ignore import IgnoreRules
        ignore = IgnoreRules(root, versions, onerror, onpermission)
    else:
        ignore = None
    for current, dir_names, file_names in os.walk(root, onerror=onerror):
        check_tool_control()
        current_path = Path(current)
        if not guard_path(current_path, search=True):
            if onpermission:
                onpermission(current_path)
            dir_names[:] = []
            continue
        if ignore and not ignore.enter(current_path):
            dir_names[:] = []
            continue
        depth = len(current_path.parts) - root_depth
        candidates = sorted(
            name for name in dir_names
            if not any(fnmatch.fnmatch(name.lower(), rule.lower()) for rule in exclusions)
            and not (ignore and ignore.ignored(current_path / name, True))
        )
        keep_dirs = []
        for name in candidates:
            if guard_path(current_path / name, search=True):
                keep_dirs.append(name)
            elif onpermission:
                onpermission(current_path / name)
        for name in keep_dirs:
            yield current_path / name, "dir"
        # 深度用尽时剪枝，不再下钻（目录本身已在上面列出）
        dir_names[:] = keep_dirs if depth < max_depth else []
        for name in sorted(file_names):
            if ignore and ignore.ignored(current_path / name):
                continue
            yield current_path / name, "file"


def _iter_text_lines(text):
    """Match str.splitlines() without allocating an object for every line at once."""
    start = 0
    for match in re.finditer(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text):
        yield text[start:match.start()]
        start = match.end()
    if start < len(text):
        yield text[start:]


def _read_text_snapshot(path, encoding):
    from . import text_index
    version = _file_version(path)
    key = (str(path.resolve()), version, encoding)
    indexed = text_index.get(key)
    if indexed is not None:
        return indexed
    with _read_snapshot_lock:
        cached = _read_snapshot_cache.get(key)
        if cached is not None:
            _read_snapshot_cache.move_to_end(key)
            return cached
    data = _read_bytes_with_limit(path, _READ_FILE_MAX_BYTES)
    if _is_probably_binary(data, encoding):
        raise ValueError(f"{_display_path(path)} 疑似二进制文件，拒绝按文本读取")
    text, used_encoding = _decode_bytes_used(data, encoding)
    _assert_file_version(path, version)
    small = len(text) <= 1024 * 1024
    line_count = sum(1 for _ in re.finditer(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text)) if small else 4097
    lines = tuple(_iter_text_lines(text)) if small and line_count <= 4096 else None
    weight = len(text) * 4 + (sum(len(line) * 4 + 64 for line in lines) if lines is not None else 0)
    snapshot = (text, used_encoding, _content_hash(text), lines, weight)
    if lines is None:
        indexed = text_index.store(key, text, used_encoding, snapshot[2], _iter_text_lines)
        if indexed is not None:
            return indexed
    if lines is not None and weight <= _READ_SNAPSHOT_CACHE_WEIGHT:
        with _read_snapshot_lock:
            _read_snapshot_cache[key] = snapshot
            while sum(item[4] for item in _read_snapshot_cache.values()) > _READ_SNAPSHOT_CACHE_WEIGHT:
                _read_snapshot_cache.popitem(last=False)
    return snapshot


def execute_read_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 read_file 实现：语义对齐 MCP 同名工具，返回结构化结果。"""
    args = tool_args or {}
    return_max_chars = max(512, min(_READ_FILE_MAX_CHARS, int(args.get("_max_chars") or _READ_FILE_MAX_CHARS)))
    file_path = _tool_path(args)
    if not file_path:
        raise ValueError("path 不能为空")
    from memory.storage_layout import remap_legacy_attachment
    path = remap_legacy_attachment(file_path)
    guard_path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在")
    if not path.is_file():
        raise IsADirectoryError(f"{path} 不是文件")
    size = path.stat().st_size
    if size > _READ_FILE_MAX_BYTES:
        raise ValueError(
            f"{_display_path(path)} 超过 {_READ_FILE_MAX_BYTES // (1024 * 1024)} MiB 读取上限；"
            "请缩小文件，或用命令按块查看"
        )
    encoding = string_arg(args, "encoding")
    text, used_encoding, content_hash, cached_lines, _weight = _read_text_snapshot(path, encoding)
    start = int_arg(args, "start_line", 1, minimum=1)
    limit = int_arg(args, "limit", 200, minimum=1, maximum=_READ_FILE_MAX_LINES)
    char_offset = int_arg(args, "char_offset", 0)
    show_numbers = _optional_bool(args, "show_line_numbers", default=True)
    raw_end = int_arg(args, "end_line", 0)
    if "end_line" in args and "limit" in args:
        raise ValueError("limit 与 end_line 不能同时指定")
    requested_end = (raw_end if raw_end > 0 else float("inf")) if "end_line" in args else start + limit - 1
    selected = []
    total = 0
    from .text_index import TextIndex
    if isinstance(text, TextIndex):
        total = text.total
        selected = text.select(start, min(requested_end, total), _READ_FILE_MAX_LINES)
    elif cached_lines is not None:
        total = len(cached_lines)
        selected = list(cached_lines[start - 1:min(start - 1 + _READ_FILE_MAX_LINES,
                                                int(requested_end) if requested_end != float("inf") else total)])
    else:
        for total, line in enumerate(_iter_text_lines(text), 1):
            if total % 1024 == 0:
                check_tool_control()
            if start <= total <= requested_end and len(selected) < _READ_FILE_MAX_LINES:
                selected.append(line)
    if total == 0:
        return {
            "success": True,
            "message": f"[read_file] {_display_path(path)} | 空文件 | 编码 {used_encoding}",
            "content": "", "path": _display_path(path), "encoding": used_encoding,
            "mode": "lines", "total_lines": 0, "range_start": 0,
            "range_end": 0, "truncated": False, "has_more": False,
            "content_hash": content_hash,
        }
    # end_line 仅为旧调用保留，不再暴露给模型；新调用统一使用 start_line + limit。
    if "end_line" in args:
        end = total if raw_end <= 0 else min(raw_end, total)
    else:
        end = min(start + limit - 1, total)
    if start > total:
        raise ValueError(f"start_line={start} 超出文件总行数 {total}，无内容可读")
    if end < start:
        raise ValueError(f"end_line={raw_end} 小于 start_line={start}，无内容可读")
    limited = end - start + 1 > _READ_FILE_MAX_LINES
    # 单行超长（如压缩过的大 JSON/日志）时按字符分块返回，避免头尾截断丢失中间内容
    if len(selected[0]) >= return_max_chars - 32:
        line_text = selected[0]
        offset = char_offset
        chunk = line_text[offset:offset + return_max_chars]
        if not chunk:
            raise ValueError(f"char_offset={offset} 已超出该行长度 {len(line_text)}，无内容可读")
        next_offset = offset + len(chunk)
        footer = (f"[read_file] {_display_path(path)} | 单行超长（共 {len(line_text)} 字符，编码 {used_encoding}）"
                  f" | 本次返回第 {offset + 1}-{next_offset} 字符")
        if next_offset < len(line_text):
            footer += f" | 未读完，请用 char_offset={next_offset} 继续读取"
        elif start < total:
            footer += f" | 下一行请用 start_line={start + 1} 继续读取"
        return {
            "success": True,
            "message": footer,
            "content": chunk,
            "path": _display_path(path),
            "encoding": used_encoding,
            "mode": "char_chunk",
            "total_lines": total,
            "range_start": start,
            "range_end": start,
            "truncated": next_offset < len(line_text) or start < total,
            "has_more": next_offset < len(line_text) or start < total,
            "char_offset": offset,
            "next_char_offset": next_offset if next_offset < len(line_text) else None,
            "next_start_line": start + 1 if next_offset >= len(line_text) and start < total else None,
            "line_length": len(line_text),
            "content_hash": content_hash,
        }
    if char_offset != 0:
        raise ValueError("char_offset 只适用于单行超长的 char_chunk 模式")
    width = len(str(start + len(selected) - 1))
    output_lines = []
    used_chars = 0
    for no, line in enumerate(selected, start=start):
        shown = f"{no:>{width}}| {line}" if show_numbers else line
        added = len(shown) + (1 if output_lines else 0)
        if output_lines and used_chars + added > return_max_chars:
            break
        output_lines.append(shown)
        used_chars += added
    body = "\n".join(output_lines)
    char_limited = len(output_lines) < len(selected)
    last_line = start + len(output_lines) - 1
    footer = f"[read_file] {_display_path(path)} | 共 {total} 行 | 本次第 {start}-{last_line} 行 | 编码 {used_encoding}"
    if char_limited:
        footer += f" | 字符预算已满，请从 start_line={last_line + 1} 继续读取"
    elif last_line < total or limited:
        footer += f" | 未读完，请从 start_line={last_line + 1} 继续读取"
    result = {
        "success": True,
        "message": footer,
        "content": body,
        "path": _display_path(path),
        "encoding": used_encoding,
        "mode": "lines",
        "total_lines": total,
        "range_start": start,
        "range_end": last_line,
        "truncated": bool(limited or char_limited),
        "has_more": last_line < total or char_limited,
        # 全文（非本次区间）的内容指纹：模型可在 edit_file 时以 expected_hash
        # 引用，用于检测读后文件被外部修改的竞态
        "content_hash": content_hash,
    }
    if last_line < total:
        result["next_start_line"] = last_line + 1
    return result


def _format_match_block(path: Path, lines: list, hit_indexes: list, context: int, offset=0) -> list:
    """把（相邻+上下文合并后的）命中区间格式化为 "路径:行号" 区块。"""
    ranges = []
    for idx in hit_indexes:
        if ranges and idx <= ranges[-1][1] + 2 * context + 1:
            ranges[-1][1] = idx
        else:
            ranges.append([idx, idx])
    blocks = []
    hits = set(hit_indexes)
    for start, end in ranges:
        low = max(0, start - context)
        high = min(len(lines) - 1, end + context)
        header = f"{_display_path(path)}:{start + offset + 1}" + (f"-{end + offset + 1}" if end != start else "")
        body = []
        for no in range(low, high + 1):
            marker = ">" if no in hits else " "
            line = lines[no][:300] + ("…[长行请用 read_file 续读]" if len(lines[no]) > 300 else "")
            body.append(f"{marker}{no + offset + 1}| {line}")
        blocks.append(header + "\n" + "\n".join(body))
    return blocks


def execute_search_files(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Search file contents; return bounded snippets, counts and completeness."""
    args = tool_args or {}
    from . import query_pages
    resumed = query_pages.resume(args)
    if resumed is not None:
        return resumed
    if not _optional_bool(args, 'count_total'):
        from .search_scan import execute
        return execute(args)
    return_max_chars = max(512, min(60000, int(args.get("_max_chars") or 60000)))
    pattern = string_arg(args, "pattern", required=True, max_length=8192)
    if not pattern:
        raise ValueError("pattern 不能为空")
    root = _resolve_dir_path(args.get("dir_path") or ".")
    guard_path(root)
    ignore_case = _optional_bool(args, "ignore_case")
    flags = re.IGNORECASE if ignore_case else 0
    # 键名兼容：schema 定义为 is_regex；旧实现误读 isregex 导致该参数被静默忽略
    is_regex_raw = args.get("is_regex")
    if is_regex_raw is None:
        is_regex_raw = args.get("isregex")
    isregex = False if is_regex_raw is None else _optional_bool({"is_regex": is_regex_raw}, "is_regex")
    if isregex:
        from util.bounded_regex import compile_pattern
        regex = compile_pattern(pattern, ignore_case=ignore_case)
    else:
        regex = re.compile(re.escape(pattern), flags)
    context = int_arg(args, "context_lines", 0, maximum=_SEARCH_CONTEXT_MAX)
    limit = int_arg(args, "max_results", 50, minimum=1, maximum=_SEARCH_MAX_RESULTS_LIMIT)
    name_filter = string_arg(args, "file_pattern").strip()
    size_limit = number_arg(args, "max_file_mb", _SEARCH_DEFAULT_FILE_MB, maximum=16)
    effective_max_bytes = _SEARCH_HARD_FILE_MAX_BYTES
    if size_limit:
        effective_max_bytes = min(effective_max_bytes, max(1, int(size_limit * 1024 * 1024)))
    blocks = []
    snapshot_chars = 0
    versions = {root: _file_version(root)}
    total_matches = 0
    files_scanned = 0
    files_matched = 0
    skipped_large = 0
    skipped_unreadable = 0
    total_blocks = 0
    scan_truncated = False
    capture_truncated = False
    started_at = time.monotonic()
    max_scanned_files = int_arg(args, "max_scanned_files", _SEARCH_DEFAULT_MAX_SCANNED_FILES, minimum=1, maximum=20000)
    max_scan_seconds = number_arg(args, "max_scan_seconds", _SEARCH_DEFAULT_MAX_SCAN_SECONDS, minimum=0.1, maximum=120)
    deadline = started_at + max_scan_seconds
    files_checked = 0
    max_depth = int_arg(args, "max_depth", _SEARCH_DEFAULT_MAX_DEPTH, maximum=128)
    exclude_dirs = args.get("exclude_dirs")
    if exclude_dirs is not None and (not isinstance(exclude_dirs, list) or
            any(not isinstance(rule, str) or not rule for rule in exclude_dirs)):
        raise ValueError("exclude_dirs 必须为目录名字符串数组")
    no_default_excludes = _optional_bool(args, "no_default_excludes")
    walk_errors = []
    permission_skips = []
    for path, kind in _walk_entries(root, max(0, max_depth), exclude_dirs, walk_errors.append,
                                    permission_skips.append, no_default_excludes,
                                    _optional_bool(args, 'respect_gitignore'), versions):
        check_tool_control()
        if len(versions) >= 20000 or time.monotonic() - started_at >= max_scan_seconds:
            scan_truncated = True
            break
        if kind == 'file' and path.name.startswith('.') and not no_default_excludes:
            continue
        if not guard_path(path, search=True):
            permission_skips.append(path)
            continue
        try:
            versions.setdefault(path, _file_version(path))
        except OSError:
            skipped_unreadable += 1
            continue
        if kind != "file":
            continue
        if name_filter and not _path_matches(path, root, name_filter):
            continue
        if files_checked >= max_scanned_files or len(versions) >= 20000 or time.monotonic() - started_at >= max_scan_seconds:
            scan_truncated = True
            break
        files_checked += 1
        try:
            file_size = path.stat().st_size
            if file_size > effective_max_bytes:
                skipped_large += 1
                continue  # 跳过超过大小上限的文件
            data = _read_bytes_with_limit(path, effective_max_bytes)
        except ValueError:
            skipped_large += 1
            continue
        except OSError:
            skipped_unreadable += 1
            continue
        if _is_probably_binary(data):
            continue
        files_scanned += 1
        decoded, _used = _decode_bytes_used(data)
        lines = decoded.splitlines()
        hit_indexes = []
        file_matches = 0
        file_blocks = 0
        previous_hit = None
        try:
            for idx, line in enumerate(lines):
                if idx % 64 == 0:
                    check_tool_control()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    scan_truncated = True
                    break
                matched = regex.search(line, timeout=min(0.25, remaining)) if isregex else regex.search(line)
                if matched:
                    file_matches += 1
                    if previous_hit is None or idx > previous_hit + 2 * context + 1:
                        file_blocks += 1
                    previous_hit = idx
                    if len(hit_indexes) < 20000:
                        hit_indexes.append(idx)
                    else:
                        capture_truncated = True
        except TimeoutError:
            scan_truncated = True
        if not hit_indexes:
            if scan_truncated:
                break
            continue
        files_matched += 1
        total_matches += file_matches
        block_group = _format_match_block(path, lines, hit_indexes, context)
        total_blocks += file_blocks
        for block in block_group:
            if snapshot_chars + len(block) > query_pages.MAX_SNAPSHOT_CHARS:
                capture_truncated = True
                remaining = query_pages.MAX_SNAPSHOT_CHARS - snapshot_chars
                if remaining > 0:
                    cut = block.rfind('\n', 0, remaining)
                    if cut > 0:
                        blocks.append(block[:cut])
                        snapshot_chars += cut
                break
            blocks.append(block)
            snapshot_chars += len(block)
        if scan_truncated:
            break
    search_label = "正则" if isregex else "文本"
    skipped_unreadable += len(walk_errors)
    skipped_permission = len(permission_skips)
    scan_complete = not (scan_truncated or skipped_large or skipped_unreadable or skipped_permission)
    warnings = []
    if scan_truncated:
        warnings.append("[扫描未完成：达到文件数或时间上限；统计仅覆盖已扫描部分，不能判定目标不存在。]")
    if skipped_large:
        warnings.append(f"[跳过 {skipped_large} 个超过 {effective_max_bytes / (1024 * 1024):g}MB 的文件（未读取）；可用 max_file_mb 调整，最高 16MB。]")
    if skipped_unreadable:
        warnings.append(f"[未读取 {skipped_unreadable} 个文件或目录（权限/读取失败）；结果不完整。]")
    if skipped_permission:
        warnings.append(f"[跳过 {skipped_permission} 个未经授权的路径；需要另行指定实际目标并确认后访问。]")
    if not isregex and not total_matches and any(token in pattern for token in ("|", ".*", "\\d", "\\s")):
        warnings.append("[当前按普通文本匹配；若 pattern 使用正则语法，请显式传 is_regex=true。]")
    header = (f"[search_files] {search_label}: {pattern[:160]} | 目录: {_display_path(root)[:160]}"
              f" | 扫描 {files_scanned} 个文本文件，命中 {files_matched} 个文件 / {total_matches} 处")
    prefix = "\n".join(warnings + [header])
    if len(prefix) > return_max_chars - 180:
        header = f"[search_files] 扫描 {files_scanned} 个文本文件，命中 {files_matched} 个文件 / {total_matches} 处；路径和表达式见返回字段。"
        prefix = "\n".join(warnings + [header])
    metadata = {
        "success": True,
        "path": _display_path(root),
        "pattern": regex.pattern,
        "files_scanned": files_scanned,
        "files_checked": files_checked,
        "files_matched": files_matched,
        "total_matches": total_matches,
        "total_blocks": total_blocks,
        "skipped_large": skipped_large,
        "skipped_unreadable": skipped_unreadable,
        "skipped_permission": skipped_permission,
        "scan_complete": scan_complete,
        "scan_truncated": scan_truncated,
        "max_depth": max_depth,
        "no_default_excludes": no_default_excludes,
        "exclude_dirs": list(exclude_dirs or []),
        "capture_truncated": capture_truncated,
        "total_matches_exact": scan_complete,
        "scan_pending": False,
        "_prefix": prefix,
    }
    return query_pages.create(args, blocks, metadata, versions)


# ------------------- read_document：读取文档解析文本（按需/分页） -------------------
# 支持会话上传文件，以及用户明确传入的本地文档路径；用户显式启用后才注入。

_READ_DOCUMENT_DEFAULT_MAX_CHARS = 10000
_READ_DOCUMENT_MAX_CHARS = 20000
_READ_DOCUMENT_LOCAL_MAX_BYTES = 10 * 1024 * 1024
_DOCUMENT_TEXT_CACHE_MAX_CHARS = 4 * 1024 * 1024
_document_text_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_document_text_cache_lock = Lock()
_document_table_cache: OrderedDict[tuple[str, int, int], Any] = OrderedDict()
_document_table_cache_lock = Lock()


def _bounded_document_result(result, token_budget, *, render_table=None):
    """Fit JSON metadata as well as content; preserve exact continuation positions."""
    from factory.agent_runtime.chat_runtime import estimate_text_tokens
    for _ in range(40):
        if estimate_text_tokens(result) <= token_budget:
            return result
        content = result.get("content", "")
        if not content:
            break
        size = int(len(content) * 0.75)
        if render_table is not None:
            try:
                page = render_table(size)
            except ValueError:
                return {"error": "表头或单行超过当前模型剩余预算；请用 mode=text、offset/limit 分段读取，或先压缩上下文"}
            result.pop("next_row", None)
            result.pop("next_sheet", None)
            result.update(page)
            result["limit"] = size
            result["message"] = "已按当前模型预算返回完整表格行并重复表头；继续用 next_row/start_row 或 next_sheet/sheet"
        else:
            result["content"] = content[:size]
            end = result["offset"] + size
            result.update(end_char=end, has_more=end < result["total_chars"])
            result.pop("next_offset", None)
            result.pop("next_start_char", None)
            if result["has_more"]:
                result.update(next_offset=end, next_start_char=end)
            result["message"] = f"按当前模型预算返回字符 [{result['offset']}, {end})；继续用 offset={end}"
    return {"error": "当前模型剩余上下文不足以返回文档内容；请先压缩上下文或减少同轮工具调用"}


def _extract_document_table(path: Path, filename: str, data: bytes):
    from factory.table_document import parse_table
    if len(data) > _READ_DOCUMENT_LOCAL_MAX_BYTES:
        raise ValueError("文档超过表格解析上限 10 MiB；请拆分文件，或使用模型支持的原生文档输入")
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _document_table_cache_lock:
        cached = _document_table_cache.get(key)
        if cached is not None:
            _document_table_cache.move_to_end(key)
            return cached
    document = parse_table(filename, data)
    weight = lambda item: item.chars + item.cells * 16 + item.row_count * 32
    if weight(document) <= _DOCUMENT_TEXT_CACHE_MAX_CHARS:
        with _document_table_cache_lock:
            _document_table_cache[key] = document
            while (len(_document_table_cache) > 4 or
                   sum(map(weight, _document_table_cache.values())) > _DOCUMENT_TEXT_CACHE_MAX_CHARS):
                _document_table_cache.popitem(last=False)
    return document


def _extract_document_text(path: Path, filename: str, data: bytes) -> str:
    """缓存较小的解析文本；文件修改后由 mtime 与大小自动换键。"""
    from factory import file_factory

    if len(data) > _READ_DOCUMENT_LOCAL_MAX_BYTES:
        raise ValueError("文档超过文本解析上限 10 MiB；请拆分文件或用 read_file/终端筛选，原生输入使用独立字节配额")
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _document_text_cache_lock:
        cached = _document_text_cache.get(key)
        if cached is not None:
            _document_text_cache.move_to_end(key)
            return cached
    content = file_factory.extract_text_from_bytes(filename, data)
    if len(content) <= _DOCUMENT_TEXT_CACHE_MAX_CHARS:
        with _document_text_cache_lock:
            _document_text_cache[key] = content
            _document_text_cache.move_to_end(key)
            while len(_document_text_cache) > 4 or sum(map(len, _document_text_cache.values())) > _DOCUMENT_TEXT_CACHE_MAX_CHARS:
                _document_text_cache.popitem(last=False)
    return content


def _native_document_byte_limit() -> int:
    configured = file_memory.native_doc_max_bytes()
    hard_limit = 64 * 1024 * 1024
    return min(configured, hard_limit) if configured > 0 else hard_limit


def _load_local_document(filename: str, *, parse_text: bool = True,
                         max_bytes: int | None = None) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    """读取显式指定的本地文档并复用上传链路解析器。"""
    from memory.storage_layout import remap_legacy_attachment
    path = remap_legacy_attachment(filename)
    guard_path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, None, f"本地文档路径不存在或不可访问：{filename}"
    if not path.is_file():
        return None, None, f"本地文档路径不是文件：{filename}"
    byte_limit = _READ_DOCUMENT_LOCAL_MAX_BYTES if max_bytes is None else max_bytes
    try:
        size = path.stat().st_size
        if size > byte_limit:
            return None, None, (
                f"本地文档超过读取上限 {byte_limit / (1024 * 1024):g} MiB："
                f"{path.name}（{size / (1024 * 1024):.1f}MB）"
            )
        data = _read_bytes_with_limit(path, byte_limit)
    except (OSError, ValueError) as exc:
        return None, None, f"读取本地文档失败：{exc}"

    from factory import file_factory

    if (
        file_factory.get_parser_for_filename(path.name) is None
        and not file_factory.get_text_extension(path.name)
    ):
        return None, None, (
            f"不支持读取该文件类型：{path.suffix or '无扩展名'}；"
            "请使用 PDF/Word/Excel/CSV/Markdown/文本文件"
        )
    parse_error = ""
    content = ""
    if parse_text:
        try:
            content = _extract_document_text(path, path.name, data)
        except Exception as exc:
            parse_error = str(exc)

    try:
        from config import DEFAULT_FILE_TEXT_MAX_CHARS
        from env_manager import load_var

        max_chars = int(float(load_var("FILE_TEXT_MAX_CHARS", DEFAULT_FILE_TEXT_MAX_CHARS)))
        max_chars = max(0, max_chars)
    except (TypeError, ValueError):
        from config import DEFAULT_FILE_TEXT_MAX_CHARS

        max_chars = DEFAULT_FILE_TEXT_MAX_CHARS
    total_chars = len(content)
    truncated = max_chars > 0 and total_chars > max_chars
    if truncated:
        content = content[:max_chars]
    record = {
        "filename": str(path),
        "display_filename": path.name,
        "type": path.suffix.lower().lstrip("."),
        "content": content,
        "parse_failed": bool(parse_error),
        "parse_error": parse_error,
        "content_truncated": truncated,
        "content_total_chars": total_chars,
        "size": size,
    }
    return record, data, None


def _read_document_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    return int_arg({"offset/limit": value}, "offset/limit", default, minimum=minimum, maximum=maximum)




def execute_read_document(
    tool_args: dict[str, Any],
    session_id: str,
    support_doc_types: Any = None,
) -> dict[str, Any]:
    """读取解析文本区间，或按需构建一次性原生文档部件。

    原生能力由 supportDocTypes 与文件扩展名决定，和 vision 标志分开判断。
    原生结果不带解析文本；调用方须把 native.part 作为独立消息发送，且不得落盘。
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    try:
        filename = string_arg(args, "filename", required=True).strip()
        for key, minimum, maximum in (("offset", 0, 10**9), ("start_char", 0, 10**9),
                                      ("limit", 1, _READ_DOCUMENT_MAX_CHARS), ("max_chars", 1, 10**9),
                                      ("page_start", 1, 10**9), ("page_count", 1, 20)):
            if key in args:
                int_arg(args, key, 0, minimum=minimum, maximum=maximum)
    except ValueError as exc:
        return {"error": str(exc)}
    if not filename:
        return {"error": "filename 不能为空（传入会话文件名或明确的本地文档路径）"}

    mode = str(args.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "text", "table", "native"}:
        return {"error": "mode 只支持 auto、text、table 或 native"}
    from factory.table_document import TABLE_EXTENSIONS, parse_table, table_page
    has_table_args = any(key in args for key in ("sheet", "header_row", "start_row", "row_limit"))
    has_offset = any(key in args for key in ("offset", "start_char"))
    if has_table_args and mode in {"text", "native"}:
        return {"error": "sheet/header_row/start_row/row_limit 仅用于 mode=auto 或 table"}
    if (mode == "table" or has_table_args) and has_offset:
        return {"error": "表格按行读取，不接受 offset/start_char；请用 start_row 或改为 mode=text"}
    has_text_range = any(
        key in args for key in ("offset", "limit", "start_char", "max_chars")
    )
    has_page_start = "page_start" in args
    has_page_count = "page_count" in args
    if has_page_start != has_page_count:
        return {"error": "page_start 和 page_count 必须同时指定"}
    has_page_range = has_page_start and has_page_count
    if has_page_range and (mode == "table" or has_table_args):
        return {"error": "表格参数不能与 PDF 页面范围同时使用"}
    if has_text_range and has_page_range:
        return {"error": "字符 offset/limit 与 PDF page_start/page_count 不能同时使用"}
    if mode == "native" and has_text_range:
        return {"error": "mode=native 不接受字符区间；请移除 offset/limit 或使用 mode=text"}
    if mode == "text" and has_page_range:
        return {"error": "mode=text 使用 offset/limit；PDF 页面范围请使用 mode=auto 或 native"}

    offset = _read_document_int(
        args.get("offset", args.get("start_char", 0)),
        0, minimum=0, maximum=10**9,
    )
    limit = _read_document_int(
        args.get("limit", min(args.get("max_chars", _READ_DOCUMENT_DEFAULT_MAX_CHARS), _READ_DOCUMENT_MAX_CHARS)),
        _READ_DOCUMENT_DEFAULT_MAX_CHARS,
        minimum=1,
        maximum=_READ_DOCUMENT_MAX_CHARS,
    )
    if args.get("_max_chars") is not None:
        limit = min(limit, max(1, int(args["_max_chars"])))
    token_budget = max(0, int(args["_max_tokens"] if "_max_tokens" in args else document_output_token_budget()))

    local_data: bytes | None = None
    local_parsing_deferred = False
    try:
        record = file_memory.find_file_memory_record(session_id, filename)
    except Exception as exc:
        return {"error": f"读取文件记录失败：{exc}"}
    if record is None:
        local_native = bool(
            support_doc_types
            and file_memory.document_supports_native(Path(filename).name, support_doc_types)
            and (mode == "native" or (mode == "auto" and not has_text_range and not has_table_args))
        )
        local_parsing_deferred = bool(
            mode == "native" or mode == "table" or (
                mode == "auto" and Path(filename).suffix.lower() in TABLE_EXTENSIONS and not has_offset
            ) or (
                mode == "auto" and not has_text_range
                and support_doc_types
                and file_memory.document_supports_native(Path(filename).name, support_doc_types)
            )
        )
        local_byte_limit = _native_document_byte_limit() if local_native else _READ_DOCUMENT_LOCAL_MAX_BYTES
        if local_native and mode == "auto" and not has_page_range:
            # Keep text fallback available when its cap exceeds the native quota.
            local_byte_limit = max(local_byte_limit, _READ_DOCUMENT_LOCAL_MAX_BYTES)
        record, local_data, local_error = _load_local_document(
            filename, parse_text=not local_parsing_deferred,
            max_bytes=local_byte_limit,
        )
        if record is None:
            try:
                names = [
                    str(item.get("filename") or "未命名")
                    for item in file_memory.list_session_file_records(session_id)
                ]
            except Exception:
                names = []
            hint = f"；当前会话已上传文件：{'、'.join(names)}" if names else "；当前会话没有上传文件"
            return {"error": f"{local_error}；{hint}"}

    display_filename = str(
        record.get("display_filename") or record.get("filename") or filename
    )
    stored_name = str(record.get("stored_name") or "").strip()
    file_ref = stored_name or str(record.get("filename") or filename)
    native_supported = bool(
        support_doc_types
        and file_memory.document_supports_native(display_filename, support_doc_types)
    )

    def load_original_bytes() -> bytes | None:
        if local_data is not None:
            return local_data
        if not stored_name:
            return None
        path = file_memory.resolve_document_path(session_id, stored_name)
        if path is None:
            return None
        try:
            return _read_bytes_with_limit(path, _native_document_byte_limit())
        except (OSError, ValueError):
            return None

    wants_native = mode == "native" or (
        mode == "auto" and not has_text_range and not has_table_args and (native_supported or has_page_range)
    )
    if wants_native:
        if not native_supported:
            if mode == "native":
                return {
                    "error": (
                        f"当前模型未声明支持 {Path(display_filename).suffix or '该'} "
                        "原生文档输入，请使用 mode=text"
                    )
                }
            if has_page_range:
                return {
                    "error": (
                        "PDF 页面选段要求当前模型声明支持 PDF 原生输入；"
                        "否则请使用 offset/limit 文本选段"
                    )
                }
        else:
            if has_page_range:
                if Path(display_filename).suffix.lower() != ".pdf":
                    return {"error": "原生页面选段目前只支持 PDF；其他格式请使用 offset/limit"}
                raw = load_original_bytes()
                if raw is None:
                    return {"error": f"{display_filename} 的原始文件不可用，无法读取指定页面"}
                native_block = file_memory.build_native_pdf_page_part_from_bytes(
                    display_filename,
                    raw,
                    args.get("page_start"),
                    args.get("page_count"),
                    stored_name=stored_name,
                )
            elif local_data is not None:
                native_block = file_memory.build_native_document_part_from_bytes(
                    display_filename, local_data, stored_name=stored_name
                )
            else:
                native_block = file_memory.build_native_document_part(session_id, record)

            if native_block.get("part") is not None:
                result: dict[str, Any] = {
                    "filename": record.get("filename") or filename,
                    "type": record.get("type") or "",
                    "mode": "native",
                    "file_ref": file_ref,
                    "message": (
                        "原生文档将在下一次模型请求中发送一次；"
                        "之后如需查看原文，请再次调用 read_document。"
                    ),
                    "native": {**native_block, "file_ref": file_ref},
                }
                if has_page_range:
                    result.update({
                        "page_start": native_block.get("page_start"),
                        "page_end": native_block.get("page_end"),
                        "total_pages": native_block.get("total_pages"),
                    })
                return result
            if mode == "native" or has_page_range:
                return {"error": native_block.get("error") or "原生文档不可用"}
            native_fallback_note = str(native_block.get("error") or "原生文档不可用")
    else:
        native_fallback_note = ""

    # Table fallback reparses the original, so legacy records with lost headers recover too.
    if token_budget < 128:
        return {"error": "当前模型剩余上下文不足以读取文档；请先压缩上下文或减少同轮工具调用"}
    table_note = ""
    extension = Path(display_filename).suffix.lower()
    wants_table = mode == "table" or has_table_args or (
        mode == "auto" and extension in TABLE_EXTENSIONS and not has_offset
    )
    if wants_table:
        if extension not in TABLE_EXTENSIONS:
            if extension == ".docx":
                return {"error": "Word 表格随正文返回为 Markdown；请移除 sheet/header_row/start_row/row_limit，使用 mode=text 或 auto，并按 offset/limit 续读"}
            return {"error": "表格模式仅支持 CSV/TSV/XLS/XLSX；其他文档请用 mode=text 或 native"}
        source_path = (Path(str(record.get("filename") or filename)) if local_data is not None else
                       file_memory.resolve_document_path(session_id, stored_name) if stored_name else None)
        try:
            if source_path is not None:
                raw = local_data if local_data is not None else _read_bytes_with_limit(source_path, _READ_DOCUMENT_LOCAL_MAX_BYTES)
                document = _extract_document_table(source_path, display_filename, raw)
            elif extension in {".csv", ".tsv"} and not record.get("content_truncated"):
                document = parse_table(display_filename, str(record.get("content") or "").encode("utf-8"))
                table_note = "原始文件不可用，按已保存文本重建表格；无法核对历史解析是否已丢失数据"
            else:
                raise FileNotFoundError("原始表格文件不可用，旧解析文本无法可靠恢复表头和列关系")
            def table_int(name, default, minimum, maximum=None):
                value = args.get(name, default)
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum and value > maximum):
                    raise ValueError(f"{name} 必须是 >= {minimum}" + (f" 且 <= {maximum}" if maximum else "") + " 的整数")
                return value
            header_row = table_int("header_row", 1, 0)
            row_limit = table_int("row_limit", 100, 1, 200)
            start_row = table_int("start_row", 1, 1) if "start_row" in args else None
            sheet = args.get("sheet")
            if sheet is not None and not isinstance(sheet, str):
                raise ValueError("sheet 必须是工作表名称字符串")
            result = table_page(document, sheet, header_row=header_row, start_row=start_row,
                                row_limit=row_limit, max_chars=limit)
            result.update(filename=record.get("filename") or filename, type=record.get("type") or extension[1:],
                          mode="table", limit=limit, row_limit=row_limit, source_truncated=False)
            message = f"已读取工作表 {result['sheet']}，数据行 {result['start_row']}-{result['end_row']}；每次重复表头"
            if "next_row" in result:
                message += f"；继续用 start_row={result['next_row']}、sheet={result['sheet']!r}"
            elif "next_sheet" in result:
                message += f"；还有工作表，继续用 sheet={result['next_sheet']!r}（移除 start_row）"
            else:
                message += "；当前读取结束"
            if header_row:
                message += f"；当前以第 {header_row} 行为表头，若无表头请设 header_row=0"
            if table_note:
                message += "；" + table_note
            if native_fallback_note:
                message += "；原生读取不可用，已回退表格文本：" + native_fallback_note
            result["message"] = message
            return _bounded_document_result(
                result, token_budget, render_table=lambda size: table_page(
                    document, sheet, header_row=header_row, start_row=start_row,
                    row_limit=row_limit, max_chars=size))
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            if mode == "table" or has_table_args:
                return {"error": f"表格解析失败：{exc}"}
            table_note = f"表格结构化解析不可用，返回已有文本：{exc}"

    # 原生读取失败后才解析本地文件；截断入库记录按需重解析。
    # 旧 DOCX 记录可能只保存段落，即使未标记截断，也从原始文件恢复表格。
    if local_parsing_deferred and local_data is not None:
        local_path = Path(str(record.get("filename") or filename))
        try:
            record["content"] = _extract_document_text(local_path, local_path.name, local_data)
        except Exception as exc:
            record["parse_failed"] = True
            record["parse_error"] = str(exc)
    content = str(record.get("content") or "")
    source_truncated = bool(record.get("content_truncated"))
    parse_warning = ""
    if source_truncated or (extension == ".docx" and local_data is None):
        try:
            source_path = (
                Path(str(record.get("filename") or filename)) if local_data is not None
                else file_memory.resolve_document_path(session_id, stored_name) if stored_name else None
            )
            if source_path is None:
                raise FileNotFoundError("原始文档不可用，请重新上传文件")
            raw = local_data if local_data is not None else _read_bytes_with_limit(source_path, _READ_DOCUMENT_LOCAL_MAX_BYTES)
            content = _extract_document_text(source_path, display_filename, raw)
            source_truncated = False
            record["parse_failed"] = False
        except Exception as exc:
            parse_warning = f"原始文档重解析失败：{exc}"
            if extension == ".docx":
                parse_warning += "；仅返回已保存文本，旧记录可能缺失表格，不能据此认定没有表格"
            if not content:
                record.update(parse_failed=True, parse_error=parse_warning)

    total = len(content)
    if total == 0:
        empty_hint = "该文件解析文本为空（可能是扫描版 PDF 等无可提取文本）"
        if record.get("parse_failed"):
            empty_hint = (
                "该文件本地文本解析失败"
                f"（{str(record.get('parse_error') or '解析器不可用/文件不支持')}），"
                "无文本可读；若模型支持该类型，可使用 mode=native 读取原始文档"
            )
        message = empty_hint
        if source_truncated:
            message += "；解析文本在入库时已截断，原始文件未能重解析"
        if native_fallback_note:
            message += f"；原生文档不可用：{native_fallback_note}"
        if table_note:
            message += "；" + table_note
        if parse_warning:
            message += "；" + parse_warning
        result = {
            "filename": record.get("filename") or filename,
            "type": record.get("type") or "",
            "mode": "text",
            "total_chars": 0,
            "offset": 0,
            "limit": limit,
            "start_char": 0,
            "end_char": 0,
            "content": "",
            "has_more": False,
            "source_truncated": source_truncated,
            "message": message,
        }
        if parse_warning:
            result["parse_warning"] = parse_warning
        return _bounded_document_result(result, token_budget)

    if offset >= total:
        return {
            "error": (
                f"offset={offset} 已超出当前可读取文本长度 {total}；"
                "请检查偏移量或重新从 offset=0 读取"
            )
        }

    chunk = content[offset:offset + limit]
    end = offset + len(chunk)
    has_more = end < total
    message = (
        f"已返回字符区间 [{offset}, {end})（共 {total} 字）；"
        f"如需继续，用 offset={end} 再次调用"
        if has_more
        else f"已返回字符区间 [{offset}, {end})（当前可读取文本结束，共 {total} 字）"
    )
    if source_truncated:
        message += "；原始文件未能重解析，解析文本在入库时已截断"
    if native_fallback_note:
        message += f"；原生文档不可用，已回退到解析文本：{native_fallback_note}"
    if table_note:
        message += "；" + table_note
    if parse_warning:
        message += "；" + parse_warning
    result = {
        "filename": record.get("filename") or filename,
        "type": record.get("type") or "",
        "mode": "text",
        "total_chars": total,
        "offset": offset,
        "limit": limit,
        "start_char": offset,
        "end_char": end,
        "content": chunk,
        "has_more": has_more,
        "source_truncated": source_truncated,
        "message": message,
    }
    if has_more:
        result["next_offset"] = end
        result["next_start_char"] = end
    if source_truncated:
        result["content_truncated"] = True
        result["content_total_chars"] = record.get("content_total_chars")
    if parse_warning:
        result["parse_warning"] = parse_warning
    return _bounded_document_result(result, token_budget)


# ------------------- read_media：读取图片/音频/视频（统一入口） -------------------
# 服务端本地执行的内置媒体读取工具：把媒体来源解析为 base64 回传给多模态
# 模型，支持三类引用：
# - media://xxx（会话媒体，含用户上传与 read_media 自行注册的来源）；
# - 本地路径（相对路径基于会话工作目录，worker 进程已 os.chdir）；
# - http(s) 网络直链（下载后按魔数嗅探校验）。
# 工具由用户提供，无内容安全边界（本地/网络来源不做白名单拦截）；
# 用户授权确认逻辑后续接入：register_media_source 的 source_type 字段
# 已预留来源审计信息，届时在 load_any_media_model_part 入口挂确认钩子。
# 规模规则沿用上传链路：单次最多 5 个；图片超过 2MB（或显式传 quality）
# 自动降采样为 JPEG 缩略图（长边 1568，质量按 quality 参数，默认 85）；
# 视觉 API 拒绝的格式自动转换后再注入（静图 gif/bmp/ico/tif/tiff → PNG，
# 动图 gif → H.264 MP4；ffmpeg 定位回退 imageio-ffmpeg 包内置二进制，
# 均缺失时静图 gif 回退首帧 PNG）；大小上限按内容判定：
# 静图类 30MB、动图 gif 与视频类 600MB、音频 20MB
# （网络直链下载转换上限同口径：gif 600MB / 其他 30MB；超限直接拒绝）。

READ_MEDIA_MAX_ITEMS = 5
_READ_MEDIA_QUALITY_MIN = 50
_READ_MEDIA_QUALITY_MAX = 100


def build_read_media_tool_definition() -> dict[str, Any]:
    """动态构建 read_media 工具定义。

    视频最大读取秒数（VIDEO_MAX_READ_SECONDS）每次现读：此前作为模块级
    常量时 f-string 在 import 时求值一次，.env 热重载后工具描述里的数字
    仍是启动时的旧值；改为函数后每轮工具注入时现场取值，与
    video_max_read_seconds() 的延迟读取语义一致（配置热重载即生效）。
    """
    max_seconds = file_memory.video_max_read_seconds()
    return {
        "type": "function",
        "function": {
            "name": READ_MEDIA_NAME,
            "description": (
                "读取图片、音频或视频，供下一次模型请求查看。每轮一次，references 合并最多 5 个来源；"
                "仍受模型媒体配额限制，检查 loaded/skipped。"
                f"本地/会话视频每段最多 {max_seconds} 秒，按 video.read.end 续读；"
                "start_time=end_time 仅探测元数据。网络 URL 不支持区间。旧视频画面会回收，回看需重读。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "references": {
                        "type": "array",
                        "description": (
                            "media:// 引用、本地路径或 http(s) 媒体直链；最多 5 个。"
                        ),
                        "items": {"type": "string", "minLength": 1, "maxLength": 8192},
                        "minItems": 1,
                        "maxItems": READ_MEDIA_MAX_ITEMS,
                    },
                    "quality": {
                        "type": "integer",
                        "minimum": _READ_MEDIA_QUALITY_MIN,
                        "maximum": _READ_MEDIA_QUALITY_MAX,
                        "description": "本地/会话图片质量 50-100，默认 85；网络图片直传时不调整。",
                    },
                    "start_time": {
                        "type": "number",
                        "minimum": 0,
                        "description": (
                            "本地/会话视频或动图开始秒数，默认 0；同批来源共用区间。"
                        ),
                    },
                    "end_time": {
                        "type": "number",
                        "minimum": 0,
                        "description": (
                            "本地/会话视频或动图结束秒数；省略时读取 "
                            f"start_time 起 {max_seconds} 秒；"
                            "start_time == end_time 表示仅探测元数据（不回传视频数据）"
                        ),
                    },
                },
                "required": ["references"],
                "additionalProperties": False,
            },
        },
    }


def _media_injected_key(reference: str, start_time: float | None, end_time: float | None) -> str:
    """read_media 注入去重键：未指定区间=引用原文；指定区间=引用#start-end。

    视频分段续读语义的关键：同一视频引用不同区间是合法的新读取（不命中
    already_injected），相同区间重复读取仍被去重；未指定区间时保持旧键
    （引用原文），兼容图片/音频与既有行为。
    """
    if start_time is None and end_time is None:
        return reference
    s_text = "" if start_time is None else f"{start_time:.3f}"
    e_text = "" if end_time is None else f"{end_time:.3f}"
    return f"{reference}#{s_text}-{e_text}"


def normalize_read_media_args(tool_args: dict[str, Any]) -> tuple[list[str], int | None, str | None, float | None, float | None]:
    """校验并规整 read_media 参数：references 列表与可选 quality / 区间。

    返回 (references, quality, error_reason, start_time, end_time)；
    参数非法时 references 为空列表。
    - references：字符串/单元素自动包列表；每项接受 media:// 引用、本地路径
      或 http(s) 直链（仅做前后空白清理与去重，按出现顺序保留）；
    - quality：50-100 整数，非法时返回 error；
    - start_time / end_time：可选秒数（毫秒精度，视频/动图区间读取），
      非数字或负数返回 error；end < start 返回 error（相等=仅探测元数据）。
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    raw_references = args.get("references")
    if raw_references is None:
        raw_references = args.get("reference")
    if isinstance(raw_references, str):
        raw_references = [raw_references]
    if not isinstance(raw_references, list):
        return [], None, (
            "references 参数无效：需要媒体来源的字符串数组（media:// 引用 / 本地路径 / http(s) 直链）"
        ), None, None
    references: list[str] = []
    for item in raw_references:
        if not isinstance(item, str):
            return [], None, "references 每项必须是字符串", None, None
        text = item.strip()
        if not text:
            continue
        if text not in references:
            references.append(text)
    quality = args.get("quality")
    if quality is not None:
        if isinstance(quality, bool) or not isinstance(quality, int):
            return [], None, f"quality 参数无效: {quality!r}（应为 50-100 的整数）", None, None
        if quality < _READ_MEDIA_QUALITY_MIN or quality > _READ_MEDIA_QUALITY_MAX:
            return [], None, (
                f"quality 参数超出范围: {quality}（允许 {_READ_MEDIA_QUALITY_MIN}-"
                f"{_READ_MEDIA_QUALITY_MAX}）"
            ), None, None
    times: list[float | None] = []
    for name in ("start_time", "end_time"):
        raw = args.get(name)
        if raw is None:
            times.append(None)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return [], None, f"{name} 参数无效: {raw!r}（应为秒数，支持毫秒精度）", None, None
        value = float(raw)
        if value < 0:
            return [], None, f"{name} 不能为负数: {value}", None, None
        times.append(value)
    start_time, end_time = times
    if start_time is not None and end_time is not None and end_time < start_time:
        return [], None, (
            f"end_time({end_time}) 小于 start_time({start_time})：请保证 "
            f"start_time <= end_time（相等表示仅探测视频元数据）"
        ), None, None
    if not references:
        return [], quality, (
            "references 不能为空：请传入媒体来源（media:// 引用 / 本地文件路径 / http(s) 直链）"
        ), start_time, end_time
    return references, quality, None, start_time, end_time


def retire_prior_video_parts(messages: Any) -> int:
    """回收 messages 中已注入的 _internal 媒体消息里的视频部件（就地占位替换）。

    供应商普遍限制单请求视频数量（实测该限制为 1：第二个视频触发
    "videos in request 2 > 1" 400 错误）。模型分段串读视频时，先前注入的
    切片若继续滞留在上下文，第二次读取一注入就会崩溃；同时多段切片
    base64 滞留也是输入 token 膨胀的主因。

    视频部件一次性消费语义：本函数把所有 _internal 媒体消息中的
    video_url 部件就地替换为文本占位（说明数据已回收 + 回看方式）——
    工具结果文本（参数与成功说明）照常保留在历史里；模型需要回看时对
    同一引用与相同区间重新调用 read_media（切片有磁盘缓存，成本极低）。

    在注入新一批媒体部件**之前**调用：旧视频全部回收，随后追加的新
    消息携带本次视频数据，任何请求中视频部件数恒 ≤1。
    图片（image_url）与音频部件不受影响（供应商允许多图共存）。
    返回回收的部件数。
    """
    retired = 0
    if not isinstance(messages, list):
        return 0
    placeholder = (
        "[此前注入的视频片段数据已回收（供应商限制单请求仅 1 个视频）；"
        "工具结果文本中的参数与读取状态仍可参考，如需回看画面，"
        "请用 read_media 对同一引用与相同区间重新读取]"
    )
    for message in messages:
        if not isinstance(message, dict) or not message.get("_internal"):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "video_url":
                content[index] = {"type": "text", "text": placeholder}
                retired += 1
    return retired


def collect_injected_media_keys(messages: Any) -> set[str]:
    """只把当前消息中仍携带的图片/音频计入去重；视频回收后允许重读。"""
    keys: set[str] = set()
    if not isinstance(messages, list):
        return keys
    for message in messages:
        if not isinstance(message, dict) or not message.get("_internal"):
            continue
        parts = message.get("content")
        media_keys = message.get("_media_injected_keys")
        if not isinstance(parts, list) or not isinstance(media_keys, list):
            continue
        for key, part in zip(media_keys, parts[1:]):
            if (
                isinstance(key, str) and key
                and isinstance(part, dict)
                and part.get("type") in {"image_url", "input_audio"}
            ):
                keys.add(key)
    return keys


def cap_video_parts_in_batch(
    parts: list[dict[str, Any]],
    reference_hint: str | None = None,
    read_hint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """单次注入的并行视频部件配额（最多 1 个）：超出的转为占位文本。

    一次 read_media 调用同时读取多个视频区间时，全量注入会在**本次请求**
    就触发供应商单请求视频上限——构建注入消息阶段即裁剪：仅保留第一个
    video_url 部件，后续视频部件替换为带读取参数与回看说明的占位文本。
    reference_hint / read_hint：占位文案携带的引用与实际读取区间提示
    （来自对应 loaded 项，便于模型按序补读）。
    """
    capped: list[dict[str, Any]] = []
    video_taken = False
    for part in parts:
        if (
            isinstance(part, dict)
            and part.get("type") == "video_url"
        ):
            if video_taken:
                range_text = ""
                if isinstance(read_hint, dict) and read_hint.get("start") is not None:
                    range_text = (
                        f"（{read_hint.get('start')}-{read_hint.get('end')}s，"
                        f"工具结果已含读取元信息）"
                    )
                capped.append({
                    "type": "text",
                    "text": (
                        f"[视频片段{range_text}读取成功，但视频数据未随本请求发送"
                        f"（单请求仅 1 个视频）：请先基于当前内容继续，"
                        f"随后单独调用 read_media 读取该区间查看]"
                    ),
                })
            else:
                video_taken = True
                capped.append(part)
            continue
        capped.append(part)
    return capped


def _media_filename_from_reference(source: str, data: bytes) -> str:
    """从媒体来源推导入库文件名；扩展名缺失/可疑时按魔数嗅探校准。"""
    # 提取文件名：URL 取路径末段（去掉 query）；本地路径取完整文件名
    name = ""
    if source.startswith("http://") or source.startswith("https://"):

        path_part = urlsplit(source).path
        name = Path(path_part).name
    else:
        name = Path(source).name
    name = name.strip()
    suffix = Path(name).suffix.lower() if name else ""
    known = suffix and (file_memory.media_kind(f"probe{suffix}") is not None)
    if not known:
        sniffed = file_memory._sniff_media_filename(data)
        if sniffed is not None:
            return sniffed
    if not name:
        return "sniffed_unknown.bin"
    return name


def register_media_source(session_id: str, source: str, source_type: str) -> dict[str, Any]:
    """限量读取媒体来源并注册进会话媒体库，返回注册信息。

    - source：本地路径（相对路径基于会话工作目录）或 http(s) 直链；
    - source_type："network" / "local"，写入返回值供后续授权审计使用；
    - 按魔数嗅探校准扩展名，按媒体类型限量读取后入库
      （与用户上传完全同规则：类型校验、大小上限、ICO/TIFF 自动转 PNG）；
    - 返回 {ok, reference(media://), source_type, kind, mime, size, stored_name}
      或 {ok=False, error}。

    由任务权限上下文检查实际读盘/下载目标，确认前不读取内容。
    """
    source_text = (source or "").strip()
    try:
        if source_text.startswith("http://") or source_text.startswith("https://"):
            request = urllib.request.Request(
                source_text,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with permission_urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                if not (200 <= int(status) < 300):
                    return {"ok": False, "error": f"网络媒体下载失败: {source_text}"}
                head = response.read(32)
                filename = _media_filename_from_reference(source_text, head)
                kind = file_memory.media_kind(filename)
                if kind is None:
                    raise ValueError(f"不支持的媒体类型: {filename}")
                large_media = kind == "video" or Path(filename).suffix.lower() == ".gif"
                limit = (file_memory.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES
                         if large_media else file_memory.media_size_limit(kind))
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
                    spool.write(head)
                    total = len(head)
                    while True:
                        chunk = response.read(min(1024 * 1024, limit - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise ValueError(f"文件大小超过限制（最大 {limit // (1024 * 1024)}MB）")
                        spool.write(chunk)
                    spool.seek(0)
                    saved = (file_memory.save_session_media_stream(session_id, filename, spool)
                             if large_media else file_memory.register_media_source(
                                 session_id, filename, spool.read(), source_type))
        else:
            # 本地路径：相对路径按会话工作目录解析（worker 已 os.chdir）
            local_path = Path(source_text)
            guard_path(local_path)
            if not local_path.is_file():
                return {"ok": False, "error": f"本地媒体文件不存在: {source_text}"}
            with local_path.open("rb") as handle:
                filename = _media_filename_from_reference(source_text, handle.read(32))
                kind = file_memory.media_kind(filename)
                if kind is None:
                    raise ValueError(f"不支持的媒体类型: {filename}")
                large_media = kind == "video" or Path(filename).suffix.lower() == ".gif"
                handle.seek(0)
                saved = (file_memory.save_session_media_stream(session_id, filename, handle)
                         if large_media else file_memory.register_media_source(
                             session_id, filename,
                             _read_bytes_with_limit(local_path, file_memory.media_size_limit(kind)),
                             source_type))
        saved.setdefault("source_type", source_type)
    except (ValueError, OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "reference": saved["media_ref"],
        "source_type": source_type,
        "kind": saved["kind"],
        "mime": saved["mime"],
        "size": saved["size"],
        "stored_name": saved["stored_name"],
    }


def _fetch_url_media_bytes(
    source: str,
    max_bytes: int | None = None,
) -> bytes:
    """下载 http(s) 媒体字节（转换前的临时获取，不落盘入库）。

    上限默认按文件名后缀分流（对齐发送/读取口径，gif 动图走视频档）：
    - .gif：600MB（MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES，转码前先缩放）；
    - 其他：30MB（MEDIA_SEND_IMAGE_LIMIT_BYTES，静图本就走缩略图链路）；
    显式传 max_bytes 时按传入值。超出即拒绝（ValueError），仅 http/https；
    返回原始字节。失败抛出异常由调用方转为 skipped 说明。
    """
    import urllib.request

    if max_bytes is None:
        max_bytes = _network_media_fetch_limit_bytes(source)
    req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
    with permission_urlopen(req, timeout=60) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"网络媒体超过下载上限（{max_bytes // (1024 * 1024)}MB）")
    return data


def _network_media_fetch_limit_bytes(source_or_name: str) -> int:
    """网络直链转换下载上限：gif 动图按视频档 600MB，其余按静图档 30MB。

    未知扩展名/无扩展名按静图档保守处理（魔数嗅探在后端落盘前仍会进行）。
    """
    name = Path(urlsplit(source_or_name).path).name if "://" in source_or_name else source_or_name
    if Path(name).suffix.lower() == ".gif":
        return file_memory.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES
    return file_memory.MEDIA_SEND_IMAGE_LIMIT_BYTES


def load_any_media_model_part(
    session_id: str,
    reference: str,
    quality: int | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    api_type: str | None = None,
) -> dict[str, Any] | None:
    """本地/网络/会话统一媒体加载入口（read_media 数据注入使用）。

    引用即真实位置，不再复制落盘：
    - media:// 引用 → 会话媒体库加载（load_session_media_model_part）；
    - 本地路径 → 直接按真实路径读取文件构建部件；文件不存在时返回
      {"error": "未找到，可能已删除", ...}（调用方转为模型可见提示）；
    - http(s) 直链 → 不再下载/入库，直接把 URL 作为多模态部件透传给
      上游模型（由模型供应商自行拉取；拉取失败再考虑恢复下载链路）；
      但视觉 API 通常拒绝的格式（gif/bmp/ico/tiff 等）供应商同样会拒绝，
      此时临时下载字节走统一转换内核（静图→PNG / 动图→mp4）后再注入；
      网络视频不支持区间读取（start_time/end_time 仅对 media:// 与本地
      路径生效，透传给上游由供应商自行拉取）。
    start_time / end_time：视频与动图 gif 的区间读取秒数（帧吸附、单次
    上限 VIDEO_MAX_READ_SECONDS；start==end 仅探测元数据），透传给加载内核。
    成功返回值形态与 load_session_media_model_part 一致，并统一补充来源
    审计信息：source_type（"session" / "network" / "local"）与 source
    （本次调用的原始 reference）；转换成功时附带 converted 元信息。
    """
    source = (reference or "").strip()
    if source.startswith(file_memory.MEDIA_URL_SCHEME):
        info = file_memory.load_session_media_model_part(
            session_id, source, quality=quality,
            start_time=start_time, end_time=end_time,
            **({"api_type": api_type} if api_type in {"messages", "responses"} else {}),
        )
        if info is None:
            return None
        return {**info, "source_type": "session", "source": source}
    if source.startswith("http://") or source.startswith("https://"):
        if start_time is not None or end_time is not None:
            return {"error": "网络 URL 不支持时间区间；请下载到本地后读取", "source_type": "network", "source": source}
        # URL 直传：按扩展名推断 kind/mime；未知扩展名按图片处理。
        name = Path(urlsplit(source).path).name or "remote_media"
        kind = file_memory.media_kind(name) or "image"
        mime = file_memory.media_mime_type(name) if file_memory.media_kind(name) else "image/jpeg"
        image_only = api_type in {"messages", "responses"}
        if image_only and kind != "image":
            return {"error": "当前 API 不支持音频或视频，仅支持图片输入", "source_type": "network", "source": source}
        part: dict[str, Any]
        if kind == "image":
            part = {"type": "image_url", "image_url": {"url": source}}
        elif kind == "video":
            part = {"type": "video_url", "video_url": {"url": source}}
        else:
            part = {
                "type": "input_audio",
                "input_audio": {"data": source, "format": Path(name).suffix.lower().lstrip(".")},
            }
        # 视觉 API 拒绝的图片格式（gif/bmp/ico/tif/tiff）：URL 直传同样会被
        # 供应商拒绝——临时下载字节走统一转换内核后按转换结果注入；
        # 转换失败（下载失败/ffmpeg 缺失/损坏文件）回退 URL 直传原行为
        suffix = Path(name).suffix.lower()
        if kind == "image" and (
            (suffix == ".gif" and not image_only) or suffix in file_memory._VISION_UNSAFE_IMAGE_EXTENSIONS
        ):
            try:
                data = _fetch_url_media_bytes(source, max_bytes=file_memory.MEDIA_SEND_IMAGE_LIMIT_BYTES if image_only else _network_media_fetch_limit_bytes(name))
                sniffed = file_memory._sniff_media_filename(data)
                probe_name = (sniffed or name) if (suffix == ".gif" or not sniffed) else name
                tmp_path = file_memory._transcode_cache_dir("network_media") / f"dl_{uuid.uuid4().hex[:8]}_{Path(probe_name).name}"
                tmp_path.write_bytes(data)
                try:
                    if image_only:
                        info = file_memory._messages_image_model_part(tmp_path, quality)
                        return {**info, "source_type": "network", "source": source}
                    converted_result = file_memory._load_converted_media_base64(
                        file_memory._transcode_cache_dir("network_media"), tmp_path
                    )
                finally:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                if converted_result is not None:
                    conv_mime, conv_b64, conv_meta = converted_result
                    part = (
                        {"type": "video_url", "video_url": {"url": file_memory._media_data_url(conv_mime, conv_b64)}}
                        if conv_meta["converted_kind"] == "video"
                        else {"type": "image_url", "image_url": {"url": file_memory._media_data_url(conv_mime, conv_b64)}}
                    )
                    return {
                        "part": part,
                        "kind": conv_meta["converted_kind"],
                        "mime": conv_mime,
                        "size": None,
                        "downsampled": False,
                        "quality": None,
                        "stored_name": None,
                        "filename": name,
                        "converted": conv_meta,
                        "source_type": "network",
                        "source": source,
                    }
            except Exception as exc:
                if image_only:
                    return {"error": "图片格式转换失败：" + str(exc), "source_type": "network", "source": source}
                pass  # 转换链路任何失败都回退 URL 直传
        return {
            "part": part,
            "kind": kind,
            "mime": mime,
            "size": None,
            "downsampled": False,
            "quality": None,
            "stored_name": None,
            "filename": name,
            "source_type": "network",
            "source": source,
            **({"converted": {"converted_kind": "image", "first_frame_only": True}}
               if image_only and suffix == ".gif" else {}),
        }
    # 本地路径：直接读取真实位置（相对路径基于会话工作目录，worker 已 os.chdir）
    local_path = Path(source)
    guard_path(local_path)
    if not local_path.is_file():
        return {"error": f"未找到，可能已删除: {source}", "source_type": "local", "source": source}
    info = file_memory._media_model_part_from_path(
        local_path, quality, start_time=start_time, end_time=end_time,
        **({"api_type": api_type} if api_type in {"messages", "responses"} else {}),
    )
    if info is None:
        return {"error": f"读取失败: {source}", "source_type": "local", "source": source}
    if isinstance(info, dict) and info.get("error"):
        # 大小门控等加载内核给出的错误说明：补来源审计后透传（调用方转为
        # 模型可见的拒绝占位，绝不回退注入原始大文件）
        return {**info, "source_type": "local", "source": source}
    # 沿途保留来源审计信息（本地直读不落盘，加载内核不携带）
    info["source_type"] = "local"
    info["source"] = source
    return info


def collect_media_references(messages: Any) -> list[str]:
    """按出现顺序收集消息列表里 user 消息部件中的 media:// 引用（去重）。

    兼容旧接线的边界数据源（execute_read_media 的 available_references 参数）。
    当前策略：工具由用户提供、无安全边界——该集合仅作为 media:// 引用的
    常规过滤（非拦截）：execute_read_media 收到空集合时不做任何过滤。
    解析成功与否都算"出现过"——解析失败的引用仍保留 media:// 原文，同样收集。
    """
    collected: list[str] = []
    seen: set[str] = set()
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            for key, value in part.items():
                # URL 类部件：image_url / video_url 等以 _url 结尾的键
                if str(key).endswith("_url") and isinstance(value, dict):
                    url = value.get("url")
                    if (
                        isinstance(url, str)
                        and url.startswith("media://")
                        and url not in seen
                    ):
                        seen.add(url)
                        collected.append(url)
                # 音频部件：input_audio.data 携带 media:// 引用（解析后为纯 base64）
                if key == "input_audio" and isinstance(value, dict):
                    data = value.get("data")
                    if (
                        isinstance(data, str)
                        and data.startswith("media://")
                        and data not in seen
                    ):
                        seen.add(data)
                        collected.append(data)
    return collected


def execute_read_media(
    tool_args: dict[str, Any],
    session_id: str,
    available_references: list[str] | set[str] | tuple[str, ...] | None = None,
    already_injected: set[str] | None = None,
    load_media=None,
    vision_enabled: bool | None = None,
    media_limits: dict | None = None,
    api_type: str | None = None,
    loaded_parts: dict | None = None,
) -> dict[str, Any]:
    """内置 read_media 实现：把媒体来源解析为可注入的数据（统一入口）。

    - references 支持 media:// 引用 / 本地路径 / http(s) 直链，可混用；
    - start_time / end_time：可选区间读取秒数（视频/动图，帧吸附；
      start==end 仅探测元数据）；不指定时视频默认读前 VIDEO_MAX_READ_SECONDS 秒；
    - available_references：兼容旧接线的会话引用白名单（传 None 或空集合
      即不做边界过滤——当前策略：工具由用户提供，无安全边界）；传入集合
      时 media:// 引用仍按旧语义过滤，非 media:// 来源不受影响；
    - already_injected：本轮已注入过的引用（重复读取直接跳过）；
    - load_media：媒体加载函数（默认 load_any_media_model_part），便于测试
      注入桩；签名 (session_id, reference, quality=…, start_time=…,
      end_time=…) → info|None；
    - vision_enabled=False：当前模型不支持视觉时直接拒绝执行（兜底拦截，
      正常情况下工厂层在注入阶段已按 vision 过滤，不会把本工具给到模型）。

    返回结构：
    - ok 为 True 时 loaded/injected_references 为成功读取的媒体元信息；
      数据本体（base64 部件）不进入本返回值——工具结果文本会原样落盘 JSONL，
      且可能被超大结果逻辑截断，媒体数据由调用方按 loaded 项的
      reference+quality+start_time+end_time 经 load_any_media_model_part
      以 user 多模态部件注入（仅内存）；视频/动图附带 video 元信息块；
    - 每个拒绝项给出 reason（已注入/超限/参数错误/下载或读取失败）。
    """
    references, quality, arg_error, start_time, end_time = normalize_read_media_args(tool_args)
    if arg_error:
        return {"ok": False, "error": arg_error, "loaded": [], "skipped": [], "injected_references": []}
    # 模型不支持视觉：拒绝执行并说明原因（切回支持视觉的模型后可正常使用）
    if vision_enabled is False:
        return {
            "ok": False,
            "error": "当前模型不支持视觉（vision=false），read_media 工具不可用；请切换支持视觉的模型后重试",
            "loaded": [],
            "skipped": [],
            "injected_references": [],
        }

    available = {str(item) for item in (available_references or [])}
    injected = {str(item) for item in (already_injected or set())}
    loader = load_media or load_any_media_model_part
    if load_media is None and api_type in {"messages", "responses"}:
        from functools import partial
        loader = partial(loader, api_type=api_type)
    allowed, skipped = [], []
    for reference in references:
        # 本地/网络来源不做白名单拦截；media:// 引用在传入了边界集合时按旧语义过滤
        if (
            available
            and reference.startswith(file_memory.MEDIA_URL_SCHEME)
            and reference not in available
        ):
            skipped.append({"reference": reference, "reason": "not_in_current_task"})
            continue
        # 去重键带区间：同视频引用不同区间是新的读取（分段续读），相同区间才去重
        injected_key = _media_injected_key(reference, start_time, end_time)
        if injected_key in injected:
            skipped.append({"reference": reference, "reason": "already_injected"})
            continue
        allowed.append(reference)
    if len(allowed) > READ_MEDIA_MAX_ITEMS:
        for reference in allowed[READ_MEDIA_MAX_ITEMS:]:
            skipped.append({"reference": reference, "reason": "limit_5_per_round"})
        allowed = allowed[:READ_MEDIA_MAX_ITEMS]
    loaded_items: list[dict[str, Any]] = []
    media_limits = dict(media_limits or {})
    media_limits["video"] = min(media_limits.get("video", 1), 1)
    kind_counts: dict[str, int] = {}
    for reference in allowed:
        check_tool_control()
        probe_only = start_time is not None and end_time is not None and start_time == end_time
        source_name = Path(urlsplit(reference).path).name if "://" in reference else reference
        inferred_kind = file_memory.media_kind(source_name)
        if not probe_only and inferred_kind and (
            (inferred_kind in media_limits and kind_counts.get(inferred_kind, 0) >= media_limits[inferred_kind])
            or ("total" in media_limits and sum(kind_counts.values()) >= media_limits["total"])
        ):
            skipped.append({"reference": reference, "reason": "provider_media_limit",
                            "message": "当前请求媒体配额已满或不支持此类型；请分轮读取或更换模型"})
            continue
        # 每个引用的去重键在注入时逐项计算（勿复用 allowed 构建循环的旧变量）
        injected_key = _media_injected_key(reference, start_time, end_time)
        try:
            info = loader(
                session_id, reference, quality=quality,
                start_time=start_time, end_time=end_time,
            )
        except file_memory.MediaSendTooLargeError as gate_error:
            # 超过发送/读取上限：明确拒绝（绝不回退注入原始大文件）
            skipped.append({"reference": reference, "reason": str(gate_error)})
            continue
        except ToolCancelledError:
            raise
        except Exception as exc:
            skipped.append({"reference": reference, "reason": f"读取失败：{exc}"})
            continue
        if info is None:
            # media:// 引用解析失败 = 会话媒体库中不存在（引用无效或文件已删除）
            reason = (
                f"未找到，可能已删除: {reference}"
                if reference.startswith(file_memory.MEDIA_URL_SCHEME)
                else "load_failed"
            )
            skipped.append({"reference": reference, "reason": reason})
            continue
        if isinstance(info, dict) and info.get("error"):
            # 加载器带错误说明（如本地文件"未找到，可能已删除"）：作为拒绝项
            # 透传给模型，不计入成功读取
            skipped.append({"reference": reference, "reason": info["error"]})
            continue
        kind = info.get("kind")
        metadata_only = info.get("part", {}).get("type") == "text" and (info.get("video") or {}).get("probe")
        kind_limit = media_limits.get(kind)
        total_limit = media_limits.get("total")
        if not metadata_only and ((kind_limit is not None and kind_counts.get(kind, 0) >= kind_limit) or (
            total_limit is not None and sum(kind_counts.values()) >= total_limit
        )):
            skipped.append({"reference": reference, "reason": "provider_media_limit",
                            "message": f"当前供应商限制 {kind}≤{kind_limit}，总媒体≤{total_limit}；未注入该媒体。数量为 1 时请下一轮单独读取，数量为 0 时请更换模型。"})
            continue
        if not metadata_only:
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if loaded_parts is not None:
            loaded_parts[injected_key] = info
        loaded_items.append({
            "reference": reference,
            "injected_key": _media_injected_key(reference, start_time, end_time),
            "source_type": info.get("source_type"),
            "kind": info.get("kind"),
            "mime": info.get("mime"),
            "size": info.get("size"),
            "downsampled": info.get("downsampled"),
            "quality": info.get("quality"),
            "start_time": start_time,
            "end_time": end_time,
            "converted": info.get("converted"),
            "video": info.get("video"),
        })
    loaded = bool(loaded_items)
    return {
        "ok": loaded,
        "message": (
            f"已准备 {len(loaded_items)} 个媒体，将提供给下一次模型请求；请检查 skipped"
            if loaded
            else "没有可读取的媒体（引用无效/重复或全部被跳过）"
        ),
        "loaded": loaded_items,
        "skipped": skipped,
        # 去重键口径：未指定区间=引用原文；指定区间=引用#start-end
        # （同视频不同区间是新的读取，不命中 already_injected）
        "injected_references": [item["injected_key"] for item in loaded_items],
    }


# ------------------- run_command：终端命令执行（多 shell） -------------------
# 由 mcp_server/sys_tools_server.py 的 run_command 迁移而来（服务端本地执行，
# 语义完全对齐：多 shell 解析、超时杀进程树、超长输出截断并落盘、gbk/utf-8
# 编码自适应、后台分离模式、cmd 家族健壮性补丁——内联代码（多行或含 %）改写
# 为临时脚本 /
# 管道过滤器探测替换或本地兜底 / 分号与 Unix 命令语法提示）。
# 迁移差异：
# - 本实现为同步阻塞函数：execute_run_command 由调用方经 asyncio.to_thread
#   放到工作线程执行（与 MCP 工具的线程池语义一致，避免卡死事件循环）；
# - 解码辅助改名为 _decode_command_bytes_used / _decode_command_bytes，避免与
#   本模块文件工具使用的简化版 _decode_bytes_used 混淆；
# - 复用本模块已有的 _resolve_dir_path / _display_path。
# 旧 MCP 文件中的同名实现已移除；当前内置实现为唯一维护点。

# 前台 stdout 截断上限（防止超大输出撑爆模型上下文；超限时完整输出落盘）
_RUN_COMMAND_MAX_CHARS = 12000

def _decode_command_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """显式编码/BOM 优先；逐行严格 UTF-8 后回退系统代码页，支持混合输出。"""
    if encoding or unicode_bom(data):
        return decode_text(data, encoding, candidates)
    # Windows 的同一次命令可能同时输出 Python/Node 的 UTF-8 与 cmd/原生程序的
    # 本地代码页。按整段选择一个编码会把另一段变成乱码，甚至在宽松兜底中产生 �。
    # 逐行先试 UTF-8 严格解码，再回退到系统代码页；纯 ASCII 行任意候选等价。
    if len(data.splitlines(keepends=True)) > 1:
        decoded_lines = []
        used = set()
        for line in data.splitlines(keepends=True):
            if line.isascii():
                decoded_lines.append(line.decode("ascii"))
                continue
            line_candidates = ("utf-8",) + tuple(c for c in candidates if c != "utf-8")
            decoded, codec = _decode_command_bytes_used(line, candidates=line_candidates)
            decoded_lines.append(decoded)
            used.add(codec)
        if len(used) > 1:
            return "".join(decoded_lines), "mixed"
        if used:
            return "".join(decoded_lines), used.pop()
    return decode_text(data, candidates=candidates)


def _decode_command_bytes(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> str:
    """同 _decode_command_bytes_used，但只返回文本。"""
    return _decode_command_bytes_used(data, encoding, candidates)[0]


# Windows 控制台代码页 → Python 编解码器名
_CONSOLE_CP_TO_CODEC = {
    65001: "utf-8",    # UTF-8（开启"Beta: 使用 Unicode UTF-8 提供全球语言支持"后）
    936: "gbk",        # 简体中文（CP936/GBK）
    950: "big5",       # 繁体中文
    932: "shift_jis",  # 日语
    949: "cp949",      # 韩语
    1252: "cp1252",    # 西文 ANSI
    850: "cp850",      # 西文 OEM
    437: "cp437",      # 美式 OEM
}

_windows_console_cp_cache = None


def _windows_console_cp() -> int:
    """取控制台输出代码页（无控制台时返回新控制台的默认值，失败返回 0）。结果缓存。"""
    global _windows_console_cp_cache
    if _windows_console_cp_cache is None:
        value = 0
        try:
            import ctypes
            value = int(ctypes.windll.kernel32.GetConsoleOutputCP())
        except Exception:
            value = 0
        _windows_console_cp_cache = value
    return _windows_console_cp_cache


def _command_output_candidates() -> tuple:
    """终端命令输出的候选编码：优先匹配系统实际代码页，再回退常见候选。

    cmd 与 PowerShell 5.1 把输出重定向到管道时按控制台输出代码页编码：
    - 中文 Windows 默认 GBK(936)，优先尝试 gbk；
    - 若系统开启了"Beta: 使用 Unicode UTF-8"，代码页为 65001，必须优先 utf-8，
      否则中文会被误判成 GBK 乱码（"涓枃"类乱码的根源）。
    PowerShell 子会话统一 UTF-8 输出；原生命令仍可能使用系统代码页。
    """
    ordered = []

    def _push(name):
        if name and name not in ordered:
            ordered.append(name)

    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            _push(_CONSOLE_CP_TO_CODEC.get(_windows_console_cp()))
            _push(_CONSOLE_CP_TO_CODEC.get(int(kernel32.GetACP())))    # ANSI 代码页
            _push(_CONSOLE_CP_TO_CODEC.get(int(kernel32.GetOEMCP())))  # OEM 代码页
        except Exception:
            pass
    _push("utf-8")
    _push("gbk")
    return tuple(ordered) or ("utf-8", "gbk")


_child_env_cache = None


# Windows 启动进程时动态合成的"内置"环境变量（注册表不落盘）：
# SystemRoot/SystemDrive/ProgramFiles/ProgramData/PUBLIC/SESSIONNAME 等。
# 注册表枚举永远拿不到它们，必须用系统 API（或常规默认值）求解，
# 否则 %VAR% 引用（如 ComSpec=%SystemRoot%\system32\cmd.exe）无法展开。
def _builtin_windows_env() -> dict:
    """求解 Windows 内置环境变量（仅用于补全与展开引用，不覆盖真实值）。"""
    home = Path(os.path.expanduser("~"))
    builtin = {
        "SystemRoot": r"C:\WINDOWS", "windir": r"C:\WINDOWS", "SystemDrive": "C:",
        "ComSpec": r"C:\WINDOWS\system32\cmd.exe",
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "ProgramW6432": r"C:\Program Files",
        "CommonProgramFiles": r"C:\Program Files\Common Files",
        "CommonProgramFiles(x86)": r"C:\Program Files (x86)\Common Files",
        "CommonProgramW6432": r"C:\Program Files\Common Files",
        "ProgramData": r"C:\ProgramData",
        "ALLUSERSPROFILE": r"C:\ProgramData",
        "PUBLIC": str(home.parent / "Public"),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(home / "AppData" / "Local" / "Temp"),
        "TMP": str(home / "AppData" / "Local" / "Temp"),
        # 本服务与命令子进程均在交互桌面会话内运行
        "SESSIONNAME": "Console",
    }
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32
        buf = ctypes.create_unicode_buffer(260)
        if kernel32.GetWindowsDirectoryW(buf, 260) > 0:
            win_dir = buf.value.rstrip("\\")
            builtin["SystemRoot"] = builtin["windir"] = win_dir
            builtin["SystemDrive"] = os.path.splitdrive(win_dir)[0]
            builtin["ComSpec"] = os.path.join(win_dir, "system32", "cmd.exe")
        # SHGetFolderPathW 的 CSIDL 常量（实测返回值：Program Files /
        # Program Files (x86) / Common Files / Common Files (x86) / ProgramData）
        for csidl, name in (
            (0x26, "ProgramFiles"), (0x2A, "ProgramFiles(x86)"),
            (0x2B, "CommonProgramFiles"), (0x2C, "CommonProgramFiles(x86)"),
            (0x23, "ProgramData"),
        ):
            if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                builtin[name] = buf.value
    except Exception:
        pass
    # 64 位进程（本服务为 x64 Python）：W6432 系列不做重定向，取 64 位路径
    builtin["ProgramW6432"] = builtin["ProgramFiles"]
    builtin["CommonProgramW6432"] = builtin["CommonProgramFiles"]
    builtin["ALLUSERSPROFILE"] = builtin["ProgramData"]
    return builtin


def _merged_child_env():
    """构建子进程环境变量（Windows 返回 dict，POSIX 返回 None 表示默认继承）。

    MCP 宿主可能以裁剪过的环境块启动本服务（缺 COMPUTERNAME/USERNAME、
    SystemRoot/ProgramFiles 等内置变量、PATH 不完整等），子进程随之继承
    残缺环境。这里从注册表补全系统级与用户级环境变量后合并：进程内显式
    设置 > 用户级 > 系统级；PATH 三方拼接去重而非覆盖。注册表不落盘的
    内置变量（Windows 启动进程时动态合成）由 _builtin_windows_env 用系统
    API 求解补全，保证 %VAR% 引用可展开。变量名保持注册表/进程内的原始
    大小写（不做大写化）。结果缓存，避免每次调用都读注册表。
    """
    global _child_env_cache
    if os.name != "nt":
        return None
    if _child_env_cache is not None:
        return _child_env_cache

    def _expand(text, mapping):
        # 展开 %VAR% 引用（REG_EXPAND_SZ 类型），支持有限次嵌套
        pattern = re.compile(r"%([^%]+)%")
        prev = None
        while prev != text:
            prev = text
            text = pattern.sub(
                lambda m: mapping.get(m.group(1).upper(), m.group(0)), text)
        return text

    raw_system, raw_user = {}, {}
    try:
        import winreg
        for hive, subkey, bucket in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
             raw_system),
            (winreg.HKEY_CURRENT_USER, "Environment", raw_user),
        ):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    index = 0
                    while True:
                        try:
                            name, value, _vtype = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        index += 1
                        if isinstance(name, str) and isinstance(value, str):
                            bucket[name] = value  # 保持注册表原始大小写
            except OSError:
                continue
    except ImportError:
        pass
    # 补充"易失变量"：COMPUTERNAME 与登录用户信息不落盘在上述键中，
    # 由系统在启动/登录时动态生成，需从专门位置读取（存在则不覆盖已有值）。
    try:
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName") as key:
                value, _vtype = winreg.QueryValueEx(key, "ComputerName")
                if isinstance(value, str) and value.strip():
                    raw_system.setdefault("COMPUTERNAME", value.strip())
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Volatile Environment") as key:
                index = 0
                while True:
                    try:
                        name, value, _vtype = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if isinstance(name, str) and isinstance(value, str) \
                            and not any(k.upper() == name.upper() for k in raw_user):
                        raw_user[name] = value
        except OSError:
            pass
    except ImportError:
        pass
    builtin = _builtin_windows_env()
    # 展开用查找表：系统 < 用户/易失 < 进程 < 内置（内置仅兜底，真实值优先）
    lookup = {}
    for source in (raw_system, raw_user, os.environ, builtin):
        for name, value in source.items():
            if isinstance(name, str) and isinstance(value, str):
                lookup[name.upper()] = value
    for source in (raw_system, raw_user):
        for name in list(source):
            source[name] = _expand(source[name], lookup)

    def _get_ci(d, wanted):
        hit = next((k for k in d if k.upper() == wanted.upper()), None)
        return d[hit] if hit is not None else ""

    # 合并：键保持最高优先级来源的原始大小写（进程 > 用户 > 系统）
    merged = {}
    for source in (raw_system, raw_user, os.environ):
        for name, value in source.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            if name.upper() == "PATH":
                continue  # PATH 三方拼接，单独处理
            hit = next((k for k in merged if k.upper() == name.upper()), None)
            if hit is not None:
                del merged[hit]
            merged[name] = value
    # 内置变量补全：系统/用户/进程均未提供时才写入（子进程缺它们会导致
    # cmd 找不到 ProgramFiles、PSModulePath 残留 %ProgramFiles% 等问题）
    for name, value in builtin.items():
        if not any(k.upper() == name.upper() for k in merged):
            merged[name] = value
    # PATH 三方拼接去重（注册表 PATH 先展开，再拼进程 PATH）
    seen, deduped = set(), []
    parts = [_expand(_get_ci(raw_system, "Path"), lookup),
             _expand(_get_ci(raw_user, "Path"), lookup),
             os.environ.get("PATH", "")]
    for part in ";".join(p for p in parts if p).split(";"):
        key = part.rstrip("\\").lower()
        if part and key not in seen:
            seen.add(key)
            deduped.append(part)
    merged["PATH"] = ";".join(deduped)
    _child_env_cache = merged
    return merged


def _truncate_text(text: str, limit: int, note: str = "内容过长已截断") -> str:
    """超限时保留头部 2/3 + 尾部 1/3，中间用提示行衔接。"""
    if len(text) <= limit:
        return text
    head = int(limit * 2 / 3)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{note}，中间约 {omitted} 字符已省略]...\n{text[-tail:]}"


_SHELL_ALIASES = {
    "auto": "auto", "": "auto",
    "cmd": "cmd", "cmd.exe": "cmd", "batch": "cmd",
    "powershell": "powershell", "powershell.exe": "powershell", "ps": "powershell", "ps1": "powershell",
    "pwsh": "pwsh", "pwsh.exe": "pwsh", "powershell7": "pwsh",
    "bash": "bash", "sh": "sh", "zsh": "zsh", "dash": "sh",
}


def _resolve_shell(shell: str, is_nt: bool) -> tuple:
    """把 shell 名称解析为 (exe, shell 标识, 额外 argv 前缀)。

    - auto: Windows → 可用的 PowerShell，否则 cmd.exe；非 Windows → sh；
    - 返回 exe 为绝对路径时走 Popen 列表形式，否则仅作为提示由 shell=True 使用。
    - 找不到可执行文件时抛 ValueError。
    """
    key = str(shell or "auto").strip().lower()
    kind = _SHELL_ALIASES.get(key)
    if kind is None:
        raise ValueError(
            f"shell 仅支持 auto/cmd/powershell/pwsh/bash/sh/zsh，当前为 {shell!r}")
    if kind == "auto":
        if is_nt:
            powershell = shutil.which("powershell")
            if powershell:
                return powershell, "powershell", ["-NoProfile", "-NonInteractive", "-Command"]
            return os.environ.get("COMSPEC") or "cmd.exe", "cmd", []
        return shutil.which("sh") or "/bin/sh", "sh", ["-c"]
    if kind == "cmd":
        if not is_nt:
            raise ValueError("shell=cmd 仅在 Windows 上可用；Linux/macOS 请使用 bash/sh/zsh")
        return os.environ.get("COMSPEC") or "cmd.exe", "cmd", []
    if kind == "powershell":
        if not is_nt:
            raise ValueError("shell=powershell 仅在 Windows 上可用（非 Windows 请使用 pwsh/bash/sh）")
        exe = shutil.which("powershell") or "powershell.exe"
        return exe, "powershell", ["-NoProfile", "-NonInteractive", "-Command"]
    if kind == "pwsh":
        exe = shutil.which("pwsh") or ("pwsh.exe" if is_nt else None)
        if not exe or not shutil.which(exe) and not Path(exe).exists():
            raise ValueError("未找到 PowerShell 7 (pwsh)；可安装 https://aka.ms/powershell 或改用 shell=powershell/bash")
        return exe, "pwsh", ["-NoProfile", "-NonInteractive", "-Command"]
    # bash / sh / zsh
    exe = shutil.which(kind)
    if not exe:
        exe = f"/bin/{kind}" if Path(f"/bin/{kind}").exists() else None
    if not exe:
        hint = "Windows 上可通过 Git for Windows / WSL 获取 bash" if is_nt else f"系统未安装 {kind}"
        raise ValueError(f"未找到 {kind} 可执行文件（{hint}）")
    if is_nt:
        # Windows 侧的 bash（Git Bash/WSL 登录 shell）：-c 形式最稳，避免 MSYS 路径转换干扰
        return exe, kind, ["-c"]
    return exe, kind, ["-c"]


_BG_LOG_DIR = Path.home() / ".mcp_bg_logs"
# 后台日志文件名序号：同一进程同一秒内多次调用时保证文件名唯一
_BG_NAME_SEQ = itertools.count(1)


# ==================== run_command 的 cmd 家族健壮性补丁 ====================
# 以下三处均为实测复现的真实坑（Windows + cmd 家族 shell）：
#
# 1) 多行内联代码：命令写入 .run.cmd 后由 cmd 按行解释，`python -c "第一行`
#    之后的行被 cmd 当成独立命令执行（实测报 `i was unexpected at this time.`），
#    且首行代码因引号跨行未闭合被破坏 → 必然失败；
# 2) 管道过滤器：PATH 里的 `tail`/`head` 可能命中"非管道过滤器"实现（实测为
#    宝塔面板 E:\BtSoft\panel\script\tail.EXE）。该实现直接读 stdin 时"看起来能用"，
#    但在 cmd 管道中提前退出且不转发输出，上游写入失败（OSError [Errno 22]
#    Invalid argument）→ 整条管道静默"无输出"；必须用【管道形式】探测才能识别；
# 3) cmd 不支持 `;` 串联：`echo A; echo B` 把 `; echo B` 当普通参数原样输出
#    （exit=0），表现为"命令成功但没执行"；另有用 Unix 命令名（ls/cat/...）
#    在 cmd 下必然失败的情况。
#
# 4) 单行内联代码中的 `%`：cmd 批处理层会把孤立 `%` 直接吞掉（实测
#    `python -c "print('100%')"` 输出 `100`），或把 `%VAR%` 展开成环境变量值，
#    代码在到达子进程前就被破坏（`print('%d' % x)` 会 SyntaxError）。
#
# 补丁策略：① 内联代码（多行，或含 `%` 的单行）改写为临时脚本执行；
# ② 管道过滤器探测 + 替换/本地兜底；
# ③ 失败或可疑时附语法提示（不改变命令本身）。

# 管道过滤器探测结果缓存：key=(工具名, 参数, exe路径) -> 是否可用
_PIPE_FILTER_PROBE_CACHE: dict = {}
# 内联代码改写为脚本时的最大代码长度（防误写超大文件）
_INLINE_SCRIPT_MAX_CHARS = 200000
# cmd 内建（internal）命令：不是磁盘上的可执行文件，不能用"文件是否存在"判断，
# 否则 `del`/`dir`/`copy` 等会被误报为"命令不存在"（实测踩坑）。
_CMD_INTERNAL_COMMANDS = frozenset({
    "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del",
    "dir", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto", "if", "md",
    "mkdir", "mklink", "move", "path", "pause", "popd", "prompt", "pushd", "rd",
    "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "start", "time",
    "title", "type", "ver", "verify", "vol",
})
# 仅含行数参数的 tail/head（如 `tail -20`、`head -n 5`），允许末尾流合并重定向
_SIMPLE_FILTER_RE = re.compile(
    r"^\s*(tail|head)\s+(?:(?:-n)\s*(\d+)|-(\d+))"
    r"(?P<redirections>(?:\s+\d?>&\d+)*)\s*$", re.IGNORECASE)
_POWERSHELL_FILTER_RE = re.compile(
    r"^\s*(?P<tool>tail|head)\s+(?:(?:-n)\s*(?P<n1>\d+)|-(?P<n2>\d+))"
    r"(?P<redirections>(?:\s+\d?>&\d+)*)(?=\s*(?:;|\r?\n|#|$))", re.IGNORECASE)
# 内联代码执行入口：python -c / node -e / node --eval
_INLINE_CODE_RE = re.compile(
    r"(?P<interp>(?:^|[\s&|(])(?:python|python3|py|node)(?:\.exe)?)"
    r"(?:\s+(?:-X\s+[^\s\"']+|-u|-B|-I|-E|-s|-S|-O|-OO))*"
    r"\s+(?P<flag>-c|-e|--eval)\s+(?P<quote>[\"'])",
    re.IGNORECASE)
_INLINE_CODE_INTENT_RE = re.compile(
    r"\b(?:python|python3|py|node)(?:\.exe)?\b[^\r\n]*\s(?:-c|-e|--eval)\s*[\"']",
    re.IGNORECASE,
)
# cmd 下常见的 Unix 命令 → Windows 等价物（仅用于失败后的提示）
_UNIX_COMMAND_EQUIVALENTS = {
    "ls": "dir", "cat": "type", "rm": "del", "cp": "copy", "mv": "move",
    "pwd": "cd", "which": "where", "clear": "cls", "export": "set",
    "grep": "findstr", "ps": "tasklist", "kill": "taskkill", "ln": "mklink",
    "touch": "type nul > 文件", "head": "（无等价，可用 shell=bash）",
    "tail": "（无等价，可用 shell=bash）", "sed": "（无等价，建议 shell=bash）",
    "awk": "（无等价，建议 shell=bash）", "chmod": "（无等价，建议 shell=bash）",
}


def _cmd_env_path_dirs() -> list:
    """返回子进程实际使用的 PATH 目录列表（与执行环境一致，非宿主 os.environ）。"""
    env = _merged_child_env() or os.environ
    raw = ""
    for key, value in env.items():
        if isinstance(key, str) and key.upper() == "PATH" and isinstance(value, str):
            raw = value
            break
    return [item.strip().strip('"') for item in raw.split(os.pathsep) if item.strip()]


def _split_top_level_pipes(command: str, quote_chars: tuple = ('"',), escape_char: str = "^") -> list:
    """按顶层 `|` 切分命令（跳过引号内与 shell 转义后的 `|`）。"""
    segments: list = []
    current: list = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == escape_char:
            current.append(char)
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            current.append(char)
            continue
        if char in quote_chars:
            quote = char
            current.append(char)
            continue
        if char == "|":
            segments.append("".join(current))
            current = []
            continue
        current.append(char)
    segments.append("".join(current))
    return segments


def _parse_simple_filter(segment: str):
    """识别"仅含行数参数的 tail/head"管道段，返回 (工具名, 行数)，否则 None。"""
    match = _SIMPLE_FILTER_RE.match(segment or "")
    if not match:
        return None
    count = int(match.group(2) or match.group(3))
    if count <= 0:
        return None
    return match.group(1).lower(), count


def _resolve_filter_path(tool: str) -> str:
    """按子进程 PATH 把工具名解析为可执行文件路径（找不到返回空串）。

    按 .exe → .com → .bat → 无扩展名 的顺序逐目录查找，与 cmd 的搜索顺序一致；
    实测坑：`tail` 可能命中 E:\\BtSoft\\panel\\script\\tail.EXE（非管道过滤器实现）。
    """
    for directory in _cmd_env_path_dirs():
        for name in (f"{tool}.exe", f"{tool}.com", f"{tool}.bat", tool):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


def _pipe_filter_works(tool: str, args: str, exe_path: str = "") -> bool:
    """用【管道形式 + 独立进程生产者】探测过滤器是否真的可用（结果缓存）。

    为什么不能直接调用探测：部分实现（如宝塔 tail.exe）直接读 stdin 时能输出，
    但在 cmd 管道中提前退出且不转发数据，直接调用探测会误判为"可用"。
    为什么探针必须是"独立进程生产者"：实测该实现对 cmd 内部命令（`for /l` + `echo`）
    能正常输出，只有上游是**独立进程**（python.exe 等，自己持有管道写句柄）时
    才会出现"上游写管道失败（Errno 22）+ 过滤器不转发 → 整条管道静默无输出"；
    只测内部命令会漏判（本次修复中第一版探针即因此误判为可用）。
    探测脚本以 .cmd 文件 + `call` 执行，避免命令行引号被二次解析破坏。
    """
    key = (tool.lower(), args, exe_path.lower())
    if key in _PIPE_FILTER_PROBE_CACHE:
        return _PIPE_FILTER_PROBE_CACHE[key]
    target = f'"{exe_path}"' if exe_path else tool
    marker = "run_command_probe_line"
    total = 5000
    # 两个探针各自独立断言（不能合并计数：`-1` 这类小行数参数每个探针只产出 1 行，
    # 合并计数会把"两次都成功"误判成失败，进而错误回退到本地兜底）
    # 两个探针的输入末行必须一致：cmd `for /l (0,1,4999)` 与 python `range(5000)`
    # 都产出 marker_0..marker_4999，断言统一取 marker_{total-1}。
    # （踩坑记录：内部生产者写成 `(1,1,5000)` 时末行是 marker_5000，与断言差 1，
    #   会让 `-1` 场景永远判"不可用"、而 `-2` 及以上因倒数第二行命中而"假通过"。）
    probes = [
        # 探针 1：cmd 内部命令生产者（快速冒烟，覆盖"能否过滤"）
        f"(for /l %%i in (0,1,{total - 1}) do @echo {marker}_%%i) | {target} {args}\r\n",
        # 探针 2：独立进程生产者（真实场景；上游程序自己写管道，最易暴露丢数据）
        (f'"{sys.executable}" -c "[print(\'{marker}_\' + str(i)) for i in range({total})]"'
         f" | {target} {args}\r\n"),
    ]
    ok = True
    for probe in probes:
        check_tool_control()
        probe_path = None
        try:
            handle, probe_path = tempfile.mkstemp(prefix="rc_probe_", suffix=".cmd")
            with os.fdopen(handle, "w", encoding="mbcs", errors="replace") as file:
                file.write("@echo off\r\n")
                file.write(probe)
            proc = subprocess.run(
                [os.environ.get("COMSPEC") or "cmd.exe", "/c", "call", probe_path],
                capture_output=True, timeout=remaining_seconds(2), stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=_merged_child_env() or None)
            text = _decode_command_bytes(proc.stdout or b"", candidates=_command_output_candidates())
            if f"{marker}_{total - 1}" not in text:
                ok = False
        except ToolCancelledError:
            raise
        except Exception:
            ok = False
        finally:
            if probe_path:
                try:
                    os.unlink(probe_path)
                except OSError:
                    pass
        if not ok:
            break
    _PIPE_FILTER_PROBE_CACHE[key] = ok
    return ok


def _find_working_filter(tool: str, args: str, skip_path: str = "") -> Optional[str]:
    """在 PATH 中寻找同名的可用管道过滤器实现（跳过已知不可用的那个）。"""
    skip = os.path.normcase(os.path.abspath(skip_path)) if skip_path else ""
    for directory in _cmd_env_path_dirs():
        for name in (f"{tool}.exe", f"{tool}.com", f"{tool}.bat", tool):
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            if skip and os.path.normcase(os.path.abspath(candidate)) == skip:
                continue
            if _pipe_filter_works(tool, args, candidate):
                return candidate
    return None


def _apply_local_line_filter(text: str, kind: str, count: int) -> str:
    """本地实现 `| tail -N` / `| head -N`（按行取末尾/开头 N 行）。"""
    if not text:
        return text
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]
    if kind == "tail":
        picked = lines[-count:] if count < len(lines) else lines
    else:
        picked = lines[:count]
    return "\n".join(picked) + ("\n" if picked and trailing_newline else "")


def _guard_cmd_pipeline(command: str):
    """cmd 管道兜底：处理简单 `| tail -N` / `| head -N` 段。

    返回 (新命令, 本地过滤说明或 None, 提示列表)：
    - 过滤器可用（管道形式探测通过）：保留原命令；
    - 当前命中的实现不可用、但 PATH 中存在可用实现：替换为可用实现的绝对路径；
    - 末段均不可用：去掉该管道段，由本工具在 Python 侧取头/尾 N 行；
    - 中间段均不可用：拒绝执行，避免已知会静默丢输出的程序运行。
    """
    if "|" not in command:
        return command, None, []
    segments = _split_top_level_pipes(command)
    if len(segments) < 2:
        return command, None, []
    notes = []
    for index in range(1, len(segments)):
        parsed = _parse_simple_filter(segments[index])
        if parsed is None:
            continue
        tool, count = parsed
        args = f"-{count}"
        current_path = _resolve_filter_path(tool)
        if current_path and _pipe_filter_works(tool, args, current_path):
            continue
        alternative = _find_working_filter(tool, args, current_path)
        if alternative:
            redirections = _SIMPLE_FILTER_RE.match(segments[index]).group("redirections")
            segments[index] = f' "{alternative}" {args}{redirections}'
            notes.append(
                f"检测到 `{tool}` 当前命中的实现（{current_path or '未找到'}）不是可用的管道过滤器，"
                f"已自动改用 {alternative}（输出不再丢失）")
            continue
        if index != len(segments) - 1:
            raise ValueError(
                f"管道中间的 `{tool}` 没有可用实现（{current_path or '未找到'}），"
                "无法安全执行；请改用 shell=powershell/pwsh/bash，或把过滤器放在管道末尾")
        head = "|".join(segments[:-1]).rstrip()
        notes.append(
            f"检测到 `{tool}` 不可用（{current_path or '未找到'}），"
            f"已由 run_command 在本地实现 `| {tool} -{count}`（取"
            f"{'末尾' if tool == 'tail' else '开头'} {count} 行；如需原生过滤请安装可用实现或改用 shell=bash）")
        return head, (tool, count), notes
    return "|".join(segments), None, notes


def _guard_powershell_pipeline(command: str) -> tuple[str, list[str]]:
    """用 PowerShell 原生命令处理末尾的简单 tail/head 管道。

    Windows PATH 中可能存在不能可靠消费大量管道输入的 tail.exe；PowerShell
    会把 `tail -4` 解析为该外部程序，导致上游有输出但整条管道静默返回。
    只替换明确的行数过滤器，保留其它 tail/head 用法的原始语义。
    """
    if "|" not in command:
        return command, []
    segments = _split_top_level_pipes(command, quote_chars=('"', "'"), escape_char="`")
    if len(segments) < 2:
        return command, []
    notes = []
    for index in range(1, len(segments)):
        match = _POWERSHELL_FILTER_RE.match(segments[index])
        if match is None:
            continue
        tool = match.group("tool").lower()
        count = int(match.group("n1") or match.group("n2"))
        if count <= 0:
            continue
        direction = "Last" if tool == "tail" else "First"
        segments[index] = (
            segments[index][:match.start("tool")]
            + f"Select-Object -{direction} {count}"
            + match.group("redirections")
            + segments[index][match.end():]
        )
        notes.append(
            f"PowerShell 管道中的 `{tool}` 已改用 Select-Object -{direction} {count}，避免 PATH 中同名程序静默丢失输出")
    return "|".join(segments), notes


def _find_inline_code(command: str, min_lines: int = 2, start: int = 0):
    """定位内联代码执行（python -c / node -e）。

    返回 dict（含解释器、入口 flag 及其在命令中的位置、代码文本、multi_line 与
    quote 标记）或 None。默认 min_lines=2：仅当代码跨行（含换行）时返回——单行
    命令交给 shell 原生处理（含 `%` 的单行由 _locate_inline_code_segment 的
    退化扫描定位并改写为临时脚本）。min_lines=1 时单行代码也返回。
    start 用于从指定下标继续查找（同一命令存在多处内联代码时，跳过不合门槛的
    匹配继续向后找）。
    定位失败（引号未配对/尾部语法复杂/代码区吞并后续引号段）时跳过该匹配并
    继续向后找，全部不可用才返回 None，由调用方决定拒绝或跳过。
    """
    search_from = start
    while True:
        match = _INLINE_CODE_RE.search(command, search_from)
        if not match:
            return None
        interp_token = match.group("interp")
        interp = interp_token.strip().lstrip("&|(").strip()
        quote = match.group("quote")
        open_index = match.end() - 1
        # 闭引号：从末尾往前找，要求"后面只剩 shell 尾部语法"（重定向/管道/&&/参数）。
        # 判据（实测踩坑后放宽）：remainder 为空、或以空白开头且不含引号、不含 ";"。
        # 例：` 2>&1 | tail -2`（多行代码 + 管道）此前被过严的字符类规则漏判，
        # 导致不改写 → cmd 拆行 → 管道失效且输出混乱。
        close_index = None
        for index in range(len(command) - 1, open_index, -1):
            if command[index] != quote:
                continue
            remainder = command[index + 1:]
            if remainder == "" or (
                re.match(r"^\s[^\"']*$", remainder)
                and ";" not in remainder
                and "\n" not in remainder
            ):
                close_index = index
                break
        if close_index is None:
            # 该匹配（含尾部语法）无法安全定位：跳过，继续向后找可能存在的其他段
            search_from = match.end()
            continue
        code = command[open_index + 1: close_index]
        # 代码区含未转义的同类引号：说明闭引号定位吞并了后续引号段（如
        # `python -c "A" && echo "B"` 从末尾往前会错选最后一对引号），
        # 该匹配跳过继续向后找（`\"` 这类转义引号不算）。
        if re.search(r"(?<!\\)" + re.escape(quote), code):
            search_from = match.end()
            continue
        if len(code) > _INLINE_SCRIPT_MAX_CHARS:
            search_from = match.end()
            continue
        multi_line = "\n" in code
        if not multi_line and min_lines > 1:
            # 单行匹配不满足门槛：继续向后找（同一命令可能有多处内联代码）
            search_from = match.end()
            continue
        return {
            "interp": interp,
            "flag": match.group("flag"),
            "flag_start": match.start("flag"),
            "flag_end": match.end("flag"),
            "open_index": open_index,
            "close_index": close_index,
            "quote": quote,
            "code": code,
            "multi_line": multi_line,
        }


def _has_multiline_inline_intent(command: str) -> bool:
    """判断命令中是否存在【跨行】的内联代码写法（引号在换行之后才闭合）。

    用于"无法安全定位时是否拒绝执行"的判定：只有跨行代码才会被 cmd 拆行执行；
    单行内联代码（哪怕整条命令还有其它 shell 行）不受拆行影响——含 `%` 的单行
    代码由 _locate_inline_code_segment 定位并改写为临时脚本，不应误杀。
    """
    for match in _INLINE_CODE_INTENT_RE.finditer(command):
        quote = command[match.end() - 1]
        rest = command[match.end():]
        close = rest.find(quote)
        if close == -1 or "\n" in rest[:close]:
            return True
    return False


def _inline_code_needs_script(code: str, multi_line: bool) -> bool:
    """该段内联代码是否需要改写为临时脚本。

    需要：多行（cmd 按行解释 .cmd，必被拆行）或含 `%`（cmd 批处理层会吞掉
    孤立 `%`、把 `%VAR%` 展开成环境变量值，代码在到达子进程前就被破坏）。
    不需要：单行且不含 `%`——交给 shell 原生处理（保持既有行为与低开销）。
    """
    return multi_line or ("%" in code)


def _locate_inline_code_segment(command: str, start: int = 0):
    """定位下一段"需要脚本化"的内联代码，返回定位 dict 或 None。

    两级策略：
    1. `_find_inline_code`（从末尾往前找闭引号 + 引号吞并守卫）——多行代码
       通常能精确定位；
    2. 退化扫描"最近引号区间"逐段切分——覆盖 `python -c "A" && python -c "B"`
       这类多段串联（从末尾往前找会吞并后续引号段而定位失败），以及开头
       `python -X utf8 -c` 等前置选项形态（正则已支持）。
    只接受"单行含 `%`"或"多行"的段；其余段跳过继续向后找。
    """
    found = _find_inline_code(command, min_lines=1, start=start)
    if found is not None and _inline_code_needs_script(found["code"], found["multi_line"]):
        return found
    search_from = start
    while True:
        match = _INLINE_CODE_RE.search(command, search_from)
        if not match:
            return None
        quote = match.group("quote")
        open_index = match.end() - 1
        close_index = command.find(quote, open_index + 1)
        if close_index == -1:
            return None
        # 尾部语法校验（与主定位同规则）：闭引号后只允许空白 + 重定向/管道/链式，
        # 否则该段无法确认边界，跳过继续向后找（如 `python -c "…" ; echo done`）
        remainder = command[close_index + 1:]
        if not (
            remainder == ""
            or (
                re.match(r"^\s[^\"']*$", remainder)
                and ";" not in remainder
                and "\n" not in remainder
            )
        ):
            search_from = close_index + 1
            continue
        code = command[open_index + 1: close_index]
        if len(code) <= _INLINE_SCRIPT_MAX_CHARS and _inline_code_needs_script(
            code, "\n" in code
        ):
            return {
                "interp": match.group("interp").strip().lstrip("&|(").strip(),
                "flag": match.group("flag"),
                "flag_start": match.start("flag"),
                "flag_end": match.end("flag"),
                "open_index": open_index,
                "close_index": close_index,
                "quote": quote,
                "code": code,
                "multi_line": "\n" in code,
            }
        search_from = close_index + 1


def _rewrite_inline_code_to_script(command: str, shell_kind: str):
    """把 cmd 无法安全直接执行的内联代码改写为临时脚本（cmd 家族）。

    cmd 会破坏两类内联代码，因此两类都改写为 `<解释器> "<临时脚本>"`：
    - 多行：命令写入 .run.cmd 后按行解释，后续行被当独立命令执行
      （实测 `i was unexpected at this time.`）；
    - 含 `%` 的单行：批处理层先做 `%` 展开，孤立 `%` 被吞、`%VAR%` 被替换成
      环境变量值（实测 `python -c "print('100%')"` 输出 `100`；`print('%d' % 5)`
      直接 SyntaxError）。脚本文件内容不经 cmd 批处理解析，两种链路
      （隐藏中转链 / Popen 直连）下语义都稳定，且退出码透传。
    注意必须连入口 flag（`-c`/`-e`）一起替换掉：`python -c "<路径>"` 会把路径
    当代码字符串执行（实测 SyntaxError），而 `python "<路径>"` 才是执行脚本。
    无法安全定位且存在跨行代码意图时抛 ValueError（拒绝执行）。
    返回 (新命令, 提示列表)。
    """
    if shell_kind != "cmd":
        return command, []
    found = _locate_inline_code_segment(command)
    if found is None:
        # 不能安全改写且确有【跨行】内联代码时必须拒绝执行：否则 cmd 可能已运行
        # 首行，随后把代码正文当成一串独立命令，模型也无法从退出码判断哪些行
        # 真的执行了。单行内联代码（哪怕整条命令还有其它 shell 行）不受拆行
        # 影响，不误杀"多行 shell 命令 + 单行内联代码"的普通写法。
        if _has_multiline_inline_intent(command):
            raise ValueError(
                "cmd 下的多行内联代码无法安全改写，命令未执行；请用 write_file 写入"
                "脚本后运行，或改用适合该语法的 shell")
        return command, []
    interp = found["interp"]
    interp_name = Path(interp).stem.lower()
    extension = "js" if interp_name == "node" else "py"
    code = found["code"]
    try:
        _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        seq = next(_BG_NAME_SEQ) % 10000
        script_path = _BG_LOG_DIR / f"inline_{stamp}_{seq:04d}_{os.getpid()}.{extension}"
        script_path.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"内联代码改写失败（{exc}），命令未执行；"
            "请用 write_file 写脚本后运行"
        ) from exc
    # 用"解释器 + 脚本路径"替换"解释器 + 入口 flag + 引号内代码"
    # 无空格的路径直接传给 cmd：中转链的 call/重定向解析层对额外引号
    # 会二次保留，使 Python 把引号当成文件名的一部分。
    script_arg = f'"{script_path}"' if any(ch.isspace() for ch in str(script_path)) else str(script_path)
    new_command = (
        f'{command[:found["flag_start"]]}{script_arg}'
        f'{command[found["close_index"] + 1:]}'
    )
    if found["multi_line"]:
        note = (
            f"检测到多行内联代码（{interp} {found['flag']}）：cmd 按行解释 .cmd 文件"
            f"会把后续行当命令执行，已自动改写为临时脚本 {_display_path(script_path)} "
            "后执行（语义等价，退出码透传）"
        )
    else:
        note = (
            f"检测到单行内联代码中的 `%`（{interp} {found['flag']}）：cmd 批处理层会"
            "吞掉孤立 `%` 或把 `%VAR%` 展开成环境变量值（实测 `print('100%')` 输出丢 `%`），"
            f"已自动改写为临时脚本 {_display_path(script_path)} 后执行"
            "（代码原样交给解释器：如需百分号直接写单个 `%`，无需 cmd 的 `%%` 转义）"
        )
    return new_command, [note]


def _first_token(segment: str) -> str:
    text = segment.strip().lstrip("&|").strip()
    match = re.match(r"^([^\s&|<>()]+)", text)
    return match.group(1) if match else ""


def _command_exists_in_child_path(token: str) -> bool:
    """按子进程 PATH（含 PATHEXT 常见扩展）判断命令是否存在。

    不用宿主 shutil.which：子进程 PATH 经过注册表补全，可能比宿主更全/不同，
    提示要与实际执行环境一致（例如本机 MSYS2 提供了 ls/cat，就不该提示"不存在"）。
    """
    if not token:
        return False
    if any(sep in token for sep in ("\\", "/", ":")):
        return os.path.isfile(token)
    for directory in _cmd_env_path_dirs():
        for ext in ("", ".exe", ".com", ".bat", ".cmd"):
            if os.path.isfile(os.path.join(directory, token + ext)):
                return True
    return False


def _cmd_syntax_hints(command: str, exit_code, stdout_text: str, stderr_text: str) -> list:
    """cmd 家族的常见语法误用提示（不改命令，只解释现象并给等价写法）。

    - 顶层 `;`：cmd 不把它当分隔符，`echo A; echo B` 会把后半段原样输出（exit=0，
      表现为"命令成功但后续没执行"）——实测坑；
    - 命令名不存在（如 Unix 命令 ls/cat/grep）：给出 Windows 等价物或 shell=bash 建议。
    """
    hints: list = []
    segments = _split_top_level_pipes(command)
    joined = "|".join(segments)
    stripped = re.sub(r'"[^"]*"', '""', joined)
    if ";" in stripped:
        hints.append(
            '命令包含顶层 ";"：cmd 不支持用 ";" 分隔命令（会被当普通字符原样输出，'
            'exit=0 但后续命令没执行）。请改用 "&&"（前一条成功才执行）或 "&"（无条件顺序执行）；'
            "需要 \";\" 语义请指定 shell=powershell/pwsh/bash。")
    for segment in segments:
        token = _first_token(segment)
        if not token or any(ch in token for ch in ('%', '$', '"', "'")):
            continue
        if token.lower() in _CMD_INTERNAL_COMMANDS:
            continue
        if not _command_exists_in_child_path(token):
            equivalent = _UNIX_COMMAND_EQUIVALENTS.get(token.lower())
            extra = f"；Windows 等价：{equivalent}" if equivalent else ""
            hints.append(
                f'命令 "{token}" 在当前 PATH 中不存在{extra}。'
                "如为 Unix 命令，请指定 shell=bash（需 Git Bash/WSL）或改用 Windows 原生命令。")
    # 去重并限制条数，避免提示本身占满输出
    unique: list = []
    for hint in hints:
        if hint not in unique:
            unique.append(hint)
    return unique[:3]


def _append_command_notes(text: str, notes: list) -> str:
    """把补丁/语法提示追加到工具返回末尾（不改动正文结构，便于模型识别）。"""
    if not notes:
        return text
    lines = [f"[run_command] {note}" for note in notes if note]
    if not lines:
        return text
    return text + "\n" + "\n".join(lines)


def _prune_old_bg_files() -> None:
    """清理过期中转文件；未关闭的后台记录和仍运行的任务不按 24h 删除。"""
    try:
        cutoff = time.time() - 24 * 3600
        protected = set()
        for metadata_path in _BG_LOG_DIR.glob("bg_*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                pid = int(metadata["pid"])
                alive = _pid_alive_windows(pid) if os.name == "nt" else _pid_alive_posix(pid)
                if not metadata.get("closed") or alive or float(metadata.get("closed_at") or 0) > cutoff:
                    protected.add(metadata_path.name.removesuffix(".meta.json"))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        # bg_=后台日志；fg_=前台命令 WMI 中转的临时文件；inline_=多行内联代码改写出的脚本
        patterns = ("bg_*", "fg_*", "inline_*")
        for pattern in patterns:
            for item in _BG_LOG_DIR.glob(pattern):
                try:
                    if item.suffix == ".lock":
                        continue
                    if any(item.name.startswith(base + ".") for base in protected):
                        continue
                    if pattern == "inline_*" and protected:
                        continue
                    if item.stat().st_mtime < cutoff:
                        item.unlink()
                except OSError:
                    continue
    except OSError:
        pass


def _pid_alive_windows(pid: int) -> bool:
    """用 OpenProcess+GetExitCodeProcess 判断进程是否仍在运行。"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    exit_code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return bool(ok) and exit_code.value == STILL_ACTIVE


def _batch_env_lines() -> list:
    """把合并后的子进程环境转为批处理 set 行（供 WMI 中转/后台启动器注入环境）。

    WMI 创建的进程不继承本进程环境块，必须显式注入才能让命令拿到完整 PATH 等。
    规则：批处理内 % 需写成 %%；含换行的值无法在单行 set 中表达则跳过；
    单条超长超过 cmd 单行长度限制时也跳过，避免整条命令被截断失效。
    """
    lines = []
    for name, value in (_merged_child_env() or {}).items():
        if (isinstance(name, str) and isinstance(value, str)
                and "\n" not in value and "\r" not in value
                and len(name) + len(value) <= 4000):
            lines.append(f'set "{name}={value.replace("%", "%%")}"')
    return lines


def _wmi_create_process(command_line: str) -> int:
    """通过 WMI Win32_Process.Create 以【默认方式】启动进程，返回新进程 PID。

    新进程的父进程是系统服务（WmiPrvSE），完全脱离本工具的进程树，
    因此不受宿主环境"命令结束后清理整棵进程树"的影响，也不继承本进程
    所在的受限 Job 对象（部分原生程序如 nvidia-smi 在该 Job 内初始化会失败）。

    为什么不用 Win32_ProcessStartup 定制窗口（实测结论）：
      - CREATE_NO_WINDOW(0x08000000)：WMI 拒绝，Create 返回错误码 21；
      - DETACHED_PROCESS(0x8)：无窗口，但子进程完全没有控制台，python/ping 等
        控制台程序标准句柄环境被破坏，输出丢失甚至挂起；
      - 默认启动：分配新控制台 → 程序全部正常，但 cmd 会闪现窗口。
    因此本函数固定默认启动，窗口问题交由上层用 VBS vbHide 链路解决
    （wscript 为 GUI 程序自身无窗口，见 _write_relay_script）。
    优先用 pywin32 COM（无额外进程开销）；未安装时回退 PowerShell Invoke-CimMethod。
    """
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            wmi = win32com.client.GetObject(
                "winmgmts:{impersonationLevel=impersonate}!//./root/cimv2")
            proc_class = wmi.Get("Win32_Process")
            in_params = proc_class.Methods_("Create").InParameters.SpawnInstance_()
            in_params.Properties_("CommandLine").Value = command_line
            result = wmi.ExecMethod("Win32_Process", "Create", in_params)
            code = result.Properties_("ReturnValue").Value
            if code != 0:
                raise ValueError(f"WMI 创建进程失败（Win32_Process.Create 返回码 {code}）")
            pid = int(result.Properties_("ProcessId").Value)
            # 先释放全部 COM 引用再 CoUninitialize，避免"IUnknown 释放异常"噪音
            del result, in_params, proc_class, wmi
            return pid
        finally:
            pythoncom.CoUninitialize()
    except ImportError:
        pass
    # 回退：PowerShell（-EncodedCommand 避免引号转义问题）
    ps_script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line.replace(chr(39), chr(39) * 2)}'}}; "
        "if ($r.ReturnValue -ne 0) { Write-Error ('WMI错误码 ' + $r.ReturnValue); exit 1 } "
        "else { Write-Output $r.ProcessId }")
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=remaining_seconds(5),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = completed.stdout.decode(_command_output_candidates()[0], errors="replace").strip()
    if completed.returncode != 0 or not text.isdigit():
        detail = completed.stderr.decode(errors="replace").strip()[:300]
        raise ValueError(f"WMI 回退启动失败（exit={completed.returncode}）: {detail or text}")
    return int(text)


def _write_relay_script(command, work_dir, shell_exe, shell_kind, shell_prefix, prefix,
                        split_streams=False):
    """生成"WMI → wscript → VBS(vbHide) → cmd"隐藏执行链的脚本文件并启动，返回信息字典。

    为什么经 VBS 中转（实测结论）：
      - WMI 默认启动 cmd：分配新控制台 → 控制台程序正常，但窗口闪现；
      - WMI + DETACHED_PROCESS：无窗口，但子进程完全没有控制台，
        python/ping 等控制台程序输出丢失甚至挂起；
      - WMI + CREATE_NO_WINDOW：Win32_ProcessStartup 不接受该值（Create 返回 21）；
      - WMI 启动 wscript（GUI 程序自身无窗口）→ VBS 以 vbHide(0) 隐藏运行 cmd：
        cmd 拥有【隐藏的】控制台 → 控制台程序正常工作 + 全程无可见窗口。✔
        （即资源管理器/VBS 启动思路：由 GUI 中介拉起，天然脱离受限 Job 与进程树）

    文件三件套（均写入 _BG_LOG_DIR；前台旧文件 24h 清理，后台记录关闭后清理）：
      <prefix>_*.run.cmd   用户命令原文（仅 cmd 家族需要；其他 shell 用括号分组内联）
      <prefix>_*.cmd       启动器：环境注入 + cd + 执行命令并整体重定向到 out
      <prefix>_*.vbs       包装器：vbHide 运行启动器、等待结束、把退出码写入 done
    返回 dict：pid/out/done/launcher/runner/vbs 路径。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    seq = next(_BG_NAME_SEQ) % 10000
    base = f"{prefix}_{stamp}_{seq:04d}_{os.getpid()}"
    out_path = _BG_LOG_DIR / f"{base}.out.txt"
    # split_streams（前台中转用）：stdout/stderr 分开落盘，便于对齐工具返回结构
    err_path = _BG_LOG_DIR / f"{base}.err.txt" if split_streams else None
    done_path = _BG_LOG_DIR / f"{base}.done.txt"
    launcher_path = _BG_LOG_DIR / f"{base}.cmd"
    vbs_path = _BG_LOG_DIR / f"{base}.vbs"
    if shell_kind == "cmd":
        # cmd 家族：命令原文写入独立 runner 文件，规避引号/管道等特殊字符的二次解析；
        # 直接写 "命令 < nul > 文件" 时重定向只绑定最后一个管道段/链式命令，
        # 会丢掉前段输出（如 echo）甚至覆盖管道输入（如 dir|findstr 变空）
        runner_path = _BG_LOG_DIR / f"{base}.run.cmd"
        exec_line = f'call "{runner_path}"'
    elif shell_kind in ("powershell", "pwsh"):
        # PowerShell：命令写入 .ps1 由 -File 执行。不能把命令内联进启动器：
        # cmd 解析层会把 list2cmdline 的 \" 转义当裸引号处理，含引号/分号/换行的
        # 命令必然失败（"\"; \"... was unexpected at this time."）
        runner_path = _BG_LOG_DIR / f"{base}.run.ps1"
        exec_line = subprocess.list2cmdline([
            shell_exe, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(runner_path)])
    else:
        # bash/sh/zsh：命令写入 .sh 并以 stdin 喂给 shell（Windows 上 bash 常为
        # WSL 启动器，无法按 Windows 路径直接执行脚本文件；stdin 方式两种 bash 通吃）。
        # 必须用括号包住：外层 launcher 还会加 "< nul"，同层两个 stdin 重定向时
        # cmd 取最后一个，会把脚本 stdin 覆盖成 nul 导致命令空跑
        runner_path = _BG_LOG_DIR / f"{base}.run.sh"
        exec_line = f'( {subprocess.list2cmdline([shell_exe])} < "{runner_path}" )'
    launcher_text = (
        "@echo off\r\n"
        + "".join(line + "\r\n" for line in _batch_env_lines())
        + f'cd /d "{work_dir or os.getcwd()}"\r\n'
        + (f'{exec_line} < nul > "{out_path}" 2> "{err_path}"\r\n' if split_streams
           else f'{exec_line} < nul > "{out_path}" 2>&1\r\n')
    )
    # VBS 包装器：0=vbHide 隐藏窗口；True=等待结束拿退出码；退出码写入 done 供轮询读取
    vbs_text = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'code = sh.Run("cmd /c ""{launcher_path}""", 0, True)\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'Set f = fso.CreateTextFile("' + str(done_path) + '", True)\r\n'
        'f.Write code\r\n'
        'f.Close\r\n'
    )
    try:
        launcher_path.write_bytes(launcher_text.encode("mbcs"))
        vbs_path.write_bytes(vbs_text.encode("mbcs"))
    except (OSError, LookupError):
        launcher_path.write_bytes(launcher_text.encode("utf-8", errors="replace"))
        vbs_path.write_bytes(vbs_text.encode("utf-8", errors="replace"))
    if runner_path is not None:
        _write_runner_script(runner_path, shell_kind, command)
    # wscript 为 GUI 子系统：WMI 默认启动它不产生可见窗口；//nologo 抑制横幅
    pid = _wmi_create_process(f'wscript.exe //nologo "{vbs_path}"')
    return {
        "pid": pid, "out": out_path, "err": err_path, "done": done_path,
        "launcher": launcher_path, "runner": runner_path, "vbs": vbs_path,
    }


_POWERSHELL_EXIT_SUFFIX = (
    "$__ytools_command_ok = $?\n"
    "if ($LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "if (-not $__ytools_command_ok) { exit 1 }\n"
)


def _write_runner_script(runner_path: Path, shell_kind: str, command: str) -> None:
    """写执行脚本：cmd→.run.cmd（mbcs）；powershell/pwsh→.run.ps1（UTF-8 BOM，-File 读）；
    bash/sh/zsh→.run.sh（UTF-8，stdin 喂给 shell）。ps1/sh 末尾附加退出码透传，
    让中转链 done 文件记录命令本身的退出码。

    换行统一为 LF 再按目标 shell 组装：命令原文若已含 CRLF（如从文件复制而来），
    直接拼接会产生 CR CR LF，cmd 解析层会出现空行/多余回车（实测 runner 文件里
    出现 `\\r\\r\\n`），属于不必要的风险源。
    """
    normalized = command.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if shell_kind == "cmd":
        # cmd 按行解释 .cmd：行尾必须是 CRLF；行内 LF 会被当命令结束（多行内联代码
        # 已在上游改写为临时脚本，此处只做兜底归一化）
        text = "@echo off\r\n" + normalized.replace("\n", "\r\n") + "\r\n"
        try:
            runner_path.write_bytes(text.encode("mbcs"))
        except (OSError, LookupError):
            runner_path.write_bytes(text.encode("utf-8", errors="replace"))
        return
    text = normalized + "\n"
    if shell_kind in ("powershell", "pwsh"):
        text = powershell_text_prelude() + text
        text += _POWERSHELL_EXIT_SUFFIX
        runner_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        return
    text += "exit $?\n"
    runner_path.write_bytes(text.encode("utf-8"))


def _cleanup_relay_files(info: dict, include_streams: bool = False) -> None:
    """清理中转脚本临时文件；include_streams=True 时连 out/err 输出文件一并删除。"""
    keys = ["launcher", "runner", "vbs", "done"]
    if include_streams:
        keys += ["out", "err"]
    for key in keys:
        path = info.get(key)
        if path is None:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


def _save_full_output(stdout_text: str, stderr_text: str) -> Optional[Path]:
    """把完整输出（stdout+stderr 分段）落盘到日志目录，返回文件路径；失败返回 None。

    仅在前台输出超长截断时调用：工具返回中省略的中间部分可从该文件续读。
    文件名前缀 fg_，与中转文件同规则（24h 自动清理）。
    """
    try:
        _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = f"fg_{stamp}_{next(_BG_NAME_SEQ) % 10000:04d}_{os.getpid()}"
        path = _BG_LOG_DIR / f"{base}.full.txt"
        sections = []
        if stdout_text:
            sections.append("----- stdout -----\n" + stdout_text)
        if stderr_text:
            sections.append("----- stderr -----\n" + stderr_text)
        path.write_text("\n".join(sections), encoding="utf-8")
        return path
    except OSError:
        return None


def _terminate_windows_tree(pid):
    try:
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kill_process_tree(proc: subprocess.Popen, is_nt: bool) -> bool:
    """超时后终止进程及其子树（Windows 用 taskkill /T，POSIX 用进程组 SIGKILL）。"""
    try:
        if is_nt:
            if _terminate_windows_tree(proc.pid):
                return True
            proc.kill()
            return False
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return True
    except Exception:
        try:
            proc.kill()
            return False
        except OSError:
            return False


_RUN_COMMAND_PREVIEW_BYTES = 1024 * 1024


def _read_command_output_preview(path: Path, candidates: list[str]) -> tuple[str, bool]:
    """只读取命令输出文件开头；完整输出由原文件提供。"""
    try:
        with path.open("rb") as handle:
            data = handle.read(_RUN_COMMAND_PREVIEW_BYTES + 1)
    except FileNotFoundError:
        return "", False
    truncated = len(data) > _RUN_COMMAND_PREVIEW_BYTES
    text = _decode_command_bytes(data[:_RUN_COMMAND_PREVIEW_BYTES], candidates=candidates)
    return text.replace("\r\n", "\n"), truncated


def _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                           cwd, timeout, is_nt) -> tuple:
    """Popen 直接执行（Windows 中转链失败/超时的兜底；POSIX 常规路径）。

    返回 (退出码, stdout 预览, stderr 预览, 是否超时, 耗时秒, 输出文件信息)。
    """
    if shell_kind == "cmd":
        # cmd 用 /c 接命令原文（字符串形式），规避列表形式把引号按 MSVCRT
        # 规则序列化成 \" 而 cmd 不认 \" 导致的二次解析破坏
        argv = [shell_exe, "/c", command]
    else:
        if shell_kind in ("powershell", "pwsh"):
            # 与隐藏中转脚本保持一致：管道末段是 Select-Object 等 cmdlet 时，
            # PowerShell -Command 可能只返回 0/1，丢失上游原生程序的退出码。
            command = powershell_text_prelude() + command + "\n" + _POWERSHELL_EXIT_SUFFIX
        argv = [shell_exe, *shell_prefix, command]
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL, cwd=cwd,
        env=_merged_child_env() if is_nt else None,
    )
    if is_nt:
        # 不弹新控制台窗口；输出仍可正常通过管道捕获
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # 独立进程组：超时后可整组 SIGKILL，不留孤儿孙进程
        popen_kwargs["start_new_session"] = True
    _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"fg_{stamp}_{next(_BG_NAME_SEQ) % 10000:04d}_{os.getpid()}"
    out_path = _BG_LOG_DIR / f"{base}.out.txt"
    err_path = _BG_LOG_DIR / f"{base}.err.txt"
    started = time.monotonic()
    termination_confirmed = None
    proc = None
    with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
        try:
            check_tool_control()
            proc = subprocess.Popen(
                argv, stdout=out_handle, stderr=err_handle, **popen_kwargs)
            timed_out = False
            deadline = started + timeout
            while proc.poll() is None:
                check_tool_control(check_deadline=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    termination_confirmed = _kill_process_tree(proc, is_nt)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        termination_confirmed = False
                    break
                try:
                    proc.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc, is_nt)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            # Keep cancellation/error logs for the normal 24h cleanup. On Windows
            # these handles are still open until the surrounding context exits.
            raise
    elapsed = time.monotonic() - started
    candidates = _command_output_candidates()
    stdout_text, out_cut = _read_command_output_preview(out_path, candidates)
    stderr_text, err_cut = _read_command_output_preview(err_path, candidates)
    info = {"out": out_path, "err": err_path, "output_truncated": out_cut or err_cut,
            "termination_confirmed": termination_confirmed}
    return proc.returncode, stdout_text, stderr_text, timed_out, elapsed, info


def _run_command_foreground(command, shell_exe, shell_kind, shell_prefix,
                            cwd, timeout, is_nt) -> tuple:
    """前台执行命令，返回 (退出码, stdout 文本, stderr 文本, 是否超时, 耗时秒, 中转信息或 None)。

    Windows 上本服务进程被宿主放入受限 Job 对象（实测 LimitFlags 含
    KILL_ON_JOB_CLOSE），部分原生程序（nvidia-smi 等）在该 Job 内初始化会
    失败。与后台模式同思路，默认改走 "WMI → wscript → VBS(vbHide) → cmd"
    隐藏中转链（脱离 Job 与进程树），stdout/stderr 分别落盘后读取；
    中转链启动失败时回退 Popen 直接执行。POSIX 仍走 Popen 直接执行。
    """
    if not is_nt:
        return _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                                      cwd, timeout, is_nt)
    started = time.monotonic()
    check_tool_control()
    try:
        info = _write_relay_script(
            command, cwd, shell_exe, shell_kind, shell_prefix, "fg",
            split_streams=True)
    except ToolCancelledError:
        raise
    except Exception:
        check_tool_control()
        return _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                                       cwd, remaining_seconds(timeout), is_nt)
    timed_out = False
    deadline = started + timeout
    done_path = Path(info["done"])
    try:
        while time.monotonic() < deadline:
            check_tool_control(check_deadline=False)
            if done_path.exists():
                text = done_path.read_text(encoding="mbcs", errors="ignore").strip()
                if text:
                    break
            time.sleep(0.05)
    except BaseException:
        _terminate_windows_tree(info["pid"])
        _cleanup_relay_files(info, include_streams=True)
        raise
    exit_code = None
    if done_path.exists():
        try:
            exit_code = int(done_path.read_text(encoding="mbcs", errors="ignore").strip())
        except (ValueError, OSError):
            exit_code = None
    if exit_code is None:
        # 超时：终止执行侧进程树。wscript 包装器是树根（wscript → cmd → 命令），
        # taskkill /T 连带子孙一起杀；原实现把脚本路径传给 /PID 导致从未真正终止
        timed_out = True
        root_pid = info.get("pid")
        if root_pid:
            try:
                stopped = subprocess.run(
                    ["taskkill", "/PID", str(root_pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                info["termination_confirmed"] = stopped.returncode == 0
            except Exception:
                info["termination_confirmed"] = False
        exit_code = -1
    elapsed = time.monotonic() - started
    candidates = _command_output_candidates()
    out_path, err_path = Path(info["out"]), info.get("err")
    stdout_text, out_cut = _read_command_output_preview(out_path, candidates)
    stderr_text, err_cut = _read_command_output_preview(Path(err_path), candidates) if err_path else ("", False)
    info["output_truncated"] = out_cut or err_cut
    return exit_code, stdout_text, stderr_text, timed_out, elapsed, info


def _run_command_background(command, shell_exe, shell_kind, shell_prefix, cwd, is_nt,
                             session_id="", *, structured=False) -> str | dict:
    """后台分离模式：立即返回，输出落盘到日志文件供轮询。"""
    try:
        _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建后台日志目录 {_BG_LOG_DIR}: {exc}")
    if is_nt:
        # Windows：WMI → wscript → VBS(vbHide) → shell 隐藏执行链，
        # 新进程脱离本进程树与受限 Job，不受宿主"命令结束后清理进程树"影响
        info = _write_relay_script(command, cwd, shell_exe, shell_kind, shell_prefix, "bg")
        alive = _pid_alive_windows(info["pid"])
        job_id = _save_command_job(info["pid"], info["out"], info["done"], shell_kind, cwd, session_id, command)
        message = "\n".join([
            f"[run_command] 后台模式{'已启动' if alive else '已启动（进程状态未知，可能瞬间结束）'}"
            f" | shell={shell_kind} | pid={info['pid']} | job_id={job_id}",
            f"输出文件: {_display_path(info['out'])}",
            f"结束标记: {_display_path(info['done'])}（命令结束后写入退出码；该文件出现即已结束）",
            f"查询: poll_command(job_id=\"{job_id}\")；停止: stop_command(job_id=\"{job_id}\")",
        ])
        return _background_command_result(message, job_id, session_id) if structured else message
    # POSIX：独立进程组中的监督进程等待 shell 退出并写入完成标记。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"bg_{stamp}_{next(_BG_NAME_SEQ) % 10000}_{os.getpid()}"
    out_path = _BG_LOG_DIR / f"{base}.out.txt"
    done_path = _BG_LOG_DIR / f"{base}.done.txt"
    spec_path = _BG_LOG_DIR / f"{base}.spec.json"
    prefix = shell_prefix or ["-c"]
    spec_path.write_text(json.dumps({"argv": [shell_exe, *prefix, command], "cwd": cwd,
                                    "output": str(out_path), "done": str(done_path)}), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("background_runner.py")), str(spec_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        cwd=cwd, start_new_session=True, env=_merged_child_env())
    # Reap the supervisor while this worker lives; detached jobs also survive worker exit.
    from threading import Thread
    Thread(target=proc.wait, daemon=True).start()
    job_id = _save_command_job(proc.pid, out_path, done_path, shell_kind, cwd, session_id, command)
    message = "\n".join([
        f"[run_command] 后台模式已启动 | shell={shell_kind} | pid={proc.pid} | job_id={job_id}",
        f"输出文件: {_display_path(out_path)}（stdout+stderr 合并追加）",
        f"结束标记: {_display_path(done_path)}",
        f"查询: poll_command(job_id=\"{job_id}\")；停止: stop_command(job_id=\"{job_id}\")",
    ])
    return _background_command_result(message, job_id, session_id) if structured else message


def _background_command_result(message, job_id, session_id):
    from .command_jobs import read_job, snapshot
    _, meta = read_job(job_id, session_id)
    result = snapshot(meta)
    result.update(message=message, stdout="", stderr="", timed_out=False, truncated=False, next_cursor="")
    return result


def _save_command_job(pid: int, output_path: Path, done_path: Path | None, shell: str, cwd: str,
                      session_id: str = "", command: str = "") -> str:
    from factory.agent_runtime.command_jobs import write_metadata
    from util.process_identity import process_identity
    job_id = output_path.name.removesuffix(".out.txt")
    metadata = {
        "job_id": job_id, "pid": pid, "output_path": str(output_path),
        "completion_path": str(done_path) if done_path else None,
        "shell": shell, "cwd": cwd, "created_at": time.time(), "stopped": False,
        "session_id": session_id, "command": command, "process_identity": process_identity(pid),
        "closed": False,
    }
    write_metadata(_BG_LOG_DIR / f"{job_id}.meta.json", metadata)
    return job_id


def _command_output_tail(output_path: Path, tail_lines: int, max_chars: int) -> tuple[str, bool]:
    """Bound both disk reads and returned text; keep newest lines, not a partial prefix."""
    import codecs
    with output_path.open("rb") as handle:
        prefix = handle.read(4)
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - 256 * 1024)
        bom_encoding = unicode_bom(prefix)
        if start and bom_encoding in ("utf-16", "utf-32"):
            width = 2 if bom_encoding == "utf-16" else 4
            start -= start % width
        handle.seek(start)
        raw = handle.read(256 * 1024 + 4)
    if start and bom_encoding in ("utf-16", "utf-32"):
        # A tail lacks the file BOM; retain its byte order when decoding.
        width = 2 if bom_encoding == "utf-16" else 4
        text = codecs.getincrementaldecoder(bom_encoding)(errors="replace").decode(prefix[:width] + raw, final=False)
    else:
        text = None
        # A live writer may be between bytes of its last Unicode character.
        # Defer that incomplete suffix until the next snapshot, without switching
        # the valid UTF-8 prefix to a legacy encoding. A tail can also start mid-char.
        for skip in range(min(4, len(raw)) if start else 1):
            try:
                codec = "utf-8-sig" if not start and bom_encoding == "utf-8-sig" else "utf-8"
                text = codecs.getincrementaldecoder(codec)(errors="strict").decode(raw[skip:], final=False)
                break
            except UnicodeDecodeError:
                pass
        if text is None:
            text = _decode_command_bytes(raw, candidates=list(_command_output_candidates()))
    lines = text.splitlines(keepends=True)
    if start and len(lines) > tail_lines:
        # 起点可能落在一行中间；只有后面已有足够完整行时才丢掉它。
        # 最后一行若长于读取窗口且以换行结束，盲目按首个换行裁剪会把结果清空。
        lines = lines[1:]
    selected = ''.join(lines[-tail_lines:])
    truncated = bool(start or len(lines) > tail_lines or len(selected) > max_chars)
    return selected[-max_chars:], truncated


def _command_job_action(action: str, job_id: str, session_id: str = "", *,
                        tail_lines: int = 30, max_chars: int = 12000,
                        cursor=None, wait_seconds=0, encoding='') -> dict[str, Any]:
    from factory.agent_runtime.command_jobs import action as job_action
    result = job_action(job_id, action, session_id, tail_lines=tail_lines, max_chars=max_chars,
                        cursor=cursor, wait_seconds=wait_seconds, encoding=encoding)
    # UI needs command/session metadata; repeated model polls only need status and output.
    for key in ("command", "shell", "cwd", "session_id", "created_at", "can_stop"):
        result.pop(key, None)
    return result


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _run_command_impl(command, shell, work_dir, timeout_seconds, background, max_output_chars=None,
                      structured=False, session_id="", yield_time_ms=None, owner_agent='main',
                      interactive=False, tty=False, auto_yield=False) -> str | dict[str, Any]:
    command = string_arg({"command": command}, "command", required=True, max_length=200000).strip()
    timeout = number_arg({"timeout_seconds": timeout_seconds}, "timeout_seconds", 120, minimum=1, maximum=1800)
    with tool_deadline(30 if background else timeout):
        return _execute_command_impl(command, shell, work_dir, timeout, background,
                                     max_output_chars, structured, session_id, yield_time_ms, owner_agent, interactive, tty, auto_yield)


def _execute_command_impl(command, shell, work_dir, timeout, background, max_output_chars,
                          structured, session_id, yield_time_ms=None, owner_agent='main', interactive=False, tty=False,
                          auto_yield=False):
    output_limit = max(512, min(_RUN_COMMAND_MAX_CHARS, int(max_output_chars or _RUN_COMMAND_MAX_CHARS)))
    is_nt = os.name == "nt"
    # 顺手清理超过 24h 的历史日志/中转文件（后台输出、前台中转与完整输出文件）
    _prune_old_bg_files()
    shell_exe, shell_kind, shell_prefix = _resolve_shell(shell, is_nt)
    if shell_kind == "cmd" and re.search(
        r"\b(?:python|python3|py|node)(?:\.exe)?\s+-\s*<<", command,
        re.IGNORECASE,
    ):
        raise ValueError(
            "cmd 不支持 `python - <<标记` 这类 heredoc，命令未执行；"
            "请用 write_file 写脚本后运行，或选择 shell=bash"
        )
    # ===== cmd 家族健壮性补丁（详见文件头补丁说明）=====
    # ① 内联代码（多行，或含 `%` 的单行）改写为临时脚本：
    #    多行会被 cmd 拆行执行（实测 `i was unexpected at this time.`）；
    #    单行含 `%` 会被批处理层吞掉/展开（实测 `print('100%')` 输出 `100`）
    guard_notes = []
    for _ in range(10):
        rewritten, notes = _rewrite_inline_code_to_script(command, shell_kind)
        guard_notes.extend(notes)
        if rewritten == command:
            break
        command = rewritten
    else:
        raise ValueError("同一命令中的内联代码改写次数过多，命令未执行；请写入脚本后运行")
    # ② 管道过滤器兜底：PATH 里可能命中"非管道过滤器"实现（实测宝塔 tail.exe），
    #    在管道中提前退出且不转发输出 → 整条管道静默无输出。
    #    cmd 探测并替换/本地兜底；PowerShell 直接使用原生 Select-Object。
    local_filter = None
    if shell_kind == "cmd":
        command, local_filter, pipe_notes = _guard_cmd_pipeline(command)
        guard_notes = guard_notes + pipe_notes
        if local_filter is not None and background:
            raise ValueError(
                "后台模式没有可用的 tail/head 管道过滤器，命令未执行；"
                "请直接运行上游命令，再用 poll_command 的 tail_lines 读取末尾输出，"
                "或安装可用的过滤器")
    elif shell_kind in ("powershell", "pwsh"):
        command, pipe_notes = _guard_powershell_pipeline(command)
        guard_notes = guard_notes + pipe_notes
    work_text = string_arg({"work_dir": work_dir}, "work_dir").strip()
    cwd = str(_resolve_dir_path(work_text)) if work_text else os.getcwd()
    if auto_yield and yield_time_ms is None and not background and local_filter is None:
        yield_time_ms = 1000
    if yield_time_ms is not None:
        if background:
            raise ValueError('yield_time_ms 与 background=true 不能同时指定')
        if local_filter is not None:
            raise ValueError('当前管道需要本地过滤，请移除 yield_time_ms 或直接运行上游命令')
        from .command_leases import start
        if shell_kind == 'cmd':
            guard_notes.extend(_cmd_syntax_hints(command, None, '', ''))
        result = start(command, shell_exe, shell_kind, shell_prefix, cwd, session_id,
                       remaining_seconds(timeout), yield_time_ms, output_limit, owner_agent, interactive, tty)
        result['message'] = _append_command_notes(result['message'], guard_notes)
        return result
    if background:
        result = _run_command_background(
            command, shell_exe, shell_kind, shell_prefix, cwd, is_nt, session_id, structured=structured)
        if isinstance(result, dict):
            result["message"] = _append_command_notes(result["message"], guard_notes)
            return result
        return _append_command_notes(result, guard_notes)
    check_tool_control()
    exit_code, stdout_text, stderr_text, timed_out, elapsed, relay_info = _run_command_foreground(
        command, shell_exe, shell_kind, shell_prefix, cwd, remaining_seconds(timeout), is_nt)
    if "\ufffd" in stdout_text or "\ufffd" in stderr_text:
        guard_notes.append(
            "输出包含替换字符 �；可能是子进程读取源文件时已替换原始字节，"
            "也可能是输出编码无法可靠识别。请核对原文件编码或原始字节，勿据此判断文本原貌"
        )
    if local_filter is not None:
        kind, count = local_filter
        if relay_info and relay_info.get("output_truncated") and kind == "tail":
            # 前台只预读输出文件的开头；tail 必须从完整文件末尾读取。
            out_path = relay_info.get("out")
            if out_path:
                try:
                    stdout_text, _ = _command_output_tail(Path(out_path), count, output_limit)
                    stdout_text = stdout_text.replace("\r\n", "\n")
                    guard_notes.append("输出预览超过上限，已从完整输出文件末尾提取 tail 结果")
                except OSError:
                    guard_notes.append("无法读取完整输出文件末尾，tail 结果不完整；请读取完整输出文件")
            else:
                guard_notes.append("完整输出文件不可用，tail 结果不完整")
        else:
            if relay_info and relay_info.get("output_truncated") and kind == "head" and stdout_text.count("\n") < count:
                guard_notes.append("输出预览超过上限，head 结果可能不完整；请读取完整输出文件")
            stdout_text = _apply_local_line_filter(stdout_text, kind, count)
    raw_stdout, raw_stderr = stdout_text, stderr_text
    keep_streams = False
    more_hint = ""
    full_output_path = None
    if relay_info and relay_info.get("output_truncated"):
        keep_streams = True
        refs = [relay_info.get("out"), relay_info.get("err")]
        full_output_path = next((str(path) for path in refs if path), None)
        more_hint = "；完整输出保留在: " + " | ".join(
            _display_path(Path(path)) for path in refs if path)
    elif len(raw_stdout) > output_limit or len(raw_stderr) > min(3000, output_limit // 2):
        # 超长截断：把完整输出落盘，返回里附文件路径（中间被省略部分可用 read_file 续读）
        full_path = _save_full_output(raw_stdout, raw_stderr)
        if full_path is not None:
            full_output_path = str(full_path)
            more_hint = f"；完整输出: {_display_path(full_path)}（可用 read_file 读取）"
        else:
            # 落盘失败：保留中转输出文件并在返回中给出路径
            refs = [relay_info.get("out"), relay_info.get("err")] if relay_info else []
            refs = [path for path in refs if path]
            if refs:
                keep_streams = True
                full_output_path = str(refs[0])
                more_hint = "；完整输出保留在: " + " | ".join(
                    _display_path(Path(path)) for path in refs)
    stdout_text = _truncate_text(raw_stdout, output_limit, "stdout 过长已截断" + more_hint)
    stderr_text = _truncate_text(raw_stderr, min(3000, output_limit // 2), "stderr 过长已截断" + more_hint)
    if relay_info is not None:
        # 输出已读入内存（或已另存完整版）：清理本次中转临时文件，避免日志目录无限堆积
        _cleanup_relay_files(relay_info, include_streams=not keep_streams)
    header = (f"[run_command] shell={shell_kind} | cwd={_display_path(Path(cwd))}"
              f" | exit={exit_code} | 耗时 {elapsed:.1f}s"
              + ((" | 命令超时，进程树已被强制终止" if relay_info and relay_info.get("termination_confirmed")
                  else " | 命令超时，已请求终止进程树，请核实状态") if timed_out else ""))
    fields = {"status": "timeout" if timed_out else "completed" if exit_code == 0 else "failed",
              "exit_code": exit_code, "timed_out": timed_out, "shell": shell_kind,
              "cwd": _display_path(Path(cwd)), "background": False, "pid": None, "job_id": None,
              "completion_path": None,
              "termination_confirmed": relay_info.get("termination_confirmed") if relay_info else None,
              "truncated": bool((relay_info or {}).get("output_truncated") or len(raw_stdout) > output_limit
                                or len(raw_stderr) > min(3000, output_limit // 2))}
    # ③ 失败或可疑时的 cmd 语法提示（分号串联 / && 串联 / Unix 命令名）
    if shell_kind == "cmd":
        guard_notes = guard_notes + _cmd_syntax_hints(
            command, exit_code, stdout_text, stderr_text)
    if not stdout_text and not stderr_text:
        result_text = _append_command_notes(f"{header}\n（无输出）", guard_notes)
        if not structured:
            return result_text
        result = dict(fields)
        result.update({"message": _append_command_notes(f"{header}\n（无输出）", guard_notes),
                       "stdout": "", "stderr": "", "output_path": _display_path(Path(full_output_path)) if full_output_path else None})
        return result
    parts = [header]
    if stdout_text:
        parts.append("--- stdout ---\n" + stdout_text)
    if stderr_text:
        parts.append("--- stderr ---\n" + stderr_text)
    if timed_out:
        parts.append(
            f"[run_command] 已超过 timeout={timeout:g}s；长任务请改用 background=true，"
            "随后用 poll_command 查询结果")
    result_text = _append_command_notes("\n".join(parts), guard_notes)
    if not structured:
        return result_text
    result = dict(fields)
    result.update({"message": _append_command_notes(header, guard_notes),
                   "stdout": stdout_text, "stderr": stderr_text,
                   "output_path": _display_path(Path(full_output_path)) if full_output_path else None})
    return result

# ------------------- run_command 工具定义与执行入口 -------------------






def execute_write_stdin(args):
    from .command_input import send, MAX_INPUT_BYTES
    allowed = {'job_id', 'chars', 'eof', 'cursor', 'wait_seconds', 'request_id'}
    if any(key not in allowed and not key.startswith('_') for key in args):
        raise ValueError('write_stdin 只接受 job_id、chars、eof、cursor、wait_seconds、request_id')
    chars = string_arg(args, 'chars', max_length=16384)
    if len(chars.encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('chars 超出 64KiB')
    request_id = string_arg(args, 'request_id', max_length=128)
    if 'request_id' in args and not request_id:
        raise ValueError('request_id 不能为空')
    job_id = string_arg(args, 'job_id', required=True, max_length=256)
    session_id, agent = str(args.get('_session_id') or ''), str(args.get('_command_owner_agent') or 'main')
    eof = _optional_bool(args, 'eof')
    cursor = string_arg(args, 'cursor', max_length=512) if 'cursor' in args else None
    automatic = bool(args.get('_command_auto_defaults'))
    wait = number_arg(args, 'wait_seconds', 1 if automatic else 0, maximum=30)
    limit = max(512, min(12000, int(args.get('_max_chars') or 12000)))
    if automatic:
        from .command_reads import read
        return read(job_id, session_id, agent, cursor=cursor, wait_seconds=wait, max_chars=limit,
                    send=(lambda position, seconds, codec: send(job_id, session_id, chars, eof, request_id,
                          position, seconds, agent, limit, codec)) if chars or eof or request_id else None)
    return send(job_id, session_id, chars, eof, request_id, cursor or '', wait, agent, limit)


def execute_run_command(tool_args: dict[str, Any]) -> str | dict[str, Any]:
    """内置 run_command 实现：执行终端命令并返回文本结果（同步阻塞）。

    与 MCP 版完全同语义：多 shell、超时杀进程树、超长输出截断并落盘、编码
    自适应、后台分离模式、cmd 家族健壮性补丁。注意：本函数是同步阻塞的，
    调用方必须经 ``asyncio.to_thread`` 放到工作线程执行（与 MCP 工具的
    线程池执行语义一致），避免长时间命令卡死事件循环。
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    action = str(args.get("action") or "run").strip().lower()
    if action in ("poll", "stop"):
        if any(key in args for key in ("command", "shell", "work_dir", "timeout_seconds", "background", "yield_time_ms", "interactive", "tty")):
            raise ValueError("poll/stop 仅接受 job_id；请移除执行命令专用参数")
        tail_lines = args.get("tail_lines", 30)
        cursor = args.get('cursor')
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError('cursor 必须是字符串')
        if action == 'stop' and any(key in args for key in ('cursor', 'wait_seconds', 'encoding')):
            raise ValueError('stop 不接受输出读取参数')
        if cursor is not None and 'tail_lines' in args:
            raise ValueError('cursor 与 tail_lines 不能同时指定')
        automatic = bool(args.get('_command_auto_defaults')) and 'tail_lines' not in args
        if cursor is None and not automatic and any(key in args for key in ('wait_seconds', 'encoding')):
            raise ValueError('wait_seconds/encoding 仅用于 cursor 模式')
        wait_seconds = number_arg(args, 'wait_seconds', 1 if automatic else 0, maximum=30)
        if action == "stop" and "tail_lines" in args:
            raise ValueError("stop 不接受 tail_lines；请在 poll_command 中使用")
        if not isinstance(tail_lines, int) or isinstance(tail_lines, bool) or not 1 <= tail_lines <= 200:
            raise ValueError("tail_lines 必须是 1-200 之间的整数")
        if automatic and action == 'poll':
            from .command_reads import read
            return read(str(args.get('job_id') or '').strip(), str(args.get('_session_id') or ''),
                        str(args.get('_command_owner_agent') or 'main'), cursor=cursor, wait_seconds=wait_seconds,
                        encoding=string_arg(args, 'encoding', max_length=64) if 'encoding' in args else None,
                        max_chars=max(512, min(12000, int(args.get('_max_chars') or 12000))))
        return _command_job_action(action, str(args.get("job_id") or "").strip(),
                                   str(args.get("_session_id") or ""), tail_lines=tail_lines,
                                   max_chars=max(512, min(12000, int(args.get("_max_chars") or 12000))),
                                   cursor=cursor, wait_seconds=wait_seconds, encoding=string_arg(args, 'encoding'))
    if action != "run":
        raise ValueError("action 仅支持 run、poll、stop")
    if "job_id" in args:
        raise ValueError("run 不接受 job_id；请使用 poll_command 或 stop_command")
    if "tail_lines" in args:
        raise ValueError("run 不接受 tail_lines；请在 poll_command 中使用")
    if any(key in args for key in ('cursor', 'wait_seconds', 'encoding')):
        raise ValueError('输出读取参数请在 poll_command 中使用')
    if any(key in args for key in ('chars', 'eof', 'request_id')):
        raise ValueError('输入参数请在 write_stdin 中使用')
    yield_time = int_arg(args, 'yield_time_ms', 1000, maximum=30000) if 'yield_time_ms' in args else None
    tty = _optional_bool(args, 'tty')
    interactive = _optional_bool(args, 'interactive') or tty
    if interactive:
        if _optional_bool(args, 'background'):
            raise ValueError('interactive/tty 与 background=true 不能同时指定')
        if yield_time is None:
            yield_time = 1000
    if yield_time is not None and _optional_bool(args, 'background'):
        raise ValueError('yield_time_ms 与 background=true 不能同时指定')
    return _run_command_impl(
        args.get("command"),
        args.get("shell") or args.get("_default_shell") or "auto",
        args.get("work_dir", ""),
        args.get("timeout_seconds", 120.0),
        _optional_bool(args, "background"),
        args.get("_max_chars"),
        bool(args.get("_structured_output")),
        str(args.get("_session_id") or ""),
        yield_time,
        str(args.get('_command_owner_agent') or 'main'),
        interactive, tty, bool(args.get('_command_auto_defaults')),
    )


def _structured_command_result(message: str) -> dict[str, Any]:
    """Expose stable status fields while retaining the legacy readable output."""
    header = message.splitlines()[0] if message else ""
    exit_matches = list(re.finditer(r"(?:^|\|)\s*exit=(-?\d+)\b", header))
    exit_match = exit_matches[-1] if exit_matches else None
    pid_match = re.search(r"\bpid=(\d+)\b", header)
    job_match = re.search(r"\bjob_id=([A-Za-z0-9_]+)\b", header)
    shell_match = re.search(r"\bshell=([^\s|]+)", header)
    cwd_match = re.search(r"\bcwd=([^|]+)", header)
    background = "后台模式" in header
    result: dict[str, Any] = {
        "message": message,
        "status": "running" if background else "completed",
        "shell": shell_match.group(1) if shell_match else None,
        "cwd": cwd_match.group(1).strip() if cwd_match else None,
        "exit_code": int(exit_match.group(1)) if exit_match else None,
        "timed_out": "命令超时" in header,
        "background": background,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "job_id": job_match.group(1) if job_match else None,
        "truncated": "已截断" in message or "预览超过上限" in message,
        "output_path": None,
        "completion_path": None,
        "stdout": "",
        "stderr": "",
    }
    for line in message.splitlines()[1:]:
        if line.startswith("输出文件: "):
            result["output_path"] = line.removeprefix("输出文件: ").split("（", 1)[0].strip()
        elif line.startswith("结束标记: "):
            result["completion_path"] = line.removeprefix("结束标记: ").split("（", 1)[0].strip()
    if not background:
        if result["timed_out"]:
            result["status"] = "timeout"
        elif result["exit_code"] is not None and result["exit_code"] != 0:
            result["status"] = "failed"
    return result


def try_execute_builtin_command_tool(
    tool_name: str, tool_args: dict[str, Any]
) -> str | dict[str, Any] | None:
    """执行内置终端命令工具；非目标名称返回 ``None`` 交由其他执行器处理。

    异常统一转为 {"error": ...} 结构化结果（模型可见的错误反馈），与
    try_execute_builtin_file_tool 的错误通道保持同构。
    """
    if tool_name not in COMMAND_TOOL_NAMES:
        return None
    try:
        args = {**(tool_args if isinstance(tool_args, dict) else {}), '_command_auto_defaults': True}
        if tool_name == WRITE_STDIN_NAME:
            result = execute_write_stdin(args)
        else:
            if tool_name != RUN_COMMAND_NAME:
                if "action" in args or any(key in args for key in ("command", "shell", "work_dir", "timeout_seconds", "background", "yield_time_ms", "interactive", "tty")):
                    raise ValueError(f"{tool_name} 只接受 job_id 及相应的输出读取参数")
                args = {**args, "action": "poll" if tool_name == POLL_COMMAND_NAME else "stop"}
            result = execute_run_command({**args, "_structured_output": True})
            result = result if isinstance(result, dict) else _structured_command_result(result)
            if tool_name == RUN_COMMAND_NAME:
                from .command_reads import seed
                seed(result, str(args.get('_session_id') or ''), str(args.get('_command_owner_agent') or 'main'))
        if result.get('job_id') and (result.get('status') == 'running' or result.get('has_more')):
            result['next_call'] = {'tool': POLL_COMMAND_NAME, 'arguments': {'job_id': result['job_id']}}
        for key in ('command', 'session_id', 'created_at', 'can_stop'):
            result.pop(key, None)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc), "tool": tool_name}
        if isinstance(exc, TimeoutError):
            result.update(status="timeout", timed_out=True)
        elif isinstance(exc, ToolCancelledError):
            result.update(status="cancelled")
        return result
