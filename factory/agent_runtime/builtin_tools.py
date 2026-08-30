"""由服务端本地执行的内置工具。"""
from __future__ import annotations

from typing import Any


CHECK_TOOL_EXISTS_NAME = "check_tool_exists"
TODO_TOOL_NAME = "todo_write"
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
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """注入本地内置工具：check_tool_exists（选了外部工具时）与 todo_write（用户启用时）。"""
    tools = list(selected_tools)
    servers = dict(selected_tool_servers)
    if selected_tools and CHECK_TOOL_EXISTS_NAME not in servers:
        tools.append(CHECK_TOOL_EXISTS_DEFINITION)
        servers[CHECK_TOOL_EXISTS_NAME] = "__builtin__"
    # todo_write 独立于外部工具：用户显式启用即注入（即使没有选择任何 MCP 工具）
    if include_todo and TODO_TOOL_NAME not in servers:
        tools.append(TODO_TOOL_DEFINITION)
        servers[TODO_TOOL_NAME] = "__builtin__"
    return tools, servers


def is_builtin_tool(tool_name: str) -> bool:
    return tool_name in (CHECK_TOOL_EXISTS_NAME, TODO_TOOL_NAME)


TODO_TOOL_DEFINITION = {
    "type": "function",
    "function": {
      "name": TODO_TOOL_NAME,
      "description": (
          "写入/更新当前任务计划（todo list）。每次调用提交完整的计划列表（全量覆盖），"
          "系统会把计划展示给用户并帮助你跟踪多步骤任务。"
          "任务包含多个步骤、需要长期规划或用户要求制定计划时使用；简单一次性任务不要使用。"
          "开始一个步骤前先把该步骤置为 in_progress，完成后置为 done，并按最新进展调整后续步骤。"
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