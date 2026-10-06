"""周期发现本地 Ollama 模型，并将变化后的目录发布到 env_manager 内存快照。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from env_manager import (
    DEFAULT_OLLAMA_CHAT_URL,
    get_ollama_provider_configs,
    load_var,
    set_ollama_runtime_models,
)

from util.logger import get_logger

logger = get_logger("util.ollama_model_watcher")


POLL_INTERVAL_ENV_NAME = "OLLAMA_MODEL_POLL_INTERVAL_SECONDS"
DISCOVERY_TIMEOUT_ENV_NAME = "OLLAMA_MODEL_DISCOVERY_TIMEOUT_SECONDS"
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PARALLEL_SHOW_REQUESTS = 4


def _positive_float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = load_var(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if allow_zero and value <= 0:
        return 0.0
    return value if value > 0 else default


def _ollama_api_url(chat_url: str, endpoint: str) -> str:
    """从 OpenAI 兼容的 base URL（通常以 /v1 结尾）推导 Ollama 原生 API URL。"""
    parsed = urlsplit(str(chat_url or "").strip())
    if parsed.scheme.casefold() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Ollama URL 必须是有效的 http/https 地址")

    path = parsed.path.rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if path.casefold().endswith(suffix):
            path = path[:-len(suffix)]
            break
    api_path = f"{path.rstrip('/')}/api/{endpoint}"
    return urlunsplit((parsed.scheme, parsed.netloc, api_path, parsed.query, ""))


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if isinstance(api_key, str) and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ollama 响应超过大小限制")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Ollama 响应必须是 JSON 对象")
    return decoded


def _canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _normalize_inventory(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("Ollama 模型列表响应缺少 models 数组")

    by_id: dict[str, dict[str, str]] = {}
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model") or item.get("name")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        digest = item.get("digest")
        modified_at = item.get("modified_at")
        by_id[model_id] = {
            "id": model_id,
            "digest": digest.strip() if isinstance(digest, str) else "",
            "modified_at": modified_at.strip() if isinstance(modified_at, str) else "",
        }
    return [by_id[key] for key in sorted(by_id, key=str.casefold)]


def _model_record(model_id: str, capabilities: set[str] | None) -> dict[str, Any]:
    capability_names = {value.casefold() for value in capabilities or set()}
    return {
        "id": model_id,
        "name": model_id,
        # 缺少能力信息时采用保守值，避免误把不支持视觉的模型当作支持。
        "vision": "vision" in capability_names,
        "toolCalling": "tools" in capability_names,
    }


class OllamaModelWatcher:
    """可独立测试的 Ollama 模型发现器；网络错误不会清除最近一次成功快照。"""

    def __init__(self, request_json: Callable[..., dict[str, Any]] | None = None):
        self._request_json = request_json or _request_json
        self._state_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._last_good: dict[str, dict[str, Any]] = {}
        self._last_error: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_lock = threading.Lock()

    def poll_once(self, providers: list[dict[str, Any]] | None = None) -> dict[str, str]:
        if not self._poll_lock.acquire(blocking=False):
            return {"watcher": "already_running"}
        try:
            timeout = _positive_float_env(
                DISCOVERY_TIMEOUT_ENV_NAME, DEFAULT_DISCOVERY_TIMEOUT_SECONDS
            )
            provider_specs = providers if providers is not None else get_ollama_provider_configs()
            actions: dict[str, str] = {}
            for provider in provider_specs:
                name = str(provider.get("name") or "Ollama").strip() or "Ollama"
                chat_url = str(provider.get("url") or DEFAULT_OLLAMA_CHAT_URL).strip()
                api_key = provider.get("apiKey")
                try:
                    changed = self._refresh_provider(name, chat_url, api_key, timeout)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                    error_kind = type(exc).__name__
                    key = name.casefold()
                    with self._state_lock:
                        should_log = self._last_error.get(key) != error_kind
                        self._last_error[key] = error_kind
                    if should_log:
                        logger.warning(f"[ollama-discovery] {name} 模型发现失败（{error_kind}），"
                            "保留上次成功的模型列表")
                    actions[name] = "failed"
                    continue
                with self._state_lock:
                    self._last_error.pop(name.casefold(), None)
                actions[name] = "updated" if changed else "unchanged"
            return actions
        finally:
            self._poll_lock.release()

    def _refresh_provider(
        self,
        provider_name: str,
        chat_url: str,
        api_key: str | None,
        timeout: float,
    ) -> bool:
        tags_url = _ollama_api_url(chat_url, "tags")
        show_url = _ollama_api_url(chat_url, "show")
        inventory = _normalize_inventory(
            self._request_json(tags_url, api_key=api_key, timeout=timeout)
        )
        inventory_hash = _canonical_hash(inventory)
        key = provider_name.casefold()

        with self._state_lock:
            previous = self._last_good.get(key)
            same_endpoint = bool(previous and previous["url"].rstrip("/") == chat_url.rstrip("/"))
            same_inventory = bool(same_endpoint and previous["inventory_hash"] == inventory_hash)
            cached_models = previous["models"] if same_inventory else {}

        model_records: list[dict[str, Any]] = []
        needs_show: list[dict[str, str]] = []
        for item in inventory:
            cached = cached_models.get(item["id"])
            if cached and cached.get("digest") == item["digest"] and cached.get("capabilities_loaded"):
                model_records.append(dict(cached["record"]))
                continue
            model_records.append(_model_record(item["id"], None))
            needs_show.append(item)

        def load_capabilities(item: dict[str, str]) -> tuple[str, set[str] | None]:
            response = self._request_json(
                show_url,
                method="POST",
                payload={"model": item["id"]},
                api_key=api_key,
                timeout=timeout,
            )
            raw_capabilities = response.get("capabilities")
            if not isinstance(raw_capabilities, list):
                return item["id"], set()
            capabilities = {
                value.strip().casefold()
                for value in raw_capabilities
                if isinstance(value, str) and value.strip()
            }
            return item["id"], capabilities

        loaded_capabilities: dict[str, set[str]] = {}
        if needs_show:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_SHOW_REQUESTS, len(needs_show))) as executor:
                futures = [executor.submit(load_capabilities, item) for item in needs_show]
                for future in as_completed(futures):
                    try:
                        result_id, capabilities = future.result()
                        loaded_capabilities[result_id] = capabilities or set()
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                        pass

        next_cache: dict[str, dict[str, Any]] = {}
        record_by_id = {record["id"]: record for record in model_records}
        for item in inventory:
            model_id = item["id"]
            cached = cached_models.get(model_id)
            if model_id in loaded_capabilities:
                record = _model_record(model_id, loaded_capabilities[model_id])
                capabilities_loaded = True
            elif cached and cached.get("digest") == item["digest"]:
                record = dict(cached["record"])
                capabilities_loaded = bool(cached.get("capabilities_loaded"))
            else:
                record = _model_record(model_id, None)
                capabilities_loaded = False
            record_by_id[model_id] = record
            next_cache[model_id] = {
                "digest": item["digest"],
                "record": record,
                "capabilities_loaded": capabilities_loaded,
            }

        ordered_records = [record_by_id[item["id"]] for item in inventory]
        snapshot_changed = set_ollama_runtime_models(provider_name, chat_url, ordered_records)
        snapshot_hash = _canonical_hash({
            "inventory": inventory,
            "models": ordered_records,
        })
        with self._state_lock:
            previous = self._last_good.get(key)
            changed = bool(
                not previous
                or previous["url"].rstrip("/") != chat_url.rstrip("/")
                or previous["snapshot_hash"] != snapshot_hash
            )
            self._last_good[key] = {
                "url": chat_url,
                "inventory_hash": inventory_hash,
                "snapshot_hash": snapshot_hash,
                "models": next_cache,
            }

        if changed or snapshot_changed:
            logger.info(f"[ollama-discovery] {provider_name} 模型列表已更新（{len(ordered_records)} 个）")
        return changed or snapshot_changed

    def start(self) -> threading.Thread | None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            interval = _positive_float_env(
                POLL_INTERVAL_ENV_NAME,
                DEFAULT_POLL_INTERVAL_SECONDS,
                allow_zero=True,
            )
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="ollama-model-discovery",
                daemon=True,
            )
            self._thread.start()
            if interval <= 0:
                logger.info(f"[ollama-discovery] 自动模型发现已暂停（{POLL_INTERVAL_ENV_NAME}<=0）")
            else:
                logger.info(f"[ollama-discovery] 自动模型发现线程已启动（间隔 {interval:g}s）")
            return self._thread

    def _run(self) -> None:
        while not self._stop_event.is_set():
            interval = _positive_float_env(
                POLL_INTERVAL_ENV_NAME,
                DEFAULT_POLL_INTERVAL_SECONDS,
                allow_zero=True,
            )
            if interval <= 0:
                if self._stop_event.wait(1.0):
                    break
                continue
            started_at = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:  # 轮询线程异常后继续运行
                logger.warning(f"[ollama-discovery] 轮询异常（{type(exc).__name__}），将在下轮重试")
            interval = _positive_float_env(
                POLL_INTERVAL_ENV_NAME,
                DEFAULT_POLL_INTERVAL_SECONDS,
                allow_zero=True,
            )
            if interval <= 0:
                continue
            wait_seconds = max(0.0, interval - (time.monotonic() - started_at))
            if self._stop_event.wait(wait_seconds):
                break

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def reset_for_tests(self) -> None:
        self.stop()
        with self._state_lock:
            self._last_good.clear()
            self._last_error.clear()


_watcher = OllamaModelWatcher()


def start_ollama_model_watcher() -> threading.Thread | None:
    return _watcher.start()


def stop_ollama_model_watcher(timeout: float = 2.0) -> None:
    _watcher.stop(timeout)
