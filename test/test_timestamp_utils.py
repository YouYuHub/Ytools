import re
import unittest
from datetime import datetime

from memory.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT, format_timestamp, now_str


class TimestampUtilsTests(unittest.TestCase):
    def test_now_str_matches_default_format(self):
        text = now_str()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # 解析回来应得到与 today/now 相同字段
        parsed = datetime.strptime(text, DEFAULT_TIMESTAMP_FORMAT)
        self.assertEqual(parsed.year, datetime.now().year)

    def test_format_timestamp_with_explicit_datetime(self):
        sample = datetime(2026, 7, 30, 19, 0, 0)
        self.assertEqual(format_timestamp(sample), "2026-07-30 19:00:00")

    def test_single_format_constant(self):
        # 仓库其它位置应该共享同一个格式常量
        for path in (
            "memory/chat_memory.py",
            "memory/chat_round_store.py",
            "memory/file_memory.py",
            "mcp_server/sys_server.py",
        ):
            with open(path, "r", encoding="utf-8") as fp:
                content = fp.read()
            # 不应再出现硬编码 %Y-%m-%d %H:%M:%S 格式串
            self.assertNotIn('"%Y-%m-%d %H:%M:%S"', content, msg=f"硬编码时间戳格式仍在 {path}")
            self.assertNotIn("'%Y-%m-%d %H:%M:%S'", content, msg=f"硬编码时间戳格式仍在 {path}")


if __name__ == "__main__":
    unittest.main()