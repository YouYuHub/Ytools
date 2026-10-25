"""Pure file-diff statistics and compact model receipts; no I/O or session state."""
import difflib
from pathlib import Path
import re
from typing import Any


def build_file_diff(old_text: str, new_text: str, path: Path, *, display_path: str,
                    max_text_chars: int, max_diff_chars: int, context_lines: int) -> dict:
    """生成 unified diff 与行数统计（前端展示用）。

    返回 {diff, lines_added, lines_removed, diff_truncated, diff_skipped}：
    - 文本超限时整体跳过（diff 置空，diff_skipped=file_too_large）；
    - diff 超过字符上限时提前截断（diff_truncated=True）；
    - 新旧文本在比较前统一换行为 \n，展示行不含行尾符。
    """
    skipped = {
        "diff": "",
        "lines_added": None,
        "lines_removed": None,
        "diff_truncated": False,
        "diff_skipped": "file_too_large",
        "changed_ranges": None,
        "path": str(path),
        "display_path": display_path,
    }
    if old_text == new_text:
        return {
            "diff": "",
            "lines_added": 0,
            "lines_removed": 0,
            "diff_truncated": False,
            "diff_skipped": "unchanged",
            "path": str(path),
            "display_path": display_path,
            "changed_ranges": [],
        }
    if len(old_text) > max_text_chars or len(new_text) > max_text_chars:
        return skipped

    def _norm(text: str) -> list:
        unified = text.replace("\r\n", "\n").replace("\r", "\n")
        return unified.splitlines(keepends=True)

    display = display_path
    diff_gen = difflib.unified_diff(
        _norm(old_text),
        _norm(new_text),
        fromfile=f"a/{display}",
        tofile=f"b/{display}",
        n=context_lines,
        lineterm="",
    )
    parts: list = []
    added = removed = 0
    truncated = False
    diff_chars = 0
    changed_ranges: list[dict[str, int | None]] = []
    old_line = new_line = 0
    active = False
    old_start = old_end = new_start = new_end = None
    old_anchor = new_anchor = 0

    def flush_changed_range() -> None:
        nonlocal active, old_start, old_end, new_start, new_end
        if active:
            if old_start is not None and old_end is not None and old_end < old_start:
                old_start = old_end = None
            if new_start is not None and new_end is not None and new_end < new_start:
                new_start = new_end = None
            changed_ranges.append({
                "old_start_line": old_start,
                "old_end_line": old_end,
                "new_start_line": new_start,
                "new_end_line": new_end,
            })
        active = False
        old_start = old_end = new_start = new_end = None

    for index, line in enumerate(diff_gen):
        if index < 2:
            # --- a/path / +++ b/path 文件头（仅内容有变化时输出）
            parts.append(line.rstrip("\r\n"))
            diff_chars += len(parts[-1]) + 1
            continue
        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            flush_changed_range()
            old_line, new_line = int(hunk.group(1)), int(hunk.group(2))
            rendered = line.rstrip("\r\n")
            line_chars = len(rendered) + 1
            if not truncated and diff_chars + line_chars <= max_diff_chars:
                parts.append(rendered)
                diff_chars += line_chars
            elif not truncated:
                truncated = True
            continue
        if line.startswith("+"):
            added += 1
            if not active:
                active = True
                old_anchor, new_anchor = old_line, new_line
            new_start = new_line if new_start is None else min(new_start, new_line)
            new_end = new_line if new_end is None else max(new_end, new_line)
            new_line += 1
        elif line.startswith("-"):
            removed += 1
            if not active:
                active = True
                old_anchor, new_anchor = old_line, new_line
            old_start = old_line if old_start is None else min(old_start, old_line)
            old_end = old_line if old_end is None else max(old_end, old_line)
            old_line += 1
        elif line.startswith(" "):
            flush_changed_range()
            old_line += 1
            new_line += 1
        elif line.startswith("\\"):
            # unified diff 的 no-newline 标记不占文件行号。
            pass
        else:
            continue
        if active:
            if old_start is None:
                old_start, old_end = old_anchor, old_anchor - 1
            if new_start is None:
                new_start, new_end = new_anchor, new_anchor - 1
        rendered = line.rstrip("\r\n")
        line_chars = len(rendered) + 1
        if not truncated and diff_chars + line_chars <= max_diff_chars:
            parts.append(rendered)
            diff_chars += line_chars
        elif not truncated:
            truncated = True
    flush_changed_range()
    if not parts:
        return {
            "diff": "",
            "lines_added": 0,
            "lines_removed": 0,
            "diff_truncated": False,
            "diff_skipped": "unchanged",
            "path": str(path),
            "display_path": display,
            "changed_ranges": [],
        }
    return {
        "diff": "\n".join(parts),
        "lines_added": added,
        "lines_removed": removed,
        "diff_truncated": truncated,
        "diff_skipped": "",
        "path": str(path),
        "display_path": display,
        "changed_ranges": changed_ranges,
    }


def build_edit_model_result(path: Path, edits: list[dict[str, Any]], replacements: int,
                            file_diff: dict[str, Any], content_hash: str, *,
                            display_path: str, max_diff_chars: int) -> dict[str, Any]:
    """Build a compact result for the model; UI diff stays in the separate _file_diff field."""
    changed_ranges = file_diff.get("changed_ranges")
    changed_range = None
    if changed_ranges:
        bounds: dict[str, int | None] = {}
        for key in ("old_start_line", "old_end_line", "new_start_line", "new_end_line"):
            present = [item.get(key) for item in changed_ranges if item.get(key) is not None]
            bounds[key] = (min(present) if key.endswith("start_line") else max(present)) if present else None
        changed_range = bounds

    result: dict[str, Any] = {
        "success": True,
        "path": display_path,
        "replacements": replacements,
        "changed_range": changed_range,
        "stats": {
            "added_lines": file_diff.get("lines_added"),
            "removed_lines": file_diff.get("lines_removed"),
        },
        "content_hash": content_hash,
        "diff_truncated": bool(file_diff.get("diff_truncated")),
    }

    diff_text = str(file_diff.get("diff") or "")
    if file_diff.get("diff_skipped") == "file_too_large":
        result["diff_truncated"] = False
        result["diff_omitted_reason"] = "file_too_large"
        return result
    if file_diff.get("diff_skipped") == "unchanged":
        result["diff_omitted_reason"] = "no_content_changes"
        return result
    if len(diff_text) > max_diff_chars:
        result["diff_truncated"] = True
        result["diff_omitted_reason"] = "over_model_budget"
        # The full diff is retained separately for the UI. Keep an actual diff
        # excerpt (including hunk location), rather than echoing requested edits
        # which may have been normalized or applied in multiple places.
        lines = diff_text.splitlines()
        location = "\n".join(line for line in lines[:4] if line.startswith(("---", "+++", "@@")))[:200]
        removed = [line for line in lines if line.startswith("-") and not line.startswith("---")]
        added = [line for line in lines if line.startswith("+") and not line.startswith("+++")]
        result["diff_preview"] = (location + "\n" + "\n".join(removed[:3])[:200] +
                                  "\n" + "\n".join(added[:3])[:200] + "\n[仅展示部分修改行]")
        result["message"] = "编辑成功；diff_preview 仅为实际修改的开头预览，完整 diff 已留给前端。可按 changed_range 用 read_file 核验。"
        return result

    removed_lines = [line[1:] for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---")]
    added_lines = [line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    requested_old_lines = {line for edit in edits for line in str(edit["old_string"]).replace("\r\n", "\n").splitlines()}
    requested_new_lines = {line for edit in edits for line in str(edit["new_string"]).replace("\r\n", "\n").splitlines()}
    if (removed_lines or added_lines) and all(line in requested_old_lines for line in removed_lines) and all(
        line in requested_new_lines for line in added_lines
    ):
        result["diff_omitted_reason"] = "duplicates_edit_request"
        result["message"] = "编辑成功；diff 仅重复本次替换文本，已省略。修改位置和数量见 changed_range、stats。"
    elif diff_text:
        result["diff"] = diff_text
    else:
        result["diff_omitted_reason"] = "no_diff_available"
    return result
