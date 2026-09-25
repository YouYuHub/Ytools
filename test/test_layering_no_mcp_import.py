# coding: utf-8
"""分层守卫测试：主程序不得依赖 mcp_server 层的实现。

架构约定：
- mcp_server/ 下的文件是 MCP 协议层工具（由 MCP 客户端按 stdio 协议以子进程
  方式拉起，通过协议交互）；主程序运行时代码不得 import 其实现（含
  sys_tools_server.py / restart_tools_server.py 的辅助函数）；
- 内置工具（factory/agent_runtime/builtin_tools.py）必须自包含：历史上
  read/write/edit/search 文件四件套与 run_command 已先后从
  sys_tools_server.py 迁移为内置工具，迁移方式均为「复制实现」；
- 如需在主程序与 MCP 层间共享工具函数，公共实现应放在主程序侧
  （util/ 或 factory/agent_runtime/），由 MCP 层按需自行引用/复制，
  而不是主程序反向依赖 MCP 层文件。

本测试做两类静态校验：
1. 扫描主程序运行时代码，禁止出现对 mcp_server 实现层的导入/动态加载
   （import sys_tools_server / from mcp_server... / importlib 动态加载 /
   sys.path 注入 mcp_server 目录等模式）；
2. 对 builtin_tools.py 做 AST 未定义名检查（自包含证明）：任何被引用但
   从未在本文件定义/导入的全局名字都会导致失败——若迁移代码曾引用 MCP
   层辅助函数而未复制实现，这里会立刻暴露。

注意：test/ 下的脚本不属于主程序运行时代码——手动验证脚本允许通过 MCP
协议（util.mcp_client）调用 MCP 工具，个别测试允许用 importlib 直接加载
MCP 层模块做单元测试，均不在本守卫扫描范围内。
"""
import ast
import builtins
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 主程序运行时代码（MCP 协议层之外的全部业务/框架代码）
MAIN_PROGRAM_DIRS = ("factory", "routers", "memory", "util", "chat", "prompt")
MAIN_PROGRAM_FILES = ("config.py", "env_manager.py", "main.py")

# 违规模式：对 mcp_server 实现层的导入 / 动态加载 / 路径注入
_VIOLATION_PATTERNS = (
    # import sys_tools_server / from sys_tools_server import ...
    # import mcp_server / from mcp_server import ... / from mcp_server.xxx import ...
    re.compile(r"(?m)^\s*(?:from|import)\s+(?:mcp_server|sys_tools_server)\b"),
    # importlib.import_module("mcp_server...") / import_module('mcp_server...')
    re.compile(r"import_module\s*\(\s*[\"']mcp_server"),
    # importlib.util.spec_from_file_location(..., "...mcp_server/xxx.py")
    re.compile(r"spec_from_file_location\s*\([^)]*mcp_server"),
    # sys.path.insert/append(..., "...mcp_server...")
    re.compile(r"sys\.path\.(?:insert|append)\s*\([^)]*mcp_server"),
)


def _find_violations(source: str) -> list[str]:
    """在单份源码中查找违规引用，返回 ["L{行号}: {行内容}", ...]（去重）。"""
    violations: list[str] = []
    seen: set[tuple[int, str]] = set()
    lines = source.splitlines()
    for pattern in _VIOLATION_PATTERNS:
        for match in pattern.finditer(source):
            line_no = source.count("\n", 0, match.start()) + 1
            line = lines[line_no - 1].strip() if line_no - 1 < len(lines) else ""
            key = (line_no, line)
            if key not in seen:
                seen.add(key)
                violations.append(f"L{line_no}: {line}")
    return violations


def _iter_main_sources():
    """遍历主程序运行时代码的全部 .py 文件。"""
    for dirname in MAIN_PROGRAM_DIRS:
        base = PROJECT_ROOT / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path
    for filename in MAIN_PROGRAM_FILES:
        path = PROJECT_ROOT / filename
        if path.is_file():
            yield path


def _collect_undefined_global_names(source: str) -> list[str]:
    """AST 静态检查：返回「被引用但从未定义/导入」的全局名字列表。

    收集规则（故意宽松，只抓完全未定义的名字）：
    - 已定义：builtins、模块/函数/类名、所有赋值目标（含局部变量、参数、
      for/with/except 目标、global/nonlocal 声明）、import 别名；
    - 被引用：所有 Load 上下文的 Name；
    - 差集即「从未在任何地方出现过定义的名字」——正常的自包含模块应为空。
    """
    tree = ast.parse(source)
    defined: set[str] = set(dir(builtins)) | {
        "__name__", "__file__", "__doc__", "__package__",
        "__spec__", "__loader__", "__builtins__",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.alias):
            defined.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return sorted(used - defined)


class LayeringGuardTests(unittest.TestCase):
    def test_main_program_sources_do_not_reference_mcp_layer(self):
        """主程序运行时代码不得导入 mcp_server 层的实现。"""
        violations: list[str] = []
        scanned = 0
        for path in _iter_main_sources():
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for item in _find_violations(source):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {item}")
        self.assertGreater(scanned, 20, "扫描文件数异常，请检查 MAIN_PROGRAM_DIRS")
        self.assertEqual(
            violations, [],
            msg=(
                "检测到主程序对 MCP 协议层实现的依赖（禁止）：\n"
                + "\n".join(violations)
                + "\n\n正确做法：把需要的实现复制/迁移到主程序侧"
                "（factory/agent_runtime/ 或 util/），而不是 import mcp_server/ 下的文件。"
            ),
        )

    def test_scanner_detects_known_violation_samples(self):
        """扫描器有效性：合成违规样本必须被检出，合法写法不得误报。"""
        violation_samples = (
            "import sys_tools_server",
            "from sys_tools_server import _run_command_impl",
            "from mcp_server.sys_tools_server import _run_command_impl",
            "import mcp_server.restart_tools_server",
            "import importlib\nmod = importlib.import_module('mcp_server.sys_tools_server')",
            'spec = importlib.util.spec_from_file_location("x", "mcp_server/sys_tools_server.py")',
            'sys.path.insert(0, str(ROOT / "mcp_server"))',
        )
        for sample in violation_samples:
            with self.subTest(sample=sample):
                self.assertTrue(_find_violations(sample), msg=f"未检出违规样本: {sample}")

        clean_samples = (
            "import os\nfrom util.mcp_client import call_mcp_tool",
            "from factory.agent_runtime.builtin_tools import execute_run_command",
            "# 注释里提到 mcp_server/sys_tools_server.py 是允许的（非导入）",
            'config = {"command": "python", "args": ["mcp_server/sys_tools_server.py"]}',
        )
        for sample in clean_samples:
            with self.subTest(sample=sample):
                self.assertEqual(_find_violations(sample), [], msg=f"误报: {sample}")

    def test_builtin_tools_module_is_self_contained(self):
        """内置工具模块自包含：不存在任何「从未定义/导入」的全局名字引用。

        该检查是迁移完整性的硬保证：若内置实现引用了 MCP 层辅助函数
        （或任何本文件没有的名字），会在这里以列表形式暴露。
        """
        path = PROJECT_ROOT / "factory" / "agent_runtime" / "builtin_tools.py"
        source = path.read_text(encoding="utf-8")
        undefined = _collect_undefined_global_names(source)
        self.assertEqual(
            undefined, [],
            msg=f"builtin_tools.py 存在未定义的全局名字引用（迁移不完整或依赖外部实现）: {undefined}",
        )


if __name__ == "__main__":
    unittest.main()
