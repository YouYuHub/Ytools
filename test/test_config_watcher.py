"""配置文件热重载轮询（util/config_watcher.py）行为验证：

- hash 比对：首次读取建立基线（触发回调），未变化跳过；
- JSON 解析失败不更新 hash/内存（下一轮自动重试）；
- mcp_servers 目标的 servers_changed 标志（仅 servers 键变化才触发工具重探）；
- 回调异常不影响轮询循环与其余回调。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

from util import config_watcher


def _wait_filesystem() -> None:
    # Windows NTFS mtime_ns 精度足够，但留出微小间隔确保 size/mtime 快路径可靠
    time.sleep(0.02)


def test_first_poll_primes_and_dispatches():
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mcp_servers.json"
        path.write_text(json.dumps({"servers": {"a": {"command": "x"}}, "inputs": {"a": []}}), encoding="utf-8")
        config_watcher.register_reload_callback("mcp_servers", "recorder", lambda d, m: calls.append((d, m)))
        actions = config_watcher.poll_once({"mcp_servers": path})
        assert actions["mcp_servers"] == "reloaded", f"首轮应建立基线并重载: {actions}"
        assert len(calls) == 1 and calls[0][0]["servers"] == {"a": {"command": "x"}}
        assert calls[0][1]["servers_changed"] is True, "首轮无 servers hash 基线，应视为变更"
        # 未变化 → 跳过
        actions = config_watcher.poll_once({"mcp_servers": path})
        assert actions["mcp_servers"] == "unchanged"
        assert len(calls) == 1
    print("PASS: 首轮建立基线并派发回调，未变化时跳过")


def test_invalid_json_keeps_state_for_retry():
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mcp_servers.json"
        path.write_text(json.dumps({"servers": {"a": {}}}), encoding="utf-8")
        config_watcher.register_reload_callback("mcp_servers", "recorder", lambda d, m: calls.append(d))
        assert config_watcher.poll_once({"mcp_servers": path})["mcp_servers"] == "reloaded"
        # 写入半截 JSON（模拟文件写了一半被读到）
        _wait_filesystem()
        path.write_text('{"servers": {"a": ', encoding="utf-8")
        assert config_watcher.poll_once({"mcp_servers": path})["mcp_servers"] == "failed"
        assert len(calls) == 1, "解析失败不应派发回调"
        # 修好后自动重试成功
        _wait_filesystem()
        path.write_text(json.dumps({"servers": {"a": {}}, "inputs": {"a": ["t"]}}), encoding="utf-8")
        assert config_watcher.poll_once({"mcp_servers": path})["mcp_servers"] == "reloaded"
        assert len(calls) == 2 and calls[1]["inputs"] == {"a": ["t"]}
    print("PASS: 解析失败保留内存现状，修复后自动重试")


def test_servers_changed_flag_distinguishes_inputs_only():
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mcp_servers.json"
        path.write_text(json.dumps({
            "servers": {"a": {"command": "x"}},
            "inputs": {"a": []},
        }), encoding="utf-8")
        config_watcher.register_reload_callback("mcp_servers", "recorder", lambda d, m: calls.append(m))
        config_watcher.poll_once({"mcp_servers": path})
        assert len(calls) == 1
        # 只改 inputs：不触发工具重探
        _wait_filesystem()
        path.write_text(json.dumps({
            "servers": {"a": {"command": "x"}},
            "inputs": {"a": ["tool1"]},
        }), encoding="utf-8")
        actions = config_watcher.poll_once({"mcp_servers": path})
        assert actions["mcp_servers"] == "reloaded"
        assert calls[-1]["servers_changed"] is False, "仅 inputs 变化不应触发工具重探"
        # servers 变化：触发工具重探
        _wait_filesystem()
        path.write_text(json.dumps({
            "servers": {"a": {"command": "x"}, "b": {"command": "y"}},
            "inputs": {"a": ["tool1"], "b": []},
        }), encoding="utf-8")
        config_watcher.poll_once({"mcp_servers": path})
        assert calls[-1]["servers_changed"] is True, "servers 变化应触发工具重探"
    print("PASS: servers_changed 标志区分 inputs-only 与 servers 变更")


def test_callback_exception_does_not_break_polling():
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "models.json"
        path.write_text(json.dumps({"model_selection": {}}), encoding="utf-8")

        def _boom(_data, _meta):
            raise RuntimeError("回调内部错误")

        config_watcher.register_reload_callback("models", "boom", _boom)
        config_watcher.register_reload_callback("models", "recorder", lambda d, m: calls.append(d))
        actions = config_watcher.poll_once({"models": path})
        assert actions["models"] == "reloaded", "回调异常不应影响整体重载判定"
        assert len(calls) == 1, "回调异常不应影响后续回调执行"
    print("PASS: 单个回调异常不影响轮询与其余回调")


if __name__ == "__main__":
    test_first_poll_primes_and_dispatches()
    test_invalid_json_keeps_state_for_retry()
    test_servers_changed_flag_distinguishes_inputs_only()
    test_callback_exception_does_not_break_polling()
    print("ALL PASS")
