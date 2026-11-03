# coding: utf-8
"""merge_tool_call_delta 回归：部分 OpenAI 兼容网关省略 index 字段。

背景：Iceman/OpenCode 网关的 tool_calls 增量不带 index，后端累积器
曾整条丢弃（`index is None → continue`），而同一份增量原样转发前端
渲染——后果是轮次直接 done（无 assistant、无工具执行），前端工具块
永远“无输出”。现缺 index 时按 id 归并/单槽语义累积，与前端一致。
"""
import pytest

from factory.agent_runtime.chat_runtime import merge_tool_call_delta


def test_indexed_deltas_unchanged():
    """标准 OpenAI 形状行为不变：按 index 归并。"""
    acc = []
    merge_tool_call_delta(acc, [
        {"index": 0, "id": "call_a", "function": {"name": "list", "arguments": "{\"a\":"}},
    ])
    merge_tool_call_delta(acc, [
        {"index": 0, "function": {"arguments": "1}"}},
        {"index": 1, "id": "call_b", "function": {"name": "read", "arguments": "{}"}},
    ])
    assert len(acc) == 2
    assert acc[0]["function"] == {"name": "list", "arguments": "{\"a\":1}"}
    assert acc[1]["id"] == "call_b"


def test_unindexed_single_call_split_deltas():
    """本轮故障形状：单调用分片、无 index、仅首片带 id。"""
    acc = []
    merge_tool_call_delta(acc, [
        {"id": "call_1", "type": "function",
         "function": {"name": "list_files", "arguments": "{\"dir_path\": \"C:/a"}},
    ])
    merge_tool_call_delta(acc, [
        {"function": {"arguments": "/b\"}"}},
    ])
    assert len(acc) == 1
    assert acc[0]["id"] == "call_1"
    assert acc[0]["index"] == 0
    assert acc[0]["function"]["name"] == "list_files"
    assert acc[0]["function"]["arguments"] == "{\"dir_path\": \"C:/a/b\"}"


def test_unindexed_complete_call_single_delta():
    """整调用一次到达、无 index：直接成一条完整调用。"""
    acc = []
    merge_tool_call_delta(acc, [
        {"id": "call_1", "function": {"name": "read_file", "arguments": "{\"p\":1}"}},
    ])
    assert len(acc) == 1
    assert acc[0]["function"] == {"name": "read_file", "arguments": "{\"p\":1}"}


def test_unindexed_no_id_merges_single_slot():
    """无 id 又无 index：单槽语义（与前端缺省 idx=0 一致）。"""
    acc = []
    merge_tool_call_delta(acc, [{"function": {"name": "li", "arguments": "{"}}])
    merge_tool_call_delta(acc, [{"function": {"name": "st", "arguments": "\"}"}}])
    assert len(acc) == 1
    assert acc[0]["function"] == {"name": "list", "arguments": "{\"}"}
    assert acc[0]["id"].startswith("call_")


def test_unindexed_parallel_calls_split_by_id():
    """多并行调用无 index：按 id 分槽，参数不互串。"""
    acc = []
    merge_tool_call_delta(acc, [
        {"id": "call_a", "function": {"name": "list_files", "arguments": "{\"a\":"}},
        {"id": "call_b", "function": {"name": "read_file", "arguments": "{\"b\":"}},
    ])
    merge_tool_call_delta(acc, [
        {"id": "call_a", "function": {"arguments": "1}"}},
        {"id": "call_b", "function": {"arguments": "2}"}},
    ])
    assert len(acc) == 2
    by_id = {tc["id"]: tc["function"] for tc in acc}
    assert by_id["call_a"] == {"name": "list_files", "arguments": "{\"a\":1}"}
    assert by_id["call_b"] == {"name": "read_file", "arguments": "{\"b\":2}"}


def test_unindexed_late_id_adopted():
    """首片无 id、后续才带 id：续接同一调用并认领 id。"""
    acc = []
    merge_tool_call_delta(acc, [{"function": {"name": "list_files", "arguments": "{\"a\":"}}])
    merge_tool_call_delta(acc, [{"id": "call_1", "function": {"arguments": "1}"}}])
    assert len(acc) == 1
    assert acc[0]["id"] == "call_1"
    assert acc[0]["function"]["arguments"] == "{\"a\":1}"


def test_unindexed_new_call_does_not_pollute_indexed_slot():
    """已有具名 0 号槽 + 新 id 携带名称：另开槽，不污染旧调用。"""
    acc = []
    merge_tool_call_delta(acc, [
        {"index": 0, "id": "call_a", "function": {"name": "list_files", "arguments": "{}"}},
    ])
    merge_tool_call_delta(acc, [
        {"id": "call_b", "function": {"name": "read_file", "arguments": "{\"p\":1}"}},
    ])
    assert len(acc) == 2
    by_id = {tc["id"]: tc["function"]["name"] for tc in acc}
    assert by_id == {"call_a": "list_files", "call_b": "read_file"}


def test_garbage_items_ignored():
    """非列表/非字典增量仍被忽略（原有守卫保留）。"""
    acc = []
    merge_tool_call_delta(acc, None)
    merge_tool_call_delta(acc, "not-a-list")
    merge_tool_call_delta(acc, [None, "x", 42])
    assert acc == []
