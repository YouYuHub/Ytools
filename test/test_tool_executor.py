import asyncio
import re
import unittest
from unittest.mock import patch

from factory.agent_runtime.tool_executor import (
    _invoke_tool_function,
    execute_tool_round,
    normalize_tool_calls,
    parse_tool_call,
    prepare_tool_execution,
)
import util.mcp_client as mcp_client
from util.timestamp_utils import now_slash_str


class ToolExecutorTests(unittest.TestCase):
    def test_parse_tool_call_parses_arguments_json(self):
        tool_name, arguments = parse_tool_call({
            "function": {
                "name": "format_current_time",
                "arguments": "{\"timezone\": \"Asia/Shanghai\"}"
            }
        })

        self.assertEqual(tool_name, "format_current_time")
        self.assertEqual(arguments["timezone"], "Asia/Shanghai")

    def test_normalize_tool_calls_splits_concatenated_name(self):
        normalized = normalize_tool_calls(
            [
                {
                    "index": 0,
                    "id": "call_demo",
                    "type": "function",
                    "function": {
                        "name": "run_pipe_commandread_pipe_history",
                        "arguments": "{}{}"
                    }
                }
            ],
            ["run_pipe_command", "read_pipe_history", "over_task"],
        )

        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["function"]["name"], "run_pipe_command")
        self.assertEqual(normalized[1]["function"]["name"], "read_pipe_history")

    def test_prepare_tool_execution_separates_over_task(self):
        plan = prepare_tool_execution(
            [
                {
                    "index": 0,
                    "function": {
                        "name": "over_task",
                        "arguments": "{}"
                    }
                },
                {
                    "index": 1,
                    "function": {
                        "name": "format_current_time",
                        "arguments": "{}"
                    }
                },
            ],
            ["format_current_time", "over_task"],
        )

        self.assertIsNotNone(plan.over_task_call)
        self.assertEqual(plan.parsed_tools[0][2], "format_current_time")
        self.assertFalse(plan.has_parse_error)

    def test_invoke_tool_function_propagates_client_timeout(self):
        async def timed_out_call(**_kwargs):
            raise TimeoutError("client timeout")

        with patch("factory.agent_runtime.tool_executor.call_mcp_tool", timed_out_call):
            with self.assertRaisesRegex(TimeoutError, "client timeout"):
                _invoke_tool_function("slow_tool", {}, {"slow_tool": "server"})

    def test_invoke_tool_function_restores_original_mcp_name(self):
        calls = []

        async def fake_call(**kwargs):
            calls.append(kwargs)
            return "ok"

        with patch("factory.agent_runtime.tool_executor.call_mcp_tool", fake_call):
            result = _invoke_tool_function(
                "mcp_123456789abc_find_item",
                {"query": "x"},
                {"mcp_123456789abc_find_item": "catalog_server"},
                {"mcp_123456789abc_find_item": "find_item"},
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls[0]["function_name"], "find_item")
        self.assertEqual(calls[0]["mcp_service"], "catalog_server")

    def test_pipe_connection_error_is_not_retried_by_executor(self):
        calls = []

        async def failed_call(**_kwargs):
            calls.append(1)
            raise RuntimeError("Failed to connect to named-pipe server")

        with patch("factory.agent_runtime.tool_executor.call_mcp_tool", failed_call):
            with self.assertRaisesRegex(RuntimeError, "named-pipe server"):
                _invoke_tool_function(
                    "run_pipe_command", {}, {"run_pipe_command": "pipe_server"}
                )

        self.assertEqual(len(calls), 1)

    def test_mcp_client_direct_call_honors_timeout(self):
        async def slow_call(*_args, **_kwargs):
            await asyncio.sleep(0.05)

        async def run():
            with patch.object(mcp_client, "load_var", return_value="0.01"), \
                    patch.object(mcp_client, "_call_mcp_tool_impl", slow_call):
                with self.assertRaises(asyncio.TimeoutError):
                    await mcp_client.call_mcp_tool("slow_tool", {})

        asyncio.run(run())

    def test_mcp_client_timeout_carries_tool_and_seconds(self):
        """超时异常必须带可读信息（工具名/服务/秒数），不能是裸 TimeoutError。"""
        async def slow_call(*_args, **_kwargs):
            await asyncio.sleep(0.05)

        async def run():
            with patch.object(mcp_client, "load_var", return_value="0.01"), \
                    patch.object(mcp_client, "_call_mcp_tool_impl", slow_call):
                with self.assertRaisesRegex(TimeoutError, "超时") as ctx:
                    await mcp_client.call_mcp_tool(
                        "slow_tool", {}, mcp_service="demo_server"
                    )
            message = str(ctx.exception)
            self.assertIn("slow_tool", message)
            self.assertIn("demo_server", message)
            self.assertIn("0.01", message)

        asyncio.run(run())

    def test_execute_tool_round_reports_timeout_accurately(self):
        """裸超时异常也要报成超时（含工具名与系统时间），不能归一成执行失败。"""
        async def timed_out_call(**_kwargs):
            raise asyncio.TimeoutError()

        with patch("factory.agent_runtime.tool_executor.call_mcp_tool", timed_out_call):
            results = execute_tool_round(
                [(1, {"id": "call_1"}, "slow_tool", {})],
                {"slow_tool": "server"},
                1,
            )
        self.assertEqual(len(results), 1)
        ret = results[0]["result"]
        self.assertIn("MCP 工具调用超时", ret)
        self.assertIn("slow_tool", ret)
        self.assertNotIn("工具执行失败", ret)
        self.assertRegex(ret, r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}")
        self.assertIsInstance(results[0]["error"], TimeoutError)

    def test_execute_tool_round_error_keeps_original_and_timestamp(self):
        """普通错误保留原始信息，并附加系统时间。"""
        async def failed_call(**_kwargs):
            raise RuntimeError("MCP 调用失败: boom")

        with patch("factory.agent_runtime.tool_executor.call_mcp_tool", failed_call):
            results = execute_tool_round(
                [(1, {"id": "call_1"}, "demo_tool", {})],
                {"demo_tool": "server"},
                1,
            )
        ret = results[0]["result"]
        self.assertIn("工具执行失败", ret)
        self.assertIn("MCP 调用失败: boom", ret)
        self.assertRegex(ret, r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}")

    def test_now_slash_str_format(self):
        self.assertRegex(now_slash_str(), r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
