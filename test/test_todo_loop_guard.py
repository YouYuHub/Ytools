"""计划空转保护的批次语义和任务隔离。"""
from factory.agent_runtime.todo_runtime import TodoLoopGuard


def test_multi_call_batch_counts_once_and_other_tool_resets():
    guard = TodoLoopGuard()
    for _ in range(3):
        assert not guard.observe(["todo_write", "todo_write"])
    assert guard.suspended
    assert not guard.observe(["todo_write", "read_file"])
    assert guard.streak == 0


def test_guard_is_per_task_and_new_user_instruction_resets():
    guard = TodoLoopGuard()
    for _ in range(3):
        guard.observe(["todo_write"])
    assert not TodoLoopGuard().suspended
    messages = [{"role": "user", "content": "任务"}]
    guard.update_context(messages)
    guard.update_context(messages)
    assert len(messages) == 2  # 提示不随每轮请求无限累积
    guard.reset()
    guard.update_context(messages)
    assert messages == [{"role": "user", "content": "任务"}]


def test_filter_does_not_mutate_user_tool_selection():
    tools = [{"function": {"name": name}} for name in ["todo_write", "read_file"]]
    guard = TodoLoopGuard()
    for _ in range(3):
        guard.observe(["todo_write"])
    assert guard.filter_tools(tools) == tools[1:]
    assert len(tools) == 2
