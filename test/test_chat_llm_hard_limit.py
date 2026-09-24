"""请求硬限制预检（input + max_tokens ≤ 供应商上限）与重试间隔测试：

背景：opencode.ai 网关（orcarouter 上游）实测 input_tokens + max_tokens > 1048576
（2^20）时返回 HTTP 400 且响应体为空（text/event-stream），属确定性失败；
旧行为对该失败无间隔重试 NETWORK_RETRY_MAX_ATTEMPTS 次（可配置 50）造成长时间卡死。

覆盖：
- _estimate_payload_input_tokens：文本/多模态/工具定义估算口径；
- _precheck_hard_limit：通过 / 自动降级 max_tokens / 不可挽救拦截 / 禁用（<=0）；
- 流式路径：超限请求直接发 hard_limit 错误帧（retrying=False）且不发起网络请求；
- 重试间隔常量存在且为 1 秒（固定、不做配置）。

运行：python test\\test_chat_llm_hard_limit.py
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat import chat_llm as chat_llm_mod  # noqa: E402
from chat.chat_llm import ChatLLM  # noqa: E402
from config import ChatLLMRequest  # noqa: E402


class EstimatePayloadTokensTests(unittest.TestCase):
    def test_ascii_text_quarter_rate(self):
        payload = {"messages": [{"role": "user", "content": "a" * 400}]}
        # ASCII 400 字符 → 100 tokens
        self.assertEqual(chat_llm_mod._estimate_payload_input_tokens(payload), 100)

    def test_non_ascii_one_per_char(self):
        payload = {"messages": [{"role": "user", "content": "填" * 100}]}
        self.assertEqual(chat_llm_mod._estimate_payload_input_tokens(payload), 100)

    def test_multimodal_part_placeholder(self):
        # base64 数据按固定占位计费：不应因 5MB 字符串虚高到百万 token
        payload = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5_000_000}},
        ]}]}
        tokens = chat_llm_mod._estimate_payload_input_tokens(payload)
        self.assertLess(tokens, 10_000)
        self.assertGreaterEqual(tokens, chat_llm_mod._MULTIMODAL_PART_PLACEHOLDER_TOKENS)

    def test_tools_and_tool_calls_counted(self):
        payload = {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            ],
            "tools": [{"type": "function", "function": {"name": "f", "description": "d" * 400}}],
        }
        self.assertGreater(chat_llm_mod._estimate_payload_input_tokens(payload), 100)


class PrecheckHardLimitTests(unittest.TestCase):
    def setUp(self):
        import env_manager
        self._original_load_var = chat_llm_mod.load_var
        self._env_manager = env_manager
        env_manager.env_vars.pop("REQUEST_HARD_LIMIT_TOKENS", None)

    def tearDown(self):
        chat_llm_mod.load_var = self._original_load_var
        self._env_manager.env_vars.pop("REQUEST_HARD_LIMIT_TOKENS", None)

    def _payload(self, content_chars, max_tokens):
        return {
            "model": "m",
            "messages": [{"role": "user", "content": "填" * content_chars}],
            "max_tokens": max_tokens,
        }

    def _patch_limit(self, value):
        """按测试文件既有模式 patch load_var（未 init_path 时默认值优先）。"""
        chat_llm_mod.load_var = lambda name, default=None: (
            value if name == "REQUEST_HARD_LIMIT_TOKENS" else default
        )

    def test_small_request_passes(self):
        payload = self._payload(100, 1000)
        self.assertIsNone(ChatLLM._precheck_hard_limit(payload))
        self.assertEqual(payload["max_tokens"], 1000)

    def test_downgrades_max_tokens(self):
        # 输入 800k + max_tokens 500k > 1048576 → 降级到 1048576 - 800000 - 512
        payload = self._payload(800_000, 500_000)
        self.assertIsNone(ChatLLM._precheck_hard_limit(payload))
        expected = 1048576 - 800_000 - chat_llm_mod._HARD_LIMIT_SAFETY_MARGIN_TOKENS
        self.assertEqual(payload["max_tokens"], expected)

    def test_unsalvageable_input_returns_error(self):
        # 输入本身超限：即使 max_tokens 最小也无法发送 → 返回说明文本
        payload = self._payload(1_200_000, 64)
        message = ChatLLM._precheck_hard_limit(payload)
        self.assertIsNotNone(message)
        self.assertIn("硬限制", message)

    def test_min_max_tokens_guard(self):
        # 输入 1048000 + max_tokens 1000 超限，headroom = 64 < 1024 → 不可挽救
        payload = self._payload(1_048_000, 1000)
        self.assertIsNotNone(ChatLLM._precheck_hard_limit(payload))

    def test_disabled_with_negative(self):
        self._patch_limit("-1")
        payload = self._payload(2_000_000, 500_000)
        self.assertIsNone(ChatLLM._precheck_hard_limit(payload))

    def test_custom_limit_from_env(self):
        self._patch_limit("200000")
        payload = self._payload(100_000, 500_000)
        # 100000 + 500000 > 200000 → 降级到 200000 - 100000 - 512
        self.assertIsNone(ChatLLM._precheck_hard_limit(payload))
        self.assertEqual(payload["max_tokens"], 200000 - 100_000 - 512)


class StreamPrecheckTests(unittest.TestCase):
    """流式路径：超限请求直接失败（hard_limit 帧），不发网络请求。"""

    def setUp(self):
        import env_manager
        self._env_manager = env_manager
        env_manager.env_vars.pop("REQUEST_HARD_LIMIT_TOKENS", None)
        self._original_open = ChatLLM._open_connection
        self.opened = []

        async def _should_not_open(*args, **kwargs):
            self.opened.append(1)
            raise AssertionError("超限请求不应发起网络连接")

        ChatLLM._open_connection = staticmethod(_should_not_open)

    def tearDown(self):
        ChatLLM._open_connection = self._original_open
        self._env_manager.env_vars.pop("REQUEST_HARD_LIMIT_TOKENS", None)

    def test_oversized_request_fails_fast_without_network(self):
        request = ChatLLMRequest(
            messages=[{"role": "user", "content": "填" * 1_200_000}],
            max_tokens=64,
        )
        model_config = {
            "url": "http://upstream.test/v1",
            "apiKey": "k",
            "apiType": "chat-completions",
            "selected_model_id": "m",
        }

        async def run():
            frames = []
            async for frame in ChatLLM.async_std_completions_sse(
                request=request, model_config=model_config
            ):
                frames.append(frame)
            return frames

        frames = asyncio.run(run())
        self.assertEqual(len(self.opened), 0)
        error_frames = [f for f in frames if '"error"' in f]
        self.assertTrue(error_frames)
        payload = json.loads(error_frames[0][len("data: "):].strip())
        self.assertEqual(payload["error_type"], "hard_limit")
        self.assertFalse(payload["retrying"])
        self.assertTrue(any("[DONE]" in f for f in frames))


class RetryIntervalTests(unittest.TestCase):
    def test_interval_constant_is_one_second(self):
        self.assertEqual(chat_llm_mod._NETWORK_RETRY_INTERVAL_SECONDS, 1.0)

    def test_hard_limit_default_constant(self):
        from config import DEFAULT_REQUEST_HARD_LIMIT_TOKENS
        self.assertEqual(DEFAULT_REQUEST_HARD_LIMIT_TOKENS, 1048576)


if __name__ == "__main__":
    unittest.main(verbosity=2)
