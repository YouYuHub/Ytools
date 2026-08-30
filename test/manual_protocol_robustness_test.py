# coding: utf-8
"""第二轮：mcp 2.0.0 SDK 端到端协商 + 协议健壮性测试

  A. 用真实 mcp 2.0.0 ClientSession 连接 exe（客户端默认请求 2026-07-28），
     验证协商结果与完整会话（list_tools / call_tool）。
  B. 原始 JSON-RPC 健壮性：ping、未声明能力的方法、非法游标、未知工具、
     缺必填参数、坏 JSON 行、非法 JSON-RPC、批量请求（2025-06-18 起已移除）、
     未初始化就调用工具，以及坏输入后连接是否存活。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manual_protocol_version_test import RawMcpClient, EXE_MY  # noqa: E402


async def sdk_e2e():
    print("===== A. mcp 2.0.0 SDK 端到端 =====")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import mcp.types

    print(f"[INFO] SDK LATEST_PROTOCOL_VERSION = {mcp.types.LATEST_PROTOCOL_VERSION}")
    params = StdioServerParameters(command=EXE_MY, args=[])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            negotiated = getattr(init, "protocol_version", None) or getattr(
                init, "protocolVersion", None)
            print(f"[{'PASS' if negotiated == mcp.types.LATEST_PROTOCOL_VERSION else 'WARN'}] "
                  f"SDK 协商结果: {negotiated!r} (serverInfo={init.server_info.name})")
            tools = await s.list_tools()
            print(f"[PASS] SDK list_tools: {[t.name for t in tools.tools]}")
            res = await s.call_tool("get_pipe_status", {})
            print(f"[{'PASS' if not res.isError else 'FAIL'}] SDK call_tool(get_pipe_status): "
                  f"isError={res.isError}, content={res.content[0].text[:80]!r}")


def robustness():
    print("\n===== B. 原始 JSON-RPC 健壮性 =====")
    c = RawMcpClient(EXE_MY)

    def show(label, resp, expect_ok=True):
        if resp is None:
            print(f"[FAIL] {label}: 无响应")
        elif "__timeout__" in resp:
            print(f"[{'WARN' if expect_ok else 'PASS'}] {label}: 超时（无响应）")
        elif "__eof__" in resp:
            print(f"[FAIL] {label}: 进程退出 EOF")
        elif "__raw__" in resp:
            print(f"[WARN] {label}: 非JSON响应 {resp['__raw__'][:80]!r}")
        elif "error" in resp:
            print(f"[{'PASS' if not expect_ok else 'WARN'}] {label}: 错误 "
                  f"code={resp['error'].get('code')} msg={resp['error'].get('message', '')[:90]!r}")
        else:
            r = resp.get("result")
            brief = json.dumps(r, ensure_ascii=False)[:90] if r is not None else "null"
            import json as _json
            print(f"[{'PASS' if expect_ok else 'WARN'}] {label}: result={brief}")

    import json
    # 1) 正常初始化
    show("initialize", c.request(1, "initialize", {
        "protocolVersion": "2026-07-28", "capabilities": {},
        "clientInfo": {"name": "robust", "version": "0"}}, timeout=8))
    c.notify("notifications/initialized")

    # 2) ping
    show("ping", c.request(2, "ping", {}))

    # 3) 未声明的方法（server 只声明了 tools 能力）
    show("resources/list（未声明能力）", c.request(3, "resources/list", {}), expect_ok=False)
    show("prompts/list（未声明能力）", c.request(4, "prompts/list", {}), expect_ok=False)

    # 4) 非法分页游标
    show("tools/list cursor='bogus'", c.request(5, "tools/list", {"cursor": "bogus"}))

    # 5) 未知工具
    show("tools/call 未知工具", c.request(6, "tools/call",
         {"name": "no_such_tool", "arguments": {}}), expect_ok=False)

    # 6) 缺必填参数
    show("tools/call run_pipe_command 缺 command", c.request(7, "tools/call",
         {"name": "run_pipe_command", "arguments": {}}), expect_ok=False)

    # 7) 未初始化前（新连接）直接 tools/list
    c2 = RawMcpClient(EXE_MY)
    show("未 initialize 直接 tools/list", c2.request(1, "tools/list", {}), expect_ok=False)
    c2.close()

    # 8) 坏 JSON 行 + 非法 JSON-RPC，然后连接是否存活
    c.proc.stdin.write("this is not json\n")
    c.proc.stdin.flush()
    show("坏JSON后仍响应 ping", c.request(8, "ping", {}))
    c.send({"jsonrpc": "2.0"})
    show("缺method的JSON-RPC后仍响应 ping", c.request(9, "ping", {}))
    c.send({"jsonrpc": "1.0", "id": 10, "method": "ping"})
    show("jsonrpc版本=1.0", c.request(10, "ping", {}))

    # 9) 批量请求（数组）——2025-06-18 起协议已移除批量
    c.proc.stdin.write(json.dumps([
        {"jsonrpc": "2.0", "id": 11, "method": "ping"},
        {"jsonrpc": "2.0", "id": 12, "method": "ping"},
    ]) + "\n")
    c.proc.stdin.flush()
    got_batch = []
    for _ in range(2):
        got_batch.append(c._recv(3))
    print(f"[INFO] 批量数组请求响应: {json.dumps(got_batch, ensure_ascii=False)[:200]}")

    # 10) 同连接二次 initialize
    show("二次 initialize", c.request(13, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "again", "version": "0"}}, timeout=8))

    # 11) 收尾存活确认
    show("收尾 ping", c.request(14, "ping", {}))
    c.close()
    print(f"[INFO] 期间收到通知: {[n.get('method') for n in c.notifications]}")


if __name__ == "__main__":
    try:
        asyncio.run(sdk_e2e())
    except Exception as e:
        import traceback
        print(f"[FAIL] SDK 端到端异常: {e}")
        traceback.print_exc()
    robustness()
