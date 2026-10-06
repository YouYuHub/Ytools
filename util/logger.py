"""项目统一日志基础设施（基于标准库 logging）。

设计要点
--------
- 命名空间：所有项目日志挂在 ``ytools`` logger 下（子 logger 如 ``ytools.main``），
  不干扰 uvicorn / fastapi 等第三方库自己的日志配置；
- 级别配置：读取 .env 的 ``LOG_LEVEL``（debug / info / warning / error / critical，
  大小写不敏感；兼容 warn→warning、fatal→critical、trace→debug）；
  未配置或非法时默认 ``info``；
- 终端输出：配置级别及以上的日志带时间戳打印到 stderr；
- 文件输出：**ERROR 及以上**追加写入项目根 ``log/error.log``（目录不存在自动创建，
  RotatingFileHandler 滚动 10MB × 5 备份），格式含完整文件路径、行号与函数名；
- DEBUG 专属：SSE 流式数据（模型增量输出）在 DEBUG 等级时以原始 ``print`` 形式
  实时打印到终端（不经日志格式化、不加时间戳，保真流式观感），见
  :func:`debug_stream_print`；
- 多进程：worker 子进程（Windows spawn）重建环境后调用 ``setup_logging(force=True)``
  重新读取 .env 生效；模块 import 期（env 尚未加载时）以默认 info 兜底，
  主进程在 ``init_path()`` 之后显式调用 ``setup_logging(force=True)`` 校准。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "debug_stream_print",
    "get_logger",
    "is_debug_enabled",
    "is_level_enabled",
    "setup_logging",
]

# 日志命名空间根（所有项目日志挂在它下面；不影响第三方库日志）
_LOGGER_ROOT = "ytools"
# 日志目录：项目根/log（以本文件位置推导，不依赖运行时 cwd——worker 会切换工作目录）
_LOG_DIR = Path(__file__).resolve().parent.parent / "log"
_ERROR_LOG_FILENAME = "error.log"
_FILE_MAX_BYTES = 10 * 1024 * 1024   # 单文件上限 10MB
_FILE_BACKUP_COUNT = 5               # 滚动保留 5 份历史

_CONSOLE_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
# 文件格式：时间戳 + 级别 + logger 名 + 完整文件路径 + 行号 + 函数名（错误定位友好）
_FILE_FORMAT = (
    "[%(asctime)s] [%(levelname)s] [%(name)s] "
    "%(pathname)s:%(lineno)d %(funcName)s(): %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 常见别名 → 标准级别名
_LEVEL_ALIASES = {
    "WARN": "WARNING",
    "FATAL": "CRITICAL",
    "TRACE": "DEBUG",
}
_STANDARD_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_setup_lock = threading.RLock()
_configured = False


def _normalize_level_name(raw: str | None) -> str | None:
    """把用户配置的级别名归一化为标准名；非法值返回 None。"""
    if raw is None:
        return None
    name = str(raw).strip().upper()
    if not name:
        return None
    name = _LEVEL_ALIASES.get(name, name)
    return name if name in _STANDARD_LEVELS else None


def _read_level_from_env() -> str | None:
    """读取 ``LOG_LEVEL``：优先 .env（经 env_manager），其次进程环境变量。

    env_manager 延迟导入，避免与 env_manager 自身的 logger 引用形成循环依赖；
    env 尚未初始化（load_var 返回默认值）时回退进程环境变量。
    """
    try:
        from env_manager import load_var  # 延迟导入：破除循环依赖
        name = _normalize_level_name(load_var("LOG_LEVEL", None))
        if name:
            return name
    except Exception:
        pass
    return _normalize_level_name(os.environ.get("LOG_LEVEL"))


def setup_logging(force: bool = False) -> int:
    """初始化 / 重配项目日志系统，返回生效的级别值。

    - 幂等：已配置且 ``force=False`` 时直接返回当前级别；
    - ``force=True`` 用于三类重配时机：主进程 ``init_path()`` 之后、
      .env 热重载回调、worker 子进程重建运行环境之后。
    """
    global _configured
    with _setup_lock:
        logger = logging.getLogger(_LOGGER_ROOT)
        if _configured and not force:
            return logger.level

        level_name = _read_level_from_env() or "INFO"
        level_value = getattr(logging, level_name)
        logger.setLevel(level_value)
        logger.propagate = False  # 不向 root 冒泡，避免与第三方日志重复输出

        # 替换旧 handlers（force 重配时清理，防止重复挂载）
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setLevel(logging.NOTSET)  # 由 logger 级别统一过滤
        console_handler.setFormatter(
            logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT)
        )
        logger.addHandler(console_handler)

        # 文件仅收 ERROR 及以上（用户口径：error 以上记录到文件）
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(_LOG_DIR / _ERROR_LOG_FILENAME),
                maxBytes=_FILE_MAX_BYTES,
                backupCount=_FILE_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setLevel(logging.ERROR)
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT)
            )
            logger.addHandler(file_handler)
        except Exception as exc:  # 日志目录不可用时降级为仅终端输出
            logger.warning("日志文件初始化失败（仅终端输出）: %s", exc)

        _configured = True
        return level_value


def get_logger(name: str | None = None) -> logging.Logger:
    """获取项目命名空间下的 logger。

    ``name`` 传模块名（如 ``"factory.chat_factory"``），最终 logger 名为
    ``ytools.factory.chat_factory``；不传则返回命名空间根 logger。
    """
    setup_logging()
    if not name:
        return logging.getLogger(_LOGGER_ROOT)
    prefix = _LOGGER_ROOT + "."
    if str(name).startswith(prefix):
        return logging.getLogger(str(name))
    return logging.getLogger(f"{prefix}{name}")


def is_level_enabled(level: int) -> bool:
    """当前配置下，指定 logging 级别是否会输出（供高频调用点做前置判断）。"""
    setup_logging()
    return logging.getLogger(_LOGGER_ROOT).isEnabledFor(level)


def is_debug_enabled() -> bool:
    """DEBUG 等级是否生效。"""
    return is_level_enabled(logging.DEBUG)


def debug_stream_print(text: str) -> None:
    """DEBUG 等级时把 SSE 流式增量原样打印到终端。

    刻意使用内置 ``print``（``end=""`` / ``flush=True``）：流式高频输出不加
    时间戳、不做日志格式化，保持逐字实时观感；非 DEBUG 等级静默丢弃
    （高频调用点开销仅为一次级别判断）。
    """
    if is_debug_enabled():
        print(text, end="", flush=True)
