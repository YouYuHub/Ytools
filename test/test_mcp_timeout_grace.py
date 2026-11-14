"""MCP 调用超时宽限：实际等待 = 配置值 + 5 秒。

实测漏洞：工具内部超时与配置值相同时（如都是 60 秒），客户端在边界
抢先掐断，工具输出丢失。宽限让工具先超时返回、输出正常落袋。
"""
import asyncio
import unittest
from unittest.mock import patch

from config import MCP_TOOL_CALL_TIMEOUT_GRACE_SECONDS
from util import mcp_client


class EffectiveTimeoutTests(unittest.TestCase):
    def test_default_grace_is_five_seconds(self):
        self.assertEqual(MCP_TOOL_CALL_TIMEOUT_GRACE_SECONDS, 5.0)

    def test_configured_plus_grace(self):
        self.assertAlmostEqual(mcp_client._mcp_effective_timeout_seconds(60), 65.0)
        self.assertAlmostEqual(mcp_client._mcp_effective_timeout_seconds(300), 305.0)

    def test_unlimited_stays_unlimited(self):
        self.assertEqual(mcp_client._mcp_effective_timeout_seconds(0), 0.0)
        self.assertEqual(mcp_client._mcp_effective_timeout_seconds(-1), 0.0)


class FakeClient:
    def __init__(self, delay, result="tool-output"):
        self.delay = delay
        self.result = result

    async def call(self, *_args):
        await asyncio.sleep(self.delay)
        return self.result


def _patched_call(delay, configured, grace, result="tool-output"):
    client = FakeClient(delay, result)
    with patch.object(mcp_client, "_persistent_server_signature", return_value="sig"), \
            patch.object(mcp_client, "_get_persistent_client", return_value=client), \
            patch.object(mcp_client, "_drop_persistent_client"), \
            patch.object(mcp_client, "_mcp_call_timeout_seconds", return_value=configured), \
            patch.object(mcp_client, "MCP_TOOL_CALL_TIMEOUT_GRACE_SECONDS", grace):
        return asyncio.run(mcp_client.call_mcp_tool("browser_snapshot", {}, "Browser"))


class GraceBehaviorTests(unittest.TestCase):
    def test_boundary_tool_output_survives_within_grace(self):
        # 工具耗时 0.07 > 配置 0.05：无宽限会被掐断；宽限 0.05 下正常返回
        ret = _patched_call(0.07, 0.05, 0.05)
        self.assertIn("tool-output", ret)

    def test_truly_stalled_tool_still_times_out_with_both_numbers(self):
        with self.assertRaises(TimeoutError) as ctx:
            _patched_call(0.3, 0.05, 0.05)
        detail = str(ctx.exception)
        self.assertIn("超过 0.05 秒无响应", detail)
        self.assertIn("实际等待 0.1 秒", detail)

    def test_zero_grace_keeps_old_boundary_behavior(self):
        # 宽限为 0 时回到旧语义：边界完成即超时（旧测试的提速补丁依赖此口径）
        with self.assertRaises(TimeoutError):
            _patched_call(0.07, 0.05, 0)


if __name__ == "__main__":
    unittest.main()
