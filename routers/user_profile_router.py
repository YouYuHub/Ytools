# coding: utf-8
"""访客用户配置接口：全局显示名的读取与保存（项目 .env 的 USER_NAME 键）。

账号 / 数据库体系落地前的过渡实现——显示名不挂在任何会话上，全局唯一，
持久化在项目 .env（env_manager.set_env_vars 写盘 + 同步内存 env_vars，
配置热重载线程也会把外部直接改文件的改动同步回内存）。前端侧边栏点击名字
即可编辑；留空 = 清空该键值，界面回退默认「访客用户」。

后续接入数据库管理用户时，只需替换本路由的数据源（读写 USER_NAME 的两端），
对外的 GET/POST /user/profile 契约保持不变。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import DEFAULT_USER_NAME, MAX_USER_NAME_LENGTH
from env_manager import get_env_file_path, load_var, set_env_vars

# 实例化APIRouter
api_user_profile_router = APIRouter(prefix="/user")

# 显示名在 .env 中的键名（后续换数据库时保留该键作为迁移来源）
USER_NAME_ENV_KEY = "USER_NAME"


class UserProfileRequest(BaseModel):
    """用户资料更新请求体（当前仅显示名）。"""

    name: str = Field(
        ...,
        description="新的显示名；空白串表示清空，界面回退默认「访客用户」",
    )


def _normalize_user_name(value) -> tuple[str | None, str | None]:
    """把任意输入规整为显示名，返回 (显示名, 错误信息)。

    规则：去首尾空白 → 空串视为「未设置」（返回 None，由调用方回退默认名）
    → 含控制字符（换行/制表等，会破坏 .env 单行键值结构）或超长则报错。
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"name 必须是字符串，当前为 {type(value).__name__}"
    text = value.strip()
    if not text:
        return None, None
    if any(ch in "\r\n\t" or ord(ch) < 32 for ch in text):
        return None, "显示名不能包含换行、制表等控制字符"
    if len(text) > MAX_USER_NAME_LENGTH:
        return None, f"显示名最长 {MAX_USER_NAME_LENGTH} 个字符，当前 {len(text)} 个"
    return text, None


def _current_user_name() -> str:
    """读取当前生效显示名：.env USER_NAME（有效时）→ 默认「访客用户」。"""
    normalized, _ = _normalize_user_name(load_var(USER_NAME_ENV_KEY))
    return normalized or DEFAULT_USER_NAME


def _profile_payload() -> dict:
    """组装用户资料响应体（读写接口共用，保证字段一致）。"""
    raw_value = load_var(USER_NAME_ENV_KEY)
    stored, _ = _normalize_user_name(raw_value)
    return {
        "state": "succeed",
        "name": _current_user_name(),
        "default_name": DEFAULT_USER_NAME,
        "max_length": MAX_USER_NAME_LENGTH,
        # .env 是否显式保存过有效显示名（false = 正在使用默认值）
        "persisted": bool(stored),
        "env_name": USER_NAME_ENV_KEY,
        "env_value": raw_value,
        "env_file": get_env_file_path(),
        "source": "project .env",
    }


@api_user_profile_router.get("/profile")
async def get_user_profile():
    """读取全局用户资料（当前仅显示名）。

    Returns:
        {"state", "name", "default_name", "max_length", "persisted",
         "env_name", "env_value", "env_file", "source"}
        - name：当前生效显示名（.env 未设置 / 被清空时回退 default_name）
        - persisted：.env 是否显式保存过显示名
    """
    try:
        return _profile_payload()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取用户配置失败: {e}")


@api_user_profile_router.post("/profile")
async def update_user_profile(req: UserProfileRequest):
    """保存全局显示名到项目 .env 的 USER_NAME 键（写盘 + 内存同步，立即生效）。

    - 传空白串 / 纯空格 = 清空该键值，界面回退默认「访客用户」；
    - 超长、含控制字符 → 400；
    - 写入后立即读回校验：若值因 .env 语法（引号 / # 注释 / ${} 变量引用等）
      发生变形，则回滚到保存前的值并返回 400，避免出现「保存成功但显示不对」；
    - 响应携带写盘后的实际值与 .env 路径，供前端核对。
    """
    try:
        normalized, error = _normalize_user_name(req.name)
        if error:
            raise HTTPException(status_code=400, detail=error)

        previous_raw = load_var(USER_NAME_ENV_KEY)
        # 空值也写入（清空语义）：键位保留为空串，读取侧回退默认名
        target_text = normalized or ""
        set_env_vars({USER_NAME_ENV_KEY: target_text})
        if (load_var(USER_NAME_ENV_KEY) or "") != target_text:
            # 读回不一致（多为 .env 保留语法被解析：引号、#、${}）→ 回滚
            set_env_vars({USER_NAME_ENV_KEY: previous_raw or ""})
            raise HTTPException(
                status_code=400,
                detail=(
                    "显示名包含 .env 不支持的特殊字符（如引号、#、${}），"
                    "保存后会被配置解析改写，请更换后重试"
                ),
            )

        payload = _profile_payload()
        payload["message"] = (
            f"显示名已更新为「{payload['name']}」"
            if normalized
            else f"显示名已恢复默认「{DEFAULT_USER_NAME}」"
        )
        return payload
    except HTTPException:
        raise
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"保存用户配置失败: {e}")
