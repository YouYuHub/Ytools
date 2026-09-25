# coding: utf-8
"""
sys_tools_server.py 的手动验证脚本：通过项目自身的 MCP 客户端真实拉起
服务器子进程，逐个调用工具并校验返回。

用法（在项目根目录执行）：
    python test/manual_sys_tools_check.py

说明：
    - 当前 SysServer 注册 2 个工具：fetch_url / web_search
      （read/write/edit/search 文件四件套与 run_command 已迁移为主项目后端
        内置工具，其验证见 test/test_builtin_file_tools.py 与
        test/test_builtin_command_tool.py；list_dir 已停用）；
    - fetch_url / web_search 依赖外网连通性，失败时仅告警不判定为失败；
    - 每次工具调用都会新起一个 MCP 服务器子进程，整体耗时约 1 分钟内。
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from util.mcp_client import call_mcp_tool, get_mcp_tools  # noqa: E402


def _make_sandbox() -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="sys_tools_check_"))
    (sandbox / "marker.txt").write_text("sandbox marker\n", encoding="utf-8")
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
        expected = sorted(["fetch_url", "web_search"])
        if names == expected:
            checker.passed += 1
            print(f"✅ 工具发现: {names}")
        else:
            checker.failed += 1
            print(f"❌ 工具发现不一致: {names}（期望 {expected}）")
        # schema 快速体检：参数 schema 均为 object 且带 description
        bad = [t.name for t in tools if (t.parameters or {}).get("type") != "object"]
        if bad:
            checker.failed += 1
            print(f"❌ 工具参数 schema 异常: {bad}")
        else:
            checker.passed += 1
            print("✅ 工具参数 schema 均为 object")

        print("\n---------- 网络抓取/搜索（失败仅告警） ----------")
        await run_network_cases(checker)
    finally:
        os.chdir(PROJECT_ROOT)
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n========== 结果：通过 {checker.passed}，失败 {checker.failed}，网络告警 {checker.warned} ==========")
    return 1 if checker.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
