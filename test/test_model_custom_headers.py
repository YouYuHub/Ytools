"""模型角色自定义请求头（model_selection.<role>.headers）单元测试。

覆盖：
- env_manager：headers 规范化（列表/字典形态、非法条目丢弃、同名去重）、
  get_role_headers、inject_custom_headers_into_config、build_model_selection_entry
  继承语义、_normalize_model_selection 携带 headers；
- chat_llm：_build_custom_header_lines 拼接与 CRLF 注入防御；
- 路由层：ChatModelSelection.headers 字段与 select 透传（用 env_manager 侧验证）。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import env_manager
from chat import chat_llm


class NormalizeCustomHeadersTests(unittest.TestCase):
    def test_list_form(self):
        result = env_manager._normalize_custom_headers([
            {"name": "x-foo", "value": "bar"},
            {"name": "x-num", "value": 123},
        ])
        self.assertEqual(result, [
            {"name": "x-foo", "value": "bar"},
            {"name": "x-num", "value": "123"},
        ])

    def test_dict_form(self):
        result = env_manager._normalize_custom_headers({"x-a": "1", "x-b": None})
        self.assertEqual(result, [{"name": "x-a", "value": "1"}, {"name": "x-b", "value": ""}])

    def test_invalid_names_dropped(self):
        result = env_manager._normalize_custom_headers([
            {"name": "", "value": "x"},
            {"name": "bad name", "value": "x"},
            {"name": "bad:name", "value": "x"},
            {"name": "good", "value": "ok"},
            "not-a-dict",
        ])
        self.assertEqual(result, [{"name": "good", "value": "ok"}])

    def test_duplicate_name_last_wins(self):
        result = env_manager._normalize_custom_headers([
            {"name": "x-k", "value": "v1"},
            {"name": "x-k", "value": "v2"},
        ])
        self.assertEqual(result, [{"name": "x-k", "value": "v2"}])

    def test_invalid_input_types(self):
        self.assertEqual(env_manager._normalize_custom_headers(None), [])
        self.assertEqual(env_manager._normalize_custom_headers("str"), [])
        self.assertEqual(env_manager._normalize_custom_headers(123), [])


class RoleHeadersTests(unittest.TestCase):
    def test_get_role_headers_from_selection(self):
        with mock.patch.object(env_manager, "get_model_selection", return_value={
            "title_model": {
                "ownership_name": "P", "model_name": "M", "parameter": {}, "api_type": "chat_completions",
                "headers": [{"name": "x-opencode-session", "value": "abc"}, {"bad": "item"}],
            },
        }):
            headers = env_manager.get_role_headers("title_model")
        self.assertEqual(headers, [{"name": "x-opencode-session", "value": "abc"}])

    def test_get_role_headers_missing_role(self):
        with mock.patch.object(env_manager, "get_model_selection", return_value={}):
            self.assertEqual(env_manager.get_role_headers("compaction_model"), [])

    def test_inject_into_config(self):
        config = {"url": "https://x", "apiKey": "k"}
        merged = env_manager.inject_custom_headers_into_config(
            config, [{"name": "x-s", "value": "sid"}]
        )
        self.assertEqual(merged["_custom_headers"], [{"name": "x-s", "value": "sid"}])
        # 原配置不被污染
        self.assertNotIn("_custom_headers", config)

    def test_inject_empty_keeps_copy(self):
        config = {"url": "https://x"}
        merged = env_manager.inject_custom_headers_into_config(config, [])
        self.assertEqual(merged, config)
        self.assertIsNot(merged, config)

    def test_inject_none_passthrough(self):
        self.assertIsNone(env_manager.inject_custom_headers_into_config(None, [{"name": "x", "value": "y"}]))

    def test_inject_merges_and_dedups(self):
        config = {"_custom_headers": [{"name": "x-a", "value": "old"}]}
        merged = env_manager.inject_custom_headers_into_config(
            config, [{"name": "x-a", "value": "new"}, {"name": "x-b", "value": "v"}]
        )
        self.assertEqual(merged["_custom_headers"], [
            {"name": "x-a", "value": "new"},
            {"name": "x-b", "value": "v"},
        ])


class BuildEntryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_entry_includes_headers(self):
        with mock.patch.object(env_manager, "_resolve_selection_target",
                               return_value=({"selected_model_name": "M"}, "chat_completions")):
            entry = env_manager.build_model_selection_entry(
                "P", "M", "chat_model",
                headers=[{"name": "x-h", "value": "v"}],
                inherit_headers=[{"name": "x-old", "value": "old"}],
            )
        self.assertEqual(entry["headers"], [{"name": "x-h", "value": "v"}])
        self.assertEqual(entry["api_type"], "chat_completions")

    def test_entry_headers_inherit_when_none(self):
        with mock.patch.object(env_manager, "_resolve_selection_target",
                               return_value=({"selected_model_name": "M"}, "chat_completions")):
            entry = env_manager.build_model_selection_entry(
                "P", "M", "chat_model",
                headers=None,
                inherit_headers=[{"name": "x-h", "value": "keep"}],
            )
        self.assertEqual(entry["headers"], [{"name": "x-h", "value": "keep"}])

    def test_entry_empty_headers_clears(self):
        with mock.patch.object(env_manager, "_resolve_selection_target",
                               return_value=({"selected_model_name": "M"}, "chat_completions")):
            entry = env_manager.build_model_selection_entry(
                "P", "M", "chat_model",
                headers=[],
                inherit_headers=[{"name": "x-h", "value": "old"}],
            )
        self.assertEqual(entry["headers"], [])


class NormalizeSelectionHeadersTests(unittest.TestCase):
    def test_selection_carries_headers(self):
        raw = {
            "chat_model": {
                "ownership_name": "P", "model_name": "M",
                "headers": [{"name": "x-s", "value": "sid"}],
            },
            "compaction_model": {
                "ownership_name": "P2", "model_name": "M2",
                "headers": {"x-dict": "form"},
            },
        }
        result = env_manager._normalize_model_selection(raw)
        self.assertEqual(result["chat_model"]["headers"], [{"name": "x-s", "value": "sid"}])
        self.assertEqual(result["compaction_model"]["headers"], [{"name": "x-dict", "value": "form"}])

    def test_default_selection_has_headers_key(self):
        result = env_manager._default_model_selection()
        for entry in result.values():
            self.assertEqual(entry.get("headers"), [])

    def test_legacy_selection_backfills_empty_headers(self):
        result = env_manager._normalize_model_selection({
            "chat_model": {"ownership_name": "P", "model_name": "M"},
        })
        self.assertEqual(result["chat_model"]["headers"], [])


class RequireDefaultChatConfigHeaderInjectionTests(unittest.TestCase):
    def test_chat_config_injects_chat_role_headers(self):
        fake_config = {"url": "https://x", "apiKey": "k", "selected_model_id": "m"}
        with mock.patch.object(env_manager, "_resolve_chat_role_names",
                               return_value=("P", "M")), \
            mock.patch.object(env_manager, "get_model_config", return_value=fake_config), \
            mock.patch.object(env_manager, "get_model_selection", return_value={
                "chat_model": {
                    "ownership_name": "P", "model_name": "M", "parameter": {},
                    "api_type": "chat_completions",
                    "headers": [{"name": "x-sess", "value": "abc"}],
                },
            }):
            config = env_manager.require_default_chat_config()
        # 聊天角色头已注入配置副本（值取自 get_role_headers → ambient 合并结果）
        self.assertEqual(
            [(h["name"], h["value"]) for h in config.get("_custom_headers") or []],
            [("x-sess", "abc")],
        )


class BuildCustomHeaderLinesTests(unittest.TestCase):
    def test_lines_rendered(self):
        config = {"_custom_headers": [
            {"name": "x-opencode-session", "value": "abc123"},
            {"name": "x-sign", "value": "sig"},
        ]}
        lines = chat_llm._build_custom_header_lines(config)
        self.assertIn("x-opencode-session: abc123\r\n", lines)
        self.assertIn("x-sign: sig\r\n", lines)

    def test_crlf_injection_blocked(self):
        config = {"_custom_headers": [{"name": "x-bad\r\nX-Evil", "value": "v"}]}
        self.assertEqual(chat_llm._build_custom_header_lines(config), "")

    def test_invalid_structure(self):
        self.assertEqual(chat_llm._build_custom_header_lines({}), "")
        self.assertEqual(chat_llm._build_custom_header_lines({"_custom_headers": "bad"}), "")
        self.assertEqual(chat_llm._build_custom_header_lines(None), "")

    def test_none_value_becomes_empty(self):
        lines = chat_llm._build_custom_header_lines({"_custom_headers": [{"name": "x-e", "value": None}]})
        self.assertEqual(lines, "x-e: \r\n")


if __name__ == "__main__":
    unittest.main()
