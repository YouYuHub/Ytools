# -*- coding: utf-8 -*-
"""视频部件一次性消费回归：回收旧 video 部件 + 单批视频配额≤1。

供应商普遍限制单请求视频数量（实测 2 > 1 即 400）：分段串读视频时，
先前注入的切片若继续滞留上下文，第二次读取一注入就崩溃；多段切片
base64 滞留也是输入 token 膨胀的主因。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt


def _video_part(ref="data:video/mp4;base64,QUJD"):
    return {"type": "video_url", "video_url": {"url": ref}}


def _image_part():
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


class RetirePriorVideoPartsTests(unittest.TestCase):
    """retire_prior_video_parts：仅回收 _internal user 消息中的 video 部件。"""

    def test_retires_only_internal_user_video_parts(self):
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {
                "role": "user",
                "_internal": True,
                "content": [
                    {"type": "text", "text": "读取结果："},
                    _video_part(),
                    _image_part(),
                ],
            },
            {"role": "user", "content": [_video_part()]},  # 非 _internal：不动
            {"role": "assistant", "_internal": True, "content": [_video_part()]},  # 非 user：不动
        ]
        retired = bt.retire_prior_video_parts(messages)
        self.assertEqual(retired, 1)
        inner = messages[1]["content"]
        self.assertEqual(inner[1]["type"], "text")
        self.assertIn("已回收", inner[1]["text"])
        self.assertIn("重新读取", inner[1]["text"])
        # 图片部件不受影响
        self.assertEqual(inner[2]["type"], "image_url")
        # 非 _internal 的 user 视频与 assistant 视频原样保留
        self.assertEqual(messages[2]["content"][0]["type"], "video_url")
        self.assertEqual(messages[3]["content"][0]["type"], "video_url")

    def test_repeated_retirement_is_idempotent(self):
        messages = [
            {"role": "user", "_internal": True, "content": [_video_part(), _video_part()]}
        ]
        first = bt.retire_prior_video_parts(messages)
        second = bt.retire_prior_video_parts(messages)
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)

    def test_non_list_messages_return_zero(self):
        self.assertEqual(bt.retire_prior_video_parts(None), 0)
        self.assertEqual(bt.retire_prior_video_parts("nope"), 0)


class CapVideoPartsInBatchTests(unittest.TestCase):
    """cap_video_parts_in_batch：单批仅保留 1 个 video 部件。"""

    def test_single_video_passes_through(self):
        parts = [_video_part(), _image_part()]
        capped = bt.cap_video_parts_in_batch(parts)
        self.assertEqual(capped, parts)

    def test_multiple_videos_capped_to_one(self):
        parts = [_image_part(), _video_part(), _video_part(), _video_part()]
        capped = bt.cap_video_parts_in_batch(parts)
        video_count = sum(1 for p in capped if p["type"] == "video_url")
        self.assertEqual(video_count, 1)
        self.assertEqual(capped[0]["type"], "image_url")
        # 第 2/3 个视频转占位：说明读取成功但数据未随请求发送
        for placeholder in capped[2:]:
            self.assertEqual(placeholder["type"], "text")
            self.assertIn("视频数据未随本请求发送", placeholder["text"])
            self.assertIn("单独调用 read_media", placeholder["text"])

    def test_hint_text_carries_range(self):
        parts = [_video_part(), _video_part()]
        capped = bt.cap_video_parts_in_batch(
            parts, reference_hint="media://v.mp4", read_hint={"start": 60, "end": 120}
        )
        self.assertIn("60-120s", capped[1]["text"])


class RetireAndCapIntegrationTests(unittest.TestCase):
    """两内核串联：注入新批次前回收旧视频，新批次自身配额≤1。"""

    def test_serial_second_read_keeps_single_video_in_context(self):
        # 第一批注入：1 个视频
        messages = []
        first_parts = bt.cap_video_parts_in_batch([_video_part()])
        messages.append({
            "role": "user", "_internal": True,
            "content": [{"type": "text", "text": "窗口1"}] + first_parts,
        })
        # 第二批注入前：回收旧视频 → 新批只有 1 个视频
        bt.retire_prior_video_parts(messages)
        second_parts = bt.cap_video_parts_in_batch([_video_part(), _video_part()])
        messages.append({
            "role": "user", "_internal": True,
            "content": [{"type": "text", "text": "窗口2"}] + second_parts,
        })
        video_count = sum(
            1
            for m in messages
            for p in (m.get("content") or [])
            if isinstance(p, dict) and p.get("type") == "video_url"
        )
        self.assertEqual(video_count, 1)
        # 第一批的视频部件已变占位
        self.assertEqual(messages[0]["content"][1]["type"], "text")


if __name__ == "__main__":
    unittest.main()
