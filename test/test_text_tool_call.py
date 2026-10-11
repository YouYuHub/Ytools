import pytest

from factory.agent_runtime.text_tool_call import TextToolCallBuffer, text_tool_call_name


ENVELOPES = [
    '@@TodoWrite@@{"todos": [{"id":"1","content":"测试","status":"done"}]}',
    '<tool_calls><invoke name="todo_write"><parameter name="todos">[]</parameter></invoke></tool_calls>',
    '@@<tool_call>\n<tool_call>{"name":"todo_write","arguments":{"todos":[]}}</tool_call>',
]


@pytest.mark.parametrize("envelope", ENVELOPES)
def test_recorded_formats_hidden_for_every_stream_split(envelope):
    assert text_tool_call_name(envelope) == "todo_write"
    for split in range(len(envelope) + 1):
        buffer = TextToolCallBuffer()
        assert buffer.push(envelope[:split]) == ""
        assert buffer.push(envelope[split:]) == ""
        assert buffer.finish() == ("", "todo_write")


@pytest.mark.parametrize("prose", [
    "正常回答", "代码示例：@@TodoWrite@@{}", "`@@TodoWrite@@{}`", "```xml\n<tool_calls></tool_calls>\n```",
    ENVELOPES[0] + "\n这是正文调用的错误示例。", ENVELOPES[1] + "\n请不要使用这种写法。",
])
def test_normal_prose_and_examples_are_not_classified_as_calls(prose):
    buffer = TextToolCallBuffer()
    visible = "".join(buffer.push(char) for char in prose)
    held, name = buffer.finish()
    assert name is None
    assert visible + held == prose


@pytest.mark.parametrize("content", ['@@TodoWrite@@{broken', '<tool_calls><invoke name="todo_write">'])
def test_incomplete_envelopes_cannot_be_reported_as_success(content):
    buffer = TextToolCallBuffer()
    assert buffer.push(content) == ""
    assert buffer.finish() == ("", "<invalid>")
