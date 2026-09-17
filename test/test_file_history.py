# coding: utf-8
"""V2 文件历史版本链测试（docs/file_diff.md §9）：record_change / diff 视图 / 撤回 / 回退 / 保留 / 保存。"""
# 标准库
import hashlib
from pathlib import Path

# 第三方库
import pytest

# 自定义模块
from memory import file_history as store


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """把版本链根目录指到临时目录 + 提供一个被跟踪的目标文件。"""
    fake_root = tmp_path / "history_upload"
    monkeypatch.setattr(store, "HISTORY_DIFF_ROOT", fake_root)
    target = tmp_path / "proj" / "main.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8", newline="")
    return {"root": fake_root, "target": target}


SESSION = "pytest-history"


def _record(target: Path, old: str, new: str, tool="edit_file", rnd=1):
    return store.record_change(
        SESSION,
        path=str(target),
        display_path=str(target),
        old_text=old,
        new_text=new,
        tool=tool,
        round_number=rnd,
    )


def test_record_creates_baseline_and_tool_versions(workdir):
    target = workdir["target"]
    result = _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    assert result["recorded"] is True
    files = store.list_files(SESSION)
    assert len(files) == 1
    entry = files[0]
    assert entry["path"] == str(target)
    assert entry["added"] == 1 and entry["removed"] == 1
    # 目录结构：meta.json + g0/v000_base.txt + g0/v001.txt
    key_dir = workdir["root"] / SESSION / "file_diffs" / entry["key"]
    assert (key_dir / "meta.json").exists()
    assert (key_dir / "g0" / "v000_base.txt").read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"
    assert (key_dir / "g0" / "v001.txt").read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 3\n"


def test_total_diff_is_baseline_vs_current(workdir):
    """多次修改后 Total Diff = diff(基线, 当前)，而不是增量拼接（GPT 建议核心）。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n", rnd=1)
    _record(target, "a = 10\nb = 2\nc = 3\n", "a = 10\nb = 20\nc = 3\n", rnd=1)
    result = _record(target, "a = 10\nb = 20\nc = 3\n", "a = 10\nb = 20\nc = 30\n", rnd=1)
    assert result["recorded"] is True
    total = store.total_diff(SESSION, result["key"])
    assert total["lines_added"] == 3
    assert total["lines_removed"] == 3
    assert "+a = 10" in total["diff"] and "-a = 1" in total["diff"]
    assert "+b = 20" in total["diff"] and "-b = 2" in total["diff"]
    assert "+c = 30" in total["diff"] and "-c = 3" in total["diff"]


def test_change_diff_is_single_step(workdir):
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n", rnd=1)
    result = _record(target, "a = 10\nb = 2\nc = 3\n", "a = 10\nb = 20\nc = 3\n", rnd=1)
    single = store.single_diff(SESSION, result["key"], 2)
    assert single["prev_v"] == 1
    assert single["lines_added"] == 1 and single["lines_removed"] == 1
    assert "+b = 20" in single["diff"] and "-b = 2" in single["diff"]


def test_record_idempotent_on_same_content(workdir):
    target = workdir["target"]
    text = "a = 1\nb = 2\nc = 3\n"
    _record(target, text, "a = 10\nb = 2\nc = 3\n")
    result = _record(target, "a = 10\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    assert result["recorded"] is False and result.get("skipped") == "unchanged"
    assert len(store.list_files(SESSION)) == 1


def test_external_change_detected(workdir):
    """用户在两次编辑之间手改文件 → 时间线为 [base, tool, external, tool]。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    # 外部手改（不经 record_change）
    target.write_text("a = 10\nb = 2\nc = 3\n# user note\n", encoding="utf-8", newline="")
    result = _record(
        target,
        "a = 10\nb = 2\nc = 3\n# user note\n",
        "a = 10\nb = 2\nc = 99\n",
    )
    assert result["external_snapshot"] == 2
    versions = store.versions_of(SESSION, result["key"])
    assert [item["role"] for item in versions] == ["baseline", "tool", "external", "tool"]
    # external 版本的 diff 展示"用户改了什么"（+ 用户注释行）
    single = store.single_diff(SESSION, result["key"], 2)
    assert "+# user note" in single["diff"]


def test_rollback_to_round(workdir):
    """回退到第 N 轮发起时状态 = round < N 的最新快照。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n", rnd=1)
    # 真实流程中工具已写盘，这里同步写盘模拟
    target.write_text("a = 10\nb = 2\nc = 3\n", encoding="utf-8", newline="")
    _record(target, "a = 10\nb = 2\nc = 3\n", "a = 10\nb = 20\nc = 3\n", rnd=2)
    target.write_text("a = 10\nb = 20\nc = 3\n", encoding="utf-8", newline="")
    assert target.read_text(encoding="utf-8") == "a = 10\nb = 20\nc = 3\n"
    # 回退到第 2 轮发起时（round<2 的最新 = v1）
    result = store.rollback(SESSION, store.file_key(str(target)), to_round=2)
    assert result["restored_to"]["v"] == 1
    assert target.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 3\n"
    versions = store.versions_of(SESSION, store.file_key(str(target)))
    assert versions[-1]["role"] == "rollback"
    assert versions[-1]["v"] == 3


def test_rollback_to_baseline_restores_disk(workdir):
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 20\nc = 30\n")
    store.rollback(SESSION, store.file_key(str(target)), target="baseline")
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"


def test_hunk_undo_restores_only_one_block(workdir):
    """间隔开的修改形成独立 hunk，各 hunk 可单独撤回。"""
    target = workdir["target"]
    # 20 行文件，修改第 1/7/13 行（未变区间 5 行 > 2n=4 → 3 个独立 hunk）
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    result = store.hunk_undo(SESSION, key, 1)  # 撤回第二个 hunk（line 7）
    assert result["ok"] is True
    content = store.read_content(SESSION, key)
    assert "line 1 v2" in content["content"]
    assert "line 7\n" in content["content"]      # 已还原
    assert "line 13 v2" in content["content"]    # 未撤回的保留
    total = store.total_diff(SESSION, key)
    assert total["lines_added"] == 2 and total["lines_removed"] == 2


def test_hunk_undo_noop_errors(workdir):
    target = workdir["target"]
    _record(target, "a = 1\n", "a = 10\n")
    key = store.file_key(str(target))
    with pytest.raises(ValueError):
        store.hunk_undo(SESSION, key, 5)  # 越界
    # 全部还原后再撤 → 无变更可撤
    store.rollback(SESSION, key, target="baseline")
    with pytest.raises(ValueError):
        store.hunk_undo(SESSION, key, 0)


def test_user_save_with_optimistic_lock(workdir):
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\n", "a = 10\nb = 2\n")
    key = store.file_key(str(target))
    latest_hash = store.read_content(SESSION, key)["hash"]
    # 错误 hash → 冲突
    with pytest.raises(PermissionError):
        store.user_save(SESSION, key, "a = 111\nb = 2\n", "deadbeef")
    # 正确保存
    result = store.user_save(SESSION, key, "a = 111\nb = 2\n", latest_hash)
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "a = 111\nb = 2\n"
    versions = store.versions_of(SESSION, key)
    assert versions[-1]["role"] == "user_edit"


def test_keep_locks_history_and_opens_new_generation(workdir):
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\n", "a = 10\nb = 2\n")
    key = store.file_key(str(target))
    result = store.keep(SESSION, key)
    assert result["ok"] is True and result["new_gen"] == 1
    # 保留后：当前代（g1）基线 = 保留内容，Total 归零
    total = store.total_diff(SESSION, key)
    assert total["diff_skipped"] == "unchanged"
    # 保留后继续编辑仍可入链（新代）
    _record(target, "a = 10\nb = 2\n", "a = 10\nb = 22\n")
    total = store.total_diff(SESSION, key)
    assert total["lines_added"] == 1 and total["lines_removed"] == 1
    # 历史代锁定：回退到旧代版本被拒绝
    with pytest.raises(PermissionError):
        store.rollback(SESSION, key, to_version=1)


def test_oversized_file_not_recorded(workdir, monkeypatch):
    target = workdir["target"]
    big = "x" * (store._FILE_HISTORY_MAX_TEXT_CHARS + 1)
    result = _record(target, "a = 1\n", big)
    assert result["recorded"] is False
    assert result.get("skipped") == "file_too_large"
    assert store.list_files(SESSION) == []


def test_file_key_stable_case_insensitive(workdir):
    """Windows 大小写不敏感：同路径不同大小写 → 同一 key。"""
    assert store.file_key("C:/A/B.py") == store.file_key("c:\\a\\b.py")


def test_hunk_parsing(workdir):
    hunks = store.parse_hunks(
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n-a = 1\n+a = 10\n@@ -10 +10 @@\n-z\n+z"
    )
    assert len(hunks) == 2
    assert hunks[0] == {"old_start": 1, "old_count": 3, "new_start": 1, "new_count": 3}
    assert hunks[1] == {"old_start": 10, "old_count": 1, "new_start": 10, "new_count": 1}


def test_hunk_keep_accepts_only_target(workdir):
    """保留此处：只接受目标差异块，其余还原为基线，写回磁盘并开新代。"""
    target = workdir["target"]
    # 20 行文件，改动第 1/7/13 行 → 3 个独立 hunk
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    result = store.hunk_keep(SESSION, key, 1)  # 只保留 line 7 的修改
    assert result["ok"] is True and result["new_gen"] == 1
    content = store.read_content(SESSION, key)
    assert "line 1\n" in content["content"]      # 未保留的 hunk 已还原
    assert "line 7 v2" in content["content"]     # 目标 hunk 保留
    assert "line 13\n" in content["content"]     # 未保留的 hunk 已还原
    assert target.read_text(encoding="utf-8") == content["content"]  # 磁盘同步
    total = store.total_diff(SESSION, key)
    assert total["diff_skipped"] == "unchanged"  # 新代 Total 归零
    # 历史版本仍可查看（hunk_keep 版本含"其余还原"前后状态）
    versions = store.versions_of(SESSION, key)
    assert versions[-2]["role"] == "hunk_keep"
    assert versions[-1]["role"] == "baseline"


def test_hunk_keep_delete_only_block(workdir):
    """保留"纯删除"差异块：目标区段被删除，其余保留。"""
    target = workdir["target"]
    old = "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\nl11\nl12\nl13\nl14\nl15\n"
    # 删除 l7（20 行中间），加尾部注释 → 2 个 hunk
    new = "l1\nl2\nl3\nl4\nl5\nl6\nl8\nl9\nl10\nl11\nl12\nl13\nl14\nl15\n# tail\n"
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    result = store.hunk_keep(SESSION, key, 0)  # 只接受"删除 l7"这一块
    assert result["ok"] is True
    content = store.read_content(SESSION, key)["content"]
    assert "l7" not in content          # 目标删除生效
    assert "# tail" not in content      # 其余 hunk 还原为基线
    assert content.count("l1\n") == 1 and "l15\n" in content


def test_sync_from_disk_merges_external_edit(workdir):
    """从磁盘刷新：外部修改并入版本链，Total Diff 随之重算。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\n", "a = 10\nb = 2\n")
    key = store.file_key(str(target))
    # 外部（如 VS Code）改文件
    target.write_text("a = 10\nb = 999\n", encoding="utf-8", newline="")
    result = store.sync_from_disk(SESSION, key)
    assert result["synced"] is True
    versions = store.versions_of(SESSION, key)
    assert versions[-1]["role"] == "external"
    total = store.total_diff(SESSION, key)
    assert "+b = 999" in total["diff"] and "-b = 2" in total["diff"]
    # 再同步一次 → 一致，no-op
    result2 = store.sync_from_disk(SESSION, key)
    assert result2["synced"] is False


def test_list_files_hide_clean(workdir):
    """hide_clean=True 隐藏已全部撤回（无行数变化）的文件；留档仍可删除。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\n", "a = 10\nb = 2\n")
    key = store.file_key(str(target))
    assert len(store.list_files(SESSION, hide_clean=True)) == 1
    # 全部还原 → Total 归零 → hide_clean 过滤掉；显式列出仍可见
    store.rollback(SESSION, key, target="baseline")
    assert store.list_files(SESSION, hide_clean=True) == []
    assert len(store.list_files(SESSION, hide_clean=False)) == 1
    # 留档清理接口
    assert store.delete_file_history(SESSION, key) is True
    assert store.list_files(SESSION, hide_clean=False) == []


# ---------------- V2.2：全文视图 / 区间撤留 / 磁盘同步 / 批量清理 ----------------

def test_full_view_rows_and_hunks(workdir):
    """full_view：ctx/del/add 行序列 + hunk 坐标（含 index），供前端全文渲染。"""
    target = workdir["target"]
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    view = store.full_view(SESSION, store.file_key(str(target)))
    kinds = [row["t"] for row in view["rows"]]
    assert kinds.count("del") == 3 and kinds.count("add") == 3
    assert len(view["hunks"]) == 3
    assert [h["index"] for h in view["hunks"]] == [0, 1, 2]
    # ctx 行带双行号，del 行无 n、add 行无 o（hunk0 占行 1 → 首个 ctx 为行 2）
    first_ctx = next(r for r in view["rows"] if r["t"] == "ctx")
    assert first_ctx["o"] == 2 and first_ctx["n"] == 2
    del_row = next(r for r in view["rows"] if r["t"] == "del")
    assert "n" not in del_row and del_row["o"] == 1
    add_row = next(r for r in view["rows"] if r["t"] == "add")
    assert "o" not in add_row and add_row["s"].endswith("v2")
    assert view["truncated"] is False


def test_full_view_truncation(workdir):
    """full_view 超过 max_rows 截断（前端回退 compact 模式）。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 30\n")
    view = store.full_view(SESSION, store.file_key(str(target)), max_rows=2)
    assert view["truncated"] is True
    assert len(view["rows"]) <= 2


def test_hunk_undo_until_hunk_restores_tail(workdir):
    """撤回此处及之后：hunk0..末尾全部还原，内容等于基线。"""
    target = workdir["target"]
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    result = store.hunk_undo(SESSION, key, 0, until_hunk=True)
    assert result["ok"] is True and result["undone_count"] == 3
    content = store.read_content(SESSION, key)["content"]
    assert "line 1 v2" not in content and "line 13 v2" not in content
    assert content == old                     # 全部还原 = 基线
    assert target.read_text(encoding="utf-8") == old   # 磁盘同步写回


def test_hunk_keep_until_hunk_accepts_tail(workdir):
    """保留此处及之后：接受目标及之后所有差异块，之前的还原为基线。"""
    target = workdir["target"]
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    result = store.hunk_keep(SESSION, key, 1, until_hunk=True)
    assert result["ok"] is True and result["kept_count"] == 2
    content = store.read_content(SESSION, key)["content"]
    assert "line 1\n" in content and "line 1 v2" not in content   # hunk0 还原
    assert "line 7 v2" in content and "line 13 v2" in content     # 尾部保留
    total = store.total_diff(SESSION, key)
    assert total["diff_skipped"] == "unchanged"   # 新代 Total 归零


def test_hunk_undo_single_writes_disk(workdir):
    """单块撤回后磁盘同步更新（编辑器与磁盘一致）。"""
    target = workdir["target"]
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    store.hunk_undo(SESSION, key, 1)
    disk = target.read_text(encoding="utf-8")
    assert "line 7\n" in disk and "line 7 v2" not in disk
    assert "line 1 v2" in disk and "line 13 v2" in disk


def test_cleanup_file_histories_removes_only_clean(workdir):
    """批量清理：只清无行数变化的留档；仍有变更的文件不受影响。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\n", "a = 10\nb = 2\n")
    dirty_key = store.file_key(str(target))
    # 第二个文件：全部撤回 → clean
    other = workdir["target"].parent / "other.txt"
    other.write_text("x = 1\n", encoding="utf-8", newline="")
    store.record_change(
        SESSION, path=str(other), display_path=str(other),
        old_text="x = 1\n", new_text="x = 2\n", tool="write_file", round_number=1,
    )
    clean_key = store.file_key(str(other))
    store.rollback(SESSION, clean_key, target="baseline")
    result = store.cleanup_file_histories(SESSION, clean_only=True)
    assert result["removed_count"] == 1
    assert result["removed"][0]["key"] == clean_key
    assert store.list_files(SESSION, hide_clean=False) == [
        item for item in store.list_files(SESSION, hide_clean=False) if item["key"] == dirty_key
    ]
    assert len(store.list_files(SESSION, hide_clean=True)) == 1


def test_full_view_hunks_match_hunk_undo(workdir):
    """full_view.hunks 与 hunk_undo/keep 的 hunk_index 同一坐标体系。"""
    target = workdir["target"]
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = "".join(
        f"line {i} v2\n" if i in (1, 7, 13) else f"line {i}\n"
        for i in range(1, 21)
    )
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    view = store.full_view(SESSION, key)
    total = store.total_diff(SESSION, key)
    assert len(view["hunks"]) == len(store.parse_hunks(total["diff"])) == 3
    # 按 full_view 坐标撤回 hunk1，结果与 parse_hunks 坐标一致
    store.hunk_undo(SESSION, key, view["hunks"][1]["index"])
    content = store.read_content(SESSION, key)["content"]
    assert "line 7 v2" not in content and "line 1 v2" in content


def test_hunk_index_consistent_with_full_view(workdir):
    """V2.3 回归：相邻变更在 unified n=2 下合并、SequenceMatcher 下独立——
    编辑器按 full_view 序号撤回必须精确命中同一块（此前两套坐标错位导致越界）。"""
    target = workdir["target"]
    # 相隔 3 行的两处修改：unified(n=2) 合并为 1 个 hunk；SequenceMatcher 是 2 块
    old = "".join(f"line {i}\n" for i in range(1, 12))
    new = "\n".join(
        "line 2 CHANGED" if i == 2 else ("line 6 CHANGED" if i == 6 else f"line {i}")
        for i in range(1, 12)
    ) + "\n"
    target.write_text(old, encoding="utf-8", newline="")
    _record(target, old, new)
    key = store.file_key(str(target))
    view = store.full_view(SESSION, key)
    assert len(view["hunks"]) == 2
    # 撤回第二块（前端显示的差异块 #2）→ 只还原 line 6，line 2 保留
    result = store.hunk_undo(SESSION, key, 1)
    assert result["ok"] is True
    content = store.read_content(SESSION, key)["content"]
    assert "line 2 CHANGED" in content
    assert "line 6\n" in content and "line 6 CHANGED" not in content
    # 保留第二块 → 新代只含第二处修改
    target.write_text(new, encoding="utf-8", newline="")
    _record(target, old, new, rnd=2)
    result2 = store.hunk_keep(SESSION, key, 1)
    assert result2["ok"] is True
    content2 = store.read_content(SESSION, key)["content"]
    assert "line 6 CHANGED" in content2 and "line 2 CHANGED" not in content2


def test_keep_all_batches(workdir, tmp_path):
    """批量保留：多文件未决变更一次性封版——Total Diff 归零、当前代锁定。"""
    target = workdir["target"]
    target2 = tmp_path / "proj" / "util.py"
    target2.write_text("x = 1\n", encoding="utf-8", newline="")
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    _record(target2, "x = 1\n", "x = 2\ny = 3\n")
    # record 只入链不落盘：模拟真实流程中工具已把新内容写到磁盘
    target.write_text("a = 10\nb = 2\nc = 3\n", encoding="utf-8", newline="")
    target2.write_text("x = 2\ny = 3\n", encoding="utf-8", newline="")
    result = store.keep_all(SESSION)
    assert result["kept_count"] == 2 and len(result["skipped"]) == 0
    for item in result["kept"]:
        total = store.total_diff(SESSION, item["key"])
        assert total["lines_added"] == 0 and total["lines_removed"] == 0
    # 磁盘内容保持不变（保留 = 接受当前内容，不改文件）
    assert target.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 3\n"
    assert target2.read_text(encoding="utf-8") == "x = 2\ny = 3\n"
    # 已无未决变更 → hide_clean 列表为空；重复批量保留返回空
    assert store.list_files(SESSION, hide_clean=True) == []
    assert store.keep_all(SESSION)["kept_count"] == 0
    # 锁定语义与单文件 keep 一致：已封版旧代的版本拒绝作为回退目标
    key = store.file_key(str(target))
    with pytest.raises(PermissionError):
        store.rollback(SESSION, key, to_version=1)


def test_revert_all_batches(workdir, tmp_path):
    """批量撤回：多文件未决变更一次性回退到各自本轮基线（磁盘同步写回）。"""
    target = workdir["target"]
    target2 = tmp_path / "proj" / "util.py"
    target2.write_text("x = 1\n", encoding="utf-8", newline="")
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    _record(target2, "x = 1\n", "x = 2\ny = 3\n")
    # record 只入链不落盘：模拟真实流程中磁盘已是新内容
    target.write_text("a = 10\nb = 2\nc = 3\n", encoding="utf-8", newline="")
    target2.write_text("x = 2\ny = 3\n", encoding="utf-8", newline="")
    result = store.revert_all(SESSION)
    assert result["reverted_count"] == 2 and len(result["skipped"]) == 0
    for item in result["reverted"]:
        assert item["restored_to"]["hash"]
        assert item["disk_removed"] is False  # 基线非空 = 已存在文件的回退
    # 磁盘内容恢复为基线
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"
    assert target2.read_text(encoding="utf-8") == "x = 1\n"
    # Total Diff 归零 + hide_clean 列表为空 + 重复撤回为 no-op
    assert store.list_files(SESSION, hide_clean=True) == []
    assert store.revert_all(SESSION)["reverted_count"] == 0


def test_rollback_empty_baseline_removes_file(workdir):
    """新建文件回退到基线（空文本）：磁盘文件应被删除而非留 0 字节空文件。"""
    target = workdir["target"]
    new_path = target.parent / "brand_new.py"
    new_path.write_text("created = True\n", encoding="utf-8", newline="")
    store.record_change(
        SESSION, path=str(new_path), display_path=str(new_path),
        old_text="", new_text="created = True\n",
        tool="write_file", round_number=1,
    )
    key = store.file_key(str(new_path))
    result = store.rollback(SESSION, key, target="baseline")
    assert result["ok"] is True
    assert result["disk_removed"] is True
    assert not new_path.exists()  # 空文件不留磁盘
    # 版本链留档还在（编辑器仍可回看/找回内容）
    assert (workdir["root"] / SESSION / "file_diffs" / key / "meta.json").exists()


def test_revert_all_removes_new_files_and_keeps_modified(workdir, tmp_path):
    """revert_all 混合场景：新建文件删除磁盘文件，已有文件恢复基线内容。"""
    target = workdir["target"]
    new_file = tmp_path / "proj" / "created_by_tool.py"
    new_file.write_text("flag = 1\n", encoding="utf-8", newline="")
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    store.record_change(
        SESSION, path=str(new_file), display_path=str(new_file),
        old_text="", new_text="flag = 1\n",
        tool="write_file", round_number=1,
    )
    target.write_text("a = 10\nb = 2\nc = 3\n", encoding="utf-8", newline="")
    result = store.revert_all(SESSION)
    by_path = {item["path"]: item for item in result["reverted"]}
    assert result["reverted_count"] == 2
    assert by_path[str(new_file)]["disk_removed"] is True
    assert by_path[str(target)]["disk_removed"] is False
    assert not new_file.exists()
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"


def test_keep_all_with_cleanup_removes_histories(workdir):
    """keep_all(cleanup=True)：封版后版本链留档一并清理（默认行为）。"""
    target = workdir["target"]
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    key = store.file_key(str(target))
    key_dir = workdir["root"] / SESSION / "file_diffs" / key
    assert key_dir.exists()
    result = store.keep_all(SESSION, cleanup=True)
    assert result["kept_count"] == 1
    assert result["cleaned_count"] == 1
    assert not key_dir.exists()  # 封版后代已锁定，留档随之清理
    # cleanup=False 时不清理（供"想保留回看能力"的调用方使用）
    _record(target, "a = 10\nb = 2\nc = 3\n", "a = 11\n", rnd=2)
    result2 = store.keep_all(SESSION, cleanup=False)
    assert result2["kept_count"] == 1
    assert "cleaned_count" not in result2
    key_dir2 = workdir["root"] / SESSION / "file_diffs" / key
    assert key_dir2.exists()


def test_keep_all_skips_broken_entry_isolates_errors(workdir, tmp_path):
    """错误隔离：单个文件操作失败（孤儿条目）不影响其余文件（逐条记入 skipped）。"""
    target = workdir["target"]
    target2 = tmp_path / "proj" / "util.py"
    target2.write_text("x = 1\n", encoding="utf-8", newline="")
    _record(target, "a = 1\nb = 2\nc = 3\n", "a = 10\nb = 2\nc = 3\n")
    _record(target2, "x = 1\n", "x = 2\ny = 3\n")
    key2 = store.file_key(str(target2))
    # 模拟索引孤儿：删掉 target2 的版本链目录但 index.json 残留（磁盘异常场景）
    import shutil as _shutil
    _shutil.rmtree(workdir["root"] / SESSION / "file_diffs" / key2, ignore_errors=True)
    result = store.keep_all(SESSION)
    assert result["kept_count"] == 1
    assert len(result["skipped"]) == 1
    skipped = result["skipped"][0]
    assert skipped["key"] == key2
    assert skipped.get("reason")  # 带原因说明
    # 正常的 target 仍被批量保留成功
    assert store.total_diff(SESSION, store.file_key(str(target)))["lines_added"] == 0
