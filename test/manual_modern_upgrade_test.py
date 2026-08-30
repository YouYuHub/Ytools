# coding: utf-8
"""第四轮：验证 2026-07-28 modern 版本完整升级路径
  initialize(2025-11-25 握手) -> discover(supported_versions) -> adopt/升级 -> 2026-07-28 会话全流程
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE = r"C:\Users\Administrator\MyMcp\PipeIpcMCP.exe"


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=EXE, args=[])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"[1] 握手版本: {init.protocol_version!r}")

            disc = await s.discover()
            print(f"[2] discover.supported_versions: {disc.supported_versions}")

            ver_after = s.protocol_version
            print(f"[3] discover 后 session.protocol_version: {ver_after!r}")

            if ver_after == "2026-07-28":
                print("[PASS] 会话已升级到 modern 2026-07-28")
            else:
                print(f"[WARN] 会话版本: {ver_after!r}")

            tools = await s.list_tools()
            print(f"[4] 2026-07-28 会话 list_tools: {[t.name for t in tools.tools]}")

            res = await s.call_tool("get_pipe_status", {})
            is_err = getattr(res, "is_error", None)
            if is_err is None:
                is_err = getattr(res, "isError", None)
            print(f"[5] 2026-07-28 会话 call_tool(get_pipe_status): "
                  f"is_error={is_err}, text={res.content[0].text[:60]!r}")

            res2 = await s.call_tool("setup_pipe", {
                "pipe_name": r"\\.\pipe\proto_test_server",
                "terminal_mode": "cmd.exe /k chcp 65001",
                "first_command": "echo protocol_modern_ok",
                "wait_milliseconds": 3000,
            })
            is_err2 = getattr(res2, "is_error", None)
            print(f"[6] 2026-07-28 会话 call_tool(setup_pipe): is_error={is_err2}")
            for item in res2.content:
                if hasattr(item, "text"):
                    print(f"    输出片段: {item.text[:120]!r}")
    print("[7] 会话正常关闭")


if __name__ == "__main__":
    asyncio.run(main())
