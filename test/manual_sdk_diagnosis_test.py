# coding: utf-8
"""第三轮：mcp 2.0.0 SDK 精确诊断
  1. 初始化协商版本
  2. call_tool 具体异常定位
  3. send_ping 行为（exe 未实现 ping）
  4. discover（modern 版本升级路径）是否可用
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE = r"C:\Users\Administrator\MyMcp\PipeIpcMCP.exe"


def print_exc_group(e, indent="  "):
    if hasattr(e, "exceptions"):
        for i, sub in enumerate(e.exceptions):
            print(f"{indent}[子异常{i}] {type(sub).__name__}: {sub}")
            print_exc_group(sub, indent + "  ")
    else:
        print(f"{indent}{type(e).__name__}: {e}")


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=EXE, args=[])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"[1] 协商版本: {init.protocol_version!r}  server={init.server_info.name}")

            # 2) call_tool 异常定位
            try:
                res = await s.call_tool("get_pipe_status", {})
                print(f"[2] call_tool OK: isError={res.isError}, "
                      f"text={res.content[0].text[:60]!r}")
            except BaseException as e:
                print("[2] call_tool 失败:")
                print_exc_group(e)

            # 3) send_ping
            try:
                pong = await s.send_ping()
                print(f"[3] send_ping OK: {pong}")
            except BaseException as e:
                print("[3] send_ping 失败:")
                print_exc_group(e)

            # 4) discover（modern 升级路径）
            try:
                disc = await s.discover()
                print(f"[4] discover OK: {disc}")
            except BaseException as e:
                print("[4] discover 失败:")
                print_exc_group(e)
    print("[5] 会话正常关闭")


if __name__ == "__main__":
    asyncio.run(main())
