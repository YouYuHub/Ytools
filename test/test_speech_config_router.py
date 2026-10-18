"""Speech preferences use an isolated temporary .env; never edit project settings."""
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import env_manager
from routers import speech_config_router as router


class SpeechConfigRouterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.env_file = Path(directory) / ".env"
        self.env_file.write_text('# keep comment\nUNRELATED_KEY="keep"\n', encoding="utf-8")
        self.stack.enter_context(patch.object(env_manager, "env_file", str(self.env_file)))
        self.stack.enter_context(patch.object(env_manager, "env_vars", {"UNRELATED_KEY": "keep"}))
        self.stack.enter_context(patch.object(env_manager, "setting_vars", {}))
        app = FastAPI()
        app.include_router(router.api_speech_config_router)
        self.client = self.stack.enter_context(TestClient(app))

    def save(self, language, **extra):
        return self.client.post("/chat_config/speech_input", json={"language": language, **extra})

    def get(self):
        return self.client.get("/chat_config/speech_input").json()

    def test_default_read_does_not_write_or_expose_other_values(self):
        before = self.env_file.read_bytes()
        env_manager.env_vars["FAKE_API_KEY"] = "never-return-this-test-value"
        result = self.get()
        self.assertEqual(result["language"], "zh-CN")
        self.assertFalse(result["persisted"])
        self.assertEqual(result["env_name"], "SPEECH_INPUT_LANGUAGE")
        self.assertNotIn("never-return", str(result))
        self.assertEqual(before, self.env_file.read_bytes())

    def test_save_preserves_other_settings_and_survives_env_reload(self):
        response = self.save("en-US")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("# keep comment", text)
        self.assertIn('UNRELATED_KEY="keep"', text)
        self.assertIn('SPEECH_INPUT_LANGUAGE="en-US"', text)
        env_manager.env_vars = {}
        with patch("util.logger.setup_logging"):
            self.assertTrue(env_manager.reload_env_vars())
        self.assertEqual(self.get()["language"], "en-US")
        self.assertTrue(self.get()["persisted"])

    def test_repeat_save_replaces_only_target_key(self):
        self.save("ja-JP")
        self.save("fr-FR")
        text = self.env_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("SPEECH_INPUT_LANGUAGE="), 1)
        self.assertEqual(self.get()["language"], "fr-FR")

    def test_project_env_is_authoritative_over_models_runtime(self):
        env_manager.env_vars[router.ENV_KEY] = "de-DE"
        env_manager.setting_vars[router.ENV_KEY] = "en-US"
        self.assertEqual(self.get()["language"], "de-DE")

    def test_invalid_stored_value_falls_back_without_write(self):
        env_manager.env_vars[router.ENV_KEY] = "bad-language"
        before = self.env_file.read_bytes()
        self.assertEqual(self.get()["language"], "zh-CN")
        self.assertFalse(self.get()["persisted"])
        self.assertEqual(self.env_file.read_bytes(), before)

    def test_unknown_language_rejected_without_write(self):
        before = self.env_file.read_bytes()
        for language in ["", "English", "en-AU", 'en-US\nINJECT=value', '"en-US"']:
            with self.subTest(language=language):
                self.assertEqual(self.save(language).status_code, 400)
        self.assertEqual(self.env_file.read_bytes(), before)

    def test_strict_request_validation(self):
        for body in [{"language": 1}, {"language": None}, {"language": ["en-US"]},
                     {"language": "en-US", "only_if_unset": "false"},
                     {"language": "en-US", "unknown": True}, {}]:
            with self.subTest(body=body):
                response = self.client.post("/chat_config/speech_input", json=body)
                self.assertEqual(response.status_code, 422)

    def test_migration_only_fills_unset_backend(self):
        result = self.save("ja-JP", only_if_unset=True).json()
        self.assertTrue(result["updated"])
        before = self.env_file.read_bytes()
        result = self.save("en-US", only_if_unset=True).json()
        self.assertFalse(result["updated"])
        self.assertEqual(result["language"], "ja-JP")
        self.assertEqual(self.env_file.read_bytes(), before)

    def test_concurrent_legacy_migrations_have_one_winner(self):
        barrier = Barrier(2)

        def migrate(language):
            barrier.wait(timeout=5)
            return router.update_speech_input_config(
                router.SpeechInputRequest(language=language, only_if_unset=True)
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(migrate, ["fr-FR", "de-DE"]))
        self.assertEqual(sum(result["updated"] for result in results), 1)
        self.assertEqual(results[0]["language"], results[1]["language"])
        self.assertEqual(self.get()["language"], results[0]["language"])

    def test_disk_error_does_not_leave_false_in_memory_success(self):
        for previous in [None, "ja-JP"]:
            with self.subTest(previous=previous):
                if previous:
                    env_manager.env_vars[router.ENV_KEY] = previous
                else:
                    env_manager.env_vars.pop(router.ENV_KEY, None)
                before = self.env_file.read_bytes()
                with patch.object(env_manager, "_persist_env_values", side_effect=PermissionError("test only")):
                    self.assertEqual(self.save("en-US").status_code, 500)
                self.assertEqual(self.get()["language"], previous or "zh-CN")
                self.assertEqual(self.get()["persisted"], previous is not None)
                self.assertEqual(self.env_file.read_bytes(), before)
                self.assertEqual(env_manager.env_vars["UNRELATED_KEY"], "keep")

    def test_missing_env_initialization_returns_unavailable(self):
        with patch.object(env_manager, "env_file", None):
            self.assertEqual(self.save("en-US").status_code, 503)

    def test_browser_and_backend_language_catalogs_agree(self):
        source = (Path(__file__).resolve().parents[1] / "H5/js/speech_language.js").read_text(encoding="utf-8")
        codes = re.findall(r'code:\s*"([a-z]{2}-[A-Z]{2})"', source)
        self.assertEqual(set(codes), set(router.SUPPORTED_LANGUAGES))
        self.assertEqual(len(codes), len(router.SUPPORTED_LANGUAGES))


if __name__ == "__main__":
    unittest.main()
