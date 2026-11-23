"""web_tools_server.py 独立性回归：零项目依赖，可放任意目录独立运行。"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

SERVER_FILE = Path(__file__).resolve().parents[1] / "mcp_server" / "web_tools_server.py"
PROJECT_TOP_LEVELS = {"util", "factory", "config", "routers", "memory", "chat",
                      "security", "mcp_server", "test"}


class StandaloneImportTests(unittest.TestCase):
    def test_no_project_imports(self):
        tree = ast.parse(SERVER_FILE.read_text(encoding="utf-8"), str(SERVER_FILE))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                continue
            bad.extend(m for m in mods if m in PROJECT_TOP_LEVELS)
        self.assertEqual(bad, [], "web_tools_server.py 不得导入项目模块")

    def test_no_sys_path_hack(self):
        text = SERVER_FILE.read_text(encoding="utf-8")
        self.assertNotIn("sys.path", text)
        self.assertNotIn("PROJECT_ROOT", text)

    def test_runs_from_isolated_directory(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory(prefix="standalone_web_") as tmp:
            shutil.copy(SERVER_FILE, Path(tmp) / "web_tools_server.py")
            probe = (
                "import sys;"
                "assert not [p for p in sys.path if 'agent_tool_sse' in p.replace(chr(92), '/')];"
                "import web_tools_server as s;"
                "assert all(callable(getattr(s, n)) for n in"
                " ('fetch_url','web_search','web_open','web_click','web_find'));"
                "assert s.decode_text('中文'.encode('gbk'))[0] == '中文';"
                "assert s.int_arg({'n': 5}, 'n', 0) == 5;"
                "print('ISOLATED-OK')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe], cwd=tmp, capture_output=True,
                text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("ISOLATED-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
