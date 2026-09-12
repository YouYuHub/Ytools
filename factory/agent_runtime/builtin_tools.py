"""由服务端本地执行的内置工具。"""
# from __future__ import annotations

import difflib
import fnmatch
# import io
import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


CHECK_TOOL_EXISTS_NAME = "check_tool_exists"
TODO_TOOL_NAME = "todo_write"
ASK_USER_TOOL_NAME = "ask_user"
WRITE_FILE_NAME = "write_file"
EDIT_FILE_NAME = "edit_file"
READ_FILE_NAME = "read_file"
SEARCH_FILES_NAME = "search_files"
READ_MEDIA_NAME = "read_media"
# 内置工具在工具选择（_meta.tool_selection / mcp_servers.json inputs）中的伪服务键：
# 前端把它作为“内置工具”分组渲染在工具模态框首位，保存/加载与 MCP 工具走同一链路
BUILTIN_TOOL_SERVER_KEY = "__builtin__"
# 工具选择中允许出现的内置工具名（check_tool_exists 由后端按外部工具自动注入，不开放手选）
SELECTABLE_BUILTIN_TOOL_NAMES = (
    TODO_TOOL_NAME, ASK_USER_TOOL_NAME, WRITE_FILE_NAME, EDIT_FILE_NAME,
    READ_FILE_NAME, SEARCH_FILES_NAME, READ_MEDIA_NAME,
)
# 前端回答 ask_user 提问时的消息前缀（前端 app.js 中同名拼接，需保持一致）；
# 后端据此识别"覆盖式重新回答"：截断最新提问轮之后的旧回答轮
ASK_ANSWER_PREFIX = "【回答模型提问】"
CHECK_TOOL_EXISTS_DEFINITION = {
    "type": "function",
    "function": {
      "name": CHECK_TOOL_EXISTS_NAME,
      "description": "检查指定工具在当前轮次任务中是否可用（含后端是否存在、是否被用户禁用两种状态），建议某个工具使用失败时才进行检查",
      "parameters": {
        "type": "object",
        "properties": {
          "tool_name": {
            "type": "string",
            "description": "要检查的工具名称",
          }
        },
        "required": ["tool_name"],
      },
    },
}

def inject_builtin_tools(
    selected_tools: list[dict[str, Any]],
    selected_tool_servers: dict[str, str],
    include_todo: bool = False,
    include_ask_user: bool = False,
    include_write_file: bool = False,
    include_edit_file: bool = False,
    include_read_file: bool = False,
    include_search_files: bool = False,
    include_read_media: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """注入本地内置工具：check_tool_exists（选了外部工具时）、todo_write、
    ask_user、write_file、edit_file、read_file、search_files 与 read_media
    （用户在工具选择中勾选时）。"""
    tools = list(selected_tools)
    servers = dict(selected_tool_servers)
    if selected_tools and CHECK_TOOL_EXISTS_NAME not in servers:
        tools.append(CHECK_TOOL_EXISTS_DEFINITION)
        servers[CHECK_TOOL_EXISTS_NAME] = BUILTIN_TOOL_SERVER_KEY
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
    if include_read_file and READ_FILE_NAME not in servers:
        tools.append(READ_FILE_TOOL_DEFINITION)
        servers[READ_FILE_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_search_files and SEARCH_FILES_NAME not in servers:
        tools.append(SEARCH_FILES_TOOL_DEFINITION)
        servers[SEARCH_FILES_NAME] = BUILTIN_TOOL_SERVER_KEY
    if include_read_media and READ_MEDIA_NAME not in servers:
        tools.append(READ_MEDIA_TOOL_DEFINITION)
        servers[READ_MEDIA_NAME] = BUILTIN_TOOL_SERVER_KEY
    return tools, servers


def is_builtin_tool(tool_name: str) -> bool:
    return tool_name in (
        CHECK_TOOL_EXISTS_NAME, TODO_TOOL_NAME, ASK_USER_TOOL_NAME,
        WRITE_FILE_NAME, EDIT_FILE_NAME, READ_FILE_NAME, SEARCH_FILES_NAME,
        READ_MEDIA_NAME,
    )


TODO_TOOL_DEFINITION = {
    "type": "function",
    "function": {
      "name": TODO_TOOL_NAME,
      "description": (
          "写入或更新当前任务计划（todo list）。"
          "用于跟踪需要多个步骤、较长时间或多次工具调用才能完成的任务。"
          "简单的一次性任务不要使用。"
          "每次调用必须提交完整的 Todo 列表，新的列表会全量覆盖旧列表。"
          "开始执行某个步骤前将其设为 in_progress，完成后设为 done，"
          "根据实际执行结果及时调整后续步骤。"
          "除非计划发生变化，否则保留已有步骤和已完成步骤。"
          "执行过程中可以新增、删除或调整后续步骤；保持正确步骤的 id 不变。"
          "同一时间最多只能有一个步骤处于 in_progress 状态。"
      ),
      "parameters": {
        "type": "object",
        "properties": {
          "todos": {
            "type": "array",
            "description": (
                "当前完整的任务计划列表。"
                "每次调用都会全量覆盖之前的列表，最多 20 项。"
            ),
            "maxItems": 20,
            "items": {
              "type": "object",
              "properties": {
                "id": {
                  "type": "string",
                  "description": "Todo 唯一标识。创建后保持不变，用于标识同一个任务步骤。",
                },
                "content": {
                  "type": "string",
                  "description": "步骤内容（一句话，祈使句，≤200 字符）",
                },
                "status": {
                  "type": "string",
                  "enum": ["pending", "in_progress", "done"],
                  "description": "pending=待办 in_progress=进行中 done=已完成",
                },
              },
              "required": ["id", "content", "status"],
            },
          }
        },
        "required": ["todos"],
      },
    },
}

_TODO_VALID_STATUSES = ("pending", "in_progress", "done")
_TODO_MAX_ITEMS = 20
_TODO_MAX_CONTENT_CHARS = 200


def _next_todo_id(existing: set[str]) -> str:
    """分配下一个未占用的自增数字 id（字符串形式，"1"、"2"...）。"""
    num = 1
    while str(num) in existing:
        num += 1
    return str(num)


def normalize_todo_items(
    raw: Any,
    prev_items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]] | None, list[str] | None, str | None]:
    """校验并规整模型提交的 todo 列表（唯一校验入口）。

    返回 ``(items, notices, error_reason)``：
    - 成功：items 为规整后的计划（保证 id 唯一且非空、in_progress ≤ 1），
      notices 为自动修正告警，error_reason 为 None；
    - 失败：items/notices 为 None，error_reason 描述首个硬性错误（含条目
      序号与具体值），供工具结果直接回传，帮助模型一次性纠正。

    硬性校验（拒绝并给原因）：非数组/条目非对象/content 为空或超长/
    status 非法/超过 20 项。自动修复（放行并告警）：id 缺失或为空
    （同 content+status 匹配上一版计划则继承其 id，否则本地自增分配）、
    id 重复（为重复者重新分配）、多个 in_progress（保留首个，其余改
    pending）。兼容旧模型不按新 Schema 提交时缺失 id 的情况。
    """
    if not isinstance(raw, list):
        return None, None, "todos 需要是对象数组，且每次调用提交完整列表（全量覆盖）"
    if len(raw) > _TODO_MAX_ITEMS:
        return None, None, (
            f"todo 数量超过上限：收到 {len(raw)} 项，最多 {_TODO_MAX_ITEMS} 项，"
            "请合并同类步骤或拆分任务"
        )
    # 上一版计划索引 (content, status) -> id，供缺失 id 的条目继承
    prev_by_key: dict[tuple[str, str], str] = {}
    for prev in prev_items or []:
        if not isinstance(prev, dict):
            continue
        prev_id = str(prev.get("id") or "").strip()
        prev_content = str(prev.get("content") or "").strip()
        prev_status = str(prev.get("status") or "pending").strip()
        if prev_id and prev_content:
            prev_by_key[(prev_content, prev_status)] = prev_id
    items: list[dict[str, str]] = []
    notices: list[str] = []
    used_ids: set[str] = set()
    id_missing = id_duplicated = in_progress_fixed = 0
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            return None, None, f"第 {index} 项必须是对象（含 id/content/status 字段）"
        content = str(entry.get("content") or "").strip()
        if not content:
            return None, None, f"第 {index} 项 content 不能为空"
        if len(content) > _TODO_MAX_CONTENT_CHARS:
            return None, None, (
                f"第 {index} 项 content 超长（{len(content)} > "
                f"{_TODO_MAX_CONTENT_CHARS} 字符），请压缩为一句祈使句"
            )
        status = str(entry.get("status") or "pending").strip().lower()
        if status not in _TODO_VALID_STATUSES:
            return None, None, (
                f"第 {index} 项 status 非法：{status!r}，"
                "仅支持 pending / in_progress / done"
            )
        raw_id = str(entry.get("id") or "").strip()
        if raw_id:
            if raw_id in used_ids:
                id_duplicated += 1
                raw_id = _next_todo_id(used_ids)
        else:
            inherited = prev_by_key.get((content, status))
            if inherited and inherited not in used_ids:
                raw_id = inherited
            else:
                raw_id = _next_todo_id(used_ids)
            id_missing += 1
        used_ids.add(raw_id)
        if status == "in_progress" and any(i["status"] == "in_progress" for i in items):
            in_progress_fixed += 1
            status = "pending"
        items.append({"id": raw_id, "content": content, "status": status})
    if id_missing:
        notices.append(
            f"{id_missing} 个步骤未携带 id，已自动分配/继承"
            "（下次调用请保持相同步骤的 id 不变）"
        )
    if id_duplicated:
        notices.append(f"{id_duplicated} 个步骤 id 重复，已为重复项重新分配")
    if in_progress_fixed:
        notices.append(
            f"{in_progress_fixed} 个步骤多余地标记为 in_progress"
            "（同一时间最多一个），仅保留首个，其余已改为 pending"
        )
    return items, notices, None


def execute_builtin_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    available_tool_names: set[str] | list[str],
    available_tool_servers: dict[str, str],
    enabled_tool_names: set[str] | list[str] | None = None,
) -> dict[str, Any] | None:
    """执行内置工具；非内置名称返回 ``None`` 交由 MCP 执行器处理。

    available_* 为后端实时注册表全集（工具是否存在），enabled_* 为当前轮
    用户启用的工具集合（是否可用）。被禁用 ≠ 不存在：exists 仍为 True，
    但补充 disabled 状态与提示文案，避免模型误以为被禁用工具可再次调用。
    """
    if tool_name != CHECK_TOOL_EXISTS_NAME:
        return None
    query_name = ""
    if isinstance(tool_args, dict):
        query_name = str(tool_args.get("tool_name", "")).strip()
    # 内置工具不进 MCP 注册表（调用方传入的 available_tool_names 只含 MCP 工具），
    # 但它们真实存在、可在工具选择中勾选：这里并入内置工具全集一起识别，
    # 避免查询 ask_user / todo_write / read_file 等时误报"不存在"。
    all_known_names = (
        {str(n).strip() for n in available_tool_names if str(n).strip()}
        | set(SELECTABLE_BUILTIN_TOOL_NAMES)
        | {CHECK_TOOL_EXISTS_NAME}
    )
    exists = bool(query_name in all_known_names) if query_name else False
    # 当前轮启用集合缺省回退全集（旧调用方未传时行为不变）
    enabled_names = (
        {str(n).strip() for n in enabled_tool_names if str(n).strip()}
        if enabled_tool_names is not None
        else None
    )
    disabled = exists and enabled_names is not None and query_name not in enabled_names
    if not exists:
        message = f"后端实时工具列表中不存在名为 {query_name} 的工具"
        # 模型常因大小写/下划线写错工具名：给出最接近候选帮助一次纠正
        close = difflib.get_close_matches(
            query_name, sorted(all_known_names), n=3, cutoff=0.6
        )
        if close:
            message += f"；名称最接近的已有工具：{'、'.join(close)}（注意大小写与下划线）"
    elif disabled:
        message = (
            f"{query_name} 在后端已注册但未被当前轮次任务启用（用户禁用了该工具），"
            "本轮无法使用；请改用其他方式完成，或提示用户在工具选择中重新勾选该工具"
        )
    else:
        message = f"{query_name} 当前轮次任务中可用"
    if not exists:
        server_value = ""
    elif query_name in available_tool_servers:
        server_value = available_tool_servers[query_name]
    elif query_name in SELECTABLE_BUILTIN_TOOL_NAMES or query_name == CHECK_TOOL_EXISTS_NAME:
        server_value = BUILTIN_TOOL_SERVER_KEY
    else:
        server_value = ""
    return {
        "tool_name": query_name,
        "exists": exists,
        "disabled": disabled,
        "server": server_value,
        "message": message,
    }


# --------------------------- ask_user：向用户提问 ---------------------------

ASK_USER_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": ASK_USER_TOOL_NAME,
        "description": (
            "向用户提问并等待回答：前端会弹出交互卡片，用户可点选选项或在输入框自由作答，"
            "回答会作为下一条用户消息发送给你。"
            "适用场景：任务存在关键分歧（方案二选一、缺失必要参数、执行不可逆操作前确认）；"
            "一次提出 1-5 个问题，问题要具体、选项要互斥且可直接执行；"
            "需要用户同时选择多项时把该题的 multiple 设为 true。"
            "注意：调用后当前任务会暂停等待用户回答，收到用户的回答消息后再继续任务；"
            "不要用本工具进行闲聊或询问常识性问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "问题列表（1-5 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "问题内容（一句话，具体明确，≤200 字符）",
                            },
                            "options": {
                                "type": "array",
                                "description": "候选选项列表（可选，2-6 项，每项 ≤100 字符；用户也可以不选选项而自由输入）",
                                "items": {"type": "string"},
                            },
                            "multiple": {
                                "type": "boolean",
                                "description": "该题是否允许多选（用户可同时选择多个选项）；默认 false 单选",
                            },
                        },
                        "required": ["question"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
}

_ASK_MAX_QUESTIONS = 3
_ASK_MAX_QUESTION_CHARS = 200
_ASK_MAX_OPTIONS = 6
_ASK_MAX_OPTION_CHARS = 100


def normalize_ask_questions(raw: Any) -> list[dict[str, Any]] | None:
    """校验并规整模型提交的提问列表；整体非法返回 None。

    规整规则：question 非空且 ≤200 字符；options 可选，规整后保留 0-6 个
    非空选项（每项 ≤100 字符），全部无效时视为未提供选项。
    容错：questions 传成单个对象时自动包一层数组；传成 JSON 字符串时尝试解析。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    if not raw or len(raw) > _ASK_MAX_QUESTIONS:
        return None
    questions: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        question = str(entry.get("question") or "").strip()
        if not question or len(question) > _ASK_MAX_QUESTION_CHARS:
            return None
        raw_options = entry.get("options")
        options: list[str] = []
        if isinstance(raw_options, list):
            for option in raw_options:
                text = str(option or "").strip()
                if not text or len(text) > _ASK_MAX_OPTION_CHARS:
                    continue
                if text not in options:
                    options.append(text)
            options = options[:_ASK_MAX_OPTIONS]
        questions.append({
            "question": question,
            "options": options,
            "multiple": bool(entry.get("multiple")),
        })
    return questions


# ------------------- write_file / edit_file：内置文件编辑工具 -------------------
# 与 MCP sys_tools_server 的同名工具保持语义一致（相对路径基于会话工作目录，
# worker 进程已 os.chdir），但结果为结构化 dict：除人类可读 message 外携带
# path/action/changed_bytes 等机器可读字段，为后续文件 diff 功能预留数据基础。

_WRITE_EDIT_MAX_CHARS = 2 * 1024 * 1024   # 单次写入/替换内容上限（防超大参数）
_READ_CHUNK = 8 * 1024 * 1024             # 文件读取上限：超过按二进制拒绝前先截断

WRITE_FILE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": WRITE_FILE_NAME,
        "description": (
            "写入文本文件（整文件覆盖或追加，自动创建多级目录）"
            "参数：\n"
            "    full_file_name: 文件路径；不存在则创建，父目录不存在自动创建；相对路径基于会话工作目录\n"
            "    content:        要写入的完整内容（覆盖模式会清空原内容）\n"
            "    encoding:       文件编码，默认 utf-8\n"
            "    append:         true=在文件末尾追加；false（默认）=整文件覆盖\n"
            "返回：\n"
            "    写入结果说明（含绝对路径与字符数）；只改文件局部内容时优先用 edit_file\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_file_name": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的完整内容（覆盖模式会清空原内容）"},
                "encoding": {"type": "string", "description": "文件编码，默认 utf-8", "default": "utf-8"},
                "append": {"type": "boolean", "description": "true=在文件末尾追加；false（默认）=整文件覆盖", "default": False},
            },
            "required": ["full_file_name", "content"],
        },
    },
}

EDIT_FILE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": EDIT_FILE_NAME,
        "description": (
            "按精确字符串替换编辑文件（类似查找替换，比按行号改写更安全）\n"
            "参数：\n"
            "    full_file_name: 文件路径，相对路径基于会话工作目录\n"
            "    old_string:     要被替换的原文，必须与文件内容完全一致（建议从 read_file 的输出复制）\n"
            "    new_string:     替换后的新内容；传空串表示删除 old_string\n"
            "    replace_all:    old_string 出现多次时是否全部替换；默认 false（多处匹配会报错并提示加长上下文）\n"
            "    encoding:       指定文件编码（如 gbk）；留空自动尝试 utf-8 → gbk，并按读到的编码写回\n"
            "返回：\n"
            "    替换结果说明（替换处数/换行风格/编码）；old_string 为 0 处或多处歧义时报错\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_file_name": {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "要被替换的原文，必须与文件内容完全一致"},
                "new_string": {"type": "string", "description": "替换后的新内容；传空串表示删除 old_string"},
                "replace_all": {"type": "boolean", "description": "出现多次时是否全部替换；默认 false", "default": False},
                "encoding": {"type": "string", "description": "指定文件编码（如 gbk）；留空自动尝试 utf-8 → gbk", "default": ""},
            },
            "required": ["full_file_name", "old_string", "new_string"],
        },
    },
}


def _decode_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """按候选编码依次尝试严格解码，返回 (文本, 实际使用的编码)。
    显式指定 encoding 时直接按该编码宽松解码（保证不抛异常）。"""
    if encoding:
        return data.decode(encoding, errors="replace"), encoding
    for candidate in candidates:
        try:
            return data.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(candidates[-1], errors="replace"), candidates[-1]


def _is_probably_binary(data: bytes) -> bool:
    """前 8KB 中出现空字节即视为二进制文件。"""
    return b"\x00" in data[:8192]


def _display_path(path: Path) -> str:
    """统一用 / 作为路径分隔符展示，避免模型转义反斜杠出错。"""
    return str(path).replace("\\", "/")


def execute_write_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 write_file 实现：语义对齐 MCP 同名工具，返回结构化结果。"""
    full_file_name = str((tool_args or {}).get("full_file_name") or "").strip()
    if not full_file_name:
        raise ValueError("full_file_name 不能为空")
    content = tool_args.get("content")
    if not isinstance(content, str):
        raise ValueError("content 必须为字符串")
    if len(content) > _WRITE_EDIT_MAX_CHARS:
        raise ValueError(f"content 超过 {_WRITE_EDIT_MAX_CHARS // (1024 * 1024)}MB 上限")
    encoding = str(tool_args.get("encoding") or "utf-8").strip() or "utf-8"
    append = bool(tool_args.get("append"))
    path = Path(full_file_name).expanduser()
    dir_path = path.parent if str(path.parent).strip() else Path(".")
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    size_before = path.stat().st_size if existed else 0
    mode = "a" if append else "w"
    with open(path, mode, encoding=encoding, newline="") as file:
        file.write(content)
    action = "追加" if append else "写入"
    return {
        "message": f"[write_file] 已{action} {len(content)} 字符 → {_display_path(path.resolve())}",
        "path": _display_path(path.resolve()),
        "action": "append" if append else "overwrite",
        "created": not existed,
        "size_before": size_before,
        "size_after": path.stat().st_size,
        "chars_written": len(content),
        "encoding": encoding,
    }


def execute_edit_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 edit_file 实现：语义对齐 MCP 同名工具，返回结构化结果。

    换行归一化匹配 + 原风格写回、0 匹配给最接近候选行提示等行为均与 MCP 版一致；
    额外返回 replacements/eol_style/encoding/matched_lines 等字段供后续 diff 使用。
    """
    args = tool_args or {}
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    if not isinstance(old_string, str) or not old_string:
        raise ValueError("old_string 不能为空；如需清空文件请使用 write_file 写入空内容")
    if not isinstance(new_string, str):
        raise ValueError("new_string 必须为字符串（可为空串表示删除）")
    replace_all = bool(args.get("replace_all"))
    encoding = str(args.get("encoding") or "").strip()
    full_file_name = str(args.get("full_file_name") or "").strip()
    if not full_file_name:
        raise ValueError("full_file_name 不能为空")
    path = Path(full_file_name).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{path} 不存在或不是文件")
    data = path.read_bytes()[:_READ_CHUNK]
    if _is_probably_binary(data):
        raise ValueError(f"{path} 疑似二进制文件，不支持文本编辑")
    original, used_encoding = _decode_bytes_used(data, encoding)
    eol = "\r\n" if "\r\n" in original else "\n"
    # 匹配在换行归一化文本上进行，写回时还原原文件的换行风格
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    old_norm = old_string.replace("\r\n", "\n")
    new_norm = new_string.replace("\r\n", "\n")
    count = text.count(old_norm)
    if count == 0:
        # 0 匹配时给出最接近的候选行，帮助模型快速定位"凭记忆写错"的差异
        hint = ""
        close = difflib.get_close_matches(
            old_norm.strip(), [line.strip() for line in text.split("\n")], n=2, cutoff=0.6
        )
        if close:
            shown = " / ".join(f"「{c[:120]}」" for c in close)
            hint = f"；文件中最相似的行：{shown}（注意空格/缩进/全角差异）"
        raise ValueError(
            f"old_string 在文件中 0 处匹配；请先用 read_file 核对内容（空格/缩进/换行必须完全一致）{hint}"
        )
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string 在文件中匹配到 {count} 处，存在歧义；请提供更长的上下文使其唯一，或传 replace_all=true 全部替换"
        )
    updated = text.replace(old_norm, new_norm) if replace_all else text.replace(old_norm, new_norm, 1)
    with open(path, "w", encoding=used_encoding, newline="") as file:
        file.write(updated.replace("\n", eol))
    replaced = count if replace_all else 1
    style = "CRLF" if eol == "\r\n" else "LF"
    matched_lines = len(old_norm.split("\n"))
    return {
        "message": (
            f"[edit_file] 已在 {_display_path(path)} 中替换 {replaced} 处"
            f"（换行风格 {style}，编码 {used_encoding}）"
        ),
        "path": _display_path(path),
        "action": "replace",
        "replacements": replaced,
        "matched_occurrences": count,
        "eol_style": style,
        "encoding": used_encoding,
        "matched_lines": matched_lines,
    }


def try_execute_builtin_file_tool(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any] | None:
    """执行内置文件编辑工具；非目标名称返回 None 交由其他执行器处理。

    异常统一转为 {"error": ...} 结构化结果（模型可见的错误反馈），
    与 todo/ask_user 的 builtin_results 错误通道保持同构。
    """
    if tool_name == WRITE_FILE_NAME:
        executor = execute_write_file
    elif tool_name == EDIT_FILE_NAME:
        executor = execute_edit_file
    elif tool_name == READ_FILE_NAME:
        executor = execute_read_file
    elif tool_name == SEARCH_FILES_NAME:
        executor = execute_search_files
    else:
        return None
    try:
        return executor(tool_args if isinstance(tool_args, dict) else {})
    except Exception as exc:
        return {"error": str(exc), "tool": tool_name}


# ------------------- read_file / search_files：内置读取与检索工具 -------------------
# 与 MCP sys_tools_server 的同名工具保持语义一致（相对路径基于会话工作目录）；
# read_file 返回结构化结果（含 path/total_lines/range 等字段），search_files
# 返回纯文本（与 MCP 版相同的区块格式，便于模型直接阅读）。

_READ_FILE_MAX_LINES = 2000
_READ_FILE_MAX_CHARS = 60000
_SEARCH_MAX_RESULTS_LIMIT = 200
_SEARCH_CONTEXT_MAX = 5
_SEARCH_DEFAULT_FILE_MB = 2.0
# 模型未传 max_depth 时的默认递归深度（与 schema 描述一致）。
# 注意：内置工具走 dict 参数分发，schema 的 default 不会被框架自动应用，
# 缺省值必须在这里显式兜底，否则会退化成 0（只扫当前目录一层）。
_SEARCH_DEFAULT_MAX_DEPTH = 6
# 跨文件搜索/遍历时默认跳过的目录名（含各语言依赖与构建产物目录）
_SKIP_DIR_NAMES = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build",
}

READ_FILE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": READ_FILE_NAME,
        "description": (
            "读取文本文件内容（自动尝试 utf-8/gbk 编码，带行号与读取进度提示）\n"
            "参数：\n"
            "    full_file_name:    文件路径，相对路径基于会话工作目录；传入目录会报错\n"
            "    start_line:        起始行（从 1 开始），默认 1\n"
            "    end_line:          结束行（包含该行），0 或负数表示读到文件末尾；默认 0\n"
            "    encoding:          指定文件编码（如 utf-8、gbk）；留空自动尝试 utf-8 → gbk\n"
            "    show_line_numbers: 是否带 \"行号| 内容\" 前缀显示，默认 true；行号可用于后续 edit_file 定位\n"
            "    char_offset:       单行超长时的字符偏移（按返回的 char_offset 提示续读）；普通文件忽略该参数\n"
            "返回：\n"
            "    文件内容；末尾附 [read_file] 摘要（总行数/本次范围/编码），未读完会提示分段读取。\n"
            "    单次最多返回 2000 行；二进制文件拒绝读取；\n"
            "    单行超过 6 万字符（如压缩大 JSON）时自动转为字符分块模式，按提示的 char_offset 续读\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_file_name": {"type": "string", "description": "文件路径"},
                "start_line": {"type": "integer", "description": "起始行（从 1 开始），默认 1", "default": 1},
                "end_line": {"type": "integer", "description": "结束行（包含该行），0 或负数表示读到文件末尾；默认 0", "default": 0},
                "encoding": {"type": "string", "description": "指定文件编码（如 utf-8、gbk）；留空自动尝试 utf-8 → gbk", "default": ""},
                "show_line_numbers": {"type": "boolean", "description": "是否带 \"行号| 内容\" 前缀显示，默认 true", "default": True},
                "char_offset": {"type": "integer", "description": "单行超长时的字符偏移（按返回提示续读）；普通文件忽略", "default": 0},
            },
            "required": ["full_file_name"],
        },
    },
}

SEARCH_FILES_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": SEARCH_FILES_NAME,
        "description": (
            "跨文件正则搜索（类似 grep）：在目录下的文本文件中逐行匹配\n"
            "参数：\n"
            "    pattern:       正则表达式（is_regex=false 时按普通文本查找）\n"
            "    dir_path:      搜索根目录，默认当前目录（会话工作目录）\n"
            "    file_pattern:  文件名通配符过滤，例如 *.py、*.md；空串表示所有文件\n"
            "    ignore_case:   是否忽略大小写，默认 false\n"
            "    is_regex:      pattern 是否按正则解析，默认 true；false 时按字面文本匹配\n"
            "    context_lines: 每处匹配附带上下文行数（0-5），默认 0\n"
            "    max_results:   最多返回的匹配区块数（1-200），默认 50\n"
            "    max_depth:     递归深度，0=仅当前目录；默认 6\n"
            "    max_file_mb:   单文件大小上限 MB（默认 2；设 0 表示不限制，可搜索大 JSON/日志）\n"
            "返回：\n"
            "    \"文件路径:行号\" 区块列表，> 前缀标记命中行；头部附扫描/命中统计。\n"
            "    自动跳过二进制文件、隐藏目录及 node_modules/__pycache__/.git 等目录；\n"
            "    超过 max_file_mb 的文件默认跳过并在头部统计中提示\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式（is_regex=false 时按普通文本查找）"},
                "dir_path": {"type": "string", "description": "搜索根目录，默认当前目录", "default": "."},
                "file_pattern": {"type": "string", "description": "文件名通配符过滤，例如 *.py；空串表示所有文件", "default": ""},
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false", "default": False},
                "is_regex": {"type": "boolean", "description": "pattern 是否按正则解析，默认 true", "default": True},
                "context_lines": {"type": "integer", "description": "每处匹配附带上下文行数（0-5），默认 0", "default": 0},
                "max_results": {"type": "integer", "description": "最多返回的匹配区块数（1-200），默认 50", "default": 50},
                "max_depth": {"type": "integer", "description": "递归深度，0=仅当前目录；默认 6", "default": 6},
                "max_file_mb": {"type": "number", "description": "单文件大小上限 MB（默认 2；0=不限制）", "default": 2},
            },
            "required": ["pattern"],
        },
    },
}


def _decode_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """按候选编码依次尝试严格解码，返回 (文本, 实际使用的编码)。
    显式指定 encoding 时直接按该编码宽松解码（保证不抛异常）。"""
    if encoding:
        return data.decode(encoding, errors="replace"), encoding
    for candidate in candidates:
        try:
            return data.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(candidates[-1], errors="replace"), candidates[-1]


def _is_probably_binary(data: bytes) -> bool:
    """前 8KB 中出现空字节即视为二进制文件。"""
    return b"\x00" in data[:8192]


def _display_path(path: Path) -> str:
    """统一用 / 作为路径分隔符展示，避免模型转义反斜杠出错。"""
    return str(path).replace("\\", "/")


def _resolve_dir_path(dir_path: Any) -> Path:
    """解析目录参数：空串回退当前目录（生成任务中即会话工作目录）。"""
    text = str(dir_path).strip() if dir_path else ""
    candidate = Path(text).expanduser() if text else Path(".")
    if not candidate.exists():
        raise FileNotFoundError(f"路径不存在: {candidate}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"不是目录: {candidate}")
    return candidate.resolve()


def _walk_entries(root: Path, max_depth: int):
    """遍历目录树，yield (路径, "file"|"dir")。
    跳过隐藏目录与 _SKIP_DIR_NAMES 中的目录；max_depth 为相对 root 的深度上限（0=仅当前目录）。"""
    root_depth = len(root.parts)
    for current, dir_names, file_names in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        keep_dirs = sorted(
            name for name in dir_names
            if name not in _SKIP_DIR_NAMES and not name.startswith(".")
        )
        for name in keep_dirs:
            yield current_path / name, "dir"
        # 深度用尽时剪枝，不再下钻（目录本身已在上面列出）
        dir_names[:] = keep_dirs if depth < max_depth else []
        for name in sorted(file_names):
            yield current_path / name, "file"


def execute_read_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 read_file 实现：语义对齐 MCP 同名工具，返回结构化结果。"""
    args = tool_args or {}
    full_file_name = str(args.get("full_file_name") or "").strip()
    if not full_file_name:
        raise ValueError("full_file_name 不能为空")
    path = Path(full_file_name).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在")
    if not path.is_file():
        raise IsADirectoryError(f"{path} 不是文件")
    data = path.read_bytes()
    if _is_probably_binary(data):
        raise ValueError(f"{_display_path(path)} 疑似二进制文件（{len(data)} 字节），拒绝按文本读取")
    text, used_encoding = _decode_bytes_used(data, str(args.get("encoding") or ""))
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(args.get("start_line") or 1))
    raw_end = int(args.get("end_line") or 0)
    end = total if raw_end <= 0 else min(raw_end, total)
    if start > total:
        raise ValueError(f"start_line={start} 超出文件总行数 {total}，无内容可读")
    if end < start:
        raise ValueError(f"end_line={raw_end} 小于 start_line={start}，无内容可读")
    selected = lines[start - 1:end]
    limited = False
    if len(selected) > _READ_FILE_MAX_LINES:
        selected = selected[:_READ_FILE_MAX_LINES]
        limited = True
    # 单行超长（如压缩过的大 JSON/日志）时按字符分块返回，避免头尾截断丢失中间内容
    if len(selected) == 1 and len(selected[0]) > _READ_FILE_MAX_CHARS:
        line_text = selected[0]
        offset = max(0, int(args.get("char_offset") or 0))
        chunk = line_text[offset:offset + _READ_FILE_MAX_CHARS]
        if not chunk:
            raise ValueError(f"char_offset={offset} 已超出该行长度 {len(line_text)}，无内容可读")
        next_offset = offset + len(chunk)
        footer = (f"[read_file] {_display_path(path)} | 单行超长（共 {len(line_text)} 字符，编码 {used_encoding}）"
                  f" | 本次返回第 {offset + 1}-{next_offset} 字符")
        if next_offset < len(line_text):
            footer += f" | 未读完，请用 char_offset={next_offset} 继续读取"
        return {
            "message": footer,
            "content": chunk,
            "path": _display_path(path),
            "encoding": used_encoding,
            "mode": "char_chunk",
            "char_offset": offset,
            "next_char_offset": next_offset if next_offset < len(line_text) else None,
            "line_length": len(line_text),
        }
    if bool(args.get("show_line_numbers", True)):
        width = len(str(start + len(selected) - 1))
        body = "\n".join(f"{no:>{width}}| {line}" for no, line in enumerate(selected, start=start))
    else:
        body = "\n".join(selected)
    char_limited = len(body) > _READ_FILE_MAX_CHARS
    if char_limited:
        head = int(_READ_FILE_MAX_CHARS * 2 / 3)
        tail = max(0, _READ_FILE_MAX_CHARS - head)
        body = f"{body[:head]}\n...[文件内容过长已截断，中间约 {len(body) - head - tail} 字符已省略]...\n{body[-tail:]}"
    last_line = start + len(selected) - 1
    footer = f"[read_file] {_display_path(path)} | 共 {total} 行 | 本次第 {start}-{last_line} 行 | 编码 {used_encoding}"
    if last_line < total or limited or char_limited:
        footer += " | 未读完，可调整 start_line/end_line 继续读取"
    return {
        "message": body + ("\n" + footer if body else footer),
        "content": body,
        "path": _display_path(path),
        "encoding": used_encoding,
        "mode": "lines",
        "total_lines": total,
        "range_start": start,
        "range_end": last_line,
        "truncated": bool(limited or char_limited),
        "has_more": last_line < total,
    }


def _format_match_block(path: Path, lines: list, hit_indexes: list, context: int) -> list:
    """把（相邻+上下文合并后的）命中区间格式化为 "路径:行号" 区块。"""
    ranges = []
    for idx in hit_indexes:
        if ranges and idx <= ranges[-1][1] + context + 1:
            ranges[-1][1] = idx
        else:
            ranges.append([idx, idx])
    blocks = []
    for start, end in ranges:
        low = max(0, start - context)
        high = min(len(lines) - 1, end + context)
        header = f"{_display_path(path)}:{start + 1}" + (f"-{end + 1}" if end != start else "")
        body = []
        for no in range(low, high + 1):
            marker = ">" if start <= no <= end else " "
            body.append(f"{marker}{no + 1}| {lines[no][:300]}")
        blocks.append(header + "\n" + "\n".join(body))
    return blocks


def execute_search_files(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 search_files 实现：语义对齐 MCP 同名工具，返回纯文本结果。"""
    args = tool_args or {}
    pattern = (str(args.get("pattern") or "")).strip()
    if not pattern:
        raise ValueError("pattern 不能为空")
    root = _resolve_dir_path(args.get("dir_path") or ".")
    flags = re.IGNORECASE if bool(args.get("ignore_case")) else 0
    # 键名兼容：schema 定义为 is_regex；旧实现误读 isregex 导致该参数被静默忽略
    is_regex_raw = args.get("is_regex")
    if is_regex_raw is None:
        is_regex_raw = args.get("isregex")
    isregex = True if is_regex_raw is None else bool(is_regex_raw)
    if isregex:
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"正则表达式不合法: {exc}")
    else:
        regex = re.compile(re.escape(pattern), flags)
    context = max(0, min(int(args.get("context_lines") or 0), _SEARCH_CONTEXT_MAX))
    limit = max(1, min(int(args.get("max_results") or 50), _SEARCH_MAX_RESULTS_LIMIT))
    name_filter = (str(args.get("file_pattern") or "")).strip()
    try:
        size_limit = float(args.get("max_file_mb")) if args.get("max_file_mb") is not None else _SEARCH_DEFAULT_FILE_MB
    except (TypeError, ValueError):
        size_limit = _SEARCH_DEFAULT_FILE_MB
    if size_limit <= 0:
        size_limit = 0.0
    blocks = []
    total_matches = 0
    files_scanned = 0
    files_matched = 0
    skipped_large = 0
    try:
        raw_depth = args.get("max_depth")
        max_depth = int(raw_depth) if raw_depth is not None else _SEARCH_DEFAULT_MAX_DEPTH
    except (TypeError, ValueError):
        max_depth = _SEARCH_DEFAULT_MAX_DEPTH
    for path, kind in _walk_entries(root, max(0, max_depth)):
        if kind != "file":
            continue
        if name_filter and not fnmatch.fnmatch(path.name, name_filter):
            continue
        try:
            file_size = path.stat().st_size
            if size_limit and file_size > size_limit * 1024 * 1024:
                skipped_large += 1
                continue  # 跳过超过大小上限的文件
            data = path.read_bytes()
        except OSError:
            continue
        if _is_probably_binary(data):
            continue
        files_scanned += 1
        decoded, _used = _decode_bytes_used(data)
        lines = decoded.splitlines()
        hit_indexes = [idx for idx, line in enumerate(lines) if regex.search(line)]
        if not hit_indexes:
            continue
        files_matched += 1
        total_matches += len(hit_indexes)
        if len(blocks) < limit:
            room = limit - len(blocks)
            block_group = _format_match_block(path, lines, hit_indexes, context)
            blocks.extend(block_group[:room])
    header = (f"[search_files] 正则: {regex.pattern} | 目录: {_display_path(root)}"
              f" | 扫描 {files_scanned} 个文本文件，命中 {files_matched} 个文件 / {total_matches} 处")
    if skipped_large:
        header += f" | 跳过 {skipped_large} 个超过 {size_limit:g}MB 的文件（可用 max_file_mb 调整）"
    if not blocks:
        message = header + "\n（无匹配结果）"
    else:
        parts = [header] + blocks
        if len(blocks) >= limit:
            parts.append(f"[已达到 max_results={limit} 上限；可缩小目录/加 file_pattern 过滤或提高 max_results]")
        message = "\n".join(parts)
    return {
        "message": message,
        "path": _display_path(root),
        "pattern": regex.pattern,
        "files_scanned": files_scanned,
        "files_matched": files_matched,
        "total_matches": total_matches,
        "truncated": len(blocks) >= limit,
    }


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
# GIF 动图跳过降采样；大小上限图片/音频 20MB、视频 500MB。

READ_MEDIA_MAX_ITEMS = 5
_READ_MEDIA_QUALITY_MIN = 50
_READ_MEDIA_QUALITY_MAX = 100

READ_MEDIA_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": READ_MEDIA_NAME,
        "description": (
            "读取媒体文件（本地/网络/用户上传统一入口），把数据回传给你（多模态部件）"
            "以便观察内容，最多 5 个。\n"
            "参数：\n"
            "    references: 媒体来源列表，支持三类引用（可混用）：\n"
            "                1) media:// 引用（用户消息附件中已展示的引用，如 "
            "media://xxx_abc12345.png，不要臆造）；\n"
            "                2) 本地文件路径（绝对路径如 C:/data/x.png，或相对当前"
            "工作目录的路径；支持 png/jpg/gif/webp/bmp 等图片与常见音频视频格式）；\n"
            "                3) http(s) 网络直链（公开可访问的图片/音频/视频文件）；\n"
            "                单个字符串视为单个引用\n"
            "    quality:    图片清晰度 50-100（越大越清晰/占用越大，默认 85）；"
            "仅对超过 2MB 的大图生效（小图按原样回传）\n"
            "返回：\n"
            "    文本说明（读取了哪些媒体、来源类型、是否降采样/超限跳过）；"
            "数据本身以 user 消息多模态部件注入后续请求，不在本文本内\n"
            "注意：\n"
            "    视频/音频仅回传元信息与部分模型的有限支持，读取结果以图片为主；\n"
            "    已读取过的引用无需重复读取（同一轮上下文已注入）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "references": {
                    "type": "array",
                    "description": (
                        "媒体来源列表（最多 5 个）：media:// 引用 / 本地文件路径 / "
                        "http(s) 网络直链，可混用"
                    ),
                    "items": {"type": "string"},
                },
                "quality": {
                    "type": "integer",
                    "description": (
                        "图片清晰度 50-100（默认 85；仅对超过 2MB 的大图降采样生效，"
                        "小图/动图按原样回传）"
                    ),
                },
            },
            "required": ["references"],
        },
    },
}


def normalize_read_media_args(tool_args: dict[str, Any]) -> tuple[list[str], int | None, str | None]:
    """校验并规整 read_media 参数：references 列表与可选 quality。

    返回 (references, quality, error_reason)；参数非法时 references 为空列表。
    - references：字符串/单元素自动包列表；每项接受 media:// 引用、本地路径
      或 http(s) 直链（仅做前后空白清理与去重，按出现顺序保留）；
    - quality：50-100 整数，非法时返回 error。
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
        )
    references: list[str] = []
    for item in raw_references:
        text = str(item or "").strip()
        if not text:
            continue
        if text not in references:
            references.append(text)
    quality = args.get("quality")
    if quality is not None:
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            return [], None, f"quality 参数无效: {quality!r}（应为 50-100 的整数）"
        if quality < _READ_MEDIA_QUALITY_MIN or quality > _READ_MEDIA_QUALITY_MAX:
            return [], None, (
                f"quality 参数超出范围: {quality}（允许 {_READ_MEDIA_QUALITY_MIN}-"
                f"{_READ_MEDIA_QUALITY_MAX}）"
            )
    if not references:
        return [], quality, (
            "references 不能为空：请传入媒体来源（media:// 引用 / 本地文件路径 / http(s) 直链）"
        )
    return references, quality, None


def roll_recent_media_parts(
    pairs: list[tuple[str, Any]],
    evicted: set[str] | None = None,
) -> list[tuple[str, Any]]:
    """任务内累计媒体部件的滚动窗口：只保留最近 READ_MEDIA_MAX_ITEMS 个。

    - pairs：[(reference, media_part), ...]，按注入时间排列；
    - evicted：可选输出集合，被挤出窗口的引用会写入其中（调用方应把它们从
      already_injected 里移除，这样模型再次读取同一引用时能重新注入）。

    返回裁剪后的列表（末尾 READ_MEDIA_MAX_ITEMS 个）。
    """
    limit = max(1, READ_MEDIA_MAX_ITEMS)
    trimmed = pairs[-limit:] if len(pairs) > limit else list(pairs)
    if evicted is not None and len(pairs) > limit:
        evicted.update(reference for reference, _ in pairs[:-limit])
    return trimmed


def _load_network_media_bytes(source: str) -> bytes | None:
    """下载 http(s) 媒体直链字节；失败（网络错误/非 2xx/超时）返回 None。

    超时 30 秒；UA 伪装浏览器；跟随重定向；不限制大小上限——落盘校验交给
    register_media_source（与用户上传同规则）。
    """
    request = urllib.request.Request(
        source,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            if not (200 <= int(status) < 300):
                return None
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _media_filename_from_reference(source: str, data: bytes) -> str:
    """从媒体来源推导入库文件名；扩展名缺失/可疑时按魔数嗅探校准。"""
    from memory import file_memory as fm

    # 提取文件名：URL 取路径末段（去掉 query）；本地路径取完整文件名
    name = ""
    if source.startswith("http://") or source.startswith("https://"):
        from urllib.parse import urlsplit

        path_part = urlsplit(source).path
        name = Path(path_part).name
    else:
        name = Path(source).name
    name = name.strip()
    suffix = Path(name).suffix.lower() if name else ""
    known = suffix and (fm.media_kind(f"probe{suffix}") is not None)
    if not known:
        sniffed = fm._sniff_media_filename(data)
        if sniffed is not None:
            return sniffed
    if not name:
        return "sniffed_unknown.bin"
    return name


def register_media_source(session_id: str, source: str, source_type: str) -> dict[str, Any]:
    """获取（下载/读取）媒体来源字节并注册进会话媒体库，返回注册信息。

    - source：本地路径（相对路径基于会话工作目录）或 http(s) 直链；
    - source_type："network" / "local"，写入返回值供后续授权审计使用；
    - 下载/读取后按魔数嗅探校准扩展名，再走 save_session_media 入库
      （与用户上传完全同规则：类型校验、大小上限、ICO/TIFF 自动转 PNG）；
    - 返回 {ok, reference(media://), source_type, kind, mime, size, stored_name}
      或 {ok=False, error}。

    用户授权确认逻辑预留：接入时在本函数入口（下载/读盘前）挂确认钩子，
    当前按"工具由用户提供、无安全边界"策略直接放行。
    """
    from memory import file_memory as fm

    source_text = (source or "").strip()
    try:
        if source_text.startswith("http://") or source_text.startswith("https://"):
            data = _load_network_media_bytes(source_text)
            if data is None:
                return {"ok": False, "error": f"网络媒体下载失败: {source_text}"}
        else:
            # 本地路径：相对路径按会话工作目录解析（worker 已 os.chdir）
            local_path = Path(source_text)
            if not local_path.is_file():
                return {"ok": False, "error": f"本地媒体文件不存在: {source_text}"}
            data = local_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"读取媒体失败: {exc}"}
    filename = _media_filename_from_reference(source_text, data)
    try:
        saved = fm.register_media_source(session_id, filename, data, source_type)
    except ValueError as exc:
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


def load_any_media_model_part(
    session_id: str,
    reference: str,
    quality: int | None = None,
) -> dict[str, Any] | None:
    """本地/网络/会话统一媒体加载入口（read_media 数据注入使用）。

    引用即真实位置，不再复制落盘：
    - media:// 引用 → 会话媒体库加载（load_session_media_model_part）；
    - 本地路径 → 直接按真实路径读取文件构建部件；文件不存在时返回
      {"error": "未找到，可能已删除", ...}（调用方转为模型可见提示）；
    - http(s) 直链 → 不再下载/入库，直接把 URL 作为多模态部件透传给
      上游模型（由模型供应商自行拉取；拉取失败再考虑恢复下载链路）。
    成功返回值形态与 load_session_media_model_part 一致，并统一补充来源
    审计信息：source_type（"session" / "network" / "local"）与 source
    （本次调用的原始 reference）。
    """
    from memory import file_memory as fm

    source = (reference or "").strip()
    if source.startswith(fm.MEDIA_URL_SCHEME):
        info = fm.load_session_media_model_part(session_id, source, quality=quality)
        if info is None:
            return None
        return {**info, "source_type": "session", "source": source}
    if source.startswith("http://") or source.startswith("https://"):
        # URL 直传：按扩展名推断 kind/mime；未知扩展名按图片处理。
        from urllib.parse import urlsplit

        name = Path(urlsplit(source).path).name or "remote_media"
        kind = fm.media_kind(name) or "image"
        mime = fm.media_mime_type(name) if fm.media_kind(name) else "image/jpeg"
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
        }
    # 本地路径：直接读取真实位置（相对路径基于会话工作目录，worker 已 os.chdir）
    local_path = Path(source)
    if not local_path.is_file():
        return {"error": f"未找到，可能已删除: {source}", "source_type": "local", "source": source}
    info = fm._media_model_part_from_path(local_path, quality)
    if info is None:
        return {"error": f"读取失败: {source}", "source_type": "local", "source": source}
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
) -> dict[str, Any]:
    """内置 read_media 实现：把媒体来源解析为可注入的数据（统一入口）。

    - references 支持 media:// 引用 / 本地路径 / http(s) 直链，可混用；
    - available_references：兼容旧接线的会话引用白名单（传 None 或空集合
      即不做边界过滤——当前策略：工具由用户提供，无安全边界）；传入集合
      时 media:// 引用仍按旧语义过滤，非 media:// 来源不受影响；
    - already_injected：本轮已注入过的引用（重复读取直接跳过）；
    - load_media：媒体加载函数（默认 load_any_media_model_part），便于测试
      注入桩；签名 (session_id, reference, quality=…) → info|None。

    返回结构：
    - ok 为 True 时 loaded/injected_references 为成功读取的媒体元信息；
      数据本体（base64 部件）不进入本返回值——工具结果文本会原样落盘 JSONL，
      且可能被超大结果逻辑截断，媒体数据由调用方按 loaded 项的 reference+quality
      经 load_any_media_model_part 以 user 多模态部件注入（仅内存）；
    - 每个拒绝项给出 reason（已注入/超限/下载或读取失败）。
    """
    references, quality, arg_error = normalize_read_media_args(tool_args)
    if arg_error:
        return {"ok": False, "error": arg_error, "loaded": [], "skipped": [], "injected_references": []}
    from memory import file_memory as fm

    available = {str(item) for item in (available_references or [])}
    injected = {str(item) for item in (already_injected or set())}
    loader = load_media or load_any_media_model_part
    allowed, skipped = [], []
    for reference in references:
        # 本地/网络来源不做白名单拦截；media:// 引用在传入了边界集合时按旧语义过滤
        if (
            available
            and reference.startswith(fm.MEDIA_URL_SCHEME)
            and reference not in available
        ):
            skipped.append({"reference": reference, "reason": "not_in_current_task"})
            continue
        if reference in injected:
            skipped.append({"reference": reference, "reason": "already_injected"})
            continue
        allowed.append(reference)
    if len(allowed) > READ_MEDIA_MAX_ITEMS:
        for reference in allowed[READ_MEDIA_MAX_ITEMS:]:
            skipped.append({"reference": reference, "reason": "limit_5_per_round"})
        allowed = allowed[:READ_MEDIA_MAX_ITEMS]
    loaded_items: list[dict[str, Any]] = []
    for reference in allowed:
        info = loader(session_id, reference, quality=quality)
        if info is None:
            # media:// 引用解析失败 = 会话媒体库中不存在（引用无效或文件已删除）
            reason = (
                f"未找到，可能已删除: {reference}"
                if reference.startswith(fm.MEDIA_URL_SCHEME)
                else "load_failed"
            )
            skipped.append({"reference": reference, "reason": reason})
            continue
        if isinstance(info, dict) and info.get("error"):
            # 加载器带错误说明（如本地文件"未找到，可能已删除"）：作为拒绝项
            # 透传给模型，不计入成功读取
            skipped.append({"reference": reference, "reason": info["error"]})
            continue
        loaded_items.append({
            "reference": reference,
            "source_type": info.get("source_type"),
            "kind": info.get("kind"),
            "mime": info.get("mime"),
            "size": info.get("size"),
            "downsampled": info.get("downsampled"),
            "quality": info.get("quality"),
        })
    loaded = bool(loaded_items)
    return {
        "ok": loaded,
        "message": (
            f"已读取 {len(loaded_items)} 个媒体（数据已注入后续请求的多模态部件）"
            if loaded
            else "没有可读取的媒体（引用无效/重复或全部被跳过）"
        ),
        "loaded": loaded_items,
        "skipped": skipped,
        "injected_references": [item["reference"] for item in loaded_items],
    }