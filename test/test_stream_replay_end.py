"""附接回放结束标记：replay marker 配对 replay_end，创建者/已完成流不发。

回归背景：恢复/刷新附接时，流 buffer 会重放历史已渲染过的正文与工具
调用，前端无法区分回放帧与实时帧，只能全部新建节点，导致重复显示。
后端在回放耗尽后补一帧 {"replay_end": true}，前端据此关闭回放去重窗口。
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


class ReplayEndFakeLLM:
    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        for i in range(4):
            yield f"data: {json.dumps({'content': f'块{i}'})}\n\n"
            await asyncio.sleep(0.05)
        yield "data: " + json.dumps({"finish_reason": "stop", "usage": {"total_tokens": 9}}) + "\n\n"
        yield "data: [DONE]\n\n"


def frames(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                out.append({"done": True})
                continue
            try:
                out.append(json.loads(payload))
            except ValueError:
                continue
    return out


class StreamReplayEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = {}
        for name in ("_CHAT_WORKER_MODE", "load_all_tools", "require_default_chat_config",
                     "compact_session_history_if_needed", "resolve_model_max_input_tokens"):
            self._orig[name] = getattr(cf, name)
        self._orig["chat_completions"] = cf.ChatLLM.chat_completions
        self._orig["all_tools"] = cf.tool_registry.ALL_TOOLS
        self._orig["servers"] = cf.tool_registry.TOOL_MCP_SERVERS
        cf._CHAT_WORKER_MODE = "inline"
        cf.ChatLLM.chat_completions = ReplayEndFakeLLM.chat_completions
        cf.load_all_tools = lambda: asyncio.sleep(0)
        cf.tool_registry.ALL_TOOLS = []
        cf.tool_registry.TOOL_MCP_SERVERS = {}
        cf.require_default_chat_config = lambda: None
        cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)
        cf.resolve_model_max_input_tokens = lambda default=8192: 1000000

    def tearDown(self):
        cf._CHAT_WORKER_MODE = self._orig["_CHAT_WORKER_MODE"]
        cf.ChatLLM.chat_completions = self._orig["chat_completions"]
        cf.load_all_tools = self._orig["load_all_tools"]
        cf.require_default_chat_config = self._orig["require_default_chat_config"]
        cf.compact_session_history_if_needed = self._orig["compact_session_history_if_needed"]
        cf.resolve_model_max_input_tokens = self._orig["resolve_model_max_input_tokens"]
        cf.tool_registry.ALL_TOOLS = self._orig["all_tools"]
        cf.tool_registry.TOOL_MCP_SERVERS = self._orig["servers"]

    async def _collect(self, gen, count=None):
        out = []
        async for chunk in gen:
            out.append(chunk)
            if count is not None and len(out) >= count:
                break
        return out

    async def test_attach_gets_paired_replay_markers(self):
        sid = f"mock_replay_end_{uuid.uuid4().hex}"
        try:
            req = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "hi"}])
            await self._collect(cf.tool_chat_server(req), count=1)
            attached = await self._collect(cf.tool_chat_server(
                ChatLLMRequest(session_id=sid, messages=[])))
            parsed = frames(attached)
            self.assertTrue(parsed and parsed[0].get("replay") is True)
            self.assertTrue(parsed[0].get("replay_end_supported") is True)
            if "round" in parsed[0]:
                self.assertIn("round_usage_total", parsed[0])
            ends = [i for i, frame in enumerate(parsed) if frame.get("replay_end") is True]
            self.assertEqual(len(ends), 1, "附接流应恰好有一帧 replay_end")
            dones = [i for i, frame in enumerate(parsed) if frame.get("done")]
            self.assertEqual(dones, [len(parsed) - 1])
            self.assertLess(ends[0], dones[0])
            self.assertTrue(any("块" in json.dumps(frame, ensure_ascii=False) for frame in parsed))
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_creator_and_finished_stream_have_no_replay_markers(self):
        sid = f"mock_replay_end_{uuid.uuid4().hex}"
        try:
            req = ChatLLMRequest(session_id=sid, messages=[{"role": "user", "content": "hi"}])
            created = frames(await self._collect(cf.tool_chat_server(req)))
            self.assertFalse(any(frame.get("replay") or frame.get("replay_end") for frame in created))
            self.assertIn({"round_usage_total": {"total_tokens": 9}}, created)
            attached = frames(await self._collect(cf.tool_chat_server(
                ChatLLMRequest(session_id=sid, messages=[]))))
            self.assertFalse(any(frame.get("replay") or frame.get("replay_end") for frame in attached))
            self.assertTrue(attached and attached[-1].get("done") is True)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_attach_marker_contains_latest_round_usage_when_buffer_is_empty(self):
        sid = f"mock_replay_usage_{uuid.uuid4().hex}"
        stream = cf._set_session_stream(sid)
        stream.live_round_no = 2
        stream.live_round_usage_total = {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}
        stream.task = asyncio.create_task(asyncio.sleep(10))
        try:
            chunks = await self._collect(cf.tool_chat_server(
                ChatLLMRequest(session_id=sid, messages=[])), count=1)
            marker = frames(chunks)[0]
            self.assertEqual(marker["round"], 2)
            self.assertEqual(marker["round_usage_total"]["total_tokens"], 9)
            stream.live_round_usage_total = None
            empty_chunks = await self._collect(cf.tool_chat_server(
                ChatLLMRequest(session_id=sid, messages=[])), count=1)
            self.assertEqual(frames(empty_chunks)[0]["round_usage_total"], {})
        finally:
            stream.task.cancel()
            await asyncio.gather(stream.task, return_exceptions=True)
            cf._SESSION_STREAMS.pop(sid, None)


if __name__ == "__main__":
    unittest.main()
