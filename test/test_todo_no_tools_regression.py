"""回归测试：未传 tool_names（无工具模式，含关闭 todo）时生成流程可正常走到模型调用。

曾因 requested_names 未初始化在无工具分支触发 NameError，后台任务静默死亡，
前端表现为"卡住、消息永远到不了模型服务"。
"""
import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ChatLLMRequest
from factory import chat_factory

TEST_SESSION = "todo_no_tools_ut"


class _FakeStream:
    """最小 _SessionStream 桩：捕获 emit 的事件。"""

    def __init__(self):
        self.chunks = []
        self.seq = 0
        self.done = False
        self.question_text = ""
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


class _FakeFileMemory:
    def get_file_memory_chat(self):
        return ""

    def get_file_memory_text(self, *args, **kwargs):
        return ""


class _FakeLLM:
    """最小模型桩：返回固定的流式回复帧，不发起真实网络请求。"""

    @staticmethod
    def chat_completions(*, request=None, stream=True, model_config=None, **_kwargs):
        async def gen():
            yield 'data: {"content": "你好！"}\n\n'
            yield "data: [DONE]\n\n"
        return gen()


async def _run_case(tool_names):
    chat_factory.cleanup_chat_memory_manager(TEST_SESSION)
    chat_factory.cleanup_file_memory_manager(TEST_SESSION)
    memory = _MemoryStub()
    stream = _FakeStream()
    request = ChatLLMRequest(
        messages=[{"role": "user", "content": "你好"}],
        session_id=TEST_SESSION,
        tool_names=tool_names,
    )

    async def fake_load_all_tools():
        return None

    async def fake_chat_completions(*, request=None, stream=True, model_config=None, **_kwargs):
        async def gen():
            yield 'data: {"content": "你好！"}\n\n'
            yield "data: [DONE]\n\n"
        return gen()

    from factory.agent_runtime import tool_registry

    fake_config = {
        "selected_provider_name": "TestProvider",
        "selected_model_name": "TestModel",
        "selected_model_id": "test-model",
        "apiType": "chat-completions",
        "maxInputTokens": 128000,
    }
    with patch.object(chat_factory, "load_all_tools", fake_load_all_tools), \
            patch.object(chat_factory, "require_default_chat_config", lambda: fake_config), \
            patch.object(chat_factory, "ChatLLM", _FakeLLM), \
            patch.object(chat_factory, "get_chat_memory_manager", get_manager), \
            patch.object(chat_factory, "get_file_memory_manager", get_file_memory), \
            patch.object(tool_registry, "ALL_TOOLS", []), \
            patch.object(tool_registry, "TOOL_MCP_SERVERS", {}):
        await chat_factory._run_chat_generation(request, stream)
    return stream


_last_memory = None


async def get_manager(_session_id):
    global _last_memory
    _last_memory = _MemoryStub()
    return _last_memory


async def get_file_memory(_session_id):
    return _FakeFileMemory()


class NoToolNamesRegressionTests(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def test_no_tool_names_reaches_model_without_name_error(self):
        stream = asyncio.run(_run_case(None))
        joined = "".join(stream.chunks)
        # 到达模型并收到回复（NameError 会让任务静默死亡、无任何内容帧）
        self.assertIn("你好！", joined)
        # 轮次正常收尾（done 事件写入历史；SSE [DONE] 帧由外层消费者追加）
        self.assertIn("round_finalized_done", _last_memory.records)

    def test_empty_tool_names_reaches_model(self):
        stream = asyncio.run(_run_case([]))
        joined = "".join(stream.chunks)
        self.assertIn("你好！", joined)


def _cleanup():
    root = Path(__file__).resolve().parents[1] / "history_files"
    for suffix in ("", ".pending"):
        target = root / f"{TEST_SESSION}_chat.jsonl{suffix}"
        if target.exists():
            target.unlink()
    upload_dir = root / TEST_SESSION
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)
    try:
        asyncio.run(chat_factory.cleanup_chat_memory_manager(TEST_SESSION))
    except Exception:
        pass
    try:
        asyncio.run(chat_factory.cleanup_file_memory_manager(TEST_SESSION))
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
