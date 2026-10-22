"""Small, strict argument readers shared by local and MCP tools."""
import math


def string_arg(args, name, default="", *, required=False, max_length=None):
    value = args.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    if required and not value.strip():
        raise ValueError(f"{name} 不能为空")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} 最多 {max_length} 字符")
    return value


def int_arg(args, name, default, *, minimum=0, maximum=None):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} 必须为 {bounds} 的整数")
    return value


def number_arg(args, name, default, *, minimum=0, maximum=None):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} 必须是有限数字")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} 必须在 {bounds} 范围内")
    return float(value)
