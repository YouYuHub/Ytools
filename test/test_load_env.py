import tempfile
import unittest
from pathlib import Path

from env_manager import init_path, load_var, set_env_vars
from config import apply_persisted_work_dir, get_current_dir


class LoadEnvTests(unittest.TestCase):
    def test_set_env_vars_persists_to_env_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        original_env = repo_root / ".env"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            env_path = temp_path / ".env"
            env_path.write_text("EXISTING=1\n", encoding="utf-8")

            try:
                init_path(str(temp_path))
                updated = set_env_vars({
                    "HISTORY_COMPACT_KEEP_ROUNDS": 9,
                    "HISTORY_COMPACT_TRIGGER_RATIO": 0.75,
                })

                content = env_path.read_text(encoding="utf-8")
                self.assertIn("HISTORY_COMPACT_KEEP_ROUNDS=9", content)
                self.assertIn("HISTORY_COMPACT_TRIGGER_RATIO=0.75", content)
                self.assertEqual(updated["HISTORY_COMPACT_KEEP_ROUNDS"], "9")
                self.assertEqual(updated["HISTORY_COMPACT_TRIGGER_RATIO"], "0.75")
                self.assertEqual(load_var("HISTORY_COMPACT_KEEP_ROUNDS"), "9")
                self.assertEqual(load_var("HISTORY_COMPACT_TRIGGER_RATIO"), "0.75")
            finally:
                init_path(str(repo_root))
                self.assertTrue(original_env.exists())

    def test_apply_persisted_work_dir_from_env(self):
        repo_root = Path(__file__).resolve().parents[1]
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            env_path = temp_path / ".env"
            target_dir = temp_path / "workspace_a"
            target_dir.mkdir(parents=True, exist_ok=True)
            env_path.write_text(f"CHAT_WORK_DIR={target_dir.as_posix()}\n", encoding="utf-8")

            try:
                init_path(str(temp_path))
                changed = apply_persisted_work_dir()
                self.assertTrue(changed)
                self.assertEqual(
                    Path(get_current_dir()).resolve(),
                    target_dir.resolve(),
                )
                self.assertEqual(load_var("CHAT_WORK_DIR"), target_dir.as_posix())
            finally:
                init_path(str(repo_root))
                try:
                    from config import set_current_dir
                    set_current_dir(str(original_cwd))
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()