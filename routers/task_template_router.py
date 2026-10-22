"""Reusable task definitions. Templates contain references, never provider credentials."""
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factory import task_templates as service
from memory import task_template_store as store, task_cost_store
from memory.chat_memory import normalize_session_id

api_task_template_router = APIRouter(prefix="/task_templates")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(Strict):
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)

    @field_validator("provider", "model")
    @classmethod
    def trim(cls, value):
        if not value.strip():
            raise ValueError("模型和供应商名称不能为空")
        return value.strip()


class WorkRule(Strict):
    mode: Literal["inherit", "fixed", "relative"] = "inherit"
    path: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def validate_path(self):
        self.path = self.path.strip()
        if self.mode != "inherit" and not self.path:
            raise ValueError("请填写工作路径")
        if self.mode == "inherit":
            self.path = ""
        return self


class TaskTemplate(Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    prompt: str = Field(default="", max_length=32768)
    output_format: str = Field(default="", max_length=16384)
    acceptance: str = Field(default="", max_length=16384)
    models: dict[str, ModelRef] = Field(default_factory=dict)
    tools: dict[str, list[str]] | None = None
    budget: dict | None = None
    budget_currency: Literal["CNY", "USD", "EUR"] | None = None
    work_dir: WorkRule = Field(default_factory=WorkRule)

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode("utf-8")) > 262144:
            raise ValueError("单个模板不能超过 256KB")
        return self

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError("模板名称不能为空")
        return value.strip()

    @field_validator("models")
    @classmethod
    def roles_valid(cls, value):
        if set(value) - set(service.ROLES):
            raise ValueError("模型角色无效")
        return value

    @field_validator("tools")
    @classmethod
    def tools_valid(cls, value):
        if value is None:
            return value
        if len(value) > 200 or sum(len(names) for names in value.values()) > 2000:
            raise ValueError("模板工具数量过多")
        if any(not server.strip() or len(server) > 512 or any(not name.strip() or len(name) > 256 for name in names) for server, names in value.items()):
            raise ValueError("工具名称或服务名称无效")
        return {server: list(dict.fromkeys(names)) for server, names in value.items()}

    @field_validator("budget")
    @classmethod
    def budget_valid(cls, value):
        return task_cost_store.validate_limits(value) if value is not None else None


class SaveTemplate(Strict):
    template: TaskTemplate
    version: int = Field(ge=1)


class Preview(Strict):
    template: TaskTemplate
    source_session_id: str | None = Field(default=None, max_length=256)


class Start(Preview):
    request_id: UUID
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def source(value):
    return normalize_session_id(value) if value else None


def error(exc):
    from env_manager import ChatModelConfigurationError
    if isinstance(exc, ChatModelConfigurationError):
        return HTTPException(400, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, store.Conflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(500, "任务模板操作失败，请检查存储目录权限或后端日志")


@api_task_template_router.get("")
def listing():
    try:
        return {"templates": store.listing()}
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.get("/options")
def options(source_session_id: str | None = Query(default=None, max_length=256)):
    try:
        return service.options(source(source_session_id))
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.post("/preview")
def preview(payload: Preview):
    try:
        return service.prepare(payload.template.model_dump(), source(payload.source_session_id))[0]
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.post("/validate")
def validate(payload: TaskTemplate):
    return {"template": payload.model_dump()}


@api_task_template_router.post("/start")
def start(payload: Start):
    try:
        return service.start(payload.template.model_dump(), source(payload.source_session_id), payload.request_id, payload.expected_fingerprint)
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.post("")
def create(payload: TaskTemplate):
    try:
        return store.save(payload.model_dump())
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.get("/{identifier}")
def read(identifier: str):
    try:
        return store.read(identifier)
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.put("/{identifier}")
def update(identifier: str, payload: SaveTemplate):
    try:
        return store.save(payload.template.model_dump(), identifier, payload.version)
    except Exception as exc:
        raise error(exc) from exc


@api_task_template_router.delete("/{identifier}")
def delete(identifier: str, version: int = Query(ge=1)):
    try:
        store.delete(identifier, version)
        return {"ok": True}
    except Exception as exc:
        raise error(exc) from exc
