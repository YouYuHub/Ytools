# coding: utf-8
"""内置终端命令工具（run_command）单元测试。

覆盖：
- 工具定义与注入（include_run_command）
- 名称注册（is_builtin_tool / SELECTABLE_BUILTIN_TOOL_NAMES）
- execute_run_command / try_execute_builtin_command_tool 基本执行（echo / 退出码 / 参数校验）
- cmd 家族补丁辅助：内联代码定位（多行 / 含 % 的单行）、临时脚本改写、简单管道过滤器解析、本地 tail/head 兜底
说明：真实命令执行依赖系统 shell（Windows 为 cmd）；非 Windows 环境自动跳过。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt

IS_WINDOWS = os.name == "nt"


class BuiltinCommandToolTests(unittest.TestCase):
    def test_poll_tail_lines_and_budget_leave_log_intact(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bt, '_BG_LOG_DIR', Path(folder)):
            path = Path(folder) / 'bg_tail.out.txt'
            source = ''.join(f'第{i}行输出\n' for i in range(400))
            path.write_bytes(source.encode())
            job_id = bt._save_command_job(12345, path, None, 'powershell', folder, 'tail-session')
            with patch.object(bt, '_pid_alive_windows', return_value=True), patch.object(bt, '_pid_alive_posix', return_value=True):
                result = bt.try_execute_builtin_command_tool('poll_command', {'job_id': job_id, 'tail_lines': 3, '_session_id': 'tail-session'})
                self.assertEqual(result['output_tail'], ''.join(f'第{i}行输出\n' for i in range(397, 400)))
                self.assertTrue(result['output_truncated'])
                limited = bt.execute_run_command({'action': 'poll', 'job_id': job_id, 'tail_lines': 200, '_session_id': 'tail-session', '_max_chars': 512})
                self.assertLessEqual(len(limited['output_tail']), 512)
                self.assertTrue(limited['output_tail'].endswith('第399行输出\n'))
                self.assertEqual(path.read_bytes(), source.encode())
                with self.assertRaisesRegex(ValueError, '不属于当前会话'):
                    bt.execute_run_command({'action': 'poll', 'job_id': job_id, 'tail_lines': 3, '_session_id': 'other'})

    def test_poll_rejects_invalid_tail_lines_before_job_access(self):
        for value in (0, 201, True, 1.5, '3'):
            result = bt.try_execute_builtin_command_tool('poll_command', {'job_id': 'bg_missing', 'tail_lines': value})
            self.assertIn('1-200', result['error'])
        for name in ('run_command', 'stop_command'):
            result = bt.try_execute_builtin_command_tool(name, {'tail_lines': 3, 'job_id': 'bg_missing'} if name == 'stop_command' else {'tail_lines': 3, 'command': 'echo bad'})
            self.assertIn('不接受 tail_lines', result['error'])

    def test_tail_large_unicode_and_single_line_logs_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tail.txt'
            for encoding in ('utf-8', 'utf-16', 'utf-32'):
                source = '汉字🙂' * 50000 + '\n最后一行中文\n'
                path.write_bytes(source.encode(encoding))
                text, truncated = bt._command_output_tail(path, 1, 12000)
                self.assertEqual(text, '最后一行中文\n')
                self.assertTrue(truncated)
            path.write_bytes(('汉字🙂' * 50000).encode())
            text, truncated = bt._command_output_tail(path, 1, 512)
            self.assertTrue(text.endswith('汉字🙂'))
            self.assertNotIn('�', text)
            self.assertEqual(len(text), 512)
            self.assertTrue(truncated)
            path.write_bytes((('汉字🙂' * 50000) + '\n').encode())
            text, truncated = bt._command_output_tail(path, 4, 512)
            self.assertTrue(text.endswith('汉字🙂\n'))
            self.assertTrue(text)
            self.assertTrue(truncated)
    # ---------- 注入与名称注册 ----------

    def test_auto_shell_prefers_windows_powershell_and_falls_back_to_cmd(self):
        with patch.object(bt.shutil, "which", return_value="C:/Windows/powershell.exe"):
            exe, kind, args = bt._resolve_shell("auto", True)
            self.assertEqual(kind, "powershell")
            self.assertIn("-Command", args)
        with patch.object(bt.shutil, "which", return_value=None):
            self.assertEqual(bt._resolve_shell("auto", True)[1], "cmd")

    def test_auto_shell_is_sh_on_linux_even_when_bash_is_available(self):
        with patch.object(bt.shutil, "which", side_effect=lambda name: "/bin/" + name):
            self.assertEqual(bt._resolve_shell("auto", False), ("/bin/sh", "sh", []))

    def test_large_command_output_preview_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "output.txt"
            path.write_bytes(b"a" * 65)
            with patch.object(bt, "_RUN_COMMAND_PREVIEW_BYTES", 64):
                preview, truncated = bt._read_command_output_preview(path, ["utf-8"])
            self.assertEqual(preview, "a" * 64)
            self.assertTrue(truncated)

    def test_inject_run_command(self):
        tools, servers = bt.inject_builtin_tools([], {}, include_run_command=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("run_command", names)
        self.assertIn("poll_command", names)
        self.assertIn("stop_command", names)
        self.assertEqual(servers["run_command"], "__builtin__")
        # 不勾选时不注入
        tools2, _ = bt.inject_builtin_tools([], {})
        self.assertNotIn("run_command", {t["function"]["name"] for t in tools2})

    def test_selectable_names_include_run_command(self):
        self.assertIn("run_command", bt.SELECTABLE_BUILTIN_TOOL_NAMES)
        self.assertTrue(bt.is_builtin_tool("run_command"))
        self.assertFalse(bt.is_builtin_tool("run_command_extra"))

    def test_tool_definition_schema(self):
        definition = bt.RUN_COMMAND_TOOL_DEFINITION["function"]
        self.assertEqual(definition["name"], "run_command")
        params = definition["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["command"])
        for key in ("command", "shell", "work_dir", "timeout_seconds", "background"):
            self.assertIn(key, params["properties"])
        self.assertNotIn("job_id", params["properties"])
        self.assertEqual(bt.POLL_COMMAND_TOOL_DEFINITION["function"]["parameters"]["required"], ["job_id"])
        self.assertEqual(bt.STOP_COMMAND_TOOL_DEFINITION["function"]["parameters"]["required"], ["job_id"])

    def test_command_action_tools_dispatch_and_reject_mixed_parameters(self):
        for name in ("poll_command", "stop_command"):
            result = bt.try_execute_builtin_command_tool(name, {"job_id": "missing"})
            self.assertIsNotNone(result)
            self.assertEqual(result.get("tool"), name)
            invalid = bt.try_execute_builtin_command_tool(name, {"job_id": "missing", "command": "echo bad"})
            self.assertIn("只接受 job_id", invalid["error"])
        with self.assertRaisesRegex(ValueError, "不接受 job_id"):
            bt.execute_run_command({"command": "echo bad", "job_id": "missing"})
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            bt.execute_run_command({"command": "echo bad", "timeout_seconds": 0})

    # ---------- 执行入口 ----------

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_execute_echo(self):
        result = bt.execute_run_command({"command": "echo builtin_rc_ok"})
        self.assertIsInstance(result, str)
        self.assertIn("exit=0", result)
        self.assertIn("builtin_rc_ok", result)

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_execute_exit_code(self):
        result = bt.execute_run_command({"command": "cmd /c exit 7"})
        self.assertIn("exit=7", result)

    def test_execute_empty_command_rejected(self):
        with self.assertRaises(ValueError):
            bt.execute_run_command({"command": "   "})

    def test_try_execute_dispatch(self):
        self.assertIsNone(bt.try_execute_builtin_command_tool("read_file", {}))
        result = bt.try_execute_builtin_command_tool("run_command", {"command": ""})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)
        self.assertEqual(result["tool"], "run_command")

    def test_structured_result_preserves_legacy_message(self):
        result = bt._structured_command_result(
            "[run_command] shell=cmd | cwd=C:/work | exit=7 | 耗时 0.1s\n--- stderr ---\nfailed"
        )
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["shell"], "cmd")
        self.assertIn("--- stderr ---", result["message"])
        background = bt._structured_command_result(
            "[run_command] 后台模式已启动 | shell=cmd | pid=12 | job_id=bg_demo\n"
            "输出文件: C:/logs/bg_demo.out.txt\n"
            "结束标记: C:/logs/bg_demo.done.txt（完成后写入退出码）"
        )
        self.assertEqual(background["status"], "running")
        self.assertEqual(background["job_id"], "bg_demo")
        self.assertEqual(background["output_path"], "C:/logs/bg_demo.out.txt")

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_model_result_has_nonduplicated_output_fields(self):
        result = bt.try_execute_builtin_command_tool("run_command", {"command": "echo structured_ok"})
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("structured_ok", result["stdout"])
        self.assertNotIn("structured_ok", result["message"])

    def test_background_job_can_be_polled_and_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(bt, "_BG_LOG_DIR", Path(folder)), \
                    patch("util.process_identity.process_identity", return_value="test:12345"), \
                    patch("factory.agent_runtime.command_jobs.process_identity", return_value="test:12345"):
                output = Path(folder) / "bg_test.out.txt"
                output.write_text("progress", encoding="utf-8")
                job_id = bt._save_command_job(12345, output, None, "cmd", folder, "session-a")
                with patch.object(bt, "_pid_alive_windows", return_value=True), \
                        patch.object(bt, "_pid_alive_posix", return_value=True), \
                        patch.object(bt.os, "killpg", create=True) as stop_group, \
                        patch.object(bt.subprocess, "run") as stop_command:
                    stop_command.return_value.returncode = 0
                    with self.assertRaisesRegex(ValueError, "不属于当前会话"):
                        bt.execute_run_command({"action": "poll", "job_id": job_id, "_session_id": "session-b"})
                    polled = bt.execute_run_command({"action": "poll", "job_id": job_id, "_session_id": "session-a"})
                    self.assertEqual(polled["status"], "running")
                    self.assertEqual(polled["output_tail"], "progress")
                    stopped = bt.execute_run_command({"action": "stop", "job_id": job_id, "_session_id": "session-a"})
                    self.assertEqual(stopped["status"], "stopped")
                    if IS_WINDOWS:
                        stop_command.assert_called_once()
                    else:
                        stop_group.assert_called_once()
                with self.assertRaisesRegex(ValueError, "job_id 无效"):
                    bt.execute_run_command({"action": "poll", "job_id": "../other"})

    # ---------- cmd 家族补丁辅助 ----------

    def test_multiline_inline_code_rewrite(self):
        command = 'python -c "\nimport sys\nprint(sys.version)\n"'
        found = bt._find_inline_code(command)
        self.assertIsNotNone(found)
        self.assertEqual(found["interp"], "python")
        self.assertEqual(found["flag"], "-c")
        # 单行内联代码不改写（交给 shell 原生处理）
        self.assertIsNone(bt._find_inline_code('python -c "print(1)"'))

    def test_multiline_python_x_option_and_unsafe_syntax(self):
        command = 'python -X utf8 -c "import sys\nprint(sys.version)"'
        found = bt._find_inline_code(command)
        self.assertIsNotNone(found)
        self.assertEqual(found["flag"], "-c")
        self.assertEqual(found["code"], "import sys\nprint(sys.version)")
        with self.assertRaisesRegex(ValueError, "命令未执行"):
            bt._rewrite_inline_code_to_script(
                'python -c "print(1)\nprint(2)" ; echo done', "cmd")

    @unittest.skipUnless(IS_WINDOWS, "cmd 语法仅 Windows 验证")
    def test_cmd_heredoc_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, "heredoc.*命令未执行"):
            bt.execute_run_command({"command": "python - <<'EOF'\nprint('hello')\nEOF", "shell": "cmd"})

    def test_mixed_utf8_and_gbk_output_keeps_each_line(self):
        raw = "子任务 supportDocTypes 解析失败\n".encode("utf-8")
        raw += "模型配置字段\n".encode("gbk")
        decoded, used = bt._decode_command_bytes_used(raw, candidates=("gbk", "utf-8"))
        self.assertEqual(decoded, "子任务 supportDocTypes 解析失败\n模型配置字段\n")
        self.assertEqual(used, "mixed")

    def test_replacement_characters_are_flagged_as_uncertain(self):
        with patch.object(bt, "_run_command_foreground", return_value=(0, "字段��损坏", "", False, 0.1, None)):
            result = bt.execute_run_command({"command": "echo check"})
        self.assertIn("输出包含替换字符", result)
        self.assertIn("勿据此判断文本原貌", result)

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_multiline_python_x_option_executes_as_one_script(self):
        with tempfile.TemporaryDirectory(prefix="rc_test_", dir=".") as directory:
            with patch.object(bt, "_BG_LOG_DIR", Path(directory).resolve()), \
                    patch.object(bt, "_wmi_create_process", side_effect=RuntimeError("test direct path")):
                result = bt.execute_run_command({
                    "command": 'python -X utf8 -c "print(\'第一行\')\nprint(\'第二行\')"'
                })
        self.assertIn("exit=0", result)
        self.assertIn("第一行\n第二行", result)

    @unittest.skipUnless(IS_WINDOWS, "cmd 脚本换行仅 Windows 验证")
    def test_cmd_runner_has_single_crlf(self):
        with tempfile.TemporaryDirectory(prefix="rc_test_", dir=".") as directory:
            runner = Path(directory) / "run.cmd"
            bt._write_runner_script(runner, "cmd", "echo one\r\necho two")
            contents = runner.read_bytes()
        self.assertNotIn(b"\r\r\n", contents)
        self.assertIn(b"echo one\r\necho two", contents)

    def test_single_line_percent_needs_script(self):
        # 需要脚本化：多行，或单行含 `%`（cmd 批处理层会破坏 %）
        self.assertTrue(bt._inline_code_needs_script("print(1)\nprint(2)", True))
        self.assertTrue(bt._inline_code_needs_script("print('100%')", False))
        self.assertFalse(bt._inline_code_needs_script("print(1)", False))

    def test_locate_single_line_percent_segment(self):
        # 单行含 % 的段可被定位（供改写为临时脚本）
        found = bt._locate_inline_code_segment('python -c "print(\'100%\')"')
        self.assertIsNotNone(found)
        self.assertEqual(found["code"], "print('100%')")
        self.assertFalse(found["multi_line"])
        # 单行不含 %：不需要脚本化，定位结果为 None（命令原样交给 shell）
        self.assertIsNone(bt._locate_inline_code_segment('python -c "print(1)"'))

    def test_locate_then_rewrite_single_line_percent(self):
        # 定位后改写为临时脚本（含 % 的单行走脚本而非 %% 转义）
        cmd, notes = bt._rewrite_inline_code_to_script('python -c "print(\'100%\')"', "cmd")
        self.assertNotEqual(cmd, 'python -c "print(\'100%\')"')
        self.assertNotIn("-c", cmd)
        self.assertTrue(notes)
        self.assertIn("单行内联代码中的 `%`", notes[0])

    def test_rewrite_multiple_single_line_percent_segments(self):
        # 双命令串联：两段各自改写为独立脚本（不能因引号吞并而漏改写）
        cmd = 'python -c "print(\'a%\')" && python -c "print(\'b%\')"'
        all_notes = []
        for _ in range(10):
            new_cmd, notes = bt._rewrite_inline_code_to_script(cmd, "cmd")
            all_notes.extend(notes)
            if new_cmd == cmd:
                break
            cmd = new_cmd
        self.assertNotIn("-c ", cmd)
        self.assertEqual(cmd.count(".py"), 2)
        self.assertTrue(all_notes)

    def test_rewrite_skips_plain_single_line_and_non_cmd(self):
        # 单行不含 %：不改写；非 cmd：不处理
        cmd, notes = bt._rewrite_inline_code_to_script('python -c "print(1)"', "cmd")
        self.assertEqual(cmd, 'python -c "print(1)"')
        self.assertEqual(notes, [])
        cmd2, notes2 = bt._rewrite_inline_code_to_script('python -c "print(\'1%\')"', "bash")
        self.assertEqual(cmd2, 'python -c "print(\'1%\')"')
        self.assertEqual(notes2, [])

    def test_has_multiline_inline_intent(self):
        # 拒绝判定只看跨行代码意图：单行内联代码（含其它 shell 行）不应误杀
        self.assertTrue(bt._has_multiline_inline_intent('python -c "print(1)\nprint(2)"'))
        self.assertFalse(bt._has_multiline_inline_intent('python -c "print(1)"'))
        self.assertFalse(bt._has_multiline_inline_intent('echo done\npython -c "print(1)"'))

    def test_find_inline_code_min_lines_and_quote_guard(self):
        single = 'python -c "print(1)"'
        self.assertIsNone(bt._find_inline_code(single))
        found = bt._find_inline_code(single, min_lines=1)
        self.assertIsNotNone(found)
        self.assertFalse(found["multi_line"])
        # 前段单行、后段多行：默认门槛跳过单行匹配，继续找到多行
        mixed = 'python -c "print(1)" && python -c "print(2)\nprint(3)"'
        found2 = bt._find_inline_code(mixed)
        self.assertIsNotNone(found2)
        self.assertTrue(found2["multi_line"])
        # 闭引号吞并后续引号段（`"A" && echo "x"`）：安全判定失败返回 None
        self.assertIsNone(bt._find_inline_code('python -c "print(1)" && echo "x"', min_lines=1))

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_single_line_percent_executes_with_literal_percent(self):
        with tempfile.TemporaryDirectory(prefix="rc_test_", dir=".") as directory:
            with patch.object(bt, "_BG_LOG_DIR", Path(directory).resolve()), \
                    patch.object(bt, "_wmi_create_process", side_effect=RuntimeError("test direct path")):
                result = bt.execute_run_command({"command": 'python -c "print(\'100%\')"', "shell": "cmd"})
        self.assertIn("exit=0", result)
        self.assertIn("100%", result)
        self.assertIn("已自动改写为临时脚本", result)

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_single_line_percent_format_executes(self):
        # 原坑：`print('百分比 %d' % 5)` 因 % 被吞而产生 SyntaxError
        with tempfile.TemporaryDirectory(prefix="rc_test_", dir=".") as directory:
            with patch.object(bt, "_BG_LOG_DIR", Path(directory).resolve()), \
                    patch.object(bt, "_wmi_create_process", side_effect=RuntimeError("test direct path")):
                result = bt.execute_run_command({"command": 'python -c "print(\'百分比 %d\' % 5)"'})
        self.assertIn("exit=0", result)
        self.assertIn("百分比 5", result)

    def test_simple_filter_parse_and_local_fallback(self):
        self.assertEqual(bt._parse_simple_filter("tail -20"), ("tail", 20))
        self.assertEqual(bt._parse_simple_filter("tail -4 2>&1"), ("tail", 4))
        self.assertEqual(bt._parse_simple_filter("head -n 5"), ("head", 5))
        self.assertIsNone(bt._parse_simple_filter("tail -f"))
        text = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
        self.assertEqual(bt._apply_local_line_filter(text, "tail", 2), "line9\nline10\n")
        self.assertEqual(bt._apply_local_line_filter(text, "head", 2), "line1\nline2\n")


    def test_powershell_simple_filters_use_native_pipeline(self):
        command, notes = bt._guard_powershell_pipeline("'a | b' | tail -4")
        self.assertEqual(command, "'a | b' | Select-Object -Last 4")
        self.assertTrue(notes)
        command, _ = bt._guard_powershell_pipeline("1..10 | head -n 3")
        self.assertEqual(command, "1..10 | Select-Object -First 3")
        command, _ = bt._guard_powershell_pipeline("1..10 | tail -n 4 | Out-String")
        self.assertEqual(command, "1..10 | Select-Object -Last 4 | Out-String")
        command, _ = bt._guard_powershell_pipeline("1..10 | tail -4 2>&1; Write-Output done")
        self.assertEqual(command, "1..10 | Select-Object -Last 4 2>&1; Write-Output done")
        self.assertEqual(bt._guard_powershell_pipeline("Write-Output a`|tail -4"), ("Write-Output a`|tail -4", []))
        self.assertEqual(bt._guard_powershell_pipeline("1..10 | tail -f"), ("1..10 | tail -f", []))

    @unittest.skipUnless(IS_WINDOWS, "PowerShell 管道执行用例仅 Windows 验证")
    def test_powershell_tail_preserves_large_upstream_output(self):
        command = f'& "{sys.executable}" -c "[print(i) for i in range(5000)]" 2>&1 | tail -4'
        result = bt.execute_run_command({
            "command": command, "shell": "powershell", "timeout_seconds": 30,
            "_structured_output": True,
        })
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].splitlines(), ["4996", "4997", "4998", "4999"])

    @unittest.skipUnless(IS_WINDOWS, "PowerShell 管道执行用例仅 Windows 验证")
    def test_powershell_tail_before_another_stage_and_redirection(self):
        source = f'& "{sys.executable}" -c "[print(i) for i in range(5000)]" 2>&1'
        for suffix in ("tail -4 | Out-String", "tail -4 2>&1"):
            with self.subTest(suffix=suffix):
                result = bt.execute_run_command({
                    "command": f"{source} | {suffix}", "shell": "powershell",
                    "timeout_seconds": 30, "_structured_output": True,
                })
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["stdout"].strip().splitlines(), ["4996", "4997", "4998", "4999"])

    @unittest.skipUnless(IS_WINDOWS, "PowerShell 直连执行用例仅 Windows 验证")
    def test_powershell_direct_fallback_preserves_upstream_exit_code(self):
        command = f'& "{sys.executable}" -c "import sys; print(\'marker\'); sys.exit(7)" 2>&1 | tail -4'
        with patch.object(bt, "_write_relay_script", side_effect=RuntimeError("force direct")):
            result = bt.execute_run_command({
                "command": command, "shell": "powershell", "timeout_seconds": 30,
                "_structured_output": True,
            })
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"].strip(), "marker")

    @unittest.skipUnless(IS_WINDOWS, "cmd 管道执行用例仅 Windows 验证")
    def test_cmd_local_filters_handle_large_output_file(self):
        source = f'"{sys.executable}" -c "[print(str(i).zfill(5) + \'x\'*1024) for i in range(2000)]"'
        for filter_name, expected in (
            ("tail", ["01996", "01997", "01998", "01999"]),
            ("head", ["00000", "00001", "00002", "00003"]),
        ):
            with self.subTest(filter_name=filter_name), \
                    patch.object(bt, "_resolve_filter_path", return_value=""), \
                    patch.object(bt, "_find_working_filter", return_value=None):
                result = bt.execute_run_command({
                    "command": f"{source} | {filter_name} -4", "shell": "cmd",
                    "timeout_seconds": 30, "_structured_output": True,
                })
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual([line[:5] for line in result["stdout"].splitlines()], expected)
            if filter_name == "tail":
                self.assertIn("完整输出文件末尾", result["message"])

    @unittest.skipUnless(IS_WINDOWS, "cmd 管道执行用例仅 Windows 验证")
    def test_cmd_unavailable_filter_rejects_unsafe_background_and_middle_stage(self):
        with patch.object(bt, "_resolve_filter_path", return_value=""), \
                patch.object(bt, "_find_working_filter", return_value=None):
            with self.assertRaisesRegex(ValueError, "命令未执行"):
                bt.execute_run_command({"command": "echo a | tail -4", "shell": "cmd", "background": True})
            with self.assertRaisesRegex(ValueError, "管道中间"):
                bt._guard_cmd_pipeline("echo a | tail -4 | findstr a")
            command, local_filter, _ = bt._guard_cmd_pipeline("echo a | tail -4 2>&1")
            self.assertEqual(command, "echo a")
            self.assertEqual(local_filter, ("tail", 4))

if __name__ == "__main__":
    unittest.main()
