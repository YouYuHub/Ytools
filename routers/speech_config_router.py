"""Account speech preference; standalone calls retain instance defaults."""
from threading import RLock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

import env_manager

api_speech_config_router = APIRouter(prefix="/chat_config")
ENV_KEY = "SPEECH_INPUT_LANGUAGE"
DEFAULT_LANGUAGE = "zh-CN"
SUPPORTED_LANGUAGES = (
    "zh-CN", "zh-TW", "en-US", "en-GB", "fr-FR", "de-DE", "ja-JP", "ko-KR",
    "es-ES", "pt-BR", "ru-RU", "it-IT", "ar-SA", "hi-IN", "bn-BD", "ur-PK",
    "id-ID", "vi-VN", "th-TH", "tr-TR",
)
_lock = RLock()


class SpeechInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: StrictStr
    only_if_unset: StrictBool = False


def _stored_language() -> str | None:
    from security.principal import current
    from security.preferences import runtime_value
    raw = runtime_value(ENV_KEY) if current() else env_manager.env_vars.get(ENV_KEY)
    value = raw.strip() if isinstance(raw, str) else ""
    return value if value in SUPPORTED_LANGUAGES else None


def _payload() -> dict:
    stored = _stored_language()
    from security.principal import current
    fallback = env_manager.env_vars.get(ENV_KEY, DEFAULT_LANGUAGE)
    if fallback not in SUPPORTED_LANGUAGES:
        fallback = DEFAULT_LANGUAGE
    return {
        "language": stored or fallback,
        "default_language": DEFAULT_LANGUAGE,
        "persisted": stored is not None,
        "env_name": ENV_KEY,
        "source": "account" if current() else "project .env",
    }


@api_speech_config_router.get("/speech_input")
def get_speech_input_config():
    """Read only the speech language; never expose other .env values."""
    with _lock:
        return _payload()


@api_speech_config_router.post("/speech_input")
def update_speech_input_config(request: SpeechInputRequest):
    """Save language, or conditionally migrate a legacy browser preference."""
    language = request.language.strip()
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail="不支持的语音识别语言，请从语言列表中选择")
    from security.principal import current
    if current() is not None:
        from security.preferences import set_runtime
        previous = _stored_language()
        set_runtime({ENV_KEY: language}, only_if_unset=request.only_if_unset)
        return {**_payload(), "updated": not (request.only_if_unset and previous is not None)}
    with _lock:
        if request.only_if_unset and _stored_language() is not None:
            return {**_payload(), "updated": False}
        if not env_manager.get_env_file_path():
            raise HTTPException(status_code=503, detail="项目 .env 尚未初始化，暂不能保存语音语言")
        existed = ENV_KEY in env_manager.env_vars
        previous = env_manager.env_vars.get(ENV_KEY)
        try:
            env_manager.set_env_vars({ENV_KEY: language})
        except (OSError, RuntimeError):
            # set_env_vars updates memory before writing. Do not claim that a
            # failed disk write has successfully changed the project setting.
            if existed:
                env_manager.env_vars[ENV_KEY] = previous
            else:
                env_manager.env_vars.pop(ENV_KEY, None)
            raise HTTPException(status_code=500, detail="保存语音语言失败，请检查项目 .env 的写入权限") from None
        return {**_payload(), "updated": True}
