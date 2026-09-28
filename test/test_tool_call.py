import unittest
from unittest.mock import patch

from factory.agent_runtime import tool_registry
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

        with patch("factory.agent_runtime.tool_registry._read_server_ids", return_value=["sysServer"]), \
               patch("factory.agent_runtime.tool_registry.get_mcp_tools", side_effect=fake_get_mcp_tools), \
               patch("factory.agent_runtime.tool_registry.load_var", side_effect=lambda k, d=None: {"MCP_DISCOVERY_MAX_CONCURRENCY": "2", "MCP_DISCOVERY_TIMEOUT_SECONDS": "5"}.get(k, d)):
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

        with patch("factory.agent_runtime.tool_registry._read_server_ids", return_value=["badServer", "sysServer"]), \
               patch("factory.agent_runtime.tool_registry.get_mcp_tools", side_effect=fake_get_mcp_tools), \
               patch("factory.agent_runtime.tool_registry.load_var", side_effect=lambda k, d=None: {"MCP_DISCOVERY_MAX_CONCURRENCY": "2", "MCP_DISCOVERY_TIMEOUT_SECONDS": "5"}.get(k, d)):
            result = await tool_registry.refresh_tools_from_mcp(".")

        self.assertEqual(result["total"], 1)
        self.assertIn("badServer", result["failed_servers"])
        self.assertEqual(result["tools"][0]["function"]["name"], "ok_tool")

    async def test_duplicate_and_builtin_names_get_stable_model_names(self):
        async def fake_get_mcp_tools(server_id: str):
            names = ["shared_tool", "read_file"]
            if server_id == "alpha":
                names.append("unique_tool")
            return [FunctionDefinition(name=name, description="", parameters={}) for name in names]

        with patch("factory.agent_runtime.tool_registry._read_server_ids", return_value=["alpha", "beta"]), \
               patch("factory.agent_runtime.tool_registry.get_mcp_tools", side_effect=fake_get_mcp_tools), \
               patch("factory.agent_runtime.tool_registry.load_var", side_effect=lambda _k, d=None: d):
            result = await tool_registry.refresh_tools_from_mcp(".")

        by_identity = {
            (tool["server_id"], tool["original_name"]): tool["function"]["name"]
            for tool in result["tools"]
        }
        alpha_shared = by_identity[("alpha", "shared_tool")]
        beta_shared = by_identity[("beta", "shared_tool")]
        alpha_read = by_identity[("alpha", "read_file")]
        beta_read = by_identity[("beta", "read_file")]

        self.assertNotEqual(alpha_shared, beta_shared)
        self.assertNotEqual(alpha_read, "read_file")
        self.assertNotEqual(beta_read, "read_file")
        self.assertEqual(by_identity[("alpha", "unique_tool")], "unique_tool")
        self.assertEqual(
            tool_registry.resolve_tool_selection({"alpha": ["shared_tool", "read_file"]}),
            [alpha_shared, alpha_read],
        )
        canonical, ambiguous = tool_registry.canonicalize_requested_names(["shared_tool"])
        self.assertEqual(canonical, [])
        self.assertEqual(ambiguous, ["shared_tool"])
        canonical, ambiguous = tool_registry.canonicalize_requested_names(["unique_tool"])
        self.assertEqual(canonical, ["unique_tool"])
        self.assertEqual(ambiguous, [])


if __name__ == "__main__":
    unittest.main()
