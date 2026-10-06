import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import env_manager
from util.ollama_model_watcher import (
    DEFAULT_OLLAMA_CHAT_URL,
    OllamaModelWatcher,
    _ollama_api_url,
)


class OllamaModelWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "setting").mkdir()
        (self.root / ".env").write_text("", encoding="utf-8")
        self.write_config({})
        env_manager.clear_ollama_runtime_models()
        env_manager.init_path(str(self.root))
        self.inventory = []
        self.capabilities = {}
        self.show_failures = set()
        self.calls = []
        self.watcher = OllamaModelWatcher(request_json=self.fake_request)

    def tearDown(self):
        self.watcher.reset_for_tests()
        env_manager.clear_ollama_runtime_models()
        env_manager.init_path(str(ROOT))
        self.temp_dir.cleanup()

    def write_config(self, config):
        (self.root / "setting" / "models.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

    def fake_request(self, url, *, method="GET", payload=None, api_key=None, timeout=3):
        self.calls.append((url, method, payload, api_key, timeout))
        if url.endswith("/api/tags"):
            return {"models": list(self.inventory)}
        if url.endswith("/api/show") and method == "POST":
            model_id = payload["model"]
            if model_id in self.show_failures:
                raise URLError("local Ollama unavailable")
            return {"capabilities": list(self.capabilities.get(model_id, []))}
        raise AssertionError(f"Unexpected Ollama request: {method} {url}")

    def configure_ollama(self, *, url="http://ollama.example:11434/v1", models=None):
        self.write_config({
            "Ollama": {
                "vendor": "ollama",
                "url": url,
                "models": models or {},
            }
        })
        env_manager.init_path(str(self.root))

    def test_uses_local_default_when_not_configured_and_configured_url_when_present(self):
        targets = env_manager.get_ollama_provider_configs()
        self.assertEqual(targets[0]["name"], "Ollama")
        self.assertEqual(targets[0]["url"], DEFAULT_OLLAMA_CHAT_URL)

        configured_url = "http://ollama.example:11434/v1"
        self.configure_ollama(url=configured_url)
        targets = env_manager.get_ollama_provider_configs()
        self.assertEqual(targets[0]["url"], configured_url)

    def test_discovers_models_capabilities_and_survives_models_json_reload(self):
        configured_url = "http://ollama.example:11434/v1"
        self.configure_ollama(url=configured_url)
        self.inventory = [
            {"name": "llava:latest", "digest": "vision-digest"},
            {"model": "qwen:7b", "digest": "tools-digest"},
        ]
        self.capabilities = {
            "llava:latest": ["completion", "vision"],
            "qwen:7b": ["completion", "tools"],
        }

        actions = self.watcher.poll_once()

        self.assertEqual(actions, {"Ollama": "updated"})
        self.assertEqual(self.calls[0][0], "http://ollama.example:11434/api/tags")
        available = {item["model_name"]: item for item in env_manager.list_available_models()}
        self.assertTrue(available["llava:latest"]["vision"])
        self.assertTrue(available["qwen:7b"]["tool_calling"])
        self.assertEqual(available["llava:latest"]["url"], configured_url)
        self.assertTrue(env_manager.get_model_config("Ollama", "llava:latest")["vision"])
        _, api_type = env_manager._resolve_selection_target("Ollama", "qwen:7b", "chat_model")
        self.assertEqual(api_type, "chat_completions")

        # GET /chat_config/models 会重读磁盘 models.json；动态列表仍由独立快照提供。
        env_manager.init_path(str(self.root))
        self.assertIn("llava:latest", {
            item["model_name"] for item in env_manager.list_available_models()
        })
        self.assertEqual(env_manager.models_config["Ollama"]["models"], {})

    def test_default_local_provider_is_added_to_model_list_after_successful_scan(self):
        self.inventory = [{"name": "llama3:latest", "digest": "llama-digest"}]
        self.capabilities = {"llama3:latest": ["completion"]}

        self.watcher.poll_once()

        model = env_manager.list_available_models()[0]
        self.assertEqual(model["provider_name"], "Ollama")
        self.assertEqual(model["url"], DEFAULT_OLLAMA_CHAT_URL)
        self.assertEqual(
            env_manager.get_model_config("Ollama", "llama3:latest")["selected_model_id"],
            "llama3:latest",
        )

    def test_runtime_snapshot_is_transferred_to_spawned_worker_catalog(self):
        configured_url = "http://ollama.example:11434/v1"
        self.configure_ollama(url=configured_url)
        env_manager.set_ollama_runtime_models(
            "Ollama",
            configured_url,
            [{"id": "qwen3.5:9b", "name": "qwen3.5:9b", "vision": False}],
        )
        snapshots = env_manager.get_ollama_runtime_model_snapshots()
        self.assertEqual(snapshots[0]["provider_name"], "Ollama")
        self.assertEqual(snapshots[0]["provider_url"], configured_url)

        from factory.session_worker import _apply_runtime_ollama_model_snapshots

        env_manager.clear_ollama_runtime_models()
        _apply_runtime_ollama_model_snapshots(snapshots)

        config = env_manager.get_model_config("Ollama", "qwen3.5:9b")
        self.assertIsNotNone(config)
        self.assertEqual(config["selected_model_id"], "qwen3.5:9b")

        from memory import chat_memory

        global_selection = {"chat_model": {
            "ownership_name": "Fallback",
            "model_name": "static-model",
            "parameter": {},
        }}
        with patch.object(chat_memory, "_get_global_model_selection", return_value=global_selection), \
                patch.object(
                    chat_memory,
                    "read_session_meta_value",
                    return_value={"chat_model": {
                        "ownership_name": "Ollama",
                        "model_name": "qwen3.5:9b",
                    }},
                ):
            selection, warnings = chat_memory.resolve_session_model_selection("worker-snapshot-test")
        self.assertEqual(selection["chat_model"]["model_name"], "qwen3.5:9b")
        self.assertEqual(warnings, [])

    def test_start_generation_includes_parent_runtime_ollama_snapshot(self):
        from factory.session_worker import SessionWorkerProxy

        proxy = SessionWorkerProxy("ollama-snapshot-test")
        sent = []
        snapshot = [{
            "provider_name": "Ollama",
            "provider_url": DEFAULT_OLLAMA_CHAT_URL,
            "models": [{"id": "qwen3.5:9b", "name": "qwen3.5:9b"}],
        }]
        with patch.object(proxy, "_ensure_process"), \
                patch.object(proxy, "_send", side_effect=lambda payload: sent.append(payload) or True), \
                patch.object(env_manager, "get_ollama_runtime_model_snapshots", return_value=snapshot):
            self.assertTrue(proxy.start_generation({"session_id": "ollama-snapshot-test"}))

        self.assertEqual(sent[0]["type"], "generate")
        self.assertEqual(sent[0]["runtime_ollama_models"], snapshot)

    def test_unchanged_inventory_and_order_do_not_requery_model_details(self):
        self.configure_ollama()
        self.inventory = [
            {"name": "model-b:latest", "digest": "b"},
            {"name": "model-a:latest", "digest": "a"},
        ]
        self.capabilities = {
            "model-a:latest": ["completion"],
            "model-b:latest": ["completion"],
        }

        first = self.watcher.poll_once()
        first_show_count = sum(1 for call in self.calls if call[1] == "POST")
        self.inventory.reverse()
        second = self.watcher.poll_once()
        second_show_count = sum(1 for call in self.calls if call[1] == "POST")

        self.assertEqual(first, {"Ollama": "updated"})
        self.assertEqual(second, {"Ollama": "unchanged"})
        self.assertEqual(first_show_count, 2)
        self.assertEqual(second_show_count, first_show_count)

    def test_network_failure_keeps_last_good_snapshot_and_successful_empty_list_clears_it(self):
        self.configure_ollama()
        self.inventory = [{"name": "llama3:latest", "digest": "llama-digest"}]
        self.capabilities = {"llama3:latest": ["completion"]}
        self.watcher.poll_once()

        self.watcher._request_json = lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline"))
        self.assertEqual(self.watcher.poll_once(), {"Ollama": "failed"})
        self.assertEqual(len(env_manager.list_available_models()), 1)

        self.watcher._request_json = self.fake_request
        self.inventory = []
        self.assertEqual(self.watcher.poll_once(), {"Ollama": "updated"})
        self.assertEqual(env_manager.list_available_models(), [])

    def test_capability_failure_is_retried_without_removing_model(self):
        self.configure_ollama()
        self.inventory = [{"name": "llava:latest", "digest": "same-digest"}]
        self.show_failures.add("llava:latest")

        self.watcher.poll_once()
        first = env_manager.list_available_models()[0]
        self.assertFalse(first["vision"])

        self.show_failures.clear()
        self.capabilities["llava:latest"] = ["completion", "vision"]
        self.assertEqual(self.watcher.poll_once(), {"Ollama": "updated"})
        self.assertTrue(env_manager.list_available_models()[0]["vision"])

    def test_model_digest_change_updates_snapshot_even_when_model_id_is_unchanged(self):
        self.configure_ollama()
        self.inventory = [{"name": "llama3:latest", "digest": "first-digest"}]
        self.capabilities = {"llama3:latest": ["completion"]}
        self.watcher.poll_once()

        self.inventory[0]["digest"] = "second-digest"
        self.assertEqual(self.watcher.poll_once(), {"Ollama": "updated"})

    def test_configured_url_change_hides_old_snapshot_until_new_endpoint_is_scanned(self):
        self.configure_ollama(url="http://first.example:11434/v1")
        self.inventory = [{"name": "llama3:latest", "digest": "digest"}]
        self.capabilities = {"llama3:latest": ["completion"]}
        self.watcher.poll_once()
        self.assertEqual(len(env_manager.list_available_models()), 1)

        self.configure_ollama(url="http://second.example:11434/v1")
        self.assertEqual(env_manager.list_available_models(), [])
        self.watcher.poll_once()
        tags_urls = [call[0] for call in self.calls if call[0].endswith("/api/tags")]
        self.assertEqual(tags_urls[-1], "http://second.example:11434/api/tags")
        self.assertEqual(env_manager.list_available_models()[0]["url"], "http://second.example:11434/v1")

    def test_static_model_metadata_wins_and_dynamic_models_are_appended(self):
        self.configure_ollama(models={
            "Llava manual": {"id": "llava:latest", "vision": False, "toolCalling": False},
        })
        self.inventory = [
            {"name": "llava:latest", "digest": "a"},
            {"name": "qwen:7b", "digest": "b"},
        ]
        self.capabilities = {
            "llava:latest": ["completion", "vision"],
            "qwen:7b": ["completion", "tools"],
        }

        self.watcher.poll_once()

        records = env_manager.list_available_models()
        self.assertEqual(len(records), 2)
        manual = next(item for item in records if item["model_name"] == "Llava manual")
        self.assertFalse(manual["vision"])
        self.assertIn("qwen:7b", {item["model_name"] for item in records})

    def test_native_api_url_is_derived_from_configured_chat_url(self):
        self.assertEqual(
            _ollama_api_url("http://localhost:11434/v1", "tags"),
            "http://localhost:11434/api/tags",
        )
        self.assertEqual(
            _ollama_api_url("https://host.example/ollama/v1/", "show"),
            "https://host.example/ollama/api/show",
        )


if __name__ == "__main__":
    unittest.main()
