# coding: utf-8
"""枚举 PipeIpcMCP.exe 的工具清单与参数 schema（测试辅助脚本）"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from util.mcp_client import call_mcp_tool, get_mcp_tools  # noqa: E402


async def main():
    print("=" * 70)
    print("STEP 1: get_mcp_tools('pipeIpcMcp')  -- 走项目配置路径")
    print("=" * 70)
    tools = await get_mcp_tools("pipeIpcMcp")
    for t in tools or []:
        print("-" * 60)
        print("name:", t.name)
        print("description:", (t.description or "").strip())
        print("inputSchema:", json.dumps(t.parameters, ensure_ascii=False, indent=2))

    print()
    print("=" * 70)
    print("STEP 2: 直接传 exe 相对路径调用 get_pipe_status（未初始化状态探测）")
    print("=" * 70)
    try:
        res = await call_mcp_tool("get_pipe_status", {}, mcp_service="mcp_server/PipeIpcMCP.exe")
        print("RESULT:", res)
    except Exception as e:
        print("ERROR:", e)


if __name__ == "__main__":
    asyncio.run(main())
