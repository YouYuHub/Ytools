# -*- coding: utf-8 -*-
"""悬空 tool_call 清理回归测试（上游 400 空响应体防护）。

背景：上游（opencode / DeepSeek 等严格网关）要求 assistant.tool_calls 中
每个声明都必须有配对的 tool 结果消息，违反时返回 HTTP 400 且响应体为空
（实测 body 恰为 "0\\r\\n\\r\\n"），客户端重试同一 payload 必然全败。
本组用例覆盖各层防护：
- _replace_todo_context：todo 归并同步清除悬空声明（源头修复）；
- sanitize_tool_call_pairing：发请求前的统一防线；
- copy_for_request：请求副本集成（运行时 messages 不受影响）；
- _round_entry_to_tool_result_context_messages：历史构建侧清理与错挂修复；
- 子智能体 _execute_tool_round：over_task 声明的配对结果。
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import chat_factory
from factory.agent_runtime import chat_runtime
from factory.agent_runtime.chat_runtime import sanitize_tool_call_pairing
from memory.chat_history_format import _round_entry_to_tool_result_context_messages


def _dangling(messages):
    """返回上下文中未配对的 tool_call id 列表（悬空）。"""
    pending = []
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                pending.append(tool_call.get("id"))
        elif message.get("role") == "tool":
            tid = message.get("tool_call_id")
            if tid in pending:
                pending.remove(tid)
    return pending


class ReplaceTodoContextDanglingTests(unittest.TestCase):
    """_replace_todo_context 悬空回归：只删结果、保留声明是 400 的根因。"""

    def test_single_todo_with_content_clears_tool_calls(self):
        # 真实现场：assistant（正文 + 单独一个 todo_write 调用）——归并后
        # tool_calls 必须整体清除（连同配对结果一起消失），正文保留
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "我先更新一下计划。", "tool_calls": [
                {"id": "call_todo_1", "type": "function",
                 "function": {"name": "todo_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_todo_1",
             "_tool_name": "todo_write", "content": "ok"},
        ]
        chat_factory._replace_todo_context(messages, [
            {"content": "步骤一", "status": "done"},
        ])
        self.assertEqual(_dangling(messages), [])
        # 正文保留（有 content 不丢对话信息），声明已清除
        kept = [m for m in messages if m.get("role") == "assistant"]
        self.assertEqual(len(kept), 1)
        self.assertNotIn("tool_calls", kept[0])
        self.assertEqual(kept[0]["content"], "我先更新一下计划。")
        # 配对结果也被删除
        self.assertTrue(all(m.get("role") != "tool" for m in messages))
        # 摘要仍在末尾
        self.assertTrue(messages[-1].get("_todo_summary"))

    def test_single_todo_without_content_drops_message(self):
        # 无正文的纯 todo 调用：整条丢弃（现有行为，保持）
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_todo_1", "function": {"name": "todo_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_todo_1",
             "_tool_name": "todo_write", "content": "ok"},
        ]
        chat_factory._replace_todo_context(messages, [{"content": "x", "status": "done"}])
        self.assertEqual(_dangling(messages), [])
        self.assertEqual([m["role"] for m in messages], ["user", "system"])

    def test_mixed_tool_calls_keeps_only_non_todo(self):
        # 混合调用：todo 声明与结果被移除，其余声明保留且仍有配对结果
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "查询并更新计划", "tool_calls": [
                {"id": "call_search", "function": {"name": "search_files", "arguments": "{}"}},
                {"id": "call_todo_1", "function": {"name": "todo_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_search",
             "_tool_name": "search_files", "content": "结果"},
            {"role": "tool", "tool_call_id": "call_todo_1",
             "_tool_name": "todo_write", "content": "ok"},
        ]
        chat_factory._replace_todo_context(messages, [{"content": "x", "status": "done"}])
        self.assertEqual(_dangling(messages), [])
        assistant = messages[1]
        self.assertEqual(
            [tc["id"] for tc in assistant["tool_calls"]], ["call_search"],
        )
        # search 的配对结果保留、todo 结果删除
        tool_ids = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
        self.assertEqual(tool_ids, ["call_search"])

    def test_idempotent_repeat(self):
        # 重复执行（如两轮连续归并）：第二次执行不得产生新的悬空/异常
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "正文", "tool_calls": [
                {"id": "call_todo_1", "function": {"name": "todo_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_todo_1",
             "_tool_name": "todo_write", "content": "ok"},
        ]
        todos = [{"content": "x", "status": "done"}]
        chat_factory._replace_todo_context(messages, todos)
        first_pass = [dict(m) for m in messages]
        chat_factory._replace_todo_context(messages, todos)
        self.assertEqual(_dangling(messages), [])
        # 第二次执行：旧摘要被替换为新摘要，其余消息不变
        self.assertEqual(len(messages), len(first_pass))


class SanitizeToolCallPairingTests(unittest.TestCase):
    """统一防线：请求前清理悬空声明。"""

    def test_dangling_with_content_kept_as_text(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "正文保留", "tool_calls": [
                {"id": "ghost_1", "function": {"name": "run_command", "arguments": "{}"}},
            ]},
        ]
        cleaned, removed = sanitize_tool_call_pairing(messages)
        self.assertEqual(removed, 1)
        self.assertEqual(_dangling(cleaned), [])
        self.assertEqual(cleaned[1]["content"], "正文保留")
        self.assertNotIn("tool_calls", cleaned[1])

    def test_dangling_without_content_dropped(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "ghost_1", "function": {"name": "run_command", "arguments": "{}"}},
            ]},
        ]
        cleaned, removed = sanitize_tool_call_pairing(messages)
        self.assertEqual(removed, 1)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["role"], "user")

    def test_paired_messages_reused_by_reference(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "调用", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ]
        cleaned, removed = sanitize_tool_call_pairing(messages)
        self.assertEqual(removed, 0)
        # 全部配对：原样复用引用（无拷贝开销）
        self.assertIs(cleaned[1], messages[1])
        self.assertIs(cleaned[2], messages[2])

    def test_partial_cleanup_keeps_paired_declaration(self):
        messages = [
            {"role": "assistant", "content": "两个调用", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "search_files", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ]
        cleaned, removed = sanitize_tool_call_pairing(messages)
        self.assertEqual(removed, 1)
        self.assertEqual(
            [tc["id"] for tc in cleaned[0]["tool_calls"]], ["c1"],
        )

    def test_empty_or_missing_id_declarations_kept(self):
        # 畸形声明（无 id / 空 id）：无法判断，保留原样（不主动引入差异）
        messages = [
            {"role": "assistant", "content": "x", "tool_calls": [
                {"function": {"name": "f", "arguments": "{}"}},
                {"id": "", "function": {"name": "g", "arguments": "{}"}},
            ]},
        ]
        cleaned, removed = sanitize_tool_call_pairing(messages)
        self.assertEqual(removed, 0)
        self.assertIs(cleaned[0], messages[0])

    def test_input_not_mutated(self):
        messages = [
            {"role": "assistant", "content": "x", "tool_calls": [
                {"id": "ghost_1", "function": {"name": "f", "arguments": "{}"}},
            ]},
        ]
        sanitize_tool_call_pairing(messages)
        # 原列表/消息不受影响（只返回新列表）
        self.assertEqual(len(messages[0]["tool_calls"]), 1)

    def test_empty_list(self):
        self.assertEqual(sanitize_tool_call_pairing([]), ([], 0))


class CopyForRequestIntegrationTests(unittest.TestCase):
    """copy_for_request 集成：请求副本清理悬空，运行时 messages 不受影响。"""

    def test_request_copy_cleans_dangling(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "旧轮正文", "tool_calls": [
                {"id": "ghost_1", "function": {"name": "run_command", "arguments": "{}"}},
            ]},
            {"role": "assistant", "content": "最新回复"},
        ]
        copied = chat_runtime.copy_for_request(messages)
        self.assertEqual(_dangling(copied), [])
        # 运行时 messages 保留原始值（清理只发生在请求副本上）
        self.assertEqual(len(messages[1]["tool_calls"]), 1)


class HistoryRoundDanglingTests(unittest.TestCase):
    """历史构建侧：中断轮次的悬空声明被清理、错挂结果不再制造新悬空。"""

    def test_interrupted_round_declaration_without_result_cleaned(self):
        # 并行双调用中断：call2 无结果——构建后 call2 声明应被清除
        round_entry = {
            "question": "测试",
            "events": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "content": "开始", "tool_calls": [
                    {"id": "call_a1", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "call_a2", "function": {"name": "search_files", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "call_a1", "content": "文件内容"},
            ],
        }
        messages = _round_entry_to_tool_result_context_messages(round_entry, -1)
        self.assertEqual(_dangling(messages), [])
        assistant = [m for m in messages if m.get("role") == "assistant"][0]
        self.assertEqual([tc["id"] for tc in assistant["tool_calls"]], ["call_a1"])

    def test_result_for_unknown_declaration_skipped_not_misattached(self):
        # 结果的声明不在本轮（已被压缩覆盖/中断残留）：跳过该结果而不是
        # 错挂到最近调用（错挂会让真属主声明悬空、该调用多收结果）
        round_entry = {
            "question": "测试",
            "events": [
                {"role": "user", "content": "测试"},
                {"role": "assistant", "tool_calls": [
                    {"id": "call_real", "function": {"name": "read_file", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "call_ghost", "content": "来自其它轮的残留"},
                {"role": "tool", "tool_call_id": "call_real", "content": "真实结果"},
            ],
        }
        messages = _round_entry_to_tool_result_context_messages(round_entry, -1)
        self.assertEqual(_dangling(messages), [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["call_real"])
        self.assertEqual(tool_msgs[0]["content"], "真实结果")

    def test_missing_id_result_falls_back_to_latest_call(self):
        # 兼容路径：结果缺 tool_call_id（旧格式数据）仍回退挂到最近调用
        round_entry = {
            "question": "查时间",
            "events": [
                {"role": "user", "content": "现在几点？"},
                {"role": "assistant", "tool_calls": [
                    {"function": {"name": "run_pipe_command", "arguments": "{}"}}
                ]},
                {"role": "tool", "content": "14:20"},
            ],
        }
        messages = _round_entry_to_tool_result_context_messages(round_entry, -1)
        self.assertEqual(_dangling(messages), [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["content"], "14:20")


class SubAgentOverTaskPairingTests(unittest.TestCase):
    """子智能体执行侧：over_task 声明必须生成配对结果。"""

    def _runner(self):
        from factory.agent_runtime.sub_agent import (
            SubAgentContext, SubAgentRunner, new_agent_id,
        )

        async def emit(payload):
            return None

        ctx = SubAgentContext(
            agent_id=new_agent_id(),
            parent_agent_id="main",
            parent_tool_call_id="call_p1",
            parent_tool_index=0,
            agent_index=0,
            session_id="sub_over_task_test",
            task="子任务",
            initial_todo=None,
            tools=[],
            tool_servers={},
            configured_tool_names=set(),
            configured_tool_servers={},
            max_rounds=3,
            timeout_seconds=0,
            reply_max_chars=2000,
            emit_event=emit,
            stop_checker=lambda: False,
        )
        return SubAgentRunner(ctx)

    def test_over_task_call_gets_paired_result(self):
        runner = self._runner()
        tool_call = {
            "id": "call_ot1",
            "type": "function",
            "function": {"name": "over_task", "arguments": "{}"},
        }
        results = asyncio.run(runner._execute_tool_round([tool_call]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_call"]["id"], "call_ot1")
        self.assertIn("over_task", results[0]["result"])

    def test_blocked_tool_declaration_kept_in_execution(self):
        # 未授权工具：执行侧给出拒绝结果（与父循环一致），供声明配对
        runner = self._runner()
        tool_call = {
            "id": "call_b1",
            "type": "function",
            "function": {"name": "no_such_tool", "arguments": "{}"},
        }
        results = asyncio.run(runner._execute_tool_round([tool_call]))
        self.assertEqual(len(results), 1)
        self.assertIn("未在子任务工具列表中", results[0]["result"])


if __name__ == "__main__":
    unittest.main()
