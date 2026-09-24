"""vision 能力参数链路回归测试。

覆盖：
- env_manager.get_model_config：模型条目 vision 字段提升为顶层 bool（缺省 False）
- memory.file_memory.resolve_media_content_parts / resolve_message_media_refs：
  vision_enabled=False 时图片/视频部件替换为带引用的文本占位（不发 base64）、
  音频部件不受影响；None/缺省保持原解析行为（向后兼容）
- factory.agent_runtime.builtin_tools.execute_read_media：
  vision_enabled=False 时拒绝执行并说明原因
"""
import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from memory import file_memory as fm
from factory.agent_runtime import builtin_tools as bt

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_SESSION = "vision_cap_test"
_TEST_MEDIA_DIR = Path("history_files") / "upload" / _TEST_SESSION

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WAV_BYTES = b"RIFF" + b"\x00" * 24


def _cleanup_media_dir():
    shutil.rmtree(_TEST_MEDIA_DIR, ignore_errors=True)


def _fake_loader(session_id, reference, quality=None, start_time=None, end_time=None):
    if reference == "media://fail.png":
        return None
    return {
        "kind": "image", "mime": "image/png", "base64": "AAAA",
        "stored_name": "a.png", "filename": "a.png", "size": 4,
        "downsampled": False, "quality": None,
    }


class GetModelConfigVisionTests(unittest.TestCase):
    """models.json 模型条目 vision → 模型配置顶层 bool。"""

    def test_vision_promoted_to_top_level(self):
        import env_manager
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "setting").mkdir(parents=True, exist_ok=True)
            (temp_path / "setting" / "models.json").write_text(
                _VISION_FALSE_TRUE_CONFIG(),
                encoding="utf-8",
            )
            try:
                env_manager.init_path(str(temp_path))
                cfg_yes = env_manager.get_model_config("P", "with-vision")
                cfg_no = env_manager.get_model_config("P", "no-vision")
                self.assertIsNotNone(cfg_yes)
                self.assertIsNotNone(cfg_no)
                self.assertIs(cfg_yes["vision"], True)
                self.assertIs(cfg_no["vision"], False)
            finally:
                env_manager.init_path(str(_REPO_ROOT))
                try:
                    from config import set_current_dir
                    set_current_dir(str(original_cwd))
                except Exception:
                    pass


def _VISION_FALSE_TRUE_CONFIG():
    return (
        "{\n"
        '  "P": {\n'
        '    "vendor": "custom_endpoint",\n'
        '    "apiKey": "test-key",\n'
        '    "apiType": "chat-completions",\n'
        '    "models": {\n'
        '      "with-vision": {"id": "with-vision", "vision": true},\n'
        '      "no-vision": {"id": "no-vision"}\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


class ResolveMediaVisionTests(unittest.TestCase):
    """vision_enabled=False：媒体部件转文本占位，不发 base64。"""

    def setUp(self):
        _cleanup_media_dir()

    def tearDown(self):
        _cleanup_media_dir()

    def test_image_replaced_by_reference_label_when_vision_false(self):
        saved = fm.save_session_media(_TEST_SESSION, "示例.png", PNG_BYTES)
        content = [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": saved["media_ref"]}},
        ]
        resolved, unresolved = fm.resolve_media_content_parts(
            _TEST_SESSION, content, vision_enabled=False
        )
        self.assertEqual(unresolved, [])
        # 图片部件被替换为带引用的文本占位（与历史回放口径一致）
        self.assertEqual(resolved[0], content[0])
        self.assertEqual(resolved[1].get("type"), "text")
        self.assertIn("[图片", resolved[1]["text"])
        self.assertIn(saved["media_ref"], resolved[1]["text"])
        # 原始引用落盘口径不受影响：media:// 引用保留（存档不变）
        self.assertNotIn("base64", resolved[1]["text"])

    def test_video_replaced_and_audio_kept_when_vision_false(self):
        saved = fm.save_session_media(_TEST_SESSION, "演示.mp4", PNG_BYTES)
        wav = fm.save_session_media(_TEST_SESSION, "语音.wav", WAV_BYTES)
        content = [
            {"type": "video_url", "video_url": {"url": saved["media_ref"]}},
            {"type": "input_audio", "input_audio": {"data": wav["media_ref"], "format": "wav"}},
        ]
        resolved, unresolved = fm.resolve_media_content_parts(
            _TEST_SESSION, content, vision_enabled=False
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(resolved[0].get("type"), "text")
        self.assertIn("[视频", resolved[0]["text"])
        # 音频不属于视觉范畴：vision=false 仍解析为纯 base64
        self.assertEqual(resolved[1]["type"], "input_audio")
        self.assertEqual(
            base64.b64decode(resolved[1]["input_audio"]["data"]), WAV_BYTES
        )

    def test_default_vision_none_keeps_base64(self):
        saved = fm.save_session_media(_TEST_SESSION, "默认.png", PNG_BYTES)
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(_TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        self.assertTrue(resolved[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_resolve_message_media_refs_passes_vision(self):
        saved = fm.save_session_media(_TEST_SESSION, "消息.png", PNG_BYTES)
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": saved["media_ref"]}},
            ],
        }
        unresolved = fm.resolve_message_media_refs(
            _TEST_SESSION, [message], vision_enabled=False
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(message["content"][1].get("type"), "text")
        self.assertIn(saved["media_ref"], message["content"][1]["text"])


class ExecuteReadMediaVisionTests(unittest.TestCase):
    """execute_read_media：vision_enabled=False 拒绝执行。"""

    def test_vision_false_rejected_with_reason(self):
        result = bt.execute_read_media(
            {"references": ["media://a.png"]},
            _TEST_SESSION, {"media://a.png"}, None,
            load_media=_fake_loader, vision_enabled=False,
        )
        self.assertFalse(result["ok"])
        self.assertIn("不支持视觉", result["error"])
        self.assertEqual(result["loaded"], [])
        self.assertEqual(result["injected_references"], [])

    def test_vision_true_runs_normally(self):
        result = bt.execute_read_media(
            {"references": ["media://a.png"]},
            _TEST_SESSION, {"media://a.png"}, None,
            load_media=_fake_loader, vision_enabled=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["injected_references"], ["media://a.png"])


class SystemPromptVisionBranchTests(unittest.TestCase):
    """系统提示词按当前生效模型视觉能力分叉。

    根因回归：notes 曾对所有模型无条件写「需要重新查看历史图片时用 read_media」，
    vision=false 的模型（且无工具模式）看到该指引后照着模仿文本版工具调用
    （<tool name="read_media">，连参数名都是编的）。修复后 vision=false 分支
    必须明确「read_media 工具不可用、不要尝试读取」。
    """

    def _prompt_with_vision(self, vision_value, include_read_media_note=False):
        import env_manager
        from factory import system_prompt as sp
        original = env_manager.get_default_chat_config
        try:
            if vision_value is None:
                env_manager.get_default_chat_config = lambda: None
            else:
                env_manager.get_default_chat_config = lambda: {"vision": vision_value}
            return sp.build_sys_prompt(include_read_media_note=include_read_media_note)
        finally:
            env_manager.get_default_chat_config = original

    def test_vision_false_hides_read_media_guidance(self):
        prompt = self._prompt_with_vision(False)
        # 整个系统提示词不出现 read_media 字样（伪调用根因）：不支持视觉的
        # 模型连工具名都不应知道，更不能看到「用 read_media 查看图片」的指引
        self.assertNotIn("read_media", prompt)
        # 必须明确告知内容不会回传（仅存档）
        self.assertIn("当前模型不支持视觉", prompt)
        self.assertIn("不会回传给你", prompt)

    def test_vision_true_keeps_read_media_guidance(self):
        # 支持视觉且本轮真实注入 read_media（include_read_media_note=True）：
        # 保留使用指引
        prompt = self._prompt_with_vision(True, include_read_media_note=True)
        self.assertIn("用 read_media", prompt)

    def test_vision_true_without_tool_injection_hides_guidance(self):
        # 支持视觉但本轮未注入 read_media（默认 False）：提示词与请求 tools
        # 必须一致——不出现使用指引，只说明附件不会自动回传
        prompt = self._prompt_with_vision(True)
        self.assertNotIn("用 read_media", prompt)
        self.assertIn("本轮未启用媒体读取工具", prompt)

    def test_config_missing_falls_back_to_vision_true(self):
        # 配置获取失败（None）保守按支持处理：不发误导性禁用提示；
        # 本轮注入了工具时保留使用指引
        prompt = self._prompt_with_vision(None, include_read_media_note=True)
        self.assertIn("用 read_media", prompt)


if __name__ == "__main__":
    unittest.main()
