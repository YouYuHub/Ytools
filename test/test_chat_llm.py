import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import init_path


class ChatLLMConfigTests(unittest.TestCase):
    def _initialize_selected_model_config(self, temp_path: Path) -> None:
        (temp_path / "setting").mkdir(parents=True, exist_ok=True)
        (temp_path / ".env").write_text(
            "CHAT_OWNERSHIP_NANE=Selected Provider\nCHAT_MODEL_NAME=selected-model\n",
            encoding="utf-8",
        )
        (temp_path / "setting" / "models.json").write_text(
            json.dumps({
                "Selected Provider": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-api-key",
                    "apiType": "chat-completions",
                    "models": {
                        "selected-model": {
                            "id": "selected-model",
                            "url": "https://selected.example.test/v1",
                            "maxInputTokens": 123456,
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        init_path(str(temp_path))

    def test_selected_models_json_config_overrides_legacy_request_fields(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                self._initialize_selected_model_config(Path(temp_dir))
                request = ChatLLMRequest(
                    api_url="https://request.example.test/v1",
                    model="request-model",
                    messages=[{"role": "user", "content": "你好"}],
                    extra_body={"model": "extra-body-model", "custom_option": True},
                )

                chat_config = ChatLLM._resolve_active_chat_config()
                payload = ChatLLM._build_request_payload(
                    request,
                    stream=False,
                    model_name=chat_config["selected_model_name"],
                )

                self.assertEqual(chat_config["url"], "https://selected.example.test/v1")
                self.assertEqual(payload["model"], "selected-model")
                self.assertTrue(payload["custom_option"])
                self.assertNotIn("api_url", request.model_dump())
                self.assertNotIn("model", request.model_dump())
            finally:
                init_path(str(repo_root))

    def test_async_https_connection_uses_request_connect_timeout_for_tls_handshake(self):
        async def run_case():
            open_connection = AsyncMock(return_value=(object(), object()))
            with patch("chat.chat_llm.asyncio.open_connection", new=open_connection):
                reader, writer = await ChatLLM._open_connection(
                    "example.test",
                    443,
                    use_ssl=True,
                    timeout_connect=300,
                )

            self.assertIsNotNone(reader)
            self.assertIsNotNone(writer)
            self.assertEqual(open_connection.await_args.args, ("example.test", 443))
            self.assertEqual(open_connection.await_args.kwargs["server_hostname"], "example.test")
            self.assertEqual(open_connection.await_args.kwargs["ssl_handshake_timeout"], 300)
            self.assertIsNotNone(open_connection.await_args.kwargs["ssl"])

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
