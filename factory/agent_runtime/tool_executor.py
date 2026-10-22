"""MCP 工具调用的归一化、解析和并发执行。"""
import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List

from util.mcp_client import call_mcp_tool


ARGUMENT_PARSE_ERROR = "__ytools_invalid_arguments__"


def _reject_json_constant(value):
    raise ValueError(f"JSON 不支持 {value}")


@dataclass
class ToolExecutionPlan:
    parsed_tools: list[tuple[int, dict, str, dict]]
    over_task_call: tuple[int, dict, str, dict] | None
    has_parse_error: bool


def parse_tool_call(tool_call: dict) -> tuple[str | None, dict]:
    if not isinstance(tool_call, dict):
        return None, {}
    function_info = tool_call.get("function") or {}
    if not isinstance(function_info, dict):
        return None, {}
    tool_name = function_info.get("name")
    arguments = function_info.get("arguments")
    if arguments is None:
        return tool_name, {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            return tool_name, {ARGUMENT_PARSE_ERROR: "arguments 必须是完整的 JSON 对象；请修正后重试"}
    if not isinstance(arguments, dict):
        return tool_name, {ARGUMENT_PARSE_ERROR: "arguments 必须是 JSON 对象，不能是数组、字符串或 null"}
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
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
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
        except (ValueError, RecursionError):
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
        if not isinstance(function_info, dict):
            normalized.append(tc)
            continue
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
        if len(split_args) != len(split_names) or not all(isinstance(arg, dict) for arg in split_args):
            normalized.append(tc)
            continue
        arg_texts = [json.dumps(arg, ensure_ascii=False) for arg in split_args]
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
        if ARGUMENT_PARSE_ERROR in tool_args:
            has_parse_error = True
        if tool_name == "over_task" and ARGUMENT_PARSE_ERROR not in tool_args:
            over_task_call = (i, tool_call, tool_name, tool_args)
        else:
            parsed_tools.append((i, tool_call, tool_name, tool_args))
    return ToolExecutionPlan(parsed_tools=parsed_tools, over_task_call=over_task_call, has_parse_error=has_parse_error)


def _invoke_tool_function(
    name: str,
    arguments: dict,
    tool_mcp_servers: Dict[str, str],
    tool_original_names: Dict[str, str] | None = None,
    session_scope: str = "main",
) -> Any:
    if ARGUMENT_PARSE_ERROR in arguments:
        raise ValueError(arguments[ARGUMENT_PARSE_ERROR])
    if name not in tool_mcp_servers:
        raise ValueError(f"工具 {name} 未在MCP服务器中注册，无法调用")
    mcp_service = tool_mcp_servers[name]
    original_name = (tool_original_names or {}).get(name, name)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(call_mcp_tool(
            function_name=original_name,
            arguments=arguments,
            mcp_service=mcp_service,
            session_scope=session_scope,
        ))
    finally:
        loop.close()


def execute_tool_round(
    parsed_tools: list[tuple[int, dict, str, dict]],
    tool_mcp_servers: Dict[str, str],
    max_workers: int,
    tool_original_names: Dict[str, str] | None = None,
    session_scope: str = "main",
) -> list[dict[str, Any]]:
    tool_results: list[dict[str, Any]] = []

    def execute_tool(index, tool_call, tool_name, tool_args) -> tuple:
        try:
            result = _invoke_tool_function(
                tool_name, tool_args, tool_mcp_servers, tool_original_names, session_scope
            )
            ret = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return index, tool_call, tool_name, tool_args, ret, None
        except Exception as exc:
            ret = f"工具执行失败【{exc}】"
            return index, tool_call, tool_name, tool_args, ret, exc
        except BaseException as exc:
            # SystemExit 等 BaseException 派生异常（MCP 客户端内部逃逸）不能穿透
            # 包装：一旦逃出 execute_tool_round 会经 to_thread 一路上抛终止整个
            # 生成任务。统一转为工具错误反馈，让模型改用其他工具继续任务。
            ret = f"工具执行失败【{exc.__class__.__name__}: {exc}】，该工具当前不可用；请改用其他可用工具完成任务，或直接向用户说明情况"
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
