"""删除链路各收尾点必须在 cleanup 语境下调授权。

真凶复盘（实测堆栈）：
  routers/chat_router.delete_chat_history
    → ChatMemoryManager.delete_chat_session_file   ← 成功，会话标记 deleted
    → permission_store.delete_session               ← 内部 authorize_session(默认 cleanup=False)
        → task_cost_store.delete_session            ← 同样 cleanup=False
            → authorization.session → HTTPException(404, 会话已删除或正在清理)

结果：文件已删、行已删，但接口 404。前端弹窗不关、背景页报“会话可能不存在”，
再点确认仍然 404（因为状态已是 deleted）。这三处都必须 cleanup=True。
"""
import inspect
import unittest

from memory import permission_store, task_cost_store
from security import authorization


class DeleteChainCleanupAuthTests(unittest.TestCase):
    def test_task_cost_store_delete_uses_cleanup(self):
        src = inspect.getsource(task_cost_store.delete_session)
        self.assertRegex(
            src, r"authorize_session\([^)]*cleanup\s*=\s*True",
            "task_cost_store.delete_session 必须 cleanup=True，否则删完会抛 404",
        )

    def test_permission_store_delete_uses_cleanup(self):
        src = inspect.getsource(permission_store.delete_session)
        self.assertRegex(
            src, r"authorize_session\([^)]*cleanup\s*=\s*True",
            "permission_store.delete_session 必须 cleanup=True",
        )

    def test_authorization_cleanup_allows_all_blocked_states(self):
        src = inspect.getsource(authorization.session)
        # 删除接口应幂等：BLOCKED 三态（deleting/deleted/cleanup_pending）都要放行
        self.assertIn("not cleanup", src,
                      "cleanup=True 时必须放行全部 BLOCKED 状态（含卡死的 deleting）")
        self.assertNotRegex(
            src, r"cleanup and row\[.state.\] in \{",
            "不要再按状态子集白名单放行，deleting 会永久卡死",
        )

    def test_router_tolerates_post_delete_cleanup_failure(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        src = (root / "routers" / "chat_router.py").read_text(encoding="utf-8")
        start = src.index("async def delete_chat_history(")
        body = src[start:src.index("class MediaTagRemoveRequest", start)]
        # 收尾清理必须容错：删除已成功，不能因清理失败把响应改成错误
        self.assertRegex(
            body,
            r"try:\s*\n\s*from memory\.permission_store import delete_session[\s\S]{0,400}except Exception",
            "删除成功后的收尾清理必须包 try/except，否则会把成功变成失败响应",
        )


if __name__ == "__main__":
    unittest.main()
