"""由服务端本地执行的内置工具。"""
# from __future__ import annotations
from urllib.parse import urlsplit
import base64
import difflib
import fnmatch
import hashlib
import itertools
import json
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
from pathlib import Path
from typing import Any, Optional


from memory import file_memory


CHECK_TOOL_EXISTS_NAME = "check_tool_exists"
TODO_TOOL_NAME = "todo_write"
ASK_USER_TOOL_NAME = "ask_user"
WRITE_FILE_NAME = "write_file"
EDIT_FILE_NAME = "edit_file"
READ_FILE_NAME = "read_file"
SEARCH_FILES_NAME = "search_files"
READ_MEDIA_NAME = "read_media"
READ_DOCUMENT_NAME = "read_document"
SUB_AGENT_TOOL_NAME = "sub_agent"
RUN_COMMAND_NAME = "run_command"
# 内置工具在工具选择（_meta.tool_selection / mcp_servers.json inputs）中的伪服务键：
# 前端把它作为“内置工具”分组渲染在工具模态框首位，保存/加载与 MCP 工具走同一链路
BUILTIN_TOOL_SERVER_KEY = "__builtin__"
# 工具选择中允许出现的内置工具名（check_tool_exists 由后端按外部工具自动注入、
# read_document 由后端按会话上传文件自动注入，均不开放手选）
SELECTABLE_BUILTIN_TOOL_NAMES = (
    TODO_TOOL_NAME, ASK_USER_TOOL_NAME, WRITE_FILE_NAME, EDIT_FILE_NAME,
    READ_FILE_NAME, SEARCH_FILES_NAME, READ_MEDIA_NAME, SUB_AGENT_TOOL_NAME,
    RUN_COMMAND_NAME,
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
    include_run_command: bool = False,
    include_read_media: bool = False,
    include_read_document: bool = False,
    include_sub_agent: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """注入本地内置工具：check_tool_exists（选了外部工具时）、todo_write、
    ask_user、write_file、edit_file、read_file、search_files、run_command、
    read_media、read_document（会话存在上传文件且本轮携带工具时由 chat_factory
    自动注入）与 sub_agent（用户在工具选择中勾选时）。"""
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
    if include_run_command and RUN_COMMAND_NAME not in servers:
        tools.append(RUN_COMMAND_TOOL_DEFINITION)
        servers[RUN_COMMAND_NAME] = BUILTIN_TOOL_SERVER_KEY
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
        CHECK_TOOL_EXISTS_NAME, TODO_TOOL_NAME, ASK_USER_TOOL_NAME,
        WRITE_FILE_NAME, EDIT_FILE_NAME, READ_FILE_NAME, SEARCH_FILES_NAME,
        READ_MEDIA_NAME, READ_DOCUMENT_NAME, SUB_AGENT_TOOL_NAME,
        RUN_COMMAND_NAME,
    )


TODO_TOOL_DEFINITION = {
    "type": "function",
    "function": {
      "name": TODO_TOOL_NAME,
      "description": (
          "写入或更新当前任务计划（todo list），用于跟踪需要多步骤、"
          "较长时间或多次工具调用的任务；简单一次性任务不要使用。"
          "每次调用必须提交完整列表（全量覆盖旧列表，最多 20 项），"
          "合并状态变更以减少调用：步骤完成时在下一次调用中同时把它设为"
          " done 并将下一待办设为 in_progress（每步一次调用）。"
          "返回 plan_complete=true 表示全部步骤已完成：直接汇总执行结果"
          "答复用户，除非需要新建后续计划，否则不要再调用本工具。"
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
        | {CHECK_TOOL_EXISTS_NAME, READ_DOCUMENT_NAME}
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
    elif (
        query_name in SELECTABLE_BUILTIN_TOOL_NAMES
        or query_name in (CHECK_TOOL_EXISTS_NAME, READ_DOCUMENT_NAME)
    ):
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

_ASK_MAX_QUESTIONS = 5
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


# --------------------------- sub_agent：子智能体派发 ---------------------------
# 派发参数长度上限（防超大参数直接进上下文）
_SUB_AGENT_TASK_MAX_CHARS = 20_000

SUB_AGENT_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": SUB_AGENT_TOOL_NAME,
        "description": (
            "派发子任务给一个子智能体（sub agent）执行，并在同一回复中并发派发多个相互独立的子任务。"
            "子智能体拥有独立的上下文与与你相同的一套工具（MCP 工具、内置文件读写/搜索等），"
            "会独立完成该子任务并只把最终回复返回给你——它的思考过程、工具轨迹不会进入你的上下文。"
            "适用场景：可并行的独立调研/检索/批量处理子任务；需要大量中间工具调用而你只关心结论的部分。"
            "不适用：单次工具调用就能完成的事（直接调用该工具）；需要用户交互的决策（用 ask_user）。"
            "关键契约：1. task 必须自包含——子智能体看不到对话历史、上传文件内容与你掌握的任何上下文，"
            "目标、涉及文件/目录的绝对路径、已有事实与结论、约束（如'只读不改'）、期望的回复内容"
            "（结论+关键证据/数值+文件路径）都必须写进 task；"
            "2. 你只能通过子智能体的最终回复获取结果，不能与它多轮交互；"
            "3. 子智能体无法向用户提问，阻塞时它会在回复中说明；"
            "4. 可选提供 todo 预置子任务计划（元素 {id, content, status}，与 todo_write 同一语义）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "子任务目标（必填，自包含）。必须包含：目标与验收标准、涉及文件/目录的绝对路径、"
                        "已有事实与结论、执行约束、期望回复的内容结构"
                    ),
                },
                "todo": {
                    "type": "array",
                    "description": "可选：为子智能体预置的初始任务计划（每次全量覆盖，最多 20 项）",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "步骤唯一标识"},
                            "content": {"type": "string", "description": "步骤内容（一句话，≤200 字符）"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done"],
                                "description": "pending=待办 in_progress=进行中 done=已完成",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                },
            },
            "required": ["task"],
        },
    },
}

# 子智能体侧的 ask_user 占位定义：同名同参 schema，但执行时返回明确错误
# （子任务没有用户通道，避免子模型反复尝试向用户提问）。
ASK_USER_PLACEHOLDER_DEFINITION = {
    "type": "function",
    "function": {
        "name": ASK_USER_TOOL_NAME,
        "description": (
            "不可用：你是被父智能体调度的子智能体，没有与用户交互的通道，无法通过本工具提问。"
            "遇到必须由用户决定的事项时，请在最终回复中明确说明阻塞点与所需信息，交给父智能体处理。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "无效参数（占位定义，仅用于拦截）",
                    "items": {"type": "object"},
                },
            },
            "required": ["questions"],
        },
    },
}


def normalize_sub_agent_task(raw: Any) -> tuple[str | None, str | None]:
    """校验 sub_agent 的 task 参数；返回 (规整后的 task, 错误原因)。

    容错：task 传成 {text/content/task: ...} 对象或 JSON 字符串时自动提取。
    """
    if isinstance(raw, str):
        task_text = raw.strip()
    elif isinstance(raw, dict):
        for key in ("text", "content", "task"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                task_text = value.strip()
                break
        else:
            task_text = ""
    else:
        task_text = str(raw or "").strip()
    if not task_text:
        return None, "task 参数无效：需要一段非空的子任务描述（自包含目标、路径、约束与期望回复）"
    if len(task_text) > _SUB_AGENT_TASK_MAX_CHARS:
        return None, (
            f"task 超长（{len(task_text)} > {_SUB_AGENT_TASK_MAX_CHARS} 字符）；"
            "请精简子任务描述，或拆分为多个子任务"
        )
    return task_text, None


def execute_ask_user_placeholder(tool_args: dict[str, Any]) -> dict[str, Any]:
    """子智能体侧 ask_user 占位执行：永远返回不可用说明（不阻塞、不提问）。"""
    questions = normalize_ask_questions(
        tool_args.get("questions") if isinstance(tool_args, dict) else None)
    return {
        "status": "unavailable_in_sub_agent",
        "message": (
            "子智能体无法向用户提问（没有用户交互通道）。"
            "请基于已有信息继续决策；确实需要用户输入的事项，请在最终回复中"
            "列出阻塞点与所需信息，由父智能体决定是否向用户转达。"
        ),
        "questions": questions or [],
    }


# ------------------- write_file / edit_file：内置文件编辑工具 -------------------
# 与 MCP sys_tools_server 的同名工具保持语义一致（相对路径基于会话工作目录，
# worker 进程已 os.chdir），但结果为结构化 dict：除人类可读 message 外携带
# path/action/changed_bytes 等机器可读字段；edit/写改类结果另带 _file_diff
# 顶级键（unified diff + 行数统计，仅前端展示与 JSONL 落盘，不进模型上下文）
# 与 content_hash（read_file 亦可获取，为阶段 2 文件版本校验预留）。

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
    """生成 unified diff 与行数统计（前端展示用）。

    返回 {diff, lines_added, lines_removed, diff_truncated, diff_skipped}：
    - 文本超限时整体跳过（diff 置空，diff_skipped=file_too_large）；
    - diff 超过字符上限时提前截断（diff_truncated=True）；
    - 新旧文本在比较前统一换行为 \n，展示行不含行尾符。
    """
    skipped = {
        "diff": "",
        "lines_added": 0,
        "lines_removed": 0,
        "diff_truncated": False,
        "diff_skipped": "file_too_large",
    }
    if len(old_text) > _FILE_DIFF_MAX_TEXT_CHARS or len(new_text) > _FILE_DIFF_MAX_TEXT_CHARS:
        return skipped

    def _norm(text: str) -> list:
        unified = text.replace("\r\n", "\n").replace("\r", "\n")
        return unified.splitlines(keepends=True)

    display = _display_path(path)
    diff_gen = difflib.unified_diff(
        _norm(old_text),
        _norm(new_text),
        fromfile=f"a/{display}",
        tofile=f"b/{display}",
        n=_FILE_DIFF_CONTEXT_LINES,
        lineterm="",
    )
    parts: list = []
    added = removed = 0
    truncated = False
    for index, line in enumerate(diff_gen):
        if index < 2:
            # --- a/path / +++ b/path 文件头（仅内容有变化时输出）
            parts.append(line.rstrip("\r\n"))
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        parts.append(line.rstrip("\r\n"))
        if sum(len(part) + 1 for part in parts) > _FILE_DIFF_MAX_DIFF_CHARS:
            truncated = True
            break
    if not parts:
        return {
            "diff": "",
            "lines_added": 0,
            "lines_removed": 0,
            "diff_truncated": False,
            "diff_skipped": "unchanged",
            "path": str(path),
            "display_path": display,
        }
    return {
        "diff": "\n".join(parts),
        "lines_added": added,
        "lines_removed": removed,
        "diff_truncated": truncated,
        "diff_skipped": "",
        "path": str(path),
        "display_path": display,
    }


def _content_hash(text: str) -> str:
    """内容指纹（阶段 2 文件版本校验预留）：解码后全文的 sha256 前 16 位。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


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
    # 旧内容仅用于 diff 生成（成功失败均不中断写入主流程）
    old_text = ""
    if existed:
        old_data = path.read_bytes()[:_READ_CHUNK]
        if not _is_probably_binary(old_data):
            old_text, _ = _decode_bytes_used(old_data, "")
    mode = "a" if append else "w"
    with open(path, mode, encoding=encoding, newline="") as file:
        file.write(content)
    action = "追加" if append else "写入"
    # 追加模式的新文本 = 旧内容 + 新内容（diff 才能正确反映最终文件变化）
    new_text_for_diff = old_text + content if append else content
    file_diff = _build_file_diff(old_text, new_text_for_diff, path)
    hash_note = f"，内容指纹 {_content_hash(content)}" if content else ""
    return {
        "message": (
            f"[write_file] 已{action} {len(content)} 字符 → {_display_path(path.resolve())}"
            f"{hash_note}"
            + (f"（diff +{file_diff['lines_added']} -{file_diff['lines_removed']} 行）"
               if (file_diff["lines_added"] or file_diff["lines_removed"]) and not file_diff["diff_skipped"] else "")
        ),
        "path": _display_path(path.resolve()),
        "action": "append" if append else "overwrite",
        "created": not existed,
        "size_before": size_before,
        "size_after": path.stat().st_size,
        "chars_written": len(content),
        "encoding": encoding,
        "content_hash": _content_hash(content),
        "_file_diff": file_diff,
        # V2 版本链入链数据：{path, display_path, old_text, new_text}
        # （消费点 chat_factory / sub_agent 摘取后调 record_change，再统一剥离）
        _FILE_HISTORY_PAYLOAD_KEY: {
            "path": str(path.resolve()),
            "display_path": _display_path(path.resolve()),
            "encoding": encoding,
            "old_text": old_text if file_diff.get("diff_skipped") != "file_too_large" else None,
            # 追加模式的最终全文 = 旧内容 + 新写入内容
            "new_text": (
                (old_text + content) if append else content
            ) if len(old_text + content) <= _FILE_DIFF_MAX_TEXT_CHARS else None,
        },
    }


def execute_edit_file(tool_args: dict[str, Any]) -> dict[str, Any]:
    """内置 edit_file 实现：语义对齐 MCP 同名工具，返回结构化结果。

    换行归一化匹配 + 原风格写回、0 匹配给最接近候选行提示等行为均与 MCP 版一致；
    额外返回 replacements/eol_style/encoding/content_hash 与 _file_diff（unified diff
    + 行数统计，chat_factory/sub_agent 摘取后仅推送前端与落盘，不进模型上下文）。
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
    file_diff = _build_file_diff(text, updated, path)
    with open(path, "w", encoding=used_encoding, newline="") as file:
        file.write(updated.replace("\n", eol))
    replaced = count if replace_all else 1
    style = "CRLF" if eol == "\r\n" else "LF"
    matched_lines = len(old_norm.split("\n"))
    diff_note = (
        f"，diff +{file_diff['lines_added']} -{file_diff['lines_removed']} 行"
        if not file_diff["diff_skipped"] and (file_diff["lines_added"] or file_diff["lines_removed"])
        else ""
    )
    return {
        "message": (
            f"[edit_file] 已在 {_display_path(path)} 中替换 {replaced} 处"
            f"（换行风格 {style}，编码 {used_encoding}{diff_note}），"
            f"内容指纹 {_content_hash(updated)}"
        ),
        "path": _display_path(path),
        "action": "replace",
        "replacements": replaced,
        "matched_occurrences": count,
        "eol_style": style,
        "encoding": used_encoding,
        "matched_lines": matched_lines,
        "content_hash": _content_hash(updated),
        "_file_diff": file_diff,
        # V2 版本链入链数据（text/updated 均为换行归一化后的全文）
        _FILE_HISTORY_PAYLOAD_KEY: {
            "path": str(path.resolve()),
            "display_path": _display_path(path.resolve()),
            "encoding": used_encoding,
            "old_text": text if file_diff.get("diff_skipped") != "file_too_large" else None,
            "new_text": updated if len(updated) <= _FILE_DIFF_MAX_TEXT_CHARS else None,
        },
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
            "content_hash": _content_hash(text),
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
        # 全文（非本次区间）的内容指纹：模型可在 edit_file 时以 expected_hash
        # 引用（阶段 2 版本校验），用于检测读后文件被外部修改的竞态
        "content_hash": _content_hash(text),
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


# ------------------- read_document：读取上传文件的解析文本（按需/分页） -------------------
# 用户上传文档（pdf/docx/doc/xls/xlsx/csv/md/txt）解析出的文本由系统提示词以
# 「文件清单」形式注入：小文件内联全文、大文件仅开头节选（见
# memory.file_memory.build_file_manifest_text）。需要文件其余内容时模型调用
# 本工具，按字符区间分页读取完整解析文本。
# 由 chat_factory 在「会话存在上传文件且本轮携带工具」时自动注入（不开放手选，
# 与 check_tool_exists 同类）；只读工具，无内容安全边界（文件由用户自己上传）。

_READ_DOCUMENT_DEFAULT_MAX_CHARS = 8000
_READ_DOCUMENT_MAX_CHARS = 20000


def _read_document_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


READ_DOCUMENT_TOOL_DEFINITION = {
    "type": "function",
    "function": {
      "name": READ_DOCUMENT_NAME,
      "description": (
          "读取用户上传文件（PDF/Word/Excel/CSV/MD/TXT 等已解析文档）的完整内容。"
          "系统提示词中会列出上传文件清单：小文件已内联全文，大文件仅显示开头节选；"
          "需要文件其余内容时用本工具读取。支持按字符区间分页（start_char/max_chars），"
          "返回带 total_chars 与 has_more，据此继续读取后续区间。"
      ),
      "parameters": {
        "type": "object",
        "properties": {
          "filename": {
            "type": "string",
            "description": "上传文件的原始文件名（见系统提示词文件清单中的文件名）",
          },
          "start_char": {
            "type": "integer",
            "description": "起始字符位置（从 0 开始，默认 0）",
          },
          "max_chars": {
            "type": "integer",
            "description": (
                f"本次读取的最大字符数（默认 {_READ_DOCUMENT_DEFAULT_MAX_CHARS}，"
                f"最大 {_READ_DOCUMENT_MAX_CHARS}）"
            ),
          },
        },
        "required": ["filename"],
      },
    },
}


def execute_read_document(
    tool_args: dict[str, Any],
    session_id: str,
    support_doc_types: Any = None,
) -> dict[str, Any]:
    """执行内置 read_document：按文件名读取上传文件的解析文本（分页）。

    support_doc_types 为当前生效模型的 supportDocTypes 声明（原生文档输入能力）：
    - 命中声明且原始文件可用 → 结果携带 native 块（原生文档部件），调用方把它
      注入后续请求（供应商侧视觉解析），同时返回解析文本节选作为兜底/速读；
    - 未命中/原始文件缺失 → 纯文本分页口径（与历史行为一致），native 省略。

    返回 {filename, type, total_chars, start_char, end_char, content, has_more,
    next_start_char?, native?, message}；失败返回 {"error": ...}（模型可见）。
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    filename = str(args.get("filename") or "").strip()
    if not filename:
        return {"error": "filename 不能为空（文件名见系统提示词中的上传文件清单）"}
    start = _read_document_int(args.get("start_char"), 0, minimum=0, maximum=10**9)
    max_chars = _read_document_int(
        args.get("max_chars"),
        _READ_DOCUMENT_DEFAULT_MAX_CHARS,
        minimum=1,
        maximum=_READ_DOCUMENT_MAX_CHARS,
    )
    try:
        record = file_memory.find_file_memory_record(session_id, filename)
    except Exception as exc:
        return {"error": f"读取文件记录失败：{exc}"}
    if record is None:
        try:
            names = [
                str(item.get("filename") or "未命名")
                for item in file_memory.list_session_file_records(session_id)
            ]
        except Exception:
            names = []
        hint = f"；当前会话已上传文件：{'、'.join(names)}" if names else "；当前会话没有上传文件"
        return {"error": f"未找到文件「{filename}」{hint}"}
    # 原生文档分支：当前模型声明支持该类型且原始文件可用时，构建原生文档部件
    # （调用方注入后续请求；文本节选仍一并返回，供应商拒绝 file 部件时可兜底）
    native_block: dict[str, Any] | None = None
    native_note = ""
    if support_doc_types and file_memory.document_supports_native(
        str(record.get("filename") or filename), support_doc_types
    ):
        loaded_native = file_memory.build_native_document_part(session_id, record)
        if loaded_native.get("part") is not None:
            native_block = loaded_native
            native_note = (
                "；当前模型支持该文档类型，原始文件已作为原生文档注入后续请求"
                "（以下为解析文本节选，供快速定位）"
            )
        elif loaded_native.get("error"):
            native_note = f"；原生文档注入不可用：{loaded_native['error']}"
    content = str(record.get("content") or "")
    total = len(content)
    if total == 0:
        empty_hint = "该文件解析文本为空（可能是扫描版 PDF 等无可提取文本）"
        if record.get("parse_failed"):
            empty_hint = (
                "该文件本地文本解析失败"
                f"（{str(record.get('parse_error') or '解析器不可用/文件不支持')}），"
                "无文本可读；原始文件仍保留，可尝试按绝对路径用 read_file 读取"
                "（若为纯文本），或改用支持该文档类型的模型按原生文档读取"
            )
        result: dict[str, Any] = {
            "filename": record.get("filename") or filename,
            "type": record.get("type") or "",
            "total_chars": 0,
            "start_char": 0,
            "end_char": 0,
            "content": "",
            "has_more": False,
            "message": empty_hint + native_note,
        }
        if native_block is not None:
            result["native"] = native_block
        return result
    if start >= total:
        return {
            "error": (
                f"start_char={start} 已超出文件总长度 {total}；"
                "请用 start_char=0 重新开始或检查文件名是否正确"
            )
        }
    chunk = content[start:start + max_chars]
    end = start + len(chunk)
    result = {
        "filename": record.get("filename") or filename,
        "type": record.get("type") or "",
        "total_chars": total,
        "start_char": start,
        "end_char": end,
        "content": chunk,
        "has_more": end < total,
    }
    if native_block is not None:
        result["native"] = native_block
    if end < total:
        result["next_start_char"] = end
        result["message"] = (
            f"已返回第 {start}-{end} 字符（共 {total} 字）；"
            f"如需继续，用 start_char={end} 再次调用" + native_note
        )
    else:
        result["message"] = f"已返回第 {start}-{end} 字符（全文结束，共 {total} 字）" + native_note
    return result


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
                "    start_time: 视频区间读取开始秒数（可选，精确到帧，毫秒精度）\n"
                "    end_time:   视频区间读取结束秒数（可选，精确到帧；省略时读取 "
                f"start_time 起 {max_seconds} 秒——该上限可"
                "由项目 .env 的 VIDEO_MAX_READ_SECONDS 配置）\n"
                "返回：\n"
                "    文本说明（读取了哪些媒体、来源类型、是否转换/降采样/超限跳过）；"
                "数据本身以 user 消息多模态部件注入后续请求，不在本文本内\n"
                "注意：\n"
                "    并发限制：同一条模型回复只允许 1 个 read_media 调用——同一回复"
                "并发调用本工具 2 个及以上时，仅首个会执行，其余返回错误占位且不注入"
                "媒体数据（多个媒体部件并发注入会使供应商请求报错）。要读多个来源时"
                "把它们合并进一次调用的 references 列表；必须先后依赖结果时改为分多轮调用；\n"
                "    视频/动图 gif 走区间读取（不再全量注入）：单次最多 "
                f"{max_seconds} 秒（.env 可配），"
                "请求区间超上限自动截断为 [start, "
                "start+上限] 并在返回中标记 truncated；先传 start_time == end_time "
                "（如 0/0）可只探测元数据（时长/帧率/分辨率，不回传视频数据）；"
                "返回元信息含视频时长/帧率/分辨率与实际读取区间，据此规划下一段 "
                "[上一段 end, end+上限] 续读；\n"
                "    视频数据一次性消费：供应商单请求仅接受 1 个视频——读取新片段"
                "时此前已注入的视频数据会被回收（工具结果中的参数与读取状态文本"
                "仍保留）；需要回看画面就对同一引用与相同区间重新调用本工具"
                "（切片有缓存，成本很低）；一次调用读多个视频时只有第一个片段"
                "带画面数据，其余按序补读；\n"
                "    视觉 API 拒绝的图片格式会自动转换后再注入：静图 gif 与 "
                "bmp/ico/tif/tiff 转 PNG、动图 gif 转 H.264 MP4（ffmpeg 定位"
                "回退 imageio-ffmpeg 包内置二进制，均缺失时动图回退首帧 PNG）；"
                "原生格式（png/jpg/jpeg/webp）直接按原样注入；\n"
                "    大小上限按内容判定：静图类 30MB、动图 gif 与视频 600MB、"
                "音频 20MB；超过上限的媒体直接拒绝（错误信息注明原因），"
                "不会注入原始大文件；\n"
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
                    "start_time": {
                        "type": "number",
                        "description": (
                            "视频区间读取开始秒数（可选，精确到帧）；仅对视频与动图 gif "
                            "生效"
                        ),
                    },
                    "end_time": {
                        "type": "number",
                        "description": (
                            "视频区间读取结束秒数（可选，精确到帧）；省略时读取 "
                            f"start_time 起 {max_seconds} 秒；"
                            "start_time == end_time 表示仅探测元数据（不回传视频数据）"
                        ),
                    },
                },
                "required": ["references"],
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
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return [], None, f"{name} 参数无效: {raw!r}（应为秒数，支持毫秒精度）", None, None
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
        saved = file_memory.register_media_source(session_id, filename, data, source_type)
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
    with urllib.request.urlopen(req, timeout=60) as resp:
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
        )
        if info is None:
            return None
        return {**info, "source_type": "session", "source": source}
    if source.startswith("http://") or source.startswith("https://"):
        # URL 直传：按扩展名推断 kind/mime；未知扩展名按图片处理。
        name = Path(urlsplit(source).path).name or "remote_media"
        kind = file_memory.media_kind(name) or "image"
        mime = file_memory.media_mime_type(name) if file_memory.media_kind(name) else "image/jpeg"
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
            suffix == ".gif" or suffix in file_memory._VISION_UNSAFE_IMAGE_EXTENSIONS
        ):
            try:
                data = _fetch_url_media_bytes(source, max_bytes=_network_media_fetch_limit_bytes(name))
                sniffed = file_memory._sniff_media_filename(data)
                probe_name = (sniffed or name) if (suffix == ".gif" or not sniffed) else name
                tmp_path = file_memory._transcode_cache_dir("network_media") / f"dl_{uuid.uuid4().hex[:8]}_{Path(probe_name).name}"
                tmp_path.write_bytes(data)
                try:
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
            except Exception:
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
        }
    # 本地路径：直接读取真实位置（相对路径基于会话工作目录，worker 已 os.chdir）
    local_path = Path(source)
    if not local_path.is_file():
        return {"error": f"未找到，可能已删除: {source}", "source_type": "local", "source": source}
    info = file_memory._media_model_part_from_path(
        local_path, quality, start_time=start_time, end_time=end_time
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
    for reference in allowed:
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
            f"已读取 {len(loaded_items)} 个媒体（数据已注入后续请求的多模态部件）"
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
# 编码自适应、后台分离模式、cmd 家族健壮性补丁——多行内联代码改写为临时脚本 /
# 管道过滤器探测替换或本地兜底 / 分号与 Unix 命令语法提示）。
# 迁移差异：
# - 本实现为同步阻塞函数：execute_run_command 由调用方经 asyncio.to_thread
#   放到工作线程执行（与 MCP 工具的线程池语义一致，避免卡死事件循环）；
# - 解码辅助改名为 _decode_command_bytes_used / _decode_command_bytes，避免与
#   本模块文件工具使用的简化版 _decode_bytes_used 混淆；
# - 复用本模块已有的 _resolve_dir_path / _display_path。
# 原 MCP 侧注册块已在 sys_tools_server.py 中注释保留（回滚时取消注释即可）。

# 前台 stdout 截断上限（防止超大输出撑爆模型上下文；超限时完整输出落盘）
_RUN_COMMAND_MAX_CHARS = 12000

# BOM 前缀 → 编码名（嗅探优先级最高，字节级无歧义）
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# 乱码特征：CJK 区字符与常见替换符。GBK 误按 UTF-8 解码会大量产生 CJK 扩展区生僻字，
# UTF-8 误按 GBK 解码则表现为成对"锟斤拷/烫烫烫"类噪声，两者都可用比例识别。
_REPLACEMENT_CHARS = ("\ufffd", "�")

# UTF-8 中文被误按 GBK 解码的高频特征字（如"中"→"涓"、"："→"锛"、"的"→"鐨"）。
# 这些字大多落在常用汉字区 U+4E00-9FFF，仅靠扩展区惩罚无法识别，需单独计罚。
_GBK_MISDECODE_SIGNS = frozenset(
    "锛涓鐨璁璁鐢ㄦ浣鍚鍦ㄧ笉鑷姝ソ鈥銆銉鏄鍙涔浠鏂鎴戣澶娈鎷瀹寮"
)


def _mojibake_score(text: str) -> float:
    """估计一段文本的乱码程度（0=干净，越高越乱）。

    规则：
      - 替换符 � 计重罚；
      - CJK 兼容/扩展区生僻字（U+3400-4DBF、U+E000-F8FF、U+FE30-FE4F 等）计轻罚；
      - 正常常用汉字（U+4E00-9FFF）不算乱码。
    """
    if not text:
        return 0.0
    sample = text[:20000]
    penalty = 0
    for ch in sample:
        code = ord(ch)
        if ch in _REPLACEMENT_CHARS:
            penalty += 2
        elif 0x3400 <= code <= 0x4DBF or 0xE000 <= code <= 0xF8FF or 0xFE30 <= code <= 0xFE4F:
            penalty += 1
        elif ch in _GBK_MISDECODE_SIGNS:
            # UTF-8 中文被误按 GBK 解码的高频特征字（如"中"→"涓"、全角冒号→"锛"）
            penalty += 1
        elif 0x9FA6 <= code <= 0x9FFF:
            # U+9FA6-9FFF 属于 GBK 有映射但 Unicode 主区少用的字，GBK 串被 utf-8 硬解时高频出现
            penalty += 1
    return penalty / len(sample)


def _decode_command_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """把字节解码为文本，返回 (文本, 实际使用的编码)。

    优先级：显式 encoding > BOM 嗅探 > 多候选严格解码评分。
    多候选都能严格解码时（如 GBK 中文恰好构成合法 UTF-8 的情形），按乱码特征
    打分取最优，避免固定顺序导致的高概率互串乱码。
    显式指定 encoding 时直接按该编码宽松解码（保证不抛异常）。
    """
    if encoding:
        return data.decode(encoding, errors="replace"), encoding
    for bom, name in _BOM_TABLE:
        if data.startswith(bom):
            try:
                return data.decode(name), name
            except (UnicodeDecodeError, LookupError):
                break
    best_text, best_encoding, best_score = None, None, None
    for candidate in candidates:
        try:
            decoded = data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _mojibake_score(decoded)
        if best_score is None or score < best_score:
            best_text, best_encoding, best_score = decoded, candidate, score
        if score == 0.0:
            break  # 已找到零乱码候选，无需继续比较
    if best_text is None:
        return data.decode(candidates[-1], errors="replace"), candidates[-1]
    return best_text, best_encoding


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
    pwsh 7 / Git Bash 通常直接输出 UTF-8，由多候选乱码评分兜底。
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

    - auto: Windows → cmd.exe；非 Windows → bash 可用则 bash，否则 sh；
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
            return os.environ.get("COMSPEC") or "cmd.exe", "cmd", []
        bash_path = shutil.which("bash")
        return (bash_path or "/bin/sh"), ("bash" if bash_path else "sh"), []
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
# 补丁策略：① 多行内联代码改写为临时脚本执行；② 管道过滤器探测 + 替换/本地兜底；
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
# 仅含行数参数的 tail/head（如 `tail -20`、`head -n 5`）
_SIMPLE_FILTER_RE = re.compile(
    r"^\s*(tail|head)\s+(?:(?:-n)\s*(\d+)|-(\d+))\s*$", re.IGNORECASE)
# 内联代码执行入口：python -c / node -e / node --eval
_INLINE_CODE_RE = re.compile(
    r"(?P<interp>(?:^|[\s&|(])(?:python|python3|py|node)(?:\.exe)?)"
    r"\s+(?P<flag>-c|-e|--eval)\s+(?P<quote>[\"'])",
    re.IGNORECASE)
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


def _split_top_level_pipes(command: str, quote_chars: tuple = ('"',)) -> list:
    """按顶层 `|` 切分命令（跳过引号内与 `^` 转义后的 `|`）。"""
    segments: list = []
    current: list = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "^":
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
        probe_path = None
        try:
            handle, probe_path = tempfile.mkstemp(prefix="rc_probe_", suffix=".cmd")
            with os.fdopen(handle, "w", encoding="mbcs", errors="replace") as file:
                file.write("@echo off\r\n")
                file.write(probe)
            proc = subprocess.run(
                [os.environ.get("COMSPEC") or "cmd.exe", "/c", "call", probe_path],
                capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=_merged_child_env() or None)
            text = _decode_command_bytes(proc.stdout or b"", candidates=_command_output_candidates())
            if f"{marker}_{total - 1}" not in text:
                ok = False
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
    """cmd 家族管道兜底：处理末尾的简单 `| tail -N` / `| head -N` 段。

    返回 (新命令, 本地过滤说明或 None, 提示列表)：
    - 过滤器可用（管道形式探测通过）：命令原样返回；
    - 当前命中的实现不可用、但 PATH 中存在可用实现：替换为可用实现的绝对路径；
    - 均不可用：去掉该管道段，改由本工具在 Python 侧取头/尾 N 行（输出不丢）。
    """
    if "|" not in command:
        return command, None, []
    segments = _split_top_level_pipes(command)
    if len(segments) < 2:
        return command, None, []
    parsed = _parse_simple_filter(segments[-1])
    if parsed is None:
        return command, None, []
    tool, count = parsed
    args = f"-{count}"
    current_path = _resolve_filter_path(tool)
    if current_path and _pipe_filter_works(tool, args, current_path):
        return command, None, []
    alternative = _find_working_filter(tool, args, current_path)
    head = "|".join(segments[:-1]).rstrip()
    if alternative:
        return (
            f'{head} | "{alternative}" {args}',
            None,
            [f"检测到 `{tool}` 当前命中的实现（{current_path or '未找到'}）不是可用的管道过滤器，"
             f"已自动改用 {alternative}（输出不再丢失）"],
        )
    return (
        head,
        (tool, count),
        [f"检测到 `{tool}` 不可用（{current_path or '未找到'}），"
         f"已由 run_command 在本地实现 `| {tool} -{count}`（取"
         f"{'末尾' if tool == 'tail' else '开头'} {count} 行；如需原生过滤请安装可用实现或改用 shell=bash）"],
    )


def _find_inline_code(command: str):
    """定位多行内联代码执行（python -c / node -e）。

    返回 dict（含解释器、入口 flag 及其在命令中的位置、代码文本）或 None。
    仅当代码跨行（含换行）时才返回——单行命令交给 shell 原生处理。
    改写时需要把"入口 flag + 引号内代码"整体替换为"脚本路径"，
    因此这里同时给出 flag 的起止下标。
    """
    match = _INLINE_CODE_RE.search(command)
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
        return None
    code = command[open_index + 1: close_index]
    if "\n" not in code:
        return None
    if len(code) > _INLINE_SCRIPT_MAX_CHARS:
        return None
    return {
        "interp": interp,
        "flag": match.group("flag"),
        "flag_start": match.start("flag"),
        "flag_end": match.end("flag"),
        "open_index": open_index,
        "close_index": close_index,
        "code": code,
    }


def _rewrite_multiline_inline_code(command: str, shell_kind: str):
    """把多行内联代码改写为临时脚本执行（cmd 家族）。

    cmd 按行解释 .cmd 文件，多行内联代码必然被拆行执行；改写为
    `<解释器> "<临时脚本>"` 后语义与 `-c` 一致（退出码透传），且支持中文与引号。
    注意必须连入口 flag（`-c`/`-e`）一起替换掉：`python -c "<路径>"` 会把路径
    当代码字符串执行（实测 SyntaxError），而 `python "<路径>"` 才是执行脚本。
    返回 (新命令, 提示列表)。
    """
    if shell_kind != "cmd" or "\n" not in command:
        return command, []
    found = _find_inline_code(command)
    if not found:
        # 有多行内联代码痕迹但无法安全改写（尾部语法复杂/含分号等）：给出明确提示，
        # 避免"首行被当命令执行 + 后续行报错"的混乱结果被误读为成功
        if _INLINE_CODE_RE.search(command):
            return command, [
                "检测到多行内联代码但尾部语法复杂，未自动改写；cmd 按行解释 .cmd 文件，"
                "多行 `-c` 代码会被拆行执行（可能部分执行并伴随报错）。"
                "建议把代码写入脚本文件（write_file）后执行，或改用 shell=bash/pwsh"
            ]
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
        return command, [f"多行内联代码改写失败（{exc}）；cmd 按行解释 .cmd，多行 `-c` 代码会失败，"
                         "建议改用 write_file 写脚本后执行"]
    # 用"解释器 + 脚本路径"替换"解释器 + 入口 flag + 引号内代码"
    new_command = (
        f'{command[:found["flag_start"]]}"{script_path}"'
        f'{command[found["close_index"] + 1:]}'
    )
    return new_command, [
        f"检测到多行内联代码（{interp} {found['flag']}）：cmd 按行解释 .cmd 文件会把后续行当命令执行，"
        f"已自动改写为临时脚本 {_display_path(script_path)} 后执行（语义等价，退出码透传）"
    ]


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
    """清理后台日志/WMI 中转临时文件中超过 24 小时的旧文件（忽略一切错误）。"""
    try:
        cutoff = time.time() - 24 * 3600
        # bg_=后台日志；fg_=前台命令 WMI 中转的临时文件；inline_=多行内联代码改写出的脚本
        patterns = ("bg_*", "fg_*", "inline_*")
        for pattern in patterns:
            for item in _BG_LOG_DIR.glob(pattern):
                try:
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
    kernel32 = ctypes.windll.kernel32
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
        capture_output=True, timeout=30,
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

    文件三件套（均写入 _BG_LOG_DIR，24h 自动清理）：
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
        launcher_path.write_text(launcher_text, encoding="mbcs")
        vbs_path.write_text(vbs_text, encoding="mbcs")
    except (OSError, LookupError):
        launcher_path.write_text(launcher_text, encoding="utf-8", errors="replace")
        vbs_path.write_text(vbs_text, encoding="utf-8", errors="replace")
    if runner_path is not None:
        _write_runner_script(runner_path, shell_kind, command)
    # wscript 为 GUI 子系统：WMI 默认启动它不产生可见窗口；//nologo 抑制横幅
    pid = _wmi_create_process(f'wscript.exe //nologo "{vbs_path}"')
    return {
        "pid": pid, "out": out_path, "err": err_path, "done": done_path,
        "launcher": launcher_path, "runner": runner_path, "vbs": vbs_path,
    }


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
            runner_path.write_text(text, encoding="mbcs")
        except (OSError, LookupError):
            runner_path.write_text(text, encoding="utf-8", errors="replace")
        return
    text = normalized + "\n"
    if shell_kind in ("powershell", "pwsh"):
        text += "if ($LASTEXITCODE -is [int]) { exit $LASTEXITCODE }\n"
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


def _kill_process_tree(proc: subprocess.Popen, is_nt: bool) -> None:
    """超时后终止进程及其子树（Windows 用 taskkill /T，POSIX 用进程组 SIGKILL）。"""
    try:
        if is_nt:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                           cwd, timeout, is_nt) -> tuple:
    """Popen 直接执行（Windows 中转链失败/超时的兜底；POSIX 常规路径）。

    返回 (退出码, stdout 文本, stderr 文本, 是否超时, 耗时秒)。
    """
    if shell_kind == "cmd":
        # cmd 用 /c 接命令原文（字符串形式），规避列表形式把引号按 MSVCRT
        # 规则序列化成 \" 而 cmd 不认 \" 导致的二次解析破坏
        argv = [shell_exe, "/c", command]
    else:
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
    started = time.monotonic()
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs)
    timed_out = False
    try:
        out_bytes, err_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc, is_nt)
        try:
            out_bytes, err_bytes = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out_bytes, err_bytes = b"", b""
    elapsed = time.monotonic() - started
    # 按系统代码页自适应解码（candidates 已含控制台代码页优先级与乱码评分兜底）
    candidates = _command_output_candidates()
    stdout_text = _decode_command_bytes(out_bytes or b"", candidates=candidates).replace("\r\n", "\n")
    stderr_text = _decode_command_bytes(err_bytes or b"", candidates=candidates).replace("\r\n", "\n")
    return proc.returncode, stdout_text, stderr_text, timed_out, elapsed


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
        result = _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                                        cwd, timeout, is_nt)
        return (*result, None)
    started = time.monotonic()
    try:
        info = _write_relay_script(
            command, cwd, shell_exe, shell_kind, shell_prefix, "fg",
            split_streams=True)
    except Exception:
        result = _run_foreground_direct(command, shell_exe, shell_kind, shell_prefix,
                                        cwd, timeout, is_nt)
        return (*result, None)
    timed_out = False
    deadline = started + timeout
    done_path = Path(info["done"])
    while time.monotonic() < deadline:
        if done_path.exists():
            text = done_path.read_text(encoding="mbcs", errors="ignore").strip()
            if text:
                break  # 退出码已写入
            time.sleep(0.02)  # done 已创建但退出码尚在写入
        else:
            time.sleep(0.05)
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
                subprocess.run(
                    ["taskkill", "/PID", str(root_pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        exit_code = -1
    elapsed = time.monotonic() - started
    candidates = _command_output_candidates()
    out_path, err_path = Path(info["out"]), info.get("err")
    out_bytes = out_path.read_bytes() if out_path.exists() else b""
    err_bytes = err_path.read_bytes() if err_path and Path(err_path).exists() else b""
    stdout_text = _decode_command_bytes(out_bytes, candidates=candidates).replace("\r\n", "\n")
    stderr_text = _decode_command_bytes(err_bytes, candidates=candidates).replace("\r\n", "\n")
    return exit_code, stdout_text, stderr_text, timed_out, elapsed, info


def _run_command_background(command, shell_exe, shell_kind, shell_prefix, cwd, is_nt) -> str:
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
        return "\n".join([
            f"[run_command] 后台模式{'已启动' if alive else '已启动（进程状态未知，可能瞬间结束）'}"
            f" | shell={shell_kind} | pid={info['pid']}",
            f"输出文件: {_display_path(info['out'])}",
            f"结束标记: {_display_path(info['done'])}（命令结束后写入退出码；该文件出现即已结束）",
            "轮询建议: 用 read_file 读取输出文件（推荐）；确认退出码时读取结束标记文件内容",
            f"终止建议: taskkill /PID {info['pid']} /T /F（必须带 /T 终止整棵进程树；"
            "只杀该 pid 会留下实际服务进程）",
        ])
    # POSIX：setsid 分离 + 输出重定向到日志文件
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"bg_{stamp}_{next(_BG_NAME_SEQ) % 10000}_{os.getpid()}"
    out_path = _BG_LOG_DIR / f"{base}.out.txt"
    with open(out_path, "ab") as log_file:
        proc = subprocess.Popen(
            [shell_exe, *shell_prefix, command],
            stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=cwd, start_new_session=True)
    return "\n".join([
        f"[run_command] 后台模式已启动 | shell={shell_kind} | pid={proc.pid}",
        f"输出文件: {_display_path(out_path)}（stdout+stderr 合并追加）",
        "轮询建议: 用 read_file 读取输出文件，或用 ps -p <pid> 确认进程仍在运行",
        f"终止建议: kill -TERM -{proc.pid}（负号=整个进程组，含子进程）",
    ])


def _run_command_impl(command, shell, work_dir, timeout_seconds, background) -> str:
    command = str(command or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    is_nt = os.name == "nt"
    # 顺手清理超过 24h 的历史日志/中转文件（后台输出、前台中转与完整输出文件）
    _prune_old_bg_files()
    shell_exe, shell_kind, shell_prefix = _resolve_shell(shell, is_nt)
    # ===== cmd 家族健壮性补丁（详见文件头补丁说明）=====
    # ① 多行内联代码（python -c / node -e）改写为临时脚本：cmd 按行解释 .cmd，
    #    多行 `-c` 代码必然被拆行执行（实测 `i was unexpected at this time.`）
    command, guard_notes = _rewrite_multiline_inline_code(command, shell_kind)
    # ② 管道过滤器兜底：PATH 里可能命中"非管道过滤器"实现（实测宝塔 tail.exe），
    #    在管道中提前退出且不转发输出 → 整条管道静默无输出；此处探测并替换/本地兜底
    local_filter = None
    if shell_kind == "cmd":
        command, local_filter, pipe_notes = _guard_cmd_pipeline(command)
        guard_notes = guard_notes + pipe_notes
        if local_filter is not None and background:
            # 后台模式无法对输出做本地过滤：回退为"提示 + 保持原命令"，避免静默丢结果
            guard_notes.append(
                "后台模式不支持本地 tail/head 兜底：如输出为空，请改用前台执行或安装可用的 tail/head 实现")
            local_filter = None
    work_text = str(work_dir or "").strip()
    cwd = str(_resolve_dir_path(work_text)) if work_text else os.getcwd()
    try:
        timeout = min(max(float(timeout_seconds), 1.0), 1800.0)
    except (TypeError, ValueError):
        timeout = 120.0
    if background:
        result_text = _run_command_background(
            command, shell_exe, shell_kind, shell_prefix, cwd, is_nt)
        return _append_command_notes(result_text, guard_notes)
    exit_code, stdout_text, stderr_text, timed_out, elapsed, relay_info = _run_command_foreground(
        command, shell_exe, shell_kind, shell_prefix, cwd, timeout, is_nt)
    # 本地 tail/head 兜底：在读取完整输出后按行取头/尾（不改变退出码）
    if local_filter is not None:
        stdout_text = _apply_local_line_filter(stdout_text, local_filter[0], local_filter[1])
    raw_stdout, raw_stderr = stdout_text, stderr_text
    keep_streams = False
    more_hint = ""
    if len(raw_stdout) > _RUN_COMMAND_MAX_CHARS or len(raw_stderr) > 3000:
        # 超长截断：把完整输出落盘，返回里附文件路径（中间被省略部分可用 read_file 续读）
        full_path = _save_full_output(raw_stdout, raw_stderr)
        if full_path is not None:
            more_hint = f"；完整输出: {_display_path(full_path)}（可用 read_file 读取）"
        else:
            # 落盘失败：保留中转输出文件并在返回中给出路径
            refs = [relay_info.get("out"), relay_info.get("err")] if relay_info else []
            refs = [path for path in refs if path]
            if refs:
                keep_streams = True
                more_hint = "；完整输出保留在: " + " | ".join(
                    _display_path(Path(path)) for path in refs)
    stdout_text = _truncate_text(raw_stdout, _RUN_COMMAND_MAX_CHARS, "stdout 过长已截断" + more_hint)
    stderr_text = _truncate_text(raw_stderr, 3000, "stderr 过长已截断" + more_hint)
    if relay_info is not None:
        # 输出已读入内存（或已另存完整版）：清理本次中转临时文件，避免日志目录无限堆积
        _cleanup_relay_files(relay_info, include_streams=not keep_streams)
    header = (f"[run_command] shell={shell_kind} | cwd={_display_path(Path(cwd))}"
              f" | exit={exit_code} | 耗时 {elapsed:.1f}s"
              + (" | 命令超时，进程树已被强制终止" if timed_out else ""))
    # ③ 失败或可疑时的 cmd 语法提示（分号串联 / && 串联 / Unix 命令名）
    if shell_kind == "cmd":
        guard_notes = guard_notes + _cmd_syntax_hints(
            command, exit_code, stdout_text, stderr_text)
    if not stdout_text and not stderr_text:
        return _append_command_notes(f"{header}\n（无输出）", guard_notes)
    parts = [header]
    if stdout_text:
        parts.append("--- stdout ---\n" + stdout_text)
    if stderr_text:
        parts.append("--- stderr ---\n" + stderr_text)
    if timed_out:
        parts.append(
            f"[run_command] 已超过 timeout={timeout:g}s；长任务请改用 background=true，"
            "随后用输出文件路径轮询结果")
    return _append_command_notes("\n".join(parts), guard_notes)

# ------------------- run_command 工具定义与执行入口 -------------------

RUN_COMMAND_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": RUN_COMMAND_NAME,
        "description": (
            "执行终端命令并返回退出码与输出（多 shell：cmd/powershell/pwsh/bash/sh/zsh）\n"
            "参数：\n"
            "    command:         命令原文，由目标 shell 解释（管道、重定向、链式命令、多行均可；\n"
            "                     多行按顺序逐行执行）。每次调用都是新 shell，cd 不跨调用保持\n"
            "                     （用 work_dir 或链式命令）。cmd 家族用 `&&`/`&` 串联（不支持 `;`，\n"
            "                     写了会被当普通字符原样输出）；PowerShell 5.1 用 `;` 串联。\n"
            "                     多行内联代码（python -c）与 `| tail/head -N` 会自动适配，无需特殊写法\n"
            "    shell:           默认 auto（Windows→cmd，非 Windows→bash/sh）；可选 cmd/powershell/\n"
            "                     pwsh/bash/sh/zsh（bash/sh/zsh 在 Windows 上需 Git Bash/WSL）\n"
            "    work_dir:        工作目录，默认当前目录（会话工作目录）；目录不存在时报错\n"
            "    timeout_seconds: 默认 120，可能受限于系统配置\n"
            "    background:      true=后台分离模式：立即返回 pid 与输出/结束标记文件路径（适合长任务）；\n"
            "                     默认 false 前台等待\n"
            "返回：\n"
            "    头部（shell/工作目录/退出码/耗时）+ stdout/stderr 分段；超长截断保留头尾，\n"
            "    完整输出落盘并附文件路径。后台模式读输出文件轮询，结束标记出现即已结束（内容为退出码）。\n"
            "    命令被自动改写/兜底或疑似语法误用时，末尾附 `[run_command] …` 说明行。\n"
            "    文件读写/搜索请优先使用专用工具；本工具适用于安装依赖、运行脚本、git、进程管理等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "命令原文，由目标 shell 解释（管道、重定向、链式命令、多行均可；"
                        "多行按顺序逐行执行）。cmd 家族用 && / & 串联（不支持 ;）"
                    ),
                },
                "shell": {
                    "type": "string",
                    "description": (
                        "目标 shell：auto=默认（Windows→cmd，非 Windows→bash/sh）；"
                        "可选 cmd/powershell/pwsh/bash/sh/zsh"
                    ),
                    "default": "auto",
                },
                "work_dir": {
                    "type": "string",
                    "description": "工作目录，默认当前目录（会话工作目录）；目录不存在时报错",
                    "default": "",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "超时秒数（默认 120，最大 1800）",
                    "default": 120.0,
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "true=后台分离模式：立即返回 pid 与输出/结束标记文件路径"
                        "（适合长任务）；默认 false 前台等待"
                    ),
                    "default": False,
                },
            },
            "required": ["command"],
        },
    },
}


def execute_run_command(tool_args: dict[str, Any]) -> str:
    """内置 run_command 实现：执行终端命令并返回文本结果（同步阻塞）。

    与 MCP 版完全同语义：多 shell、超时杀进程树、超长输出截断并落盘、编码
    自适应、后台分离模式、cmd 家族健壮性补丁。注意：本函数是同步阻塞的，
    调用方必须经 ``asyncio.to_thread`` 放到工作线程执行（与 MCP 工具的
    线程池执行语义一致），避免长时间命令卡死事件循环。
    """
    args = tool_args if isinstance(tool_args, dict) else {}
    return _run_command_impl(
        args.get("command"),
        args.get("shell", "auto"),
        args.get("work_dir", ""),
        args.get("timeout_seconds", 120.0),
        bool(args.get("background")),
    )


def try_execute_builtin_command_tool(
    tool_name: str, tool_args: dict[str, Any]
) -> str | dict[str, Any] | None:
    """执行内置终端命令工具；非目标名称返回 ``None`` 交由其他执行器处理。

    异常统一转为 {"error": ...} 结构化结果（模型可见的错误反馈），与
    try_execute_builtin_file_tool 的错误通道保持同构。
    """
    if tool_name != RUN_COMMAND_NAME:
        return None
    try:
        return execute_run_command(tool_args if isinstance(tool_args, dict) else {})
    except Exception as exc:
        return {"error": str(exc), "tool": tool_name}
