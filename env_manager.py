"""
.env文件加载模块
其中的变量设置应该为字符串类型类似于：

```
API_KEY=your_api_key
SECRET_KEY   =       your_secret_key
```
或者：
```
API_KEY = " your_api_key "
SECRET_KEY =   "     your_secret_key      "
```
"""
import base64
import ctypes
import json
import os
import sys
from typing import Any
from pathlib import Path
from threading import Lock

env_file = None
env_vars = {}
setting_file = None
setting_vars = {}
models_config = {}
mcp_config = {}
model_selection = {}
_env_file_lock = Lock()


_MODEL_CONFIG_RESERVED_KEYS = frozenset({"variables", "runtime", "model_selection"})
_MODEL_CONFIG_INTERNAL_PROVIDER_KEYS = frozenset({
    "vendor", "apiKey", "apiType", "models", "url", "name",
    "toolCalling", "vision", "maxInputTokens", "maxOutputTokens",
    "settings", "id", "description",
})


class ChatModelConfigurationError(RuntimeError):
    """当前聊天模型选择无法在 models.json 中解析时抛出。"""


def _normalize_config_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _iter_provider_records(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []

    providers: list[dict[str, Any]] = []
    for provider_name, provider_def in config.items():
        if provider_name in _MODEL_CONFIG_RESERVED_KEYS or not isinstance(provider_def, dict):
            continue
        provider_item = dict(provider_def)
        provider_item["name"] = provider_name
        providers.append(provider_item)

        # 扫描该 provider 内部是否还有嵌套的子 provider
        # （如 models.json 中 "Wsl Completions" 下嵌套的 "OpenCode Completions" 等）
        for sub_key, sub_value in provider_def.items():
            if sub_key in _MODEL_CONFIG_INTERNAL_PROVIDER_KEYS:
                continue
            if sub_key.startswith("_"):
                continue
            if not isinstance(sub_value, dict):
                continue
            # 判定为子 provider：包含 vendor 或 models 字段
            if "vendor" in sub_value or "models" in sub_value:
                sub_item = dict(sub_value)
                sub_item["name"] = sub_key
                providers.append(sub_item)
    return providers


def _iter_model_records(provider: dict[str, Any]) -> list[dict[str, Any]]:
    models = provider.get("models")
    if not isinstance(models, dict):
        return []

    model_items: list[dict[str, Any]] = []
    for model_name, model_def in models.items():
        if not isinstance(model_def, dict):
            continue
        model_item = dict(model_def)
        model_item.setdefault("id", model_name)
        model_item.setdefault("name", model_name)
        model_items.append(model_item)
    return model_items


def get_model_config(provider_name: str | None = None, model_name: str | None = None) -> dict[str, Any] | None:
    selected_provider_name = _normalize_config_name(provider_name)
    selected_model_name = _normalize_config_name(model_name)
    if not selected_provider_name or not selected_model_name:
        return None

    provider_item = next((
            provider for provider
            in _iter_provider_records(models_config)
            if _normalize_config_name(provider.get("name"))
            and _normalize_config_name(provider.get("name")).casefold() == selected_provider_name.casefold()
        ),
        None,
    )
    if provider_item is None:
        return None

    model_item = next((
            model for model
            in _iter_model_records(provider_item)
            if any(
                isinstance(candidate, str) and candidate.strip().casefold() == selected_model_name.casefold()
                for candidate in (model.get("id"), model.get("name"))
            )
        ),
        None,
    )
    if model_item is None:
        return None
    resolved_config = {
        key: value
        for key, value in provider_item.items()
        if key != "models"
    }
    resolved_config.update({
        key: value
        for key, value in model_item.items()
        if value is not None
    })
    resolved_config["selected_provider_name"] = selected_provider_name
    # selected_model_name 为 models.json 中 models 字段的键名（模型命名），
    # 与 .env 中 CHAT_MODEL_NAME 记录的值一致
    resolved_config["selected_model_name"] = _normalize_config_name(
        model_item.get("name") or model_item.get("id")
    )
    # selected_model_id 为请求 LLM API 时实际使用的模型 id
    resolved_config["selected_model_id"] = _normalize_config_name(
        model_item.get("id") or model_item.get("name")
    )
    resolved_config["selected_model"] = dict(model_item)
    resolved_config.setdefault("apiType", "chat-completions")
    return resolved_config


def get_default_chat_config() -> dict[str, Any] | None:
    chat_selection = model_selection.get("chat_model") if isinstance(model_selection, dict) else None
    provider_name = None
    model_name = None
    if isinstance(chat_selection, dict):
        provider_name = _normalize_config_name(chat_selection.get("ownership_name"))
        model_name = _normalize_config_name(chat_selection.get("model_name"))
    if not provider_name or not model_name:
        provider_name = _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE"))
        model_name = _normalize_config_name(env_vars.get("CHAT_MODEL_NAME"))
    if not provider_name or not model_name:
        return None
    return get_model_config(provider_name, model_name)


def require_default_chat_config() -> dict[str, Any]:
    chat_selection = model_selection.get("chat_model") if isinstance(model_selection, dict) else None
    provider_name = None
    model_name = None
    if isinstance(chat_selection, dict):
        provider_name = _normalize_config_name(chat_selection.get("ownership_name"))
        model_name = _normalize_config_name(chat_selection.get("model_name"))
    if not provider_name or not model_name:
        provider_name = _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE"))
        model_name = _normalize_config_name(env_vars.get("CHAT_MODEL_NAME"))
    if not provider_name or not model_name:
        raise ChatModelConfigurationError(
            f"models.json 的 model_selection.chat_model（或 .env 的 CHAT_OWNERSHIP_NANE/CHAT_MODEL_NAME）"
            f"必须配置聊天模型"
        )
    chat_config = get_model_config(provider_name, model_name)
    if chat_config is None:
        raise ChatModelConfigurationError(
            f"models.json 中找不到已选择的模型：{provider_name} / {model_name}"
        )
    return chat_config


def list_available_models() -> list[dict[str, Any]]:
    """返回 models.json 中所有 provider 及其模型的扁平列表，便于前端下拉选择。

    每条记录的字段：
    - provider_name: 顶层 provider 名称
    - model_name:   模型命名（models.json 中 models 字段的键名，即写入 .env CHAT_MODEL_NAME 的值）
    - model_id:     请求 API 时使用的模型 id（若未提供则用键名）
    - api_type:     协议类型（chat-completions / messages）
    - url:          端点地址
    - vision / tool_calling / max_input_tokens / max_output_tokens: 模型能力
    - api_key_present: 是否配置了 API Key（不返回明文）
    """
    records: list[dict[str, Any]] = []
    for provider in _iter_provider_records(models_config):
        provider_name = _normalize_config_name(provider.get("name"))
        if not provider_name:
            continue
        for model in _iter_model_records(provider):
            model_name = _normalize_config_name(model.get("name") or model.get("id")) or ""
            model_id = _normalize_config_name(model.get("id")) or model_name
            api_key = provider.get("apiKey")
            records.append({
                "provider_name": provider_name,
                "model_name": model_name,
                "model_id": model_id,
                "api_type": str(provider.get("apiType") or "chat-completions"),
                "url": _normalize_config_name(provider.get("url") or model.get("url")),
                "vision": bool(model.get("vision", False)),
                "tool_calling": bool(model.get("toolCalling", False)),
                "max_input_tokens": model.get("maxInputTokens") or model.get("max_input_tokens"),
                "max_output_tokens": model.get("maxOutputTokens") or model.get("max_output_tokens"),
                "api_key_present": bool(isinstance(api_key, str) and api_key.strip()),
            })
    return records


def _default_model_selection() -> dict[str, dict[str, Any]]:
    """三种模型选择的空结构（chat/compaction/title）。

    api_type 为只读回显字段：由后端根据 models.json 中对应模型的 apiType 自动确定
    （chat_completions / messages / responses），前端不传。
    parameter 为按 api_type 分桶结构：{"chat_completions": {...}, "messages": {...}, "responses": {...}}，
    字段名即各协议原生字段；取参时按当前模型 api_type 定位桶，缺失回退 chat_completions 桶。
    """
    return {
        role: {"ownership_name": None, "model_name": None, "parameter": {}, "api_type": None}
        for role in ("chat_model", "compaction_model", "title_model")
    }


def _normalize_parameter_buckets(parameter: Any) -> dict[str, dict[str, Any]]:
    """归一化 parameter 为按 api_type 分桶结构。

    - 旧版扁平格式（键为字段名，如 temperature）自动迁移为 chat_completions 桶
    - 新版分桶格式（键为 api_type）原样保留，字段按白名单过滤
    """
    if not isinstance(parameter, dict):
        return {}
    buckets: dict[str, dict[str, Any]] = {}
    if any(key in _CHAT_PARAMETER_FIELDS for key in parameter):
        normalized = {key: value for key, value in parameter.items() if key in _CHAT_PARAMETER_FIELDS}
        if normalized:
            buckets["chat_completions"] = normalized
        return buckets
    for api_type, fields in parameter.items():
        if not isinstance(fields, dict):
            continue
        normalized = {key: value for key, value in fields.items() if key in _CHAT_PARAMETER_FIELDS}
        if normalized:
            buckets[str(api_type).strip().casefold().replace("-", "_")] = normalized
    return buckets


def _normalize_model_selection(raw: Any) -> dict[str, dict[str, Any]]:
    """把 models.json 顶层的任意结构归一化为 {role: {ownership_name, model_name, parameter, api_type}}。

    parameter 为按 api_type 分桶结构；旧版扁平格式自动迁移为 chat_completions 桶。
    """
    normalized = _default_model_selection()
    if not isinstance(raw, dict):
        return normalized
    for role, defaults in normalized.items():
        entry = raw.get(role)
        if not isinstance(entry, dict):
            continue
        entry = _normalize_loaded_value(entry)
        if not isinstance(entry, dict):
            continue
        ownership_name = _normalize_config_name(entry.get("ownership_name"))
        model_name = _normalize_config_name(entry.get("model_name"))
        if not ownership_name or not model_name:
            continue
        parameter = entry.get("parameter")
        api_type = _normalize_config_name(entry.get("api_type"))
        normalized[role] = {
            "ownership_name": ownership_name,
            "model_name": model_name,
            "parameter": _normalize_parameter_buckets(parameter),
            "api_type": api_type,
        }
    return normalized


def _backfill_selection_api_type() -> None:
    """为内存中的模型选择补齐 api_type（已配置但缺失的，从 models.json 的 apiType 推导）。

    仅内存同步、不写盘；select 时保存的 api_type 优先。
    """
    global model_selection
    if not isinstance(model_selection, dict):
        return
    for role, entry in model_selection.items():
        if not isinstance(entry, dict) or entry.get("api_type"):
            continue
        provider = entry.get("ownership_name")
        model = entry.get("model_name")
        if not provider or not model:
            continue
        chat_config = get_model_config(provider, model)
        if chat_config is not None:
            entry["api_type"] = _derive_api_type(chat_config)


def get_model_selection() -> dict[str, dict[str, Any]]:
    """返回内存中的三模型选择（深拷贝，调用方修改不影响全局状态）。"""
    import copy
    return copy.deepcopy(model_selection if isinstance(model_selection, dict) else {})


def get_role_selection(role: str) -> dict[str, Any]:
    """返回单个角色（chat_model/compaction_model/title_model）的选择。"""
    return get_model_selection().get(role, {"ownership_name": None, "model_name": None, "parameter": {}, "api_type": None})


_CHAT_PARAMETER_FIELDS = (
    "temperature",
    "max_tokens",
    "top_p",
    "presence_penalty",
    "reasoning_effort",
    "extra_body",
    # 协议专属字段：messages 的 thinking、responses 的 max_output_tokens/instructions
    "thinking",
    "max_output_tokens",
    "instructions",
)


def get_role_parameter(role: str = "chat_model") -> dict[str, Any]:
    """按角色的模型 api_type 取 parameter 桶。

    回退链：当前 api_type 的桶 -> chat_completions 桶 -> 任意第一个非空桶 -> 空。
    返回新字典，调用方修改不影响全局状态。
    """
    selection = get_role_selection(role)
    buckets = selection.get("parameter")
    if not isinstance(buckets, dict) or not buckets:
        return {}
    api_type = selection.get("api_type") or "chat_completions"
    bucket = buckets.get(api_type)
    if isinstance(bucket, dict) and bucket:
        return dict(bucket)
    fallback = buckets.get("chat_completions")
    if isinstance(fallback, dict) and fallback:
        return dict(fallback)
    for candidate in buckets.values():
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
    return {}


def apply_role_parameter_defaults(body: dict[str, Any], role: str = "chat_model") -> dict[str, Any]:
    """请求体未显式提供的字段，用模型选择配置的 parameter 填充。

    优先级：请求体显式传参 > select 配置的 parameter（按角色模型的 api_type 取桶）> ChatLLMRequest 默认值。
    返回新字典，不修改入参。
    """
    if not isinstance(body, dict):
        return body
    parameter = get_role_parameter(role)
    if not parameter:
        return body
    merged = dict(body)
    for key in _CHAT_PARAMETER_FIELDS:
        if key not in merged and key in parameter and parameter[key] is not None:
            merged[key] = parameter[key]
    return merged


def save_model_selection(selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把三模型选择写回 models.json 顶层 `model_selection` 键，并同步内存。

    - 保留 models.json 其它顶层键（variables/runtime/各 provider）不动
    - 临时文件 + 原子替换写入（Windows 上避免读到半截文件）
    - 返回归一化后的新选择（内存中的同一份）
    """
    if not isinstance(selection, dict):
        raise ChatModelConfigurationError("model_selection 必须是字典")
    if not setting_file:
        raise ChatModelConfigurationError("models.json 路径未初始化")
    normalized = _normalize_model_selection(selection)
    setting_path = Path(setting_file)
    raw_config = {}
    if setting_path.exists():
        try:
            with setting_path.open("r", encoding="utf-8") as f:
                raw_config = json.load(f)
        except Exception:
            raw_config = {}
    if not isinstance(raw_config, dict):
        raw_config = {}
    raw_config["model_selection"] = normalized
    tmp_path = setting_path.with_name(setting_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(raw_config, fp, ensure_ascii=False, indent=2)
    for attempt in range(6):
        try:
            os.replace(tmp_path, setting_path)
            break
        except OSError:
            if attempt >= 5:
                raise ChatModelConfigurationError(f"写入 models.json 失败（文件可能被占用）：{setting_path}")
            import time
            time.sleep(0.01 * (attempt + 1))
    global models_config, model_selection
    models_config = raw_config
    model_selection = normalized
    return get_model_selection()


def get_current_model_selection() -> dict[str, str | None]:
    """返回当前模型选择（三角色），优先读取 models.json 的 model_selection 键。

    为向后兼容，chat_model 未配置时回退读取 .env 的 CHAT_OWNERSHIP_NANE/CHAT_MODEL_NAME。
    """
    selection = get_model_selection()
    chat = selection.get("chat_model") or {}
    provider = chat.get("ownership_name") or _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE"))
    model = chat.get("model_name") or _normalize_config_name(env_vars.get("CHAT_MODEL_NAME"))
    return {
        "provider": provider,
        "model": model,
        "selection": selection,
    }


def _derive_api_type(chat_config: dict[str, Any]) -> str:
    """根据 models.json 中模型的 apiType 推导 api_type（chat_completions/messages/responses）。

    仅作归一化：连字符转下划线、小写；未配置时默认 chat_completions。
    """
    raw = str(chat_config.get("apiType") or "").strip().casefold()
    if not raw:
        return "chat_completions"
    if raw in ("chat-completions", "chat_completions"):
        return "chat_completions"
    if raw in ("messages", "responses"):
        return raw
    return raw.replace("-", "_")


def select_chat_model(
    provider_name: str,
    model_name: str,
    role: str = "chat_model",
    parameter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把模型选择写入 models.json 顶层 `model_selection` 键，并同步内存（实时生效）。

    - role: chat_model / compaction_model / title_model（默认 chat_model）
    - 校验 models.json 目录中存在该组合；compaction_model 必须为 chat-completions 协议
    - parameter: 该模型的默认生成参数（如 temperature/max_tokens 等）。按 api_type 分桶存储：
      写入该模型 api_type 对应的桶（如 messages 模型写入 parameter["messages"]），其它桶保留。
      parameter 传 None 时仅切换模型、参数桶不变（同协议切换沿用参数）。
    - api_type: 只读回显字段，前端不传；由后端根据 models.json 中该模型的 apiType 自动确定
    - 不再写 .env 的 CHAT_OWNERSHIP_NANE / CHAT_MODEL_NAME
    """
    normalized_provider = _normalize_config_name(provider_name)
    normalized_model = _normalize_config_name(model_name)
    if not normalized_provider or not normalized_model:
        raise ChatModelConfigurationError("provider 和 model 均不能为空")
    if role not in _default_model_selection():
        raise ChatModelConfigurationError(
            f"role 必须是 {'/'.join(_default_model_selection().keys())} 之一，当前为 {role!r}"
        )
    if parameter is not None and not isinstance(parameter, dict):
        raise ChatModelConfigurationError("parameter 必须是字典")

    chat_config = get_model_config(normalized_provider, normalized_model)
    if chat_config is None:
        raise ChatModelConfigurationError(
            f"models.json 中找不到模型：{normalized_provider} / {normalized_model}"
        )
    if role == "compaction_model" and str(chat_config.get("apiType") or "chat-completions").casefold() != "chat-completions":
        raise ChatModelConfigurationError(
            f"压缩模型必须为 chat-completions 协议，{normalized_model} 配置为 {chat_config.get('apiType')}"
        )

    api_type = _derive_api_type(chat_config)
    current = get_model_selection()
    existing = current.get(role) or {}
    buckets = dict(existing.get("parameter")) if isinstance(existing.get("parameter"), dict) else {}
    if parameter is not None:
        normalized_fields = {key: value for key, value in parameter.items() if key in _CHAT_PARAMETER_FIELDS}
        buckets[api_type] = normalized_fields
        current[role] = {
            "ownership_name": normalized_provider,
            "model_name": _normalize_config_name(chat_config.get("selected_model_name") or normalized_model),
            "parameter": buckets,
            "api_type": api_type,
        }
    else:
        current[role] = {
            "ownership_name": normalized_provider,
            "model_name": _normalize_config_name(chat_config.get("selected_model_name") or normalized_model),
            "parameter": buckets,
            "api_type": api_type,
        }
    save_model_selection(current)
    selected = get_model_selection()[role]
    return {
        "role": role,
        "provider": selected["ownership_name"],
        "model": selected["model_name"],
        "model_id": chat_config.get("selected_model_id"),
        "parameter": selected["parameter"],
        "api_type": selected["api_type"],
    }



class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _resolve_variable_references(value: str, variables: dict) -> str:
    """
    解析字符串中的变量引用，如${VAR_NAME}
    :param value: 要解析的字符串
    :param variables: 变量字典
    :return: 解析后的字符串
    """
    # 支持使用 # 在变量名中注释后面所有内容
    result = value.split("#")[0].strip().strip("'").strip("\"").strip()
    while True:
        start = result.find("${")
        if start == -1:
            break
        end = result.find("}", start)
        if end == -1:
            break
        var_name = result[start+2:end].strip()
        if var_name in variables:
            # 递归解析，防止嵌套引用
            replacement = _resolve_variable_references(variables[var_name], variables)
            result = result[:start] + replacement + result[end+1:]
        else:
            # 变量未定义，保留原样
            break
    return result


def _decrypt_dpapi_secret(secret_text: str) -> str:
    if not isinstance(secret_text, str):
        return secret_text
    if not secret_text.startswith("enc:dpapi:"):
        return secret_text
    payload = secret_text[len("enc:dpapi:"):]
    try:
        encrypted_bytes = base64.b64decode(payload)
    except Exception:
        return secret_text
    if sys.platform != "win32":
        return secret_text
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob = _DATA_BLOB(len(encrypted_bytes), ctypes.cast(ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes)), ctypes.POINTER(ctypes.c_byte)))
        out_blob = _DATA_BLOB()
        if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)) == 0:
            return secret_text
        try:
            data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return data.decode("utf-8")
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return secret_text


def _load_setting_array_or_dict(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _normalize_loaded_value(value: Any) -> Any:
    if isinstance(value, str):
        return _decrypt_dpapi_secret(value)
    if isinstance(value, list):
        return [_normalize_loaded_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_loaded_value(item) for key, item in value.items()}
    return value


def _normalize_to_str_dict(raw: dict) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            normalized[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            normalized[key] = str(value)
        else:
            normalized[key] = str(value).strip()
    return normalized


def _read_env_file_lines(path: Path, coding: str = "utf-8") -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding=coding) as f:
        return f.readlines()


def _split_env_assignment(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    left, right = line.split("=", 1)
    key = left.strip()
    if not key or key.startswith("#"):
        return None
    value = right.strip().rstrip("\n")
    return key, value


def _format_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if value is None:
        return ""
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text
    return json.dumps(text, ensure_ascii=False)


def _persist_env_values(updated_vars: dict[str, Any], coding: str = "utf-8") -> None:
    if not env_file:
        raise RuntimeError("env_file is not initialized")
    env_path = Path(env_file)
    with _env_file_lock:
        existing_lines = _read_env_file_lines(env_path, coding=coding)
        if not existing_lines:
            existing_lines = []
        updated_keys = {str(key): _format_env_value(value) for key, value in updated_vars.items()}
        output_lines: list[str] = []
        seen_keys: set[str] = set()
        for line in existing_lines:
            parsed = _split_env_assignment(line)
            if parsed is None:
                output_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            key, _ = parsed
            if key in updated_keys:
                output_lines.append(f"{key}={updated_keys[key]}\n")
                seen_keys.add(key)
            else:
                output_lines.append(line if line.endswith("\n") else line + "\n")
        for key, value in updated_keys.items():
            if key not in seen_keys and key not in {parsed_key for parsed_key, _ in filter(None, (_split_env_assignment(line) for line in existing_lines))}:
                output_lines.append(f"{key}={value}\n")
        with env_path.open("w", encoding=coding) as f:
            f.writelines(output_lines)


def set_env_vars(values: dict[str, Any], coding: str = "utf-8") -> dict[str, str]:
    """批量写入 .env 并同步更新内存中的 env_vars。

    返回 {key: 写入磁盘的实际字符串}，调用方可用于响应体展示。
    函数末尾会通过 ``global env_vars`` 显式绑定全局字典，
    以防 hot-reload 或并发场景下 env_vars 被上游 ``init_path()`` 重建。
    """
    if not isinstance(values, dict):
        raise TypeError("values 必须是字典")
    # 显式声明全局，保证调用方通过 env_manager.env_vars 取到的就是本函数更新后的字典。
    global env_vars
    normalized_updates: dict[str, str] = {}
    raw_updates: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            continue
        normalized_key = key.strip()
        normalized_value = _format_env_value(value)
        normalized_updates[normalized_key] = normalized_value
        raw_updates[normalized_key] = value
        env_vars[normalized_key] = _resolve_variable_references(normalized_value, {**env_vars, normalized_key: normalized_value})
    if raw_updates:
        _persist_env_values(raw_updates, coding=coding)
    return normalized_updates


def get_env_file_path() -> str | None:
    """返回当前加载的项目 .env 文件绝对路径。"""
    if not env_file:
        return None
    return str(Path(env_file).resolve()).replace("\\", "/")


def init_path(
    path: str = None, /,
    filename: str = ".env",
    coding: str = "utf-8",
    setting_path: str | None = None,
) -> None:
    """
    初始化.env文件路径
    :param path: 文件路径
    :return: None
    """
    global env_file, env_vars, setting_file, setting_vars, models_config, mcp_config, model_selection
    if path is None:
        path = Path.cwd() / filename
    p = Path(path)
    if p.is_dir():
        p = p / filename
    env_file = str(p)
    env_path = Path(env_file)
    if setting_path is None:
        setting_root = env_path.parent / "setting"
    else:
        setting_root = Path(setting_path)
        if setting_root.is_file():
            setting_root = setting_root.parent
    setting_candidate = setting_root / "models.json"
    setting_file = str(setting_candidate)
    raw_models = _load_setting_array_or_dict(setting_candidate)
    models_config = _normalize_loaded_value(raw_models)
    model_selection = _normalize_model_selection(
        models_config.get("model_selection") if isinstance(models_config, dict) else None
    )
    _backfill_selection_api_type()
    setting_vars = {}
    if isinstance(models_config, dict):
        variables = models_config.get("variables")
        if isinstance(variables, dict):
            normalized_setting_vars = _normalize_to_str_dict(variables)
            for var_name, raw_value in normalized_setting_vars.items():
                setting_vars[var_name] = _resolve_variable_references(raw_value, normalized_setting_vars)
        runtime = models_config.get("runtime")
        if isinstance(runtime, dict):
            for key, value in runtime.items():
                if isinstance(value, bool):
                    setting_vars[key] = "true" if value else "false"
                elif isinstance(value, (int, float)):
                    setting_vars[key] = str(value)
                elif value is not None:
                    setting_vars[key] = str(value)
    if not env_path.exists():
        env_vars = {}
        return
    # .env配置读取第一阶段：读取所有变量到临时字典
    raw_vars = {}
    with open(env_file, "r", encoding=coding) as f:
        for line in f:
            if "=" in line:
                var_name, var_value = line.strip().split("=", 1)
                if not var_name.strip().startswith("#"):
                    raw_vars[var_name.strip().strip("\"").strip("\'").strip()] = var_value.strip().strip("\"").strip("'").strip()
    # .env配置读取第二阶段：解析所有变量引用
    env_vars = {}
    for var_name, raw_value in raw_vars.items():
        env_vars[var_name] = _resolve_variable_references(raw_value, raw_vars)


def load_var(var_name: str, default: str = None,
    #coding: str = "utf-8"
) -> str | None:
    """
    加载.env文件中的变量值
    :param var_name: 变量名
    :param default: 默认值
    :return: 变量值
    """
    if env_file is None and setting_file is None:
        return default
        # raise ValueError("env_file is not set, please call init_path() first")
    #with open(env_file, "r", encoding=coding) as f:
    #    for line in f:
    #        if line.startswith(var_name):
    #            return line.split("=")[1].strip().strip("\"").strip("'").strip()
    if var_name in setting_vars:
        return setting_vars[var_name]
    if var_name in env_vars:
        return env_vars[var_name]
    return default


if __name__ == "__main__":
    init_path()
    print(env_file)
    print(env_vars)
    print(setting_file)
    print(setting_vars)
    # print(load_var("API_KEY"))
