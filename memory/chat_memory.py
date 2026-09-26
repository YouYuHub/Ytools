""" 记录当前轮次对话所有工具调用的历史 - 支持多会话文件持久化 """
# from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import copy
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# fastapi
from fastapi import HTTPException
from fastapi.responses import FileResponse

from factory.agent_runtime.builtin_tools import ASK_ANSWER_PREFIX, ASK_USER_TOOL_NAME
from memory import file_memory
from memory.chat_round_store import (
    ChatRoundStore,
    compression_usage_values,
    merge_compression_usage,
    merge_usage_dict,
    parse_round_entry,
)
from util.file_lock import cross_process_lock
from util.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT, now_str
from config import (
    DEFAULT_CONTEXT_HISTORY_ROUNDS,
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    get_global_tool_inputs,
    get_persisted_work_dir,
    normalize_tool_inputs,
    resolve_work_dir,
)

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)

# 会话上传文件的根目录（与 memory.file_memory.HISTORY_ROOT 保持一致：
# history_files/session_files/，由 upload 目录改名而来）
UPLOAD_HISTORY_ROOT = HISTORY_ROOT / "session_files"

# 跨进程写锁文件的统一存放目录：避免 *.lock 散落在历史文件根目录
# （会话附属侧车 .todo/.pending/.bak/.tmp 同样集中在 sidecars/ 子目录）
LOCK_ROOT = HISTORY_ROOT / "lock"

# 会话附属侧车文件（进行中轮次检查点 .pending、任务计划 .todo、写前备份
# .bak、原子写临时文件 .tmp）的统一存放目录：历史根目录只保留 *.jsonl
# 正文件。惰性求值（每次实时读 HISTORY_ROOT）：测试会把 HISTORY_ROOT
# 指向临时目录，模块级常量快照会绕过该替换
def _sidecar_dir() -> Path:
    root = HISTORY_ROOT / "sidecars"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _migrate_legacy_sidecar_files() -> None:
    """把旧版散落在 history_files 根目录的会话侧车文件迁移到 sidecars/ 子目录。

    覆盖 <session>_chat.jsonl.todo / .pending / .bak / .tmp（与 lock/ 迁移
    同款策略）。个别文件正被其他进程持有时 Windows 会拒绝移动，静默跳过
    留待下次启动再迁；迁移后旧路径不再被读取（重启部署后所有进程统一新路径）。
    """
    try:
        for legacy in HISTORY_ROOT.glob("*_chat.jsonl.*"):
            if not legacy.is_file():
                continue
            try:
                legacy.replace(_sidecar_dir() / legacy.name)
            except OSError:
                pass
    except OSError:
        pass


_migrate_legacy_sidecar_files()


def _migrate_legacy_lock_files() -> None:
    """把旧版散落在 history_files 根目录的 *.lock 迁移到 lock/ 子目录。

    锁文件内容为空、只承载句柄互斥语义，迁移后新旧路径不要求互通：
    服务重启（部署本改动）后所有进程统一使用新路径。个别文件正被
    其他进程持有时 Windows 会拒绝移动，静默跳过留待下次启动再迁。
    """
    try:
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        for legacy in HISTORY_ROOT.glob("*.lock"):
            try:
                legacy.replace(LOCK_ROOT / legacy.name)
            except OSError:
                pass
    except OSError:
        pass


_migrate_legacy_lock_files()



def _safe_session_id(session_id: str) -> str:
    """把 session_id 规整为安全文件名片段。

    会话标识以文件名为准，因此必须无损保留合法的文件名字符：
    - 保留 Unicode 字母/数字（含中文）、空格、下划线、点、横线
    - 路径分隔符等其余字符替换为下划线
    - 拦截 `..` 防止路径穿越
    """
    value = re.sub(r"[^\w .-]+", "_", session_id)
    value = value.replace("..", "_")
    return value.strip(" ._-")


def _get_chat_history_file(session_id: str) -> Path:
    safe_id = _safe_session_id(session_id)
    return HISTORY_ROOT / f"{safe_id}_chat.jsonl"


def _pid_alive(pid: Any) -> bool:
    """判断进程是否存活（Windows/POSIX 通用，尽力而为）。

    检查点写者存活校验用：写者进程仍活着时不能把检查点恢复成历史轮次。
    psutil 不可用时 Windows 用 OpenProcess 探测（非破坏性，不向目标进程
    发信号）；POSIX 用 os.kill(pid, 0) 存在性检查。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # 打开失败但错误码为拒绝访问 → 进程存在（通常是系统保护进程）
            return kernel32.GetLastError() == ERROR_ACCESS_DENIED
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# def _write_jsonline(file_path: Path, data: dict[str, Any]) -> None:
#     with file_path.open("a+", encoding="utf-8") as fp:
#         fp.write(json.dumps(data, ensure_ascii=False) + "\n")


def _read_jsonlines(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _default_title(session_id: str) -> str:
    return f"session_{session_id}"


def _default_meta(session_id: str) -> dict[str, Any]:
    now = now_str()
    return {
        "title": _default_title(session_id),
        "user_questions": [],
        "todo": [],
        "usage": {},
        "created_at": now,
        "updated_at": now,
        "record_count": 0,
        "completion_count": 0,
        "context_summary": None,
        "compress_usage": {},
        # 会话独立工作目录：None 表示未覆盖，跟随全局默认 DEFAULT_CHAT_WORK_DIR。
        # 惰性写入——只有用户显式设置过才落盘，新会话不预填
        "work_dir": None,
        # 会话独立工具选择：None 表示未覆盖，跟随全局默认（mcp_servers.json 的 inputs 键）。
        # 结构与 inputs 一致：{服务名: [工具名]}；全取消勾选后保存会写入「空选择哨兵」
        # （EMPTY_TOOL_SELECTION_KEY），表示显式无工具模式而非未覆盖
        "tool_selection": None,
        # 会话独立模型选择：None 表示未覆盖，跟随全局默认（models.json 顶层 model_selection）。
        # 结构为 {角色: {ownership_name, model_name, parameter, api_type}}，仅存已覆盖的角色；
        # 空选择/非法条目视为未覆盖（清空即恢复跟随全局）
        "model_selection": None,
    }


def normalize_session_id(raw: Any) -> str:
    """把任意输入规整为合法 session_id。

    会话标识以文件名为准（`history_files/<session_id>_chat.jsonl`），
    因此这里会：
    - 兼容直接传入文件名（去掉 `_chat.jsonl` / `.jsonl` 后缀）
    - 无损保留合法的文件名字符（中文等 Unicode 字符、空格、`_ . -`），
      仅把路径分隔符等危险字符替换为下划线（与 _safe_session_id 一致）
    - 空值回退为 "default"
    """
    if raw is None:
        return "default"
    value = str(raw).strip()
    # 兼容直接传入文件名（如 web-xxx_chat.jsonl / web-xxx.jsonl）
    value = re.sub(r"_chat\.jsonl$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.jsonl$", "", value, flags=re.IGNORECASE)
    value = _safe_session_id(value).strip(" ._-")
    return value or "default"


def _is_meta_record(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("_meta"), dict)


def read_session_meta_value(session_id: str, key: str) -> Any:
    """只读读取会话首行 _meta 的指定字段（不创建文件/目录，不触发重算落盘）。

    供解析会话工作目录等轻量场景使用；文件不存在或首行非 _meta 返回 None。
    """
    try:
        file_path = _get_chat_history_file(normalize_session_id(session_id))
    except Exception:
        return None
    if not file_path.exists():
        return None
    rows = _read_jsonlines(file_path)
    if rows and _is_meta_record(rows[0]):
        meta = rows[0].get("_meta")
        if isinstance(meta, dict):
            return meta.get(key)
    return None


# ---------- 会话分组（分组注册表 + 会话归属） ----------
# 归属真源：会话 JSONL 首行 _meta.group_id（None/缺失 = 未分组）。
# 分组定义真源：history_files/session_groups.json（单文件小 JSON，跨进程锁 +
# 原子写）。双写一致性规则：删除分组时解除其全部成员归属；会话删除时注册表
# 不记录成员、天然一致；重命名分组只动注册表。

SESSION_GROUPS_FILENAME = "session_groups.json"
SESSION_GROUP_NAME_MAX = 40
SESSION_GROUP_ID_PREFIX = "g-"


def _session_groups_file() -> Path:
    """分组注册表文件路径（惰性求值：测试会把 HISTORY_ROOT 指向临时目录）。"""
    return HISTORY_ROOT / SESSION_GROUPS_FILENAME


def _session_groups_lock_path() -> Path:
    return LOCK_ROOT / f"{SESSION_GROUPS_FILENAME}.lock"


def _new_group_id() -> str:
    return SESSION_GROUP_ID_PREFIX + uuid.uuid4().hex[:12]


def _normalize_group_name(raw: Any) -> str:
    """规整分组名：trim、折叠控制字符、截断到上限；空名抛 ValueError。"""
    name = re.sub(r"[\r\n\t]+", " ", str(raw or "")).strip()
    if not name:
        raise ValueError("分组名不能为空")
    return name[:SESSION_GROUP_NAME_MAX]


def _read_session_groups_raw() -> list[dict[str, Any]]:
    """读注册表原始条目；文件缺失/损坏返回空列表（尽力而为，不抛错）。"""
    path = _session_groups_file()
    try:
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    groups = raw.get("groups") if isinstance(raw, dict) else raw
    if not isinstance(groups, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not gid or not name:
            continue
        cleaned.append({
            "id": gid,
            "name": name[:SESSION_GROUP_NAME_MAX],
            "created_at": str(item.get("created_at") or ""),
            "collapsed": bool(item.get("collapsed")),
            "order": item.get("order") if isinstance(item.get("order"), int) else 0,
        })
    return cleaned


def _write_session_groups_raw(groups: list[dict[str, Any]]) -> None:
    """原子写注册表（tmp + os.replace + 占用重试，与 _meta 写同口径）。"""
    path = _session_groups_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _sidecar_dir() / f"{SESSION_GROUPS_FILENAME}.tmp"
    payload = json.dumps({"groups": groups}, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    for delay in _ATOMIC_WRITE_RETRY_DELAYS:
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
            return
        except OSError as err:
            last_error = err
            time.sleep(delay)
    raise last_error


def list_session_groups() -> list[dict[str, Any]]:
    """列出全部分组（按 order、created_at 排序）。"""
    groups = _read_session_groups_raw()
    groups.sort(key=lambda g: (g.get("order") or 0, g.get("created_at") or ""))
    return groups


def list_session_group_assignments() -> dict[str, str]:
    """扫描全部会话首行 _meta，返回 {session_id: group_id} 归属映射。

    归属随会话文件走（会话删除/新增无需维护注册表），首行只读解析，
    损坏/旧格式文件静默跳过。
    """
    assignments: dict[str, str] = {}
    try:
        files = list(HISTORY_ROOT.glob("*_chat.jsonl"))
    except OSError:
        return assignments
    for file_path in files:
        meta = _read_meta_first_line(file_path)
        if not isinstance(meta, dict):
            continue
        gid = meta.get("group_id")
        if not isinstance(gid, str) or not gid.strip():
            continue
        name = file_path.name
        session_id = name[:-len("_chat.jsonl")] if name.endswith("_chat.jsonl") else file_path.stem
        assignments[session_id] = gid.strip()
    return assignments


def create_session_group(name: str) -> dict[str, Any]:
    """新建分组；同名分组已存在时直接返回既有分组（幂等）。"""
    clean_name = _normalize_group_name(name)
    with cross_process_lock(_session_groups_lock_path()):
        groups = _read_session_groups_raw()
        for item in groups:
            if item.get("name") == clean_name:
                return item
        group = {
            "id": _new_group_id(),
            "name": clean_name,
            "created_at": now_str(),
            "collapsed": False,
            "order": len(groups),
        }
        groups.append(group)
        _write_session_groups_raw(groups)
    return group


def update_session_group(
    group_id: str,
    name: str | None = None,
    collapsed: bool | None = None,
) -> dict[str, Any]:
    """更新分组名 / 折叠状态；分组不存在抛 ValueError。"""
    gid = str(group_id or "").strip()
    if not gid:
        raise ValueError("group_id 不能为空")
    with cross_process_lock(_session_groups_lock_path()):
        groups = _read_session_groups_raw()
        target = next((g for g in groups if g["id"] == gid), None)
        if target is None:
            raise ValueError(f"分组不存在: {gid}")
        if name is not None:
            target["name"] = _normalize_group_name(name)
        if collapsed is not None:
            target["collapsed"] = bool(collapsed)
        _write_session_groups_raw(groups)
    return target


def _set_session_group_id(session_id: str, group_id: str | None) -> bool:
    """写会话 _meta.group_id（None=清除归属）；成功返回 True。

    不改动 updated_at：归组不是内容更新，不应扰乱「最近」排序。
    """
    try:
        sid = normalize_session_id(session_id)
        file_path = _get_chat_history_file(sid)
    except Exception:
        return False
    if not file_path.exists():
        return False
    with cross_process_lock(LOCK_ROOT / f"{file_path.name}.lock"):
        try:
            meta, entries = _load_meta_and_entries(file_path, sid)
        except OSError:
            return False
        if group_id is None:
            meta.pop("group_id", None)
        else:
            meta["group_id"] = group_id
        try:
            _write_meta_and_entries(file_path, meta, entries)
        except OSError:
            return False
    return True


def delete_session_group(group_id: str) -> dict[str, Any]:
    """删除分组：先解除全部成员会话的 _meta.group_id，再移除注册表条目。"""
    gid = str(group_id or "").strip()
    if not gid:
        raise ValueError("group_id 不能为空")
    released = 0
    for session_id, assigned in list_session_group_assignments().items():
        if assigned != gid:
            continue
        if _set_session_group_id(session_id, None):
            released += 1
    with cross_process_lock(_session_groups_lock_path()):
        groups = _read_session_groups_raw()
        remaining = [g for g in groups if g["id"] != gid]
        removed = len(groups) - len(remaining)
        if removed:
            _write_session_groups_raw(remaining)
    return {"state": "succeed", "group_id": gid, "removed": removed, "released_sessions": released}


def assign_session_group(session_id: str, group_id: str | None) -> dict[str, Any]:
    """把会话加入分组（group_id=None/空 = 移出分组）。

    写入前校验分组存在；会话文件不存在/写盘失败抛 ValueError。
    """
    sid = normalize_session_id(session_id)
    gid = str(group_id or "").strip() or None
    if gid is not None:
        exists = any(g["id"] == gid for g in _read_session_groups_raw())
        if not exists:
            raise ValueError(f"分组不存在: {gid}")
    if not _set_session_group_id(sid, gid):
        raise ValueError(f"会话不存在或写入失败: {sid}")
    return {"state": "succeed", "session_id": sid, "group_id": gid}


def mark_title_attempted(session_id: str) -> dict[str, Any] | None:
    """在 _title_state 上标记"已尝试生成标题"（title_attempted_at + attempted）。

    标题模型调用失败后调用：防止上游持续不可用时每个新任务都白发一次
    标题请求。会话不存在/无 _meta/写盘失败一律静默返回 None（标题任务
    本身已失败，此标记尽力而为）。开启「每条消息重新标题」开关时该
    标记会被清除并在每轮重新生成时不再写入。
    """
    try:
        file_path = _get_chat_history_file(normalize_session_id(session_id))
        if not file_path.exists():
            return None
        meta, entries = _load_meta_and_entries(file_path, normalize_session_id(session_id))
        if not isinstance(meta, dict) or not meta:
            return None
        title_state = dict(meta.get("_title_state")) if isinstance(meta.get("_title_state"), dict) else {}
        if title_state.get("title_generated"):
            return None
        title_state["attempted"] = True
        title_state["title_attempted_at"] = now_str()
        meta["_title_state"] = title_state
        meta["updated_at"] = now_str()
        _write_meta_and_entries(file_path, meta, entries)
        return meta
    except Exception as exc:
        print(f"[WARN] 标题尝试标记写入失败（不影响聊天任务）: {exc}")
        return None


def resolve_session_work_dir(session_id: str) -> tuple[str | None, str | None]:
    """解析会话生效工作目录（惰性：不落盘、不 chdir）。

    解析顺序（会话覆盖优先，全局默认兜底）：
    1. `_meta.work_dir` 存在且目录有效 → 直接生效；
    2. `_meta.work_dir` 存在但已失效 → 记录警告，回退全局默认；
    3. `.env` 的 DEFAULT_CHAT_WORK_DIR 有效 → 生效（新会话的初始目录）；
    4. 都没有 → (None, None)，调用方保持当前进程 cwd。

    Returns:
        (生效目录绝对路径或 None, 警告消息或 None)
    """
    warning: str | None = None
    override = read_session_meta_value(session_id, "work_dir")
    if isinstance(override, str) and override.strip():
        resolved = resolve_work_dir(override)
        if resolved is not None:
            return str(resolved), None
        warning = f"会话工作目录已失效（{override}），已回退默认工作目录"
    persisted = get_persisted_work_dir()
    if isinstance(persisted, str) and persisted.strip():
        resolved = resolve_work_dir(persisted)
        if resolved is not None:
            return str(resolved), warning
        warning = warning or f"默认工作目录已失效（{persisted}），保持当前目录"
    return None, warning


# 「空选择哨兵」：会话级工具选择全取消后保存的落盘形态。
# normalize_tool_inputs 对空数组键原样保留（{"__empty__": []} 规整后不变），
# 使空选择在 meta 重算/清空历史/配置快照等既有链路中不被当作「未覆盖」清除；
# 解析时命中哨兵 → 会话显式无工具模式，不再回退全局默认。
# 兼容：旧落盘无哨兵（未覆盖=None）语义不变；读到了手工构造的同形 dict 也按哨兵处理。
EMPTY_TOOL_SELECTION_KEY = "__empty__"
_EMPTY_TOOL_SELECTION = {EMPTY_TOOL_SELECTION_KEY: []}


def resolve_session_tool_selection(
    session_id: str,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """解析会话生效工具选择（惰性：只读，不落盘）。

    解析顺序（会话覆盖优先，全局默认兜底）：
    1. `_meta.tool_selection` 为覆盖快照时按快照生效：
       - 非空 {服务名: [工具名]} → 直接生效；
       - 空选择哨兵（EMPTY_TOOL_SELECTION_KEY）→ 会话显式无工具模式，返回 ({}，None)；
         请求未携带 tool_names 时由生成流程按无工具处理，不再回退全局默认；
    2. 未覆盖（None）/非法 → 回退全局默认（mcp_servers.json 的 inputs 键）；
    3. 全局配置读取失败 → 记录警告，返回 (None, 警告)，调用方按无默认工具处理。

    Returns:
        (生效工具选择或 None（None=无工具），警告消息或 None)
    """
    override = read_session_meta_value(session_id, "tool_selection")
    if override is not None:
        normalized_override = normalize_tool_inputs(override)
        if normalized_override == _EMPTY_TOOL_SELECTION:
            # 会话显式无工具：不回退全局默认（覆盖语义完整）
            return {}, None
        if normalized_override:
            return normalized_override, None
    inputs, _servers, error = get_global_tool_inputs()
    if error:
        return None, f"全局默认工具配置读取失败（{error}），本轮按未配置默认工具处理"
    return (inputs or None), None


def resolve_session_model_selection(
    session_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """解析会话生效模型选择（惰性：只读，不落盘；返回全部角色的合并结果）。

    解析顺序（按角色独立覆盖，全局默认兜底）：
    1. `_meta.model_selection` 中该角色的覆盖条目存在且模型仍在 models.json 目录中 → 生效；
    2. 覆盖条目引用的模型已不存在（如 models.json 被手工编辑）→ 记录警告，该角色回退全局默认；
    3. 未覆盖的角色直接使用全局默认（models.json 顶层 model_selection）。

    Returns:
        ({role: {ownership_name, model_name, parameter, api_type}} 全角色合并结果, 警告消息列表)
    """
    effective = _get_global_model_selection()
    warnings: list[str] = []
    override = read_session_meta_value(session_id, "model_selection")
    normalized_override = _normalize_model_selection(override) if override is not None else {}
    for role in _MODEL_SELECTION_ROLES:
        entry = normalized_override.get(role) if isinstance(normalized_override, dict) else None
        if not (isinstance(entry, dict) and entry.get("ownership_name") and entry.get("model_name")):
            continue
        provider = entry.get("ownership_name")
        model = entry.get("model_name")
        if _get_model_config(provider, model) is None:
            warnings.append(
                f"会话独立{role} 已失效（{provider} / {model} 不在 models.json 中），已回退全局默认"
            )
            continue
        effective[role] = entry
    return effective, warnings


# 摘要/历史格式化已迁移到 memory.chat_history_format，本文件仅保留调用入口
from memory.chat_history_format import (
    normalize_context_summary as _normalize_context_summary,
    adjust_context_summary_for_deleted_rounds as _adjust_summary_for_deleted_rounds,
    clamp_context_summary_to_rounds as _clamp_summary_to_rounds,
    render_context_summary as _render_context_summary,
    render_recent_questions_message as _render_recent_questions_message,
    extract_recent_questions as _extract_recent_questions,
    extract_recent_question_items as _extract_recent_question_items,
    split_context_window as _split_context_window,
    RECENT_QUESTIONS_TOKEN_BUDGET,
    round_entry_to_context_messages as _round_entry_to_context_messages,
    round_entry_compression_usage as _round_entry_compression_usage,
    format_tool_call_history_line as _format_tool_call_history_line,
    _round_question as _extract_round_question,
)
from env_manager import load_var as _load_var
from env_manager import normalize_model_selection as _normalize_model_selection
from env_manager import get_model_config as _get_model_config
from env_manager import get_global_model_selection as _get_global_model_selection
from env_manager import MODEL_SELECTION_ROLES as _MODEL_SELECTION_ROLES
from factory.agent_runtime.chat_runtime import (
    estimate_messages_tokens as _estimate_messages_tokens,
    estimate_request_context_tokens as _estimate_request_context_tokens,
    estimate_text_tokens as _estimate_text_tokens,
    estimate_tool_definition_tokens as _estimate_tool_definition_tokens,
    parse_return_length as _parse_return_length,
    resolve_model_max_input_tokens as _resolve_model_max_input_tokens,
)


def _estimate_pending_reasoning_tokens(pending_round: Any) -> int:
    """发送口径补偿：估算"随请求回传的最新思考"token 数。

    真实请求（copy_for_request）只回传最近一次 API 调用输出的思考，历史轮次
    思考一律剥离；而历史展开器不携带思考字段。当前轮（pending）的最后一条
    非空思考会保留在真实请求中，统计需按同一规则补偿（否则前端显示低估）。
    返回 0 表示无补偿（无 pending 轮 / 无思考 / 配置为 0 不回传）。
    """
    if not isinstance(pending_round, dict):
        return 0
    pending_events = pending_round.get("events")
    if not isinstance(pending_events, list):
        return 0
    for event in reversed(pending_events):
        if not isinstance(event, dict) or event.get("role") != "assistant":
            continue
        reasoning = event.get("reasoning_content")
        if not (isinstance(reasoning, str) and reasoning.strip()):
            continue
        reasoning_limit = _parse_return_length(
            _load_var(
                "REASONING_RETURN_MAX_LENGTH",
                DEFAULT_REASONING_RETURN_MAX_LENGTH,
            ),
            DEFAULT_REASONING_RETURN_MAX_LENGTH,
        )
        if reasoning_limit == 0:
            return 0  # 不回传（仅占位符）：无补偿
        kept_reasoning = (
            reasoning if reasoning_limit < 0
            else reasoning[-reasoning_limit:]
        )
        return _estimate_text_tokens(kept_reasoning) + 4
    return 0


def _default_context_history_rounds() -> int:
    """上下文 API 省略轮数时的默认值：0（无限轮次窗口）。

    历史轮次窗口（HISTORY_COMPACT_KEEP_ROUNDS）已废弃：历史规模由压缩阈值与
    历史压缩目标控制，不再按"保留最近 N 轮"截断；保留该函数返回 0 以保持
    旧调用点语义（<=0 即"不限制轮次"）。
    """
    return 0


def _normalize_usage_record_usage(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    usage_total = {}
    completion_count = 0
    if entry.get("event") == "chat_round":
        round_total = entry.get("usage_total")
        if isinstance(round_total, dict):
            usage_total = round_total
        if isinstance(entry.get("completion_count"), int):
            completion_count = entry.get("completion_count", 0)
    return usage_total, completion_count


# 上述格式化函数已迁移至 memory/chat_history_format.py（统一入口）
# 为保留向后兼容，旧名字仍然指向新实现，供其他模块使用。


def _recompute_meta_from_entries(
    session_id: str,
    base_meta: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    bump_updated_at: bool = True,
) -> dict[str, Any]:
    now = now_str()
    meta = dict(base_meta) if isinstance(base_meta, dict) else _default_meta(session_id)
    # 会话标识以文件名为准，不再写入 _meta；每次重算都要清理，
    # 因为上传的 jsonl（如 web-xxx_chat.jsonl）其 _meta 里仍带 session_id，
    # 不是“只清理一次”就能完成的
    meta.pop("session_id", None)
    if not meta.get("created_at"):
        meta["created_at"] = now
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        meta["title"] = _default_title(session_id)
    questions: list[str] = []
    usage_total: dict[str, Any] = {}
    compress_usage_total: dict[str, Any] = {}
    completion_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "chat_round":
            # question 为空时回退从事件提取（多模态消息的文本部件），
            # 兼容修复前已落盘的空 question 轮次，重建 user_questions / 会话标题
            question = _extract_round_question(entry)
            if question:
                questions.append(question)
        entry_usage_total, entry_completion_count = _normalize_usage_record_usage(entry)
        if entry_usage_total:
            merge_usage_dict(usage_total, entry_usage_total)
        if entry.get("event") == "chat_round":
            merge_compression_usage(
                compress_usage_total,
                _round_entry_compression_usage(entry),
            )
        completion_count += entry_completion_count
    history_compress_usage = meta.get("_history_compress_usage")
    if merge_compression_usage(compress_usage_total, history_compress_usage):
        merge_usage_dict(usage_total, compression_usage_values(history_compress_usage))
    active_round_compaction = meta.get("_active_round_compaction")
    if isinstance(active_round_compaction, dict):
        active_usage = _round_entry_compression_usage({
            "events": active_round_compaction.get("events", []),
        })
        if not active_usage:
            active_usage = active_round_compaction.get("compress_usage")
        if merge_compression_usage(compress_usage_total, active_usage):
            merge_usage_dict(usage_total, compression_usage_values(active_usage))
    meta["user_questions"] = questions
    meta["usage"] = usage_total
    meta["compress_usage"] = compress_usage_total
    meta["record_count"] = len(entries)
    meta["completion_count"] = completion_count
    # 会话工作目录：显式 None 表示未覆盖（保留）；非字符串/空白视为非法清除。
    # 目录是否存在不在重算时校验（失效由 resolve_session_work_dir 回退处理），
    # 避免目录临时被占用（如重命名）时静默丢失用户配置
    work_dir = meta.get("work_dir")
    if work_dir is not None and (not isinstance(work_dir, str) or not work_dir.strip()):
        meta.pop("work_dir", None)
    # 会话工具选择：None 表示未覆盖（保留）；非 dict 或规整后为空（无有效工具条目）
    # 视为未覆盖清除，恢复跟随全局默认 inputs
    tool_selection = meta.get("tool_selection")
    if tool_selection is not None:
        normalized_selection = normalize_tool_inputs(tool_selection)
        if normalized_selection:
            meta["tool_selection"] = normalized_selection
        else:
            meta.pop("tool_selection", None)
    # 会话模型选择：None 表示未覆盖（保留）；仅保留配置完整的角色条目
    # （模型是否仍存在不在重算时校验，失效由 resolve_session_model_selection 回退全局处理），
    # 非法结构或空选择视为未覆盖清除
    model_selection_override = meta.get("model_selection")
    if model_selection_override is not None:
        normalized_models = _normalize_model_selection(model_selection_override)
        valid_models = {
            role: entry for role, entry in normalized_models.items()
            if entry.get("ownership_name") and entry.get("model_name")
        }
        if valid_models:
            meta["model_selection"] = valid_models
        else:
            meta.pop("model_selection", None)
    # 只有真实写入（追加轮次 / 改标题 / 删除记录 / 导入）才刷新 updated_at；
    # 纯读取（如打开会话列表时逐个拉 meta）不能改动它，
    # 否则“按最近更新排序”会退化为“按最后一次 meta 读取顺序排序”，顺序失真
    if bump_updated_at:
        meta["updated_at"] = now
    elif not meta.get("updated_at"):
        meta["updated_at"] = now
    meta["context_summary"] = _normalize_context_summary(meta.get("context_summary"))
    # 会话分组归属：None/缺失 = 未分组（保留原样）；非字符串/空白视为非法清除。
    # 只做格式校验，不校验分组是否仍存在（分组删除时由 delete_session_group
    # 主动解除归属；注册表损坏时残留的孤儿归属由前端「分组不存在则归入未分组」兜底）
    group_id = meta.get("group_id")
    if group_id is not None and (not isinstance(group_id, str) or not group_id.strip()):
        meta.pop("group_id", None)
    if (
        meta.get("title") == _default_title(session_id)
        and questions
    ):
        first_q = questions[0]
        meta["title"] = first_q[:40] if first_q else _default_title(session_id)
    return meta


def _relocate_anchored_compaction_rows(
    entries: list[Any],
    display_round: int,
    target_index: int,
) -> tuple[list[Any], int]:
    """把锚定到指定轮次的跨轮压缩行搬到 target_index 之前（就地重排）。

    行序契约：任务内独立落盘的跨轮压缩行（带 display_round 锚点）应位于其
    锚定轮次 chat_round 行之前——历史回放（前端解析器与旧版客户端）据此把
    压缩块归位到轮次内部的事件位置。正常追加收尾天然满足（压缩行先落盘、
    轮次行收尾时追加在压缩行之后）；回答插入（ask_user 再答）与编辑重发
    （原地重跑）这两条"轮次行写在非末尾"的收尾路径会让压缩行滞留在其锚定
    轮次行之后（旧前端把压缩块兜底显示到会话末尾），这里统一重排为
    「压缩行 → 轮次行」顺序。

    target_index 为目标轮次行（或即将插入新轮次行的位置）在当前 entries 中
    的下标；返回 (重排后的 entries 列表, 目标位置的新下标)——新下标指向
    搬移后的压缩行之后，即轮次行应落的位置。无锚定行时原样返回。
    """
    if not isinstance(display_round, int):
        return entries, target_index
    anchored: list[Any] = []
    remaining: list[Any] = []
    removed_before = 0
    for index, entry in enumerate(entries):
        if (
            isinstance(entry, dict)
            and entry.get("event") == "context_compaction"
            and entry.get("display_round") == display_round
        ):
            anchored.append(entry)
            if index < target_index:
                removed_before += 1
        else:
            remaining.append(entry)
    if not anchored:
        return entries, target_index
    new_target = target_index - removed_before
    # 压缩行按原有相对顺序放到目标位置之前
    new_entries = remaining[:new_target] + anchored + remaining[new_target:]
    return new_entries, new_target + len(anchored)


def _load_meta_and_entries(
    file_path: Path,
    session_id: str,
    rows: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if rows is None:
        rows = _read_jsonlines(file_path)
    if rows and _is_meta_record(rows[0]):
        base_meta = rows[0].get("_meta") or _default_meta(session_id)
        entries = [row for row in rows[1:] if isinstance(row, dict)]
    else:
        base_meta = _default_meta(session_id)
        entries = [row for row in rows if isinstance(row, dict)]
    meta = _recompute_meta_from_entries(session_id, base_meta, entries, bump_updated_at=False)
    return meta, entries


def _read_meta_first_line(file_path: Path) -> dict[str, Any] | None:
    """只读 jsonl 首行解析 _meta；首行缺失/损坏/非 _meta 记录时返回 None。

    写路径（追加轮次/改标题/删除/导入等）总是把重算后的完整 _meta 落在
    首行，因此正常文件单行读取即可拿到与全量重算一致的元数据；
    返回 None 时调用方应回退 _load_meta_and_entries 全量路径兜底。
    """
    try:
        with file_path.open("r", encoding="utf-8") as fp:
            first_line = fp.readline().strip()
    except OSError:
        return None
    if not first_line:
        return None
    try:
        row = json.loads(first_line)
    except ValueError:
        return None
    if not _is_meta_record(row):
        return None
    meta = row.get("_meta")
    return meta if isinstance(meta, dict) else None


# 原子写重试退避序列（秒）：Windows 上刚写入的文件可能被搜索索引、杀软或
# 其他进程短暂占用（open / os.replace 偶发 WinError 5），指数退避把重试窗口
# 拉长到约 9s——大上下文会话的 JSONL 可达数十 MB，杀软/索引器扫描耗时会随
# 体积增长（实测大文件场景 2.6s 窗口偶发不够用，压缩写盘失败导致任务终止）；
# 仍失败由调用方决定中止还是降级。
_ATOMIC_WRITE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.5, 2.0, 3.0)


def _write_meta_and_entries(file_path: Path, meta: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    # 先写临时文件再原子替换：流式期间其他请求（如 /chat_history/file 读取）
    # 不会读到写了一半的文件，避免并发竞态导致记录丢失。
    # Windows 上临时文件与目标文件都可能被搜索索引/杀软短暂占用，临时文件
    # 的 open 与 os.replace 偶发 WinError 5，指数退避重试规避；全部失败则抛出。
    tmp_path = _sidecar_dir() / (file_path.name + ".tmp")
    last_error: OSError | None = None
    for delay in _ATOMIC_WRITE_RETRY_DELAYS:
        try:
            with tmp_path.open("w", encoding="utf-8") as fp:
                fp.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
                for entry in entries:
                    fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, file_path)
            return
        except OSError as err:
            last_error = err
            time.sleep(delay)
    raise last_error


def _delete_path_with_retry(path: Path, *, recursive: bool = False, attempts: int = 6) -> None:
    """删除文件或目录，带重试。

    与 _write_meta_and_entries 同理：Windows 上文件/目录可能被搜索索引、杀软
    或刚关闭的句柄短暂占用，首次删除偶发 WinError 5；重试几次规避，
    仍失败则抛出 OSError 交由上层处理。
    """
    for attempt in range(attempts):
        try:
            if recursive:
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


class ChatMemoryManager:
    """工具调用历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._run_task = True
        self._lock = threading.Lock()
        self._file_path = _get_chat_history_file(session_id)
        self._round_store = ChatRoundStore(session_id)
        # 编辑重发（target_round）的轮内状态：仅设置方（生成任务）所在进程内生效，
        # 任务结束或轮次收尾后自动复位；None 表示正常追加语义。
        self._target_round_number: int | None = None
        # 回答插入模式（ask_user 卡片再答专用）：收尾轮次插入历史第 N 轮之后
        # （后续轮次整体后推），上下文截到第 N 轮为止。与 target_round 互斥
        self._insert_round_number: int | None = None
        # 回答插入模式（ask_user 卡片再答专用）：收尾轮次插入历史第 N 轮之后
        # （后续轮次整体后推），上下文截到第 N 轮为止。与 target_round 互斥
        self._insert_round_number: int | None = None
        # 历史构建截断点（原地重跑时上下文只取第 N-1 轮及以前）
        self._history_cutoff_rounds: int | None = None
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        # 仅在文件缺失时 touch：touch 对已存在的文件也会刷新 mtime，
        # 服务重启后逐会话实例化会平白搅动所有历史文件的修改时间；
        # exist_ok=True 兜住"检查后被他进程抢先创建"的竞态窗口
        if not self._file_path.exists():
            self._file_path.touch(exist_ok=True)
        with self._write_guard():
            rows = _read_jsonlines(self._file_path)
            stored_meta = rows[0].get("_meta") if (rows and _is_meta_record(rows[0])) else None
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id, rows=rows)
            # 首行 _meta 与重算结果一致（写路径总是落盘重算后的 _meta）时跳过重写：
            # 服务重启后每个会话首次实例化都会走到这里，无变化的全量重写
            # （临时文件 + os.replace）会成批拖慢重启后的首个首屏加载；
            # 仅新文件/旧格式/内容漂移时才物化 _meta 首行
            if stored_meta != meta:
                _write_meta_and_entries(self._file_path, meta, entries)
        self._recover_pending_round_checkpoint()

    def _sidecar_path(self, suffix: str) -> Path:
        """会话附属侧车文件路径：统一放 history_files/sidecars/ 子目录。

        suffix 形如 ".pending" / ".todo" / ".bak"。旧版同目录侧车文件在
        服务启动时由 _migrate_legacy_sidecar_files 一次性搬迁；文件仍被
        占用等迁移失败场景下旧位置可能仍有残留，读取类路径做旧位置回退。
        """
        return _sidecar_dir() / (self._file_path.name + suffix)

    @property
    def _meta_lock_path(self) -> Path:
        """跨进程写锁文件：统一放 history_files/lock/ 下（只创建不删除），
        避免与 JSONL、.pending 检查点混在同一目录。"""
        return LOCK_ROOT / (self._file_path.name + ".lock")

    @contextmanager
    def _write_guard(self):
        """写路径统一守卫：跨进程文件锁（OS 级，崩溃自动释放）+ 进程内线程锁。

        生成任务在独立 worker 进程写轮次/压缩/usage，主进程写标题/删除/导入/
        上传记录，双方都是「读全量→改→原子替换」，必须互斥，否则后写者会
        用自己的旧快照覆盖对方的更新。
        """
        with cross_process_lock(self._meta_lock_path):
            with self._lock:
                yield

    def _recover_pending_round_checkpoint(self) -> None:
        """进程重启后恢复上次未收尾的轮次（工具调用/思考/工具结果不丢）。

        仅在管理器新建时执行：检查点由活跃轮次逐事件写入，而活跃写进程
        一定持有缓存的管理器实例——走到新建，即说明写入它的进程已结束，
        不会误伤仍在进行中的轮次。恢复为 interrupted 状态的历史轮次，
        user_questions/标题/回放照常可用。

        写者存活校验：生成任务在每会话独立 worker 进程中执行（与主进程
        内存隔离），主进程因读接口新建管理器时不能假设"写进程已结束"。
        检查点记录写入方 PID，恢复前确认该进程已不存在（重启后 PID 复用
        的窗口极小，且必须恰好是本主进程/另一 worker 才可能误判）；写者
        仍存活时说明轮次仍在生成，保留检查点交由真正的收尾逻辑处理。
        """
        with self._write_guard():
            recovered = None
            try:
                if self._pending_checkpoint_path.exists():
                    recovered = json.loads(self._pending_checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                recovered = None
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                return
            # 写者进程仍存活：轮次还在生成中（多进程架构下主进程读接口
            # 新建管理器会走到这里），绝不能恢复成 interrupted 历史
            writer_pid = recovered.get("writer_pid")
            if isinstance(writer_pid, int) and _pid_alive(writer_pid):
                return
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            legacy_checkpoint = meta.pop("_pending_round_checkpoint", None)
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                recovered = legacy_checkpoint
            if not isinstance(recovered, dict) or recovered.get("event") != "chat_round":
                return
            normalized = dict(recovered)
            if normalized.get("status") == "running":
                normalized["status"] = "interrupted"
            normalized.setdefault("ended_at", normalized.get("started_at"))
            # 幂等去重：同轮（started_at + 首个用户事件一致）已存在于历史时
            # 不再追加，只清理残留检查点。避免恢复逻辑异常路径下产生重复轮次
            if not self._round_already_recorded(entries, normalized):
                entries.append(normalized)
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        self._clear_pending_checkpoint()

    @staticmethod
    def _round_already_recorded(entries: list[dict[str, Any]], recovered_round: dict[str, Any]) -> bool:
        """判断恢复轮次是否已完整落盘过（按 started_at + 首个用户事件匹配）。

        正常收尾的轮次 events 应为恢复快照的超集（恢复后收尾只会继续追加）。
        """
        started_at = recovered_round.get("started_at")
        if not started_at:
            return False
        recovered_user = next(
            (
                event for event in (recovered_round.get("events") or [])
                if isinstance(event, dict) and event.get("role") == "user"
            ),
            None,
        )
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("event") != "chat_round":
                continue
            if entry.get("started_at") != started_at:
                continue
            if recovered_user is None:
                return True
            existing_user = next(
                (
                    event for event in (entry.get("events") or [])
                    if isinstance(event, dict) and event.get("role") == "user"
                ),
                None,
            )
            if existing_user is not None and existing_user == recovered_user:
                return True
        return False

    @property
    def run_task(self) -> bool:
        return self._run_task

    @run_task.setter
    def run_task(self, value: bool) -> None:
        """
        优雅控制对话启停功能
        """
        self._run_task = value

    def set_target_round(self, target_round: int | None) -> None:
        """设置原地重跑目标轮次号（编辑重发：本轮回复替换历史第 N 轮）。

        仅影响设置之后本进程内的轮次收尾与历史构建；轮次收尾（替换写入）
        完成后自动复位为 None。设置无效值（非正整数）按 None 处理。
        与回答插入模式（set_insert_round）互斥：设置一方会清掉另一方。
        """
        try:
            normalized = int(target_round) if target_round is not None else None
        except (TypeError, ValueError):
            normalized = None
        if normalized is not None and normalized < 1:
            normalized = None
        self._target_round_number = normalized
        self._history_cutoff_rounds = normalized
        if normalized is not None:
            self._insert_round_number = None

    def set_insert_round(self, insert_round: int | None) -> None:
        """设置回答插入轮次号（ask_user 卡片再答：回答轮插入历史第 N 轮之后）。

        收尾时新轮插入第 N 轮之后、后续轮次整体后推；上下文历史截到第 N 轮
        为止（第 N 轮的提问与模型已发生的行为保留，被回答驱动继续）。
        轮次收尾（插入写入）完成后自动复位为 None；与 target_round 互斥。
        """
        try:
            normalized = int(insert_round) if insert_round is not None else None
        except (TypeError, ValueError):
            normalized = None
        if normalized is not None and normalized < 1:
            normalized = None
        self._insert_round_number = normalized
        # 插入语义：上下文包含提问轮（第 N 轮）本身——模型需要看到提问卡片
        # 内容才能作答。截断口径 [:cutoff-1] 与 target_round 共用（后者截到
        # 第 N-1 轮），这里 +1 表示保留前 N 轮（提问轮及其之前）。
        self._history_cutoff_rounds = normalized + 1 if normalized is not None else None
        if normalized is not None:
            self._target_round_number = None

    async def get_insert_round(self) -> int | None:
        """当前回答插入轮次号（任务内调用方读取用）。"""
        with self._lock:
            return self._insert_round_number

    async def get_target_round(self) -> int | None:
        """当前原地重跑目标轮次号（任务内调用方读取用）。"""
        with self._lock:
            return self._target_round_number

    def invalidate_context_summary(self) -> None:
        """使跨轮压缩累计摘要失效（下次聊天前从剩余原始轮次重建）。

        语义 = "摘要整体不再可信，必须整段重建"（历史被外部改写、游标与现存
        轮次无法对齐时的兜底）。注意：回答插入（insert_round）已改用
        clamp_context_summary_for_insert_round（钳制覆盖范围、保留摘要），不再
        调用本方法——整份失效会让下次任务全部轮次退回未压缩、auto 从第 1 轮
        整段重压（用户可见的"回答模型提问后立即大规模压缩"即此现象）。

        写盘失败会抛出（不再静默吞掉）：静默失效失败会让旧摘要带着失效游标
        继续参与上下文拼装。
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if meta.get("context_summary") is None:
                return
            meta["context_summary"] = None
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)

    def clamp_context_summary_for_insert_round(self, insert_round: int | None) -> None:
        """回答插入（insert_round）前把累计摘要的覆盖范围钳到插入点。

        新回答轮插入第 N 轮之后：摘要对插入点之前轮次的描述继续有效（内容与
        编号未变），但覆盖范围必须收缩到第 N 轮为止——否则插入轮落在摘要覆盖
        范围内却没有摘要正文，模型看不到它。钳制后插入轮及其后轮次以原始对话
        回传，下一次压缩再自然纳入新批次（不再整份失效、从第 1 轮重压）。

        写盘失败抛出 RuntimeError：静默跳过会让"游标覆盖插入轮但摘要正文缺失"
        的畸形状态进入模型上下文，由调用方决定终止或降级。
        """
        if insert_round is None:
            return
        try:
            limit = int(insert_round)
        except (TypeError, ValueError):
            return
        if limit < 1:
            return
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            current = meta.get("context_summary")
            clamped = _clamp_summary_to_rounds(current, limit)
            if clamped is current:
                # 游标已在插入点之内（含相等）：插入不改变已压缩轮次的内容与
                # 编号，摘要与索引原样可用，无需写盘
                return
            meta["context_summary"] = _normalize_context_summary(clamped)
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            try:
                _write_meta_and_entries(self._file_path, meta, entries)
            except OSError as write_error:
                print(
                    f"[ERROR] insert_round 摘要钳制写盘失败（游标将覆盖插入轮，"
                    f"上下文会缺摘要正文）: {write_error}"
                )
                raise RuntimeError(f"摘要钳制写盘失败：{write_error}") from write_error

    async def get_file_text(self) -> str | None:
        """锁内读取完整 jsonl 文本（文件不存在返回 None），
        避免与并发写入交替读写读到不完整内容。"""
        with self._lock:
            if not self._file_path.exists():
                return None
            return self._file_path.read_text(encoding="utf-8")

    @staticmethod
    def _user_event_signature(event: dict[str, Any]) -> Any:
        """用户事件的身份签名（忽略 timestamp）：原地重跑时判断「同一用户消息」用。"""
        comparable = {k: v for k, v in event.items() if k != "timestamp"}
        return json.dumps(comparable, ensure_ascii=False, sort_keys=True)

    def _user_messages_equal(self, old_events: list[Any], new_events: list[Any]) -> bool:
        """比较新旧轮次的用户消息是否相同（忽略 timestamp 与事件顺序外的噪声）。"""
        old_users = [
            self._user_event_signature(event)
            for event in old_events
            if isinstance(event, dict) and event.get("role") == "user"
        ]
        new_users = [
            self._user_event_signature(event)
            for event in new_events
            if isinstance(event, dict) and event.get("role") == "user"
        ]
        return old_users == new_users

    async def add_chat_history(self, input_text: Any) -> str:
        """
        追加工具调用记录到 JSONL 文件中
        Args:
            input_text: 要记录的内容，可以是字符串或字典
        Returns:
            操作结果字符串
        """
        record: dict[str, Any] = {
            "timestamp": now_str()
        }
        if isinstance(input_text, str):
            record["content"] = input_text
        elif isinstance(input_text, dict):
            record.update(input_text)
        else:
            record["content"] = str(input_text)
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            had_pending_round = self._round_store.pending_round is not None
            completed_round = self._round_store.record_message(record)
            started_new_round = (
                not had_pending_round
                and self._round_store.pending_round is not None
            )
            meta_changed = False
            # 上次进程异常退出后可能留下活动轮次检查点；新轮次开始时清掉，
            # 避免把上一轮的压缩状态误认成当前任务状态。
            if started_new_round and "_active_round_compaction" in meta:
                meta.pop("_active_round_compaction", None)
                meta_changed = True
            if completed_round is not None:
                meta.pop("_active_round_compaction", None)
                # 轮次已收尾：检查点完成使命，连同最终事件一起落盘
                self._clear_pending_checkpoint()
                target_round = self._target_round_number
                insert_round = self._insert_round_number
                replace_index = None
                if target_round is not None:
                    # 原地重跑（编辑重发）：新回复替换历史第 N 轮条目而非追加。
                    # 目标轮次越界（历史已被并发删除等）时降级为追加，避免丢数据。
                    round_positions = [
                        index for index, entry in enumerate(entries)
                        if isinstance(entry, dict) and entry.get("event") == "chat_round"
                    ]
                    if target_round <= len(round_positions):
                        replace_index = round_positions[target_round - 1]
                    else:
                        print(
                            f"[WARN] target_round={target_round} 超出当前轮次数 "
                            f"{len(round_positions)}，降级为追加写入"
                        )
                if replace_index is not None:
                    old_round = entries[replace_index]
                    if self._user_messages_equal(
                        old_round.get("events") or [], completed_round.get("events") or []
                    ):
                        # 同一用户消息的重新生成：保留原轮次开始时间（轮次身份不变）
                        completed_round["started_at"] = (
                            old_round.get("started_at") or completed_round.get("started_at")
                        )
                    # 用户消息已编辑或原样重跑：一律整轮替换原条目（新轮插回原位置）。
                    # 累计摘要保留：替换不改变轮次总数与编号，摘要游标继续有效；
                    # 摘要对被编辑轮次的旧描述按现状保留不失效重压（大会话下
                    # 整段重建摘要成本极高，见 get_context_messages 的钳制说明）
                    # 锚定到本轮次的跨轮压缩行（重跑期间独立落盘、通常位于文件
                    # 尾部）重排到替换位置之前，保持「压缩行先于其锚定轮次行」的
                    # 行序契约（见 _relocate_anchored_compaction_rows）
                    entries, replace_index = _relocate_anchored_compaction_rows(
                        entries, target_round, replace_index
                    )
                    entries[replace_index] = completed_round
                    self._target_round_number = None
                    self._history_cutoff_rounds = None
                    self._backup_history_file()
                elif insert_round is not None:
                    # 回答插入（ask_user 卡片再答）：回答轮插入历史第 N 轮之后，
                    # 第 N+1 轮及之后整体后推（原 N+1 轮变为 N+2…）。插入点越界
                    # （历史被并发删除）时降级为追加。累计摘要保留并把覆盖范围
                    # 钳到插入点（第 N 轮）：插入点之前轮次的编号与内容未变、
                    # 摘要正文继续有效；插入轮及其后轮次以原始对话回传，下次
                    # 压缩再自然纳入。清空整份摘要会让下次任务全部轮次退回
                    # 未压缩、从第 1 轮整段重压（请求开始时已钳制，这里是收尾
                    # 防御，两处幂等）
                    round_positions = [
                        index for index, entry in enumerate(entries)
                        if isinstance(entry, dict) and entry.get("event") == "chat_round"
                    ]
                    if insert_round <= len(round_positions):
                        after_index = round_positions[insert_round - 1] + 1
                        # 锚定到插入轮次的跨轮压缩行（回答轮执行中独立落盘、
                        # 通常位于文件尾部）重排到插入点之前（行序契约，同替换
                        # 分支）：不重排会滞留在插入的轮次行之后，旧前端把压缩
                        # 块兜底显示到会话末尾
                        entries, after_index = _relocate_anchored_compaction_rows(
                            entries, insert_round + 1, after_index
                        )
                        entries.insert(after_index, completed_round)
                        self._insert_round_number = None
                        self._history_cutoff_rounds = None
                        meta["context_summary"] = _clamp_summary_to_rounds(
                            meta.get("context_summary"), insert_round
                        )
                        self._backup_history_file()
                    else:
                        print(
                            f"[WARN] insert_round={insert_round} 超出当前轮次数 "
                            f"{len(round_positions)}，降级为追加写入"
                        )
                        entries.append(completed_round)
                        self._insert_round_number = None
                        self._history_cutoff_rounds = None
                        meta["context_summary"] = _clamp_summary_to_rounds(
                            meta.get("context_summary"), insert_round
                        )
                        self._backup_history_file()
                else:
                    # 正常追加语义：新收尾轮次挂到末尾
                    entries.append(completed_round)
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
            else:
                # 收尾前的轮次事件只存在内存，重启/崩溃即丢失（工具调用、
                # 思考过程、工具结果都是不可再生的数据）。追加后把 pending
                # 轮次快照写入侧车文件（仅当前轮事件，体量小、原子替换）；
                # 不做全历史重写——大会话逐事件重写 JSONL 会卡住整个请求。
                checkpoint = self._round_store.snapshot_pending_round()
                if checkpoint is not None:
                    self._write_pending_checkpoint(checkpoint)
                elif meta_changed:
                    meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                    _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    @property
    def _pending_checkpoint_path(self) -> Path:
        # 进行中轮次检查点（.pending）：统一放 sidecars/ 子目录
        return self._sidecar_path(".pending")

    def _read_pending_round_snapshot(self) -> dict[str, Any] | None:
        """读取侧车检查点中的进行中轮次快照（只读，供主进程 token 统计兜底）。

        生成任务在会话 worker 进程中执行时，pending_round 只存在于 worker
        内存，主进程读 JSONL 只能看到已完成轮次——任务过程中上下文统计
        永远不增长。检查点由 worker 逐事件原子覆盖写入，这里只读该文件，
        把进行中轮次纳入统计（绝不修改文件，不参与崩溃恢复）。
        """
        try:
            if not self._pending_checkpoint_path.exists():
                return None
            raw = json.loads(self._pending_checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("event") != "chat_round":
            return None
        if raw.get("status") != "running":
            return None
        writer_pid = raw.get("writer_pid")
        # 写者已死亡仍残留的检查点是崩溃遗留（等恢复流程清理），不计入统计；
        # _pid_alive 对无效/负数/不存在 PID 一律返回 False，无需额外判断
        if isinstance(writer_pid, int) and not _pid_alive(writer_pid):
            return None
        snapshot = dict(raw)
        snapshot.pop("writer_pid", None)
        return snapshot

    def _write_pending_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """侧车检查点：只含当前轮快照，写临时文件后原子替换。

        快照附带 writer_pid（当前写进程 PID）：恢复方据此判断写者是否仍
        存活——多进程架构下主进程读接口新建管理器时，不能把仍被 worker
        写入中的轮次误恢复成 interrupted 历史。

        Windows 上刚写入的文件可能被搜索索引/杀软短暂占用导致 open 与
        os.replace 偶发 WinError 5，按 _ATOMIC_WRITE_RETRY_DELAYS 指数退避重试。
        检查点只服务崩溃恢复，重试耗尽不抛错：保留旧检查点并打警告继续本轮
        （丢的只是最近一条事件的恢复点，下一条事件写入会重新覆盖）；若抛错
        会经 add_chat_history 打断整个生成轮次，把偶发文件占用放大成任务失败。
        """
        payload_checkpoint = dict(checkpoint)
        if payload_checkpoint.get("event") == "chat_round":
            payload_checkpoint.setdefault("writer_pid", os.getpid())
        tmp_path = self._pending_checkpoint_path.with_name(self._pending_checkpoint_path.name + ".tmp")
        payload = json.dumps(payload_checkpoint, ensure_ascii=False)
        last_error: OSError | None = None
        for delay in _ATOMIC_WRITE_RETRY_DELAYS:
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, self._pending_checkpoint_path)
                return
            except OSError as err:
                last_error = err
                time.sleep(delay)
        print(f"[WARN] 会话 {self.session_id} 轮次检查点写入失败（保留旧检查点，本轮继续）：{last_error}")

    def _clear_pending_checkpoint(self) -> None:
        try:
            self._pending_checkpoint_path.unlink()
        except OSError:
            pass

    async def stop_current_round(self) -> str:
        """用户手动停止任务：直接以 stopped 状态收尾当前轮次，不写入"停止任务"假消息。"""
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            completed_round = self._round_store.finalize_round("stopped")
            if completed_round is not None:
                meta.pop("_active_round_compaction", None)
                self._clear_pending_checkpoint()
                # 原地重跑（target_round）：停止收尾同样替换目标轮次——
                # 编辑后旧回复已失效，中断的新回复占据其历史位置
                target_round = self._target_round_number
                insert_round = self._insert_round_number
                replace_index = None
                if target_round is not None:
                    round_positions = [
                        index for index, entry in enumerate(entries)
                        if isinstance(entry, dict) and entry.get("event") == "chat_round"
                    ]
                    if target_round <= len(round_positions):
                        replace_index = round_positions[target_round - 1]
                if replace_index is not None:
                    old_round = entries[replace_index]
                    if self._user_messages_equal(
                        old_round.get("events") or [], completed_round.get("events") or []
                    ):
                        completed_round["started_at"] = (
                            old_round.get("started_at") or completed_round.get("started_at")
                        )
                    # 锚定到本轮次的跨轮压缩行重排到替换位置之前（行序契约，
                    # 见 _relocate_anchored_compaction_rows）
                    entries, replace_index = _relocate_anchored_compaction_rows(
                        entries, target_round, replace_index
                    )
                    entries[replace_index] = completed_round
                    self._target_round_number = None
                    self._history_cutoff_rounds = None
                    # 中断收尾替换同样保留累计摘要（替换不减轮次，游标仍有效；
                    # 与 add_chat_history 替换分支同口径）
                    self._backup_history_file()
                elif insert_round is not None:
                    # 回答插入（ask_user 卡片再答）：中断收尾同样插入第 N 轮之后。
                    # 累计摘要保留并钳到插入点（与 add_chat_history 插入分支同口径，
                    # 见其注释）；清空整份摘要会让下次任务从第 1 轮整段重压
                    round_positions = [
                        index for index, entry in enumerate(entries)
                        if isinstance(entry, dict) and entry.get("event") == "chat_round"
                    ]
                    if insert_round <= len(round_positions):
                        after_index = round_positions[insert_round - 1] + 1
                        # 锚定到插入轮次的跨轮压缩行重排到插入点之前（行序契约，
                        # 见 _relocate_anchored_compaction_rows）
                        entries, after_index = _relocate_anchored_compaction_rows(
                            entries, insert_round + 1, after_index
                        )
                        entries.insert(after_index, completed_round)
                    else:
                        entries.append(completed_round)
                    self._insert_round_number = None
                    self._history_cutoff_rounds = None
                    meta["context_summary"] = _clamp_summary_to_rounds(
                        meta.get("context_summary"), insert_round
                    )
                    self._backup_history_file()
                else:
                    entries.append(completed_round)
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def truncate_rounds_for_reanswer(self) -> int:
        """覆盖式重新回答：删除最近 ask_user 提问轮之后的所有轮次。

        场景：最新提问已经回答过（其后存在旧的回答轮），用户再次回答同一
        提问时应覆盖而不是追加，保证一个问题只有一个答案轮次。

        保护条件：仅当提问轮之后的第一轮以回答格式（ASK_ANSWER_PREFIX 前缀）
        开始时才截断——提问后若已开启普通新任务，则不动历史。
        返回删除的轮次数；无需截断（无提问轮 / 首次回答 / 保护命中）返回 0。
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            # 从后往前找最近一个包含 ask_user 工具调用的轮次
            ask_idx = None
            for idx in range(len(entries) - 1, -1, -1):
                entry = entries[idx]
                if not isinstance(entry, dict) or entry.get("event") != "chat_round":
                    continue
                has_ask_call = any(
                    isinstance(event, dict)
                    and event.get("role") == "assistant"
                    and any(
                        isinstance(tool_call, dict)
                        and isinstance(tool_call.get("function"), dict)
                        and tool_call["function"].get("name") == ASK_USER_TOOL_NAME
                        for tool_call in (event.get("tool_calls") or [])
                    )
                    for event in (entry.get("events") or [])
                )
                if has_ask_call:
                    ask_idx = idx
                    break
            if ask_idx is None:
                return 0
            removed = entries[ask_idx + 1:]
            if not removed:
                return 0  # 首次回答：提问轮之后还没有任何轮次
            # 保护：提问轮之后的第一轮必须是回答轮，否则不截断
            first_question = removed[0].get("question") if isinstance(removed[0], dict) else ""
            if not str(first_question or "").startswith(ASK_ANSWER_PREFIX):
                return 0
            # 截断删除的是提问轮之后的轮次（编号后移整体消失）：累计摘要
            # 按删除位置平移（与 delete_rounds 同口径）而不是留着越界游标
            # 触发"游标自愈"整份作废——后者会让下次任务从第 1 轮整段重压
            round_positions = [
                index for index, entry in enumerate(entries)
                if isinstance(entry, dict) and entry.get("event") == "chat_round"
            ]
            ask_round_number = sum(1 for index in round_positions if index <= ask_idx)
            total_rounds = len(round_positions)
            deleted_round_numbers = list(range(ask_round_number + 1, total_rounds + 1))
            meta["context_summary"] = _adjust_summary_for_deleted_rounds(
                meta.get("context_summary"), deleted_round_numbers
            )
            entries = entries[:ask_idx + 1]
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
            return len(removed)

    # 消息内容中 media:// 引用的提取正则：stored_name 由 _safe_filename 生成，
    # 字符集限定 [A-Za-z0-9_.-]，不含路径分隔符与空格，正则可安全截取
    _MEDIA_REF_PATTERN = re.compile(r"media://([A-Za-z0-9_.\-]+)")

    @classmethod
    def _collect_media_refs(cls, payload: Any) -> set[str]:
        """递归收集任意 JSON 结构中出现的 media://stored_name 引用集合。

        覆盖轮次 events 里所有可能的引用位置：用户消息 content 部件、
        助手输出的媒体伪标签、sub_agent 任务的 task 文本等（整体序列化后
        统一扫描，避免遗漏未知嵌套结构）。
        """
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return set()
        return set(cls._MEDIA_REF_PATTERN.findall(text))

    @staticmethod
    def _parse_timestamp_value(value: Any) -> datetime | None:
        """把轮次/记录里的时间字符串解析为 datetime；失败返回 None。"""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.strptime(value.strip(), DEFAULT_TIMESTAMP_FORMAT)
        except ValueError:
            return None

    def _plan_deleted_files(
        self,
        session_id: str,
        removed_entries: list[dict[str, Any]],
        remaining_entries: list[dict[str, Any]],
        doc_window_start: datetime | None,
        doc_window_inclusive: bool = True,
    ) -> dict[str, Any]:
        """预演将被清理的用户上传附件（媒体引用差集 + 文档时间窗）。

        媒体：被删轮次集合的全部 media:// 引用 − 保留轮次的引用 = 可删除
        文件（stored_name 全局唯一，天然覆盖「之后上传/生成」的文件）。
        文档：files/ 目录没有逐轮绑定，按上传时间戳落在删除窗内的记录近似；
        truncate 模式窗起点为被删首轮 started_at（含该时刻之后全部，>=），
        single 模式窗起点为前一轮 ended_at（严格大于，秒级同刻不删——
        边界归属不明的文件保守保留，宁漏删不误删）。
        """
        removed_refs = set()
        for entry in removed_entries:
            removed_refs |= self._collect_media_refs(entry)
        kept_refs = set()
        for entry in remaining_entries:
            kept_refs |= self._collect_media_refs(entry)
        planned_media = sorted(removed_refs - kept_refs)

        planned_docs: list[dict[str, Any]] = []
        if doc_window_start is not None:
            for record_path in file_memory._list_session_files(session_id):
                record = file_memory._read_json_file(record_path)
                if not isinstance(record, dict):
                    continue
                uploaded_at = self._parse_timestamp_value(record.get("timestamp"))
                if uploaded_at is None:
                    continue
                if doc_window_inclusive:
                    if uploaded_at < doc_window_start:
                        continue
                else:
                    if uploaded_at <= doc_window_start:
                        continue
                planned_docs.append({
                    "record": record_path.name,
                    "filename": str(record.get("filename") or ""),
                    "stored_name": str(record.get("stored_name") or ""),
                })
        return {"media_files": planned_media, "doc_files": planned_docs}

    def _execute_deleted_files_cleanup(
        self,
        session_id: str,
        planned: dict[str, Any],
    ) -> dict[str, Any]:
        """执行附件清理：删除 media 原图（连同缩略图）、files/ 原始字节与记录 JSON。

        单个文件删除失败不中断整体，失败的文件列入 returned failed 列表。
        """
        failed: list[str] = []
        removed: list[str] = []
        for stored_name in planned.get("media_files", []):
            safe_name = file_memory._safe_filename(stored_name)
            media_path = file_memory._media_dir(session_id) / safe_name
            try:
                file_memory._delete_media_file_with_retry(media_path)
            except OSError as err:
                failed.append(f"media/{safe_name}: {err}")
                continue
            removed.append(f"media/{safe_name}")
            # 同名缩略图缓存一并清理（失败忽略，孤儿缩略图无功能影响）
            try:
                for thumb in file_memory._thumb_dir(session_id).glob(f"{safe_name}.thumb.*"):
                    thumb.unlink(missing_ok=True)
            except OSError:
                pass
        for doc in planned.get("doc_files", []):
            stored_name = doc.get("stored_name") or ""
            try:
                if stored_name:
                    file_memory._delete_doc_file_with_retry(session_id, stored_name)
            except OSError as err:
                failed.append(f"files/{stored_name}: {err}")
            record_name = doc.get("record") or ""
            if record_name:
                record_path = file_memory._get_session_dir(session_id) / record_name
                try:
                    record_path.unlink(missing_ok=True)
                except OSError as err:
                    failed.append(f"records/{record_name}: {err}")
            removed.append(f"doc:{doc.get('filename') or record_name}")
        return {"removed": removed, "failed": failed}

    async def delete_rounds(
        self,
        start_round: int,
        mode: str = "truncate",
        delete_files: bool = False,
        dry_run: bool = False,
        keep_media_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """按轮次号删除历史轮次（编辑重发 / 整轮删除的持久层入口）。

        Args:
            start_round: 1-based 轮次号（第 N 个 chat_round 条目，与前端
                data-round 同口径；游离压缩事件行不占轮次号）。
            mode: "truncate" 删除该轮及其后所有轮次；"single" 仅删除该轮整轮
                （该轮的用户消息与回复一并删除，后续轮次保留）。
            delete_files: 是否连带清理该删除范围内引用、且保留内容不再引用的
                用户上传附件（media/ 图片视频音频 + files/ 文档）。
            dry_run: 预演模式——只返回将删除的轮次与文件明细，不写盘不删文件。
            keep_media_refs: 清理时排除的媒体 stored_name 列表（编辑重发时
                编辑态保留的附件仍要复用，不能删）。

        Notes:
            跨轮累计摘要（context_summary）不随删除丢弃：游标 source_round_count、
            摘要块覆盖轮次与保真问题索引按删除位置平移后继续生效——删除只改变
            轮次编号，不改变摘要正文承载的历史事实；丢弃摘要会让下轮上下文
            退化为原始轮次全量回传，大会话直接超出模型窗口。

        Returns:
            {state, removed_rounds, planned_rounds, planned_files, files_cleanup,
             meta_after, usage_after}；dry_run 时 state="planned"。
        """
        if mode not in ("truncate", "single"):
            raise ValueError(f"mode 必须是 truncate 或 single，收到: {mode}")
        try:
            start_round = int(start_round)
        except (TypeError, ValueError):
            raise ValueError("start_round 必须是整数")
        if start_round < 1:
            raise ValueError("start_round 必须从 1 开始")

        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            round_positions = [
                index for index, entry in enumerate(entries)
                if isinstance(entry, dict) and entry.get("event") == "chat_round"
            ]
            total_rounds = len(round_positions)
            if start_round > total_rounds:
                raise ValueError(
                    f"轮次号 {start_round} 超出范围，当前共 {total_rounds} 轮"
                )
            if total_rounds == 0:
                raise ValueError("当前会话没有可删除的轮次")

            idx_start = round_positions[start_round - 1]
            if mode == "truncate":
                idx_end = len(entries) - 1
            else:
                # 单轮删除：到下一个 chat_round 之前（游离压缩行随删除轮一并丢弃）
                idx_end = (
                    round_positions[start_round] - 1
                    if start_round < total_rounds
                    else len(entries) - 1
                )
            removed_entries = entries[idx_start: idx_end + 1]
            remaining_entries = entries[:idx_start] + entries[idx_end + 1:]

            # 文档时间窗下界：truncate 用被删首轮 started_at（该轮随消息发送上传，
            # 同一时刻或之后上传的文档归属本次删除）；single 用前一轮 ended_at
            doc_window_start = None
            first_removed = next(
                (entry for entry in removed_entries if isinstance(entry, dict) and entry.get("event") == "chat_round"),
                None,
            )
            if first_removed is not None:
                if mode == "truncate":
                    doc_window_start = self._parse_timestamp_value(first_removed.get("started_at"))
                else:
                    prev_round = next(
                        (
                            entry for entry in reversed(remaining_entries[: idx_start])
                            if isinstance(entry, dict) and entry.get("event") == "chat_round"
                        ),
                        None,
                    )
                    doc_window_start = self._parse_timestamp_value(
                        (prev_round or {}).get("ended_at")
                    )

            planned_files = self._plan_deleted_files(
                self.session_id, removed_entries, remaining_entries, doc_window_start,
                doc_window_inclusive=(mode == "truncate"),
            )
            # 编辑重发复用的附件：从清理清单排除（预演与执行同一口径）
            keep_refs = {
                file_memory._safe_filename(str(ref)) for ref in (keep_media_refs or [])
                if str(ref or "").strip()
            }
            if keep_refs:
                planned_files["media_files"] = [
                    name for name in planned_files["media_files"] if name not in keep_refs
                ]
            planned_rounds = []
            round_counter = 0
            for entry in entries:
                if not (isinstance(entry, dict) and entry.get("event") == "chat_round"):
                    continue
                round_counter += 1
                in_removed_range = (
                    round_counter >= start_round if mode == "truncate"
                    else round_counter == start_round
                )
                if in_removed_range:
                    planned_rounds.append({
                        "round": round_counter,
                        "question": str((entry or {}).get("question") or "")[:120],
                        "status": str((entry or {}).get("status") or ""),
                    })

            if dry_run:
                return {
                    "state": "planned",
                    "mode": mode,
                    "start_round": start_round,
                    "total_rounds": total_rounds,
                    "planned_rounds": planned_rounds,
                    "planned_files": planned_files,
                    "removed_rounds": 0,
                    "files_cleanup": {"removed": [], "failed": []},
                }

            files_cleanup = {"removed": [], "failed": []}
            if delete_files:
                files_cleanup = self._execute_deleted_files_cleanup(self.session_id, planned_files)

            # 破坏性写盘前留 .bak 侧车备份（sidecars/ 子目录，与 .tmp 原子写同目录），误删可手动恢复
            self._backup_history_file()
            # 累计摘要不整体作废：按删除位置平移游标（source_round_count）、
            # 摘要块覆盖范围与保真问题索引的轮次编号，摘要继续生效。
            # 丢弃摘要会让下轮上下文退化为原始轮次回传，大会话直接超窗
            # （删除只影响编号锚点，不影响摘要正文承载的历史事实）
            deleted_round_numbers = (
                list(range(start_round, total_rounds + 1))
                if mode == "truncate"
                else [start_round]
            )
            meta["context_summary"] = _adjust_summary_for_deleted_rounds(
                meta.get("context_summary"), deleted_round_numbers
            )
            meta = _recompute_meta_from_entries(self.session_id, meta, remaining_entries)
            _write_meta_and_entries(self._file_path, meta, remaining_entries)
            return {
                "state": "succeed",
                "mode": mode,
                "start_round": start_round,
                "total_rounds_before": total_rounds,
                "planned_rounds": planned_rounds,
                "planned_files": planned_files,
                "removed_rounds": len(planned_rounds),
                "files_cleanup": files_cleanup,
                "meta_after": meta,
                "usage_after": meta.get("usage", {}),
            }

    def _backup_history_file(self) -> None:
        """写盘前把当前历史文件复制为 <名字>.bak（sidecars/ 子目录覆盖式备份）。

        只在破坏性改写前调用；复制失败不阻断主流程（删除操作仍继续，
        备份属尽力而为的兜底，不因备份失败拒绝用户显式请求的删除）。
        """
        try:
            if self._file_path.exists():
                # .bak 备份同样放 sidecars/ 子目录（与 .tmp/.todo/.pending 同目录管理，
                # 历史根目录只保留 *.jsonl 正文件）
                shutil.copy2(self._file_path, self._sidecar_path(".bak"))
        except OSError as backup_error:
            print(f"[WARN] 历史文件备份失败（继续执行删除）: {backup_error}")

    async def update_current_round_usage(
        self,
        usage_total: dict[str, Any],
        completion_count: int = 0,
    ) -> str:
        """将当前轮次的 usage 汇总挂到内存中的 pending round。"""
        with self._lock:
            return self._round_store.add_usage(usage_total, completion_count)

    async def get_current_round_tool_result_count(self) -> int:
        """返回当前任务已经记录的工具结果数量，作为单轮压缩游标。"""
        with self._lock:
            return self._round_store.current_tool_result_count()

    async def get_current_round_number(self) -> int:
        """当前进行中的轮次号（已完成 chat_round 条目数 + 1），从 1 开始。

        供 file_history 版本链标注 round 使用：同轮内的多次编辑 round 相同，
        「回退到第 N 轮发起时」= 链上 round < N 的最新快照。跨进程读文件
        （worker 进程同样以此口径计算），仅数条目、不重算 meta，成本一次
        全量读取；工具循环开始处调用一次即可。

        原地重跑（target_round）时返回目标轮次号：编辑重发属于历史第 N 轮
        的重新生成，工具入链的版本链 round 必须仍标 N（而非 N+1），否则
        「回退到第 N 轮发起时」的快照语义会被破坏。回答插入（insert_round）
        同理返回 N+1：回答轮即将插入第 N 轮之后，成为新的第 N+1 轮。
        """
        with self._lock:
            if self._target_round_number is not None:
                return self._target_round_number
            if self._insert_round_number is not None:
                return self._insert_round_number + 1
            _, entries = _load_meta_and_entries(self._file_path, self.session_id)
        return (
            sum(
                1
                for entry in entries
                if isinstance(entry, dict) and entry.get("event") == "chat_round"
            )
            + 1
        )

    async def update_current_round_compaction(
        self,
        compress_content: str,
        compress_index: int,
        compress_usage: dict[str, Any] | None = None,
        compress_blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """兼容旧调用方：把当前轮累计压缩摘要写成 round done 事件。

        pending round 最终收尾时只会把事件写入 `chat_round.events`；活动期间
        同步写入 `_meta._active_round_compaction`，避免任务中断时压缩状态完全丢失。
        """
        with self._write_guard():
            if not self._round_store.update_compaction(
                compress_content,
                compress_index,
                compress_blocks=compress_blocks,
                compress_usage=compress_usage,
            ):
                return "忽略无有效压缩状态"
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            pending_round = self._round_store.pending_round or {}
            meta["_active_round_compaction"] = {
                "question": pending_round.get("question", ""),
                "events": [
                    event for event in pending_round.get("events", [])
                    if isinstance(event, dict) and event.get("event") == "context_compaction"
                ],
                "started_at": pending_round.get("started_at"),
            }
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def add_context_compaction_event(self, payload: dict[str, Any]) -> str:
        """追加压缩过程事件。

        round scope 写入当前 pending `chat_round.events`，session scope 仍写入独立
        JSONL 事件行。timestamp 由本方法补充。
        """
        if not isinstance(payload, dict) or payload.get("event") != "context_compaction":
            return "忽略非法压缩事件"
        record = dict(payload)
        record.setdefault("timestamp", now_str())
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if record.get("scope") == "round":
                if not self._round_store.record_compaction_event(record):
                    return "忽略无进行中轮次或非法压缩事件"
                pending_round = self._round_store.pending_round or {}
                meta["_active_round_compaction"] = {
                    "question": pending_round.get("question", ""),
                    "events": [
                        event for event in pending_round.get("events", [])
                        if isinstance(event, dict) and event.get("event") == "context_compaction"
                    ],
                    "started_at": pending_round.get("started_at"),
                }
            else:
                # 任务进行中触发的 session/history 压缩事件会先于 chat_round
                # 独立写入 JSONL，而 chat_round 要等任务结束才落盘。保存轮内锚点，
                # 历史回放时可把压缩块插回触发它的轮次与事件位置。
                pending_round = self._round_store.pending_round
                if isinstance(pending_round, dict):
                    if self._target_round_number is not None:
                        display_round = self._target_round_number
                    elif self._insert_round_number is not None:
                        display_round = self._insert_round_number + 1
                    else:
                        display_round = sum(
                            1 for entry in entries
                            if isinstance(entry, dict) and entry.get("event") == "chat_round"
                        ) + 1
                    record.setdefault("display_round", display_round)
                    record.setdefault(
                        "display_event_index",
                        len(pending_round.get("events", [])),
                    )
                entries.append(record)
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def add_sub_agent_event(self, payload: dict[str, Any]) -> str:
        """追加 sub_agent 子任务事件到当前 pending `chat_round.events`。

        调用契约（docs/sub_agent_v1.md §6.3/§7.1）：子任务 Runner 不直接持有
        chat_memory，事件一律经父循环提供的 emit 回调进入本方法——保证写入
        发生在事件循环的同步 `_write_guard()` 块内（跨进程文件锁不可重入，
        禁止工作线程写历史）。timestamp 由本方法补充；追加后同步刷新 `.pending`
        侧车检查点快照（子任务事件随快照落盘，崩溃恢复不丢轨迹）。
        """
        if not isinstance(payload, dict) or payload.get("event") != "sub_agent":
            return "忽略非法 sub_agent 事件"
        record = dict(payload)
        record.setdefault("timestamp", now_str())
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if not self._round_store.record_sub_agent_event(record):
                return "忽略无进行中轮次或非法 sub_agent 事件"
            # 检查点快照（与 add_chat_history 相同模式）：仅覆盖侧车文件，
            # 不做全历史重写；meta 重算开销为零（子事件不带 role，不影响聚合）
            checkpoint = self._round_store.snapshot_pending_round()
            if checkpoint is not None:
                self._write_pending_checkpoint(checkpoint)
        return "记录成功"

    async def find_orphan_compaction_events(self) -> list[dict[str, Any]]:
        """返回未完成的压缩 start 事件（任务中断遗留：有 start、无对应 done/aborted）。

        压缩结果采用"完成后一次性写入"（context_summary / pending round 的
        compress 字段），中断时不会留下半成品数据；孤儿 start 仅代表展示层
        有未闭合的压缩过程条目，由调用方标记 aborted 并在下一次请求按需重新压缩。
        """
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
        open_starts: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("event") != "context_compaction":
                continue
            scope = str(entry.get("scope") or "unknown")
            phase = entry.get("phase")
            if phase == "start":
                open_starts[scope] = entry
            elif phase in ("done", "aborted"):
                open_starts.pop(scope, None)
        active_checkpoint = meta.get("_active_round_compaction")
        if isinstance(active_checkpoint, dict):
            for event in active_checkpoint.get("events", []):
                if not isinstance(event, dict):
                    continue
                phase = event.get("phase")
                if phase == "start":
                    open_starts["round"] = event
                elif phase in ("done", "aborted"):
                    open_starts.pop("round", None)
        return list(open_starts.values())

    async def mark_compaction_aborted(self, orphan: dict[str, Any]) -> str:
        """把一个未完成的压缩 start 事件标记为 aborted（前端据此失效对应条目）。"""
        scope = orphan.get("scope")
        if scope == "round":
            with self._write_guard():
                meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
                checkpoint = meta.get("_active_round_compaction")
                if not isinstance(checkpoint, dict):
                    return "忽略无活动压缩"
                events = checkpoint.get("events")
                if not isinstance(events, list):
                    return "忽略无活动压缩"
                events.append({
                    "event": "context_compaction",
                    "scope": "round",
                    "phase": "aborted",
                    "role": "assistant",
                    "reason": "task_interrupted",
                    "timestamp": now_str(),
                })
                meta["_active_round_compaction"] = {**checkpoint, "events": events}
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
            return "记录成功"
        return await self.add_context_compaction_event({
            "event": "context_compaction",
            "scope": scope,
            "phase": "aborted",
            "role": "assistant",
            "reason": "task_interrupted",
        })

    async def add_history_compression_usage(self, usage: dict[str, Any] | None) -> str:
        if not usage:
            return "忽略空压缩 usage"
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            history_usage = meta.get("_history_compress_usage")
            if not isinstance(history_usage, dict):
                history_usage = {}
                meta["_history_compress_usage"] = history_usage
            if not merge_compression_usage(history_usage, usage):
                return "忽略空压缩 usage"
            meta = _recompute_meta_from_entries(self.session_id, meta, entries)
            try:
                _write_meta_and_entries(self._file_path, meta, entries)
            except OSError as write_error:
                # 压缩 usage 统计写盘被文件占用时降级：只影响统计数字完整性，
                # 不影响摘要内容与任务运行（见 update_context_summary 同源注释）
                print(f"[WARN] 历史压缩 usage 写盘失败（文件被占用，本次跳过）: {write_error}")
                return "usage 写盘繁忙，已跳过"
        return "记录成功"

    async def get_meta(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_session_meta(self) -> dict[str, Any]:
        """读取会话元数据（热点只读路径：优先只读首行，不回写文件）。

        旧实现每次「读全量 → 重算 → 整文件重写回盘」，而 GET /chat_history/meta
        是首屏按会话并发调用的热点（每个会话一次），全量重写在会话多时既拖慢
        首屏又产生大量无谓磁盘写。派生字段（标题/提问列表/usage 等）已由写路径
        随重算结果落盘，正常文件单行读取的响应与全量重算一致；仅当首行不可靠
        （缺失/损坏/标题仍是默认值/缺 updated_at 或 user_questions）时回退全量
        重算兜底，两种路径都只读不写。
        """
        with self._lock:
            meta = _read_meta_first_line(self._file_path)
            if (
                meta is None
                or meta.get("title") == _default_title(self.session_id)
                or not meta.get("updated_at")
                or not meta.get("user_questions")
            ):
                meta, _entries = _load_meta_and_entries(self._file_path, self.session_id)
        # todo 真源在侧车文件（见 get_session_todo）：_meta 首行的 todo 可能是
        # 被其他写路径旧快照覆盖的陈旧值，这里以侧车为准合并返回，前端
        # （会话切换时恢复计划面板）无需感知存储位置变化
        sidecar_todo = self._read_todo_sidecar()
        if sidecar_todo is not None or isinstance(meta.get("todo"), list):
            meta["todo"] = sidecar_todo if sidecar_todo is not None else meta.get("todo")
        return meta

    async def get_session_metadata(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_session_todo(self) -> list[dict[str, Any]]:
        """读取当前任务计划；无记录返回空列表。

        真源是独立侧车文件（<session>_chat.jsonl.todo）：_meta 由多条写路径
        全量重写（轮次收尾/压缩/标题等，分布在主进程与 worker 多个进程），
        任何一路用旧快照回写都会覆盖掉并发更新的 todo——实测出现过工具结果
        已回 plan_complete、下一轮 _meta.todo 却退回中间态，模型因此在系统
        提示里看不到终态而重复调用 todo_write。侧车只有一个写方，无该竞态。
        """
        with self._lock:
            data = self._read_todo_sidecar()
        if data is not None:
            return data
        # 兼容迁移：侧车不存在（旧会话首次读取）时回退 _meta.todo，不回写——
        # 首次 update 后侧车即成为唯一真源
        with self._lock:
            meta, _entries = _load_meta_and_entries(self._file_path, self.session_id)
            todo = meta.get("todo")
        return todo if isinstance(todo, list) else []

    async def update_session_todo(self, todos: list[dict[str, Any]]) -> str:
        """写入当前任务计划（侧车文件，跨进程锁串行原子替换）。"""
        if not isinstance(todos, list):
            raise ValueError("todos 必须是列表")
        with self._write_guard():
            self._write_todo_sidecar(todos)
        return "记录成功"

    @property
    def _todo_sidecar_path(self) -> Path:
        """任务计划侧车文件：<session>_chat.jsonl.todo（小 JSON 数组，sidecars/ 子目录）。"""
        return self._sidecar_path(".todo")

    def _read_todo_sidecar(self) -> list[dict[str, Any]] | None:
        """读侧车 todo；文件不存在/损坏返回 None（调用方回退 _meta.todo）。"""
        try:
            if self._todo_sidecar_path.exists():
                raw = json.loads(self._todo_sidecar_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [item for item in raw if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _write_todo_sidecar(self, todos: list[dict[str, Any]]) -> None:
        """侧车原子写（tmp + os.replace + 占用重试，与 meta 写同口径）。"""
        tmp_path = self._todo_sidecar_path.with_name(self._todo_sidecar_path.name + ".tmp")
        payload = json.dumps(todos, ensure_ascii=False)
        last_error: OSError | None = None
        for delay in _ATOMIC_WRITE_RETRY_DELAYS:
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, self._todo_sidecar_path)
                return
            except OSError as err:
                last_error = err
                time.sleep(delay)
        raise last_error

    async def update_session_title(self, title: str) -> dict[str, Any]:
        """
        更新当前会话首行 _meta 的 title 字段
        Args:
            title: 新的会话标题
        Returns:
            更新后的元数据字典
        """
        new_title = (title or "").strip()
        if not new_title:
            raise ValueError("title 不能为空")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["title"] = new_title
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def apply_generated_title(
        self,
        title: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        写入标题模型生成的会话标题与生成标记（_title_state）。

        与 update_session_title 同一写盘路径（_write_guard 互斥）；用户
        后续手动重命名走 update_session_title 时覆盖 title 本身，而
        _title_state.title_generated 标记让标题任务不再重复覆盖手动标题
        （recompute 的首问 40 字兜底也因 title != default_title 不触发）。
        Args:
            title: 标题模型生成的标题
            state: 附带生成信息（source/model/applied_at），缺省仅写标记
        Returns:
            更新后的元数据字典
        """
        new_title = (title or "").strip()
        if not new_title:
            raise ValueError("title 不能为空")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["title"] = new_title
            title_state = dict(state) if isinstance(state, dict) else {}
            title_state.setdefault("source", "title_model")
            title_state["title_generated"] = True
            meta["_title_state"] = title_state
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def update_session_retitle_setting(self, enabled: bool) -> dict[str, Any]:
        """写入会话「每条消息重新标题」开关（_meta.retitle_each_message）。

        会话独立配置：开启时每轮任务收尾都重新生成标题；关闭（默认）时
        仅首个任务收尾尝试一次。开启瞬间同时清掉 _title_state.attempted
        标记（历史失败已无意义，开启后每轮都会重新生成）。
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["retitle_each_message"] = bool(enabled)
            title_state = meta.get("_title_state")
            if isinstance(title_state, dict) and title_state.pop("attempted", None) is not None:
                meta["_title_state"] = title_state
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def update_session_upload_id(self, upload_id: str) -> dict[str, Any]:
        """
        把本会话上传文件所在目录名写入首行 _meta 的 upload_id 字段。

        上传目录按上传时前端传入的 session_id 命名（history_files/session_files/
        下的文件夹名），可能与会话文件名不一致；记录后可保证删除会话时
        能连带清理上传目录（见 delete_chat_session_file）。
        Args:
            upload_id: 上传目录名（history_files/session_files/ 下的文件夹名）
        Returns:
            更新后的元数据字典
        """
        upload_id = (upload_id or "").strip()
        if not upload_id:
            raise ValueError("upload_id 不能为空")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["upload_id"] = upload_id
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_chat_history(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近工具调用历史列表
        Args:
            number: 获取最后几条数量，-1 表示所有
        Returns:
            历史记录列表
        """
        if not hasattr(number, "__int__"):
            # 抛出类型不匹配错误
            raise TypeError("number 参数必须为整数")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return entries[-number:] if number > 0 else entries

    async def get_context_summary(self) -> dict[str, Any] | None:
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return _normalize_context_summary(meta.get("context_summary"))

    async def update_context_summary(self, summary: dict[str, Any] | str | None) -> str:
        """写入累计摘要（成功返回"记录成功"）。

        写盘失败（Windows 文件占用 WinError 5、杀软/索引器扫描大 JSONL 等）
        不再静默跳过：静默降级会让"摘要已生成但未生效"，下一次压缩基于原始
        历史从头重压——跨轮压缩反复整段执行的诱因之一。失败改为抛出
        RuntimeError 由调用方决策：压缩核心路径（context_compaction）作为批次
        失败补发 aborted 并按既有语义续跑/终止；非关键路径（问题索引同步、
        旧版多块迁移）调用方已降级为告警，不影响任务。
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["context_summary"] = _normalize_context_summary(summary)
            try:
                _write_meta_and_entries(self._file_path, meta, entries)
            except OSError as write_error:
                print(
                    f"[ERROR] 累计摘要写盘失败（不再静默跳过，交由调用方处理）: {write_error}"
                )
                raise RuntimeError(f"累计摘要写盘失败：{write_error}") from write_error
        return "记录成功"

    def _load_for_read(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """只读路径的"读全量 + 按需物化"：meta 与首行一致时不再全量重写。

        get_context_messages / get_context_token_stats 等只读查询过去每次都
        无条件 `_write_meta_and_entries`（临时文件 + os.replace 全文件重写），
        大会话下轮询/加载会明显变慢；实际上写路径总是把重算后的 _meta 落在
        首行，仅当读取到的首行 _meta 与重算结果漂移时才需要物化（与
        __init__ 的惰性物化同口径）。
        """
        with self._write_guard():
            rows = _read_jsonlines(self._file_path)
            stored_meta = rows[0].get("_meta") if (rows and _is_meta_record(rows[0])) else None
            meta, entries = _load_meta_and_entries(
                self._file_path, self.session_id, rows=rows
            )
            if stored_meta != meta:
                _write_meta_and_entries(self._file_path, meta, entries)
        return meta, entries

    async def get_context_messages(
        self,
        max_rounds: int | None = None,
        max_tool_result_length: int | None = None,
        minimal: bool = False,
        recent_questions_token_budget: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取用于模型上下文的历史视图。

        历史轮次窗口（原 HISTORY_COMPACT_KEEP_ROUNDS）已废弃：不再按"保留最近
        N 轮"截断，历史规模由压缩阈值与历史压缩目标控制。

        - 未压缩轮次以完整对话回传（含助手回答、工具调用等），其问题不重复
          进入保真索引；
        - 已压缩轮次的原始用户问题进入保真索引（受 10k token 预算限制，
          超出从最旧丢弃）。

        Args:
            max_rounds: 兼容参数；<=0（默认）表示不限制轮次
            max_tool_result_length: 窗口内完整轮次的工具结果回传长度
            minimal: 兼容参数，不再切换已完成历史表示
            recent_questions_token_budget: 保真问题索引预算；省略时为 10k
        Returns:
            符合 ChatCompletion messages 结构的累计摘要/问题索引/完整轮次消息列表
        """
        if max_rounds is None:
            max_rounds = _default_context_history_rounds()
        if not hasattr(max_rounds, "__int__"):
            raise TypeError("max_rounds 参数必须为整数")
        max_rounds = int(max_rounds)
        if max_tool_result_length is None:
            max_tool_result_length = _parse_return_length(
                _load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
                DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
            )
        meta, entries = self._load_for_read()
        context_summary = _normalize_context_summary(meta.get("context_summary"))
        summary_message = _render_context_summary(context_summary)
        round_entries = [
            row for row in entries
            if isinstance(row, dict) and row.get("event") == "chat_round"
        ]
        full_round_count = len(round_entries)
        # 原地重跑（target_round）：上下文历史截到第 N-1 轮为止——被编辑轮次
        # 的旧回复不再进入原始对话段（屏幕上第 N 轮之后的历史轮次仍保留，
        # 但其生成依据不含旧第 N 轮）。累计摘要保留：见下方游标钳制说明
        cutoff = self._history_cutoff_rounds
        sliced_by_cutoff = False
        if cutoff is not None and cutoff >= 1:
            round_entries = round_entries[: cutoff - 1]
            sliced_by_cutoff = True
        summarized_count = 0
        if context_summary is not None:
            try:
                summarized_count = max(0, int(context_summary.get("source_round_count", 0) or 0))
            except (TypeError, ValueError):
                summarized_count = 0
            if summarized_count > len(round_entries):
                if sliced_by_cutoff and summarized_count <= full_round_count:
                    # 原地重跑（target_round）的截断把游标推过了截断边界：
                    # 摘要锚定在真实历史（替换不减轮次总数，游标仍有效），
                    # 只是覆盖范围越过第 N-1 轮。保留摘要并把有效游标钳到
                    # 截断边界——摘要替换截断点之前的全部上下文，原始对话
                    # 从截断点起拼装。被编辑轮次的旧描述留在摘要里且不加任
                    # 何提示：用户显式编辑本身即最新意图（与删除轮次不同，
                    # 后者编号整体变化必须重建摘要）；模型执行中也会以工具
                    # 实测校验。大会话下避免超窗与整段重压缩的 token 开销
                    summarized_count = len(round_entries)
                else:
                    # 游标失效（历史被删等）：视为无摘要，下次压缩前从原始轮次重建
                    context_summary = None
                    summary_message = None
                    summarized_count = 0
        if recent_questions_token_budget is None:
            question_budget = RECENT_QUESTIONS_TOKEN_BUDGET
        else:
            try:
                question_budget = max(256, int(recent_questions_token_budget))
            except (TypeError, ValueError):
                question_budget = 10_000

        messages: list[dict[str, Any]] = []
        if summary_message:
            messages.append({"role": "system", "content": summary_message})
        if context_summary is not None:
            # 摘要模式：未压缩轮次全部完整对话回传，已压缩轮次问题进入保真索引
            # （未压缩轮次的问题不重复进入索引）
            question_items, raw_rounds = _split_context_window(
                round_entries, summarized_count, None, question_budget
            )
            recent_questions_message = _render_recent_questions_message(question_items)
            if recent_questions_message:
                messages.append(recent_questions_message)
        else:
            # 未触发过压缩：按窗口回传最近 N 轮完整对话（最旧的超出窗口舍弃），
            # 此时没有已压缩轮次，无需问题索引
            question_items = []
            raw_rounds = round_entries[-max_rounds:] if max_rounds > 0 else round_entries
        for round_entry in raw_rounds:
            messages.extend(
                _round_entry_to_context_messages(round_entry, max_tool_result_length)
            )
        return messages

    async def get_context_token_stats(
        self,
        max_rounds: int | None = None,
        max_tool_result_length: int | None = None,
        tools: list[Any] | None = None,
        system_prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """返回模型上下文构成的 token 统计，供调试/前端展示压缩效果。

        进入摘要模式后，统计的历史部分与 `get_context_messages` 一致：
        累计摘要 + 已压缩轮次保真问题索引 + 窗口内未压缩轮次完整对话；
        没有摘要的旧会话暂按原始轮次估算。

        `system_prompt_text` 为运行时系统提示附加文本（工作路径+系统提示），
        真实请求会追加到首条 system 消息；传入后单独统计并计入请求上下文总额。
        """
        if max_rounds is None:
            max_rounds = _default_context_history_rounds()
        if not hasattr(max_rounds, "__int__"):
            raise TypeError("max_rounds 参数必须为整数")
        max_rounds = int(max_rounds)
        if max_tool_result_length is None:
            max_tool_result_length = _parse_return_length(
                _load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
                DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
            )
        meta, entries = self._load_for_read()
        # 流式任务尚未收到 done 时，当前轮仍只存在于 ChatRoundStore.pending_round；
        # 复制快照供 token_stats 纳入统计，避免轮询只能看到上一轮的旧数据。
        pending_round = copy.deepcopy(self._round_store.pending_round)
        if pending_round is None:
            # 生成任务跑在会话 worker 进程时，pending_round 在 worker 内存，
            # 主进程实例里恒为 None；兜底读取 worker 逐事件写入的检查点
            # 侧车快照，让任务进行中（无 usage/压缩事件的静默间隔）统计
            # 也能实时反映当前轮增量。
            pending_round = self._read_pending_round_snapshot()
        context_summary = _normalize_context_summary(meta.get("context_summary"))
        summary_message = (
            _render_context_summary(context_summary)
            if context_summary is not None else None
        )
        round_entries = [
            row for row in entries
            if isinstance(row, dict) and row.get("event") == "chat_round"
        ]
        summarized_round_count = 0
        if context_summary is not None:
            try:
                stored_source_count = max(0, int(context_summary.get("source_round_count", 0) or 0))
            except (TypeError, ValueError):
                stored_source_count = 0
            if stored_source_count > len(round_entries):
                # token_stats 不能展示一个游标已失效的摘要；下次聊天前会重建它。
                context_summary = None
                summary_message = None
            else:
                summarized_round_count = stored_source_count
        # 有摘要后按总轮次窗口切分：未压缩轮次完整回传（占窗口），
        # 已压缩轮次问题按剩余窗口进入保真索引；没有摘要的旧会话
        # 暂时保留原始估算，供前端展示并触发首次压缩。
        summary_only = context_summary is not None
        if summary_only:
            question_items, raw_rounds = _split_context_window(
                round_entries, summarized_round_count, None
            )
            retained_rounds: list[dict[str, Any]] = list(raw_rounds)
        else:
            question_items = []
            # 必须取副本：下方会把 pending_round 追加进 retained_rounds，
            # 若直接引用 round_entries 会连带污染轮次总数统计
            retained_rounds = list(round_entries)
            if max_rounds > 0:
                retained_rounds = retained_rounds[-max_rounds:]
        if (
            isinstance(pending_round, dict)
            and isinstance(pending_round.get("events"), list)
            and pending_round.get("events")
        ):
            retained_rounds.append(pending_round)

        messages: list[dict[str, Any]] = []
        if summary_message:
            messages.append({"role": "system", "content": summary_message})
        recent_questions_message = (
            _render_recent_questions_message(question_items)
            if summary_only else None
        )
        if recent_questions_message:
            messages.append(recent_questions_message)
        round_tokens: list[dict[str, Any]] = []
        # 发送口径补偿：当前轮（pending）最后一条非空思考随请求回传，
        # 单轮明细与总额同步计入（历史轮次思考不回传、不计入）
        pending_reasoning_tokens = _estimate_pending_reasoning_tokens(pending_round)
        for round_entry in retained_rounds:
            round_messages = _round_entry_to_context_messages(
                round_entry, max_tool_result_length
            )
            # 当前轮只有 user 事件时，通用历史格式化器会等待 assistant 内容；
            # 实时统计仍应计入这条已经送出的用户消息。
            if (
                not round_messages
                and round_entry is pending_round
                and isinstance(round_entry.get("question"), str)
                and round_entry.get("question", "").strip()
            ):
                round_messages = [{
                    "role": "user",
                    "content": round_entry["question"].strip(),
                }]
            compress_usage = _round_entry_compression_usage(round_entry)
            entry_reasoning_tokens = (
                pending_reasoning_tokens if round_entry is pending_round else 0
            )
            round_tokens.append({
                "question": round_entry.get("question", ""),
                "status": round_entry.get("status", ""),
                "messages": len(round_messages),
                "tokens": _estimate_messages_tokens(round_messages) + entry_reasoning_tokens,
                "reasoning_tokens": entry_reasoning_tokens,
                "compress_count": (
                    compress_usage.get("compression_count", 0)
                    if isinstance(compress_usage, dict) else 0
                ),
            })
            messages.extend(round_messages)

        messages_tokens = _estimate_messages_tokens(messages)
        # 发送口径对齐：真实请求（copy_for_request）只回传"最近一条真实思考"，
        # 历史轮次思考一律剥离；当前轮（pending）的最后一条非空思考会保留，
        # 这里按同一规则补偿估算（详见 _estimate_pending_reasoning_tokens）。
        reasoning_tokens = pending_reasoning_tokens
        messages_tokens += reasoning_tokens
        tool_definition_tokens = _estimate_tool_definition_tokens(tools)
        # 文件清单块（方案二：小文件内联 / 大文件节选 + 按需读取）：真实请求会把
        # 「文件清单」追加进首条 system 消息，统计口径需与真实请求一致；无文件时
        # 为 0。（read_document 自动注入条件近似：会话有文件即视为可用，仅影响
        # 清单尾部提示文案的少量字数，对总量可忽略。）
        file_memory_tokens = 0
        try:
            file_records = file_memory.list_session_file_records(self.session_id)
            if file_records:
                file_records.reverse()  # 与清单构建同序：最近上传在前
                file_block_text = file_memory.build_file_manifest_text(
                    file_records, read_document_available=True
                )
                if file_block_text:
                    file_memory_tokens = _estimate_text_tokens(file_block_text)
        except Exception as file_stats_error:
            print(f"[WARN] 文件清单统计失败（按 0 计入）: {file_stats_error}")
        # 运行时系统提示词（工作路径+系统提示）：真实请求追加在首条 system 消息，
        # 单独统计并计入请求上下文总额，避免低估
        system_prompt_tokens = (
            _estimate_text_tokens(system_prompt_text)
            if isinstance(system_prompt_text, str) and system_prompt_text.strip()
            else 0
        )
        request_context_tokens = (
            messages_tokens + system_prompt_tokens + tool_definition_tokens
            + file_memory_tokens
        )
        context_token_limit = _resolve_model_max_input_tokens(default=8192)

        compress_usage_total = meta.get("compress_usage")
        if not isinstance(compress_usage_total, dict):
            compress_usage_total = {}
        history_compress_usage = meta.get("_history_compress_usage")
        history_compress_count = 0
        if isinstance(history_compress_usage, dict):
            try:
                history_compress_count = max(
                    0, int(history_compress_usage.get("compression_count", 0) or 0)
                )
            except (TypeError, ValueError):
                history_compress_count = 0

        return {
            "context_token_limit": context_token_limit,
            "messages_tokens": messages_tokens,
            "reasoning_tokens": reasoning_tokens,
            "system_prompt_tokens": system_prompt_tokens,
            "tool_definition_tokens": tool_definition_tokens,
            "file_memory_tokens": file_memory_tokens,
            "request_context_tokens": request_context_tokens,
            "estimated_budget_ratio": round(
                request_context_tokens / context_token_limit, 4
            ) if context_token_limit > 0 else 0.0,
            "rounds": {
                "total": len(round_entries) + (1 if pending_round else 0),
                "summarized": summarized_round_count,
                "retained": len(retained_rounds),
                "max_rounds": max_rounds,
            },
            "history_context_mode": "summary_only" if summary_only else "legacy_raw",
            "raw_history_rounds_sent": (
                max(0, len(retained_rounds) - (1 if pending_round else 0))
            ),
            "has_context_summary": bool(summary_message),
            "summary_text_length": len(summary_message) if summary_message else 0,
            "recent_questions_length": (
                len(recent_questions_message.get("content", ""))
                if recent_questions_message else 0
            ),
            "recent_questions_count": len(question_items) if summary_only else 0,
            "context_compress_count": compress_usage_total.get("compression_count", 0),
            "history_compress_count": history_compress_count,
            "round_tokens": round_tokens,
        }

    async def clear_chat_history(self) -> str:
        """
        清空当前会话的所有工具调用历史
        Returns:
            操作结果字符串
        """
        with self._write_guard():
            self._round_store.reset()
            meta = _default_meta(self.session_id)
            # 清空历史只重置对话内容；工作目录/工具选择/模型选择是用户的持久配置，应予保留
            old_meta, _ = _load_meta_and_entries(self._file_path, self.session_id)
            old_work_dir = old_meta.get("work_dir")
            if isinstance(old_work_dir, str) and old_work_dir.strip():
                meta["work_dir"] = old_work_dir
            old_tool_selection = normalize_tool_inputs(old_meta.get("tool_selection"))
            if old_tool_selection:
                meta["tool_selection"] = old_tool_selection
            old_model_selection = _normalize_model_selection(old_meta.get("model_selection"))
            old_model_selection = {
                role: entry for role, entry in old_model_selection.items()
                if entry.get("ownership_name") and entry.get("model_name")
            }
            if old_model_selection:
                meta["model_selection"] = old_model_selection
            _write_meta_and_entries(self._file_path, meta, [])
        return "清空成功"

    async def get_session_work_dir(self) -> str | None:
        """读取会话独立工作目录（_meta.work_dir）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "work_dir")
        if isinstance(value, str) and value.strip():
            return value
        return None

    async def update_session_work_dir(self, work_dir: str | None) -> dict[str, Any]:
        """写入/清除会话独立工作目录（_meta.work_dir）。

        Args:
            work_dir: 目录路径；空串/None 表示清除覆盖，恢复跟随全局默认。
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: 目录不存在或不可访问
        """
        raw = (work_dir or "").strip()
        if raw:
            resolved = resolve_work_dir(raw)
            if resolved is None:
                raise ValueError(f"工作目录不存在或不可访问: {raw}")
            new_value: str | None = str(resolved)
        else:
            new_value = None
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if new_value is None:
                meta.pop("work_dir", None)
            else:
                meta["work_dir"] = new_value
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_session_tool_selection(self) -> dict[str, list[str]] | None:
        """读取会话独立工具选择（_meta.tool_selection）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "tool_selection")
        if value is None:
            return None
        return normalize_tool_inputs(value) or None

    async def update_session_tool_selection(
        self, tool_selection: dict[str, list[Any]] | None
    ) -> dict[str, Any]:
        """写入/清除会话独立工具选择（_meta.tool_selection）。

        Args:
            tool_selection: {服务名: [工具名]} 覆盖快照；传**空 dict** 表示
                「显式无工具模式」（落盘为 EMPTY_TOOL_SELECTION_KEY 哨兵，
                后续不回退全局默认）；传 None 表示清除覆盖，恢复跟随
                全局默认（mcp_servers.json 的 inputs 键）。
                非法条目（非字符串/空串/重复工具名）由服务端规整时丢弃。
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: tool_selection 不是字典
        """
        if tool_selection is not None and not isinstance(tool_selection, dict):
            raise ValueError("tool_selection 必须是 {服务名: [工具名]} 形式的字典")
        normalized = normalize_tool_inputs(tool_selection)
        # 写入值三态：非空快照 / 空选择哨兵（显式无工具）/ 删除键（清除覆盖）
        stored = normalized if normalized else _EMPTY_TOOL_SELECTION
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if tool_selection is None:
                meta.pop("tool_selection", None)
            else:
                meta["tool_selection"] = stored
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_session_model_selection(self) -> dict[str, dict[str, Any]] | None:
        """读取会话独立模型选择（_meta.model_selection）；未覆盖返回 None。"""
        value = read_session_meta_value(self.session_id, "model_selection")
        if value is None:
            return None
        valid = {
            role: entry for role, entry in _normalize_model_selection(value).items()
            if entry.get("ownership_name") and entry.get("model_name")
        }
        return valid or None

    async def update_session_model_selection(
        self, role: str, entry: dict[str, Any] | None
    ) -> dict[str, Any]:
        """写入/清除会话独立模型选择的单个角色（_meta.model_selection）。

        Args:
            role: 模型角色（chat_model / compaction_model / title_model，随版本扩展）
            entry: {ownership_name, model_name, parameter, api_type} 覆盖条目；
                None 表示清除该角色覆盖（恢复跟随全局默认）
        Returns:
            更新后的元数据字典
        Raises:
            ValueError: role 未知或 entry 结构非法
        """
        if role not in _MODEL_SELECTION_ROLES:
            raise ValueError(f"未知模型角色: {role!r}；可选: {'/'.join(_MODEL_SELECTION_ROLES)}")
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            stored = dict(meta.get("model_selection")) if isinstance(meta.get("model_selection"), dict) else {}
            if entry is None:
                stored.pop(role, None)
            else:
                if not isinstance(entry, dict):
                    raise ValueError("model_selection 覆盖条目必须是字典")
                normalized = _normalize_model_selection({role: entry}).get(role) or {}
                if not (normalized.get("ownership_name") and normalized.get("model_name")):
                    raise ValueError("覆盖条目必须包含 ownership_name 与 model_name")
                stored[role] = normalized
            if stored:
                meta["model_selection"] = stored
            else:
                meta.pop("model_selection", None)
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def ensure_session_config_snapshot(self) -> tuple[bool, dict[str, Any]]:
        """会话配置快照：首次正式开始任务时把当前全局默认配置固化为会话独立配置。

        幂等：`_meta.config_snapshot_at` 已存在时直接返回（快照一次后不再重写，
        用户后续的手动会话级修改不受影响，清除单键的「恢复跟随全局」语义也不变）。

        固化内容（仅在会话尚无对应覆盖时写入，已有覆盖键保持不动）：
          - work_dir        ← 全局默认工作目录（.env DEFAULT_CHAT_WORK_DIR，目录有效才写入）
          - tool_selection  ← mcp_servers.json 的 inputs（规整后，非空才写入）
          - model_selection ← models.json 顶层 model_selection（全角色有效条目）
        单项读取失败只跳过该项，不阻断快照与聊天。

        Returns:
            (本次是否真正写入快照, 快照后的元数据字典)
        """
        with self._write_guard():
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            if meta.get("config_snapshot_at"):
                return False, meta
            # 全局默认工作目录
            try:
                persisted = get_persisted_work_dir()
                existing_work_dir = meta.get("work_dir")
                if (
                    isinstance(persisted, str) and persisted.strip()
                    and not (isinstance(existing_work_dir, str) and existing_work_dir.strip())
                ):
                    resolved = resolve_work_dir(persisted)
                    if resolved is not None:
                        meta["work_dir"] = str(resolved)
            except Exception as work_error:
                print(f"[WARN] 会话配置快照（work_dir）失败，已跳过该项: {work_error}")
            # 全局默认工具选择
            try:
                inputs, _servers, tool_error = get_global_tool_inputs()
                if tool_error is None:
                    normalized_inputs = normalize_tool_inputs(inputs)
                    if normalized_inputs and meta.get("tool_selection") is None:
                        meta["tool_selection"] = normalized_inputs
            except Exception as tool_error:
                print(f"[WARN] 会话配置快照（tool_selection）失败，已跳过该项: {tool_error}")
            # 全局默认模型选择（全部角色）
            try:
                global_selection = _get_global_model_selection()
                valid_selection = {
                    role: entry for role, entry in (global_selection or {}).items()
                    if isinstance(entry, dict) and entry.get("ownership_name") and entry.get("model_name")
                }
                if valid_selection and not isinstance(meta.get("model_selection"), dict):
                    meta["model_selection"] = valid_selection
            except Exception as model_error:
                print(f"[WARN] 会话配置快照（model_selection）失败，已跳过该项: {model_error}")
            meta["config_snapshot_at"] = now_str()
            meta["updated_at"] = now_str()
            _write_meta_and_entries(self._file_path, meta, entries)
        return True, meta

    @staticmethod
    def list_chat_sessions():
        """
        列出 HISTORY_ROOT 目录下的所有 jsonl 文件
        :return: 一个包含所有 jsonl 文件路径的列表
        """
        jsonl_files = list(HISTORY_ROOT.glob('*.jsonl'))
        return jsonl_files

    @staticmethod
    def get_chat_session_file(session_id: str):
        """
        返回指定 session_id 的 jsonl 文件响应
        :param session_id: 会话ID
        :return: 文件响应
        """
        chat_history_file = _get_chat_history_file(session_id)
        if chat_history_file.exists():
            return FileResponse(chat_history_file, filename=chat_history_file.name)
        else:
            raise HTTPException(status_code=404, detail="Chat session file not found")

    @staticmethod
    def get_chat_session_meta(session_id: str) -> dict[str, Any]:
        """
        获取指定 session_id 的聊天历史元数据（首行 _meta）
        :param session_id: 会话ID
        :return: 元数据字典
        """
        chat_history_file = _get_chat_history_file(session_id)
        if not chat_history_file.exists():
            raise HTTPException(status_code=404, detail="Chat session file not found")
        manager = ChatMemoryManager(session_id)
        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            _write_meta_and_entries(manager._file_path, meta, entries)
        return meta

    @staticmethod
    def delete_chat_session_file(session_id: str) -> dict:
        """
        删除指定 session_id 的 jsonl 文件，并连同该会话上传文件所在目录一并删除。

        - 上传目录名优先取 jsonl 首行 _meta 记录的 upload_id；
          旧记录无该字段时回退为按 session_id 推导的目录名；
        - jsonl 与上传目录互不依赖：任一侧存在都会被删除
          （可顺带清理有上传目录但无聊天文件的孤儿会话）；
        - 任一删除步骤失败（如 Windows 上文件被占用）都会抛出 OSError，
          由路由层转换为 HTTP 异常返回给前端。

        :param session_id: 会话 ID
        :return: 操作结果字典
        :raises OSError: 删除失败时抛出
        """
        chat_history_file = _get_chat_history_file(session_id)
        recorded_upload_id: Any = None
        if chat_history_file.exists():
            # 先读 _meta 里记录的 upload_id（jsonl 即将删除，需先取出）
            try:
                meta, _ = _load_meta_and_entries(chat_history_file, session_id)
                recorded_upload_id = meta.get("upload_id")
            except OSError:
                recorded_upload_id = None
        deleted_parts: list[str] = []
        # 1) 删除 jsonl 历史文件与其 pending 检查点/todo 计划/.bak 备份侧车
        #    （侧车统一在 sidecars/ 子目录，旧版残留同目录文件也一并清理）
        if chat_history_file.exists():
            _delete_path_with_retry(chat_history_file)
            deleted_parts.append(f"会话文件 {chat_history_file.name}")
        for suffix, label in ((".pending", "检查点"), (".todo", "任务计划"), (".bak", "备份")):
            sidecar = _sidecar_dir() / (chat_history_file.name + suffix)
            if sidecar.exists():
                _delete_path_with_retry(sidecar)
                deleted_parts.append(f"侧车 {sidecar.name}")
            legacy = chat_history_file.with_name(chat_history_file.name + suffix)
            if legacy.exists():
                _delete_path_with_retry(legacy)
                deleted_parts.append(f"旧版侧车 {legacy.name}")
        # 2) 删除上传目录：优先用记录的 upload_id（需重新清洗，避免畸形值逃逸），
        #    否则回退为按 session_id 推导的目录名
        upload_dir_name = ""
        if recorded_upload_id:
            upload_dir_name = _safe_session_id(str(recorded_upload_id))
        if not upload_dir_name:
            upload_dir_name = _safe_session_id(session_id)
        if upload_dir_name:
            upload_dir = UPLOAD_HISTORY_ROOT / upload_dir_name
            if upload_dir.exists():
                _delete_path_with_retry(upload_dir, recursive=True)
                deleted_parts.append(f"上传目录 history_files/session_files/{upload_dir_name}")
        if not deleted_parts:
            return {
                "state": "succeed",
                "describe": f"会话文件 {session_id}_chat.jsonl 及其上传目录均不存在",
            }
        return {
            "state": "succeed",
            "describe": "已删除：" + "、".join(deleted_parts),
        }

    @staticmethod
    def delete_chat_session_file_line(session_id: str, startline: int, endline: int) -> dict[str, Any]:
        """
        删除指定 session_id 的 jsonl 文件指定行范围
        :param session_id: 会话 ID
        :param startline: 要删除的起始行号(从1开始)
        :param endline: 要删除的结束行号(包含,从1开始)
        :return: 操作结果字典
        """
        # 验证参数
        if startline is None or endline is None:
            raise ValueError("startline 和 endline 参数不能为空")
        try:
            startline = int(startline)
            endline = int(endline)
        except (TypeError, ValueError):
            raise TypeError("startline 和 endline 必须是整数")
        if startline < 1 or endline < 1:
            raise ValueError("行号必须是大于等于 1 的整数")
        if startline > endline:
            raise ValueError(f"起始行号({startline})不能大于结束行号({endline})")
        manager = ChatMemoryManager(session_id)
        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            total_lines = len(entries)
            if total_lines == 0:
                return {
                    "state": "failed",
                    "describe": "当前会话历史文件为空，无可删除行"
                }
            # 这里的行号以“业务记录行”计数（不包含第一行 _meta）
            valid_start = max(1, startline)
            valid_end = min(endline, total_lines)
            if valid_start > valid_end:
                return {
                    "state": "failed",
                    "describe": f"指定的行号范围超出文件范围,当前总行数为 {total_lines}"
                }
            remaining_entries = [
                entry for index, entry in enumerate(entries, start=1)
                if index < valid_start or index > valid_end
            ]
            deleted_count = valid_end - valid_start + 1
            # 删除会改变轮次顺序/数量，旧摘要游标不再可靠；下次聊天前由统一
            # 跨轮压缩流程从剩余原始轮次重建累计摘要和问题索引。
            meta["context_summary"] = None
            meta = _recompute_meta_from_entries(manager.session_id, meta, remaining_entries)
            _write_meta_and_entries(manager._file_path, meta, remaining_entries)
        return {
            "state": "succeed",
            "describe": f"已删除第 {valid_start} 到 {valid_end} 行,共 {deleted_count} 行。原始记录数 {total_lines},剩余记录数 {len(remaining_entries)}",
            "meta_after": meta,
            "usage_after": meta.get("usage", {})
        }

    @staticmethod
    def replace_content_in_chat_session(
        session_id: str,
        find_text: str,
        replace_text: str,
    ) -> dict[str, Any]:
        """在会话 JSONL 全部业务记录中把 find_text 替换为 replace_text。

        供前端媒体伪标签删除使用：用户删除消息中渲染的媒体控件后，把历史
        记录里对应的标签原文替换为占位说明（如"用户已删除/文件不存在"）。
        递归遍历每条记录的所有字符串字段（events[].content 等）；未命中时
        不重写文件；轮次数量与顺序不变，摘要游标保持有效，无需重置。
        """
        if not isinstance(find_text, str) or not find_text.strip():
            raise ValueError("find_text 不能为空")
        if not isinstance(replace_text, str):
            replace_text = ""
        manager = ChatMemoryManager(session_id)
        replaced_total = 0
        touched_entries = 0

        def _walk(value: Any) -> Any:
            nonlocal replaced_total
            if isinstance(value, str):
                count = value.count(find_text)
                if count:
                    replaced_total += count
                    return value.replace(find_text, replace_text)
                return value
            if isinstance(value, list):
                return [_walk(item) for item in value]
            if isinstance(value, dict):
                return {key: _walk(item) for key, item in value.items()}
            return value

        with manager._write_guard():
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            new_entries: list[Any] = []
            for entry in entries:
                new_entry = _walk(entry)
                if new_entry is not entry:
                    touched_entries += 1
                new_entries.append(new_entry)
            if replaced_total <= 0:
                return {
                    "state": "failed",
                    "describe": "未在会话历史中找到对应内容",
                    "replaced": 0,
                }
            meta = _recompute_meta_from_entries(manager.session_id, meta, new_entries)
            _write_meta_and_entries(manager._file_path, meta, new_entries)
        return {
            "state": "succeed",
            "describe": f"已替换 {replaced_total} 处（涉及 {touched_entries} 条记录）",
            "replaced": replaced_total,
        }

    @staticmethod
    def import_jsonl_chat_history(
        session_id: str,
        raw_bytes: bytes,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """导入上传的 jsonl 聊天历史文件并写入 history_files 目录。

        流程：
        - 解析上传字节流；首行若是 `{"_meta": ...}` 则作为基础 meta（`session_id` 字段会被移除）；
        - 逐行通过 `parse_round_entry` 验证；不合法行直接丢弃；
        - 目标文件（`<session_id>_chat.jsonl`）已存在且 `overwrite=False`（默认）时，
          自动追加时间戳另存为 `<session_id>_<时间戳>_chat.jsonl`，避免覆盖旧会话；
          `overwrite=True` 时强制覆盖目标文件；
        - 写入后通过 `_recompute_meta_from_entries` 重新计算 user_questions / usage / record_count / completion_count。

        Args:
            session_id: 目标会话 ID（会先经 normalize_session_id 规整）。
            raw_bytes: 上传的 jsonl 字节流。
            overwrite: 是否强制覆盖同名历史文件（默认 False，重名自动另存）。

        Returns:
            包含 state / session_id（实际写入）/ filename / total_lines / imported_rounds /
            skipped_lines / collision / meta 等字段的字典。
        """
        return _import_jsonl_payload(session_id, raw_bytes, overwrite=overwrite)


def _resolve_import_target(
    session_id: str,
    overwrite: bool,
) -> tuple[str, Path, bool]:
    """解析导入目标文件，处理同名冲突。

    Returns:
        (实际 session_id, 目标文件路径, 是否发生冲突另存)
    - overwrite=True：强制写入 `<session_id>_chat.jsonl`（覆盖），collision=False
    - overwrite=False：目标不存在则直接使用；已存在则追加时间戳
      另存为 `<session_id>_<时间戳>_chat.jsonl`，collision=True
    """
    base_id = normalize_session_id(session_id)
    target_file = _get_chat_history_file(base_id)
    if overwrite:
        return base_id, target_file, False
    if not target_file.exists():
        return base_id, target_file, False
    # 同名冲突：追加时间戳（同一秒再次冲突时递增序号）
    counter = 0
    while True:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        if counter:
            suffix = f"{suffix}_{counter}"
        candidate_id = f"{base_id}_{suffix}"
        candidate_file = _get_chat_history_file(candidate_id)
        if not candidate_file.exists():
            return candidate_id, candidate_file, True
        counter += 1


def _parse_jsonl_payload(raw_bytes: bytes) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, int]:
    """解析 jsonl 字节流（不落盘），返回 (base_meta, round_entries, total_lines, skipped_lines)。

    解析规则：
    - 跳过空行
    - 第 1 行若为 `{"_meta": {...}}` 结构，作为基础元数据（其中 session_id 字段会被移除）；
      否则忽略元数据并以默认 meta 起算
    - 其余行尝试用 `parse_round_entry` 还原为标准 `chat_round` 条目；
      无法解析的行直接丢弃并计入 skipped_lines
    兼容 UTF-8 / GBK / UTF-8-sig 编码；单会话导入与 zip 多会话导入共用。
    """
    if raw_bytes is None:
        raise ValueError("raw_bytes 不能为空")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # 兼容 GBK/UTF-8-sig 等常见中文编码
        text = raw_bytes.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    total_lines = len(lines)
    base_meta: dict[str, Any] | None = None
    candidate_entries: list[dict[str, Any]] = []
    if lines and _is_meta_record(_safe_parse_jsonl_line(lines[0])):
        try:
            first_row = json.loads(lines[0])
            base_meta = first_row.get("_meta") if isinstance(first_row, dict) else None
        except json.JSONDecodeError:
            base_meta = None
        lines = lines[1:]
    skipped_lines = 0
    for line in lines:
        parsed = _safe_parse_jsonl_line(line)
        if parsed is None:
            skipped_lines += 1
            continue
        round_entry = parse_round_entry(parsed)
        if round_entry is None:
            skipped_lines += 1
            continue
        candidate_entries.append(round_entry)
    return base_meta, candidate_entries, total_lines, skipped_lines


def _import_jsonl_payload(
    session_id: str,
    raw_bytes: bytes,
    overwrite: bool = False,
) -> dict[str, Any]:
    """将上传的 jsonl 字节流解析并写入历史文件。

    解析规则见 _parse_jsonl_payload；写入逻辑见 _write_imported_session。
    目标文件同名且 overwrite=False（默认）时自动追加时间戳另存，避免覆盖旧会话。

    Returns:
        包含 session_id（实际写入）/ filename / total_lines / imported_rounds /
        skipped_lines / collision / meta 的字典
    """
    base_meta, candidate_entries, total_lines, skipped_lines = _parse_jsonl_payload(raw_bytes)
    actual_session_id, file_path, collision, merged_meta = _write_imported_session(
        session_id, base_meta, candidate_entries, overwrite,
    )
    return {
        "state": "succeed",
        "session_id": actual_session_id,
        "filename": file_path.name,
        "total_lines": total_lines,
        "imported_rounds": len(candidate_entries),
        "skipped_lines": skipped_lines,
        "record_count": merged_meta.get("record_count", len(candidate_entries)),
        "completion_count": merged_meta.get("completion_count", 0),
        "title": merged_meta.get("title"),
        "overwrite": bool(overwrite),
        "collision": bool(collision),
        "meta": merged_meta,
    }


# ---------- 多会话分享打包（zip） / 导入（zip / jsonl 两阶段） ----------
# zip 内目录布局：
#   <session>_chat.jsonl                  # 会话历史（与 history_files 根一致）
#   session_files/<upload_id>/...         # 该会话上传数据（文件解析记录/media/files/file_diffs 等）
#   manifest.json                         # {version: 2, groups: [{id, name}], sessions: [{session_id,
#                                         #   filename, title, upload_id, group_id}]}
#                                         # groups 为随包携带的分组定义（v1 包无此键按空处理；
#                                         #   导入端按组名映射还原归属，跨机器 ID 不同也能还原）
EXPORT_MANIFEST_NAME = "manifest.json"
# 单个导入包的大小上限（视频等媒体原字节在 session_files/ 内，放宽到 512MB）
IMPORT_PACKAGE_MAX_BYTES = 512 * 1024 * 1024
# 导出会话数量上限（防误操作全量打包）
EXPORT_MAX_SESSIONS = 100


def _export_session_payload(session_id: str) -> dict[str, Any] | None:
    """收集单个会话的导出数据（jsonl 文本 + 上传目录），文件缺失返回 None。

    上传目录数据由文件系统直接枚举（与删除会话同口径：目录名优先取
    _meta.upload_id，旧记录回退为按 session_id 推导）。
    """
    session_id = normalize_session_id(session_id)
    file_path = _get_chat_history_file(session_id)
    if not file_path.exists():
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    # 只解析首行 _meta，一次拿 title / upload_id / updated_at
    meta: dict[str, Any] | None = None
    first_line = text.split("\n", 1)[0].strip()
    first_row = _safe_parse_jsonl_line(first_line)
    if _is_meta_record(first_row) and isinstance(first_row.get("_meta"), dict):
        meta = first_row["_meta"]
    recorded_upload_id = ""
    if isinstance(meta, dict) and isinstance(meta.get("upload_id"), str) and meta["upload_id"].strip():
        recorded_upload_id = str(meta["upload_id"])
    if recorded_upload_id:
        upload_dir_name = _safe_session_id(recorded_upload_id)
    else:
        upload_dir_name = _safe_session_id(session_id)
    session_data_root = HISTORY_ROOT / "session_files" / upload_dir_name
    files: list[tuple[str, bytes]] = []
    if session_data_root.is_dir():
        for path in sorted(session_data_root.rglob("*")):
            if path.is_file():
                try:
                    files.append((path.relative_to(session_data_root).as_posix(), path.read_bytes()))
                except OSError:
                    continue
    return {
        "session_id": session_id,
        "filename": file_path.name,
        "text": text,
        "upload_dir_name": upload_dir_name if files else "",
        "files": files,
        "title": str(meta.get("title") or "") if isinstance(meta, dict) else "",
        "updated_at": str(meta.get("updated_at") or "") if isinstance(meta, dict) else "",
        "group_id": str(meta.get("group_id") or "").strip() if isinstance(meta, dict) else "",
    }


def export_sessions_to_zip(session_ids: list[str]) -> tuple[bytes, str, list[str]]:
    """把多个会话打包为 zip 字节流（会话历史 jsonl + 对应 session_files 数据）。

    Args:
        session_ids: 要导出的会话 ID 列表（1 个即为单会话 zip 分享）

    Returns:
        (zip 字节流, 建议下载文件名, 跳过的会话 ID 列表)
    Raises:
        ValueError: 会话 ID 为空 / 超上限 / 全部会话都不存在时抛出
    """
    ids = [normalize_session_id(str(sid)) for sid in (session_ids or []) if str(sid or "").strip()]
    if not ids:
        raise ValueError("请指定要分享的会话")
    if len(ids) > EXPORT_MAX_SESSIONS:
        raise ValueError(f"一次最多分享 {EXPORT_MAX_SESSIONS} 个会话")
    # 去重保序
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            ordered_ids.append(sid)

    used_names: set[str] = {"EXPORT_MANIFEST_NAME"}
    manifest_sessions: list[dict[str, Any]] = []
    referenced_group_ids: list[str] = []
    skipped: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for sid in ordered_ids:
            payload = _export_session_payload(sid)
            if payload is None:
                skipped.append(sid)
                continue
            gid = str(payload.get("group_id") or "").strip()
            if gid and gid not in referenced_group_ids:
                referenced_group_ids.append(gid)
            # jsonl 归档名与会话 ID 对应（同名会话理论上不可能重复出现：ID 即唯一标识）
            jsonl_name = payload["filename"]
            zf.writestr(jsonl_name, payload["text"].encode("utf-8"))
            used_names.add(jsonl_name)
            upload_dir_name = payload["upload_dir_name"]
            for rel_name, data in payload["files"]:
                zf.writestr(f"session_files/{upload_dir_name}/{rel_name}", data)
            manifest_sessions.append({
                "session_id": payload["session_id"],
                "filename": payload["filename"],
                "title": payload["title"],
                "upload_id": upload_dir_name,
                "group_id": gid,
            })
        # 分组定义随包携带（version 2）：导入端按「组名」映射还原归属
        # （本地同名分组复用、缺失则新建；无归属会话不携带任何分组信息）
        name_by_id = {g["id"]: g["name"] for g in _read_session_groups_raw()}
        manifest_groups = [
            {"id": gid, "name": name_by_id[gid]}
            for gid in referenced_group_ids
            if gid in name_by_id
        ]
        manifest = {
            "version": 2,
            "exported_at": now_str(),
            "groups": manifest_groups,
            "sessions": manifest_sessions,
        }
        zf.writestr(EXPORT_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    zip_bytes = buffer.getvalue()
    if not manifest_sessions:
        raise ValueError("所选会话均不存在，未生成分享文件")
    # 下载文件名：单会话用 <session>_chat.zip；多会话用 ytools_sessions_<时间戳>.zip
    if len(manifest_sessions) == 1:
        download_name = f"{manifest_sessions[0]['session_id']}_chat.zip"
    else:
        download_name = f"ytools_sessions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return zip_bytes, download_name, skipped


def list_zip_sessions(raw_bytes: bytes) -> dict[str, Any]:
    """解析分享 zip 包，返回会话清单与各会话在本地是否冲突（预检不落盘）。

    Returns:
        {sessions: [{session_id, filename, title, imported_rounds, skipped_lines,
                     exists, upload_dir_name, media_count}], total}
    Raises:
        ValueError: 非 zip / 无 manifest / manifest 不合法时抛出
    """
    parsed = _parse_import_zip(raw_bytes)
    entries = parsed["items"]
    group_name_by_id = {g["id"]: g["name"] for g in parsed["groups"]}
    # 本机回导兜底：旧包（v1）/裸 zip 无分组定义时，同 ID 分组的名称从本地
    # 注册表解析，仅供前端展示（导入端「按名映射 / 本地已知保留」逻辑不变）
    local_name_by_id = {g["id"]: g["name"] for g in _read_session_groups_raw()}
    sessions: list[dict[str, Any]] = []
    for item in entries:
        meta, round_entries, total_lines, skipped_lines = _parse_jsonl_payload(item["data"])
        title = ""
        if isinstance(meta, dict) and isinstance(meta.get("title"), str):
            title = meta["title"]
        exists = _get_chat_history_file(item["session_id"]).exists()
        upload_dir_name = ""
        if isinstance(meta, dict) and isinstance(meta.get("upload_id"), str) and meta["upload_id"].strip():
            upload_dir_name = _safe_session_id(str(meta["upload_id"]))
        media_count = sum(1 for rel in item["file_names"] if rel.startswith("media/"))
        group_id = ""
        if isinstance(meta, dict) and isinstance(meta.get("group_id"), str) and meta["group_id"].strip():
            group_id = meta["group_id"].strip()
        sessions.append({
            "session_id": item["session_id"],
            "filename": f"{item['session_id']}_chat.jsonl",
            "title": title or item["session_id"],
            "imported_rounds": len(round_entries),
            "skipped_lines": skipped_lines,
            "total_lines": total_lines,
            "exists": exists,
            "upload_dir_name": upload_dir_name,
            "media_count": media_count,
            "group_id": group_id,
            "group_name": (group_name_by_id.get(group_id)
                           or local_name_by_id.get(group_id, "")),
        })
    # 仅返回被包内会话实际引用的分组定义（供前端提示展示）
    referenced_groups = [
        g for g in parsed["groups"]
        if any(s.get("group_id") == g["id"] for s in sessions)
    ]
    return {"sessions": sessions, "total": len(sessions), "groups": referenced_groups}


def _parse_import_zip(raw_bytes: bytes) -> dict[str, Any]:
    """解析 zip 包字节流，校验 manifest 并防路径穿越。

    Returns:
        {
          "items": [{session_id, data(jsonl 字节), files: [(zip 内相对路径, 字节)], file_names}],
          "groups": [{id, name}]  # 随包携带的分组定义（v1 包/裸 zip 为空列表）
        }
    """
    if raw_bytes is None:
        raise ValueError("raw_bytes 不能为空")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 zip 分享包") from exc
    names = zf.namelist()
    manifest_groups: list[dict[str, str]] = []
    if EXPORT_MANIFEST_NAME not in names:
        # 不是 Ytools 分享包：按无 manifest 的裸 zip 处理（仅收根级 *_chat.jsonl）
        manifest_sessions: list[dict[str, Any]] | None = None
        jsonl_names = [
            n for n in names
            if not n.endswith("/") and re.fullmatch(r"[^/]+_chat\.jsonl", n.split("/")[-1])
            and "/" not in n
        ]
    else:
        try:
            manifest = json.loads(zf.read(EXPORT_MANIFEST_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("分享包 manifest.json 解析失败") from exc
        manifest_sessions = manifest.get("sessions")
        if not isinstance(manifest_sessions, list) or not manifest_sessions:
            raise ValueError("分享包 manifest 无会话数据")
        raw_groups = manifest.get("groups")
        if isinstance(raw_groups, list):
            for entry in raw_groups:
                if not isinstance(entry, dict):
                    continue
                gid = str(entry.get("id") or "").strip()
                gname = str(entry.get("name") or "").strip()
                if gid and gname:
                    manifest_groups.append({
                        "id": gid,
                        "name": gname[:SESSION_GROUP_NAME_MAX],
                    })
        jsonl_names = []
        for entry in manifest_sessions:
            filename = entry.get("filename") if isinstance(entry, dict) else None
            if isinstance(filename, str) and filename and filename in names:
                jsonl_names.append(filename)

    items: list[dict[str, Any]] = []
    for jsonl_name in jsonl_names:
        session_id = normalize_session_id(jsonl_name)
        data = zf.read(jsonl_name)
        prefix = f"session_files/{session_id}/"
        file_items: list[tuple[str, bytes]] = []
        for name in names:
            if name.endswith("/") or not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or rel.startswith("/") or ".." in rel or "\\" in rel:
                continue
            try:
                file_items.append((rel, zf.read(name)))
            except (OSError, zipfile.BadZipFile, RuntimeError):
                continue
        items.append({
            "session_id": session_id,
            "data": data,
            "files": file_items,
            "file_names": [rel for rel, _data in file_items],
        })
    if not items:
        raise ValueError("分享包中没有可导入的会话 jsonl")
    return {"items": items, "groups": manifest_groups}


def _write_imported_session(
    session_id: str,
    base_meta: dict[str, Any] | None,
    candidate_entries: list[dict[str, Any]],
    overwrite: bool,
) -> tuple[str, Path, bool, dict[str, Any]]:
    """把解析好的轮次写入本地 jsonl（meta 重算 + 摘要承接），单会话/zip 导入共用。

    Returns:
        (实际写入的 session_id, 目标文件路径, 是否冲突另存, 最终合并 meta)
    """
    actual_session_id, file_path, collision = _resolve_import_target(session_id, overwrite)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    manager = ChatMemoryManager(actual_session_id)
    with manager._write_guard():
        final_entries = list(candidate_entries)
        merged_meta = _recompute_meta_from_entries(
            actual_session_id,
            base_meta,
            final_entries,
        )
        if isinstance(base_meta, dict) and isinstance(base_meta.get("title"), str) and base_meta["title"].strip():
            merged_meta["title"] = base_meta["title"].strip()
        imported_summary = (
            _normalize_context_summary(base_meta.get("context_summary"))
            if isinstance(base_meta, dict) else None
        )
        imported_source_count = 0
        if imported_summary is not None:
            try:
                imported_source_count = int(imported_summary.get("source_round_count", 0) or 0)
            except (TypeError, ValueError):
                imported_source_count = 0
        if imported_summary is not None and 0 <= imported_source_count <= len(candidate_entries):
            # 保真索引只覆盖已压缩轮次（游标之前），未压缩轮次以完整对话回传
            imported_question_items, _raw = _split_context_window(
                candidate_entries, imported_source_count, _default_context_history_rounds()
            )
            imported_summary["recent_questions"] = [
                question for _number, question in imported_question_items
            ]
            imported_summary["recent_question_numbers"] = [
                number for number, _question in imported_question_items
            ]
            imported_summary["recent_questions_scope"] = "all_history"
            merged_meta["context_summary"] = imported_summary
        else:
            # 不能证明摘要游标与导入轮次对应时，宁可下次重建，也不要静默跳过历史。
            merged_meta["context_summary"] = None
        # 导入分组归属规整：包内 group_id 指向本地不存在的分组时清除（避免孤儿
        # 引用；zip 导入随后会按「组名」映射还原有效归属，jsonl 导入保持清除，
        # 本机回导（ID 仍存在）则原样保留）
        group_id = merged_meta.get("group_id")
        if isinstance(group_id, str) and group_id.strip():
            known_ids = {g["id"] for g in _read_session_groups_raw()}
            if group_id.strip() not in known_ids:
                merged_meta.pop("group_id", None)
        _write_meta_and_entries(file_path, merged_meta, final_entries)
    return actual_session_id, file_path, collision, merged_meta


async def import_sessions_from_zip(
    raw_bytes: bytes,
    conflict_strategy: str = "ask",
    decisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从分享 zip 导入多个会话（jsonl + session_files 数据目录）。

    Args:
        raw_bytes: zip 字节流
        conflict_strategy: 全局冲突策略 ask（默认，逐会话等待前端决策，缺失时跳过）/
                           overwrite / rename / skip
        decisions: {session_id: overwrite|rename|skip}，ask 模式下对每个冲突会话的逐项选择

    Returns:
        {state, imported: [...], skipped: [...], failed: [...]}；单个会话失败不阻断其他会话
    """
    if len(raw_bytes) > IMPORT_PACKAGE_MAX_BYTES:
        raise ValueError(
            f"分享包大小超过限制（最大 {IMPORT_PACKAGE_MAX_BYTES // (1024 * 1024)}MB）"
        )
    strategy = (conflict_strategy or "ask").strip().lower()
    if strategy not in {"ask", "overwrite", "rename", "skip"}:
        raise ValueError(f"未知的冲突策略: {conflict_strategy}")
    per_decisions = {str(k): str(v).strip().lower() for k, v in (decisions or {}).items()}
    parsed = _parse_import_zip(raw_bytes)
    entries = parsed["items"]
    package_group_names = {g["id"]: g["name"] for g in parsed["groups"]}
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in entries:
        session_id = item["session_id"]
        try:
            base_meta, round_entries, _total_lines, _skipped = _parse_jsonl_payload(item["data"])
            exists = _get_chat_history_file(session_id).exists()
            if exists:
                # 冲突决策：逐会话 decisions 优先，其次全局非 ask 策略；
                # ask 且无逐项决策时跳过（绝不静默改名/覆盖）
                decision = per_decisions.get(session_id) or (strategy if strategy != "ask" else "skip")
                if decision == "skip":
                    skipped.append({"session_id": session_id, "reason": "conflict_skip"})
                    continue
                if decision not in {"overwrite", "rename"}:
                    failed.append({"session_id": session_id, "error": f"未知冲突决策: {decision}"})
                    continue
            else:
                decision = "create"
            result = await _import_session_with_data(
                session_id=session_id,
                raw_jsonl=item["data"],
                base_meta=base_meta,
                data_files=item["files"],
                overwrite=(decision == "overwrite"),
            )
            resolved_id, resolved_name = _restore_import_group(
                str(result.get("session_id") or session_id),
                base_meta,
                package_group_names,
            )
            result["group_id"] = resolved_id
            result["group_name"] = resolved_name
            imported.append(result)
        except Exception as exc:  # 单会话失败不阻断其他会话
            failed.append({"session_id": session_id, "error": str(exc)})
    return {
        "state": "succeed",
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
    }


def _restore_import_group(
    actual_session_id: str,
    base_meta: dict[str, Any] | None,
    package_group_names: dict[str, str],
) -> tuple[str, str]:
    """导入后的分组归属还原：包内 group_id → 本地分组（按「组名」映射）。

    规则：
    - 包内提供分组定义（manifest.groups，v2）→ 按组名映射：本地同名分组复用、
      缺失则新建（跨机器导入分组 ID 必然不同，组名是唯一可迁移的锚点）；
    - 包内无定义（v1 旧包 / 裸 zip / 单 jsonl）→ 仅当本地已存在同 ID 分组
      （本机回导场景）时保留，否则清除（界面显示为未分组）。
    返回 (最终 group_id 或空串, 最终分组名或空串)。
    """
    gid = ""
    if isinstance(base_meta, dict) and isinstance(base_meta.get("group_id"), str):
        gid = base_meta["group_id"].strip()
    if not gid:
        return "", ""
    name = package_group_names.get(gid, "")
    if name:
        group: dict[str, Any] | None = None
        try:
            group = create_session_group(name)  # 同名幂等：复用既有分组
        except Exception:
            group = None
        if group:
            resolved_id = str(group.get("id") or "")
            resolved_name = str(group.get("name") or "")
            if resolved_id and resolved_id != gid:
                _set_session_group_id(actual_session_id, resolved_id)
            return resolved_id, resolved_name
        # 新建失败：退回「本地已知则保留 / 否则清除」逻辑
    known_ids = {g["id"] for g in _read_session_groups_raw()}
    if gid in known_ids:
        return gid, ""
    _set_session_group_id(actual_session_id, None)
    return "", ""


async def _import_session_with_data(
    session_id: str,
    raw_jsonl: bytes,
    base_meta: dict[str, Any] | None,
    data_files: list[tuple[str, bytes]],
    overwrite: bool,
) -> dict[str, Any]:
    """导入单个会话：写 jsonl（复用单会话导入 meta 逻辑）+ 落盘 session_files 数据目录。

    数据目录一律按「实际会话 ID」命名，绝不移动/混写本地已有会话的目录：
    - 冲突另存（rename/时间戳）时：新会话数据写入 `<新ID>/` 目录，本地原会话的
      数据目录保持原样不动；并把新会话首行 _meta.upload_id 修正为该目录名
      （否则删除新会话时会误删原会话的目录）；
    - overwrite 时：先清空「本地被覆盖会话」的原数据目录（其归属必须在覆盖
      jsonl 之前读取——覆盖写入后首行 _meta.upload_id 会被包内值替换，届时
      无从追溯），再写入新数据；
    - 目标目录名已被占用（残留/撞名）且包内带数据时，追加时间戳换唯一目录；
    - 写入成功后清理注册表缓存管理器（同 ID 旧数据的内存态已过期）。
    """
    base_id = normalize_session_id(session_id)
    # 本地既有数据目录归属（overwrite 决策的清空目标）：必须在覆盖 jsonl 之前读取
    local_dir_name = ""
    local_file = _get_chat_history_file(base_id)
    if local_file.exists():
        try:
            local_rows = _read_jsonlines(local_file)
        except OSError:
            local_rows = []
        if local_rows and _is_meta_record(local_rows[0]):
            local_meta = local_rows[0].get("_meta")
            if (isinstance(local_meta, dict)
                    and isinstance(local_meta.get("upload_id"), str)
                    and str(local_meta["upload_id"]).strip()):
                local_dir_name = _safe_session_id(str(local_meta["upload_id"]))
    if not local_dir_name:
        local_dir_name = _safe_session_id(base_id)

    result = _import_jsonl_payload(session_id, raw_jsonl, overwrite=overwrite)
    actual_session_id = str(result.get("session_id") or base_id)
    final_dir_name = _safe_session_id(actual_session_id)
    session_root = file_memory.HISTORY_ROOT
    if overwrite:
        # 覆盖：清空本地既有数据目录（旧文件不残留）；必须在占用检查之前执行，
        # 否则原目录残留会被误判为“撞名”而把新数据另存到时间戳目录
        legacy_dir = session_root / local_dir_name
        if legacy_dir.is_dir():
            shutil.rmtree(legacy_dir, ignore_errors=True)
    target_dir = session_root / final_dir_name
    # 目录名占用防护：目标目录已存在且本次要写数据（残留/撞名场景）→ 换唯一目录名，
    # 绝不把外来数据混写进他人目录（另存名的 jsonl 由 _resolve_import_target 保证不冲突，
    # 但 session_files/<id>/ 目录可能因历史残留与其它会话目录同名）
    if target_dir.exists() and data_files:
        target_dir = session_root / f"{final_dir_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        final_dir_name = target_dir.name
    written = 0
    for rel, data in data_files:
        safe_rel = rel.replace("\\", "/").strip("/")
        if not safe_rel or safe_rel.startswith("/") or ".." in safe_rel:
            continue
        dest = target_dir / safe_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(data)
            written += 1
        except OSError:
            continue
    # 回写 _meta.upload_id：包内记录与最终目录名不一致时统一修正为新目录名
    #（覆盖 rename 后 delete_file 连带清理才能指向正确的目录）
    recorded_upload_id = ""
    if isinstance(base_meta, dict) and isinstance(base_meta.get("upload_id"), str) and base_meta["upload_id"].strip():
        recorded_upload_id = _safe_session_id(str(base_meta["upload_id"]))
    if recorded_upload_id != final_dir_name:
        try:
            manager = await get_chat_memory_manager(actual_session_id)
            await manager.update_session_upload_id(final_dir_name)
            result["meta"] = await manager.get_session_meta()
            result["title"] = result["meta"].get("title")
        except Exception:
            pass
    # 同 ID 覆盖场景：注册表里可能缓存了旧数据的管理器，统一清掉
    await cleanup_chat_memory_manager(actual_session_id)
    await file_memory.cleanup_file_memory_manager(actual_session_id)
    result["data_files"] = written
    result["upload_dir_name"] = final_dir_name
    return result


def _safe_parse_jsonl_line(line: str) -> Any:
    """把单行 jsonl 安全解析为对象；解析失败返回 None。"""
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


# 会话管理器注册表（用于缓存不同 session_id 的管理器实例）
_session_managers: dict[str, ChatMemoryManager] = {}
_session_lock = threading.Lock()


async def get_chat_memory_manager(session_id: str) -> ChatMemoryManager:
    """
    根据 session_id 获取或创建工具记忆管理器实例
    Args:
        session_id: 会话ID
    Returns:
        ChatMemoryManager实例
    """
    with _session_lock:
        if session_id not in _session_managers:
            _session_managers[session_id] = ChatMemoryManager(session_id)
        return _session_managers[session_id]


def chat_memory_file_exists(session_id: str) -> bool:
    """判断会话历史文件是否已存在（只读检查，绝不创建文件/目录）。

    供查询类接口（如 /chat_context/token_stats）在实例化管理器前使用：
    ChatMemoryManager.__init__ 会 touch 文件并写入 _meta，若查询接口直接
    get_chat_memory_manager，会让"仅查询"的会话凭空产生空历史文件。
    """
    try:
        return _get_chat_history_file(normalize_session_id(session_id)).exists()
    except Exception:
        return False


async def cleanup_chat_memory_manager(session_id: str) -> None:
    """
    清理指定会话的记忆管理器（释放实例引用）
    Args:
        session_id: 会话ID
    """
    with _session_lock:
        if session_id in _session_managers:
            del _session_managers[session_id]


if __name__ == "__main__":
    print("=" * 60)
    print("ChatMemoryManager 并发安全测试")
    print("=" * 60)

    # 测试1：基本功能
    print("\n【测试1】基本功能测试")

    # 为不同session创建独立的管理器
    manager_1 = get_chat_memory_manager("session_1")
    manager_2 = get_chat_memory_manager("session_2")

    # 测试会话1
    manager_1.add_chat_history("工具调用1-1")
    manager_1.add_chat_history("工具调用1-2")
    manager_1.add_chat_history("工具调用1-3")
    print(f"Session 1 历史: {manager_1.get_chat_history()}")

    # 测试会话2
    manager_2.add_chat_history("工具调用2-1")
    manager_2.add_chat_history("工具调用2-2")
    print(f"Session 2 历史: {manager_2.get_chat_history()}")

    # 验证隔离
    print(f"\n验证隔离 - Session 1: {manager_1.get_chat_history()}")
    print(f"验证隔离 - Session 2: {manager_2.get_chat_history()}")

    # 测试2：数量限制
    print("\n【测试2】数量限制测试（最多10条）")
    manager_limit = get_chat_memory_manager("session_limit")
    for i in range(12):
        manager_limit.add_chat_history(f"调用{i+1}")
    history = manager_limit.get_chat_history(10)
    print(f"添加12条后保留: {len(history)} 条")
    print(f"历史记录: {history}")

    # 测试3：文本摘要
    print("\n【测试3】文本摘要测试")
    manager_text = get_chat_memory_manager("session_text")
    manager_text.add_chat_history("第一条很长的工具调用记录" * 10)
    manager_text.add_chat_history("第二条记录")
    manager_text.add_chat_history("第三条记录")
    # text_summary = manager_text.get_chat_history_text(max_total_chars=50)
    # print(f"文本摘要（限制50字符）:\n{text_summary}")

    # 测试4：清理功能
    print("\n【测试4】清理功能测试")
    print(f"清理前 Session 1: {len(manager_1.get_chat_history())} 条")
    manager_1.clear_chat_history()
    print(f"清理后 Session 1: {len(manager_1.get_chat_history())} 条")
    print(f"Session 2 未受影响: {len(manager_2.get_chat_history())} 条")

    # 测试5：并发安全测试
    print("\n【测试5】并发安全测试")
    import time

    errors = []

    def worker(session_id, num_calls):
        try:
            manager = get_chat_memory_manager(session_id)
            for i in range(num_calls):
                manager.add_chat_history(f"Thread-{session_id}-Call-{i}")
                time.sleep(0.001)  # 模拟一些工作
        except Exception as e:
            errors.append(str(e))

    # 创建多个线程同时操作不同的session
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(f"concurrent_session_{i}", 20))
        threads.append(t)
        t.start()

    # 等待所有线程完成
    for t in threads:
        t.join()

    print(f"并发测试完成，错误数: {len(errors)}")
    if errors:
        print(f"错误详情: {errors[:3]}")

    # 验证每个会话的数据完整性
    for i in range(5):
        session_id = f"concurrent_session_{i}"
        manager = get_chat_memory_manager(session_id)
        history = manager.get_chat_history(10)
        print(f"  {session_id}: {len(history)} 条记录")

    # 清理测试数据
    for i in range(5):
        cleanup_chat_memory_manager(f"concurrent_session_{i}")
    cleanup_chat_memory_manager("session_1")
    cleanup_chat_memory_manager("session_2")
    cleanup_chat_memory_manager("session_limit")
    cleanup_chat_memory_manager("session_text")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)
