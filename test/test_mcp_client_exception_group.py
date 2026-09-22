# -*- coding: utf-8 -*-
"""mcp_client 异常组处理的版本无关性测试。

旧电脑（Python < 3.11 + mcp 1.x）上 `except ExceptionGroup` 因内建名缺失在
求值 except 子句时抛 NameError，导致所有 MCP 工具调用必失败
（报错：name 'ExceptionGroup' is not defined）。
修复后按「类型名 + .exceptions 结构」鸭子类型识别与解包，不引用 3.11+ 专用名。
"""
import asyncio
import contextlib
import sys
import unittest
from types import SimpleNamespace

import util.mcp_client as mcp_client


class _FakeGroup(Exception):
    """模拟旧解释器上 exceptiongroup 回退包/anyio 内部的同结构异常组。"""

    def __init__(self, exceptions):
        self.exceptions = list(exceptions)
        super().__init__("fake group")


# 让鸭子类型判定命中（类型名必须叫 ExceptionGroup；真实内建类无法改名，这里
# 改类实例的 __name__ 不影响内建，仅影响自定义类的识别路径）
_FakeGroup.__name__ = "ExceptionGroup"


class IsExceptionGroupTests(unittest.TestCase):
    def test_builtin_group_on_new_interpreters(self):
        if sys.version_info < (3, 11):
            self.skipTest("需要 Python 3.11+ 内建 ExceptionGroup")
        group = ExceptionGroup("g", [RuntimeError("boom")])
        self.assertTrue(mcp_client._is_exception_group(group))

    def test_duck_type_group_detected_without_builtin_name(self):
        exc = _FakeGroup([RuntimeError("boom")])
        self.assertTrue(mcp_client._is_exception_group(exc))
        # 非组异常、无名实类不误判
        self.assertFalse(mcp_client._is_exception_group(RuntimeError("x")))

    def test_plain_exception_not_group(self):
        self.assertFalse(mcp_client._is_exception_group(RuntimeError("x")))
        self.assertFalse(mcp_client._is_exception_group(ValueError("x")))


class UnwrapTests(unittest.TestCase):
    def test_nested_groups_unwrap_to_first_leaf(self):
        inner = _FakeGroup([KeyError("k")])
        outer = _FakeGroup([inner, RuntimeError("other")])
        unwrapped = mcp_client._unwrap_exception_group(outer)
        self.assertIsInstance(unwrapped, KeyError)
        self.assertEqual(str(unwrapped), "'k'")

    def test_plain_exception_passthrough(self):
        err = RuntimeError("plain")
        self.assertIs(mcp_client._unwrap_exception_group(err), err)


class FormatErrorTests(unittest.TestCase):
    def test_joins_sub_messages_across_versions(self):
        group = _FakeGroup([RuntimeError("a"), RuntimeError("b")])
        self.assertEqual(mcp_client._format_mcp_error(group), "a; b")

    def test_plain_message(self):
        self.assertEqual(mcp_client._format_mcp_error(RuntimeError("x")), "x")


class CallToolUnwrapTests(unittest.IsolatedAsyncioTestCase):
    """端到端：模拟 anyio 退出 stdio_client 时抛异常组。"""

    def _patch_mcp_internals(self, session_stub, raise_on_exit=None):
        @contextlib.asynccontextmanager
        async def fake_stdio_client(_params):
            yield None, None
            if raise_on_exit is not None:
                raise raise_on_exit

        fake_cm = fake_stdio_client
        sessions = contextlib.AsyncExitStack()

        @contextlib.asynccontextmanager
        async def fake_session(_read, _write):
            yield session_stub

        self._patches = [
            unittest.mock.patch.object(mcp_client, "build_stdio_server_parameters", lambda _svc: SimpleNamespace()),
            unittest.mock.patch.object(mcp_client, "stdio_client", fake_cm),
            unittest.mock.patch.object(mcp_client, "ClientSession", fake_session),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def setUp(self):
        import unittest.mock
        self.mock = unittest.mock

    async def test_group_exception_unwrapped_and_wrapped(self):
        session_stub = SimpleNamespace(
            initialize=self.mock.AsyncMock(),
            discover=None,
            list_tools=self.mock.AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="demo")])),
            call_tool=self.mock.AsyncMock(
                return_value=SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")])
            ),
        )
        # 退出 stdio_client 时抛旧版结构异常组（首个子异常为 RuntimeError）
        self._patch_mcp_internals(session_stub, raise_on_exit=_FakeGroup([RuntimeError("连接中断")]))
        with self.assertRaises(RuntimeError) as ctx:
            await mcp_client._call_mcp_tool_impl("demo", {}, "svc")
        self.assertIn("MCP 调用失败", str(ctx.exception))
        self.assertIn("连接中断", str(ctx.exception))
        # 真实子异常保留在异常链上
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    async def test_group_with_value_error_raises_value_error(self):
        session_stub = SimpleNamespace(
            initialize=self.mock.AsyncMock(),
            discover=None,
            list_tools=self.mock.AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="demo")])),
            call_tool=self.mock.AsyncMock(
                return_value=SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")])
            ),
        )
        self._patch_mcp_internals(session_stub, raise_on_exit=_FakeGroup([ValueError("工具执行失败: x")]))
        with self.assertRaises(ValueError):
            await mcp_client._call_mcp_tool_impl("demo", {}, "svc")

    async def test_plain_runtime_error_re_raised_unchanged(self):
        session_stub = SimpleNamespace(
            initialize=self.mock.AsyncMock(),
            discover=None,
            list_tools=self.mock.AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="demo")])),
            call_tool=self.mock.AsyncMock(
                return_value=SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")])
            ),
        )
        self._patch_mcp_internals(session_stub, raise_on_exit=RuntimeError("普通异常"))
        with self.assertRaises(RuntimeError) as ctx:
            await mcp_client._call_mcp_tool_impl("demo", {}, "svc")
        self.assertEqual(str(ctx.exception), "普通异常")

    async def test_new_builtin_group_also_handled(self):
        if sys.version_info < (3, 11):
            self.skipTest("需要 Python 3.11+ 内建 ExceptionGroup")
        session_stub = SimpleNamespace(
            initialize=self.mock.AsyncMock(),
            discover=None,
            list_tools=self.mock.AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="demo")])),
            call_tool=self.mock.AsyncMock(
                return_value=SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")])
            ),
        )
        self._patch_mcp_internals(session_stub, raise_on_exit=BaseExceptionGroup(
            "g", [ExceptionGroup("inner", [RuntimeError("anyio 取消")])]
        ))
        with self.assertRaises(RuntimeError) as ctx:
            await mcp_client._call_mcp_tool_impl("demo", {}, "svc")
        self.assertIn("anyio 取消", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
