import asyncio
import unittest

from routers.chat_router import get_chat_work_dir_config


class ChatRouterTests(unittest.TestCase):
    def test_work_dir_status_endpoint_is_read_only_and_uses_project_env(self):
        response = asyncio.run(get_chat_work_dir_config())
        self.assertEqual(response.status_code, 200)
        payload = response.body.decode("utf-8")
        self.assertIn('"read_only":true', payload)
        self.assertIn('"source":"project .env"', payload)
        self.assertIn('"env_name":"CHAT_WORK_DIR"', payload)
        self.assertIn('"env_value":', payload)
        self.assertIn('"env_file":', payload)


if __name__ == "__main__":
    unittest.main()