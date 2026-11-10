"""删除会话确认逻辑：弹窗必须先关闭，且 404 按幂等成功处理。

回归背景：用户点“确认删除”后会话确实被删了，但模态框不关、背景页报
「会话可能不存在」。根因两处：
  1. 后端 delete_file 先 await stop_chat_task，其内部收尾会走写路径，
     在会话已被标 deleted/cleanup_pending 时抛 HTTPException(404)；
     路由层只 except OSError，404 直接冒到前端 → 前端 catch 只 toast，
     弹窗留在前台。
  2. 前端 closeDeleteModal() 写在 try 内成功分支末尾，任何异常都不关窗。
修复：后端停任务失败不阻断删除、404 视为幂等成功；前端先关窗再删、
并把 404 当作删除成功来做本地清理。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeleteSessionRouteTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "routers" / "chat_router.py").read_text(encoding="utf-8")

    def _body(self):
        start = self.source.index("async def delete_chat_history(")
        end = self.source.index("class MediaTagRemoveRequest", start)
        return self.source[start:end]

    def test_stop_failure_does_not_block_delete(self):
        body = self._body()
        # 停任务必须包在自己的 try 里，且 except 覆盖面足够宽（含 HTTPException）
        stop_at = body.index("stop_chat_task(session_id)")
        head = body[:stop_at]
        self.assertIn("try:", head, "stop_chat_task 必须在 try 内调用")
        tail = body[stop_at:]
        self.assertRegex(
            tail, r"except Exception as e:[\s\S]{0,200}logger\.warning",
            "停任务失败必须被兜住并记日志，不能阻断删除",
        )

    def test_404_is_idempotent_success(self):
        body = self._body()
        self.assertIn("except HTTPException as e:", body, "必须单独接住 HTTPException")
        self.assertRegex(
            body, r"e\.status_code == 404[\s\S]{0,200}已删除",
            "404（已删除/清理中）应按幂等成功返回",
        )

    def test_delete_still_runs_after_stop_failure(self):
        body = self._body()
        # delete_chat_session_file 必须在 stop 之后、且不在同一个 try 里被连带跳过
        self.assertLess(
            body.index("stop_chat_task(session_id)"),
            body.index("delete_chat_session_file(session_id)"),
            "删除必须在停任务之后执行",
        )
        stop_block = body[body.index("try:", body.index("会话都要删了") - 200):]
        # 停任务的 try 必须在 delete 之前闭合
        first_except = stop_block.index("except Exception as e:")
        self.assertLess(first_except, body.index("delete_chat_session_file(session_id)"),
                        "停任务的 try/except 必须早于删除调用闭合")


class DeleteSessionFrontendTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "H5" / "src" / "app" / "sessions.ts").read_text(encoding="utf-8")

    def _fn(self):
        start = self.source.index("async function confirmDeleteSession(")
        end = self.source.index("function markActiveSession(", start)
        return self.source[start:end]

    def test_modal_closed_before_api_call(self):
        fn = self._fn()
        self.assertLess(
            fn.index("closeDeleteModal()"),
            fn.index("await API.deleteSession("),
            "必须先关弹窗再发起删除，否则异常时弹窗留在前台",
        )

    def test_modal_close_outside_try(self):
        fn = self._fn()
        try_at = fn.index("try {")
        close_at = fn.index("closeDeleteModal()")
        self.assertLess(close_at, try_at, "关窗不能放在 try 里（否则异常时不执行）")

    def test_404_treated_as_success(self):
        fn = self._fn()
        self.assertIn("status === 404", fn, "前端必须把 404 当删除成功")
        self.assertRegex(
            fn, r"status === 404[\s\S]{0,400}applyDeletedSession",
            "404 分支也要做本地清理（移行/清缓存）",
        )

    def test_error_path_still_toasts(self):
        fn = self._fn()
        self.assertIn("删除失败", fn, "真正的失败仍要提示")


if __name__ == "__main__":
    unittest.main()
