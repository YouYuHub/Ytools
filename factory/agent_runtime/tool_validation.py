"""Validate model arguments before requesting permission or executing a batch."""
from functools import lru_cache
import math

from jsonschema import Draft202012Validator, validators


def _finite_number(_checker, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


_types = Draft202012Validator.TYPE_CHECKER.redefine("number", _finite_number).redefine(
    "integer", lambda _checker, value: isinstance(value, int) and not isinstance(value, bool))
_ArgumentValidator = validators.extend(Draft202012Validator, type_checker=_types)


@lru_cache(maxsize=32)
def _validator(name):
    from . import builtin_tools as bt
    if name == bt.READ_MEDIA_NAME:
        definition = bt.build_read_media_tool_definition()
    else:
        definition = next(value for key, value in vars(bt).items()
                          if key.endswith("TOOL_DEFINITION") and isinstance(value, dict)
                          and value.get("function", {}).get("name") == name)
    return _ArgumentValidator(definition["function"]["parameters"])


def argument_error(name, args):
    """Return a bounded correction message; never echo a large argument value."""
    error = next(_validator(name).iter_errors(args), None)
    if error is None:
        return None
    path = ".".join(map(str, error.absolute_path)) or "arguments"
    kind = error.validator
    if kind == "type":
        detail = "需要有限数字" if error.validator_value == "number" else f"需要 {error.validator_value} 类型"
    elif kind in {"maxLength", "maxItems", "maximum", "minLength", "minItems", "minimum"}:
        detail = f"{kind}={error.validator_value}"
    elif kind == "enum":
        detail = "允许值：" + ", ".join(map(str, error.validator_value))
    else:
        detail = error.message[:200]
    return f"参数 {path} 无效：{detail}；请按工具参数重试"


def prepare_builtin_calls(calls):
    """An ask_user call defers the rest of its batch until the next user reply."""
    from .builtin_tools import ASK_USER_TOOL_NAME, is_builtin_tool
    from .tool_executor import ARGUMENT_PARSE_ERROR
    has_question = any(name == ASK_USER_TOOL_NAME for _, _, name, _ in calls)
    allowed, rejected = [], []
    question_count = 0
    for index, call, name, args in calls:
        error = (args.get(ARGUMENT_PARSE_ERROR) if ARGUMENT_PARSE_ERROR in args else
                 argument_error(name, args) if is_builtin_tool(name) else None)
        code = "INVALID_ARGUMENTS"
        if has_question and name != ASK_USER_TOOL_NAME:
            error = "同轮包含 ask_user，本调用未执行；收到用户回答后按需重新调用"
            code = "WAITING_USER"
        if not error and name == ASK_USER_TOOL_NAME:
            count = len(args["questions"])
            if question_count + count > 5:
                error = "同轮提问总数最多 5 个；请合并 questions 后重试"
            else:
                question_count += count
        if error:
            rejected.append({"index": index, "tool_call": call, "tool_name": name, "tool_args": args,
                             "result": {"success": False, "error": error, "code": code, "executed": False},
                             "error": None})
        else:
            allowed.append((index, call, name, args))
    return allowed, rejected
