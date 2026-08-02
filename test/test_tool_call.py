import unittest
from unittest.mock import patch

from factory import tool_registry
from config import FunctionDefinition


class ToolRegistryDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_tools_from_mcp_returns_discovery_metadata(self):
        async def fake_get_mcp_tools(_server_id: str):
            return [
                FunctionDefinition(
                    name="mock_tool",
                    description="mock desc",
                    parameters={"type": "object", "properties": {}},
                )
            ]

        with patch("factory.tool_registry._read_server_ids", return_value=["sysServer"]), \
             patch("factory.tool_registry._resolve_server_ref", return_value="python mcp_server/sys_server.py"), \
             patch("factory.tool_registry.get_mcp_tools", side_effect=fake_get_mcp_tools), \
             patch("factory.tool_registry.load_var", side_effect=lambda k, d=None: {"MCP_DISCOVERY_MAX_CONCURRENCY": "2", "MCP_DISCOVERY_TIMEOUT_SECONDS": "5"}.get(k, d)):
            result = await tool_registry.refresh_tools_from_mcp(".")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["failed_servers"], [])
        self.assertIn("discovery", result)
        self.assertEqual(result["discovery"]["max_concurrency"], 1)
        self.assertEqual(result["discovery"]["timeout_seconds"], 5.0)

    async def test_refresh_tools_from_mcp_marks_failed_servers(self):
        async def fake_get_mcp_tools(server_id: str):
            if server_id == "badServer":
                raise RuntimeError("connect failed")
            return [FunctionDefinition(name="ok_tool", description="", parameters={})]

        with patch("factory.tool_registry._read_server_ids", return_value=["badServer", "sysServer"]), \
             patch("factory.tool_registry._resolve_server_ref", side_effect=lambda _dir, sid: sid), \
             patch("factory.tool_registry.get_mcp_tools", side_effect=fake_get_mcp_tools), \
             patch("factory.tool_registry.load_var", side_effect=lambda k, d=None: {"MCP_DISCOVERY_MAX_CONCURRENCY": "2", "MCP_DISCOVERY_TIMEOUT_SECONDS": "5"}.get(k, d)):
            result = await tool_registry.refresh_tools_from_mcp(".")

        self.assertEqual(result["total"], 1)
        self.assertIn("badServer", result["failed_servers"])
        self.assertEqual(result["tools"][0]["function"]["name"], "ok_tool")


if __name__ == "__main__":
    unittest.main()
