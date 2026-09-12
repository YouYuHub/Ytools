"""聊天 SSE 心跳保活验证：生成任务长时间无增量输出时消费流出现 ": ping" 注释帧。

回归背景：tool_chat_server 消费循环原先 await stream.cond.wait() 无超时、
无心跳——模型长思考/慢工具期间 SSE 连接静默，nginx 等代理默认 60s 无数据
即断连、浏览器也可能把静默连接判死，用户端表现为任务被错误终止。
现与手动压缩流一致：无事件时每 _SSE_HEARTBEAT_SECONDS 发一帧 SSE 注释，
前端 readSseResponse 只解析 "data:" 行，注释帧自动忽略。
"""
import asyncio
import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factory.chat_factory as cf
from config import ChatLLMRequest
from memory.chat_memory import (
    ChatMemoryManager,
    cleanup_chat_memory_manager,
)


class SlowFakeLLM:
    """两次增量之间停顿 0.3s 的桩模型（模拟模型长思考/网关缓冲）。"""

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        for i in range(3):
            yield f"data: {json.dumps({'content': f'块{i}'})}\n\n"
            await asyncio.sleep(0.3)
        yield (
            "data: "
            + json.dumps({"finish_reason": "stop", "usage": {"total_tokens": 9}})
            + "\n\n"
        )
        yield "data: [DONE]\n\n"


class ChatSseHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 生成循环默认跑在独立 worker 进程（无法继承本测试的 monkeypatch），
        # 切回 inline 模式让桩生效
        cf._CHAT_WORKER_MODE = "inline"
        self._orig_chat = cf.ChatLLM.chat_completions
        self._orig_heartbeat = cf._SSE_HEARTBEAT_SECONDS
        cf.ChatLLM.chat_completions = SlowFakeLLM.chat_completions
        cf._SSE_HEARTBEAT_SECONDS = 0.05
        cf.load_all_tools = lambda: asyncio.sleep(0)
        cf.tool_registry.ALL_TOOLS = []
        cf.tool_registry.TOOL_MCP_SERVERS = {}
        cf.require_default_chat_config = lambda: None
        cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)

    def tearDown(self):
        cf.ChatLLM.chat_completions = self._orig_chat
        cf._SSE_HEARTBEAT_SECONDS = self._orig_heartbeat

    async def test_ping_frames_appear_during_pause(self):
        sid = f"mock_hb_{uuid.uuid4().hex}"
        try:
            req = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "hi"}])
            chunks = []
            async for chunk in cf.tool_chat_server(req):
                chunks.append(chunk)
            text = "".join(chunks)
            # 内容完整、正常收尾
            self.assertIn("块0", text)
            self.assertIn("块2", text)
            self.assertIn("[DONE]", text)
            # 0.3s 停顿期间（心跳 0.05s）应出现多帧 ping 注释
            self.assertGreaterEqual(text.count(": ping"), 1, "长停顿期间应出现心跳注释帧")
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)


if __name__ == "__main__":
    unittest.main()
