"""sub_agent（子智能体）V1 测试。

覆盖：工具定义/注入、参数校验、round_store 事件校验与落盘、
chat_memory.add_sub_agent_event（JSONL + 检查点）、历史重建/压缩文本排除、
Runner 端到端（假 LLM 流）、父上下文隔离、停止传播、超时兜底、批量并发。
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools
from memory.chat_round_store import ChatRoundStore, _is_valid_event, _is_valid_sub_agent_event
from memory.chat_memory import ChatMemoryManager
from memory import chat_history_format


TEST_SESSION = "sub_agent_ut_session"


def _cleanup_files():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "history_files"
    target = root / f"{TEST_SESSION}_chat.jsonl"
    for suffix in ("", ".pending"):
        try:
            (root / f"{target.name}{suffix}").unlink()
        except OSError:
            pass


def _make_sub_agent_event(phase: str = "start", **fields) -> dict:
    base = {
        "event": "sub_agent",
        "agent_id": "agent_abc12345",
        "parent_tool_call_id": "call_p1",
        "phase": phase,
    }
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# 工具定义 / 注入 / 参数校验
# ---------------------------------------------------------------------------

class SubAgentToolDefinitionTests(unittest.TestCase):
    def test_is_builtin_tool_covers_sub_agent(self):
        self.assertTrue(builtin_tools.is_builtin_tool("sub_agent"))
        self.assertIn("sub_agent", builtin_tools.SELECTABLE_BUILTIN_TOOL_NAMES)

    def test_inject_sub_agent(self):
        tools, servers = builtin_tools.inject_builtin_tools([], {}, include_sub_agent=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("sub_agent", names)
        self.assertEqual(servers["sub_agent"], "__builtin__")

    def test_definition_has_task_required(self):
        params = builtin_tools.SUB_AGENT_TOOL_DEFINITION["function"]["parameters"]
        self.assertEqual(params["required"], ["task"])
        self.assertIn("task", params["properties"])
        self.assertIn("todo", params["properties"])

    def test_normalize_task_valid(self):
        task, err = builtin_tools.normalize_sub_agent_task("  分析目录  ")
        self.assertEqual(task, "分析目录")
        self.assertIsNone(err)

    def test_normalize_task_from_object(self):
        task, err = builtin_tools.normalize_sub_agent_task({"task": "调研 X"})
        self.assertEqual(task, "调研 X")
        self.assertIsNone(err)

    def test_normalize_task_empty_rejected(self):
        task, err = builtin_tools.normalize_sub_agent_task("   ")
        self.assertIsNone(task)
        self.assertIn("task", err)

    def test_normalize_task_too_long_rejected(self):
        task, err = builtin_tools.normalize_sub_agent_task("x" * 20001)
        self.assertIsNone(task)
        self.assertIn("超长", err)

    def test_ask_user_placeholder_result(self):
        result = builtin_tools.execute_ask_user_placeholder({"questions": [{"question": "?"}]})
        self.assertEqual(result["status"], "unavailable_in_sub_agent")
        self.assertIn("无法向用户提问", result["message"])


# ---------------------------------------------------------------------------
# chat_round_store：事件校验 + 记录
# ---------------------------------------------------------------------------

class RoundStoreSubAgentEventTests(unittest.TestCase):
    def _fresh_store(self) -> ChatRoundStore:
        store = ChatRoundStore("ut")
        store.record_message({"role": "user", "content": "hi"})
        return store

    def test_valid_events_recorded(self):
        store = self._fresh_store()
        self.assertTrue(store.record_sub_agent_event(_make_sub_agent_event("start", task="do")))
        self.assertTrue(store.record_sub_agent_event(_make_sub_agent_event(
            "model_call", seq=1, reasoning_content="r", content="c", tool_calls=[])))
        self.assertTrue(store.record_sub_agent_event(_make_sub_agent_event(
            "tool_result", tool_call_id="c1", tool_name="read_file", result="r")))
        self.assertTrue(store.record_sub_agent_event(_make_sub_agent_event(
            "todo", todos=[{"id": "1", "content": "a", "status": "pending"}])))
        self.assertTrue(store.record_sub_agent_event(_make_sub_agent_event(
            "done", status="done", final_reply="ok")))
        phases = [e["phase"] for e in store.pending_round["events"] if e.get("event") == "sub_agent"]
        self.assertEqual(phases, ["start", "model_call", "tool_result", "todo", "done"])

    def test_invalid_events_rejected(self):
        store = self._fresh_store()
        # agent_id 非法
        bad = _make_sub_agent_event("start", task="x")
        bad["agent_id"] = "call_123"
        self.assertFalse(store.record_sub_agent_event(bad))
        # delta 仅推流不落盘
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event("delta", content_delta="x")))
        # 缺 task
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event("start")))
        # done 缺 final_reply
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event("done", status="done")))
        # 非法 status
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event(
            "done", status="bogus", final_reply="x")))
        # heartbeat 仅推流不落盘
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event("heartbeat")))

    def test_no_pending_round_rejected(self):
        store = ChatRoundStore("ut")
        self.assertFalse(store.record_sub_agent_event(_make_sub_agent_event("start", task="x")))

    def test_generic_validator_routes_sub_agent(self):
        self.assertTrue(_is_valid_event(_make_sub_agent_event("start", task="x")))
        self.assertFalse(_is_valid_event({"event": "sub_agent", "phase": "start"}))


# ---------------------------------------------------------------------------
# chat_memory：JSONL 落盘 + 检查点快照
# ---------------------------------------------------------------------------

class ChatMemorySubAgentEventTests(unittest.TestCase):
    """add_sub_agent_event 落盘断言：JSONL 内 chat_round.events 聚合。"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_events_persisted(self):
        asyncio.run(asyncio.wait_for(_test_memory_flow(), timeout=30))

        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "history_files" / f"{TEST_SESSION}_chat.jsonl"
        self.assertTrue(path.exists())
        rounds = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rounds.append(json.loads(line))
        round_entries = [r for r in rounds if r.get("event") == "chat_round"]
        self.assertEqual(len(round_entries), 1)
        sub_events = [
            e for e in round_entries[0]["events"] if e.get("event") == "sub_agent"
        ]
        self.assertEqual(len(sub_events), 2)
        self.assertEqual(sub_events[0]["phase"], "start")
        self.assertIn("timestamp", sub_events[0])
        self.assertEqual(sub_events[1]["phase"], "done")
        self.assertEqual(sub_events[1]["final_reply"], "结论")


async def _test_memory_flow():
    manager = ChatMemoryManager(TEST_SESSION)
    # user 消息自动开启 chat_round；done 标记自动收尾
    await manager.add_chat_history({"role": "user", "content": "帮我调研"})
    ok = await manager.add_sub_agent_event(_make_sub_agent_event("start", task="t1"))
    assert ok == "记录成功", ok
    ok2 = await manager.add_sub_agent_event(_make_sub_agent_event(
        "done", status="done", final_reply="结论"))
    assert ok2 == "记录成功", ok2
    ok3 = await manager.add_sub_agent_event({"event": "sub_agent", "phase": "start"})
    assert ok3 != "记录成功", ok3
    await manager.add_chat_history({"role": "assistant", "content": "完成"})
    await manager.add_chat_history({"role": "assistant", "content": "", "done": "[DONE]"})


# ---------------------------------------------------------------------------
# 历史重建 / 压缩文本排除
# ---------------------------------------------------------------------------

class HistoryExclusionTests(unittest.TestCase):
    def _round_entry(self) -> dict:
        return {
            "event": "chat_round",
            "question": "调研任务",
            "events": [
                {"role": "assistant", "content": "我先派子任务", "tool_calls": [
                    {"id": "call_p1", "type": "function",
                     "function": {"name": "sub_agent", "arguments": "{\"task\": \"t1\"}"}},
                ]},
                _make_sub_agent_event("start", task="t1"),
                _make_sub_agent_event("model_call", seq=1, content="子任务思考正文"),
                _make_sub_agent_event("tool_start", tool_call_id="c9", tool_name="read_file", arguments="{}"),
                _make_sub_agent_event("tool_result", tool_call_id="c9", tool_name="read_file", result="子任务工具结果"),
                _make_sub_agent_event("done", status="done", final_reply="子任务最终回复"),
                {"role": "tool", "tool_call_id": "call_p1", "tool_name": "sub_agent",
                 "arguments": "{\"task\": \"t1\"}", "result": "子任务最终回复"},
                {"role": "assistant", "content": "子任务完成，结论如下"},
            ],
        }

    def test_context_messages_exclude_sub_agent_events(self):
        messages = chat_history_format.round_entry_to_context_messages(self._round_entry(), -1)
        rendered = json.dumps(messages, ensure_ascii=False)
        # 父级 assistant + tool 结果必须保留
        self.assertIn("我先派子任务", rendered)
        self.assertIn("sub_agent", rendered)
        self.assertIn("子任务最终回复", rendered)
        # 子任务轨迹绝不能出现
        self.assertNotIn("子任务思考正文", rendered)
        self.assertNotIn("agent_abc12345", rendered)

    def test_compaction_text_excludes_sub_agent_events(self):
        text = chat_history_format.round_entry_to_compaction_text(self._round_entry())
        self.assertIn("【用户问题】调研任务", text)
        self.assertIn("我先派子任务", text)
        self.assertIn("【工具结果：sub_agent】", text)
        # 子任务轨迹绝不能出现
        self.assertNotIn("子任务思考正文", text)
        self.assertNotIn("agent_abc12345", text)


# ---------------------------------------------------------------------------
# Runner 端到端（假 LLM 流）
# ---------------------------------------------------------------------------

def _fake_llm_script(script: list[list[str]]):
    """按调用次数返回脚本化 SSE 片段列表。"""
    state = {"n": 0, "requests": []}

    def fake_chat_completions(request=None, stream=None, stop_checker=None, model_config=None):
        state["n"] += 1
        state["requests"].append(request)
        chunks = script[min(state["n"], len(script)) - 1]

        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()
    return fake_chat_completions, state


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


TOOL_CALL_CHUNKS = [
    _sse({"reasoning_content": "子任务思考"}),
    _sse({"tool_calls": [
        {"index": 0, "id": "call_c1", "type": "function",
         "function": {"name": "write_file", "arguments": "{\"path\""}},
    ]}),
    _sse({"tool_calls": [
        {"index": 0, "function": {"arguments": ": \"tmp/sub_ut.txt\", \"content\": \"hi\"}}"}},
    ]}),
    _sse({"finish_reason": "tool_calls"}),
    "data: [DONE]\n\n",
]
FINAL_CHUNKS = [
    _sse({"reasoning_content": "收尾思考"}),
    _sse({"content": "子任务结论：已完成。"}),
    _sse({"finish_reason": "stop"}),
    "data: [DONE]\n\n",
]


def _make_context(**overrides) -> "object":
    from factory.agent_runtime.sub_agent import SubAgentContext, new_agent_id

    emitted: list[tuple[str, dict]] = []

    async def emit(payload):
        emitted.append((payload.get("phase"), payload))

    stop = {"flag": False}

    def stop_checker():
        return stop["flag"]

    ctx = SubAgentContext(
        agent_id=new_agent_id(),
        parent_agent_id="main",
        parent_tool_call_id="call_p1",
        parent_tool_index=0,
        agent_index=0,
        session_id=TEST_SESSION,
        task="写测试文件",
        initial_todo=[{"id": "1", "content": "写文件", "status": "pending"}],
        tools=[{"type": "function", "function": {"name": "write_file", "parameters": {}}}],
        tool_servers={"write_file": "__builtin__"},
        configured_tool_names=set(),
        configured_tool_servers={},
        max_rounds=5,
        timeout_seconds=0,
        reply_max_chars=1000,
        emit_event=emit,
        stop_checker=stop_checker,
    )
    return ctx, emitted, stop


class SubAgentRunnerTests(unittest.TestCase):
    def setUp(self):
        _cleanup_files()
        self._original_chat_completions = None
        from chat.chat_llm import ChatLLM
        self._original_chat_completions = ChatLLM.chat_completions

    def tearDown(self):
        from chat.chat_llm import ChatLLM
        ChatLLM.chat_completions = self._original_chat_completions
        # 清理测试期写入的文件
        from pathlib import Path
        target = Path(__file__).resolve().parents[1] / "tmp" / "sub_ut.txt"
        try:
            target.unlink()
        except OSError:
            pass

    def test_runner_completes_task(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.sub_agent import SubAgentRunner

        fake, fake_state = _fake_llm_script([TOOL_CALL_CHUNKS, FINAL_CHUNKS])
        ChatLLM.chat_completions = fake
        ctx, emitted, _stop = _make_context()
        result = asyncio.run(SubAgentRunner(ctx).run())
        phases = [p for p, _ in emitted]
        self.assertEqual(result.status, "done")
        self.assertIn("子任务结论", result.final_reply)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(phases.count("start"), 1)
        self.assertEqual(phases.count("done"), 1)
        self.assertIn("model_call", phases)
        self.assertIn("tool_start", phases)
        self.assertIn("tool_result", phases)
        self.assertIn("delta", phases)
        start_payload = next(p for ph, p in emitted if ph == "start")
        self.assertEqual(start_payload["task"], "写测试文件")
        self.assertEqual(
            start_payload["todo"],
            [{"id": "1", "content": "写文件", "status": "pending"}],
        )
        # tool_result 带 tool_call_id（并发区分）
        tool_result_payload = next(p for ph, p in emitted if ph == "tool_result")
        self.assertEqual(tool_result_payload["tool_call_id"], "call_c1")
        # 首轮模型请求必须已带上父级下发的任务文本（回归防护）
        first_request = fake_state["requests"][0]
        self.assertEqual(first_request.messages[0].role, "user")
        self.assertEqual(first_request.messages[0].content, "写测试文件")

    def test_parent_context_isolation(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.sub_agent import SubAgentRunner

        fake, _state = _fake_llm_script([TOOL_CALL_CHUNKS, FINAL_CHUNKS])
        ChatLLM.chat_completions = fake
        ctx, _emitted, _stop = _make_context()
        runner = SubAgentRunner(ctx)
        asyncio.run(runner.run())
        roles = [m.get("role") for m in runner.messages]
        # 首条消息必须是父级下发的任务文本（不带 _internal）：子任务独立
        # 上下文的唯一信息来源，缺失会导致模型收不到任务、空转收尾
        self.assertEqual(roles[0], "user")
        self.assertNotIn("_internal", runner.messages[0])
        self.assertEqual(runner.messages[0].get("content"), "写测试文件")
        # 其余 user 消息只能是内部提示（超时重试/截断续写等）
        for message in runner.messages[1:]:
            if message.get("role") == "user":
                self.assertTrue(message.get("_internal"), message)
        self.assertEqual(roles.count("assistant"), 2)
        self.assertEqual(roles.count("tool"), 1)

    def test_stop_checker_stops_child(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.sub_agent import SubAgentRunner

        fake, _state = _fake_llm_script([FINAL_CHUNKS])
        ChatLLM.chat_completions = fake
        ctx, emitted, stop = _make_context()
        stop["flag"] = True  # 派发前父级已停止
        result = asyncio.run(SubAgentRunner(ctx).run())
        self.assertEqual(result.status, "stopped")
        self.assertIn("子任务未完成", result.final_reply)
        done_payload = next(p for ph, p in emitted if ph == "done")
        self.assertEqual(done_payload["status"], "stopped")

    def test_timeout_fallback(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime import sub_agent as sub_agent_module

        slow_chunks = [
            _sse({"content": "慢慢思考"}),
            _sse({"finish_reason": "stop"}),
            "data: [DONE]\n\n",
        ]

        def slow_fake(request=None, stream=None, stop_checker=None, model_config=None):
            async def gen():
                await asyncio.sleep(5)
                for chunk in slow_chunks:
                    yield chunk
            return gen()

        ChatLLM.chat_completions = slow_fake
        ctx, emitted, _stop = _make_context()
        with mock.patch.object(
            sub_agent_module, "load_sub_agent_limits",
            return_value={
                "max_rounds": 5, "max_concurrent": 3,
                "timeout_seconds": 0.5, "reply_max_chars": 1000,
            },
        ):
            batch = asyncio.run(sub_agent_module.run_sub_agent_batch([ctx]))
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["_sub_agent"]["status"], "timeout")
        self.assertIn("超时", batch[0]["result"])
        done_payloads = [p for ph, p in emitted if ph == "done"]
        self.assertEqual(len(done_payloads), 1)
        self.assertEqual(done_payloads[0]["status"], "timeout")

    def test_batch_concurrent_two_agents(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime import sub_agent as sub_agent_module
        from factory.agent_runtime.sub_agent import SubAgentContext, new_agent_id

        fake, _state = _fake_llm_script([FINAL_CHUNKS])
        ChatLLM.chat_completions = fake
        emitted: list[tuple[str, dict]] = []

        async def emit(payload):
            emitted.append((payload.get("phase"), payload))

        def stop_checker():
            return False

        contexts = []
        for i in range(2):
            contexts.append(SubAgentContext(
                agent_id=new_agent_id(),
                parent_agent_id="main",
                parent_tool_call_id=f"call_p{i}",
                parent_tool_index=i,
                agent_index=i,
                session_id=TEST_SESSION,
                task=f"任务{i}",
                initial_todo=None,
                tools=[],
                tool_servers={},
                configured_tool_names=set(),
                configured_tool_servers={},
                max_rounds=3,
                timeout_seconds=10,
                reply_max_chars=500,
                emit_event=emit,
                stop_checker=stop_checker,
            ))
        batch = asyncio.run(sub_agent_module.run_sub_agent_batch(contexts))
        self.assertEqual(len(batch), 2)
        for i, item in enumerate(batch):
            self.assertEqual(item["tool_name"], "sub_agent")
            self.assertEqual(item["index"], i)
            self.assertIn("子任务结论", item["result"])
            self.assertEqual(item["_sub_agent"]["status"], "done")
        agent_ids = {payload["agent_id"] for _, payload in emitted}
        self.assertEqual(len(agent_ids), 2)


# ---------------------------------------------------------------------------
# 共享函数提取等价性（chat_factory re-export 不漂移）
# ---------------------------------------------------------------------------

class SharedFunctionExtractionTests(unittest.TestCase):
    def test_chat_factory_reexports_runtime_functions(self):
        """共享纯函数迁入 chat_runtime；思考回传/loader 在父循环命名空间保留本地实现。

        _retain_latest_reasoning / _copy_for_request 在 chat_factory 中是
        「显式传 limit 的包装」（limit 来自 cf 命名空间 loader，测试可打桩），
        chat_runtime 侧函数保持 limit 可选参数供子任务等独立调用方使用。
        """
        from factory import chat_factory
        from factory.agent_runtime import chat_runtime

        self.assertIs(chat_factory._parse_sse_event, chat_runtime.parse_sse_event)
        self.assertIs(
            chat_factory._merge_tool_call_delta, chat_runtime.merge_tool_call_delta)
        self.assertIs(
            chat_factory._filter_tool_calls_fields, chat_runtime.filter_tool_calls_fields)
        self.assertIs(
            chat_factory._REASONING_PLACEHOLDER, chat_runtime.REASONING_PLACEHOLDER)
        # loader 与回传包装：父循环命名空间本地实现（可打桩）
        self.assertTrue(callable(chat_factory._load_reasoning_return_max_length))
        self.assertTrue(callable(chat_factory._load_tool_call_stream_timeout))
        # 共享实现仍在 chat_runtime（子任务 Runner 直接使用）
        self.assertTrue(callable(chat_runtime.copy_for_request))
        self.assertTrue(callable(chat_runtime.retain_latest_reasoning))

    def test_parent_wrapper_passes_limit_into_runtime(self):
        """父循环包装显式传 limit：patch cf loader 时下游行为随之变化。"""
        import inspect
        from factory import chat_factory

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
            ]},
        ]
        from unittest import mock

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
            ]},
        ]
        with mock.patch.object(chat_factory, "_load_reasoning_return_max_length", return_value=0):
            chat_factory._retain_latest_reasoning(messages)
            self.assertEqual(messages[1]["reasoning_content"], "...")
        copied = chat_factory._copy_for_request(messages)
        self.assertTrue(inspect.isfunction(chat_factory._copy_for_request))

    def test_copy_for_request_placeholder_for_missing_reasoning(self):
        from factory.agent_runtime.chat_runtime import copy_for_request

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "完成"},  # 旧非工具轮：无思考字段
            {"role": "assistant", "tool_calls": [     # 最新工具轮：缺思考 → 占位符
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ]
        copied = copy_for_request(messages)
        # 最新工具轮带 reasoning_content 字段（占位符）
        self.assertEqual(copied[2].get("reasoning_content"), "...")
        # 历史 assistant 非工具轮：请求副本中剥离思考字段（本就没有）
        self.assertNotIn("reasoning_content", copied[1])


# ---------------------------------------------------------------------------
# 子智能体模型配置（model_selection.sub_agent_model）
# ---------------------------------------------------------------------------

class SubAgentModelConfigTests(unittest.TestCase):
    def test_model_selection_roles_include_sub_agent(self):
        from env_manager import MODEL_SELECTION_ROLES
        self.assertIn("sub_agent_model", MODEL_SELECTION_ROLES)

    def test_router_model_roles_include_sub_agent(self):
        from routers import chat_config_router

        self.assertIn("sub_agent_model", chat_config_router._MODEL_ROLES)

    def test_resolve_request_model_config_unconfigured_falls_back(self):
        from factory.agent_runtime.sub_agent import SubAgentRunner

        ctx, _emitted, _stop = _make_context()
        runner = SubAgentRunner(ctx)
        with mock.patch(
            "factory.agent_runtime.sub_agent.get_role_selection",
            return_value={"ownership_name": None, "model_name": None},
        ), mock.patch(
            "factory.agent_runtime.sub_agent.get_role_parameter",
            return_value={"temperature": 0.7},
        ) as fallback_param:
            config, parameter = runner._resolve_request_model_config()
        self.assertIsNone(config)
        self.assertEqual(parameter.get("temperature"), 0.7)
        # 未配置时参数继承自父级聊天模型角色
        fallback_param.assert_any_call("chat_model")

    def test_resolve_request_model_config_uses_sub_agent_role(self):
        from factory.agent_runtime.sub_agent import SubAgentRunner

        role_entry = {"ownership_name": "p1", "model_name": "m1"}
        model_cfg = {"apiType": "chat-completions", "url": "https://x/v1"}
        ctx, _emitted, _stop = _make_context()
        runner = SubAgentRunner(ctx)
        with (
            mock.patch(
                "factory.agent_runtime.sub_agent.get_role_selection",
                return_value=role_entry,
            ),
            mock.patch(
                "factory.agent_runtime.sub_agent.get_model_config",
                return_value=model_cfg,
            ),
            mock.patch(
                "factory.agent_runtime.sub_agent.get_role_parameter",
                return_value={"temperature": 0.2, "enable_thinking": True},
            ) as param_mock,
        ):
            config, parameter = runner._resolve_request_model_config()
            self.assertIs(config, model_cfg)
            self.assertEqual(parameter.get("temperature"), 0.2)
            param_mock.assert_any_call("sub_agent_model")

        # 协议不符 → 回退父级
        model_cfg_bad = {"apiType": "messages", "url": "https://x/v1"}
        with (
            mock.patch(
                "factory.agent_runtime.sub_agent.get_role_selection",
                return_value=role_entry,
            ),
            mock.patch(
                "factory.agent_runtime.sub_agent.get_model_config",
                return_value=model_cfg_bad,
            ),
        ):
            config, _parameter = runner._resolve_request_model_config()
        self.assertIsNone(config)

    def test_build_request_applies_sub_agent_parameter(self):
        from factory.agent_runtime.sub_agent import SubAgentRunner

        ctx, _emitted, _stop = _make_context()
        runner = SubAgentRunner(ctx)
        request = runner._build_request({
            "temperature": 0.1,
            "max_tokens": 1234,
            "reasoning_effort": "low",
            "top_p": 0.5,
            "extra_body": {"enable_thinking": False},
        })
        self.assertEqual(request.temperature, 0.1)
        self.assertEqual(request.max_tokens, 1234)
        self.assertEqual(request.reasoning_effort, "low")
        self.assertEqual(request.top_p, 0.5)
        self.assertEqual(request.extra_body.get("enable_thinking"), False)
        self.assertEqual(request.session_id, TEST_SESSION)

    def test_precheck_uses_sub_agent_model_window(self):
        """超窗预检查应使用子模型配置的窗口（而非父级聊天模型窗口）。"""
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.sub_agent import SubAgentRunner

        fake, _state = _fake_llm_script([FINAL_CHUNKS])
        ChatLLM.chat_completions = fake
        cfg = {"apiType": "chat-completions", "url": "https://x/v1", "maxInputTokens": 1}
        ctx, _emitted, _stop = _make_context()
        runner = SubAgentRunner(ctx)
        with (
            mock.patch(
                "factory.agent_runtime.sub_agent.get_role_selection",
                return_value={"ownership_name": "p1", "model_name": "m1"},
            ),
            mock.patch(
                "factory.agent_runtime.sub_agent.get_model_config",
                return_value=cfg,
            ),
            mock.patch(
                "factory.agent_runtime.sub_agent.get_role_parameter",
                return_value={},
            ),
        ):
            result = asyncio.run(runner.run())
        self.assertEqual(result.status, "error")
        self.assertIn("超过模型窗口", result.final_reply)


if __name__ == "__main__":
    unittest.main()
