"""工具探测缓存跨进程传递（IPC 快照）回归。

背景：worker 是 spawn 的独立进程，模块级 _TOOLS_CACHE 不共享；父进程探测完
缓存后必须能把「缓存 + 注册表」一起下发给 worker，否则每个 worker 首次生成
都要重新拉起全部 MCP 子进程（秒级~十秒级，落在首 token 前）。
"""
import unittest

from factory.agent_runtime import tool_registry as registry


class ToolsCacheSnapshotTests(unittest.TestCase):
    def setUp(self):
        # 保存并清空全局状态，避免与其他用例互相污染
        self._saved = (
            registry._TOOLS_CACHE,
            registry._TOOLS_CACHE_MONO,
            registry.ALL_TOOLS,
            registry.TOOL_MCP_SERVERS,
            registry.TOOL_ORIGINAL_NAMES,
            registry.TOOL_NAME_INDEX,
        )
        registry._TOOLS_CACHE = None
        registry._TOOLS_CACHE_MONO = 0.0
        registry.ALL_TOOLS = []
        registry.TOOL_MCP_SERVERS = {}
        registry.TOOL_ORIGINAL_NAMES = {}
        registry.TOOL_NAME_INDEX = {}

    def tearDown(self):
        (registry._TOOLS_CACHE,
         registry._TOOLS_CACHE_MONO,
         registry.ALL_TOOLS,
         registry.TOOL_MCP_SERVERS,
         registry.TOOL_ORIGINAL_NAMES,
         registry.TOOL_NAME_INDEX) = self._saved

    def _seed(self):
        payload = {
            "tools": [{"function": {"name": "demo_tool"}}],
            "total": 1,
            "servers": ["sysServer"],
            "failed_servers": [],
            "server_metadata": {},
            "discovery": {"max_concurrency": 1, "timeout_seconds": 12, "elapsed_ms": 42},
            "server_metrics": [],
        }
        registry._store_tools_cache(payload)
        registry.ALL_TOOLS = [{"type": "function", "function": {"name": "demo_tool"}}]
        registry.TOOL_MCP_SERVERS = {"demo_tool": "sysServer"}
        registry.TOOL_ORIGINAL_NAMES = {"demo_tool": "demo_tool"}
        registry.TOOL_NAME_INDEX = {"demo_tool": ["demo_tool"]}

    def test_snapshot_roundtrip_restores_cache_and_registry(self):
        self._seed()
        snap = registry.get_tools_cache_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["registry"]["servers"], {"demo_tool": "sysServer"})
        self.assertIn("age_seconds", snap)

        # 模拟 worker：先清空，再安装快照
        registry._TOOLS_CACHE = None
        registry.ALL_TOOLS = []
        registry.TOOL_MCP_SERVERS = {}
        self.assertTrue(registry.install_tools_cache_snapshot(snap))

        self.assertEqual(len(registry.ALL_TOOLS), 1)
        self.assertEqual(registry.TOOL_MCP_SERVERS, {"demo_tool": "sysServer"})
        cached = registry.get_cached_tools_payload(60)
        self.assertIsNotNone(cached, "安装后应当直接命中缓存")
        self.assertNotIn("registry", cached, "缓存对外不暴露 registry 冗余字段")
        self.assertEqual(cached["total"], 1)

    def test_installed_cache_prevents_rediscovery(self):
        """关键断言：load_all_tools 命中缓存后不应再触发 MCP 探测。"""
        self._seed()
        snap = registry.get_tools_cache_snapshot()
        registry._TOOLS_CACHE = None
        registry.install_tools_cache_snapshot(snap)

        calls = []

        async def fake_refresh(current_dir):
            calls.append(current_dir)
            return {}

        import asyncio
        from factory import chat_factory
        original = registry.refresh_tools_from_mcp
        registry.refresh_tools_from_mcp = fake_refresh
        # chat_factory 内部是 `tool_registry.refresh_tools_from_mcp`，
        # 打补丁在模块对象上即可被看到
        try:
            asyncio.run(chat_factory.load_all_tools())
        finally:
            registry.refresh_tools_from_mcp = original
        self.assertEqual(calls, [], "缓存新鲜时不得重探 MCP")

    def test_malformed_snapshot_is_ignored(self):
        self.assertFalse(registry.install_tools_cache_snapshot(None))
        self.assertFalse(registry.install_tools_cache_snapshot({"tools": []}))
        self.assertFalse(registry.install_tools_cache_snapshot({"registry": {"tools": "bad"}}))
        self.assertIsNone(registry.get_cached_tools_payload(60))

    def test_snapshot_none_when_cache_empty(self):
        self.assertIsNone(registry.get_tools_cache_snapshot())


if __name__ == "__main__":
    unittest.main()
