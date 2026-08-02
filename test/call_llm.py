"""使用当前 .env 选择的 models.json 配置进行手工聊天验证。"""
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from chat.chat_llm import ChatLLM
from config import ChatLLMRequest
from env_manager import init_path, require_default_chat_config


async def main() -> None:
    init_path(str(PROJECT_ROOT))
    chat_config = require_default_chat_config()
    print(
        "使用模型："
        f"{chat_config['selected_provider_name']} / {chat_config['selected_model_name']}"
    )

    request = ChatLLMRequest(messages=[
        {
            "role": "system",
            "content": "你是一个简洁、准确的 AI 助手。",
        },
        {
            "role": "user",
            "content": "你好，请介绍一下你能做什么。",
        },
    ])
    response_stream = ChatLLM.chat_completions(request=request, stream=True)
    async for event in response_stream:
        print(event, end="")


if __name__ == "__main__":
    asyncio.run(main())
