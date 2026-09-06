import json
import tempfile
import unittest
from pathlib import Path

from env_manager import init_path


class ChatConfigModelTests(unittest.IsolatedAsyncioTestCase):
    """为配置路由测试提供独立的临时 .env / models.json，避免污染项目真实配置。

    为了让被测代码中的 `init_path(PROJECT_ROOT)` 在测试期间指向临时目录，
    我们临时把 config.PROJECT_ROOT 替换为临时目录。
    """

    async def asyncSetUp(self) -> None:
        from config import PROJECT_ROOT as original_root
        from routers import chat_config_router
        self.router = chat_config_router

        self._repo_root = original_root
        self._temp_dir = tempfile.TemporaryDirectory()
        self._temp_path = Path(self._temp_dir.name)
        (self._temp_path / "setting").mkdir(parents=True, exist_ok=True)
        (self._temp_path / ".env").write_text(
            "CHAT_OWNERSHIP_NANE=OpenCode Completions\n"
            "CHAT_MODEL_NAME=Deepseek V4 Pro\n",
            encoding="utf-8",
        )
        (self._temp_path / "setting" / "models.json").write_text(
            json.dumps({
                "OpenCode Completions": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-key",
                    "apiType": "chat-completions",
                    "models": {
                        "Deepseek V4 Pro": {
                            "id": "deepseek-v4-pro",
                            "url": "https://example.test/v1",
                            "toolCalling": True,
                            "maxInputTokens": 888888,
                        },
                        "Deepseek V4 Flash": {
                            "id": "deepseek-v4-flash",
                            "url": "https://example.test/v1",
                            "toolCalling": True,
                            "maxInputTokens": 428000,
                        },
                    },
                },
                "OpenCode Message": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-key",
                    "apiType": "messages",
                    "models": {
                        "Qwen3.7 Max": {
                            "id": "qwen3.7-max",
                            "url": "https://example.test/v1/messages",
                            "maxInputTokens": 428000,
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        (self._temp_path / "setting" / "mcp_servers.json").write_text(
            json.dumps({
                "servers": {
                    "pipeIpcMcp": {
                        "type": "stdio",
                        "command": "mcp_server/PipeIpcMCP.exe",
                        "args": [],
                        "version": "1.0.0",
                    },
                    "sysServer": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["mcp_server/sys_tools_server.py"],
                        "version": "1.0.0",
                    },
                },
                "inputs": {"pipeIpcMcp": [], "sysServer": []},
            }),
            encoding="utf-8",
        )

        import config
        config.PROJECT_ROOT = self._temp_path
        # 重新加载 router 模块使其重新读取 PROJECT_ROOT
        import importlib
        importlib.reload(chat_config_router)
        self.router = chat_config_router
        init_path(self._temp_path)

    async def asyncTearDown(self) -> None:
        import config
        config.PROJECT_ROOT = self._repo_root
        # 恢复 project 模块缓存中的 router 引用
        from routers import chat_config_router
        import importlib
        importlib.reload(chat_config_router)
        init_path(self._repo_root)
        self._temp_dir.cleanup()

    async def test_list_chat_models_returns_providers_and_current(self) -> None:
        response = await self.router.list_chat_models()
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["state"], "succeed")
        self.assertIn('"provider_name":"OpenCode Completions"', response.body.decode("utf-8"))
        self.assertEqual(body["role"], "chat_model")
        # 默认 role=chat_model 的角色详情：selection / effective_parameter / available_models
        role_info = body["role_info"]
        self.assertEqual(role_info["role"], "chat_model")
        self.assertEqual(role_info["selection"]["provider"], "OpenCode Completions")
        self.assertEqual(role_info["selection"]["model"], "Deepseek V4 Pro")
        self.assertIn("parameter", role_info["selection"])
        self.assertIn("api_type", role_info["selection"])
        self.assertEqual(role_info["effective_parameter"], {})
        self.assertEqual(role_info["available_count"], 3)
        self.assertEqual(
            {m["api_type"] for m in role_info["available_models"]},
            {"chat-completions", "messages"},
        )

    async def test_list_chat_models_role_compaction_filters_protocol(self) -> None:
        # compaction_model 只列出 chat-completions 协议模型，且带 compaction_status
        response = await self.router.list_chat_models(role="compaction_model")
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["role"], "compaction_model")
        role_info = body["role_info"]
        self.assertEqual(role_info["available_count"], 2)
        self.assertTrue(all(m["api_type"] == "chat-completions" for m in role_info["available_models"]))
        self.assertIn("compaction_status", role_info)
        self.assertTrue(role_info["compaction_status"]["uses_active_chat_model"])

    async def test_list_chat_models_invalid_role_returns_400(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.router.list_chat_models(role="unknown_role")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_select_active_chat_model_updates_model_selection(self) -> None:
        response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Flash",
            )
        )
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('"state":"succeed"', body)
        self.assertIn('"model":"Deepseek V4 Flash"', body)
        self.assertIn('"model_id":"deepseek-v4-flash"', body)
        self.assertIn('"role":"chat_model"', body)

        # models.json 顶层 model_selection.chat_model 应该已被更新（写入临时目录）
        saved = json.loads((self._temp_path / "setting" / "models.json").read_text(encoding="utf-8"))
        chat_selection = saved["model_selection"]["chat_model"]
        self.assertEqual(chat_selection["ownership_name"], "OpenCode Completions")
        self.assertEqual(chat_selection["model_name"], "Deepseek V4 Flash")

        # 内存同步生效
        from env_manager import model_selection, get_role_selection
        self.assertEqual(model_selection["chat_model"]["model_name"], "Deepseek V4 Flash")
        self.assertEqual(get_role_selection("chat_model")["model_name"], "Deepseek V4 Flash")

    async def test_apply_role_parameter_defaults_priority(self) -> None:
        from env_manager import apply_role_parameter_defaults
        await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Flash",
                parameter={"temperature": 0.5, "max_tokens": 65536, "reasoning_effort": "high"},
            )
        )
        # 未显式传参 -> 用 select 配置
        filled = apply_role_parameter_defaults({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(filled["temperature"], 0.5)
        self.assertEqual(filled["max_tokens"], 65536)
        self.assertEqual(filled["reasoning_effort"], "high")
        # 请求体显式传参 -> 优先请求体
        filled = apply_role_parameter_defaults({
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 1.2,
        })
        self.assertEqual(filled["temperature"], 1.2)
        self.assertEqual(filled["max_tokens"], 65536)
        # 配置里没有的字段不填充
        self.assertNotIn("top_p", filled)

    async def test_select_compaction_and_parameter_persisted(self) -> None:
        response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Flash",
                role="compaction_model",
                parameter={"temperature": 0.2, "max_tokens": 2048},
            )
        )
        self.assertEqual(response.status_code, 200)
        saved = json.loads((self._temp_path / "setting" / "models.json").read_text(encoding="utf-8"))
        compaction = saved["model_selection"]["compaction_model"]
        self.assertEqual(compaction["ownership_name"], "OpenCode Completions")
        # parameter 按 api_type 分桶存储（该模型为 chat_completions 协议 -> chat_completions 桶）
        self.assertEqual(compaction["parameter"], {
            "chat_completions": {"temperature": 0.2, "max_tokens": 2048},
        })
        # api_type 为后端推导的只读字段（models.json 的 apiType -> chat_completions），前端不传
        self.assertEqual(compaction["api_type"], "chat_completions")
        # .env 不应再被写入 CHAT_* 选择字段
        env_content = (self._temp_path / ".env").read_text(encoding="utf-8")
        self.assertNotIn("CHAT_MODEL_NAME=Deepseek V4 Flash", env_content)

    async def test_select_unknown_model_returns_400(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.router.select_active_chat_model(
                self.router.ChatModelSelection(
                    provider="OpenCode Completions",
                    model="nonexistent-model",
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_parameter_buckets_per_api_type(self) -> None:
        from env_manager import get_role_parameter
        # messages 协议模型带参数 -> 写入 messages 桶，api_type=messages
        response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Message",
                model="Qwen3.7 Max",
                role="chat_model",
                parameter={"temperature": 0.6, "max_tokens": 4096, "thinking": {"type": "enabled"}},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body.decode("utf-8"))["current"]["api_type"], "messages")
        saved = json.loads((self._temp_path / "setting" / "models.json").read_text(encoding="utf-8"))
        buckets = saved["model_selection"]["chat_model"]["parameter"]
        self.assertEqual(buckets["messages"]["temperature"], 0.6)
        self.assertEqual(buckets["messages"]["max_tokens"], 4096)
        self.assertEqual(buckets["messages"]["thinking"], {"type": "enabled"})
        # 当前协议桶命中
        self.assertEqual(get_role_parameter("chat_model")["temperature"], 0.6)
        # 协议专属字段（max_output_tokens）不在白名单内被丢弃
        self.assertNotIn("max_output_tokens", buckets["messages"])

        # 不传 parameter 切换回 chat-completions 模型 -> 桶全部保留，chat_completions 桶缺失回退
        response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Pro",
                role="chat_model",
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body.decode("utf-8"))["current"]["api_type"], "chat_completions")
        saved = json.loads((self._temp_path / "setting" / "models.json").read_text(encoding="utf-8"))
        buckets = saved["model_selection"]["chat_model"]["parameter"]
        # messages 桶保留
        self.assertIn("messages", buckets)
        # chat_completions 桶未配置 -> 回退 chain 命中 messages 桶
        self.assertEqual(get_role_parameter("chat_model")["temperature"], 0.6)

        # 给 chat_completions 桶配置参数
        await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Pro",
                role="chat_model",
                parameter={"temperature": 0.3},
            )
        )
        self.assertEqual(get_role_parameter("chat_model")["temperature"], 0.3)
        saved = json.loads((self._temp_path / "setting" / "models.json").read_text(encoding="utf-8"))
        buckets = saved["model_selection"]["chat_model"]["parameter"]
        self.assertEqual(buckets["chat_completions"], {"temperature": 0.3})
        self.assertEqual(buckets["messages"]["temperature"], 0.6)

    async def test_parameter_buckets_migrate_flat_legacy_format(self) -> None:
        # 旧版扁平 parameter 加载时自动迁移为 chat_completions 桶
        from env_manager import get_role_parameter, init_path as env_init
        (self._temp_path / "setting" / "models.json").write_text(
            json.dumps({
                "OpenCode Completions": {
                    "vendor": "custom_endpoint",
                    "apiKey": "test-key",
                    "apiType": "chat-completions",
                    "models": {
                        "Deepseek V4 Pro": {
                            "id": "deepseek-v4-pro",
                            "url": "https://example.test/v1",
                            "maxInputTokens": 888888,
                        },
                    },
                },
                "model_selection": {
                    "chat_model": {
                        "ownership_name": "OpenCode Completions",
                        "model_name": "Deepseek V4 Pro",
                        "parameter": {"temperature": 0.5, "max_tokens": 65536},
                        "api_type": "chat_completions",
                    },
                },
            }),
            encoding="utf-8",
        )
        env_init(self._temp_path)
        self.assertEqual(get_role_parameter("chat_model")["temperature"], 0.5)
        self.assertEqual(get_role_parameter("chat_model")["max_tokens"], 65536)
        env_init(self._repo_root)

    async def test_history_compaction_config_uses_unified_trigger_ratio(self) -> None:
        before = await self.router.get_history_compaction_config()
        self.assertEqual(before.status_code, 200)
        before_body = json.loads(before.body.decode("utf-8"))
        self.assertNotIn("round_context_token_ratio", before_body)
        self.assertIn("defaults", before_body)
        self.assertEqual(before_body["defaults"]["keep_rounds"], 20)
        self.assertIn("trigger_ratio", before_body)
        self.assertNotIn("max_summary_blocks", before_body)
        # chunk_rounds 已不再作为配置暴露（内部常量保护单次摘要输入规模）
        self.assertNotIn("chunk_rounds", before_body)
        self.assertNotIn("chunk_rounds", before_body["defaults"])
        self.assertIn("summary_budget_ratio", before_body)
        self.assertIn("compaction_model", before_body)
        self.assertTrue(before_body["compaction_model"]["uses_active_chat_model"])

        response = await self.router.update_history_compaction_config(
            self.router.HistoryCompactionConfig(
                keep_rounds=7,
                trigger_ratio=0.85,
                summary_budget_ratio=0.25,
            )
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertNotIn("round_context_token_ratio", body["config"])
        self.assertEqual(body["config"]["summary_budget_ratio"], 0.25)
        self.assertEqual(body["config"]["keep_rounds"], 7)
        self.assertEqual(body["config"]["trigger_ratio"], 0.85)

        # 压缩模型不写入 .env；压缩策略配置写入 .env 并同步内存
        content = (self._temp_path / ".env").read_text(encoding="utf-8")
        self.assertNotIn("HISTORY_COMPACT_ROUND_CONTEXT_TOKEN_RATIO", content)
        self.assertIn("HISTORY_COMPACT_SUMMARY_BUDGET_RATIO=0.25", content)
        self.assertNotIn("HISTORY_COMPACT_MODEL_PROVIDER", content)
        self.assertNotIn("HISTORY_COMPACT_MODEL_NAME", content)
        self.assertIn("HISTORY_COMPACT_KEEP_ROUNDS=7", content)

        # 压缩模型改由 select 接口配置后，GET 状态应体现
        select_response = await self.router.select_active_chat_model(
            self.router.ChatModelSelection(
                provider="OpenCode Completions",
                model="Deepseek V4 Flash",
                role="compaction_model",
            )
        )
        self.assertEqual(select_response.status_code, 200)
        after = json.loads((await self.router.get_history_compaction_config()).body.decode("utf-8"))
        self.assertEqual(
            after["compaction_model"]["configured"],
            {
                "provider": "OpenCode Completions",
                "model": "Deepseek V4 Flash",
                "api_type": "chat_completions",
                "parameter": {},
            },
        )
        self.assertEqual(
            after["compaction_model"]["effective"]["model_id"],
            "deepseek-v4-flash",
        )


    async def test_tool_selection_get_defaults_and_post_updates(self) -> None:
        # 初始 GET：磁盘 inputs 为空数组占位，补全全部已配置服务
        before = await self.router.get_tool_selection()
        self.assertEqual(before.status_code, 200)
        before_body = json.loads(before.body.decode("utf-8"))
        self.assertEqual(before_body["state"], "succeed")
        self.assertEqual(before_body["inputs"], {"pipeIpcMcp": [], "sysServer": []})
        self.assertEqual(set(before_body["servers"]), {"pipeIpcMcp", "sysServer"})

        # POST：只传一个服务 -> 未提及的 sysServer 补 []（全量替换语义）
        response = await self.router.update_tool_selection(
            self.router.McpToolSelection(inputs={"pipeIpcMcp": ["setup_pipe", "run_pipe_command"]})
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["state"], "succeed")
        self.assertEqual(
            body["inputs"],
            {"pipeIpcMcp": ["setup_pipe", "run_pipe_command"], "sysServer": []},
        )

        # 磁盘 mcp_servers.json 已更新且保留 servers 等其余键
        saved = json.loads((self._temp_path / "setting" / "mcp_servers.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["inputs"]["pipeIpcMcp"], ["setup_pipe", "run_pipe_command"])
        self.assertEqual(saved["inputs"]["sysServer"], [])
        self.assertIn("sysServer", saved["servers"])

        # 内存同步生效
        self.assertEqual(self.router._MCP_TOOL_INPUTS_MEMORY["pipeIpcMcp"], ["setup_pipe", "run_pipe_command"])

        # GET 以磁盘为准：手工编辑文件后刷新页面即可读取到最新值
        saved["inputs"]["pipeIpcMcp"] = ["read_pipe_output"]
        (self._temp_path / "setting" / "mcp_servers.json").write_text(json.dumps(saved), encoding="utf-8")
        after = json.loads((await self.router.get_tool_selection()).body.decode("utf-8"))
        self.assertEqual(after["inputs"]["pipeIpcMcp"], ["read_pipe_output"])

    async def test_tool_selection_validation(self) -> None:
        from fastapi import HTTPException

        # 未知服务名报 400
        with self.assertRaises(HTTPException) as ctx:
            await self.router.update_tool_selection(
                self.router.McpToolSelection(inputs={"ghostServer": ["t"]})
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("ghostServer", ctx.exception.detail)

        # 非法工具条目被规整丢弃（非字符串/空串/重复）
        response = await self.router.update_tool_selection(
            self.router.McpToolSelection(inputs={
                "pipeIpcMcp": ["setup_pipe", "", 123, "setup_pipe"],
                "sysServer": [],
            })
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["inputs"]["pipeIpcMcp"], ["setup_pipe"])

    async def test_tool_selection_builtin_pseudo_server(self) -> None:
        """内置工具伪服务 __builtin__：全局保存时原样保留，未提及时不写入。"""
        # __builtin__ 不按未知服务拒绝，且与 MCP 服务一起保存
        response = await self.router.update_tool_selection(
            self.router.McpToolSelection(inputs={
                "__builtin__": ["ask_user", "todo_write"],
                "pipeIpcMcp": ["setup_pipe"],
            })
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["inputs"]["__builtin__"], ["ask_user", "todo_write"])
        saved = json.loads((self._temp_path / "setting" / "mcp_servers.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["inputs"]["__builtin__"], ["ask_user", "todo_write"])

        # 全量替换语义：未提及 __builtin__ 时从 inputs 中移除（未勾选 = 不写入）
        response = await self.router.update_tool_selection(
            self.router.McpToolSelection(inputs={"sysServer": ["list_dir"]})
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertNotIn("__builtin__", body["inputs"])

        # 只有内置工具、无 MCP 服务条目时也允许保存
        response = await self.router.update_tool_selection(
            self.router.McpToolSelection(inputs={"__builtin__": ["todo_write"]})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body.decode("utf-8"))["inputs"],
            {"__builtin__": ["todo_write"], "pipeIpcMcp": [], "sysServer": []},
        )

    async def test_tool_selection_builtin_session_override(self) -> None:
        """会话级内置工具选择：写入 _meta.tool_selection 并被回退解析原样返回。"""
        from memory.chat_memory import (
            get_chat_memory_manager,
            resolve_session_tool_selection,
        )

        session_id = "builtin_sel_ut"
        manager = await get_chat_memory_manager(session_id)
        try:
            await manager.update_session_tool_selection({
                "__builtin__": ["ask_user"],
                "sysServer": ["list_dir"],
            })
            effective, warning = resolve_session_tool_selection(session_id)
            self.assertIsNone(warning)
            self.assertEqual(effective, {"__builtin__": ["ask_user"], "sysServer": ["list_dir"]})

            # 路由侧回显：is_overridden / session_selection / effective_selection
            response = await self.router.get_tool_selection(session_id=session_id)
            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["is_overridden"])
            self.assertEqual(body["session_selection"], {"__builtin__": ["ask_user"], "sysServer": ["list_dir"]})
            self.assertEqual(body["effective_selection"], {"__builtin__": ["ask_user"], "sysServer": ["list_dir"]})

            # 清除覆盖后回退全局默认（临时 inputs 无 __builtin__ 键）
            await manager.update_session_tool_selection(None)
            effective, warning = resolve_session_tool_selection(session_id)
            self.assertIsNone(warning)
            self.assertNotIn("__builtin__", effective)
        finally:
            manager._file_path.unlink(missing_ok=True)

    async def test_context_return_config_get_defaults_and_post_updates(self) -> None:
        before = await self.router.get_context_return_config()
        self.assertEqual(before.status_code, 200)
        before_body = json.loads(before.body.decode("utf-8"))
        # 默认 -1 = 全部回传
        self.assertEqual(before_body["reasoning_max_length"], -1)
        self.assertEqual(before_body["tool_result_max_length"], -1)

        response = await self.router.update_context_return_config(
            self.router.ContextReturnConfig(
                reasoning_max_length=0,
                tool_result_max_length=-1,
            )
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["config"]["reasoning_max_length"], 0)
        self.assertEqual(body["config"]["tool_result_max_length"], -1)
        self.assertEqual(body["memory_state"]["REASONING_RETURN_MAX_LENGTH"], "0")
        self.assertEqual(body["memory_state"]["HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH"], "-1")

        content = (self._temp_path / ".env").read_text(encoding="utf-8")
        self.assertIn("REASONING_RETURN_MAX_LENGTH=0", content)
        self.assertIn("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH=-1", content)

        # 读取时内存与磁盘一致（set_env_vars 同步 env_vars）
        from env_manager import env_vars
        self.assertEqual(env_vars.get("REASONING_RETURN_MAX_LENGTH"), "0")
        self.assertEqual(env_vars.get("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH"), "-1")

    async def test_mcp_tool_timeout_config_get_and_post(self) -> None:
        before = await self.router.get_mcp_tool_config()
        before_body = json.loads(before.body.decode("utf-8"))
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before_body["call_timeout_seconds"], 300)
        self.assertEqual(before_body["env_names"]["call_timeout_seconds"], "MCP_TOOL_CALL_TIMEOUT_SECONDS")

        response = await self.router.update_mcp_tool_config(
            self.router.McpToolConfig(call_timeout_seconds=12)
        )
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["config"]["call_timeout_seconds"], 12)
        self.assertEqual(body["memory_state"], "12.0")
        self.assertIn("MCP_TOOL_CALL_TIMEOUT_SECONDS=12.0", (self._temp_path / ".env").read_text(encoding="utf-8"))

        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.router.update_mcp_tool_config(
                self.router.McpToolConfig(call_timeout_seconds=-1)
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
