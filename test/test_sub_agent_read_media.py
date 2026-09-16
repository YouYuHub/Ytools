# -*- coding: utf-8 -*-
"""子智能体 read_media 链路回归测试。

覆盖：
- resolve_sub_agent_vision_enabled：sub_agent_model 独立子模型 vision 判定 /
  未配置时继承父级 / 判定异常保守支持
- _collect_task_media_references：任务文本 media:// 引用提取与去重
- 子任务 vision=false：
  * 工具定义被剔除（_build_sub_agent_contexts 派发层 + _visible_tool_definitions 执行层）
  * 幻觉调用 read_media 被拒绝且给出明确文案
  * start 事件工具列表不含 read_media
- 子任务 vision=true 且父级启用：
  * execute_read_media 正常执行（任务引用白名单过滤）
  * 读取成功后媒体部件注入为 user 消息（仅内存）
  * 越界引用（不在任务文本中）被拒绝
"""
import asyncio
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.agent_runtime import builtin_tools as bt
from factory.agent_runtime.sub_agent import (
    SubAgentContext,
    SubAgentRunner,
    _collect_task_media_references,
    new_agent_id,
    resolve_sub_agent_vision_enabled,
)
from memory import file_memory as fm

_TEST_SESSION = "sub_read_media_test"
_TEST_MEDIA_DIR = fm.HISTORY_ROOT / _TEST_SESSION
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _cleanup():
    shutil.rmtree(_TEST_MEDIA_DIR, ignore_errors=True)


def _make_context(task="子任务", tools=None, tool_servers=None, **overrides):
    emitted: list[tuple[str, dict]] = []

    async def emit(payload):
        emitted.append((payload.get("phase"), payload))

    def stop_checker():
        return False

    ctx = SubAgentContext(
        agent_id=new_agent_id(),
        parent_agent_id="main",
        parent_tool_call_id="call_p1",
        parent_tool_index=0,
        agent_index=0,
        session_id=_TEST_SESSION,
        task=task,
        initial_todo=None,
        tools=tools if tools is not None else [],
        tool_servers=tool_servers if tool_servers is not None else {},
        configured_tool_names=set(),
        configured_tool_servers={},
        max_rounds=3,
        timeout_seconds=0,
        reply_max_chars=2000,
        emit_event=emit,
        stop_checker=stop_checker,
        **overrides,
    )
    return ctx, emitted


class ResolveSubAgentVisionTests(unittest.TestCase):
    def test_sub_agent_model_vision_false_wins(self):
        import factory.agent_runtime.sub_agent as sa
        original_selection = sa.get_role_selection
        original_config = sa.get_model_config
        try:
            sa.get_role_selection = lambda role: (
                {"ownership_name": "P", "model_name": "sub-model"}
                if role == "sub_agent_model" else {}
            )
            sa.get_model_config = lambda p, m: {"vision": False}
            self.assertIs(resolve_sub_agent_vision_enabled(True), False)
        finally:
            sa.get_role_selection = original_selection
            sa.get_model_config = original_config

    def test_sub_agent_model_config_invalid_falls_back_to_parent(self):
        import factory.agent_runtime.sub_agent as sa
        original_selection = sa.get_role_selection
        original_config = sa.get_model_config
        try:
            sa.get_role_selection = lambda role: (
                {"ownership_name": "P", "model_name": "gone"}
                if role == "sub_agent_model" else {}
            )
            sa.get_model_config = lambda p, m: None  # 模型配置失效
            self.assertIs(resolve_sub_agent_vision_enabled(True), True)
        finally:
            sa.get_role_selection = original_selection
            sa.get_model_config = original_config

    def test_unconfigured_inherits_parent(self):
        import factory.agent_runtime.sub_agent as sa
        original = sa.get_role_selection
        try:
            sa.get_role_selection = lambda role: {}
            self.assertIs(resolve_sub_agent_vision_enabled(False), False)
            self.assertIs(resolve_sub_agent_vision_enabled(True), True)
            self.assertIs(resolve_sub_agent_vision_enabled(None), True)
        finally:
            sa.get_role_selection = original

    def test_exception_falls_back_to_parent(self):
        import factory.agent_runtime.sub_agent as sa
        original = sa.get_role_selection
        try:
            def _boom(role):
                raise RuntimeError("boom")
            sa.get_role_selection = _boom
            self.assertIs(resolve_sub_agent_vision_enabled(False), False)
        finally:
            sa.get_role_selection = original

    def test_sub_model_vision_false_overrides_parent_true(self):
        """父级支持视觉但独立子模型不支持：以子模型为准（591 会话 bug 回归）。

        根因：旧实现仅在父级未传 vision 时才解析独立子模型，父级传 True
        会覆盖独立子模型的 vision=false，导致子任务看得到 read_media。
        """
        import factory.agent_runtime.sub_agent as sa
        original_selection = sa.get_role_selection
        original_config = sa.get_model_config
        try:
            sa.get_role_selection = lambda role: (
                {"ownership_name": "OpenCode Completions", "model_name": "Deepseek V4 Pro"}
                if role == "sub_agent_model" else {}
            )
            sa.get_model_config = lambda p, m: {
                "vision": False, "apiType": "chat-completions",
            }
            self.assertIs(resolve_sub_agent_vision_enabled(True), False)
        finally:
            sa.get_role_selection = original_selection
            sa.get_model_config = original_config

    def test_sub_model_non_chat_completions_falls_back(self):
        """独立子模型协议非 chat-completions：与模型回退链一致，继承父级。"""
        import factory.agent_runtime.sub_agent as sa
        original_selection = sa.get_role_selection
        original_config = sa.get_model_config
        try:
            sa.get_role_selection = lambda role: (
                {"ownership_name": "P", "model_name": "m"}
                if role == "sub_agent_model" else {}
            )
            sa.get_model_config = lambda p, m: {
                "vision": False, "apiType": "messages",
            }
            self.assertIs(resolve_sub_agent_vision_enabled(True), True)
        finally:
            sa.get_role_selection = original_selection
            sa.get_model_config = original_config


class CollectTaskMediaReferencesTests(unittest.TestCase):
    def test_extract_and_dedupe(self):
        task = "先读取 media://a.png 再对比 media://b.jpg，最后回看 media://a.png"
        refs = _collect_task_media_references(task)
        self.assertEqual(refs, ["media://a.png", "media://b.jpg"])

    def test_no_references(self):
        self.assertEqual(_collect_task_media_references("纯文本任务"), [])
        self.assertEqual(_collect_task_media_references(""), [])


class SubAgentReadMediaVisionFalseTests(unittest.TestCase):
    """子任务模型不支持视觉：工具不可见、调用被拒。"""

    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _runner_with_read_media(self, vision_value):
        from factory.agent_runtime.sub_agent import _tool_definition_name
        read_media_def = json.loads(json.dumps(bt.READ_MEDIA_TOOL_DEFINITION))
        ctx, emitted = _make_context(
            task="看看 media://a.png",
            tools=[read_media_def],
            tool_servers={"read_media": "__builtin__"},
            parent_vision_enabled=vision_value,
        )
        runner = SubAgentRunner(ctx)
        return runner, ctx, emitted, _tool_definition_name

    def test_definition_hidden_in_request_tools(self):
        runner, _, _, tool_name_of = self._runner_with_read_media(False)
        visible = runner._visible_tool_definitions()
        names = [tool_name_of(t) for t in visible]
        self.assertNotIn("read_media", names)

    def test_definition_visible_when_vision_true(self):
        runner, _, _, tool_name_of = self._runner_with_read_media(True)
        names = [tool_name_of(t) for t in runner._visible_tool_definitions()]
        self.assertIn("read_media", names)

    async def _hallucinated_call_rejected(self, vision_value):
        runner, _, emitted, _ = self._runner_with_read_media(vision_value)
        tool_call = {
            "id": "call_m1",
            "type": "function",
            "function": {
                "name": "read_media",
                "arguments": json.dumps({"references": ["media://a.png"]}),
            },
        }
        results = await runner._execute_tool_round([tool_call])
        return runner, results[0]

    def test_vision_false_call_rejected_with_message(self):
        runner, result = asyncio.run(self._hallucinated_call_rejected(False))
        payload = result["result"]
        self.assertFalse(payload["ok"])
        self.assertIn("不支持视觉", payload["error"])
        self.assertIn("read_media", payload["error"])
        self.assertEqual(payload["loaded"], [])
        # 不应留下注入坐标
        self.assertEqual(runner.read_media_pending_parts, [])

    def test_start_event_tools_exclude_read_media_when_vision_false(self):
        runner, _, emitted, _ = self._runner_with_read_media(False)
        runner._vision_enabled = False  # 直接置位模拟派发判定后的状态
        asyncio.run(runner._emit_start())
        start = [p for phase, p in emitted if phase == "start"][0]
        self.assertNotIn("read_media", start["tools"])


class SubAgentReadMediaVisionTrueTests(unittest.TestCase):
    """子任务模型支持视觉且父级启用：正常执行 + 数据注入。"""

    def setUp(self):
        _cleanup()
        self.saved = fm.save_session_media(_TEST_SESSION, "样例.png", PNG_BYTES)

    def tearDown(self):
        _cleanup()

    def test_success_returns_meta_and_pends_injection(self):
        ref = self.saved["media_ref"]
        task = f"读取 {ref} 并描述内容"
        read_media_def = json.loads(json.dumps(bt.READ_MEDIA_TOOL_DEFINITION))
        ctx, emitted = _make_context(
            task=task,
            tools=[read_media_def],
            tool_servers={"read_media": "__builtin__"},
            parent_vision_enabled=True,
        )
        runner = SubAgentRunner(ctx)
        # 真实加载：media:// 会话文件存在（setUp 已入库）
        tool_call = {
            "id": "call_m2",
            "type": "function",
            "function": {
                "name": "read_media",
                "arguments": json.dumps({"references": [ref]}),
            },
        }
        results = asyncio.run(runner._execute_tool_round([tool_call]))
        payload = results[0]["result"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["injected_references"], [ref])
        # 坐标已进入待注入队列
        self.assertEqual(runner.read_media_pending_parts, [(ref, None)])

    def test_reference_outside_task_rejected(self):
        read_media_def = json.loads(json.dumps(bt.READ_MEDIA_TOOL_DEFINITION))
        ctx, _ = _make_context(
            task="没有引用的任务",
            tools=[read_media_def],
            tool_servers={"read_media": "__builtin__"},
            parent_vision_enabled=True,
        )
        runner = SubAgentRunner(ctx)
        tool_call = {
            "id": "call_m3",
            "type": "function",
            "function": {
                "name": "read_media",
                "arguments": json.dumps({"references": ["media://evil.png"]}),
            },
        }
        results = asyncio.run(runner._execute_tool_round([tool_call]))
        payload = results[0]["result"]
        # 任务文本没有引用：media:// 调用一律越界拒绝（防越权读取会话其他媒体）
        self.assertFalse(payload["ok"])
        self.assertIn("没有可读取的 media:// 引用", payload["error"])
        self.assertEqual(payload["injected_references"], [])
        self.assertEqual(runner.read_media_pending_parts, [])


if __name__ == "__main__":
    unittest.main()
