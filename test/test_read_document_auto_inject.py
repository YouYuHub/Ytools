# -*- coding: utf-8 -*-
"""端到端（无真实网络）：read_document 自动注入 + 文件清单注入。

覆盖场景：
- 会话存在上传文件且本轮携带工具 → 请求 tools 含 read_document，首条 system 含文件清单；
- 无文件 → 不注入 read_document、无清单；
- 无工具模式（tool_names=[]，显式空列表）→ 不注入 read_document，但清单仍注入（无读取提示）。
"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ChatLLMRequest
from factory import chat_factory
from factory.agent_runtime import tool_registry
from memory import file_memory

TEST_SESSION = "read_doc_auto_ut"


class _FakeStream:
    """最小 _SessionStream 桩：捕获 emit 的事件。"""

    def __init__(self):
        self.chunks = []
        self.seq = 0
        self.done = False
        self.question_text = ""
        self.question_parts = None
        self.live_round_no = None
        self.round_start_seq = 0

    def emit(self, chunk):
        self.chunks.append(chunk)
        self.seq += 1

    def notify(self):
        pass

    def set_round_start(self):
        self.round_start_seq = self.seq

    def is_running(self):
        return True

    async def finish(self):
        self.done = True


class _MemoryStub:
    def __init__(self):
        self.run_task = True
        self.records = []

    async def add_chat_history(self, record):
        self.records.append(record)
        if isinstance(record, dict) and record.get("done") == "[DONE]":
            self.records.append("round_finalized_done")
        return "记录成功"

    async def get_session_todo(self):
        return []

    async def get_context_summary(self):
        return None

    async def get_chat_history(self):
        return []

    async def update_session_todo(self, todos):
        return "记录成功"

    async def find_orphan_compaction_events(self):
        return []

    async def get_context_messages(self, max_rounds=None, **_kwargs):
        return []

    async def update_context_summary(self, summary):
        return "记录成功"

    def add_context_compaction_event(self, payload):
        return "记录成功"

    async def update_session_upload_id(self, upload_id):
        return "记录成功"


class ReadDocumentAutoInjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp_files = tempfile.mkdtemp()
        self._orig_file_root = file_memory.HISTORY_ROOT
        file_memory.HISTORY_ROOT = Path(self._tmp_files)
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        file_memory.HISTORY_ROOT = self._orig_file_root
        shutil.rmtree(self._tmp_files, ignore_errors=True)

    def _cleanup(self):
        try:
            asyncio.run(file_memory.cleanup_file_memory_manager(TEST_SESSION))
        except Exception:
            pass

    def _add_file(self, content):
        manager = asyncio.run(file_memory.get_file_memory_manager(TEST_SESSION))
        manager.add_file_memory({
            "filename": "说明.txt",
            "type": "txt",
            "content": content,
            "size": len(content),
        })

    def _run(self, tool_names):
        captured = {}

        async def get_manager(_session_id):
            return _MemoryStub()

        async def get_real_file_memory(session_id):
            return await file_memory.get_file_memory_manager(session_id)

        async def fake_load_all_tools():
            return None

        class _FakeLLM:
            @staticmethod
            def chat_completions(*, request=None, stream=True, model_config=None, **_kwargs):
                captured["tools"] = list(request.tools or [])
                captured["messages"] = list(request.messages or [])

                async def gen():
                    yield 'data: {"content": "你好！"}\n\n'
                    yield "data: [DONE]\n\n"

                return gen()

        fake_config = {
            "selected_provider_name": "TestProvider",
            "selected_model_name": "TestModel",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 128000,
        }

        async def run():
            stream = _FakeStream()
            request = ChatLLMRequest(
                messages=[{"role": "user", "content": "看看这个文件"}],
                session_id=TEST_SESSION,
                tool_names=tool_names,
            )
            with patch.object(chat_factory, "load_all_tools", fake_load_all_tools), \
                    patch.object(chat_factory, "require_default_chat_config", lambda: fake_config), \
                    patch.object(chat_factory, "ChatLLM", _FakeLLM), \
                    patch.object(chat_factory, "get_chat_memory_manager", get_manager), \
                    patch.object(chat_factory, "get_file_memory_manager", get_real_file_memory), \
                    patch.object(tool_registry, "ALL_TOOLS", []), \
                    patch.object(tool_registry, "TOOL_MCP_SERVERS", {}):
                await chat_factory._run_chat_generation(request, stream)
            return stream

        stream = asyncio.run(run())
        return stream, captured

    @staticmethod
    def _tool_names(captured):
        return [
            tool.get("function", {}).get("name")
            for tool in captured.get("tools", [])
        ]

    @staticmethod
    def _message_content(message):
        """兼容 pydantic Message 与 dict 两种形态。"""
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(getattr(message, "content", "") or "")

    def test_auto_injects_read_document_with_files(self):
        self._add_file("文件正文内容" * 30)
        stream, captured = self._run(["read_file"])
        self.assertIn("你好！", "".join(stream.chunks))
        names = self._tool_names(captured)
        self.assertIn("read_document", names)
        self.assertIn("read_file", names)
        system_text = self._message_content(captured["messages"][0])
        self.assertIn("用户上传了 1 个文件", system_text)
        self.assertIn("说明.txt", system_text)
        self.assertIn("文件正文内容", system_text)

    def test_no_files_no_read_document(self):
        stream, captured = self._run(["read_file"])
        self.assertIn("你好！", "".join(stream.chunks))
        names = self._tool_names(captured)
        self.assertNotIn("read_document", names)
        system_text = self._message_content(captured["messages"][0])
        self.assertNotIn("用户上传了", system_text)

    def test_no_tools_mode_keeps_manifest_without_hint(self):
        self._add_file("文件正文内容" * 30)
        # 显式空列表 = 无工具模式；传 None 会回退「会话覆盖 → 全局默认」工具选择
        # （当前全局配置非空，会带入工具），与主程序语义不符（见 chat_factory）
        stream, captured = self._run([])
        self.assertIn("你好！", "".join(stream.chunks))
        names = self._tool_names(captured)
        self.assertNotIn("read_document", names)
        system_text = self._message_content(captured["messages"][0])
        self.assertIn("用户上传了 1 个文件", system_text)
        # 无工具模式下清单不出现 read_document 读取提示
        self.assertNotIn("read_document", system_text)


if __name__ == "__main__":
    unittest.main()
