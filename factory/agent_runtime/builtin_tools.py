"""由服务端本地执行的内置工具。"""
from __future__ import annotations

import json

from typing import Any


CHECK_TOOL_EXISTS_NAME = "check_tool_exists"
TODO_TOOL_NAME = "todo_write"
ASK_USER_TOOL_NAME = "ask_user"
# 内置工具在工具选择（_meta.tool_selection / mcp_servers.json inputs）中的伪服务键：
# 前端把它作为“内置工具”分组渲染在工具模态框首位，保存/加载与 MCP 工具走同一链路
BUILTIN_TOOL_SERVER_KEY = "__builtin__"
# 工具选择中允许出现的内置工具名（check_tool_exists 由后端按外部工具自动注入，不开放手选）
SELECTABLE_BUILTIN_TOOL_NAMES = (TODO_TOOL_NAME, ASK_USER_TOOL_NAME)
# 前端回答 ask_user 提问时的消息前缀（前端 app.js 中同名拼接，需保持一致）；
# 后端据此识别"覆盖式重新回答"：截断最新提问轮之后的旧回答轮
ASK_ANSWER_PREFIX = "【回答模型提问】"
CHECK_TOOL_EXISTS_DEFINITION = {
    "type": "function",
    "function": {
      "name": CHECK_TOOL_EXISTS_NAME,
      "description": "检查指定工具是否在后端实时可用工具中存在，建议某个工具使用失败时才进行检查",
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
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """注入本地内置工具：check_tool_exists（选了外部工具时）、todo_write 与
    ask_user（用户在工具选择中勾选时）。"""
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
    return tools, servers


def is_builtin_tool(tool_name: str) -> bool:
    return tool_name in (CHECK_TOOL_EXISTS_NAME, TODO_TOOL_NAME, ASK_USER_TOOL_NAME)


TODO_TOOL_DEFINITION = {
    "type": "function",
    "function": {
      "name": TODO_TOOL_NAME,
      "description": (
          "写入/更新当前任务计划（todo list）。每次调用提交完整的计划列表（全量覆盖），"
          "系统会把计划展示给用户并帮助你跟踪多步骤任务。"
          # "任务包含多个步骤、需要长期规划或用户要求制定计划时使用；简单一次性任务不要使用。"
          "开始一个步骤前先把该步骤置为 in_progress，完成后置为 done，并按最新进展调整后续步骤。"
          "不建议简单任务使用，复杂任务建议所有步骤都置为 done 后汇报结果。"
      ),
      "parameters": {
        "type": "object",
        "properties": {
          "todos": {
            "type": "array",
            "description": "完整的任务计划列表（每次全量覆盖，最多 20 项）",
            "items": {
              "type": "object",
              "properties": {
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
              "required": ["content", "status"],
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


def normalize_todo_items(raw: Any) -> list[dict[str, str]] | None:
    """校验并规整模型提交的 todo 列表；任何一项非法整体拒绝（返回 None）。"""
    if not isinstance(raw, list):
        return None
    if len(raw) > _TODO_MAX_ITEMS:
        return None
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        content = str(entry.get("content") or "").strip()
        status = str(entry.get("status") or "pending").strip()
        if not content or len(content) > _TODO_MAX_CONTENT_CHARS:
            return None
        if status not in _TODO_VALID_STATUSES:
            return None
        items.append({"content": content, "status": status})
    return items


def execute_builtin_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    available_tool_names: set[str] | list[str],
    available_tool_servers: dict[str, str],
) -> dict[str, Any] | None:
    """执行内置工具；非内置名称返回 ``None`` 交由 MCP 执行器处理。"""
    if tool_name != CHECK_TOOL_EXISTS_NAME:
        return None
    query_name = ""
    if isinstance(tool_args, dict):
        query_name = str(tool_args.get("tool_name", "")).strip()
    exists = bool(query_name and query_name in available_tool_names)
    return {
        "tool_name": query_name,
        "exists": exists,
        "server": available_tool_servers.get(query_name, "") if exists else "",
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
            "一次提出 1-3 个问题，问题要具体、选项要互斥且可直接执行；"
            "需要用户同时选择多项时把该题的 multiple 设为 true。"
            "注意：调用后当前任务会暂停等待用户回答，收到用户的回答消息后再继续任务；"
            "不要用本工具进行闲聊或询问常识性问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "问题列表（1-3 个）",
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