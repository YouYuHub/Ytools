"""浏览器 MCP 常驻连接与图片结果的关键回归测试。"""
import asyncio
import base64
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import util.mcp_client as mcp_client


class PersistentBrowserClientTests(unittest.TestCase):
    def test_calls_reuse_one_mcp_process_across_event_loops(self):
        launches = []
        sessions = []

        @contextlib.asynccontextmanager
        async def fake_stdio(_params):
            launches.append(1)
            yield object(), object()

        class FakeSession:
            def __init__(self, *_streams):
                self.calls = []
                sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="browser_snapshot")])

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return SimpleNamespace(is_error=False, content=[SimpleNamespace(text=f"page {len(self.calls)}")])

        with patch.object(mcp_client, "build_stdio_server_parameters", return_value=object()), \
                patch.object(mcp_client, "stdio_client", fake_stdio), \
                patch.object(mcp_client, "ClientSession", FakeSession):
            client = mcp_client._PersistentStdioClient("Browser", "test-signature")
            try:
                self.assertEqual(asyncio.run(client.call("browser_snapshot", {})), "page 1")
                self.assertEqual(asyncio.run(client.call("browser_snapshot", {})), "page 2")
                self.assertEqual(len(launches), 1)
                self.assertEqual(len(sessions), 1)
                self.assertEqual(len(sessions[0].calls), 2)
            finally:
                client.close()

    def test_browser_server_marked_persistent(self):
        signature = mcp_client._persistent_server_signature("Browser")
        self.assertIsNotNone(signature)
        self.assertTrue(json.loads(signature)["persistent"])

    def test_browser_debug_outputs_go_to_system_temp(self):
        params = mcp_client.build_stdio_server_parameters("Browser")
        self.assertEqual(
            Path(params.env["PLAYWRIGHT_MCP_OUTPUT_DIR"]),
            Path(tempfile.gettempdir()) / "ytools_playwright_artifacts",
        )

    def test_timeout_discards_stalled_browser_connection(self):
        class StalledClient:
            async def call(self, *_args):
                await asyncio.sleep(60)

        stalled = StalledClient()
        with patch.object(mcp_client, "_persistent_server_signature", return_value="sig"), \
                patch.object(mcp_client, "_get_persistent_client", return_value=stalled), \
                patch.object(mcp_client, "_drop_persistent_client") as drop, \
                patch.object(mcp_client, "_mcp_call_timeout_seconds", return_value=0.01):
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(mcp_client.call_mcp_tool("browser_snapshot", {}, "Browser"))
        drop.assert_called_once_with("Browser", "main", stalled)


class ImageResultTests(unittest.TestCase):
    def test_mcp_image_is_saved_for_read_media(self):
        raw = b"\x89PNG\r\n\x1a\nimage-test"
        result = SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(text="Screenshot captured"),
                SimpleNamespace(mimeType="image/png", data=base64.b64encode(raw).decode("ascii")),
            ],
        )
        text = mcp_client._parse_mcp_tool_result(result)
        self.assertIn("Screenshot captured", text)
        path = Path(text.split("[MCP 图片已保存] ", 1)[1].split("（", 1)[0])
        try:
            self.assertEqual(path.read_bytes(), raw)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
