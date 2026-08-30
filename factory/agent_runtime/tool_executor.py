"""MCP 工具调用的归一化、解析和并发执行。"""
import asyncio
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List

from util.mcp_client import call_mcp_tool


@dataclass
class ToolExecutionPlan:
    parsed_tools: list[tuple[int, dict, str, dict]]
    over_task_call: tuple[int, dict, str, dict] | None
    has_parse_error: bool


def parse_tool_call(tool_call: dict) -> tuple[str | None, dict]:
    if not isinstance(tool_call, dict):
        return None, {}
    function_info = tool_call.get("function") or {}
    tool_name = function_info.get("name")
    arguments = function_info.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {"arguments": arguments}
    return tool_name, arguments


def split_concatenated_tool_names(name: str, known_names: List[str]) -> List[str] | None:
    if not isinstance(name, str) or not name:
        return None
    sorted_names = sorted(set(known_names), key=len, reverse=True)
    pos = 0
    parts: List[str] = []
    while pos < len(name):
        matched = None
        for candidate in sorted_names:
            if name.startswith(candidate, pos):
                matched = candidate
                break
        if not matched:
            return None
        parts.append(matched)
        pos += len(matched)
    return parts if len(parts) >= 2 else None


def split_concatenated_json_objects(arguments: str) -> List[Any]:
    if not isinstance(arguments, str):
        return []
    decoder = json.JSONDecoder()
    text = arguments.strip()
    results: List[Any] = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, next_pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            return []
        results.append(obj)
        pos = next_pos
    return results


def normalize_tool_calls(tool_calls: List[dict], known_names: List[str]) -> List[dict]:
    if not isinstance(tool_calls, list):
        return []
    normalized: List[dict] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        function_info = tc.get("function") or {}
        tool_name = function_info.get("name")
        arguments = function_info.get("arguments")
        if tool_name in known_names:
            normalized.append(tc)
            continue
        split_names = split_concatenated_tool_names(tool_name, known_names)
        if not split_names:
            normalized.append(tc)
            continue
        split_args = split_concatenated_json_objects(arguments) if isinstance(arguments, str) else []
        if split_args and len(split_args) == len(split_names):
            arg_texts = [arg if isinstance(arg, str) else json.dumps(arg, ensure_ascii=False) for arg in split_args]
        else:
            arg_texts = ["{}" for _ in split_names]
        base_id = tc.get("id") or f"call_{uuid.uuid4().hex}"
        base_index = tc.get("index", len(normalized))
        base_type = tc.get("type", "function")
        for i, split_name in enumerate(split_names):
            normalized.append({
                "index": base_index + i,
                "id": f"{base_id}_split_{i + 1}",
                "type": base_type,
                "function": {
                    "name": split_name,
                    "arguments": arg_texts[i],
                },
            })
    return normalized


def prepare_tool_execution(tool_calls: List[dict], known_names: List[str]) -> ToolExecutionPlan:
    parsed_tools = []
    over_task_call = None
    has_parse_error = False
    for i, tool_call in enumerate(tool_calls, 1):
        tool_name, tool_args = parse_tool_call(tool_call)
        if not tool_name:
            continue
        if tool_name == "over_task":
            over_task_call = (i, tool_call, tool_name, tool_args)
        else:
            func_info = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
            arguments_str = func_info.get("arguments", "")
            if arguments_str and not tool_args and str(arguments_str).strip() not in {"{}", "[]", "null"}:
                has_parse_error = True
            parsed_tools.append((i, tool_call, tool_name, tool_args))
    return ToolExecutionPlan(parsed_tools=parsed_tools, over_task_call=over_task_call, has_parse_error=has_parse_error)


def _invoke_tool_function(name: str, arguments: dict, tool_mcp_servers: Dict[str, str]) -> Any:
    if name not in tool_mcp_servers:
        raise ValueError(f"工具 {name} 未在MCP服务器中注册，无法调用")
    mcp_server_file = tool_mcp_servers[name]
    pipe_tools = {"setup_pipe", "run_pipe_command", "read_pipe_history"}
    max_retry = 3 if name in pipe_tools else 1
    last_error = None
    for attempt in range(1, max_retry + 1):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(call_mcp_tool(
                function_name=name,
                arguments=arguments,
                mcp_service=mcp_server_file))
        except Exception as exc:
            last_error = exc
            err_text = str(exc)
            should_retry = (
                "Failed to connect to named-pipe server" in err_text
                and attempt < max_retry
            )
            if should_retry:
                time.sleep(0.4 * attempt)
                continue
            raise
        finally:
            loop.close()
    if last_error is not None:
        raise last_error


def execute_tool_round(
    parsed_tools: list[tuple[int, dict, str, dict]],
    tool_mcp_servers: Dict[str, str],
    max_workers: int,
) -> list[dict[str, Any]]:
    tool_results: list[dict[str, Any]] = []

    def execute_tool(index, tool_call, tool_name, tool_args) -> tuple:
        try:
            result = _invoke_tool_function(tool_name, tool_args, tool_mcp_servers)
            ret = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return index, tool_call, tool_name, tool_args, ret, None
        except Exception as exc:
            ret = f"工具执行失败【{exc}】"
            return index, tool_call, tool_name, tool_args, ret, exc

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(execute_tool, idx, tc, tn, ta): (idx, tc, tn, ta)
            for idx, tc, tn, ta in parsed_tools
        }
        for future in as_completed(futures):
            idx, tc, tn, ta = futures[future]
            try:
                index, tool_call, tool_name, tool_args, ret, error = future.result()
            except Exception as exc:
                tool_results.append({
                    "index": idx,
                    "tool_call": tc,
                    "tool_name": tn,
                    "tool_args": ta,
                    "result": f"工具执行异常：{exc}",
                    "error": exc,
                })
                continue
            tool_results.append({
                "index": index,
                "tool_call": tool_call,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "result": ret,
                "error": error,
            })
    tool_results.sort(key=lambda item: item["index"])
    return tool_results
