# coding: utf-8
"""内置终端命令工具（run_command）的手动验证脚本：直接调用迁移后的实现，
在临时沙箱中执行真实命令并校验返回。

用法（在项目根目录执行）：
    python test/manual_builtin_command_check.py

说明：
    - 覆盖：注入 / echo / 中文 / 退出码 / 多行内联代码改写（原迁移动机场景）/
      超时杀进程树 / work_dir / 超长输出截断并落盘 / PowerShell 多行+引号 /
      后台模式与输出落盘 / asyncio.to_thread 接线 / 分发器错误结构化；
    - run_command 已由 mcp_server/sys_tools_server.py 迁移为内置工具
      （factory/agent_runtime/builtin_tools.py），本脚本与单元测试
      test/test_builtin_command_tool.py 互补：单元测试覆盖轻量断言，
      本脚本做端到端真实命令验证（Windows 环境完整运行，非 Windows 自动跳过 cmd 用例）；
    - MCP 侧（fetch_url / web_search）的验证见 test/manual_sys_tools_check.py。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factory.agent_runtime import builtin_tools as bt  # noqa: E402

PY = sys.executable
IS_WINDOWS = os.name == "nt"
passed = failed = skipped = 0


def check(name, result, *must_contain):
    global passed, failed
    text = result if isinstance(result, str) else repr(result)
    missing = [kw for kw in must_contain if kw not in text]
    if missing:
        failed += 1
        print(f"❌ {name}: 缺少 {missing}；前 300 字符: {text[:300]!r}")
    else:
        passed += 1
        print(f"✅ {name}: {text[:140].replace(chr(10), ' | ')}")


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"⚠️  {name}: 跳过（{reason}）")


def main():
    global passed, failed
    sandbox = Path(tempfile.mkdtemp(prefix="builtin_rc_check_"))
    old_cwd = os.getcwd()
    os.chdir(sandbox)
    print(f"沙箱: {sandbox}\n")
    try:
        # ---- 注入与定义 ----
        tools, servers = bt.inject_builtin_tools([], {}, include_run_command=True)
        names = {t["function"]["name"] for t in tools}
        check("注入 run_command", str(names), "run_command")
        check("伪服务键", str(servers.get("run_command")), "__builtin__")

        if not IS_WINDOWS:
            skip("真实命令用例", "非 Windows 环境（cmd 相关用例仅 Windows 验证）")
        else:
            # ---- 基础执行 ----
            check("echo", bt.execute_run_command({"command": "echo builtin_hello"}),
                  "exit=0", "builtin_hello")
            check("中文输出", bt.execute_run_command(
                {"command": f'"{PY}" -c "print(\'中文内置ok\')"'}), "中文内置ok")
            check("非零退出码", bt.execute_run_command({"command": "cmd /c exit 5"}), "exit=5")

            # ---- 多行内联代码（原迁移动机：cmd 拆行坑） ----
            multi = 'python -c "\nimport sys\nprint(\'multi-line\', sys.version_info[0])\n"'
            result = bt.execute_run_command({"command": multi})
            check("多行内联代码改写", result, "multi-line", "已自动改写为临时脚本")

            # ---- 超时杀进程树 ----
            check("超时终止", bt.execute_run_command(
                {"command": "ping -n 30 127.0.0.1", "timeout_seconds": 2}),
                "命令超时，进程树已被强制终止")

            # ---- work_dir ----
            check("work_dir", bt.execute_run_command(
                {"command": "cd", "work_dir": str(sandbox)}), "exit=0", sandbox.name)

            # ---- 超长输出截断 + 落盘 ----
            result = bt.execute_run_command({"command": f'"{PY}" -c "print(\'y\'*30000)"'})
            check("超长截断+落盘", result, "过长已截断", "完整输出:", "可用 read_file 读取")
            full_path = None
            for line in result.splitlines():
                if "完整输出:" in line:
                    full_path = line.split("完整输出:", 1)[1].strip().split("（")[0].strip()
            check("完整输出文件存在",
                  str(Path(full_path).is_file() if full_path else False), "True")

            # ---- shell=powershell（多行+引号历史回归场景） ----
            check("powershell 多行+引号", bt.execute_run_command({
                "command": "$a = 1\n$b = 2\nWrite-Output \"sum=$($a+$b) | 'q' ok\"",
                "shell": "powershell"}), "sum=3 | 'q' ok")

            # ---- 后台模式 ----
            bg = bt.execute_run_command({"command": "echo bg_builtin_ok", "background": True})
            check("后台模式启动", bg, "后台模式", "输出文件:", "结束标记:")
            bg_out = None
            for line in bg.splitlines():
                if line.startswith("输出文件:"):
                    bg_out = line.split(":", 1)[1].strip().split("（")[0].strip()
            if bg_out:
                time.sleep(2.5)
                try:
                    content = Path(bg_out).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    content = f"<读取失败 {exc}>"
                check("后台输出落盘", content, "bg_builtin_ok")
            else:
                failed += 1
                print("❌ 后台模式: 未解析到输出文件路径")

        # ---- asyncio.to_thread 接线（与 chat_factory 执行路径一致） ----
        async def _run_async():
            return await asyncio.to_thread(
                bt.try_execute_builtin_command_tool, "run_command",
                {"command": "echo threaded_ok"})

        result = asyncio.run(_run_async())
        check("asyncio.to_thread 执行", result, "threaded_ok", "exit=0")

        # ---- 分发器 ----
        check("try_execute 分发（错误结构化）", str(bt.try_execute_builtin_command_tool(
            "run_command", {"command": ""})), "error")
        check("try_execute 非目标返回 None",
              str(bt.try_execute_builtin_command_tool("read_file", {})), "None")
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n========== 结果：通过 {passed}，失败 {failed}，跳过 {skipped} ==========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
