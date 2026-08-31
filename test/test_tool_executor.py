import asyncio
import unittest
from unittest.mock import patch

from factory.agent_runtime.tool_executor import (
    _invoke_tool_function,
    normalize_tool_calls,
    parse_tool_call,
    prepare_tool_execution,
)
import util.mcp_client as mcp_client


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

    def test_invoke_tool_function_honors_configured_timeout(self):
        async def slow_call(**_kwargs):
            await asyncio.sleep(0.05)

        with patch("factory.agent_runtime.tool_executor.load_var", return_value="0.01"), \
                patch("factory.agent_runtime.tool_executor.call_mcp_tool", slow_call):
            with self.assertRaises(TimeoutError):
                _invoke_tool_function("slow_tool", {}, {"slow_tool": "server"})

    def test_mcp_client_direct_call_honors_timeout(self):
        async def slow_call(*_args, **_kwargs):
            await asyncio.sleep(0.05)

        async def run():
            with patch.object(mcp_client, "load_var", return_value="0.01"), \
                    patch.object(mcp_client, "_call_mcp_tool_impl", slow_call):
                with self.assertRaises(asyncio.TimeoutError):
                    await mcp_client.call_mcp_tool("slow_tool", {})

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
