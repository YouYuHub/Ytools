"""Real image/PDF fixtures, request conversion, replay and failure regression."""
import asyncio
import base64
import copy
import io
import json

import pytest
from PIL import Image

import env_manager
from chat.chat_llm import ChatLLM, _estimate_payload_input_tokens
from chat.protocols import native_store
from chat.protocols.messages import MessagesProtocolError, MessagesStream, build_payload, normalize_response
from chat.protocols.messages_content import content_blocks, validate_payload
from factory.agent_runtime import builtin_tools
from factory.agent_runtime.title_generator import build_title_content
from memory import file_memory
from test_messages_api import CONFIG, TOOLS, SIGNED, Writer, request, response, frames, http, sse, sockets, decode, isolated  # noqa: F401


def image_bytes(format="PNG", size=(8, 6)):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=format)
    return output.getvalue()


def image_part(format="PNG", size=(8, 6)):
    data = image_bytes(format, size)
    mime = Image.MIME[format]
    return {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}}


def pdf_bytes(pages=1, encrypted=False):
    fitz = pytest.importorskip("fitz")
    with fitz.open() as doc:
        for _ in range(pages):
            doc.new_page().insert_text((72, 72), "Real PDF test")
        return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="password") if encrypted else doc.tobytes()


def file_part(name="manual.pdf", data=None):
    data = pdf_bytes() if data is None else data
    mime = "application/pdf" if name.endswith(".pdf") else "text/plain"
    return {"type": "file", "file": {"filename": name, "file_data": "data:" + mime + ";base64," + base64.b64encode(data).decode()}}


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"])
def test_valid_images_convert_without_mutating_history(format):
    part = image_part(format)
    source = [{"role": "user", "content": [part, {"type": "text", "text": "描述这张图"}]}]
    original = copy.deepcopy(source)
    payload, _, _ = build_payload(request(messages=source), CONFIG, True)
    blocks = payload["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] in {"image/png", "image/jpeg", "image/gif", "image/webp"}
    assert blocks[1] == {"type": "text", "text": "描述这张图"}
    assert source == original


def test_mixed_image_pdf_and_native_document_metadata():
    pdf = file_part()
    pdf.update(context="附件上下文", citations={"enabled": True})
    parts = [image_part(), pdf, {"type": "text", "text": "比较图片和文档"}]
    payload, _, _ = build_payload(request(messages=[{"role": "user", "content": parts}]), CONFIG, False)
    blocks = payload["messages"][0]["content"]
    assert [block["type"] for block in blocks] == ["image", "document", "text"]
    assert blocks[1]["source"]["media_type"] == "application/pdf"
    assert blocks[1]["title"] == "manual.pdf" and blocks[1]["context"] == "附件上下文"
    assert blocks[1]["citations"] == {"enabled": True}
    assert not {"file", "image_url"} & blocks[1].keys()


@pytest.mark.parametrize("kind", ["image", "document"])
@pytest.mark.parametrize("source", [{"type": "url", "url": "https://files.test/asset"}, {"type": "file", "file_id": "file_test"}])
def test_native_url_and_files_references_pass_through(kind, source):
    part = {"type": kind, "source": source, "cache_control": {"type": "ephemeral"}}
    assert content_blocks([part])[0] == part


def test_text_files_decode_to_text_document_and_custom_content_is_preserved():
    part = file_part("notes.md", "文档内容".encode("utf-16"))
    block = content_blocks([part])[0]
    assert block["source"] == {"type": "text", "media_type": "text/plain", "data": "文档内容"}
    custom = {"type": "document", "source": {"type": "content", "content": [{"type": "text", "text": "段落"}, image_part()]}, "title": "自定义"}
    block = content_blocks([custom])[0]
    assert [item["type"] for item in block["source"]["content"]] == ["text", "image"]


def test_structured_tool_results_keep_images_and_documents():
    messages = [{"role": "user", "content": "读取附件"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "tool_a", "function": {"name": "work", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "tool_a", "content": [image_part(), file_part()], "tool_is_error": True}]
    payload, _, _ = build_payload(request(messages=messages, tools=TOOLS), CONFIG, True)
    result = payload["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"]
    assert [block["type"] for block in result["content"]] == ["image", "document"]
    payload, _, _ = build_payload(request(messages=messages, tools=TOOLS, thinking={"type": "adaptive"}), CONFIG, True)
    facts = payload["messages"][-1]["content"]
    assert [block["type"] for block in facts] == ["text", "image", "document"]


def test_native_replay_preserves_attachment_prefix_and_tool_result_blocks():
    req = request(messages=[{"role": "user", "content": [image_part(), file_part()]}], tools=TOOLS, thinking={"type": "adaptive"})
    first, _, _ = build_payload(req, CONFIG, True)
    native = response(copy.deepcopy(SIGNED), "tool_use")
    ref = native_store.save_record(req.session_id, CONFIG, first, native)
    native_store.tool_completed(req.session_id, ref, "tool_a", [image_part(), file_part()])
    native_store.tool_completed(req.session_id, ref, "tool_b", "done")
    source = [{"role": "user", "content": "已被界面整理的前缀"}, {"role": "assistant", "content": "开始", "provider_state": ref}]
    second, parent, _ = build_payload(request(messages=source, tools=[]), CONFIG, True)
    assert second["messages"][:len(first["messages"])] == first["messages"]
    assert second["messages"][-2]["content"] == SIGNED
    assert [block["type"] for block in second["messages"][-1]["content"][0]["content"]] == ["image", "document"]
    assert parent["record_id"] == ref["record_id"]


@pytest.mark.parametrize("part", [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not base64"}},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
    {"type": "document", "source": {"type": "url", "url": "file:///secret.pdf"}},
    {"type": "document", "source": {"type": "file", "file_id": ""}},
    {"type": "document", "source": {"type": "base64", "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": "AAAA"}},
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
])
def test_bad_attachments_fail_before_network_and_never_retry(part):
    req = request(messages=[{"role": "user", "content": [part]}])
    events = decode(list(ChatLLM.std_completions_sse(req, model_config=CONFIG)))
    assert len(events) == 1
    assert events[0]["error_type"] == "protocol" and events[0]["non_retryable"]
    assert not events[0]["retrying"] and events[0]["discard_tool_calls"]


@pytest.mark.parametrize("limit", [{"max_request_bytes": 50}, {"max_image_bytes": 2}, {"max_images": 1}])
def test_request_size_image_size_and_count_limits(limit):
    req = request(messages=[{"role": "user", "content": [image_part(), image_part()]}])
    with pytest.raises(MessagesProtocolError):
        build_payload(req, dict(CONFIG, messagesLimits=limit), True)


def test_pdf_page_limit_and_encryption_are_checked():
    with pytest.raises(MessagesProtocolError, match="总页数"):
        build_payload(request(messages=[{"role": "user", "content": [file_part(data=pdf_bytes(2)), file_part(data=pdf_bytes(2))]}]), dict(CONFIG, messagesLimits={"max_pdf_pages": 3}), True)
    with pytest.raises(MessagesProtocolError, match="加密"):
        build_payload(request(messages=[{"role": "user", "content": [file_part(data=pdf_bytes(encrypted=True))]}]), CONFIG, True)


def test_many_image_dimensions_and_citation_constraints():
    part = image_part(size=(2100, 1))
    with pytest.raises(MessagesProtocolError, match="2000px"):
        build_payload(request(messages=[{"role": "user", "content": [part] * 21}]), CONFIG, True)
    first = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "a"}, "citations": {"enabled": True}}
    second = dict(first, citations={"enabled": False})
    with pytest.raises(MessagesProtocolError, match="同时启用"):
        build_payload(request(messages=[{"role": "user", "content": [first, second]}]), CONFIG, True)
    with pytest.raises(MessagesProtocolError, match="不能同时"):
        build_payload(request(messages=[{"role": "user", "content": [first]}], output_config={"format": {"type": "json_schema", "schema": {}}}), CONFIG, True)


def test_citations_delta_and_response_projection_preserve_locations():
    citation = {"type": "page_location", "document_index": 0, "document_title": "手册", "start_page_number": 1, "end_page_number": 2, "cited_text": "说明"}
    state = MessagesStream()
    events = frames([{"type": "text", "text": "据文档说明"}])
    events[1]["content_block"]["citations"] = None
    events.insert(3, {"type": "content_block_delta", "index": 0, "delta": {"type": "citations_delta", "citation": citation}})
    for event in events:
        result = state.feed(event["type"], event)
    assert result[0]["content"] == "据文档说明"
    assert result[0]["citations"] == [{"content_block_index": 0, "text_start": 0, "text_end": 5, "citation": citation}]
    assert state.response["content"][0]["citations"] == [citation]
    assert normalize_response(state.response)["citations"] == result[0]["citations"]


def test_gif_first_frame_conversion_preserves_transparency():
    image = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    image.putpixel((1, 1), (255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="GIF", transparency=0)
    part = {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": base64.b64encode(buffer.getvalue()).decode()}}
    block = content_blocks([part])[0]
    with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as converted:
        assert converted.convert("RGBA").getpixel((0, 0))[3] == 0


def test_native_attachment_token_estimates_exclude_binary_but_count_text():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 1000000}}
    pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "A" * 1000000}}
    text = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "文档" * 4000}}
    payload = {"messages": [{"role": "user", "content": [image, {"type": "tool_result", "content": [pdf, text]}]}]}
    assert 8000 <= _estimate_payload_input_tokens(payload) < 15000
    assert "AAAA" not in file_memory.content_part_to_text([image, pdf, text])
    assert "文档文档" in file_memory.content_part_to_text([text])


def test_documents_are_independent_of_vision_flag_and_system_rejects_images():
    parts = [file_part(), {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "正文"}}, image_part()]
    resolved, missing = file_memory.resolve_media_content_parts("no_vision", parts, vision_enabled=False, api_type="messages")
    assert resolved[:2] == parts[:2] and not missing
    assert resolved[-1]["type"] == "text"
    with pytest.raises(MessagesProtocolError, match="user"):
        build_payload(request(messages=[{"role": "system", "content": [image_part()]}, {"role": "user", "content": "hi"}]), CONFIG, True)


def test_messages_gif_upload_and_read_media_do_not_transcode_to_video(monkeypatch, tmp_path):
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(file_memory, "_load_converted_media_base64", lambda *a, **k: pytest.fail("Messages must not run GIF-to-video pipeline"))
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(buffer, format="GIF", save_all=True, append_images=[Image.new("RGB", (12, 8), "blue")], duration=100, loop=0)
    saved = file_memory.save_session_media("gif_test", "motion.gif", buffer.getvalue())
    part = {"type": "image_url", "image_url": {"url": saved["media_ref"]}}
    resolved, missing = file_memory.resolve_media_content_parts("gif_test", [part], api_type="messages", vision_enabled=True)
    assert not missing and resolved[0]["image_url"]["url"].startswith("data:image/png;base64,")
    loaded = builtin_tools.load_any_media_model_part("gif_test", saved["media_ref"], api_type="messages")
    assert loaded["kind"] == "image" and loaded["converted"]["first_frame_only"]
    result = builtin_tools.execute_read_media({"references": [saved["media_ref"]]}, "gif_test", api_type="messages", vision_enabled=True)
    assert result["ok"] and result["loaded"][0]["kind"] == "image"
    title = build_title_content("看图", "回答", question_parts=[part], session_id="gif_test", vision_enabled=True, api_type="messages")
    assert any(block.get("type") == "image_url" for block in title)


def test_missing_media_and_unsupported_audio_video_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path)
    parts = [{"type": "image_url", "image_url": {"url": "media://missing.png"}}, {"type": "video_url", "video_url": {"url": "media://missing.mp4"}}]
    resolved, missing = file_memory.resolve_media_content_parts("missing", parts, api_type="messages")
    assert missing == ["media://missing.png"]
    assert all(block["type"] == "text" for block in resolved)
    for reference in ("https://media.test/a.mp4", "https://media.test/a.wav"):
        info = builtin_tools.load_any_media_model_part("missing", reference, api_type="messages")
        assert "不支持" in info["error"]
    gif = builtin_tools.load_any_media_model_part("missing", "https://media.test/a.gif", api_type="messages")
    assert gif["kind"] == "image" and gif["part"]["image_url"]["url"].endswith("a.gif")


def test_network_retry_resends_complete_attachments(monkeypatch):
    req = request(messages=[{"role": "user", "content": [image_part(), file_part()]}])
    values = sockets(monkeypatch, [http(b"overloaded", 529), http(sse(frames()))])
    result = decode(list(ChatLLM.std_completions_sse(req, model_config=CONFIG)))
    assert values[0].payload()["messages"] == values[1].payload()["messages"]
    assert any(event.get("retrying") for event in result)
    assert result[-1]["finish_reason"] == "stop"


def test_model_capabilities_filter_office_types_and_sub_agent_keeps_images(monkeypatch):
    model = {"apiType": "messages", "vision": True, "supportDocTypes": ["pdf", "docx", "xlsx", "txt", "md"]}
    assert env_manager._model_document_types(model, {}) == [".pdf", ".txt", ".md"]
    from factory.agent_runtime import sub_agent
    monkeypatch.setattr(sub_agent, "get_role_selection", lambda role: {"ownership_name": "P", "model_name": "Claude"})
    monkeypatch.setattr(sub_agent, "get_model_config", lambda *a: dict(CONFIG, vision=True))
    assert sub_agent.resolve_sub_agent_vision_enabled(False)


def test_messages_read_media_schema_only_advertises_image_arguments():
    tool = builtin_tools.build_read_media_tool_definition()
    original = copy.deepcopy(tool)
    payload, _, _ = build_payload(request(tools=[tool]), CONFIG, True)
    native = payload["tools"][0]
    assert "GIF 仅读取首帧" in native["description"]
    assert not {"start_time", "end_time"} & native["input_schema"]["properties"].keys()
    assert tool == original


@pytest.mark.parametrize("runtime", ["main", "sub_agent"])
def test_runtime_read_media_and_read_document_inject_native_attachments(monkeypatch, tmp_path, runtime):
    from chat.protocols.messages_content import walk_blocks
    from factory.agent_runtime import sub_agent
    import test_todo_no_tools_regression as harness
    from test_sub_agent import _make_context
    from unittest.mock import AsyncMock

    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "uploads")
    config = dict(CONFIG, vision=True, support_doc_types=[".pdf"], maxInputTokens=128000)
    context, _, _ = _make_context()
    session_id = harness.TEST_SESSION if runtime == "main" else context.session_id
    png = file_memory.save_session_media(session_id, "photo.png", image_bytes())
    gif = file_memory.save_session_media(session_id, "motion.gif", image_bytes("GIF"))
    gif_path = str(file_memory.resolve_media_path(session_id, gif["media_ref"]))
    pdf = pdf_bytes()
    saved = file_memory.save_session_document(session_id, "manual.pdf", pdf)
    file_memory.FileMemoryManager(session_id).add_file_memory({"filename": "manual.pdf", "type": "pdf", "content": "Real PDF test", "size": len(pdf), "stored_name": saved["stored_name"]})
    calls = copy.deepcopy(SIGNED[:2]) + [
        {"type": "tool_use", "id": "read_image", "name": "read_media", "input": {"references": [gif_path]}},
        {"type": "tool_use", "id": "read_pdf", "name": "read_document", "input": {"filename": "manual.pdf", "mode": "native"}},
    ]
    scripts = iter([frames(calls, "tool_use"), frames()])
    writers = []
    async def connect(*args):
        reader = asyncio.StreamReader()
        reader.feed_data(http(sse(next(scripts)), chunked=True))
        reader.feed_eof()
        writers.append(Writer())
        return reader, writers[-1]
    monkeypatch.setattr(ChatLLM, "_open_connection", connect)
    initial_content = [{"type": "text", "text": "分析图片和手册"}, {"type": "image_url", "image_url": {"url": png["media_ref"]}}]
    async def run():
        if runtime == "main":
            monkeypatch.setattr(harness._MemoryStub, "update_current_round_usage", AsyncMock(), raising=False)
            req = request(messages=[{"role": "user", "content": initial_content}], session_id=session_id, tool_names=["read_media", "read_document"])
            stream = await harness._run_case(["read_media", "read_document"], llm=ChatLLM, request_override=req, resolve_config=lambda: config)
            public = "".join(stream.chunks)
            assert "base64" not in json.dumps(harness._last_memory.records)
            assert "provider_state" not in public and "signature" not in public
        else:
            context.tools = [builtin_tools.build_read_media_tool_definition(), copy.deepcopy(builtin_tools.READ_DOCUMENT_TOOL_DEFINITION)]
            context.tool_servers = {"read_media": "__builtin__", "read_document": "__builtin__"}
            context.initial_todo = None
            context.parent_vision_enabled = True
            runner = sub_agent.SubAgentRunner(context)
            runner.messages[1]["content"] = initial_content
            monkeypatch.setattr(runner, "_resolve_request_model_config", lambda: (config, {}))
            monkeypatch.setattr(sub_agent, "resolve_sub_agent_vision_enabled", lambda _: True)
            result = await runner.run()
            assert result.status == "done"
        assert len(writers) == 2
        payloads = [json.loads(writer.sent.split(b"\r\n\r\n", 1)[1]) for writer in writers]
        assert payloads[1]["messages"][:len(payloads[0]["messages"])] == payloads[0]["messages"]
        blocks = list(walk_blocks(payloads[1]["messages"]))
        assert len([block for block in blocks if block["type"] == "image"]) == 2
        assert any(block["type"] == "document" and block["source"]["media_type"] == "application/pdf" for block in blocks)
        assert not any(block["type"] == "tool_result" and block.get("is_error") for block in blocks)
    asyncio.run(run())


def test_nonstream_documents_and_citations_use_existing_result_envelope(monkeypatch):
    cited = [{"type": "text", "text": "文档答案", "citations": [{"type": "char_location", "document_index": 0, "start_char_index": 0, "end_char_index": 2, "cited_text": "正文"}]}]
    sock = sockets(monkeypatch, [http(json.dumps(response(cited), ensure_ascii=False).encode(), chunked=True)])[0]
    result = ChatLLM.chat_completions(request(messages=[{"role": "user", "content": [file_part()]}]), stream=False, model_config=CONFIG)
    assert result["content"] == "文档答案" and result["finish_reason"] == "stop"
    assert result["citations"][0]["citation"]["document_index"] == 0
    assert sock.payload()["messages"][0]["content"][0]["type"] == "document"
