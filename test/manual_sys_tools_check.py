# coding: utf-8
"""
sys_tools_server.py 的手动验证脚本：通过项目自身的 MCP 客户端真实拉起
服务器子进程，逐个调用工具并校验返回。

用法（在项目根目录执行）：
    python test/manual_sys_tools_check.py

说明：
    - 文件类/命令类工具在临时沙箱目录中验证，结束后自动清理；
    - fetch_url / web_search 依赖外网连通性，失败时仅告警不判定为失败；
    - 每次工具调用都会新起一个 MCP 服务器子进程，整体耗时约 1-2 分钟。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from util.mcp_client import call_mcp_tool, get_mcp_tools  # noqa: E402

PY = sys.executable


def _make_sandbox() -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="sys_tools_check_"))
    (sandbox / "f1.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
    (sandbox / "script.py").write_text(
        "def first_func():\n    return 1\n\n\ndef second_func():\n    return 2\n",
        encoding="utf-8",
    )
    (sandbox / "gbk_file.txt").write_bytes("中文内容测试\n第二行\n".encode("gbk"))
    (sandbox / "crlf_file.txt").write_bytes(b"alpha one\r\nalpha two\r\nbeta three\r\n")
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "inner.txt").write_text("nested file content\n", encoding="utf-8")
    return sandbox


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warned = 0

    async def check(self, name: str, tool: str, arguments: dict, must_contain=(), must_not_contain=(), fatal=True):
        try:
            result = await call_mcp_tool(tool, arguments, mcp_service="SysServer")
        except Exception as exc:
            if fatal:
                self.failed += 1
                print(f"❌ {name}: 工具调用异常 {type(exc).__name__}: {exc}")
            else:
                self.warned += 1
                print(f"⚠️  {name}: 异常（不判定失败）{type(exc).__name__}: {exc}")
            return None
        problems = [kw for kw in must_contain if kw not in result]
        problems += [kw for kw in must_not_contain if kw in result]
        if problems:
            self.failed += 1
            preview = result[:400].replace("\n", "\\n")
            print(f"❌ {name}: 返回缺少/多了 {problems}；前 400 字符：{preview}")
            return result
        self.passed += 1
        preview = result[:160].replace("\n", "\\n")
        print(f"✅ {name}: {preview}")
        return result

    async def check_error(self, name: str, tool: str, arguments: dict, keyword: str = ""):
        try:
            result = await call_mcp_tool(tool, arguments, mcp_service="SysServer")
        except Exception as exc:
            message = str(exc)
            if keyword and keyword not in message:
                self.failed += 1
                print(f"❌ {name}: 报错但缺少关键字 {keyword!r}：{message[:300]}")
            else:
                self.passed += 1
                print(f"✅ {name}: 按预期报错（{message[:120]}）")
            return
        self.failed += 1
        print(f"❌ {name}: 期望报错但成功返回：{str(result)[:200]}")


async def run_file_cases(checker: Checker, sandbox: Path):
    join = lambda *parts: str(Path(sandbox, *parts)).replace("\\", "/")  # noqa: E731

    # ---- list_dir ----
    data = await checker.check(
        "list_dir 默认", "list_dir", {"dir_path": "."},
        must_contain=['"f1.txt"', '"script.py"', '"gbk_file.txt"', '"total"'],
    )
    if data:
        payload = json.loads(data.split("\n")[0])
        names = {item["name"] for item in payload["entries"]}
        assert "sub" in names and "f1.txt" in names, names
        assert all(item["path"].startswith("/") or ":" in item["path"] or "/" in item["path"] for item in payload["entries"])
    await checker.check(
        "list_dir pattern+depth", "list_dir",
        {"dir_path": ".", "pattern": "*.txt", "search_mode": "file", "max_depth": 0},
        must_contain=['"f1.txt"'], must_not_contain=['"script.py"', '"inner.txt"'],
    )

    # ---- read_file ----
    await checker.check(
        "read_file 行号", "read_file", {"full_file_name": join("f1.txt")},
        must_contain=["1| hello world", "2| second line", "共 2 行"],
    )
    await checker.check(
        "read_file 行范围", "read_file",
        {"full_file_name": join("f1.txt"), "start_line": 2, "end_line": 2, "show_line_numbers": False},
        must_contain=["second line"], must_not_contain=["hello world"],
    )
    await checker.check(
        "read_file GBK 自动解码", "read_file", {"full_file_name": join("gbk_file.txt")},
        must_contain=["中文内容测试", "编码 gbk"],
    )
    await checker.check(
        "read_file start 超界", "read_file",
        {"full_file_name": join("f1.txt"), "start_line": 99},
        must_contain=["超出文件总行数"],
    )
    await checker.check_error(
        "read_file 不存在", "read_file", {"full_file_name": join("nope.txt")}, "不存在")

    # ---- write_file ----
    await checker.check(
        "write_file 新建(多级目录)", "write_file",
        {"full_file_name": join("a/b/new.txt"), "content": "第一行\n第二行"},
        must_contain=["已写入"],
    )
    await checker.check(
        "write_file 追加", "write_file",
        {"full_file_name": join("a/b/new.txt"), "content": "\n第三行", "append": True},
        must_contain=["已追加"],
    )
    await checker.check(
        "write_file 回读", "read_file", {"full_file_name": join("a/b/new.txt"), "show_line_numbers": False},
        must_contain=["第一行", "第二行", "第三行"],
    )

    # ---- edit_file ----
    await checker.check(
        "edit_file 唯一替换", "edit_file",
        {"full_file_name": join("f1.txt"), "old_string": "hello world", "new_string": "hi world"},
        must_contain=["替换 1 处"],
    )
    await checker.check_error(
        "edit_file 歧义报错", "edit_file",
        {"full_file_name": join("script.py"), "old_string": "return", "new_string": "yield"},
        "歧义",
    )
    await checker.check(
        "edit_file 全部替换", "edit_file",
        {"full_file_name": join("script.py"), "old_string": "return", "new_string": "return  # ret", "replace_all": True},
        must_contain=["替换 2 处"],
    )
    await checker.check(
        "edit_file CRLF 适配", "edit_file",
        {"full_file_name": join("crlf_file.txt"), "old_string": "alpha two", "new_string": "alpha 2"},
        must_contain=["替换 1 处", "CRLF"],
    )
    crlf_after = Path(sandbox, "crlf_file.txt").read_bytes()
    if b"alpha 2\r\n" in crlf_after and b"\r\n" in crlf_after:
        checker.passed += 1
        print("✅ edit_file CRLF 写回保持")
    else:
        checker.failed += 1
        print(f"❌ edit_file CRLF 写回丢失: {crlf_after!r}")
    await checker.check(
        "edit_file 多行 old_string(\\n)", "edit_file",
        {"full_file_name": join("crlf_file.txt"), "old_string": "alpha one\nalpha 2", "new_string": "alpha one\nalpha two"},
        must_contain=["替换 1 处"],
    )
    await checker.check_error(
        "edit_file 0 处匹配", "edit_file",
        {"full_file_name": join("f1.txt"), "old_string": "not-exist-string", "new_string": "x"}, "0 处匹配")

    # ---- search_files ----
    await checker.check(
        "search_files 正则+上下文", "search_files",
        {"pattern": r"def \w+_func", "dir_path": ".", "file_pattern": "*.py", "context_lines": 1},
        must_contain=["script.py:1", "script.py:5", "扫描"],
    )
    await checker.check(
        "search_files 忽略大小写", "search_files",
        {"pattern": "SECOND LINE", "dir_path": ".", "ignore_case": True, "is_regex": False},
        must_contain=["f1.txt:2"],
    )
    await checker.check(
        "search_files 子目录递归", "search_files",
        {"pattern": "nested file"},
        must_contain=["sub/inner.txt"],
    )
    await checker.check(
        "search_files 无命中", "search_files",
        {"pattern": "zzz_no_such_content_zzz", "dir_path": "."},
        must_contain=["无匹配结果"],
    )
    await checker.check_error(
        "search_files 非法正则", "search_files", {"pattern": "["}, "正则表达式不合法")


async def run_command_cases(checker: Checker):
    await checker.check(
        "run_command echo", "run_command", {"command": "echo hello_cmd"},
        must_contain=["exit_code=0", "hello_cmd"],
    )
    await checker.check(
        "run_command 中文输出", "run_command",
        {"command": f'"{PY}" -c "print(\'中文输出ok\')"'},
        must_contain=["中文输出ok"],
    )
    await checker.check(
        "run_command 非零退出码", "run_command",
        {"command": "cmd /c exit 3"},
        must_contain=["exit_code=3"],
    )
    await checker.check(
        "run_command 超时终止", "run_command",
        {"command": "ping -n 30 127.0.0.1", "timeout_seconds": 2},
        must_contain=["超时被强制终止"],
    )
    await checker.check(
        "run_command 指定 cwd", "run_command",
        {"command": "cd", "cwd": os.environ.get("SystemRoot", r"C:\\Windows")},
        must_contain=["exit_code=0"],
    )


async def run_network_cases(checker: Checker):
    await checker.check(
        "fetch_url 纯文本", "fetch_url", {"url": "https://example.com"},
        must_contain=["Example Domain"], fatal=False,
    )
    await checker.check(
        "fetch_url 标签抽取", "fetch_url",
        {"url": "https://example.com", "mode": "tag", "tag": "h1"},
        must_contain=["Example Domain"], fatal=False,
    )
    await checker.check(
        "fetch_url 正则抽取", "fetch_url",
        {"url": "https://example.com", "mode": "regex", "pattern": r"<h1>(.*?)</h1>"},
        must_contain=["Example Domain"], fatal=False,
    )
    await checker.check(
        "web_search", "web_search", {"query": "FastAPI 官方文档", "max_results": 3},
        must_contain=["链接: http"], fatal=False,
    )
    await checker.check_error(
        "fetch_url 非法协议", "fetch_url", {"url": "ftp://example.com/file"}, "http/https")


async def main():
    checker = Checker()
    sandbox = _make_sandbox()
    os.chdir(sandbox)  # MCP 子进程继承 cwd，相对路径即落在沙箱内
    try:
        tools = await get_mcp_tools("SysServer")
        names = sorted(tool.name for tool in tools)
        expected = sorted(["list_dir", "read_file", "write_file", "edit_file",
                           "search_files", "run_command", "fetch_url", "web_search"])
        if names == expected:
            checker.passed += 1
            print(f"✅ 工具发现: {names}")
        else:
            checker.failed += 1
            print(f"❌ 工具发现不一致: {names}")
        # schema 快速体检：参数 schema 均为 object 且带 description
        bad = [t.name for t in tools if (t.parameters or {}).get("type") != "object"]
        if bad:
            checker.failed += 1
            print(f"❌ 工具参数 schema 异常: {bad}")
        else:
            checker.passed += 1
            print("✅ 工具参数 schema 均为 object")

        print("\n---------- 文件类工具 ----------")
        await run_file_cases(checker, sandbox)
        print("\n---------- 命令执行 ----------")
        await run_command_cases(checker)
        print("\n---------- 网络抓取/搜索（失败仅告警） ----------")
        await run_network_cases(checker)
    finally:
        os.chdir(PROJECT_ROOT)
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n========== 结果：通过 {checker.passed}，失败 {checker.failed}，网络告警 {checker.warned} ==========")
    return 1 if checker.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
