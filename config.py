# 标准库
import os

# 第三方库
from typing import List, Optional, Any #, Union, Dict
from pydantic import BaseModel #, Field

# 自定义的库
from load_env import load_var       # 环境变量加载


class Message(BaseModel):
    role: str = "user"       # 角色，可以是 "user" 或 "assistant" 或 "system"
    content: str | List      # 内容可以是字符串或列表
    # name: Optional[str] = None  # 可选的名称
    # tool_calls: Optional[List[dict]] = None  # 工具调用列表
    # refusal: Optional[str] = None  # 拒绝内容
    # reasoning_content: Optional[str] = None  # 思考内容


# 新增：工具定义相关 Pydantic 模型
class FunctionDefinition(BaseModel):
    """函数定义"""
    name: str                          # 函数名称
    description: Optional[str] = None  # 函数描述
    parameters: Optional[dict] = None  # JSON Schema 格式的函数参数


class ToolDefinition(BaseModel):
    """工具定义"""
    type: str = "function"             # 工具类型，目前只支持 "function"
    function: FunctionDefinition       # 函数定义


class ChatToolRequest(BaseModel):
    """用户信息"""
    model: str = load_var("TOOL_CHAT_MODEL", "Qwen3.5-2B") # 模型名称，默认为 "Qwen3.5-2B"
    messages: List[Message]                     # 输入的消息列表，每个消息包含一个角色 role 和内容 content，用于表示对话的上下文。
    max_tokens: int = 8192                      # 指定生成的最大 token 数量，默认为 32768（官方推荐值），复杂任务可设置 81920
    temperature: float = 0.7                    # 用于控制生成文本的随机性，默认为 1.0。较高的温度会使生成的文本更加随机，而较低的温度则会使文本更加确定。
    top_p: float = 1.0                          # 用于控制生成文本的多样性，默认为 1.0。这个参数是核采样（nucleus sampling）的一部分，用于过滤掉概率低于阈值的 token。
    stream: bool = False                        # 是否使用流式响应
    reasoning_effort: str = "medium"            # 思考深度，默认为 medium，可选值有 "low", "medium", "high" 等
    presence_penalty: float = 2.0               # 这个参数用于控制模型对重复内容的惩罚程度，官方推荐非思考模式文本任务使用 2.0
    # enable_thinking: Optional[bool] = None      # 是否启用思考模式，默认为 None（优先从 extra_body 读取）
    extra_body: Optional[dict[str, Any]] = None # 额外的请求体参数（用于传递非标准参数如 enable_thinking）
    parallel_tool_calls: Optional[bool] = True  # 是否允许并行工具调用
    # tool_choice: Optional[str | dict] = None    # 工具选择策略："none", "auto", "required", 或 {"type": "function", "function": {"name": "xxx"}}
    tool_choice: Optional[str | dict] = 'auto'  # 工具选择策略："none", "auto", "required", 或 {"type": "function", "function": {"name": "xxx"}}
    tools: Optional[List[ToolDefinition]] = None  # 可用工具列表
    session_id: str = "default"                 # 会话ID，用于隔离不同会话的记忆（工具和文件历史）



# 函数定义部分
def get_current_dir() -> str:
    return os.getcwd().replace('\\', '/')