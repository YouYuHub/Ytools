import unittest
from unittest import mock

from routers import chat_router


class StopSubAgentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_ack_is_scoped_to_request_and_handler_is_removed(self):
        handlers = []
        proxy = mock.Mock()
        proxy.is_alive.return_value = True
        proxy.add_handler.side_effect = handlers.append
        proxy.remove_handler.side_effect = handlers.remove

        def send(agent_id, request_id):
            self.assertEqual(agent_id, "agent_one")
            for handler in list(handlers):
                handler({"type": "sub_agent_stop_ack", "request_id": "other", "ok": False})
                handler({"type": "sub_agent_stop_ack", "request_id": request_id, "ok": True})
            return True
        proxy.request_sub_agent_stop.side_effect = send
        with mock.patch.object(chat_router, "peek_worker_proxy", return_value=proxy):
            result = await chat_router.stop_sub_agent(
                chat_router.StopSubAgentRequest(session_id="test_stop", agent_id="agent_one")
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(handlers, [])

    async def test_inline_stop_calls_only_the_requested_agent(self):
        with mock.patch.object(chat_router, "peek_worker_proxy", return_value=None), \
             mock.patch("factory.agent_runtime.sub_agent.request_sub_agent_stop", return_value=False) as stop:
            result = await chat_router.stop_sub_agent(
                chat_router.StopSubAgentRequest(session_id="test_stop", agent_id="agent_missing")
            )
        self.assertEqual(result, {"ok": False})
        stop.assert_called_once_with("test_stop", "agent_missing")

    async def test_worker_send_failure_does_not_leave_handler_registered(self):
        proxy = mock.Mock()
        proxy.is_alive.return_value = True
        proxy.request_sub_agent_stop.return_value = False
        with mock.patch.object(chat_router, "peek_worker_proxy", return_value=proxy):
            result = await chat_router.stop_sub_agent(
                chat_router.StopSubAgentRequest(session_id="test_stop", agent_id="agent_one")
            )
        self.assertEqual(result, {"ok": False})
        proxy.remove_handler.assert_called_once()
