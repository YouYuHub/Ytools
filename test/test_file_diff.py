"""内置文件工具 diff 功能测试：_build_file_diff / _file_diff 顶级键 / content_hash。

覆盖：
- unified diff 生成正确性（行数统计、文件头格式、CRLF 归一化）
- 边界降级（超大文件跳过、unchanged、新建文件）
- edit_file / write_file 结果携带 _file_diff（前端展示通道）与 content_hash
- 报错路径不携带 _file_diff
- _format_tool_result / _format_result_text 剥离 _file_diff（不进模型上下文）
- read_file 返回 content_hash
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime.builtin_tools import (
    _build_file_diff,
    _content_hash,
    execute_read_file,
    execute_write_file,
    try_execute_builtin_file_tool,
)
from factory.agent_runtime.sub_agent import _format_result_text
from factory.chat_factory import _format_tool_result


def test_build_file_diff_basic_stats():
    old = "def test():\n    value = 1\n    print(value)\n    return value\n"
    new = "def test():\n    value = 2\n    print(value + 1)\n    return value\n"
    diff = _build_file_diff(old, new, "demo.py")
    assert diff["lines_added"] == 2
    assert diff["lines_removed"] == 2
    assert diff["diff_truncated"] is False
    assert diff["diff_skipped"] == ""
    assert diff["diff"].startswith("--- a/demo.py")
    assert "+++ b/demo.py" in diff["diff"]
    assert "+    value = 2" in diff["diff"]
    assert "-    value = 1" in diff["diff"]
    assert "@@ -1,4 +1,4 @@" in diff["diff"]


def test_build_file_diff_crlf_normalized():
    old = "a = 1\r\nb = 2\r\n"
    new = "a = 1\nb = 3\n"
    diff = _build_file_diff(old, new, "x.txt")
    assert "-b = 2" in diff["diff"]
    assert "+b = 3" in diff["diff"]
    assert "\r" not in diff["diff"]


def test_build_file_diff_new_file_all_added():
    diff = _build_file_diff("", "line1\nline2\n", "new.txt")
    assert diff["lines_added"] == 2
    assert diff["lines_removed"] == 0
    assert diff["diff_skipped"] == ""


def test_build_file_diff_too_large_skipped():
    big = "x" * (256 * 1024 + 1)
    diff = _build_file_diff(big, big, "big.bin")
    assert diff["diff"] == ""
    assert diff["diff_skipped"] == "file_too_large"
    assert diff["lines_added"] == 0 and diff["lines_removed"] == 0


def test_build_file_diff_unchanged():
    text = "same\ncontent\n"
    diff = _build_file_diff(text, text, "same.py")
    assert diff["diff_skipped"] == "unchanged"
    assert diff["diff"] == ""


def test_write_file_new_file_with_diff(tmp_path):
    target = tmp_path / "w1.txt"
    result = execute_write_file({"full_file_name": str(target), "content": "abc"})
    assert result["created"] is True
    assert result["_file_diff"]["lines_added"] == 1
    assert result["_file_diff"]["lines_removed"] == 0
    assert result["content_hash"] == _content_hash("abc")
    assert "内容指纹" in result["message"]
    assert target.read_text(encoding="utf-8") == "abc"


def test_write_file_overwrite_diff(tmp_path):
    target = tmp_path / "w2.txt"
    target.write_text("old line\n", encoding="utf-8")
    result = execute_write_file({"full_file_name": str(target), "content": "new line\n"})
    assert result["created"] is False
    assert result["_file_diff"]["lines_added"] == 1
    assert result["_file_diff"]["lines_removed"] == 1
    assert "-old line" in result["_file_diff"]["diff"]
    assert "+new line" in result["_file_diff"]["diff"]


def test_write_file_append_diff(tmp_path):
    target = tmp_path / "w3.txt"
    target.write_text("first\n", encoding="utf-8")
    result = execute_write_file({"full_file_name": str(target), "content": "second\n", "append": True})
    assert result["action"] == "append"
    assert result["_file_diff"]["lines_added"] == 1
    assert result["_file_diff"]["lines_removed"] == 0


def test_edit_file_result_contains_diff(tmp_path):
    target = tmp_path / "e1.py"
    target.write_text("value = 1\n", encoding="utf-8")
    result = try_execute_builtin_file_tool("edit_file", {
        "full_file_name": str(target),
        "old_string": "value = 1",
        "new_string": "value = 2",
    })
    assert "error" not in result
    assert result["_file_diff"]["lines_added"] == 1
    assert result["_file_diff"]["lines_removed"] == 1
    assert "-value = 1" in result["_file_diff"]["diff"]
    assert "+value = 2" in result["_file_diff"]["diff"]
    assert result["content_hash"] == _content_hash("value = 2\n")
    assert "diff +1 -1 行" in result["message"]


def test_edit_file_error_no_diff(tmp_path):
    target = tmp_path / "e2.py"
    target.write_text("value = 1\n", encoding="utf-8")
    result = try_execute_builtin_file_tool("edit_file", {
        "full_file_name": str(target),
        "old_string": "不存在的原文",
        "new_string": "x",
    })
    assert "error" in result
    assert "_file_diff" not in result


def test_format_result_text_strips_file_diff():
    payload = {"message": "ok", "path": "a.txt", "_file_diff": {"diff": "-a\n+b"}}
    text = _format_result_text(payload)
    assert "_file_diff" not in text
    assert json.loads(text)["message"] == "ok"


def test_format_tool_result_strips_file_diff():
    payload = {"message": "ok", "path": "a.txt", "_file_diff": {"diff": "-a\n+b"}}
    text = _format_tool_result(payload)
    assert "_file_diff" not in text
    assert json.loads(text)["message"] == "ok"


def test_read_file_content_hash(tmp_path):
    target = tmp_path / "r1.txt"
    # write_bytes 避开 Windows 下 text 模式的 \n→\r\n 转换，保证字节级一致
    target.write_bytes(b"hello\n")
    result = execute_read_file({"full_file_name": str(target)})
    assert result["content_hash"] == _content_hash("hello\n")
