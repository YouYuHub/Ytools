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
_env_file_lock = Lock()


_MODEL_CONFIG_RESERVED_KEYS = frozenset({"variables", "runtime"})
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
    resolved_config["selected_model_name"] = _normalize_config_name(
        model_item.get("id") or model_item.get("name")
    )
    resolved_config["selected_model"] = dict(model_item)
    resolved_config.setdefault("apiType", "chat-completions")
    return resolved_config


def get_default_chat_config() -> dict[str, Any] | None:
    provider_name = _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE"))
    model_name = _normalize_config_name(env_vars.get("CHAT_MODEL_NAME"))
    if not provider_name or not model_name:
        return None
    return get_model_config(provider_name, model_name)


def require_default_chat_config() -> dict[str, Any]:
    provider_name = _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE"))
    model_name = _normalize_config_name(env_vars.get("CHAT_MODEL_NAME"))
    if not provider_name or not model_name:
        raise ChatModelConfigurationError(
            f".env 必须同时配置 CHAT_OWNERSHIP_NANE 和 CHAT_MODEL_NAME"
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
    - model_name:   模型 id（若未提供则用 key）
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
            model_id = _normalize_config_name(model.get("id") or model.get("name")) or ""
            api_key = provider.get("apiKey")
            records.append({
                "provider_name": provider_name,
                "model_name": model_id,
                "api_type": str(provider.get("apiType") or "chat-completions"),
                "url": _normalize_config_name(provider.get("url") or model.get("url")),
                "vision": bool(model.get("vision", False)),
                "tool_calling": bool(model.get("toolCalling", False)),
                "max_input_tokens": model.get("maxInputTokens") or model.get("max_input_tokens"),
                "max_output_tokens": model.get("maxOutputTokens") or model.get("max_output_tokens"),
                "api_key_present": bool(isinstance(api_key, str) and api_key.strip()),
            })
    return records


def get_current_model_selection() -> dict[str, str | None]:
    """返回当前 .env 中的模型选择，键名与 .env 一致。"""
    return {
        "provider": _normalize_config_name(env_vars.get("CHAT_OWNERSHIP_NANE")),
        "model": _normalize_config_name(env_vars.get("CHAT_MODEL_NAME")),
    }


def select_chat_model(provider_name: str, model_name: str) -> dict[str, Any]:
    """写入 .env 中的两个选择字段，并验证 models.json 中能解析到对应配置。

    成功时返回新的选择；解析失败时抛出 ChatModelConfigurationError。

    注意：除了写盘外，本函数还会**显式同步覆盖全局 `env_vars` 字典中
    的两个键**，避免任何调用方在拿到返回值之前，`env_vars` 仍指向
    旧值（例如上游刚执行过 `init_path()`、或 hot-reload 触发了模块重载）。
    """
    normalized_provider = _normalize_config_name(provider_name)
    normalized_model = _normalize_config_name(model_name)
    if not normalized_provider or not normalized_model:
        raise ChatModelConfigurationError("provider 和 model 均不能为空")

    chat_config = get_model_config(normalized_provider, normalized_model)
    if chat_config is None:
        raise ChatModelConfigurationError(
            f"models.json 中找不到模型：{normalized_provider} / {normalized_model}"
        )

    set_env_vars({
        "CHAT_OWNERSHIP_NANE": normalized_provider,
        "CHAT_MODEL_NAME": normalized_model,
    })

    # 显式同步内存中的两个键，防止 set_env_vars 之外的副作用
    # （例如模块热重载、其他路径的 init_path 调用）让 env_vars
    # 仍然指向旧值；这是前端切换模型时立刻生效的保证。
    global env_vars
    env_vars["CHAT_OWNERSHIP_NANE"] = normalized_provider
    env_vars["CHAT_MODEL_NAME"] = normalized_model

    return {
        "provider": normalized_provider,
        "model": _normalize_config_name(
            chat_config.get("selected_model_name") or normalized_model
        ),
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
    global env_file, env_vars, setting_file, setting_vars, models_config, mcp_config
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
