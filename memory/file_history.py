# coding: utf-8
"""文件历史版本链（V2 文件 diff 增强）：Baseline + Version + Diff + Undo。

设计文档：docs/file_diff.md §9（本文件行为与该节契约绑定）。
存储布局（真源是磁盘，不依赖 JSONL 历史回放）：

    history_files/session_files/<session>/file_diffs/
    ├── index.json                    # key -> 展示信息索引（列表接口直接读）
    └── <key8>/                       # key = sha1(绝对路径) 前 8 位
        ├── meta.json                 # 版本链元数据（原子重写）
        └── g0/ g1/ ...               # generation：每代一份全文快照链
            ├── v000_base.txt         # 代基线（首次触碰 / keep 时的全文）
            └── v001.txt ...

核心模型（对齐 VS Code/Codex "Baseline + ChangeSet" 思路）：
- diff 是"两个版本之间计算出来的结果"，不累积增量 diff；
- Total Diff = diff(当前代基线, 当前内容)，回答"这轮任务总共改了什么"；
- 单次 Diff = diff(上一版本, 本版本)（同代内），回答"这一刀改了什么"；
- 撤销分三级：hunk_undo（单个差异块）/ rollback（单文件到任意未锁定版本
  或某轮发起时状态）/ keep（保留封版：历史代锁定，新代以保留内容为基线）。
"""
# 标准库
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

# 自定义模块
from util.timestamp_utils import now_str

try:
    # 同包内复用会话目录命名规则，保证 file_diffs 与 media/files 同居一个会话目录
    from memory.file_memory import _safe_session_id
except Exception:  # pragma: no cover - 独立测试环境兜底
    def _safe_session_id(session_id: str) -> str:
        value = re.sub(r"[^\w .-]+", "_", session_id)
        return value.replace("..", "_").strip(" ._-" ) or "default"

HISTORY_DIFF_ROOT = Path(__file__).resolve().parents[1] / "history_files" / "session_files"
DIFF_DIRECTORY_NAME = "file_diffs"

# 与 builtin_tools._build_file_diff 同口径的护栏（diff 展示层限制，快照同尺寸上限）
_FILE_HISTORY_MAX_TEXT_CHARS = 256 * 1024   # 任一端全文超过 → 整条变更不入链
_FILE_HISTORY_MAX_DIFF_CHARS = 20000        # 单版本 diff 文本上限，超限截断
_FILE_HISTORY_CONTEXT_LINES = 2             # hunk 上下文行数（difflib n=）
_FILE_HISTORY_MAX_FULL_ROWS = 20000         # full_view 全文渲染行数上限（超出截断）

_META_VERSION = 1

# role 白名单：hunk_keep = 编辑器"保留此处"（接受单个差异块并固化为新基线），
# source=disk_sync 的 external = "从磁盘刷新"按钮并入的外部修改
_VALID_ROLES = {"baseline", "tool", "user_edit", "hunk_undo", "rollback", "external", "hunk_keep"}

# 每会话一把进程内互斥锁：同进程 asyncio 单线程调度下防 sub_agent 并发写冲突
_LOCKS: dict[str, Any] = {}


def _session_lock(session_id: str):
    normalized = _safe_session_id(session_id)
    lock = _LOCKS.get(normalized)
    if lock is None:
        lock = _LOCKS.setdefault(normalized, __import__("threading").RLock())
    return lock


def _root_dir(session_id: str) -> Path:
    return HISTORY_DIFF_ROOT / _safe_session_id(session_id) / DIFF_DIRECTORY_NAME


def file_key(path: str) -> str:
    """文件绝对路径 → 稳定 key（sha1(normcase) 前 8 位，Windows 大小写不敏感）。"""
    normalized = os.path.normcase(os.path.normpath(str(path)))
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:8]


def _key_dir(session_id: str, key: str) -> Path:
    return _root_dir(session_id) / key


def _meta_path(session_id: str, key: str) -> Path:
    return _key_dir(session_id, key) / "meta.json"


def _version_file(meta: dict[str, Any], version: dict[str, Any]) -> Path:
    return _key_dir(meta["session_id"], meta["key"]) / version["file"]


# ---------------- 磁盘原语：原子写 JSON / 快照文本 ----------------

def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return None


def _write_snapshot(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        fp.write(text)


def _read_snapshot(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ---------------- diff 计算（与 V1 _build_file_diff 同语义） ----------------

def _norm_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def _diff_stats(old_text: str, new_text: str, display: str) -> dict[str, Any]:
    """生成 unified diff 文本与行数统计（展示用，语义同 V1）。"""
    import difflib

    if old_text == new_text:
        return {
            "diff": "", "lines_added": 0, "lines_removed": 0,
            "diff_truncated": False, "diff_skipped": "unchanged",
        }
    diff_gen = difflib.unified_diff(
        _norm_lines(old_text),
        _norm_lines(new_text),
        fromfile=f"a/{display}",
        tofile=f"b/{display}",
        n=_FILE_HISTORY_CONTEXT_LINES,
        lineterm="",
    )
    parts: list[str] = []
    added = removed = 0
    truncated = False
    for index, line in enumerate(diff_gen):
        if index < 2:
            parts.append(line.rstrip("\r\n"))
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        parts.append(line.rstrip("\r\n"))
        if sum(len(part) + 1 for part in parts) > _FILE_HISTORY_MAX_DIFF_CHARS:
            truncated = True
            break
    if not parts:
        return {
            "diff": "", "lines_added": 0, "lines_removed": 0,
            "diff_truncated": False, "diff_skipped": "unchanged",
        }
    return {
        "diff": "\n".join(parts),
        "lines_added": added,
        "lines_removed": removed,
        "diff_truncated": truncated,
        "diff_skipped": "",
    }


_HUNK_HEAD_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text: str) -> list[dict[str, int]]:
    """把 unified diff 解析成 hunk 头列表（old_start/old_count/new_start/new_count）。"""
    hunks: list[dict[str, int]] = []
    if not diff_text:
        return hunks
    old_start = old_count = new_start = new_count = None
    pending = False
    for line in diff_text.split("\n"):
        match = _HUNK_HEAD_RE.match(line)
        if match:
            if pending and old_start is not None:
                hunks.append({
                    "old_start": old_start, "old_count": old_count or 0,
                    "new_start": new_start or 0, "new_count": new_count or 0,
                })
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            pending = True
    if pending and old_start is not None:
        hunks.append({
            "old_start": old_start, "old_count": old_count or 0,
            "new_start": new_start or 0, "new_count": new_count or 0,
        })
    return hunks


def _aligned_opcodes(baseline_text: str, current_text: str) -> list[dict[str, Any]]:
    """统一 hunk 坐标系：SequenceMatcher 对齐基线/当前全文，非 equal 段即差异块。

    这是 full_view / hunk_undo / hunk_keep 共用的唯一划分标准（unified_diff 的
    n=2 上下文会把相邻变更合并成更少的 hunk，导致两套序号错位——V2.3 修复）。
    返回：[{index, tag, old_start, old_count, new_start, new_count,
            old_lines(基线该段), new_lines(当前该段)}]，行号均为 1-based。
    """
    import difflib

    old_lines = _norm_lines(baseline_text)
    new_lines = _norm_lines(current_text)
    hunks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "index": len(hunks),
            "tag": tag,
            "old_start": i1 + 1, "old_count": i2 - i1,
            "new_start": j1 + 1, "new_count": j2 - j1,
            "old_lines": old_lines[i1:i2],
            "new_lines": new_lines[j1:j2],
        })
    return hunks


def _undo_one_hunk(baseline_text: str, current_text: str, hunk: dict[str, int]) -> str:
    """把某个 hunk 撤回：current 中该 hunk 的 +/- 区段还原为基线的旧行。"""
    baseline_lines = _norm_lines(baseline_text)
    current_lines = _norm_lines(current_text)
    old_start = max(hunk.get("old_start", 1), 1) - 1          # 1-based → 0-based
    old_count = hunk.get("old_count", 0)
    new_start = max(hunk.get("new_start", 1), 1) - 1
    new_count = hunk.get("new_count", 0)
    restored = baseline_lines[old_start:old_start + old_count]
    out = current_lines[:new_start] + restored + current_lines[new_start + new_count:]
    return "\n".join(out) + ("\n" if current_text.endswith("\n") else "")


def _extract_hunk_payload(diff_text: str) -> list[dict[str, Any]]:
    """解析 unified diff：每个 hunk 附带 old/new 行序列（供 hunk_keep 重建全文）。

    old_lines = 该 hunk 的 ctx + del 行（基线视角）；new_lines = ctx + add 行
    （目标状态）。del-only hunk 的 new_lines 为空列表。
    """
    hunks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in diff_text.split("\n"):
        match = _HUNK_HEAD_RE.match(line)
        if match:
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            cur = {
                "index": len(hunks),
                "old_start": old_start,
                "old_count": int(match.group(2)) if match.group(2) is not None else 1,
                "new_start": new_start,
                "new_count": int(match.group(4)) if match.group(4) is not None else 1,
                "old_lines": [],
                "new_lines": [],
            }
            hunks.append(cur)
            continue
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            cur["new_lines"].append(line[1:])
        elif line.startswith("-"):
            cur["old_lines"].append(line[1:])
        else:
            # 上下文行（difflib lineterm="" 输出带一个前导空格）须剥掉首字符
            text = line[1:] if line.startswith(" ") else line
            cur["old_lines"].append(text)
            cur["new_lines"].append(text)
    return hunks


# ---------------- meta 读写 ----------------

def _load_meta(session_id: str, key: str) -> dict[str, Any] | None:
    meta = _read_json(_meta_path(session_id, key))
    if isinstance(meta, dict):
        meta.setdefault("session_id", _safe_session_id(session_id))
        return meta
    return None


def _save_meta(meta: dict[str, Any]) -> None:
    payload = {k: v for k, v in meta.items() if k != "session_id"}
    _atomic_write_json(_meta_path(meta["session_id"], meta["key"]), payload)
    _refresh_index(meta)


def _index_path(meta: dict[str, Any]) -> Path:
    return _root_dir(meta["session_id"]) / "index.json"


def _refresh_index(meta: dict[str, Any]) -> None:
    index = _read_json(_index_path(meta)) or {}
    total = meta.get("total") or {}
    index[meta["key"]] = {
        "key": meta["key"],
        "path": meta["path"],
        "display_path": meta["display_path"],
        "kept": bool(meta.get("kept")),
        "versions": len(meta.get("versions") or []),
        "added": total.get("lines_added", 0),
        "removed": total.get("lines_removed", 0),
        "updated_at": (meta.get("versions") or [{}])[-1].get("at", ""),
        "last_role": (meta.get("versions") or [{}])[-1].get("role", ""),
    }
    _atomic_write_json(_index_path(meta), index)


def list_files(session_id: str, hide_clean: bool = False) -> list[dict[str, Any]]:
    """主页面文件变更列表：读 index.json（缺失时扫描各 meta 兜底重建）。

    hide_clean=True 时隐藏"已全部保留/全部撤回"的文件（Total Diff 无行数变化），
    前端统计面板只展示仍有未决变更的文件；其版本链留档仍在，可经删除接口清理。
    """
    with _session_lock(session_id):
        root = _root_dir(session_id)
        index = _read_json(root / "index.json")
        if index is None:
            index = {}
            if root.exists():
                for child in sorted(root.iterdir()):
                    meta = _read_json(child / "meta.json")
                    if isinstance(meta, dict):
                        meta["session_id"] = _safe_session_id(session_id)
                        meta["key"] = child.name
                        _refresh_index(meta)
            index = _read_json(root / "index.json") or {}
        entries = sorted(index.values(), key=lambda item: item.get("updated_at", ""), reverse=True)
        if hide_clean:
            entries = [
                item for item in entries
                if int(item.get("added", 0)) != 0 or int(item.get("removed", 0)) != 0
            ]
        return entries


# ---------------- 版本追加（核心原语） ----------------

def _append_version(
    meta: dict[str, Any],
    content_text: str,
    *,
    role: str,
    tool: str | None,
    round_number: int,
    encoding: str,
    eol: str,
    prev_text: str | None,
    force: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """追加一个版本快照。返回新版本条目；内容与最新版本重复且非 force 时返回 None。"""
    versions = meta.setdefault("versions", [])
    latest = versions[-1] if versions else None
    content_hash = hashlib.sha256(
        content_text.encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    if latest is not None and not force and latest.get("hash") == content_hash:
        return None

    gen = (meta.get("gens") or [{"gen": 0}])[-1]["gen"]
    # gen 内首个版本即代基线；跨代基线（keep）也以 baseline 角色记录
    if not versions or gen != versions[-1]["gen"]:
        role = "baseline"
        prev_text = None
    v_number = (latest["v"] + 1) if latest else 0
    file_name = f"g{gen}/v{v_number:03d}.txt"
    if role == "baseline":
        file_name = f"g{gen}/v{v_number:03d}_base.txt"

    if prev_text is None:
        diff_info = {
            "diff": "", "lines_added": 0, "lines_removed": 0,
            "diff_truncated": False, "diff_skipped": "" if role == "baseline" else "unchanged",
        }
    else:
        diff_info = _diff_stats(prev_text, content_text, meta["display_path"])

    version: dict[str, Any] = {
        "v": v_number,
        "gen": gen,
        "file": file_name,
        "hash": content_hash,
        "bytes": len(content_text.encode("utf-8", errors="replace")),
        "at": now_str(),
        "round": int(round_number),
        "role": role,
        "tool": tool,
        "eol": eol,
        "encoding": encoding,
        "added": diff_info["lines_added"],
        "removed": diff_info["lines_removed"],
        "diff": diff_info["diff"],
        "diff_truncated": diff_info["diff_truncated"],
        "diff_skipped": diff_info["diff_skipped"],
        "prev_v": latest["v"] if latest and latest["gen"] == gen else None,
        "undone_hunks": [],
    }
    if extra:
        version.update(extra)
    versions.append(version)
    _write_snapshot(_key_dir(meta["session_id"], meta["key"]) / file_name, content_text)
    meta["encoding"] = encoding
    meta["eol"] = eol
    return version


def _recompute_total(meta: dict[str, Any]) -> None:
    """Total Diff 缓存：diff(当前代基线, 最新版本)，随每次追加重算。"""
    versions = meta.get("versions") or []
    total = {"lines_added": 0, "lines_removed": 0, "diff": "",
             "diff_truncated": False, "diff_skipped": "unchanged"}
    if versions:
        current_gen = versions[-1]["gen"]
        baseline = next(
            (item for item in versions if item["gen"] == current_gen),
            None,
        )
        if baseline is not None:
            baseline_text = _read_snapshot(_version_file(meta, baseline))
            latest_text = _read_snapshot(_version_file(meta, versions[-1]))
            total = _diff_stats(baseline_text, latest_text, meta["display_path"])
    meta["total"] = total


def record_change(
    session_id: str,
    *,
    path: str,
    display_path: str,
    old_text: str | None,
    new_text: str | None,
    tool: str | None = None,
    round_number: int = 0,
    encoding: str = "utf-8",
    eol: str = "LF",
) -> dict[str, Any]:
    """一次文件写入入链（chat_factory / sub_agent 消费点调用）。

    - 首次触碰：建代（gen0），基线 = 写前磁盘内容（新建文件为空串）；
    - 内容与最新版本相同 → 幂等跳过（返回 skipped）；
    - 写前内容与最新版本不符 → 先补记 role=external（外部改动探测）；
    - 任一端全文超过上限 → 不入链（返回 truncated，不破坏既有链）。
    """
    with _session_lock(session_id):
        key = file_key(path)
        key_dir = _key_dir(session_id, key)
        meta = _load_meta(session_id, key)
        result: dict[str, Any] = {"key": key, "path": path, "recorded": False}

        if old_text is None or new_text is None or (
            len(old_text) > _FILE_HISTORY_MAX_TEXT_CHARS
            or len(new_text) > _FILE_HISTORY_MAX_TEXT_CHARS
        ):
            result["skipped"] = "file_too_large"
            return result
        if old_text == new_text:
            result["skipped"] = "unchanged"
            return result

        if meta is None:
            meta = {
                "version": _META_VERSION,
                "key": key,
                "session_id": _safe_session_id(session_id),
                "path": path,
                "display_path": display_path,
                "kept": False,
                "gens": [{
                    "gen": 0, "at": now_str(), "round": int(round_number),
                    "reason": "task_start", "locked": False,
                }],
                "versions": [],
            }

        latest = (meta.get("versions") or [None])[-1]
        prev_text = old_text
        # 外部改动探测：工具读到的旧内容 ≠ 链上最新版本（用户在两次编辑之间手改）
        if (
            latest is not None and old_text
            and latest.get("hash") != hashlib.sha256(
                old_text.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
        ):
            # 补记 external 版本：diff(最新版本 → 外部状态) 展示"用户改了什么"
            external = _append_version(
                meta, old_text, role="external", tool=None,
                round_number=int(round_number),
                encoding=encoding, eol=eol,
                prev_text=_read_snapshot(_version_file(meta, latest)),
                force=True,
            )
            if external is not None:
                result["external_snapshot"] = external["v"]

        if latest is None:
            # 代基线：记录写前状态（文件已存在才有意义；新建文件记空串基线）
            _append_version(
                meta, old_text, role="baseline", tool=None,
                round_number=int(round_number), encoding=encoding, eol=eol,
                prev_text=None, force=True,
            )
        else:
            prev_text = old_text

        version = _append_version(
            meta, new_text, role="tool", tool=tool,
            round_number=int(round_number), encoding=encoding, eol=eol,
            prev_text=prev_text,
        )
        if version is None:
            result["skipped"] = "unchanged"
            _save_meta(meta)
            return result
        _recompute_total(meta)
        result["recorded"] = True
        result["version"] = version
        result["total"] = meta.get("total")
        _save_meta(meta)
        return result


# ---------------- 读接口 ----------------

def read_content(session_id: str, key: str, v: int | None = None) -> dict[str, Any]:
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        if v is None:
            version = versions[-1]
        else:
            version = next((item for item in versions if item["v"] == v), None)
            if version is None:
                raise KeyError("版本不存在")
        return {
            "key": key,
            "path": meta["path"],
            "display_path": meta["display_path"],
            "v": version["v"],
            "gen": version["gen"],
            "hash": version["hash"],
            "role": version["role"],
            "tool": version["tool"],
            "round": version["round"],
            "at": version["at"],
            "encoding": version.get("encoding", "utf-8"),
            "eol": version.get("eol", "LF"),
            "kept": bool(meta.get("kept")),
            "content": _read_snapshot(_version_file(meta, version)),
        }


def total_diff(session_id: str, key: str) -> dict[str, Any]:
    """Total Diff：diff(当前代基线, 当前内容)；读 meta 缓存，缺失时重算。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        total = meta.get("total")
        if not isinstance(total, dict):
            _recompute_total(meta)
            total = meta.get("total")
        baseline = next(
            (item for item in versions if item["gen"] == versions[-1]["gen"]),
            None,
        )
        return {
            "key": key,
            "display_path": meta["display_path"],
            "baseline_v": baseline["v"] if baseline else None,
            "current_v": versions[-1]["v"],
            "kept": bool(meta.get("kept")),
            **{k: total.get(k) for k in (
                "diff", "lines_added", "lines_removed",
                "diff_truncated", "diff_skipped")},
        }


def full_view(
    session_id: str,
    key: str,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """全文视图（VS Code 式 inline diff）：基线全文 + 差异区块标记，一次接口给全。

    - rows：全文行序列（按 SequenceMatcher 对齐顺序），t=ctx 基线未变行 /
      del 基线被删行 / add 当前新增行；o/n 为旧/新文件 1-based 行号，
      h 为所属差异块序号；
    - hunks：差异块坐标（old_start/old_count/new_start/new_count，兼容
      parse_hunks 输出语义 + index 字段），供 hunk_undo / hunk_keep 使用；
    - max_rows：渲染行数上限（默认 20000），超出 truncated=True（前端回退
      紧凑 diff 模式）。
    """
    import difflib

    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        current_gen = versions[-1]["gen"]
        baseline = next((item for item in versions if item["gen"] == current_gen), None)
        latest_text = _read_snapshot(_version_file(meta, versions[-1]))
        baseline_text = (
            _read_snapshot(_version_file(meta, baseline)) if baseline is not None else ""
        )

        cap = int(max_rows) if max_rows else _FILE_HISTORY_MAX_FULL_ROWS
        old_lines = _norm_lines(baseline_text)
        new_lines = _norm_lines(latest_text)
        rows: list[dict[str, Any]] = []
        hunks: list[dict[str, Any]] = []
        truncated = False
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if truncated:
                break
            if tag == "equal":
                for k in range(i1, i2):
                    if len(rows) >= cap:
                        truncated = True
                        break
                    rows.append({
                        "t": "ctx",
                        "o": k + 1,
                        "n": (k - i1) + j1 + 1,
                        "s": old_lines[k],
                    })
                continue
            hunk_index = len(hunks)
            if len(rows) >= cap:
                truncated = True
                break
            hunks.append({
                "index": hunk_index,
                "old_start": i1 + 1, "old_count": i2 - i1,
                "new_start": j1 + 1, "new_count": j2 - j1,
            })
            for k in range(i1, i2):
                if len(rows) >= cap:
                    truncated = True
                    break
                rows.append({"t": "del", "o": k + 1, "s": old_lines[k], "h": hunk_index})
            for k in range(j1, j2):
                if len(rows) >= cap:
                    truncated = True
                    break
                rows.append({"t": "add", "n": k + 1, "s": new_lines[k], "h": hunk_index})

        total = meta.get("total")
        if not isinstance(total, dict):
            _recompute_total(meta)
            total = meta.get("total")
        return {
            "key": key,
            "path": meta["path"],
            "display_path": meta["display_path"],
            "baseline_v": baseline["v"] if baseline else None,
            "current_v": versions[-1]["v"],
            "current_hash": versions[-1]["hash"],
            "current_ends_with_nl": latest_text.endswith("\n"),
            "kept": bool(meta.get("kept")),
            "rows": rows,
            "hunks": hunks,
            "rows_total": len(old_lines) if not old_lines else max(len(old_lines), len(new_lines)),
            "truncated": truncated,
            "max_rows": cap,
            **{k: (total or {}).get(k) for k in (
                "diff", "lines_added", "lines_removed",
                "diff_truncated", "diff_skipped")},
        }


def single_diff(session_id: str, key: str, v: int) -> dict[str, Any]:
    """单次修改 Diff：版本 v 相对同代上一版本（记录时算好落盘的 diff）。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        version = next((item for item in versions if item["v"] == v), None)
        if version is None:
            raise KeyError("版本不存在")
        return {
            "key": key,
            "display_path": meta["display_path"],
            "v": version["v"],
            "prev_v": version.get("prev_v"),
            "gen": version["gen"],
            "role": version["role"],
            "tool": version["tool"],
            "round": version["round"],
            "at": version["at"],
            "diff": version.get("diff", ""),
            "lines_added": version.get("added", 0),
            "lines_removed": version.get("removed", 0),
            "diff_truncated": version.get("diff_truncated", False),
            "diff_skipped": version.get("diff_skipped", ""),
        }


def versions_of(session_id: str, key: str) -> list[dict[str, Any]]:
    """版本时间线（编辑器侧栏/回退目标选择用，不含正文）。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        return [
            {
                "v": item["v"], "gen": item["gen"], "role": item["role"],
                "tool": item.get("tool"), "round": item.get("round"),
                "at": item.get("at"), "hash": item.get("hash"),
                "added": item.get("added", 0), "removed": item.get("removed", 0),
                "kept": bool(meta.get("kept")) and item["gen"] != (meta.get("gens") or [{}])[-1].get("gen"),
            }
            for item in meta.get("versions") or []
        ]


# ---------------- 写操作：hunk 撤回 / 回退 / 用户保存 / 保留 ----------------

def _check_locked_target(meta: dict[str, Any], version: dict[str, Any]) -> None:
    """保留（keep）之后：已封版的代全部锁定，其中的版本不允许作为回退/撤回目标。"""
    gens = meta.get("gens") or []
    gen_meta = next(
        (item for item in gens if item.get("gen") == version["gen"]), None
    )
    if gen_meta is not None and gen_meta.get("locked"):
        raise PermissionError("该文件已保留封版，历史代不可撤回")

def hunk_undo(
    session_id: str,
    key: str,
    hunk_index: int,
    until_hunk: bool = False,
) -> dict[str, Any]:
    """撤回 Total Diff 中的差异块（不可恢复：仅追加新版本，不删除历史）。

    - until_hunk=False（默认）：只还原 hunk_index 这一块；
    - until_hunk=True：「撤回此处及之后」——还原 hunk_index 及其后所有差异块；
    - 操作后磁盘文件同步写回（编辑器与磁盘保持一致）。
    """
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if len(versions) < 2:
            raise ValueError("当前没有可撤回的变更")
        current_gen = versions[-1]["gen"]
        baseline = next((item for item in versions if item["gen"] == current_gen), None)
        if baseline is None or baseline["v"] == versions[-1]["v"]:
            raise ValueError("当前代没有变更，无可撤回 hunk")
        _check_locked_target(meta, baseline)

        baseline_text = _read_snapshot(_version_file(meta, baseline))
        latest_text = _read_snapshot(_version_file(meta, versions[-1]))
        hunks = _aligned_opcodes(baseline_text, latest_text)
        if not hunks:
            raise ValueError("当前内容与基线一致，无可撤回 hunk")
        if hunk_index < 0 or hunk_index >= len(hunks):
            raise ValueError(f"hunk 序号越界（0~{len(hunks) - 1}）")

        # 区间语义：单块 = [hunk_index, hunk_index]；及之后 = [hunk_index, 末尾]
        undo_hunks = hunks[hunk_index:] if until_hunk else [hunks[hunk_index]]
        restored_text = latest_text
        for hunk in reversed(undo_hunks):   # 从后往前替换避免行号位移
            restored_text = _undo_one_hunk(baseline_text, restored_text, hunk)
        version = _append_version(
            meta, restored_text, role="hunk_undo", tool=None,
            round_number=int((versions[-1].get("round") or 0)),
            encoding=versions[-1].get("encoding", "utf-8"),
            eol=versions[-1].get("eol", "LF"),
            prev_text=latest_text, force=True,
            extra={"undone_hunks": undo_hunks},
        )
        _recompute_total(meta)
        # 磁盘写回：撤回后的内容同步落盘（EOL 按当前版本风格）
        latest = versions[-1]
        target_path = Path(meta["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_eol = "\r\n" if latest.get("eol") == "CRLF" else "\n"
        with target_path.open("w", encoding=latest.get("encoding", "utf-8"), newline="") as fp:
            fp.write(restored_text.replace("\n", write_eol))
        result = {
            "ok": bool(version),
            "hunk": hunks[hunk_index],
            "undone_count": len(undo_hunks),
            "total": meta.get("total"),
        }
        if version is not None:
            result["version"] = version
        _save_meta(meta)
        return result


def hunk_keep(
    session_id: str,
    key: str,
    hunk_index: int,
    until_hunk: bool = False,
) -> dict[str, Any]:
    """保留指定差异块，其余差异块还原为基线，结果固化为新代基线。

    - until_hunk=False（默认）：仅接受 hunk_index 这一块（其余还原为基线）；
    - until_hunk=True：「保留此处及之后」——接受 hunk_index 及其后所有差异块，
      其余（之前的）还原为基线；
    - 追加 role=hunk_keep 版本后开新代（不锁定旧代，历史轮次仍可回退）：
      新基线 = 该内容 → Total Diff 归零，已处理块的 diff 随之消失。
    """
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if len(versions) < 2:
            raise ValueError("当前没有可处理的变更")
        current_gen = versions[-1]["gen"]
        baseline = next((item for item in versions if item["gen"] == current_gen), None)
        if baseline is None or baseline["v"] == versions[-1]["v"]:
            raise ValueError("当前代没有变更，无需保留")
        _check_locked_target(meta, baseline)

        baseline_text = _read_snapshot(_version_file(meta, baseline))
        latest_text = _read_snapshot(_version_file(meta, versions[-1]))
        hunks = _aligned_opcodes(baseline_text, latest_text)
        if not hunks:
            raise ValueError("当前内容与基线一致，无需保留")
        if hunk_index < 0 or hunk_index >= len(hunks):
            raise ValueError(f"hunk 序号越界（0~{len(hunks) - 1}）")

        keep_hunks = hunks[hunk_index:] if until_hunk else [hunks[hunk_index]]
        baseline_lines = _norm_lines(baseline_text)
        # 以基线全文为底：未保留的差异块不动（基线本身即还原态），
        # 仅把被保留块的旧区段替换为新区段；从后往前替换避免行号位移
        out = list(baseline_lines)
        for hunk in reversed(keep_hunks):
            start = max(hunk["old_start"] - 1, 0)
            end = start + hunk["old_count"]
            out[start:end] = hunk["new_lines"]
        content = "\n".join(out) + ("\n" if latest_text.endswith("\n") else "")

        latest = versions[-1]
        version = _append_version(
            meta, content, role="hunk_keep", tool=None,
            round_number=int(latest.get("round") or 0),
            encoding=latest.get("encoding", "utf-8"),
            eol="CRLF" if "\r\n" in content else "LF",
            prev_text=latest_text, force=True,
            extra={"kept_hunks": keep_hunks},
        )
        if version is None:
            raise ValueError("内容无变化，无需保留")
        # 开新代：被接受的块固化为正常内容（Total Diff 归零），旧代不锁定
        meta.setdefault("gens", []).append({
            "gen": current_gen + 1,
            "at": now_str(),
            "round": int(latest.get("round") or 0),
            "reason": "hunk_keep",
            "locked": False,
        })
        _append_version(
            meta, content, role="baseline", tool=None,
            round_number=int(latest.get("round") or 0),
            encoding=latest.get("encoding", "utf-8"),
            eol="CRLF" if "\r\n" in content else "LF",
            prev_text=None, force=True,
        )
        _recompute_total(meta)
        # 写回磁盘（保留语义 = 磁盘同步为"仅含被接受变更"的内容）
        target_path = Path(meta["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_eol = "\r\n" if "\r\n" in content else "\n"
        with target_path.open("w", encoding=latest.get("encoding", "utf-8"), newline="") as fp:
            fp.write(content.replace("\n", write_eol))
        result = {
            "ok": True,
            "version": version,
            "new_gen": current_gen + 1,
            "kept_hunk": hunks[hunk_index],
            "kept_count": len(keep_hunks),
            "total": meta.get("total"),
        }
        _save_meta(meta)
        return result


def sync_from_disk(session_id: str, key: str) -> dict[str, Any]:
    """从磁盘刷新：把外部（VS Code 等）对该文件的最新修改并入版本链。

    读 meta.path 当前磁盘内容，与链上最新版本比对：
    - 内容一致 → no-op（synced=False）；
    - 内容不同 → 追加 role=external（source=disk_sync）版本（diff=外部改了什么），
      Total Diff 重算后即包含外部修改，可继续 hunk_undo / rollback。
    """
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        target_path = Path(meta["path"])
        if not target_path.is_file():
            raise ValueError("文件已不存在，无法从磁盘刷新")
        data = target_path.read_bytes()[:_FILE_HISTORY_MAX_TEXT_CHARS + 1]
        if len(data) > _FILE_HISTORY_MAX_TEXT_CHARS:
            raise ValueError("文件过大，无法并入版本链（超过 256KB）")
        if b"\x00" in data:
            raise ValueError("疑似二进制文件，无法并入文本版本链")
        encoding = (versions[-1].get("encoding") or "utf-8")
        try:
            text = data.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = data.decode("utf-8", errors="replace")
        latest = versions[-1]
        latest_text = _read_snapshot(_version_file(meta, latest))
        latest_hash = hashlib.sha256(
            latest_text.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        disk_hash = hashlib.sha256(
            text.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        if disk_hash == latest_hash:
            _save_meta(meta)
            return {"synced": False, "message": "磁盘内容与版本链一致"}
        version = _append_version(
            meta, text, role="external", tool=None,
            round_number=int(latest.get("round") or 0),
            encoding=encoding,
            eol="CRLF" if "\r\n" in text else "LF",
            prev_text=latest_text, force=True,
            extra={"source": "disk_sync"},
        )
        _recompute_total(meta)
        result = {"synced": True, "version": version, "total": meta.get("total")}
        _save_meta(meta)
        return result


def rollback(
    session_id: str,
    key: str,
    *,
    to_version: int | None = None,
    to_round: int | None = None,
    target: str = "baseline",
) -> dict[str, Any]:
    """单文件回退：把文件磁盘内容恢复为某个历史版本并追加 role=rollback 版本。

    - target="baseline"：回退到当前代基线（VS Code 的 Revert file）；
    - to_version=N：回退到指定版本（锁定代中的版本拒绝）；
    - to_round=N：回退到"第 N 轮用户会话发起时"的状态（round<=N 的最新版本）。
    """
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        current_gen = versions[-1]["gen"]

        if to_round is not None:
            candidates = [
                item for item in versions
                if int(item.get("round") or 0) < int(to_round)
            ]
            if not candidates:
                raise ValueError(f"没有 round<={to_round} 的历史版本")
            target_version = candidates[-1]
        elif to_version is not None:
            target_version = next(
                (item for item in versions if item["v"] == int(to_version)), None
            )
            if target_version is None:
                raise ValueError("目标版本不存在")
        else:
            target_version = next(
                (item for item in versions if item["gen"] == current_gen), None
            )
        _check_locked_target(meta, target_version)

        content = _read_snapshot(_version_file(meta, target_version))
        latest_text = _read_snapshot(_version_file(meta, versions[-1]))
        version = _append_version(
            meta, content, role="rollback", tool=None,
            round_number=int(versions[-1].get("round") or 0),
            encoding=target_version.get("encoding", "utf-8"),
            eol=target_version.get("eol", "LF"),
            prev_text=latest_text, force=True,
            extra={"rollback_from": versions[-1]["v"], "rollback_to": target_version["v"]},
        )
        _recompute_total(meta)
        # 磁盘写回：恢复真实文件（目录可能已被外部删除，先建目录）
        target_path = Path(meta["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_text = content.replace("\n", "\r\n") if target_version.get("eol") == "CRLF" else content
        with target_path.open("w", encoding=target_version.get("encoding", "utf-8"), newline="") as fp:
            fp.write(write_text)
        result = {
            "ok": True,
            "version": version,
            "restored_to": {"v": target_version["v"], "hash": target_version["hash"]},
            "total": meta.get("total"),
        }
        _save_meta(meta)
        return result


def user_save(
    session_id: str,
    key: str,
    content: str,
    expected_hash: str,
) -> dict[str, Any]:
    """编辑器保存：绿色区域编辑后的全文落盘 + 入链（乐观锁校验）。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        latest = versions[-1]
        if latest.get("hash") != (expected_hash or "").strip():
            raise PermissionError(
                "文件已被外部修改（expected_hash 不匹配），请刷新后重试"
            )
        _check_locked_target(meta, latest)
        if content == _read_snapshot(_version_file(meta, latest)):
            raise ValueError("内容无变化")
        eol = "CRLF" if "\r\n" in content else "LF"
        version = _append_version(
            meta, content, role="user_edit", tool=None,
            round_number=int(latest.get("round") or 0),
            encoding=latest.get("encoding", "utf-8"),
            eol=eol, prev_text=_read_snapshot(_version_file(meta, latest)),
            force=True,
        )
        _recompute_total(meta)
        target_path = Path(meta["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        write_text = content.replace("\n", "\r\n") if eol == "CRLF" else content
        with target_path.open("w", encoding=latest.get("encoding", "utf-8"), newline="") as fp:
            fp.write(write_text)
        result = {"ok": True, "version": version, "total": meta.get("total")}
        _save_meta(meta)
        return result


def keep(session_id: str, key: str) -> dict[str, Any]:
    """保留封版：当前代锁定（不可再撤回），并以当前内容开新代基线。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        if meta is None:
            raise KeyError("文件历史不存在")
        versions = meta.get("versions") or []
        if not versions:
            raise KeyError("无版本记录")
        current_gens = meta.get("gens") or []
        current_gen_number = versions[-1]["gen"]
        for gen in current_gens:
            if gen.get("gen") == current_gen_number and not gen.get("locked"):
                gen["locked"] = True
        latest = versions[-1]
        latest_text = _read_snapshot(_version_file(meta, latest))
        meta.setdefault("gens", []).append({
            "gen": current_gen_number + 1,
            "at": now_str(),
            "round": int(latest.get("round") or 0),
            "reason": "keep",
            "locked": False,
        })
        # 新代基线 = 保留时刻的内容（新增版本属于新代，prev 归零）
        baseline = _append_version(
            meta, latest_text, role="baseline", tool=None,
            round_number=int(latest.get("round") or 0),
            encoding=latest.get("encoding", "utf-8"),
            eol=latest.get("eol", "LF"),
            prev_text=None, force=True,
        )
        meta["kept"] = True
        _recompute_total(meta)
        result = {
            "ok": True,
            "kept": True,
            "new_gen": current_gen_number + 1,
            "baseline_v": baseline["v"] if baseline else None,
            "total": meta.get("total"),
        }
        _save_meta(meta)
        return result


def delete_file_history(session_id: str, key: str) -> bool:
    """删除单个文件的版本链目录（前端"不再跟踪"或清理用）。"""
    with _session_lock(session_id):
        meta = _load_meta(session_id, key)
        key_dir = _key_dir(session_id, key)
        removed = False
        if key_dir.exists():
            import shutil
            shutil.rmtree(key_dir, ignore_errors=True)
            removed = True
        root = _root_dir(session_id)
        index = _read_json(root / "index.json")
        if isinstance(index, dict) and key in index:
            index.pop(key)
            _atomic_write_json(root / "index.json", index)
        return removed


def cleanup_file_histories(
    session_id: str,
    clean_only: bool = True,
) -> dict[str, Any]:
    """批量清理留档：clean_only=True 只清无行数变化的文件链，False 清全会话。

    撤回/保留只改内容不入档，时间长了会留下"已处理完但还占磁盘"的留档目录，
    本函数按当前列表快照逐个删除（内部持锁，外部无需加锁）。
    """
    with _session_lock(session_id):
        files = list_files(session_id, hide_clean=False)
        removed: list[dict[str, Any]] = []
        for item in files:
            if clean_only and (int(item.get("added", 0)) or int(item.get("removed", 0))):
                continue
            key = item.get("key") or ""
            key_dir = _key_dir(session_id, key)
            if key_dir.exists():
                import shutil
                shutil.rmtree(key_dir, ignore_errors=True)
            root = _root_dir(session_id)
            index = _read_json(root / "index.json")
            if isinstance(index, dict) and key in index:
                index.pop(key)
                _atomic_write_json(root / "index.json", index)
            removed.append({"key": key, "path": item.get("path", "")})
        return {"removed_count": len(removed), "removed": removed}


def stats_summary(session_id: str) -> dict[str, Any]:
    """供会话顶栏徽标使用的轻量统计（文件数 / 总增删行）。"""
    files = list_files(session_id)
    return {
        "total": len(files),
        "added": sum(int(item.get("added", 0)) for item in files),
        "removed": sum(int(item.get("removed", 0)) for item in files),
    }
