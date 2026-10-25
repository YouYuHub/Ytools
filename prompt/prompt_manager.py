"""提示词库（Skills）模块：prompt/md_files/ 下的用户自管理 Markdown 文件。

存放可复用的聊天提示词，用户可在前端 Skills 对话框中维护，也可以直接
用任意编辑器增删改该目录下的 .md 文件——本模块只做经过路径安全校验的
增删改查与目录初始化，不解析、不加工文件内容。
"""
import re
from datetime import datetime
from pathlib import Path

from util.timestamp_utils import format_timestamp

# 目录：项目根/prompt/md_files
PROMPT_DIR = Path(__file__).resolve().parent
MD_FILES_DIR = PROMPT_DIR / "md_files"

MAX_NAME_CHARS = 80
MAX_CONTENT_BYTES = 512 * 1024
_MD_SUFFIX = ".md"

# 文件名主干（不含 .md 后缀）白名单：中英文、数字、下划线、连字符、空格、点，
# 首字符必须是文字/数字（同时排除路径分隔符、Windows 保留字符与纯符号名）
_NAME_PATTERN = re.compile(r"^[\w][\w.\- ]*$", re.UNICODE)
# Windows 保留设备名（不区分大小写；即使带 .md 后缀在这些系统上也创建失败）
_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

SAMPLE_PROMPT = """# 示例提示词

这是一个可复用的提示词示例。你可以在 Skills 对话框中编辑它，或点击「加载到输入框」把它填进消息框直接发起对话。

## 常用写法

- **角色设定**：你是一位严谨的代码评审专家……
- **任务步骤**：先列出计划，再逐步执行
- **输出要求**：用表格汇总结果，代码块标注语言

## 输出格式示例

| 步骤 | 内容 | 产出 |
| ---- | ---- | ---- |
| 1 | 阅读需求 | 要点清单 |
| 2 | 编写方案 | 设计文档 |
| 3 | 实现与自测 | 可运行代码 |

```python
print("代码块支持语言高亮")
```

> 提示：支持多级标题、表格、行内代码、代码块等基本 Markdown 语法，不支持图片。
"""


def _directory():
    from memory.storage_layout import require_identity
    require_identity()
    from security.principal import current
    principal = current()
    return MD_FILES_DIR / principal.user_id if principal else MD_FILES_DIR


def ensure_prompt_dir() -> None:
    """确保 md_files 目录存在（首次使用自动创建，并播种一个示例提示词）。"""
    _directory().mkdir(parents=True, exist_ok=True)
    if not any(_directory().glob("*.md")):
        try:
            (_directory() / "示例提示词.md").write_text(SAMPLE_PROMPT, encoding="utf-8")
        except OSError:
            pass  # 示例播种失败不影响功能


def _normalize_name(name: str) -> str:
    """校验并规整文件名：去掉 .md 后缀与首尾空白，返回「主干 + .md」。

    Raises:
        ValueError: 名称为空/过长/含非法字符时抛出，由路由层转 400。
    """
    if not isinstance(name, str):
        raise ValueError("文件名必须是字符串")
    stem = name.strip()
    if stem.lower().endswith(_MD_SUFFIX):
        stem = stem[: -len(_MD_SUFFIX)]
    stem = stem.strip().rstrip(". ")
    if not stem:
        raise ValueError("文件名不能为空")
    if len(stem) > MAX_NAME_CHARS:
        raise ValueError(f"文件名过长（最多 {MAX_NAME_CHARS} 个字符）")
    if not _NAME_PATTERN.match(stem):
        raise ValueError("文件名仅允许中英文、数字、下划线、连字符、空格和点")
    if stem.upper() in _RESERVED_STEMS:
        raise ValueError("文件名不能使用 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）")
    return stem + _MD_SUFFIX


def _resolve_path(name: str) -> Path:
    """把文件名解析为 md_files 内的绝对路径，并拦截目录逃逸。"""
    path = (_directory() / _normalize_name(name)).resolve()
    if _directory().resolve() not in path.parents:
        raise ValueError("非法文件路径")
    return path


def _check_content(content: str) -> None:
    if not isinstance(content, str):
        raise ValueError("内容必须是字符串")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError(f"内容超过大小上限（{MAX_CONTENT_BYTES // 1024}KB）")


def _meta_of(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "updated_at": format_timestamp(datetime.fromtimestamp(stat.st_mtime)),
    }


def list_prompts() -> list[dict]:
    """列出全部提示词文件（按更新时间倒序）。"""
    ensure_prompt_dir()
    paths = [path for path in _directory().glob("*.md") if path.is_file()]
    # Display timestamps have second precision; sort by the actual filesystem
    # timestamp so two edits within one second retain their real order.
    paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    return [_meta_of(path) for path in paths]


def read_prompt(name: str) -> dict:
    """读取单个提示词内容。

    Raises:
        FileNotFoundError: 文件不存在。
    """
    path = _resolve_path(name)
    if not path.is_file():
        raise FileNotFoundError(name)
    meta = _meta_of(path)
    meta["content"] = path.read_text(encoding="utf-8")
    return meta


def create_prompt(name: str, content: str = "") -> dict:
    """新建提示词文件，同名已存在时抛 FileExistsError。"""
    path = _resolve_path(name)
    if path.exists():
        raise FileExistsError(path.name)
    _check_content(content)
    ensure_prompt_dir()
    _write_prompt(path, content, create=True)
    return {"name": path.name}


def save_prompt(name: str, content: str) -> dict:
    """保存（新建或覆盖）提示词内容。"""
    path = _resolve_path(name)
    _check_content(content)
    ensure_prompt_dir()
    _write_prompt(path, content)
    return {"name": path.name}


def _write_prompt(path, content, *, create=False):
    import os
    import uuid
    from security.resource_limits import storage_write
    from memory.identity_store import Throttled
    data = content.encode("utf-8")
    with storage_write() as available:
        if create and path.exists():
            raise FileExistsError(path.name)
        previous = path.stat().st_size if path.exists() else 0
        if available is not None and max(0, len(data) - previous) > available:
            raise Throttled("账号存储额度不足，提示词原内容未修改")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def delete_prompt(name: str) -> None:
    """删除提示词文件，不存在时抛 FileNotFoundError。"""
    path = _resolve_path(name)
    if not path.is_file():
        raise FileNotFoundError(name)
    path.unlink()


def rename_prompt(old_name: str, new_name: str) -> dict:
    """重命名提示词文件，目标同名已存在时抛 FileExistsError。"""
    src = _resolve_path(old_name)
    dst = _resolve_path(new_name)
    if not src.is_file():
        raise FileNotFoundError(old_name)
    if dst.exists():
        raise FileExistsError(dst.name)
    src.rename(dst)
    return {"name": dst.name}
