# 标准库
import os
from pathlib import Path

# 第三方库
from typing import List, Optional, Any #, Union, Dict
from pydantic import BaseModel, Field

# 自定义的模块
from env_manager import load_var       # 环境变量加载


# 程序的根目录，通常用于相对路径的计算
PROJECT_ROOT = Path(__file__).resolve().parent



class Message(BaseModel):
    role: str = "user"       # 角色，可以是 "user" 或 "assistant" 或 "system" 或 "tool"
    content: str | List | None = None  # 内容可以是字符串或列表（tool_calls 时为 None）
    name: Optional[str] = None  # 可选的名称
    tool_calls: Optional[List[dict]] = None  # 工具调用列表
    tool_call_id: Optional[str] = None  # 工具调用 ID（tool role 时必须）
    refusal: Optional[str] = None  # 拒绝内容
    reasoning_content: Optional[str] = None  # 思考内容

    
class FunctionDefinition(BaseModel):
    """函数定义"""
    name: str                          # 函数名称
    description: Optional[str] = None  # 函数描述
    parameters: Optional[dict] = None  # JSON Schema 格式的函数参数


class ToolDefinition(BaseModel):
    """工具定义"""
    type: str = "function"             # 工具类型，目前只支持 "function"
    function: FunctionDefinition       # 函数定义


class ChatLLMRequest(BaseModel):
    """用户信息"""
    messages: List[Message]                     # 输入的消息列表，每个消息包含一个角色 role 和内容 content，用于表示对话的上下文。
    max_tokens: int = 8192                      # 指定生成的最大 token 数量，默认为 32768（官方推荐值），复杂任务可设置 81920
    temperature: float = 0.7                    # 用于控制生成文本的随机性，默认为 1.0。较高的温度会使生成的文本更加随机，而较低的温度则会使文本更加确定。
    top_p: float = 1.0                          # 用于控制生成文本的多样性，默认为 1.0。这个参数是核采样（nucleus sampling）的一部分，用于过滤掉概率低于阈值的 token。
    stream: bool = False                        # 是否使用流式响应
    reasoning_effort: str = "medium"            # 思考深度，默认为 medium，可选值有 "low", "medium", "high" 等
    presence_penalty: float = 2.0               # 这个参数用于控制模型对重复内容的惩罚程度，官方推荐非思考模式文本任务使用 2.0
    timeout_connect: int = 300                  # 建立连接超时秒数
    timeout_read: int = 1800                    # 读取响应超时秒数
    timeout_drain: int = 120                    # 发送数据冲刷超时秒数
    # enable_thinking: Optional[bool] = None      # 是否启用思考模式，默认为 None（优先从 extra_body 读取）
    extra_body: Optional[dict[str, Any]] = None # 额外的请求体参数（用于传递非标准参数如 enable_thinking）
    parallel_tool_calls: Optional[bool] = True  # 是否允许并行工具调用
    # tool_choice: Optional[str | dict] = None    # 工具选择策略："none", "auto", "required", 或 {"type": "function", "function": {"name": "xxx"}}
    tool_choice: Optional[str | dict] = 'auto'  # 工具选择策略："none", "auto", "required", 或 {"type": "function", "function": {"name": "xxx"}}
    tool_names: Optional[List[str]] = None  # 前端本轮选择的工具名称列表（唯一入口）
    tools: Optional[List[ToolDefinition]] = Field(default=None, exclude=True)  # 服务端内部字段：由后端按 tool_names 生成真实工具定义
    session_id: str = "default"                 # 会话ID，用于隔离不同会话的记忆（工具和文件历史）
    use_backend_history: Optional[bool] = None   # 是否启用后端会话历史拼接（None 表示走服务端默认）
    backend_history_rounds: Optional[int] = None # 后端拼接最近多少轮历史（None 表示走服务端默认）


class HistoryCompactionConfig(BaseModel):
    keep_rounds: int = Field(..., ge=1, description="保留最近的原始轮数")
    trigger_ratio: float = Field(..., gt=0, le=1, description="触发历史压缩的上下文占比")


class ChatModelSelection(BaseModel):
    provider: str = Field(..., min_length=1, description="顶层 provider 名称（对应 .env 中的 CHAT_OWNERSHIP_NANE）")
    model: str = Field(..., min_length=1, description="模型 id（对应 .env 中的 CHAT_MODEL_NAME）")



# 函数定义部分
def set_current_dir(target_directory: str = None) -> bool:
    if target_directory == __file__ or target_directory is None:
        return False
    target_path = Path(str(target_directory)).expanduser()
    if not target_path.is_absolute():
        target_path = Path.cwd() / target_path
    resolved_path = target_path.resolve()
    if resolved_path.is_dir():
        work_dir = resolved_path
    elif resolved_path.exists():
        work_dir = resolved_path.parent
    else:
        return False
    os.chdir(str(work_dir))
    # print(f"Current Directory set to: {work_dir}")
    return True


def get_current_dir() -> str:
    return os.getcwd().replace('\\', '/')


def get_persisted_work_dir() -> str | None:
    value = load_var("CHAT_WORK_DIR", None)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def apply_persisted_work_dir() -> bool:
    persisted_dir = get_persisted_work_dir()
    if not persisted_dir:
        return False
    return set_current_dir(persisted_dir)



if __name__ == "__main__":
    # 测试当前目录设置
    set_current_dir("c:\\")
    print("Current Directory:", get_current_dir())
