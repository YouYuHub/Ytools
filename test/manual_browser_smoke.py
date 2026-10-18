"""手动浏览器冒烟测试：真实启动 Chromium，导航、点击、截图。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import threading

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from factory.agent_runtime.tool_executor import _invoke_tool_function
from util.mcp_client import close_persistent_mcp_clients


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<!doctype html><html><title>Browser Smoke</title>"
                b"<button onclick=\"console.error('debug-click');document.getElementById('count').textContent='Count: 1'\">"
                b"Increment</button><p id='count'>Count: 0</p></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def smoke(port: int) -> None:
    # 完整走工具执行器；它会为每次调用新建事件循环。
    def call(name, arguments):
        return _invoke_tool_function(name, arguments, {name: "Browser"})

    url = f"http://127.0.0.1:{port}/"
    call("browser_navigate", {"url": url})
    snapshot = call("browser_snapshot", {})
    match = re.search(r'button "Increment" \[ref=([^\]]+)\]', snapshot)
    if not match:
        raise AssertionError(f"未找到可点击的按钮：\n{snapshot}")
    call("browser_click", {"target": match.group(1)})
    snapshot = call("browser_snapshot", {})
    if "Count: 1" not in snapshot:
        raise AssertionError(f"点击后页面未更新：\n{snapshot}")
    console = call("browser_console_messages", {"level": "error"})
    if "debug-click" not in console:
        raise AssertionError(f"未读取到点击产生的控制台错误：\n{console}")
    network = call("browser_network_requests", {"static": True})
    if "127.0.0.1" not in network:
        raise AssertionError(f"未读取到本地页面请求：\n{network}")
    screenshot = call("browser_take_screenshot", {"type": "png", "scale": "css"})
    match = re.search(r"\[MCP 图片已保存\] (.+?)（", screenshot)
    if not match or not Path(match.group(1)).is_file():
        raise AssertionError(f"截图未保存：\n{screenshot}")
    child_scope = "sub_agent:smoke"
    _invoke_tool_function(
        "browser_navigate", {"url": url}, {"browser_navigate": "Browser"},
        session_scope=child_scope,
    )
    child_snapshot = _invoke_tool_function(
        "browser_snapshot", {}, {"browser_snapshot": "Browser"},
        session_scope=child_scope,
    )
    parent_snapshot = call("browser_snapshot", {})
    if "Count: 0" not in child_snapshot or "Count: 1" not in parent_snapshot:
        raise AssertionError(
            f"父子智能体浏览器页面未隔离\nchild={child_snapshot}\nparent={parent_snapshot}"
        )
    close_persistent_mcp_clients(child_scope)
    print(f"PASS: navigate -> snapshot -> click -> console/network -> screenshot -> isolated child: {match.group(1)}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        smoke(server.server_port)
    finally:
        close_persistent_mcp_clients()
        server.shutdown()
        server.server_close()
