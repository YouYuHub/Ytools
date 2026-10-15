"""回归测试：未传 tool_names（无工具模式，含关闭 todo）时生成流程可正常走到模型调用。

曾因 requested_names 未初始化在无工具分支触发 NameError，后台任务静默死亡，
前端表现为"卡住、消息永远到不了模型服务"。
"""
import asyncio
import copy
from collections import deque
import base64
import tempfile
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
        # 与 _SessionStream 鸭子类型对齐：回放补气泡/轮次标记相关属性
        self.question_parts = None
        self.live_round_no = None
        self.live_round_model = None
        self.round_start_seq = 0
        self.injected_messages = deque()

    def pop_injected_message(self):
        return self.injected_messages.popleft() if self.injected_messages else None

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
        self.records = []
        self.todos = []

    async def add_chat_history(self, record=None, *, input_text=None):
        if input_text is not None:
            record = input_text
        self.records.append(record)
        if isinstance(record, dict) and record.get("done") == "[DONE]":
            self.records.append("round_finalized_done")
        return "记录成功"

    async def set_current_round_model(self, model):
        # 每轮生效聊天模型落盘（round_model 帧的同源写入）：最小桩仅接收
        return True

    async def get_session_todo(self):
        return copy.deepcopy(self.todos)

    async def get_context_summary(self):
        return None

    async def get_chat_history(self):
        return []

    async def update_session_todo(self, todos):
        self.todos = copy.deepcopy(todos)
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

    def count_file_memory(self):
        return 0

    def get_file_memory_manifest(self, **kwargs):
        return ""

    def get_file_memory_index(self, **kwargs):
        return ""


class _FakeLLM:
    """最小模型桩：返回固定的流式回复帧，不发起真实网络请求。"""

    @staticmethod
    def chat_completions(*, request=None, stream=True, model_config=None, **_kwargs):
        async def gen():
            yield 'data: {"content": "你好！"}\n\n'
            yield "data: [DONE]\n\n"
        return gen()


async def _run_case(tool_names, llm=_FakeLLM, stream=None, vision=False, request_override=None, resolve_config=None):
    await chat_factory.cleanup_chat_memory_manager(TEST_SESSION)
    await chat_factory.cleanup_file_memory_manager(TEST_SESSION)
    memory = _MemoryStub()
    stream = stream or _FakeStream()
    request = request_override or ChatLLMRequest(
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
        "vision": vision,
    }
    with patch.object(chat_factory, "load_all_tools", fake_load_all_tools), \
            patch.object(chat_factory, "require_default_chat_config", resolve_config or (lambda: fake_config)), \
            patch.object(chat_factory, "ChatLLM", llm), \
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

    def test_replace_todo_context_keeps_native_pair_without_plan_prompt(self):
        messages = [
            {"role": "system", "content": "基础提示"},
            {"role": "assistant", "tool_calls": [{
                "id": "todo_1", "function": {"name": "todo_write", "arguments": "{}"}
            }]},
            {"role": "tool", "tool_call_id": "todo_1", "_tool_name": "todo_write",
             "content": json.dumps({"todos": [{"id": "1", "content": "准备测试", "status": "done"}]})},
        ]
        chat_factory._replace_todo_context(messages, [])
        self.assertEqual(len(messages), 3)
        self.assertEqual([m["content"] for m in messages if m["role"] == "system"], ["基础提示"])
        self.assertEqual(messages[2]["tool_call_id"], "todo_1")


class InjectedMediaRegressionTests(unittest.TestCase):
    def test_image_and_quotes_enter_next_api_without_polluting_history(self):
        import memory.file_memory as file_memory
        for vision in (True, False):
            with self.subTest(vision=vision), tempfile.TemporaryDirectory() as directory:
                media = Path(directory) / "media"
                media.mkdir()
                (media / "steer.png").write_bytes(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZAAAAABJRU5ErkJggg=="))
                stream = _FakeStream()
                requests = []
                content = [{"type": "text", "text": "请根据这张图片继续"},
                           {"type": "image_url", "image_url": {"url": "media://steer.png"}}]
                quotes = [{"text": "引用原文"}]

                class LLM:
                    @staticmethod
                    def chat_completions(*, request, **kwargs):
                        requests.append(copy.deepcopy(request))
                        async def gen():
                            if len(requests) == 1:
                                stream.injected_messages.append({"role": "user", "content": copy.deepcopy(content),
                                                                  "quotes": quotes, "message_id": "image-steer"})
                                yield 'data: {"tool_calls":[{"index":0,"id":"r1","type":"function","function":{"name":"read_file","arguments":"{\\"path\\":\\"factory/agent_runtime/todo_runtime.py\\",\\"limit\\":1}"}}]}\n\n'
                                yield 'data: {"finish_reason":"tool_calls"}\n\n'
                            else:
                                yield 'data: {"content":"已看到引导", "finish_reason":"stop"}\n\n'
                            yield "data: [DONE]\n\n"
                        return gen()

                with patch.object(file_memory, "_get_session_dir", return_value=Path(directory)):
                    asyncio.run(_run_case(["read_file"], LLM, stream, vision))
                self.assertEqual(len(requests), 2)
                next_context = str([m.model_dump() for m in requests[1].messages])
                self.assertIn("请根据这张图片继续", next_context)
                self.assertIn("引用原文", next_context)
                self.assertEqual("data:image/png;base64," in next_context, vision)
                injected_history = [r for r in _last_memory.records if isinstance(r, dict) and r.get("content") == content]
                self.assertEqual(len(injected_history), 1)
                self.assertEqual(injected_history[0]["quotes"], quotes)
                self.assertNotIn("base64", str(injected_history))
                frames = [json.loads(c[6:]) for c in stream.chunks if c.startswith("data: {")]
                event = next(f for f in frames if f.get("event") == "message_injected")
                self.assertEqual(event["content"], content)
                self.assertEqual(event["message_id"], "image-steer")
                self.assertEqual(event["quotes"], quotes)
                _cleanup()


class TodoLoopRegressionTests(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _run_script(self, script):
        requests = []

        class ScriptLLM:
            @staticmethod
            def chat_completions(*, request, **kwargs):
                requests.append(copy.deepcopy(request))
                assert len(requests) <= len(script), "空转保护未能结束循环"
                pending = set()
                for message in request.messages:
                    if message.role == "assistant" and message.tool_calls:
                        pending.update(call["id"] for call in message.tool_calls)
                    elif message.role == "tool":
                        assert message.tool_call_id in pending, "存在孤立工具结果"
                        pending.remove(message.tool_call_id)
                assert not pending, "存在无配对结果的工具声明"
                calls = script[len(requests) - 1]

                async def gen():
                    if calls is None:
                        yield 'data: {"content":"实际进展报告", "finish_reason":"stop"}\n\n'
                    else:
                        yield "data: " + json.dumps({"tool_calls": calls}, ensure_ascii=False) + "\n\n"
                        yield 'data: {"finish_reason":"tool_calls"}\n\n'
                    yield "data: [DONE]\n\n"
                return gen()

        stream = asyncio.run(_run_case(["todo_write", "read_file"], ScriptLLM))
        return requests, stream

    @staticmethod
    def _todo(seq, status="pending", content="执行任务"):
        return [{"index": 0, "id": f"todo_{seq}", "type": "function", "function": {
            "name": "todo_write", "arguments": json.dumps({"todos": [
                {"id": str(seq), "content": content, "status": status}
            ]}, ensure_ascii=False)
        }}]

    def test_repeated_pending_and_done_plans_allow_final_report(self):
        for status in ("pending", "done"):
            with self.subTest(status=status):
                requests, stream = self._run_script([
                    self._todo(i, status) for i in range(1, 4)
                ] + [None])
                self.assertEqual(len(requests), 4)
                self.assertNotIn("todo_write", [t["function"]["name"] for t in requests[-1].tools])
                context = "\n".join(str(m.content) for m in requests[-1].messages)
                self.assertIn('"unchanged": true', context)
                self.assertEqual(json.loads(next(m.content for m in requests[-1].messages if m.role == "tool"))["todos"][0]["id"], "1")
                self.assertIn("实际进展报告", "".join(stream.chunks))
                self.assertEqual(_last_memory.todos[0]["id"], "1")

    def test_updated_plan_visible_only_in_latest_tool_result(self):
        requests, _stream = self._run_script([self._todo(1, "in_progress"), self._todo(2, "done"), None])
        result = json.loads(next(m.content for m in requests[-1].messages if m.role == "tool"))
        self.assertTrue(result["success"])
        self.assertTrue(result["plan_complete"])
        self.assertEqual(result["todos"][0]["status"], "done")
        self.assertEqual(sum(m.role == "tool" for m in requests[-1].messages), 1)
        self.assertFalse(any("执行任务" in str(m.content) for m in requests[-1].messages if m.role == "system"))

    def test_ignoring_hidden_tool_is_bounded_and_preserves_plan(self):
        requests, stream = self._run_script([
            self._todo(i, content=f"改写计划{i}") for i in range(1, 7)
        ])
        self.assertEqual(len(requests), 5)
        self.assertEqual(_last_memory.todos[0]["content"], "改写计划3")
        self.assertIn("已终止本次空转", "".join(stream.chunks))
        tool_results = [r for r in _last_memory.records if isinstance(r, dict) and r.get("role") == "tool"]
        self.assertEqual(len(tool_results), 5)  # 拒绝调用也必须有配对结果
        self.assertIn("TODO_LOOP_GUARD", str(tool_results[-1]))

    def test_other_tool_restores_todo(self):
        read = [{"index": 0, "id": "read", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"factory/agent_runtime/todo_runtime.py","limit":1}'
        }}]
        requests, stream = self._run_script([
            self._todo(1), self._todo(2), self._todo(3), read, self._todo(5, "done"), None,
        ])
        self.assertEqual(len(requests), 6)
        self.assertIn("todo_write", [t["function"]["name"] for t in requests[4].tools])
        self.assertEqual(_last_memory.todos[0]["status"], "done")
        self.assertNotIn("已终止本次空转", "".join(stream.chunks))

    def test_mixed_batch_resumes_plan_and_keeps_tool_pairs(self):
        mixed = self._todo(4, "done") + [{
            "index": 1, "id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"factory/agent_runtime/todo_runtime.py","limit":1}'
            }
        }]
        requests, stream = self._run_script([
            self._todo(1), self._todo(2), self._todo(3), mixed, None,
        ])
        self.assertEqual(len(requests), 5)
        self.assertIn("todo_write", [t["function"]["name"] for t in requests[-1].tools])
        self.assertEqual(_last_memory.todos[0]["status"], "done")
        self.assertIn("实际进展报告", "".join(stream.chunks))


def _cleanup():
    repo_root = Path(__file__).resolve().parents[1]
    root = repo_root / "history_files"
    for suffix in ("", ".pending"):
        target = root / f"{TEST_SESSION}_chat.jsonl{suffix}"
        if target.exists():
            target.unlink()
    # 上传记录目录（upload 已改名为 history_files/session_files/）
    upload_dir = root / "session_files" / TEST_SESSION 
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
