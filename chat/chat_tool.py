# from email.mime import base
# from tkinter.filedialog import Open
import requests
import json
import aiohttp
import asyncio
from typing import List, Dict, Generator, AsyncGenerator, Any, Optional
from dataclasses import dataclass, field
from openai import OpenAI, AsyncOpenAI
try:
    from load_env import load_var
except ImportError:
    load_var = None

# 导入配置模型
from config import ChatToolRequest


@dataclass
class StreamChunk:
    """流式响应的数据块"""
    content: Optional[str] = None  # 正常回复内容
    reasoning_content: Optional[str] = None  # 推理过程内容
    tool_calls: Optional[List[Dict[str, Any]]] = None  # 工具调用列表
    
    def __bool__(self):
        """判断是否有有效内容"""
        return any([
            self.content is not None and self.content != "",
            self.reasoning_content is not None and self.reasoning_content != "",
            self.tool_calls is not None and len(self.tool_calls) > 0
        ])


# openai_client = OpenAI(
#     base_url=load_var("TOOL_CHAT_URL", "http://172.23.144.63:10001/v1") if load_var else "http://172.23.144.63:10001/v1",
#     api_key=load_var("TOOL_API_KEY", "not-needed") if load_var else "not-needed",  # 如果是本地模型通常不需要 API key
# )

# openai_async_client = AsyncOpenAI(
#     base_url=load_var("TOOL_CHAT_URL", "http://172.23.144.63:10001/v1") if load_var else "http://172.23.144.63:10001/v1",
#     api_key=load_var("TOOL_API_KEY", "not-needed") if load_var else "not-needed",  # 如果是本地模型通常不需要 API key
# )


class ChatTool:
    """通义千问聊天客户端，支持多轮对话和流式输出"""

    def __init__(self,
        api_url: str = load_var("TOOL_CHAT_URL", default="http://172.23.144.63:10001/v1/chat/completions") if load_var else "http://172.23.144.63:10001/v1/chat/completions",
        model_name: str = load_var("TOOL_CHAT_MODEL", "Qwen3.5-2B") if load_var else "Qwen3.5-2B",
        max_turns: int = 10):
        """
        初始化聊天客户端
        参数:
            api_url: API端点地址
            model_name: 模型名称
            max_turns: 最大对话轮次限制（默认 10 轮）
        """
        self.api_url = api_url
        self.model_name = model_name
        self.max_turns = max_turns
        self.conversation_history: List[Dict[str, str]] = []
        self.abort_controller = None
    
    @staticmethod
    def _should_include_reasoning_effort(model_name: str) -> bool:
        """
        判断是否应该在请求中包含 reasoning_effort 参数
        该参数仅适用于 OpenAI o系列、GPT-5系列和部分DeepSeek模型
        参数:
            model_name: 模型名称
        返回:
            True 如果应该包含 reasoning_effort 参数，否则 False
        """
        if not model_name:
            return False
        model_lower = model_name.lower()
        # GPT-5系列（所有以 gpt-5 开头的模型都支持）
        if model_lower.startswith("gpt-5"):
            return True
        # o系列（所有以 o 开头的模型都支持，如 o1, o3, o4-mini 等）
        if model_lower.startswith("gpt-o"):
            return True
        # DeepSeek 支持 reasoning_effort 的模型
        deepseek_reasoning_models = [
            "deepseek-reasoner",
            "deepseek-chat",
            # "deepseek-coder"
        ]
        if any(dm in model_lower for dm in deepseek_reasoning_models):
            return True
        # # 开源 GPT-OSS 模型
        # if "gpt-oss" in model_lower:
        #     return True
        return False

    def chat(self, user_input: str, stream: bool = True, 
             request: ChatToolRequest = None) -> Generator[StreamChunk, None, None]:
        """
        发送消息并获取回复
        参数:
            user_input: 用户输入的消息
            stream: 是否使用流式输出
            request: ChatToolRequest 对象，包含所有配置参数
        返回:
            生成器的形式返回 StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        # 检查是否超过最大对话轮次
        # 一轮对话包括一条用户消息和一条助手消息
        if len(self.conversation_history) >= self.max_turns * 2:
            # 移除最早的对话，保留最近的 max_turns-1 轮
            remove_count = len(self.conversation_history) - (self.max_turns - 1) * 2
            self.conversation_history = self.conversation_history[remove_count:]
        # 添加用户消息到历史记录
        self.conversation_history.append({
            "role": "user",
            "content": user_input
        })
        # 构建请求数据
        payload = {
            "model": self.model_name,
            "messages": self.conversation_history,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "presence_penalty": presence_penalty
        }
        # 仅在支持的模型中添加 reasoning_effort 参数
        if reasoning_effort and ChatTool._should_include_reasoning_effort(self.model_name):
            payload["reasoning_effort"] = reasoning_effort
        # 添加工具相关参数
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        # 添加额外参数
        if extra_body is not None:
            payload.update(extra_body)
        headers = {
            "Content-Type": "application/json"
        }
        try:
            if stream:
                # 流式请求
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=60
                )
                response.raise_for_status()
                full_response = ""
                full_reasoning = ""
                accumulated_tool_calls = []
                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data: '):
                            data_str = decoded_line[6:]
                            if data_str.strip() == '[DONE]':
                                break
                            try:
                                data = json.loads(data_str)
                                if 'choices' in data and len(data['choices']) > 0:
                                    delta = data['choices'][0].get('delta', {})
                                    # 提取 content
                                    content = delta.get('content')
                                    # 提取 reasoning_content
                                    reasoning_content = delta.get('reasoning_content')
                                    # 提取 tool_calls
                                    tool_calls_delta = delta.get('tool_calls')
                                    # 创建 StreamChunk 对象
                                    chunk = StreamChunk(
                                        content=content,
                                        reasoning_content=reasoning_content,
                                        tool_calls=tool_calls_delta
                                    )
                                    # 累积内容用于历史记录
                                    if content:
                                        full_response += content
                                    if reasoning_content:
                                        full_reasoning += reasoning_content
                                    if tool_calls_delta:
                                        # 累积工具调用
                                        def _get_tool_call_slot(tc: dict) -> dict:
                                            tc_id = tc.get('id')
                                            idx = tc.get('index')
                                            if tc_id:
                                                for existing in accumulated_tool_calls:
                                                    if existing.get('id') == tc_id:
                                                        return existing
                                            if isinstance(idx, int) and idx >= 0:
                                                if idx >= len(accumulated_tool_calls):
                                                    accumulated_tool_calls.extend([{}] * (idx - len(accumulated_tool_calls) + 1))
                                                existing = accumulated_tool_calls[idx]
                                                if not isinstance(existing, dict):
                                                    existing = {}
                                                    accumulated_tool_calls[idx] = existing
                                                existing_id = existing.get('id')
                                                if existing_id and tc_id and existing_id != tc_id:
                                                    accumulated_tool_calls.append({})
                                                    return accumulated_tool_calls[-1]
                                                if tc_id and existing_id is None:
                                                    existing['id'] = tc_id
                                                return existing
                                            if tc_id is None:
                                                tool_name = tc.get('function', {}).get('name')
                                                for existing in accumulated_tool_calls:
                                                    if existing.get('id') is None and existing.get('function', {}).get('name') == tool_name:
                                                        return existing
                                            accumulated_tool_calls.append({})
                                            return accumulated_tool_calls[-1]
                                        for tc in tool_calls_delta:
                                            existing = _get_tool_call_slot(tc)
                                            for key, value in tc.items():
                                                if value is None:
                                                    continue
                                                if key == 'function':
                                                    if 'function' not in existing:
                                                        existing['function'] = {}
                                                    for fk, fv in value.items():
                                                        if fv is None:
                                                            continue
                                                        if fk == 'arguments':
                                                            prev_args = existing['function'].get('arguments')
                                                            if isinstance(fv, str) and isinstance(prev_args, str):
                                                                existing['function'][fk] = prev_args + fv
                                                            elif isinstance(fv, dict) and isinstance(prev_args, dict):
                                                                prev_args.update(fv)
                                                                existing['function'][fk] = prev_args
                                                            else:
                                                                existing['function'][fk] = fv
                                                        else:
                                                            if isinstance(existing['function'].get(fk), str) and isinstance(fv, str):
                                                                existing['function'][fk] = existing['function'][fk] + fv
                                                            else:
                                                                existing['function'][fk] = fv
                                                else:
                                                    existing[key] = value
                                    # 只 yield 有内容的 chunk
                                    if chunk:
                                        yield chunk
                            except json.JSONDecodeError:
                                continue
                # 将助手的完整回复添加到历史记录
                if full_response or full_reasoning or accumulated_tool_calls:
                    history_message = {
                        "role": "assistant",
                        "content": full_response if full_response else None
                    }
                    if full_reasoning:
                        history_message["reasoning_content"] = full_reasoning
                    if accumulated_tool_calls:
                        history_message["tool_calls"] = accumulated_tool_calls
                    self.conversation_history.append(history_message)
            else:
                # 非流式请求
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()
                if 'choices' in data and len(data['choices']) > 0:
                    message = data['choices'][0]['message']
                    content = message.get('content')
                    reasoning_content = message.get('reasoning_content')
                    tool_calls = message.get('tool_calls')
                    # 创建 StreamChunk 对象
                    chunk = StreamChunk(
                        content=content,
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls
                    )
                    # 将助手的回复添加到历史记录
                    history_message = {
                        "role": "assistant",
                        "content": content
                    }
                    if reasoning_content:
                        history_message["reasoning_content"] = reasoning_content
                    if tool_calls:
                        history_message["tool_calls"] = tool_calls
                    self.conversation_history.append(history_message)
                    yield chunk
        except requests.exceptions.Timeout:
            raise Exception("请求超时，请检查网络连接")
        except requests.exceptions.ConnectionError:
            raise Exception("连接失败，请检查服务是否运行")
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")

    @staticmethod
    def static_chat(user_input: str, request: ChatToolRequest = None) -> Generator[StreamChunk, None, None]:
        """
        发送消息并获取回复（同步版本），使用 openai 库
        参数:
            user_input: 用户输入的消息
            request: ChatToolRequest 对象，包含所有配置参数
        返回:
            生成器的形式返回 StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        model_name = request.model
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        try:
            # 构建基础参数
            kwargs = {
                "model": model_name,
                "messages": [{
                    "role": "user",
                    "content": user_input
                }],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "presence_penalty": presence_penalty
            }
            # 仅在支持的模型中添加 reasoning_effort 参数
            if reasoning_effort and ChatTool._should_include_reasoning_effort(model_name):
                kwargs["reasoning_effort"] = reasoning_effort
            # 添加工具相关参数
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
            # 添加额外参数
            if extra_body is not None:
                kwargs.update(extra_body)
            # 创建 OpenAi 对象
            openai_client = OpenAI(
                base_url=load_var("TOOL_CHAT_URL", "") if load_var else "",
                api_key=load_var("TOOL_API_KEY", "") if load_var else "",
            )
            response = openai_client.chat.completions.create(**kwargs)
            for chunk in response:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    # 提取各个字段
                    content = delta.content
                    reasoning_content = getattr(delta, 'reasoning_content', None)
                    tool_calls = None
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        # 转换为字典格式
                        tool_calls = []
                        for tc in delta.tool_calls:
                            tc_dict = {
                                "index": tc.index if hasattr(tc, 'index') else None,
                                "id": tc.id if hasattr(tc, 'id') else None,
                                "type": tc.type if hasattr(tc, 'type') else "function"
                            }
                            if hasattr(tc, 'function'):
                                tc_dict["function"] = {
                                    "name": tc.function.name if hasattr(tc.function, 'name') else None,
                                    "arguments": tc.function.arguments if hasattr(tc.function, 'arguments') else None
                                }
                            tool_calls.append(tc_dict)
                    # 创建并 yield StreamChunk
                    stream_chunk = StreamChunk(
                        content=content,
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls
                    )
                    if stream_chunk:
                        yield stream_chunk
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")
    
    async def chat_async(self, user_input: str, stream: bool = True, 
                         request: ChatToolRequest = None) -> AsyncGenerator[StreamChunk, None]:
        """
        发送消息并获取回复（异步版本）
        参数:
            user_input: 用户输入的消息
            stream: 是否使用流式输出
            request: ChatToolRequest 对象，包含所有配置参数
        返回:
            异步生成器的形式返回 StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        # 检查是否超过最大对话轮次
        # 一轮对话包括一条用户消息和一条助手消息
        if len(self.conversation_history) >= self.max_turns * 2:
            # 移除最早的对话，保留最近的 max_turns-1 轮
            remove_count = len(self.conversation_history) - (self.max_turns - 1) * 2
            self.conversation_history = self.conversation_history[remove_count:]
        # 添加用户消息到历史记录
        self.conversation_history.append({
            "role": "user",
            "content": user_input
        })
        # 构建请求数据
        payload = {
            "model": self.model_name,
            "messages": self.conversation_history,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "presence_penalty": presence_penalty
        }
        # 仅在支持的模型中添加 reasoning_effort 参数
        if reasoning_effort and ChatTool._should_include_reasoning_effort(self.model_name):
            payload["reasoning_effort"] = reasoning_effort
        # 添加工具相关参数
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        # 添加额外参数
        if extra_body is not None:
            payload.update(extra_body)
        headers = {
            "Content-Type": "application/json"
        }
        try:
            if stream:
                # 异步流式请求
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.api_url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as response:
                        response.raise_for_status()
                        full_response = ""
                        full_reasoning = ""
                        accumulated_tool_calls = []
                        async for line in response.content:
                            if line:
                                decoded_line = line.decode('utf-8').strip()
                                if decoded_line.startswith('data: '):
                                    data_str = decoded_line[6:]
                                    if data_str.strip() == '[DONE]':
                                        break
                                    try:
                                        data = json.loads(data_str)
                                        if 'choices' in data and len(data['choices']) > 0:
                                            delta = data['choices'][0].get('delta', {})
                                            # 提取各个字段
                                            content = delta.get('content')
                                            reasoning_content = delta.get('reasoning_content')
                                            tool_calls_delta = delta.get('tool_calls')
                                            # 创建 StreamChunk 对象
                                            chunk = StreamChunk(
                                                content=content,
                                                reasoning_content=reasoning_content,
                                                tool_calls=tool_calls_delta
                                            )
                                            # 累积内容用于历史记录
                                            if content:
                                                full_response += content
                                            if reasoning_content:
                                                full_reasoning += reasoning_content
                                            if tool_calls_delta:
                                                # 累积工具调用
                                                for tc in tool_calls_delta:
                                                    if tc.get('index') is not None:
                                                        idx = tc['index']
                                                        if idx >= len(accumulated_tool_calls):
                                                            accumulated_tool_calls.extend([{}] * (idx - len(accumulated_tool_calls) + 1))
                                                        # 合并工具调用信息
                                                        existing = accumulated_tool_calls[idx]
                                                        for key, value in tc.items():
                                                            if key == 'function':
                                                                if 'function' not in existing:
                                                                    existing['function'] = {}
                                                                for fk, fv in value.items():
                                                                    if fv is not None:
                                                                        existing['function'][fk] = existing['function'].get(fk, '') + fv
                                                            elif value is not None:
                                                                existing[key] = value
                                            # 只 yield 有内容的 chunk
                                            if chunk:
                                                yield chunk
                                    except json.JSONDecodeError:
                                        continue
                        # 将助手的完整回复添加到历史记录
                        if full_response or full_reasoning or accumulated_tool_calls:
                            history_message = {
                                "role": "assistant",
                                "content": full_response if full_response else None
                            }
                            if full_reasoning:
                                history_message["reasoning_content"] = full_reasoning
                            if accumulated_tool_calls:
                                history_message["tool_calls"] = accumulated_tool_calls
                            self.conversation_history.append(history_message)
            else:
                # 异步非流式请求
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.api_url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as response:
                        response.raise_for_status()
                        data = await response.json()
                        if 'choices' in data and len(data['choices']) > 0:
                            message = data['choices'][0]['message']
                            content = message.get('content')
                            reasoning_content = message.get('reasoning_content')
                            tool_calls = message.get('tool_calls')
                            # 创建 StreamChunk 对象
                            chunk = StreamChunk(
                                content=content,
                                reasoning_content=reasoning_content,
                                tool_calls=tool_calls
                            )
                            # 将助手的回复添加到历史记录
                            history_message = {
                                "role": "assistant",
                                "content": content
                            }
                            if reasoning_content:
                                history_message["reasoning_content"] = reasoning_content
                            if tool_calls:
                                history_message["tool_calls"] = tool_calls
                            self.conversation_history.append(history_message)
                            yield chunk
        except asyncio.TimeoutError:
            raise Exception("请求超时，请检查网络连接")
        except aiohttp.ClientConnectionError:
            raise Exception("连接失败，请检查服务是否运行")
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")
    
    @staticmethod
    def chat_with_history(request: ChatToolRequest = None) -> Generator[StreamChunk, None, None]:
        """
        使用给定的对话历史进行聊天（同步版本）
        前端传递历史记录列表，该方法将历史记录发送给模型并流式输出
        参数:
            request: ChatToolRequest 对象，包含所有配置参数（包括 messages）
        返回:
            生成器形式返回 StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        model_name = request.model
        messages = request.messages
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        try:
            # 构建基础参数
            kwargs = {
                "model": model_name,
                "messages": [msg.model_dump() if hasattr(msg, 'model_dump') else msg for msg in messages],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "presence_penalty": presence_penalty
            }
            # 仅在支持的模型中添加 reasoning_effort 参数
            if reasoning_effort and ChatTool._should_include_reasoning_effort(model_name):
                kwargs["reasoning_effort"] = reasoning_effort
            # 添加工具相关参数
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
            # 添加额外参数
            if extra_body is not None:
                kwargs.update(extra_body)
            # 创建 OpenAi 对象
            openai_client = OpenAI(
                base_url=load_var("TOOL_CHAT_URL", "") if load_var else "",
                api_key=load_var("TOOL_API_KEY", "") if load_var else "",
            )
            response = openai_client.chat.completions.create(**kwargs)
            for chunk in response:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    # 提取各个字段
                    content = delta.content
                    reasoning_content = getattr(delta, 'reasoning_content', None)
                    tool_calls = None
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        # 转换为字典格式
                        tool_calls = []
                        for tc in delta.tool_calls:
                            tc_dict = {
                                "index": tc.index if hasattr(tc, 'index') else None,
                                "id": tc.id if hasattr(tc, 'id') else None,
                                "type": tc.type if hasattr(tc, 'type') else "function"
                            }
                            if hasattr(tc, 'function'):
                                tc_dict["function"] = {
                                    "name": tc.function.name if hasattr(tc.function, 'name') else None,
                                    "arguments": tc.function.arguments if hasattr(tc.function, 'arguments') else None
                                }
                            tool_calls.append(tc_dict)
                    # 创建并 yield StreamChunk
                    stream_chunk = StreamChunk(
                        content=content,
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls
                    )
                    if stream_chunk:
                        yield stream_chunk
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")

    @staticmethod
    async def chat_with_history_async(request: ChatToolRequest = None) -> AsyncGenerator[StreamChunk, None]:
        """
        使用给定的对话历史进行聊天（异步版本）
        前端传递历史记录列表，该方法将历史记录发送给模型并流式输出
        参数:
            request: ChatToolRequest 对象，包含所有配置参数（包括 messages）
        返回:
            异步生成器形式返回 StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        model_name = request.model
        messages = request.messages
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        try:
            # 构建基础参数
            kwargs = {
                "model": model_name,
                "messages": [msg.model_dump() if hasattr(msg, 'model_dump') else msg for msg in messages],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "presence_penalty": presence_penalty
            }
            # 仅在支持的模型中添加 reasoning_effort 参数
            if reasoning_effort and ChatTool._should_include_reasoning_effort(model_name):
                kwargs["reasoning_effort"] = reasoning_effort
            # 添加工具相关参数
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
            # 添加额外参数
            if extra_body is not None:
                kwargs.update(extra_body)
            # 创建 AsyncOpenAi 对象
            openai_async_client = AsyncOpenAI(
                base_url=load_var("TOOL_CHAT_URL", "") if load_var else "",
                api_key=load_var("TOOL_API_KEY", "") if load_var else "",
            )
            response = await openai_async_client.chat.completions.create(**kwargs)
            async for chunk in response:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    # 提取各个字段
                    content = delta.content
                    reasoning_content = getattr(delta, 'reasoning_content', None)
                    tool_calls = None
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        # 转换为字典格式
                        tool_calls = []
                        for tc in delta.tool_calls:
                            tc_dict = {
                                "index": tc.index if hasattr(tc, 'index') else None,
                                "id": tc.id if hasattr(tc, 'id') else None,
                                "type": tc.type if hasattr(tc, 'type') else "function"
                            }
                            if hasattr(tc, 'function'):
                                tc_dict["function"] = {
                                    "name": tc.function.name if hasattr(tc.function, 'name') else None,
                                    "arguments": tc.function.arguments if hasattr(tc.function, 'arguments') else None
                                }
                            tool_calls.append(tc_dict)
                    # 创建并 yield StreamChunk
                    stream_chunk = StreamChunk(
                        content=content,
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls
                    )
                    if stream_chunk:
                        yield stream_chunk
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")
    
    @staticmethod
    async def chat_with_history_sse(
        request: ChatToolRequest = None,
        api_url: str | None = None
    ) -> AsyncGenerator[str, None]:
        """
        使用给定的对话历史进行聊天，返回 SSE 格式的流式数据（异步版本）
        专为 FastAPI 的 StreamingResponse 设计，直接配合 POST 接口使用
        参数:
            request: ChatToolRequest 对象，包含所有配置参数（包括 messages）
            api_url: API 地址，可选，不传则使用默认配置（此参数不在 ChatToolRequest 中）
        返回:
            异步生成器形式返回 SSE 格式数据，每条数据格式为：data: {"content": "...", "reasoning_content": "...", "tool_calls": [...]}\\n\\n
            结束时返回：data: [DONE]\\n\\n
            错误时返回：data: {"error": "错误信息"}\\n\\n
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        model_name = request.model
        messages = request.messages
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        try:
            # 根据是否提供 api_url 决定是否创建新的客户端
            if api_url:
                # 使用自定义 API 地址创建临时客户端
                temp_client = AsyncOpenAI(
                    base_url=api_url,
                    api_key="not-needed"
                )
                client_to_use = temp_client
            else:
                # 使用全局默认客户端
                client_to_use = AsyncOpenAI(
                    base_url=load_var("TOOL_CHAT_URL", "") if load_var else "",
                    api_key=load_var("TOOL_API_KEY", "") if load_var else "",
                )
            model_name = model_name if model_name else load_var("TOOL_CHAT_MODEL", "Qwen3.5-2B") if load_var else "Qwen3.5-2B"
            # 构建基础参数
            kwargs = {
                "model": model_name,
                "messages": [msg.model_dump() if hasattr(msg, 'model_dump') else msg for msg in messages],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "presence_penalty": presence_penalty
            }
            # 仅在支持的模型中添加 reasoning_effort 参数
            if reasoning_effort and ChatTool._should_include_reasoning_effort(model_name):
                kwargs["reasoning_effort"] = reasoning_effort
            # 添加工具相关参数
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
            # 添加额外参数
            if extra_body is not None and extra_body != {}:
                print(f"extra_body: {extra_body}")
                kwargs["extra_body"] = extra_body
            # 调试参数
            # print(f"kwargs: {kwargs}")
            response = await client_to_use.chat.completions.create(**kwargs)
            async for chunk in response:
                # print(f"chunk: {chunk.model_dump_json()}")  # 调试
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    # 提取各个字段
                    content = delta.content
                    reasoning_content = getattr(delta, 'reasoning_content', None)
                    tool_calls = None
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        # 转换为字典格式
                        tool_calls = []
                        for tc in delta.tool_calls:
                            tc_dict = {
                                "index": tc.index if hasattr(tc, 'index') else None,
                                "id": tc.id if hasattr(tc, 'id') else None,
                                "type": tc.type if hasattr(tc, 'type') else "function"
                            }
                            if hasattr(tc, 'function'):
                                tc_dict["function"] = {
                                    "name": tc.function.name if hasattr(tc.function, 'name') else None,
                                    "arguments": tc.function.arguments if hasattr(tc.function, 'arguments') else None
                                }
                            tool_calls.append(tc_dict)
                    # 只发送有内容的 chunk
                    if content or reasoning_content or tool_calls:
                        sse_data_dict = {}
                        if content is not None:
                            sse_data_dict["content"] = content
                        if reasoning_content is not None:
                            sse_data_dict["reasoning_content"] = reasoning_content
                        if tool_calls is not None:
                            sse_data_dict["tool_calls"] = tool_calls
                        # SSE 格式：data: {...}\n\n
                        sse_data = f"data: {json.dumps(sse_data_dict, ensure_ascii=False)}\n\n"
                        yield sse_data
                await asyncio.sleep(0.00001)  # 让出执行权，避免阻塞事件循环
            # 发送结束标记
            yield "data: [DONE]\n\n"
        except Exception as e:
            # 错误也以 SSE 格式发送
            error_data = f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield error_data
            yield "data: [DONE]\n\n"
    
    @staticmethod
    def chat_with_history_non_stream(
        request: ChatToolRequest = None,
        api_url: str | None = None
    ) -> StreamChunk:
        """
        使用给定的对话历史进行聊天，返回完整响应内容（同步非流式版本）
        基于 OpenAI 库实现，遵循 OpenAI Chat Completion API 规范
        参数:
            request: ChatToolRequest 对象，包含所有配置参数（包括 messages）
            api_url: API 地址，可选，不传则使用默认配置（此参数不在 ChatToolRequest 中）
        返回:
            StreamChunk 对象，包含 content、reasoning_content、tool_calls 字段
        """
        # 从 request 对象中提取参数
        if request is None:
            raise ValueError("request 参数不能为 None")
        model_name = request.model
        messages = request.messages
        tools = request.tools
        tool_choice = request.tool_choice
        extra_body = request.extra_body
        max_tokens = request.max_tokens
        temperature = request.temperature
        top_p = request.top_p
        reasoning_effort = request.reasoning_effort
        presence_penalty = request.presence_penalty
        parallel_tool_calls = request.parallel_tool_calls
        try:
            # 根据是否提供 api_url 决定是否创建新的客户端
            if api_url:
                # 使用自定义 API 地址创建临时客户端
                temp_client = OpenAI(
                    base_url=api_url,
                    api_key="not-needed"
                )
                client_to_use = temp_client
            else:
                # 使用全局默认客户端
                client_to_use = OpenAI(
                    base_url=load_var("TOOL_API_URL", "") if load_var else "",
                    api_key=load_var("TOOL_API_KEY", "") if load_var else "",
                )
            # 确保模型名称不 None
            model_name = model_name if model_name else load_var("TOOL_CHAT_MODEL", "Qwen3.5-2B") if load_var else "Qwen3.5-2B"
            # 构建基础参数
            kwargs = {
                "model": model_name,
                "messages": [msg.model_dump() if hasattr(msg, 'model_dump') else msg for msg in messages],
                "stream": False,  # 非流式模式
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "presence_penalty": presence_penalty
            }
            # 仅在支持的模型中添加 reasoning_effort 参数
            if reasoning_effort and ChatTool._should_include_reasoning_effort(model_name):
                kwargs["reasoning_effort"] = reasoning_effort
            # 添加工具相关参数
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
            # 添加额外参数
            if extra_body is not None:
                print(f"extra_body: {extra_body}")
                kwargs["extra_body"] = extra_body
            # 调试输出参数
            # print(f"[DEBUG] url: {load_var("TOOL_CHAT_URL", "http://172.23.144.63:10001/v1/chat/completions") if load_var else "http://172.23.144.63:10001/v1"}， model: {load_var("TOOL_CHAT_MODEL", "Qwen3.5-2B") if load_var else "Qwen3.5-2B"}")
            # print(f"[DEBUG] 请求参数: {kwargs}")
            response = client_to_use.chat.completions.create(**kwargs)
            # print(f"[DEBUG] 响应: {response.model_dump_json()}")
            if response.choices and len(response.choices) > 0:
                message = response.choices[0].message
                # 提取各个字段
                content = message.content
                reasoning_content = getattr(message, 'reasoning_content', None)
                tool_calls = None
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    # 转换为字典格式
                    tool_calls = []
                    for tc in message.tool_calls:
                        tc_dict = {
                            "id": tc.id if hasattr(tc, 'id') else None,
                            "type": tc.type if hasattr(tc, 'type') else "function"
                        }
                        if hasattr(tc, 'function'):
                            tc_dict["function"] = {
                                "name": tc.function.name if hasattr(tc.function, 'name') else None,
                                "arguments": tc.function.arguments if hasattr(tc.function, 'arguments') else None
                            }
                        tool_calls.append(tc_dict)
                # 创建并返回 StreamChunk 对象
                return StreamChunk(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls
                )
            else:
                return StreamChunk()
        except Exception as e:
            raise Exception(f"请求失败：{str(e)}")
    
    def clear_history(self):
        """清空对话历史"""
        self.conversation_history = []
    
    def get_history(self) -> List[Dict[str, str]]:
        """
        获取对话历史
        返回:
            对话历史记录列表
        """
        return self.conversation_history.copy()



if __name__ == "__main__":
    # 测试 _should_include_reasoning_effort 方法
    print("=" * 60)
    print("测试 reasoning_effort 参数智能处理")
    print("=" * 60)
    
    test_models = [
        # GPT-5系列模型（应该返回True）
        ("gpt-5", True),
        ("gpt-5-mini", True),
        ("gpt-5-nano", True),
        ("gpt-5.1", True),
        ("gpt-5.2", True),
        ("gpt-5.4", True),
        
        # OpenAI o系列模型（应该返回True）
        ("o3", True),
        ("o3-mini", True),
        ("o4-mini", True),
        
        # DeepSeek模型（应该返回True）
        ("deepseek-reasoner", True),
        ("deepseek-chat", True),
        
        # GPT-OSS开源模型（应该返回True）
        ("gpt-oss", True),
        
        # 不支持的模型（应该返回False）
        ("gpt-4o", False),          # GPT-4系列不支持
        ("gpt-4o-mini", False),     # GPT-4系列不支持
        ("gpt-4", False),           # GPT-4系列不支持
        ("gpt-3.5-turbo", False),   # GPT-3.5系列不支持
        ("Qwen3.5-2B", False),
        ("qwen-turbo", False),
        ("claude-3-opus", False),
        ("llama-3-70b", False),
        ("glm-4", False),
        ("", False),
        (None, False),
    ]
    
    print("\n测试结果：")
    print("-" * 60)
    all_passed = True
    for model_name, expected in test_models:
        result = ChatTool._should_include_reasoning_effort(model_name)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_passed = False
        print(f"{status} 模型: {model_name:25s} | 期望: {expected:5} | 实际: {result:5}")
    
    print("-" * 60)
    if all_passed:
        print("✅ 所有测试通过！")
    else:
        print("❌ 存在测试失败！")
    print("=" * 60)
    print()
    
    def test_structured_streaming():
        """测试结构化流式输出"""
        print("=" * 60)
        print("测试结构化流式输出 - StreamChunk")
        print("=" * 60)
        print()
        # 测试 1: 基础流式输出（带思考）
        print("场景 1: 启用思考模式的流式输出")
        print("-" * 60)
        try:
            for chunk in ChatTool.static_chat(
                user_input="当前时间是什么？",
                extra_body={"enable_thinking": True}
            ):
                if chunk.reasoning_content:
                    print(f"[思考] {chunk.reasoning_content}", end="", flush=True)
                if chunk.content:
                    print(chunk.content, end="", flush=True)
                if chunk.tool_calls:
                    print(f"\n[工具调用] {chunk.tool_calls}")
            print("\n")
        except Exception as e:
            print(f"\n测试失败：{str(e)}")
        print("\n" + "=" * 60)
        # 测试 2: 工具调用
        print("场景 2: 工具调用测试")
        print("-" * 60)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "format_current_time",
                    "description": "获取当前时间并按指定格式返回",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "format_str": {
                                "type": "string",
                                "description": "时间格式化字符串，如 '%Y-%m-%d %H:%M:%S'"
                            }
                        },
                        "required": []
                    }
                }
            }
        ]
        try:
            for chunk in ChatTool.static_chat(
                user_input="请告诉我当前的系统时间",
                tools=tools,
                tool_choice="auto",
                extra_body={"enable_thinking": True}
            ):
                if chunk.reasoning_content:
                    print(f"[思考] {chunk.reasoning_content}", end="", flush=True)
                if chunk.content:
                    print(chunk.content, end="", flush=True)
                if chunk.tool_calls:
                    print(f"\n[工具调用] {json.dumps(chunk.tool_calls, ensure_ascii=False, indent=2)}")
            print("\n")
        except Exception as e:
            print(f"\n测试失败：{str(e)}")
        print("\n" + "=" * 60)
        # 测试 3: 非流式输出
        print("场景 3: 非流式输出测试")
        print("-" * 60)
        try:
            result = ChatTool.chat_with_history_non_stream(
                messages=[{"role": "user", "content": "你好"}],
                extra_body={"enable_thinking": True}
            )
            print(f"回复内容: {result.content}")
            print(f"推理过程: {result.reasoning_content}")
            print(f"工具调用: {result.tool_calls}")
        except Exception as e:
            print(f"\n测试失败：{str(e)}")
        print("\n" + "=" * 60)
        print("结构化流式输出测试完成！")
        print("=" * 60)

    def test_chat_client():
        """主函数，提供命令行交互界面"""
        print("=" * 60)
        print("欢迎使用 Qwen3.5-2B 聊天助手")
        print("输入 'quit' 或 'exit' 退出程序")
        print("输入 'clear' 清空对话历史")
        print("=" * 60)
        print()
        chat_bot = ChatTool()
        while True:
            try:
                # 获取用户输入
                user_input = input("\n你：").strip()
                if not user_input:
                    continue
                # 检查退出命令
                if user_input.lower() in ['quit', 'exit']:
                    print("\n再见！")
                    break
                # 检查清空命令
                if user_input.lower() == 'clear':
                    chat_bot.clear_history()
                    print("对话历史已清空")
                    continue
                # 发送消息并获取流式响应
                print("\n助手：", end="", flush=True)
                try:
                    for chunk in chat_bot.chat(user_input, stream=True):
                        if chunk.content:
                            print(chunk.content, end="", flush=True)
                        # print(chunk)
                    print()  # 换行
                except Exception as e:
                    print(f"\n错误：{str(e)}")
            except KeyboardInterrupt:
                print("\n\n中断退出")
                break
            except EOFError:
                break

    async def test_chat_async():
        """测试异步方法 chat_async"""
        print("=" * 60)
        print("测试异步方法 chat_async")
        print("=" * 60)
        print()
        chat_bot = ChatTool()
        # 第一轮对话
        user_input = "你好，请介绍一下深圳航空"
        print(f"用户：{user_input}")
        print("助手：", end="", flush=True)
        try:
            async for chunk in chat_bot.chat_async(user_input, stream=True):
                if chunk.content:
                    print(chunk.content, end="", flush=True)
            print("\n")
            print("\n异步方法测试完成！")
        except Exception as e:
            print(f"\n异步方法测试失败：{str(e)}")
        print("=" * 60)

    def test_static_functions():
        """测试静态方法：模拟前端多轮对话输入"""
        import asyncio
        print("=" * 60)
        print("测试静态方法 - 模拟前端多轮对话")
        print("=" * 60)
        print()
        # 模拟前端传递的多轮对话历史
        messages = [
            {"role": "system", "content": "你是深圳航空的客服助手，提供关于行李规定和相关服务的信息。请根据用户的问题提供准确、清晰的回答。不知道的请直接回答“抱歉，我无法给出准确答案”，不要输出多余的话"},
            {"role": "user", "content": "你好，我想了解一下深圳航空的行李规定"},
        ]
        print("场景 1：使用同步静态方法继续对话")
        print("-" * 60)
        print("当前对话历史：")
        for i, msg in enumerate(messages, 1):
            role = "用户" if msg["role"] == "user" else "助手"
            print(f"{i}. {role}: {msg['content'][:50]}...")
        print()
        print("助手回答：")
        try:
            assistant_messages = ""
            # 测试同步版本
            for chunk in ChatTool.chat_with_history(messages):
                if chunk.content:
                    print(chunk.content, end="", flush=True)
                    assistant_messages += chunk.content
            print("\n")
        except Exception as e:
            print(f"\n同步方法测试失败：{str(e)}")
        print("\n" + "=" * 60)
        print("场景 2：使用异步静态方法继续对话")
        print("-" * 60)

        async def test_async():
            test_messages = messages + [
                {"role": "assistant", "content": "您好！深圳航空的行李规定如下：\n\n**托运行李：**\n- 经济舱旅客可免费托运 1 件行李，重量不超过 23kg"}
            ]
            test_messages.append({
                "role": "user", 
                "content": "我还想问一下宠物可以托运吗？有什么要求？"
            })
            print("助手回答：")
            try:
                async for chunk in ChatTool.chat_with_history_async(test_messages):
                    if chunk.content:
                        print(chunk.content, end="", flush=True)
                print()
            except Exception as e:
                print(f"\n异步方法测试失败：{str(e)}")
        
        asyncio.run(test_async())
        print("\n" + "=" * 60)
        print("静态方法测试完成！")
        print("=" * 60)

    # 测试入口参数
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test-structured":
        # 运行结构化流式输出测试
        test_structured_streaming()
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-static":
        # 运行静态方法测试
        test_static_functions()
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-async":
        # 运行异步方法测试
        asyncio.run(test_chat_async())
    else:
        # 运行交互式聊天
        test_chat_client()
    ...