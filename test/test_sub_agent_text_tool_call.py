import asyncio
import json
from unittest.mock import patch

import pytest

from factory.agent_runtime import sub_agent
from test_sub_agent import _make_retry_context, _retry_limits_patch


@pytest.mark.parametrize("stuck", [False, True])
def test_sub_agent_text_todo_is_hidden_and_has_bounded_recovery(stuck):
    snapshots = []
    markup = '@@TodoWrite@@{"todos":[]}'

    async def reply(request=None, **kwargs):
        snapshots.append(request)
        assert len(snapshots) <= 3
        content = markup if stuck or len(snapshots) == 1 else "真实进展报告"
        for char in content:
            yield 'data: ' + json.dumps({"content": char}, ensure_ascii=False) + '\n\n'
        yield 'data: {"finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'

    plan = [{"id": "1", "content": "保持原计划", "status": "pending"}]
    ctx, emitted = _make_retry_context(initial_todo=plan)
    with patch.object(sub_agent.ChatLLM, "chat_completions", reply), _retry_limits_patch(todo_remind_max=0):
        runner = sub_agent.SubAgentRunner(ctx)
        result = asyncio.run(runner.run())
    assert len(snapshots) == (3 if stuck else 2)
    assert result.status == ("error" if stuck else "done")
    assert runner.todo == plan
    assert not runner._todo_touched
    assert all(markup not in str(p.get("content", p.get("content_delta", ""))) for _, p in emitted)
    assert any(p.get("retry_kind") == "text_tool_call" for _, p in emitted)


def test_sub_agent_updated_plan_replaces_preset_hint_with_native_result():
    old = [{"id": "1", "content": "旧步骤", "status": "pending"}]
    new = [{"id": "1", "content": "新步骤", "status": "done"}]
    ctx, _emitted = _make_retry_context(initial_todo=old)
    runner = sub_agent.SubAgentRunner(ctx)
    call = {"id": "update", "type": "function", "function": {
        "name": "todo_write", "arguments": json.dumps({"todos": new}, ensure_ascii=False)}}
    runner.messages.append({"role": "assistant", "tool_calls": [call]})
    results = asyncio.run(runner._execute_tool_round([call]))
    runner.messages.append({"role": "tool", "tool_call_id": "update", "_tool_name": "todo_write",
                            "content": json.dumps(results[0]["result"], ensure_ascii=False)})
    request = runner._build_request({})
    assert not any("旧步骤" in str(m.content) for m in request.messages)
    assert not any("新步骤" in str(m.content) for m in request.messages if m.role == "system")
    assert json.loads(next(m.content for m in request.messages if m.role == "tool"))["todos"] == new
