# -*- coding: utf-8 -*-
"""restart_tools_server 的 stdio 协议冒烟：initialize + tools/list（不杀伤进程）。

设计为独立脚本（python test/restart_stdio_smoke.py）而非 pytest 用例：
全部逻辑收拢在 main() 内，被 pytest 收集导入时零副作用。
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER = PROJECT_ROOT / "mcp_server" / "restart_tools_server.py"


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    def _send(payload: dict) -> None:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def _recv() -> dict:
        line = process.stdout.readline()
        return json.loads(line)

    _send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0.1"},
        },
    })
    init = _recv()
    _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listing = _recv()

    tools = [item.get("name") for item in listing.get("result", {}).get("tools", [])]
    server_name = init.get("result", {}).get("serverInfo", {}).get("name")
    print(f"server_name={server_name}")
    print(f"tools={tools}")

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

    ok = (
        server_name == "restart-mcp-server"
        and set(tools) == {"restart_service", "restart_cancel", "restart_status"}
    )
    print("SMOKE_OK" if ok else "SMOKE_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
