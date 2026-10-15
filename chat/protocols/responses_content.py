"""Responses input conversion. Application attachments remain unchanged on disk."""
import copy
import json

from .errors import ResponsesProtocolError

DOCUMENT_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".html",
    ".xml", ".log", ".py", ".js", ".ts", ".css", ".doc", ".docx", ".rtf",
    ".odt", ".ppt", ".pptx", ".xls", ".xlsx",
}

# Official Responses input item schemas. Output metadata stays in the
# private journal; it is not necessarily accepted as a creation input field.
NATIVE_INPUT_FIELDS = {
    "compaction": {"type", "id", "encrypted_content"},
    "reasoning": {"type", "id", "summary", "content", "encrypted_content", "status"},
    "message": {"type", "id", "role", "content", "status", "phase"},
    "function_call": {"type", "id", "call_id", "name", "arguments", "namespace", "status"},
    "function_call_output": {"type", "id", "call_id", "output", "status"},
}


def native_input_items(items):
    """Project full native output windows to the official input schema."""
    result = []
    for item in items:
        allowed = NATIVE_INPUT_FIELDS.get(item.get("type"))
        projected = {key: copy.deepcopy(value) for key, value in item.items()
                     if allowed is None or key in allowed}
        # Older local/raw rebuilds staged assistant strings as input_text.
        # Repair those persisted windows too, without rewriting native output
        # parts (including their annotations, refusal and phase metadata).
        if projected.get("role") == "assistant":
            for part in projected.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "input_text":
                    part["type"] = "output_text"
                    part.setdefault("annotations", [])
        result.append(projected)
    return result


def supported_document_types(declared):
    return [value for value in (declared or []) if str(value).lower() in DOCUMENT_EXTENSIONS]


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def content_parts(content, session_id=None, *, role="user"):
    if content is None:
        return []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    elif not isinstance(content, list):
        content = [{"type": "text", "text": _text(content)}]
    result = []
    for part in content:
        if not isinstance(part, dict):
            result.append({"type": "input_text", "text": _text(part)})
            continue
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            text = str(part.get("text") or "")
            if text:
                result.append({"type": "input_text", "text": text})
        elif kind == "image_url":
            image = part.get("image_url") or {}
            image = {"url": image} if isinstance(image, str) else image
            url = image.get("url") or ""
            if url.startswith("media://") and session_id:
                from memory.file_memory import load_session_media_model_part
                resolved = load_session_media_model_part(session_id, url, api_type="responses")
                if not resolved:
                    raise ResponsesProtocolError("Responses 图片引用无法读取")
                result.extend(content_parts([resolved.get("part", resolved)], session_id))
                continue
            if not url.startswith(("data:", "http://", "https://")):
                raise ResponsesProtocolError("Responses 图片需要 data URL 或 http(s) URL")
            result.append({"type": "input_image", "image_url": url,
                           "detail": image.get("detail") or "auto"})
        elif kind == "image":
            source = part.get("source") or {}
            if source.get("type") == "base64":
                url = "data:" + str(source.get("media_type")) + ";base64," + str(source.get("data") or "")
            elif source.get("type") == "url":
                url = source.get("url")
            else:
                raise ResponsesProtocolError("Responses 图片 source 不受支持")
            result.append({"type": "input_image", "image_url": url, "detail": "auto"})
        elif kind in {"file", "input_file"}:
            file = part.get("file") if kind == "file" else part
            if not isinstance(file, dict):
                raise ResponsesProtocolError("Responses 文档缺少 file 数据")
            fields = {key: copy.deepcopy(file[key]) for key in
                      ("file_data", "file_id", "file_url", "filename", "detail") if file.get(key) is not None}
            if not any(fields.get(key) for key in ("file_data", "file_id", "file_url")):
                raise ResponsesProtocolError("Responses 文档缺少 file_data/file_id/file_url")
            result.append({"type": "input_file", **fields})
        elif kind == "document":
            source = part.get("source") or {}
            if source.get("type") == "base64":
                result.append({"type": "input_file", "filename": part.get("title") or "document.pdf",
                               "file_data": "data:" + str(source.get("media_type")) + ";base64," + str(source.get("data") or "")})
            elif source.get("type") == "url":
                result.append({"type": "input_file", "file_url": source.get("url")})
            elif source.get("type") == "text":
                result.append({"type": "input_text", "text": str(source.get("data") or "")})
            else:
                raise ResponsesProtocolError("Responses 文档 source 不受支持")
        elif kind == "input_image":
            result.append({key: copy.deepcopy(value) for key, value in part.items()
                           if key in {"type", "image_url", "file_id", "detail"}})
        elif kind in {"video_url", "input_audio"}:
            raise ResponsesProtocolError("Responses 此模型调用路径不支持原生音频/视频，请使用图片或文本输入")
        else:
            raise ResponsesProtocolError("Responses 不支持内容类型 " + str(kind))
    if role == "assistant":
        for part in result:
            if part["type"] == "input_text":
                part["type"] = "output_text"
                part["annotations"] = []
    return result


def convert_messages(messages, session_id=None, *, delta=False):
    items = []
    outputs = {}
    for message in messages:
        data = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else message
        role = data.get("role")
        if role == "assistant":
            if delta:
                continue  # Its actual output already belongs to the remote response.
            parts = content_parts(data.get("content"), session_id, role=role)
            if parts:
                items.append({"type": "message", "role": "assistant", "content": parts})
            for call in data.get("tool_calls") or []:
                function = call.get("function") or {}
                if not call.get("id") or not function.get("name"):
                    raise ResponsesProtocolError("Responses 历史工具调用缺少 ID 或名称")
                arguments = function.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": function["name"], "arguments": arguments})
        elif role == "tool":
            if not data.get("tool_call_id"):
                raise ResponsesProtocolError("Responses 工具结果缺少 call_id")
            output = data.get("content")
            if isinstance(output, list):
                output = content_parts(output, session_id)
            elif output is None:
                output = ""
            elif not isinstance(output, str):
                output = _text(output)
            item = {"type": "function_call_output", "call_id": data["tool_call_id"], "output": output}
            items.append(item)
            outputs[data["tool_call_id"]] = item
        elif role in {"user", "system", "developer"}:
            attachment_call = data.get("tool_call_id")
            if role == "user" and attachment_call in outputs:
                item = outputs[attachment_call]
                base = item["output"]
                item["output"] = (content_parts(base, session_id) if isinstance(base, list) else
                                  [{"type": "input_text", "text": str(base or "")}])
                item["output"].extend(content_parts(data.get("content"), session_id))
                continue
            raw_parts = data.get("content")
            mapped = [part for part in raw_parts if isinstance(part, dict) and part.get("_responses_call_id") in outputs] if isinstance(raw_parts, list) else []
            if role == "user" and mapped:
                for part in mapped:
                    item = outputs[part["_responses_call_id"]]
                    if not isinstance(item["output"], list):
                        item["output"] = [{"type": "input_text", "text": str(item["output"] or "")}]
                    item["output"].extend(content_parts([part], session_id))
                continue
            parts = content_parts(data.get("content"), session_id)
            if parts:
                items.append({"role": "developer" if role == "system" else role, "content": parts})
        else:
            raise ResponsesProtocolError("Responses 不支持消息角色 " + str(role))
    if not delta:
        declared = {item["call_id"] for item in items if item.get("type") == "function_call"}
        returned = {item["call_id"] for item in items if item.get("type") == "function_call_output"}
        repaired = []
        for item in items:
            if item.get("type") == "function_call" and item["call_id"] not in returned:
                repaired.append({"role": "assistant", "content": [{"type": "output_text", "annotations": [],
                    "text": "历史中曾请求工具 " + item["name"] + "，没有确认执行结果。请先核对实际效果，避免重复执行有副作用的操作。参数：" + item["arguments"]}]})
            elif item.get("type") == "function_call_output" and item["call_id"] not in declared:
                output = item["output"]
                repaired.append({"role": "user", "content": [
                    {"type": "input_text", "text": "以下是历史工具反馈（不代表新的执行请求）："},
                    *(output if isinstance(output, list) else [{"type": "input_text", "text": output}])]})
            else:
                repaired.append(item)
        items = repaired
    return items
