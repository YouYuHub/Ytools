"""Client-side MCP connection profiles, independent of tools and server brands."""
import copy
import json
import re
from urllib.parse import unquote

from jsonschema import validators
from referencing import Registry
from referencing.exceptions import NoSuchResource


def profiles(config):
    if not isinstance(config, dict) or "profiles" not in config:
        return {}
    entries = config["profiles"]
    if not isinstance(entries, dict) or not entries:
        raise ValueError("profiles 必须是非空对象")
    allowed = {"label", "description", "command", "args", "env", "persistent", "tools", "type"}
    for key, entry in entries.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or not isinstance(entry, dict):
            raise ValueError("profile ID 只能使用 1–64 位字母、数字、下划线或连字符，配置须为对象")
        if set(entry) - allowed:
            raise ValueError("profile 存在不支持的字段：" + ", ".join(sorted(set(entry) - allowed)))
        if "tools" in entry and (not isinstance(entry["tools"], list) or any(not isinstance(t, str) or not t for t in entry["tools"])):
            raise ValueError("profile.tools 必须是工具原名列表；空列表表示禁用全部工具")
        if "persistent" in entry and not isinstance(entry["persistent"], bool):
            raise ValueError("profile.persistent 必须为布尔值")
        for field in ("label", "description"):
            if field in entry and not isinstance(entry[field], str):
                raise ValueError("profile." + field + " 必须是字符串")
    for field in ("default_profile", "locked_profile"):
        value = config.get(field)
        if value is not None and (not isinstance(value, str) or value not in entries):
            raise ValueError(field + " 必须引用已配置的 profile")
    return entries


def select_profile(config, requested=None):
    entries = profiles(config)
    if not entries:
        if requested is not None:
            raise ValueError("此 MCP 未配置 profiles")
        return None
    selected = requested if requested is not None else config.get("locked_profile") or config.get("default_profile")
    if selected is None:
        if len(entries) != 1:
            raise ValueError("请指定 mcp_profile 或配置 default_profile")
        selected = next(iter(entries))
    if not isinstance(selected, str) or selected not in entries:
        raise ValueError("无效的 mcp_profile；可选值：" + ", ".join(entries))
    if config.get("locked_profile") and selected != config["locked_profile"]:
        raise ValueError("该服务已固定 profile=" + config["locked_profile"] + "，拒绝切换到其他连接")
    return selected


def connection_config(config, requested=None):
    selected = select_profile(config, requested)
    if not isinstance(config, dict):
        return config
    effective = copy.deepcopy(config)
    for key in ("profiles", "default_profile", "locked_profile"):
        effective.pop(key, None)
    if selected is not None:
        entry = profiles(config)[selected]
        for key in ("command", "args", "persistent", "type", "tools"):
            if key in entry:
                effective[key] = copy.deepcopy(entry[key])
        base_env, extra_env = config.get("env", {}), entry.get("env", {})
        if not isinstance(base_env, dict) or not isinstance(extra_env, dict):
            raise ValueError("env 必须是对象")
        effective["env"] = {**base_env, **extra_env}
    if effective.get("type", "stdio") != "stdio":
        raise ValueError("当前 MCP 客户端仅支持 stdio 连接")
    if not isinstance(effective.get("command"), str) or not effective["command"].strip():
        raise ValueError("MCP 配置必须提供 command（可由 profile 覆盖）")
    if not isinstance(effective.get("args", []), list) or any(not isinstance(a, str) for a in effective.get("args", [])):
        raise ValueError("MCP args 必须是字符串数组")
    if not isinstance(effective.get("env", {}), dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in effective.get("env", {}).items()):
        raise ValueError("MCP env 的键和值必须为字符串")
    if "tools" in effective and (not isinstance(effective["tools"], list) or any(not isinstance(t, str) or not t for t in effective["tools"])):
        raise ValueError("tools 必须是工具原名列表")
    if "persistent" in effective and not isinstance(effective["persistent"], bool):
        raise ValueError("persistent 必须为布尔值")
    return effective


def tool_allowed(effective, tool_name):
    return "tools" not in effective or tool_name in effective["tools"]


def public_profiles(config):
    entries = profiles(config)
    return [{"id": key, "label": entry.get("label", key)[:100], "description": entry.get("description", "")[:500],
             "persistent": entry.get("persistent", config.get("persistent")) is True}
            for key, entry in entries.items()]


def _no_remote_ref(uri):
    raise NoSuchResource(ref=uri)


def schema_validator(schema):
    kind = validators.validator_for(schema)
    kind.check_schema(schema)
    return kind(schema, registry=Registry(retrieve=_no_remote_ref))


def validate_arguments(schema, arguments):
    try:
        error = next(schema_validator(schema).iter_errors(arguments), None)
    except Exception as exc:
        raise ValueError("工具 Schema 无法验证（仅支持本地引用）") from exc
    if error:
        path = ".".join(map(str, error.absolute_path)) or "$"
        raise ValueError("工具参数不符合当前 profile 的 Schema：" + path + ": " + error.message)


def _model_schema(schema, location):
    # Walk schema positions only: a business property named "$id" or a literal
    # example containing "$ref" must not be interpreted as a schema directive.
    maps = {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas", "dependencies"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    singles = {"additionalProperties", "unevaluatedProperties", "unevaluatedItems", "additionalItems",
               "contains", "propertyNames", "not", "if", "then", "else", "contentSchema", "items"}
    def children(node):
        for key, value in node.items():
            if key in maps and isinstance(value, dict):
                for name, child in value.items():
                    if isinstance(child, (dict, bool)):
                        yield (key, name), child
            elif key in arrays or key == "items" and isinstance(value, list):
                if isinstance(value, list):
                    for index, child in enumerate(value):
                        yield (key, index), child
            elif key in singles and isinstance(value, (dict, bool)):
                yield (key,), value
    def pointer(path):
        return "".join("/" + str(item).replace("~", "~0").replace("/", "~1") for item in path)
    anchors = {}
    def collect(node, path=(), scope=()):
        if not isinstance(node, dict):
            return
        if "$id" in node:
            scope = path
        if isinstance(node.get("$anchor"), str):
            anchors[(scope, node["$anchor"])] = path
        for suffix, child in children(node):
            collect(child, path + suffix, scope)
    collect(schema)
    def visit(node, path=(), scope=()):
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        output = copy.deepcopy(node)
        if "$id" in node:
            scope = path
            output.pop("$id", None)
        output.pop("$anchor", None)
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref == "#" or ref.startswith("#/"):
                output["$ref"] = "#" + location + pointer(scope) + unquote(ref[1:])
            elif ref.startswith("#") and (scope, unquote(ref[1:])) in anchors:
                output["$ref"] = "#" + location + pointer(anchors[(scope, unquote(ref[1:]))])
        for suffix, child in children(node):
            target = output
            for part in suffix[:-1]:
                target = target[part]
            target[suffix[-1]] = visit(child, path + suffix, scope)
        return output
    return visit(schema)


def model_envelope(variants, default=None):
    grouped = {}
    for profile, schema in variants.items():
        identity = json.dumps(schema, sort_keys=True, ensure_ascii=False)
        grouped.setdefault(identity, {"schema": schema, "profiles": []})["profiles"].append(profile)
    groups = list(grouped.values())
    if len(groups) == 1:
        arguments = _model_schema(groups[0]["schema"], "/properties/arguments")
    else:
        branches = []
        for i, item in enumerate(groups):
            branch = _model_schema(item["schema"], "/properties/arguments/anyOf/" + str(i))
            branch["description"] = "适用 profile: " + ", ".join(item["profiles"]) + "。" + branch.get("description", "")
            branches.append(branch)
        arguments = {"anyOf": branches, "description": "参数结构因 profile 不同而变化；后端按所选 profile 的原始 Schema 校验。"}
    route = {"type": "string", "enum": list(variants), "description": "选择本次调用的连接配置；连续操作同一状态时保持同一 profile。"}
    if default in variants:
        route["default"] = default
    return {"type": "object", "properties": {"mcp_profile": route, "arguments": arguments},
            "required": ["mcp_profile", "arguments"], "additionalProperties": False}
