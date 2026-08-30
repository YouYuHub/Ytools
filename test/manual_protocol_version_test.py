# coding: utf-8
"""PipeIpcMCP.exe 协议版本兼容性测试（原始 JSON-RPC over stdio，绕开 SDK）

测试项：
  1. 对每个已知协议版本请求 initialize，观察服务端返回的 protocolVersion（是否回显/降级）
  2. 伪造/过新/缺失 protocolVersion 的异常协商
  3. 在 2026-07-28 下完成完整会话：initialized -> tools/list -> tools/call
  4. 检查响应是否携带 _meta["io.modelcontextprotocol/protocolVersion"]
"""
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading

EXE_MY = r"C:\Users\Administrator\MyMcp\PipeIpcMCP.exe"
EXE_PROJ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "mcp_server", "PipeIpcMCP.exe")

KNOWN_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
META_KEY = "io.modelcontextprotocol/protocolVersion"


class RawMcpClient:
    """极简 MCP stdio 客户端：换行分隔 JSON-RPC"""

    def __init__(self, exe):
        self.exe = exe
        self.proc = subprocess.Popen(
            [exe],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.notifications = []

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self.q.put(line)
        except Exception:
            pass
        self.q.put(None)  # EOF 标记

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, timeout):
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return {"__timeout__": True}
        if item is None:
            return {"__eof__": True}
        try:
            return json.loads(item)
        except json.JSONDecodeError:
            return {"__raw__": item}

    def request(self, req_id, method, params=None, timeout=8):
        """发送请求并等待同 id 响应，期间收到的通知存入 self.notifications"""
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        waited = 0.0
        step = 0.2
        while waited < timeout:
            resp = self._recv(min(step, timeout - waited))
            waited += step
            if "__timeout__" in resp:
                continue
            if "__eof__" in resp:
                return {"__eof__": True}
            if "__raw__" in resp:
                return resp
            if resp.get("id") == req_id:
                return resp
            self.notifications.append(resp)
        return {"__timeout__": True}

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def init_params(version):
    p = {
        "capabilities": {},
        "clientInfo": {"name": "protocol-version-tester", "version": "1.0.0"},
    }
    if version is not None:
        p["protocolVersion"] = version
    return p


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def case_negotiate(name, requested, expect=None):
    """单个版本协商用例：返回 (描述行, 是否符合预期)"""
    c = RawMcpClient(EXE_UNDER_TEST)
    try:
        resp = c.request(1, "initialize", init_params(requested), timeout=8)
    finally:
        c.close()
    if resp is None or "__eof__" in resp:
        return f"[FAIL] {name}: 服务端无响应（进程退出/EOF）", False
    if "__timeout__" in resp:
        return f"[FAIL] {name}: 响应超时(8s)", False
    if "error" in resp:
        err = resp["error"]
        return (f"[{'PASS' if expect == 'error' else 'WARN'}] {name}: 返回错误 "
                f"code={err.get('code')} msg={err.get('message')!r}", expect == "error")
    result = resp.get("result", {})
    got = result.get("protocolVersion")
    server_info = result.get("serverInfo", {})
    ok = (got == requested) if expect == "echo" else (expect is None or got == expect)
    tag = "PASS" if ok else "WARN"
    return (f"[{tag}] {name}: 请求={requested!r} -> 响应={got!r} "
            f"serverInfo={server_info.get('name')}@{server_info.get('version')} "
            f"capabilities={sorted(result.get('capabilities', {}).keys())}", ok)


def full_session_test(version):
    """在指定协议版本下跑完整会话：initialize -> initialized -> tools/list -> tools/call"""
    c = RawMcpClient(EXE_UNDER_TEST)
    steps = []
    try:
        resp = c.request(1, "initialize", init_params(version), timeout=8)
        if "result" not in resp:
            steps.append(f"[FAIL] initialize 失败: {resp}")
            return steps
        got = resp["result"].get("protocolVersion")
        steps.append(f"[INFO] initialize 协商结果: {got!r}")
        has_meta = META_KEY in (resp["result"].get("_meta") or {})
        steps.append(f"[INFO] initialize 响应 _meta 含协议版本键: {has_meta}")

        c.notify("notifications/initialized")
        resp = c.request(2, "tools/list", {}, timeout=8)
        if "result" not in resp:
            steps.append(f"[FAIL] tools/list 失败: {resp}")
            return steps
        tools = resp["result"].get("tools", [])
        steps.append(f"[PASS] tools/list: {len(tools)} 个工具 -> {[t['name'] for t in tools]}")

        resp = c.request(3, "tools/call",
                         {"name": "get_pipe_status", "arguments": {}}, timeout=15)
        if "result" not in resp:
            steps.append(f"[FAIL] tools/call 失败: {resp}")
            return steps
        result = resp["result"]
        is_err = result.get("isError", False)
        texts = [item.get("text", "") for item in result.get("content", [])
                 if isinstance(item, dict)]
        meta = result.get("_meta") or {}
        steps.append(f"[{'PASS' if not is_err else 'FAIL'}] tools/call(get_pipe_status): "
                     f"isError={is_err}, content片段={texts[:1]!r}"[:300])
        steps.append(f"[INFO] tools/call 响应 _meta 键: {sorted(meta.keys())} "
                     f"协议版本={meta.get(META_KEY)!r}")
        meta_ok = meta.get(META_KEY) == version
        steps.append(f"[{'PASS' if meta_ok else 'WARN'}] 响应 _meta 协议版本一致性: "
                     f"期望 {version!r}, 实际 {meta.get(META_KEY)!r}")
    finally:
        c.close()
    return steps


def main():
    global EXE_UNDER_TEST
    h1, h2 = md5(EXE_MY), md5(EXE_PROJ)
    same = h1 == h2
    print(f"exe 指纹: MyMcp={h1[:12]}, project={h2[:12]}, 完全相同={same}")
    EXE_UNDER_TEST = EXE_MY

    print("\n===== 1. 已知协议版本逐个协商 =====")
    for v in KNOWN_VERSIONS:
        expect = "echo"
        line, _ = case_negotiate(f"标准版本 {v}", v, expect=expect)
        print(line)

    print("\n===== 2. 异常版本协商 =====")
    for v, label in [("1999-01-01", "过旧伪造版本"), ("2099-12-31", "过新伪造版本")]:
        line, _ = case_negotiate(label, v, expect=None)
        print(line)
    line, _ = case_negotiate("缺失 protocolVersion 字段", None, expect=None)
    print(line)

    print("\n===== 3. 2026-07-28 完整会话 =====")
    for line in full_session_test("2026-07-28"):
        print(line)


if __name__ == "__main__":
    main()
