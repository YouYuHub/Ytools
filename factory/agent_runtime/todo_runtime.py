"""每个任务独立的计划空转保护；不更改已保存的计划或工具历史。"""
from typing import Any

TODO_NAME = "todo_write"


def latest_todo_exchanges(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取最近一次真实计划返回和最近一次纠错返回，保持原生调用/结果配对。

    同时接受模型消息和 JSONL 事件；不把正文中的调用标记转为可执行工具。
    """
    import copy
    import json

    declarations = {}
    latest = latest_plan = None
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("function", {}).get("name") == TODO_NAME and call.get("id"):
                    declarations[call["id"]] = call
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in declarations:
                continue
            content = message.get("content", message.get("result", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            pair = [
                {"role": "assistant", "tool_calls": [copy.deepcopy(declarations[call_id])]},
                {"role": "tool", "tool_call_id": call_id, "content": content,
                 "_tool_name": TODO_NAME},
            ]
            latest = pair
            try:
                result = json.loads(content)
                if (isinstance(result, dict) and isinstance(result.get("todos"), list)
                        and not result.get("error") and result.get("success") is not False):
                    latest_plan = pair
            except (TypeError, ValueError):
                pass
    if latest is None:
        return []
    return (latest_plan if latest_plan and latest_plan != latest else []) + latest


class TodoContext:
    """只保留真实的最近计划工具交互；压缩后恢复，不嵌入系统提示词。"""

    def __init__(self, exchanges: list[dict[str, Any]] | None = None):
        self.exchanges = exchanges or []
        self.previous_ids = {m["tool_call_id"] for m in self.exchanges if m.get("role") == "tool"}

    def sync(self, messages: list[dict[str, Any]]) -> None:
        import copy
        import json

        # Native pending tool exchanges must remain intact. The adapter replays
        # their signed prefix, and validates every result before the next call.
        if any(m.get("provider_state", {}).get("record_id") and m.get("tool_calls") for m in messages):
            self.exchanges = latest_todo_exchanges(messages)
            return

        present = {call.get("id") for message in messages
                   for call in message.get("tool_calls") or []}
        for index in range(0, len(self.exchanges), 2):
            pair = self.exchanges[index:index + 2]
            if len(pair) == 2 and pair[1].get("tool_call_id") in present:
                # 历史回传长度可能截断 JSON；已知真实完整返回优先，避免计划只剩半截。
                for i, message in enumerate(messages):
                    if message.get("role") == "tool" and message.get("tool_call_id") == pair[1]["tool_call_id"]:
                        try:
                            json.loads(message.get("content", ""))
                        except (ValueError, TypeError):
                            messages[i] = copy.deepcopy(pair[1])
                        break
            if len(pair) == 2 and pair[1].get("tool_call_id") not in present:
                insert_at = len(messages)
                if pair[1]["tool_call_id"] in self.previous_ids:
                    # 上一任务的交互放在当前用户指令之前，防止旧收官反馈覆盖新任务。
                    insert_at = next((i for i in range(len(messages) - 1, -1, -1)
                                      if messages[i].get("role") == "user"
                                      and not messages[i].get("_internal")), len(messages))
                messages[insert_at:insert_at] = copy.deepcopy(pair)
        exchanges = latest_todo_exchanges(messages)
        keep_ids = {m["tool_call_id"] for m in exchanges if m.get("role") == "tool"}
        todo_ids = {call.get("id") for message in messages
                    for call in message.get("tool_calls") or []
                    if call.get("function", {}).get("name") == TODO_NAME}
        compacted = []
        for message in messages:
            if message.get("_todo_summary"):
                continue
            if message.get("role") == "assistant" and message.get("tool_calls"):
                kept = [call for call in message["tool_calls"] if (
                    call.get("function", {}).get("name") != TODO_NAME or call.get("id") in keep_ids
                )]
                message = dict(message)
                if kept:
                    message["tool_calls"] = kept
                else:
                    message.pop("tool_calls", None)
                    if not message.get("content"):
                        continue
            if (message.get("role") == "tool" and (
                    message.get("tool_call_id") in todo_ids
                    or message.get("_tool_name", message.get("tool_name")) == TODO_NAME
            ) and message.get("tool_call_id") not in keep_ids):
                continue
            compacted.append(message)
        messages[:] = compacted
        self.exchanges = exchanges


class TodoLoopGuard:
    """连续三次只调用计划工具后暂停它，其他工具调用后恢复。

    按工具轮而非计划相等性计数，避免改 id、措辞或状态绕过保护。
    模型无视暂停再调用两轮时终止空转；新用户指令可 reset。
    """

    def __init__(self):
        self.streak = 0

    def reset(self):
        self.streak = 0

    @property
    def suspended(self) -> bool:
        return self.streak >= 3

    @property
    def exhausted(self) -> bool:
        return self.streak >= 5

    def observe(self, names: list[str]) -> bool:
        """返回本批 todo 是否应拒绝；混合批中有其他工具则立即恢复。"""
        if not names:
            return False
        if any(name != TODO_NAME for name in names):
            self.reset()
            return False
        blocked = self.suspended
        self.streak += 1
        return blocked

    def filter_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [tool for tool in tools if not (
            self.suspended and tool.get("function", {}).get("name") == TODO_NAME
        )]

    def update_context(self, messages: list[dict[str, Any]]) -> None:
        messages[:] = [m for m in messages if not m.get("_todo_loop_notice")]
        if self.suspended:
            messages.append({
                "role": "system", "_todo_loop_notice": True,
                "content": (
                    "连续多轮只更新计划，未调用其他工具。todo_write 暂时不可用，"
                    "当前计划已经保存。请使用其他工具执行实际工作，之后计划工具会恢复；"
                    "如果已完成，请直接汇总结果；如果无法继续，请说明实际进展和阻碍。"
                    "不要为了结束而虚报步骤完成，也不要输出文本形式的工具调用。"
                ),
            })

    def rejection(self) -> dict[str, Any]:
        return {"success": False, "error": "连续只更新计划，todo_write 已暂停；请执行其他工具或直接报告实际进展。",
                "code": "TODO_LOOP_GUARD", "unchanged": True}

    @property
    def error_message(self) -> str:
        return "模型连续只调用 todo_write，且在暂停该工具后仍重复调用，已终止本次空转。计划保持原状态，请重新发起任务。"
