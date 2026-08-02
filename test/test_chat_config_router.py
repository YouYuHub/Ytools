import json
import tempfile
import unittest
from pathlib import Path

from env_manager import init_path


class ChatConfigModelTests(unittest.IsolatedAsyncioTestCase):
    """为配置路由测试提供独立的临时 .env / models.json，避免污染项目真实配置。

    为了让被测代码中的 `init_path(PROJECT_ROOT)` 在测试期间指向临时目录，
    我们临时把 config.PROJECT_ROOT 替换为临时目录。
    """

    async def asyncSetUp(self) -> None:
        from config import PROJECT_ROOT as original_root
        from routers import chat_config_router
        self.router = chat_config_router

        self._repo_root = original_root
        self._temp_dir = tempfile.TemporaryDirectory()
        self._temp_path = Path(self._temp_dir.name)
        (self._temp_path / "setting").mkdir(parents=True, exist_ok=True)
        (self._temp_path / ".env").write_text(
            "CHAT_OWNERSHIP_NANE=OpenCode Completions\n"
            "CHAT_MODEL_NAME=deepseek-v4-pro\n",
            encoding="utf-8",
        )
        (self._temp_path / "setting" / "models.json").write_text(
            json.dumps({
                "OpenCode Completions": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-key",
                    "apiType": "chat-completions",
                    "models": {
                        "deepseek-v4-pro": {
                            "id": "deepseek-v4-pro",
                            "url": "https://example.test/v1",
                            "toolCalling": True,
                            "maxInputTokens": 888888,
                        },
                        "deepseek-v4-flash": {
                            "id": "deepseek-v4-flash",
                            "url": "https://example.test/v1",
                            "toolCalling": True,
                            "maxInputTokens": 428000,
                        },
                    },
                },
            }),
            encoding="utf-8",
        )

        import config
        config.PROJECT_ROOT = self._temp_path
        # 重新加载 router 模块使其重新读取 PROJECT_ROOT
        import importlib
        importlib.reload(chat_config_router)
        self.router = chat_config_router
        init_path(self._temp_path)

    async def asyncTearDown(self) -> None:
        import config
        config.PROJECT_ROOT = self._repo_root
        # 恢复 project 模块缓存中的 router 引用
        from routers import chat_config_router
        import importlib
        importlib.reload(chat_config_router)
        init_path(self._repo_root)
        self._temp_dir.cleanup()

    async def test_list_chat_models_returns_providers_and_current(self) -> None:
        response = await self.router.list_chat_models()
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('"state":"succeed"', body)
        self.assertIn('"provider_name":"OpenCode Completions"', body)
        self.assertIn('"model_name":"deepseek-v4-pro"', body)
        self.assertIn('"model_name":"deepseek-v4-flash"', body)
        self.assertIn('"provider":"OpenCode Completions"', body)
        self.assertIn('"model":"deepseek-v4-pro"', body)
        self.assertIn('"api_key_present":true', body)
        self.assertIn('"tool_calling":true', body)

    async def test_select_active_chat_model_updates_env(self) -> None:
        response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="deepseek-v4-flash",
            )
        )
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('"state":"succeed"', body)
        self.assertIn('"model":"deepseek-v4-flash"', body)

        # .env 应该已经被更新（写入到了临时目录）
        env_file = self._temp_path / ".env"
        content = env_file.read_text(encoding="utf-8")
        self.assertIn("CHAT_OWNERSHIP_NANE=", content)
        self.assertIn('CHAT_MODEL_NAME="deepseek-v4-flash"', content)

    async def test_select_unknown_model_returns_400(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.router.select_active_chat_model(
                self.router.ChatModelSelection(
                    provider="OpenCode Completions",
                    model="nonexistent-model",
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
