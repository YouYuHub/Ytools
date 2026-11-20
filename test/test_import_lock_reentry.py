# coding: utf-8
"""导入锁重入保护回归。

背景（用户实测）：ZIP 导入外层持 import-<会话id>.lock，内层 JSONL 写入若再抢
同名锁（Windows OS 文件锁不可重入），自己等自己直到 30s 超时；超时又被
「单会话失败不阻断」吞成 failed → 前端“没有导入任何会话”，冲突弹窗按钮
反复点击无响应。_reentrant_cross_process_lock 用 contextvar 记录当前调用链
已持有的锁路径，嵌套获取直接复用；跨线程/跨进程仍走真实互斥。
"""
import asyncio
import json
import threading
import time

import pytest

from memory import chat_memory as cm
from memory import file_memory as fm


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    root = tmp_path / "history"
    monkeypatch.setattr(cm, "HISTORY_ROOT", root)
    monkeypatch.setattr(cm, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(fm, "HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(fm, "_session_managers", {})


def _payload(text="steer import fixture"):
    return (json.dumps({"_meta": {"title": text}}) + "\n" + json.dumps({
        "event": "chat_round", "question": text, "status": "done",
        "started_at": "2026-11-20 12:00:00", "ended_at": "2026-11-20 12:00:01",
        "events": [
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
            {"role": "assistant", "done": "[DONE]"},
        ],
    }) + "\n").encode()


def test_nested_same_lock_reuses_without_deadlock(tmp_path):
    lock = tmp_path / "nested.lock"
    started = time.monotonic()
    with cm._reentrant_cross_process_lock(lock):
        with cm._reentrant_cross_process_lock(lock):
            pass
    assert time.monotonic() - started < 2, "同链路嵌套加锁不应等待超时"
    # 释放后可再次真实获取
    with cm._reentrant_cross_process_lock(lock):
        pass


def test_other_thread_still_blocked_while_held(tmp_path):
    lock = tmp_path / "mutex.lock"
    outcome = {}

    def try_acquire():
        try:
            with cm.cross_process_lock(lock, timeout=0.3):
                outcome["acquired"] = True
        except TimeoutError:
            outcome["acquired"] = False

    with cm._reentrant_cross_process_lock(lock):
        worker = threading.Thread(target=try_acquire)
        worker.start()
        worker.join(timeout=5)
    assert outcome.get("acquired") is False, "重入豁免只对持有链路生效，其它线程必须仍被互斥"


def test_inner_jsonl_import_under_outer_import_lock_is_fast():
    """复现死锁链路：外层持 import-<id>.lock 时内层裸 JSONL 导入同 ID。"""
    sid = "reentry-import"
    lock_path = cm.LOCK_ROOT / ("import-" + sid + ".lock")
    started = time.monotonic()
    with cm._reentrant_cross_process_lock(lock_path):
        result = cm._import_jsonl_payload(sid, _payload())
    elapsed = time.monotonic() - started
    assert result["state"] == "succeed" and result["imported_rounds"] == 1
    assert elapsed < 5, f"嵌套导入耗时 {elapsed:.1f}s，疑似仍在等自己的锁"


def test_zip_import_with_brand_new_session_id_succeeds_quickly():
    """ZIP 包内含全新会话 ID（用户踩坑的最常见场景）不得因锁自等而 failed。"""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fresh-zip_chat.jsonl", _payload("zip fresh content"))
        archive.writestr("manifest.json", json.dumps({
            "version": 1,
            "sessions": [{"session_id": "fresh-zip", "filename": "fresh-zip_chat.jsonl"}],
        }))
    started = time.monotonic()
    result = asyncio.run(cm.import_sessions_from_zip(buffer.getvalue()))
    elapsed = time.monotonic() - started
    assert not result["failed"], result
    assert len(result["imported"]) == 1
    assert elapsed < 10, f"ZIP 导入耗时 {elapsed:.1f}s，疑似锁自等"


def test_responses_meta_mirror_under_held_file_lock(tmp_path):
    """responses 镜像回写在导入/删除链已持文件锁时不得二次抢同一把锁。"""
    sid = "mirror-reentry"
    manager = cm.ChatMemoryManager(sid)
    path = manager._file_path
    assert path.exists()
    lock_path = cm.LOCK_ROOT / (path.name + ".lock")
    started = time.monotonic()
    with cm._reentrant_cross_process_lock(lock_path):
        cm.update_responses_session_meta(sid, {"version": 1, "status": "rebuild_pending"})
    assert time.monotonic() - started < 5
