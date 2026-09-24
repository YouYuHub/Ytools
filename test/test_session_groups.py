# -*- coding: utf-8 -*-
"""会话分组（session_groups）行为验证：

- 注册表：新建/重命名/折叠/删除；同名幂等；空名拒绝；
- 会话归属：_meta.group_id 写入/清除；归属映射扫描；非法值在 meta 重算中清除；
- 删除分组：成员归属一并解除；
- 路由层：5 个接口的请求/响应与错误码；
- 测试隔离：HISTORY_ROOT / LOCK_ROOT 指向临时目录，不污染真实 history_files。
"""
import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

import memory.chat_memory as chat_memory
from memory.chat_memory import (
    ChatMemoryManager,
    _recompute_meta_from_entries,
    assign_session_group,
    create_session_group,
    delete_session_group,
    list_session_group_assignments,
    list_session_groups,
    update_session_group,
)


def _new_sid(prefix: str = "grp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def test_group_registry_crud():
    """注册表 CRUD：新建、重命名、折叠、排序、删除。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sidecars").mkdir(parents=True, exist_ok=True)
        (root / "lock").mkdir(parents=True, exist_ok=True)
        with patch.object(chat_memory, "HISTORY_ROOT", root), \
                patch.object(chat_memory, "LOCK_ROOT", root / "lock"):
            assert list_session_groups() == [], "初始注册表为空"

            g1 = create_session_group("工作")
            g2 = create_session_group("学习")
            assert g1["id"].startswith("g-") and g1["name"] == "工作"
            assert g1["collapsed"] is False
            assert [g["name"] for g in list_session_groups()] == ["工作", "学习"]

            # 同名幂等：返回既有分组，不新增
            again = create_session_group("工作")
            assert again["id"] == g1["id"]
            assert len(list_session_groups()) == 2

            # 重命名 + 折叠
            updated = update_session_group(g1["id"], name="项目A", collapsed=True)
            assert updated["name"] == "项目A" and updated["collapsed"] is True
            # 只改折叠，不动名字
            update_session_group(g1["id"], collapsed=False)
            fetched = [g for g in list_session_groups() if g["id"] == g1["id"]][0]
            assert fetched["name"] == "项目A" and fetched["collapsed"] is False

            # 空名/不存在
            try:
                create_session_group("   ")
                raise AssertionError("空名应拒绝")
            except ValueError:
                pass
            try:
                update_session_group("g-missing", name="x")
                raise AssertionError("不存在分组应拒绝")
            except ValueError:
                pass

            # 删除
            result = delete_session_group(g2["id"])
            assert result["removed"] == 1
            assert [g["name"] for g in list_session_groups()] == ["项目A"]

            # 注册表落盘为可读 JSON
            raw = json.loads((root / "session_groups.json").read_text(encoding="utf-8"))
            assert isinstance(raw.get("groups"), list) and len(raw["groups"]) == 1
    print("PASS: 注册表 CRUD（新建/重命名/折叠/删除/同名幂等/空名拒绝）")


def test_group_name_normalization():
    """分组名规整：控制字符折叠、超长截断。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sidecars").mkdir(parents=True, exist_ok=True)
        (root / "lock").mkdir(parents=True, exist_ok=True)
        with patch.object(chat_memory, "HISTORY_ROOT", root), \
                patch.object(chat_memory, "LOCK_ROOT", root / "lock"):
            g = create_session_group("  多行\n名字\t测试  ")
            assert g["name"] == "多行 名字 测试", g["name"]
            long_name = "超" * 100
            g2 = create_session_group(long_name)
            assert len(g2["name"]) == chat_memory.SESSION_GROUP_NAME_MAX
    print("PASS: 分组名规整（控制字符折叠 + 超长截断）")


def test_session_assignment_and_meta():
    """会话归属：写入 _meta.group_id、归属扫描、移出、不改 updated_at。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sidecars").mkdir(parents=True, exist_ok=True)
        (root / "lock").mkdir(parents=True, exist_ok=True)
        with patch.object(chat_memory, "HISTORY_ROOT", root), \
                patch.object(chat_memory, "LOCK_ROOT", root / "lock"):
            g = create_session_group("测试组")
            sid = _new_sid()
            manager = ChatMemoryManager(sid)  # 构造即创建文件并物化 _meta 首行
            before = chat_memory.read_session_meta_value(sid, "updated_at")

            assign_session_group(sid, g["id"])
            assert chat_memory.read_session_meta_value(sid, "group_id") == g["id"]
            after = chat_memory.read_session_meta_value(sid, "updated_at")
            assert before == after, "归组不应改动 updated_at（不扰乱最近排序）"

            assert list_session_group_assignments() == {sid: g["id"]}

            # 移出分组
            assign_session_group(sid, None)
            assert chat_memory.read_session_meta_value(sid, "group_id") is None
            assert list_session_group_assignments() == {}

            # 不存在的分组拒绝
            try:
                assign_session_group(sid, "g-not-exist")
                raise AssertionError("不存在的分组应拒绝")
            except ValueError:
                pass

            # 不存在的会话拒绝
            try:
                assign_session_group("no_such_session_xyz", g["id"])
                raise AssertionError("不存在的会话应拒绝")
            except ValueError:
                pass
    print("PASS: 会话归属（写入/扫描/移出/校验/不动 updated_at）")


def test_meta_recompute_normalizes_group_id():
    """_meta 重算：合法 group_id 保留，非法（非字符串/空白）清除。"""
    base = {"title": "t", "group_id": "g-abc123"}
    meta = _recompute_meta_from_entries("s", base, [], bump_updated_at=False)
    assert meta.get("group_id") == "g-abc123", "合法归属应保留"

    meta2 = _recompute_meta_from_entries("s", {"title": "t", "group_id": 123}, [], bump_updated_at=False)
    assert "group_id" not in meta2, "非字符串归属应清除"

    meta3 = _recompute_meta_from_entries("s", {"title": "t", "group_id": "   "}, [], bump_updated_at=False)
    assert "group_id" not in meta3, "空白归属应清除"

    meta4 = _recompute_meta_from_entries("s", {"title": "t"}, [], bump_updated_at=False)
    assert "group_id" not in meta4, "缺失 = 未分组（不预填）"
    print("PASS: _meta 重算对 group_id 的规范化")


def test_delete_group_releases_members():
    """删除分组：成员会话归属一并解除，注册表条目移除。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sidecars").mkdir(parents=True, exist_ok=True)
        (root / "lock").mkdir(parents=True, exist_ok=True)
        with patch.object(chat_memory, "HISTORY_ROOT", root), \
                patch.object(chat_memory, "LOCK_ROOT", root / "lock"):
            g = create_session_group("待删组")
            sids = []
            for i in range(3):
                sid = _new_sid()
                ChatMemoryManager(sid)  # 构造即创建文件并物化 _meta 首行
                assign_session_group(sid, g["id"])
                sids.append(sid)
            assert len(list_session_group_assignments()) == 3

            result = delete_session_group(g["id"])
            assert result["removed"] == 1
            assert result["released_sessions"] == 3
            assert list_session_groups() == []
            assert list_session_group_assignments() == {}
            for sid in sids:
                assert chat_memory.read_session_meta_value(sid, "group_id") is None
    print("PASS: 删除分组连带解除成员归属")


def test_group_routes_end_to_end():
    """路由层：GET/POST/PUT/DELETE 五接口请求响应与错误码。"""
    import routers.chat_router as chat_router

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sidecars").mkdir(parents=True, exist_ok=True)
        (root / "lock").mkdir(parents=True, exist_ok=True)
        with patch.object(chat_memory, "HISTORY_ROOT", root), \
                patch.object(chat_memory, "LOCK_ROOT", root / "lock"):
            async def run():
                # 新建
                resp = await chat_router.create_chat_session_group(
                    chat_router.SessionGroupCreateRequest(name="路由组"))
                assert resp.status_code == 200
                gid = json.loads(resp.body)["group"]["id"]

                # 列表
                resp = await chat_router.list_chat_session_groups()
                payload = json.loads(resp.body)
                assert payload["groups"][0]["name"] == "路由组"
                assert payload["assignments"] == {}

                # 更新
                resp = await chat_router.update_chat_session_group(
                    gid, chat_router.SessionGroupUpdateRequest(name="改名后", collapsed=True))
                body = json.loads(resp.body)
                assert body["group"]["name"] == "改名后" and body["group"]["collapsed"] is True

                # 归属：会话不存在 → 400
                try:
                    await chat_router.assign_chat_session_group(
                        chat_router.SessionGroupAssignRequest(session_id="missing_xyz", group_id=gid))
                    raise AssertionError("会话不存在应 400")
                except Exception as exc:
                    assert getattr(exc, "status_code", None) == 400

                # 归属：正常写入
                sid = _new_sid()
                ChatMemoryManager(sid)  # 构造即创建文件并物化 _meta 首行
                resp = await chat_router.assign_chat_session_group(
                    chat_router.SessionGroupAssignRequest(session_id=sid, group_id=gid))
                assert resp.status_code == 200
                assert json.loads(resp.body)["group_id"] == gid

                # 列表带归属
                resp = await chat_router.list_chat_session_groups()
                assert json.loads(resp.body)["assignments"] == {sid: gid}

                # 删除
                resp = await chat_router.delete_chat_session_group(gid)
                body = json.loads(resp.body)
                assert body["removed"] == 1 and body["released_sessions"] == 1

                # 删除不存在 → 400
                try:
                    await chat_router.update_chat_session_group(
                        "g-missing", chat_router.SessionGroupUpdateRequest(name="x"))
                    raise AssertionError("不存在分组应 400")
                except Exception as exc:
                    assert getattr(exc, "status_code", None) == 400

            asyncio.run(run())
    print("PASS: 路由层五接口端到端（含 400 错误码）")


if __name__ == "__main__":
    test_group_registry_crud()
    test_group_name_normalization()
    test_session_assignment_and_meta()
    test_meta_recompute_normalizes_group_id()
    test_delete_group_releases_members()
    test_group_routes_end_to_end()
    print("ALL PASS")
