# 标准库
import json
import os
from pathlib import Path

# 第三方库
from typing import List, Optional, Any #, Union, Dict
from pydantic import BaseModel, Field

# 自定义的模块
from env_manager import load_var       # 环境变量加载


# 程序的根目录，通常用于相对路径的计算
PROJECT_ROOT = Path(__file__).resolve().parent
# 默认服务启动宿主、端口
DEFAULT_SERVICE_HOST = load_var("DEFAULT_SERVICE_HOST", "0.0.0.0")
DEFAULT_SERVICE_PORT = load_var("DEFAULT_SERVICE_PORT", 48621)

# 兼容旧版原始历史上下文与统计的默认轮数；摘要-only 模式下已完成轮次不再按轮回传。
# 历史压缩设置中的 keep_rounds 仍保留为兼容配置，避免旧客户端字段失效。
DEFAULT_CONTEXT_HISTORY_ROUNDS = 20         # 默认的上下文历史轮数
DEFAULT_HISTORY_TRIGGER_RATIO = 0.8         # 历史压缩触发比例
DEFAULT_HISTORY_CHUNK_ROUNDS = 3            # 历史压缩每次处理的轮数
DEFAULT_SUMMARY_BUDGET_RATIO = 0.2          # 历史摘要总预算比例
DEFAULT_OVERSIZED_REJECT_FACTOR = 1.5       # 超长工具结果拒绝写入模型上下文的系数
DEFAULT_MAX_OVERSIZED_REJECTIONS = 3        # 连续超长拒绝达到该次数时终止当前任务
DEFAULT_REASONING_RETURN_MAX_LENGTH = -1    # 思考过程（reasoning_content）最大回传长度；0 表示不回传，负数表示全部回传，正数表示保留末尾 N 字符
DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH = -1  # 历史轮次单个工具结果的最大回传长度；0 表示不回传，负数表示全部回传，正数表示截断到前 N 字符
DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS = 300  # MCP 工具单次执行超时秒数（含连接/初始化/调用全过程）；0 或负数表示不限制
DEFAULT_TOOL_CALL_STREAM_TIMEOUT_SECONDS = 300  # 工具调用流式阶段（模型 SSE 输出 tool_calls 期间）无输出超时秒数；超时按工具调用失败反馈模型并继续任务；0 或负数表示不限制
DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS = 3       # 模型请求连续失败重试达到该次数时终止任务；0 或负数表示不限制（一直重试）

# sub_agent 子智能体（docs/sub_agent_v1.md §10）
DEFAULT_SUB_AGENT_ENABLED = True             # 总开关：false 时不注入 sub_agent 工具定义
DEFAULT_SUB_AGENT_MAX_ROUNDS = 40            # 单个子任务的模型调用轮次上限
DEFAULT_SUB_AGENT_MAX_CONCURRENT = 3         # 同一父轮并发子任务上限（超出排队执行）
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = 900      # 单个子任务整体超时秒数；0 或负数表示不限制
DEFAULT_SUB_AGENT_REPLY_MAX_CHARS = 30000    # 子任务最终回复返回父级前的截断保护（完整轨迹在 JSONL 块内）

# 工具并发执行（前端聊天设置可调，GET/POST /chat_config/tool_concurrency）
DEFAULT_ONE_TASK_MAX_WORKERS = 3             # 同一轮多个 MCP 工具调用并发执行的线程池大小（实际取值与工具数取较小者）


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
    max_tokens: int = 8192                      # 指定生成的最大 token 数量
    temperature: float = 0.7                    # 用于控制生成文本的随机性，默认为 1.0。较高的温度会使生成的文本更加随机，而较低的温度则会使文本更加确定。
    top_p: float = 1.0                          # 用于控制生成文本的多样性，默认为 1.0。这个参数是核采样（nucleus sampling）的一部分，用于过滤掉概率低于阈值的 token。
    stream: bool = False                        # 是否使用流式响应
    reasoning_effort: str = "medium"            # 思考深度，默认为 medium；GPT-6 Astra 官方档位为 low/medium/high/xhigh/max（max 为最高档，API 无 ultra 档）
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
    backend_history_rounds: Optional[int] = None # 兼容参数；摘要-only 模式不限制已完成历史回传


class ChatModelSelection(BaseModel):
    provider: Optional[str] = Field(
        None,
        description="顶层 provider 名称；会话级 clear=true 时可省略，其余场景必填（缺失时由服务端校验报错）",
    )
    model: Optional[str] = Field(
        None,
        description="模型命名（models.json 中 models 字段的键名）；会话级 clear=true 时可省略",
    )
    role: str = Field(
        "chat_model",
        description="模型角色：chat_model（聊天）/ compaction_model（压缩）/ title_model（标题），默认 chat_model",
    )
    parameter: Optional[dict[str, Any]] = Field(
        None,
        description="该模型的默认生成参数（如 temperature/max_tokens/top_p 等），全量替换语义；不传表示保持现有配置",
    )
    session_id: Optional[str] = Field(
        None,
        description="会话ID；携带时写入该会话的 _meta.model_selection（会话级覆盖，仅覆盖该角色），"
                    "不携带时写入全局默认（models.json 顶层 model_selection）",
    )
    clear: bool = Field(
        False,
        description="仅会话级有效：为 true 时清除该会话当前角色的独立模型选择，恢复跟随全局默认（忽略 provider/model）",
    )


class HistoryCompactionConfig(BaseModel):
    """上下文压缩策略的完整配置请求。"""

    keep_rounds: int = Field(
        DEFAULT_CONTEXT_HISTORY_ROUNDS,
        ge=0,
        le=200,
        description="模型上下文保留的总轮次窗口：未压缩轮次完整对话占窗口，"
                    "已压缩轮次问题按剩余窗口保真；0 表示无限轮次（仅按阈值压缩）",
    )
    trigger_ratio: float = Field(
        DEFAULT_HISTORY_TRIGGER_RATIO,
        gt=0,
        le=0.95,
        description="历史压缩触发比例；未使用强制摘要模式的调用按该比例判断（最大 0.95）",
    )
    summary_budget_ratio: float = Field(
        DEFAULT_SUMMARY_BUDGET_RATIO,
        gt=0,
        le=0.5,
        description="累计摘要总预算 = 聊天模型窗口 × 该比例（最大 0.5）",
    )
    oversized_reject_factor: Optional[float] = Field(
        DEFAULT_OVERSIZED_REJECT_FACTOR,
        ge=0,
        le=10,
        description="单次工具结果超过 min(聊天窗口,压缩窗口)×该系数时拒绝写入模型上下文并让模型重新考虑；0 关闭，None 表示保持当前 .env 配置",
    )
    max_oversized_rejections: Optional[int] = Field(
        DEFAULT_MAX_OVERSIZED_REJECTIONS,
        ge=1,
        description="连续超长拒绝达到该次数时终止当前任务；None 表示保持当前 .env 配置",
    )


class ContextReturnConfig(BaseModel):
    """思考过程与历史工具结果的最大回传长度配置。"""

    reasoning_max_length: int = Field(
        DEFAULT_REASONING_RETURN_MAX_LENGTH,
        description="思考过程（reasoning_content）最大回传长度；0 表示不回传，负数表示全部回传，正数表示保留末尾 N 字符",
    )
    tool_result_max_length: int = Field(
        DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
        description="历史轮次单个工具结果的最大回传长度；0 表示不回传，负数表示全部回传，正数表示截断到前 N 字符",
    )


class McpToolConfig(BaseModel):
    """MCP 工具执行配置。"""

    call_timeout_seconds: float = Field(
        DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        description="MCP 工具单次执行超时秒数（含连接/初始化/调用全过程）；0 或负数表示不限制",
    )


class NetworkRetryConfig(BaseModel):
    """模型网络请求失败重试配置。"""

    max_attempts: int = Field(
        DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS,
        description="模型请求连续失败重试达到该次数时终止任务；0 或负数表示不限制（一直重试直到手动停止）",
    )


class ToolConcurrencyConfig(BaseModel):
    """工具并发执行配置：MCP 工具线程池 + 子智能体并发上限。"""

    mcp_tool_workers: int = Field(
        DEFAULT_ONE_TASK_MAX_WORKERS,
        ge=1,
        description="同一轮多个 MCP 工具调用并发执行的线程池大小；实际并发数=该值与工具数取较小者",
    )
    sub_agent_max_concurrent: int = Field(
        DEFAULT_SUB_AGENT_MAX_CONCURRENT,
        ge=1,
        description="同一父轮并发子智能体数量上限；超出的子任务排队执行",
    )


class McpToolSelection(BaseModel):
    """MCP 工具选择配置请求：服务名 -> 工具名数组。"""

    inputs: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="工具选择映射：键为 mcp_servers.json 中配置的服务名，值为该服务下选中的工具名数组；"
                    "非法条目（非字符串/空串/重复）由服务端规整时丢弃",
    )
    session_id: Optional[str] = Field(
        None,
        description="会话ID；携带时写入该会话的 _meta.tool_selection（会话级覆盖，"
                    "空 inputs 表示清除覆盖恢复跟随全局），不携带时写入全局默认（mcp_servers.json 的 inputs 键）",
    )


class SessionWorkDirConfig(BaseModel):
    """会话级工作目录配置请求（写入 _meta.work_dir）。"""

    session_id: str = Field(..., min_length=1, description="会话ID")
    work_dir: Optional[str] = Field(
        "",
        description="会话独立工作目录；空串/None 表示清除覆盖，恢复跟随全局默认 DEFAULT_CHAT_WORK_DIR",
    )



# 函数定义部分
def resolve_work_dir(target_directory: str | None) -> Path | None:
    """校验并解析目标工作目录，返回可用的绝对路径；无效返回 None（不做 chdir）。

    规则与 set_current_dir 一致：目录 → 本身；存在的文件 → 其父目录；
    不存在 → None；相对路径按当前进程 cwd 展开。
    """
    if not isinstance(target_directory, str) or not target_directory.strip():
        return None
    if target_directory == __file__:
        return None
    target_path = Path(str(target_directory)).expanduser()
    if not target_path.is_absolute():
        target_path = Path.cwd() / target_path
    try:
        resolved_path = target_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved_path.is_dir():
        return resolved_path
    if resolved_path.exists():
        return resolved_path.parent
    return None


def set_current_dir(target_directory: str = None) -> bool:
    resolved_path = resolve_work_dir(target_directory)
    if resolved_path is None:
        return False
    os.chdir(str(resolved_path))
    # print(f"Current Directory set to: {resolved_path}")
    return True


def get_current_dir() -> str:
    return os.getcwd().replace('\\', '/')


def get_persisted_work_dir() -> str | None:
    value = load_var("DEFAULT_CHAT_WORK_DIR", None)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


# ---------- setting/mcp_servers.json 读取/规整/写入 ----------
# 会话级工具选择（_meta.tool_selection）与全局默认工具选择（inputs 键）共用这里的
# 规整逻辑；放在 config.py 是因为它同时被路由层、memory 层与 worker 进程引用，
# 且只依赖标准库，不会引入循环导入。


def get_mcp_servers_config_path() -> Path:
    return PROJECT_ROOT / "setting" / "mcp_servers.json"


def read_mcp_servers_config(target_path: Path | None = None) -> dict:
    """安全读取 mcp_servers.json；文件缺失/解析失败返回 {}（不抛异常）。"""
    path = Path(target_path) if target_path is not None else get_mcp_servers_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def configured_server_names(data: dict) -> dict:
    servers = data.get("servers")
    return servers if isinstance(servers, dict) else {}


def normalize_tool_inputs(raw: Any) -> dict[str, list[str]]:
    """规整为 {服务名: [去重工具名]}；非法条目（非字符串/空串/重复）直接丢弃。"""
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for server, tool_names in raw.items():
        if not isinstance(server, str) or not server.strip() or not isinstance(tool_names, list):
            continue
        names: list[str] = []
        for name in tool_names:
            if isinstance(name, str) and name.strip() and name not in names:
                names.append(name)
        normalized[server] = names
    return normalized


def get_global_tool_inputs(target_path: Path | None = None) -> tuple[dict[str, list[str]], list[str], str | None]:
    """读取全局默认工具选择（mcp_servers.json 的 inputs 键）。

    Returns:
        (规整后的 inputs（已配置服务补 []）, 已配置服务名列表, 读取失败原因或 None)
    """
    data = read_mcp_servers_config(target_path)
    if not data:
        # 文件缺失与解析失败在这里等价：都没有全局默认可选
        path = Path(target_path) if target_path is not None else get_mcp_servers_config_path()
        return {}, [], "mcp_servers.json 解析失败" if path.exists() else None
    servers = configured_server_names(data)
    inputs = normalize_tool_inputs(data.get("inputs"))
    for server in servers:
        inputs.setdefault(str(server), [])
    return inputs, [str(server) for server in servers], None


def write_mcp_servers_inputs(inputs: dict[str, list[str]], target_path: Path | None = None) -> dict:
    """把规整后的 inputs 写回 mcp_servers.json（仅替换 inputs 键，其余内容原样保留）。

    临时文件 + 原子替换写入，避免配置热重载线程读到半截 JSON。
    """
    path = Path(target_path) if target_path is not None else get_mcp_servers_config_path()
    data = read_mcp_servers_config(path)
    if not data:
        data = {}
    data["inputs"] = normalize_tool_inputs(inputs)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    import time
    for attempt in range(6):
        try:
            os.replace(tmp_path, path)
            return data
        except OSError:
            if attempt >= 5:
                raise
            time.sleep(0.01 * (attempt + 1))
    return data  # pragma: no cover - 上面重试耗尽会直接 raise


def apply_persisted_work_dir() -> bool:
    persisted_dir = get_persisted_work_dir()
    if not persisted_dir:
        return False
    return set_current_dir(persisted_dir)



if __name__ == "__main__":
    # 测试当前目录设置
    set_current_dir("c:\\")
    print("Current Directory:", get_current_dir())
