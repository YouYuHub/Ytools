"""Background registry/API tests, including real hidden processes on the host."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from factory.agent_runtime import builtin_tools as bt, command_jobs as jobs
from routers.terminal_router import api_terminal_router
from util.process_identity import process_identity


class TerminalJobsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root_patch = patch.object(bt, "_BG_LOG_DIR", self.root)
        self.root_patch.start()
        app = FastAPI()
        app.include_router(api_terminal_router)
        self.client = TestClient(app)

    def tearDown(self):
        self.root_patch.stop()
        self.directory.cleanup()

    def record(self, name="bg_example", **changes):
        output = self.root / f"{name}.out.txt"
        output.write_text("第一行\n", encoding="utf-8")
        done = self.root / f"{name}.done.txt"
        done.write_text("0", encoding="utf-8")
        bt._save_command_job(99999999, output, done, "sh", str(self.root), "session-1", "echo 中文")
        path, meta = jobs.read_job(name)
        meta.update(changes)
        jobs.write_metadata(path, meta)
        return name, output, done

    def test_list_output_append_download_and_close(self):
        name, output, _ = self.record()
        result = jobs.list_jobs("session-1")
        self.assertEqual(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"], [])
        self.assertEqual(result[0]["command"], "echo 中文")
        self.assertNotIn("output_tail", result[0])
        self.assertEqual(self.client.get(f"/terminal/jobs/{name}?session_id=session-1").json()["status"], "completed")
        with output.open("a", encoding="utf-8") as handle:
            handle.write("追加输出\n")
        self.assertIn("追加输出", self.client.get(f"/terminal/jobs/{name}?session_id=session-1").json()["output_tail"])
        self.assertEqual(self.client.get(f"/terminal/jobs/{name}/log/download?session_id=session-1").content, output.read_bytes())
        self.assertEqual(self.client.post(f"/terminal/jobs/{name}/close?session_id=session-1").status_code, 200)
        self.assertEqual(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"], [])
        self.assertEqual(jobs.action(name, "poll", "session-1")["status"], "completed")
        self.assertTrue(output.exists(), "关闭 UI 记录不损坏模型工具日志引用")

    def test_failed_exited_and_bounded_output(self):
        name, output, done = self.record()
        done.write_text("7", encoding="utf-8")
        output.write_text("行\n" * 200000, encoding="utf-8")
        response = self.client.get(f"/terminal/jobs/{name}?session_id=session-1").json()
        self.assertEqual(response["status"], "failed")
        self.assertTrue(response["output_truncated"])
        self.assertLessEqual(len(response["output_tail"]), 131072)
        done.unlink()
        self.assertEqual(jobs.action(name, "poll")["status"], "unknown")

    def test_api_lists_only_current_session_running_jobs(self):
        name, _, done = self.record(process_identity="same")
        done.unlink()
        _, _, other_done = self.record(name="bg_other", session_id="session-2", process_identity="same")
        other_done.unlink()
        _, _, orphan_done = self.record(name="bg_orphan", session_id="", process_identity="same")
        orphan_done.unlink()
        self.record(name="bg_completed")
        self.record(name="bg_stopped", stopped=True)
        self.record(name="bg_closed", closed=True)
        _, _, failed_done = self.record(name="bg_failed")
        failed_done.write_text("7", encoding="utf-8")
        _, _, reused_done = self.record(name="bg_reused", process_identity="old")
        reused_done.unlink()
        with patch.object(bt, "_pid_alive_windows", return_value=True), \
                patch.object(bt, "_pid_alive_posix", return_value=True), \
                patch.object(jobs, "process_identity", return_value="same"):
            current = self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"]
            self.assertEqual([item["job_id"] for item in current], [name])
            other = self.client.get("/terminal/jobs?session_id=session-2").json()["jobs"]
            self.assertEqual([item["job_id"] for item in other], ["bg_other"])
            self.assertEqual(self.client.get("/terminal/jobs?session_id=new-session").json()["jobs"], [])
        # External taskkill/kill does not write our stopped flag, but still removes the entry.
        self.assertEqual(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"], [])

    def test_model_stop_hides_terminal_and_preserves_tool_log(self):
        name, output, done = self.record(process_identity="same")
        done.unlink()
        with patch.object(bt, "_pid_alive_windows", return_value=True), \
                patch.object(bt, "_pid_alive_posix", return_value=True), \
                patch.object(jobs, "process_identity", return_value="same"), \
                patch.object(jobs.os, "killpg", side_effect=[None, ProcessLookupError()], create=True), \
                patch.object(jobs.subprocess, "run", return_value=Mock(returncode=0)):
            self.assertEqual(len(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"]), 1)
            result = bt.try_execute_builtin_command_tool("stop_command", {"job_id": name, "_session_id": "session-1"})
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"], [])
            self.assertEqual(jobs.action(name, "poll", "session-1")["output_tail"].splitlines(), ["第一行"])
        self.assertTrue(output.exists())

    def test_finished_job_leaves_list_without_closing_its_record(self):
        name, output, done = self.record(process_identity="same")
        done.unlink()
        with patch.object(bt, "_pid_alive_windows", return_value=True), \
                patch.object(bt, "_pid_alive_posix", return_value=True), \
                patch.object(jobs, "process_identity", return_value="same"):
            self.assertEqual(len(jobs.list_jobs("session-1", active_only=True)), 1)
            done.write_text("0", encoding="utf-8")
            self.assertEqual(jobs.list_jobs("session-1", active_only=True), [])
            self.assertEqual(self.client.get(f"/terminal/jobs/{name}?session_id=session-1").json()["status"], "completed")
        self.assertTrue(output.exists())

    def test_http_requires_session_and_checks_read_stop_close_download_ownership(self):
        name, _, _ = self.record()
        self.assertEqual(self.client.get("/terminal/jobs").status_code, 422)
        self.assertEqual(self.client.get("/terminal/jobs?session_id=").status_code, 422)
        self.assertEqual(self.client.get(f"/terminal/jobs/{name}").status_code, 422)
        with patch.object(jobs.subprocess, "run") as signal_process:
            self.assertEqual(self.client.get(f"/terminal/jobs/{name}?session_id=other").status_code, 404)
            self.assertEqual(self.client.get(f"/terminal/jobs/{name}/log/download?session_id=other").status_code, 404)
            for operation in ("stop", "close"):
                self.assertEqual(self.client.post(f"/terminal/jobs/{name}/{operation}?session_id=other").status_code, 409)
            signal_process.assert_not_called()
        self.record(name="bg_orphan", session_id="")
        self.assertEqual(self.client.get("/terminal/jobs/bg_orphan?session_id=session-1").status_code, 404)

    def test_live_log_partial_unicode_does_not_turn_into_legacy_garbage(self):
        name, output, _ = self.record()
        raw = "实时输出中".encode("utf-8")
        output.write_bytes(raw[:-1])
        self.assertEqual(jobs.action(name, "poll")["output_tail"], "实时输出")
        output.write_bytes(raw)
        self.assertEqual(jobs.action(name, "poll")["output_tail"], "实时输出中")
        output.write_bytes("中文 GBK\n".encode("gbk"))
        self.assertEqual(jobs.action(name, "poll")["output_tail"], "中文 GBK\n")

    def test_pid_reuse_never_signals_unrelated_process(self):
        name, _, done = self.record(process_identity="original")
        done.unlink()
        with patch.object(bt, "_pid_alive_windows", return_value=True), \
                patch.object(bt, "_pid_alive_posix", return_value=True), \
                patch.object(jobs, "process_identity", return_value="different"), \
                patch.object(jobs.subprocess, "run") as signal_process:
            response = jobs.action(name, "stop")
            self.assertEqual(response["status"], "unknown")
            signal_process.assert_not_called()

    def test_legacy_job_can_only_be_hidden_without_killing(self):
        name, _, done = self.record(process_identity=None)
        done.unlink()
        with patch.object(bt, "_pid_alive_windows", return_value=True), \
                patch.object(bt, "_pid_alive_posix", return_value=True), \
                patch.object(jobs.subprocess, "run") as signal_process:
            self.assertFalse(jobs.list_jobs()[0]["can_stop"])
            self.assertEqual(self.client.post(f"/terminal/jobs/{name}/stop?session_id=session-1").status_code, 409)
            self.assertEqual(self.client.post(f"/terminal/jobs/{name}/close?session_id=session-1").status_code, 200)
            signal_process.assert_not_called()

    def test_scope_validation_and_no_arbitrary_log_paths(self):
        name, _, _ = self.record()
        with self.assertRaisesRegex(ValueError, "不属于当前会话"):
            jobs.action(name, "stop", "another-session")
        with self.assertRaisesRegex(ValueError, "job_id 无效"):
            jobs.read_job("../secret")
        path, meta = jobs.read_job(name)
        meta["output_path"] = str(self.root.parent / "secret.txt")
        jobs.write_metadata(path, meta)
        self.assertEqual(self.client.get(f"/terminal/jobs/{name}/log/download?session_id=session-1").status_code, 404)
        self.assertEqual(self.client.get("/terminal/jobs?session_id=session-1").json()["jobs"], [])
        self.assertEqual(self.client.post(f"/terminal/jobs/{name}/run?session_id=session-1").status_code, 400)

    def test_prune_retains_open_jobs_and_cleans_closed_logs(self):
        name, output, _ = self.record()
        old = time.time() - 48 * 3600
        for item in self.root.iterdir():
            os.utime(item, (old, old))
        bt._prune_old_bg_files()
        self.assertTrue(output.exists())
        jobs.action(name, "close")
        bt._prune_old_bg_files()
        self.assertTrue(output.exists(), "刚关闭记录仍至少保留 24h 日志")
        path, meta = jobs.read_job(name)
        meta["closed_at"] = old
        jobs.write_metadata(path, meta)
        for item in self.root.iterdir():
            os.utime(item, (old, old))
        bt._prune_old_bg_files()
        self.assertFalse(output.exists())
        self.assertTrue(list(self.root.glob("*.lock")), "跨进程锁文件不删除")

    def test_posix_launcher_uses_c_and_supervisor(self):
        fake = Mock(pid=99999999)
        with patch.object(bt.subprocess, "Popen", return_value=fake) as popen:
            bt._run_command_background("echo 中文", "/bin/sh", "sh", [], str(self.root), False, "s")
        arguments = popen.call_args.args[0]
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        spec = json.loads(Path(arguments[2]).read_text(encoding="utf-8"))
        self.assertEqual(spec["argv"], ["/bin/sh", "-c", "echo 中文"])
        self.assertTrue(spec["done"].endswith(".done.txt"))

    def test_portable_supervisor_writes_output_and_exit_code(self):
        output, done, spec = [self.root / name for name in ("out.txt", "done.txt", "spec.json")]
        spec.write_text(json.dumps({"argv": [sys.executable, "-u", "-c", "print('supervisor'); raise SystemExit(3)"],
            "cwd": str(self.root), "output": str(output), "done": str(done)}), encoding="utf-8")
        subprocess.run([sys.executable, str(Path(bt.__file__).with_name("background_runner.py")), str(spec)],
            timeout=10, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIn("supervisor", output.read_text())
        self.assertEqual(done.read_text(), "3")

    def test_process_identity_is_stable(self):
        first = process_identity(os.getpid())
        self.assertIsNotNone(first)
        self.assertEqual(first, process_identity(os.getpid()))
        self.assertIsNone(process_identity(99999999))

    def test_macos_identity_does_not_depend_on_running_state(self):
        import util.process_identity as module
        with patch.object(module, "os", SimpleNamespace(name="posix")), patch.object(module.sys, "platform", "darwin"), \
                patch.object(module.subprocess, "run") as ps:
            ps.return_value = Mock(returncode=0, stdout="Thu Oct  8 12:01:01 2026 S\n")
            asleep = module.process_identity(123)
            ps.return_value.stdout = "Thu Oct  8 12:01:01 2026 R+\n"
            self.assertEqual(asleep, module.process_identity(123))
            ps.return_value.stdout = "Thu Oct  8 12:01:01 2026 Z\n"
            self.assertIsNone(module.process_identity(123))

    def test_linux_identity_uses_boot_and_start_clock(self):
        import util.process_identity as module
        stat = "123 (name with spaces) " + " ".join(["S"] + ["0"] * 18 + ["987654"])
        def fake_path(path):
            return Mock(read_text=Mock(return_value="boot-id" if path.endswith("boot_id") else stat))
        with patch.object(module, "os", SimpleNamespace(name="posix")), patch.object(module.sys, "platform", "linux"), \
                patch.object(module, "Path", side_effect=fake_path):
            self.assertEqual(module.process_identity(123), "linux:boot-id:987654")
            stat = stat.replace(") S ", ") Z ")
            self.assertIsNone(module.process_identity(123))

    def test_posix_stop_signals_whole_group_and_escalates(self):
        for stubborn in (False, True):
            with self.subTest(stubborn=stubborn):
                name, _, done = self.record(name="bg_posix" + str(int(stubborn)), process_identity="same")
                done.unlink()
                group = Mock()
                if not stubborn:
                    group.side_effect = [None, ProcessLookupError()]
                platform_os = SimpleNamespace(name="posix", killpg=group, replace=os.replace)
                platform_time = SimpleNamespace(time=time.time, monotonic=Mock(side_effect=[0, 1, 3]), sleep=Mock())
                import signal
                with patch.object(jobs, "os", platform_os), patch.object(jobs, "process_identity", return_value="same"), \
                        patch.object(bt, "_pid_alive_posix", return_value=True), \
                        patch.object(jobs, "time", platform_time), \
                        patch.object(jobs, "signal", SimpleNamespace(SIGTERM=signal.SIGTERM, SIGKILL=9)):
                    self.assertEqual(jobs.action(name, "stop")["status"], "stopped")
                self.assertEqual(group.call_args_list[0].args, (99999999, signal.SIGTERM))
                if stubborn:
                    self.assertEqual(group.call_args_list[-1].args, (99999999, signal.SIGKILL if hasattr(signal, "SIGKILL") else 9))

    def test_real_background_completion_exit_code(self):
        shell, kind, prefix = bt._resolve_shell("auto", os.name == "nt")
        text = bt._run_command_background("echo completed_ok", shell, kind, prefix, str(self.root), os.name == "nt", "real")
        name = re.search(r"job_id=(bg_[A-Za-z0-9_]+)", text).group(1)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                state = jobs.action(name, "poll", "real")
                if state["status"] in ("completed", "failed"):
                    break
                time.sleep(.1)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["exit_code"], 0)
            self.assertIn("completed_ok", state["output_tail"])
        finally:
            jobs.action(name, "close", "real")

    def test_real_background_output_completion_and_stop(self):
        shell, kind, prefix = bt._resolve_shell("auto", os.name == "nt")
        script = self.root / "job.py"
        script.write_text("import time\nprint('实时中文输出', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        command = ("& " if kind in ("powershell", "pwsh") else "") + f'"{sys.executable}" -u "{script}"'
        text = bt._run_command_background(command, shell, kind, prefix, str(self.root), os.name == "nt", "real")
        name = re.search(r"job_id=(bg_[A-Za-z0-9_]+)", text).group(1)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                state = jobs.action(name, "poll", "real")
                if "实时中文输出" in state["output_tail"]:
                    break
                time.sleep(.1)
            self.assertIn("实时中文输出", state["output_tail"])
            self.assertTrue(state["can_stop"])
            stopped = jobs.action(name, "close", "real")
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(jobs.list_jobs(), [])
            deadline = time.monotonic() + 3
            while process_identity(state["pid"]) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIsNone(process_identity(state["pid"]))
        finally:
            jobs.action(name, "stop", "real")


if __name__ == "__main__":
    unittest.main()
