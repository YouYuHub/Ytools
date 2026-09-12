# coding: utf-8
"""系统提示词构建模块（原 factory/chat_factory.py 内的提示词构建逻辑）。

把与「运行时系统提示文本」相关的构建函数集中到独立模块：
- build_runtime_system_text：运行时系统提示附加文本（工作路径 + 系统提示）；
- build_sys_prompt：基础系统提示（环境/工具规则/回传长度/超时等可变配置说明）；
- build_media_tag_prompt：媒体伪标签输出说明。

chat_factory 保留 build_runtime_system_text 的 re-export（routers/chat_router
等既有导入路径不变）。

可变配置说明的策略：与真实生成请求同一读取口径（load_var），提示词每次
构造时实时求值——用户改配置后，下一轮对话模型即可看到新值。
"""
import json
from typing import Any

from config import (
    DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    get_current_dir,
)
from env_manager import load_var
from factory.agent_runtime.chat_runtime import parse_return_length


def _format_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def _load_reasoning_return_max_length(default: int = DEFAULT_REASONING_RETURN_MAX_LENGTH) -> int:
    return parse_return_length(load_var("REASONING_RETURN_MAX_LENGTH", default), default)


def _load_tool_call_timeout() -> float:
    """读取 MCP 工具单次执行超时（与 tool_executor/mcp_client 同口径）。"""
    try:
        return float(load_var(
            "MCP_TOOL_CALL_TIMEOUT_SECONDS",
            DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        ))
    except (TypeError, ValueError):
        return float(DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS)


def build_media_tag_prompt() -> str:
    """媒体伪标签输出说明：前端会把 <image>/<audio>/<video>/<pdf> 渲染为可交互控件。

    src 三种取值：网络 URL / media:// 引用（本会话上传媒体）/ 本地路径
    （相对当前工作路径或绝对路径均可，项目支持路径切换）。仅确认存在的
    文件才输出标签；标签独立成行；除 src/alt/title 外不要附加其它属性。
    另支持 ```svg 代码块图片生成格式：模型直接输出 SVG 源码，前端渲染为
    「代码/图片」双视图控件（代码可复制、渲染图可复制为 PNG）。
    """
    return (
        "## 媒体展示（伪标签）\n"
        "需要在回复中向用户展示图片/音频/视频/PDF 时，输出以下伪标签（不要输出真实 HTML 标签，前端会负责渲染）：\n"
        '- <image src="{路径或URL}" alt="{一句话描述}"></image>\n'
        '- <audio src="{路径或URL}" title="{名称}"></audio>\n'
        '- <video src="{路径或URL}" title="{名称}"></video>\n'
        '- <pdf src="{路径或URL}" title="{名称}"></pdf>\n'
        "src 支持三种取值：\n"
        "1. 网络 URL：http/https 开头的直链，公共互联网在线资源（如公开图片/音频/视频/PDF 文件直链）均可，前端可直接加载展示；\n"
        "2. media://文件名（用户本会话上传的媒体文件）；\n"
        "3. 本地路径：请使用绝对路径（如 C:/data/x.png 或者 /home/user/x.png）；\n"
        "规则：仅引用你确认存在的文件（必要时先用工具核实路径）；每个标签必须独立成行；"
        "src 使用双引号；不要附加 style/onclick 等其它属性；无法展示时直接用文字说明路径。\n"
        "\n"
        "## SVG 图片生成（代码即图片）\n"
        "需要生成图片（图表/曲线/logo/图标/示意图/几何插画/文字海报等矢量图形）时，"
        "不要输出 <image> 标签，直接输出 ```svg 代码块，前端会渲染为「代码 / 图片」双视图控件：\n"
        "````\n"
        "```svg\n"
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 300\" width=\"400\" height=\"300\">\n"
        "  <!-- 完整 SVG 源码（rect/circle/path/text 等元素） -->\n"
        "</svg>\n"
        "```\n"
        "````\n"
        "要求：\n"
        "1. 语言标签必须是小写 svg（```svg），代码块内只放一份完整 SVG 源码（以 <svg 开头、</svg> 结尾）；\n"
        "2. 每个代码块独立成段（前后留空行），一行一张图片；多张图片用多个 ```svg 代码块；\n"
        "3. 根节点建议带 viewBox 与 width/height 属性（复制为 PNG 时按此定尺寸）；\n"
        "4. 不要在 svg 代码块外输出真实 <svg>/<image> HTML 标签；\n"
        "5. 流式输出时未闭合的代码块会先按普通代码展示，闭合后自动切换为双视图控件。\n"
        "\n"
        "## 数学公式（KaTeX）\n"
        "需要表达数学公式时，输出 LaTeX 源码并用以下定界符包裹（前端用 KaTeX 渲染）：\n"
        "1. 行内公式：$…$ 或 \\(…\\)，如 $E=mc^2$，随文字段落流动排版；\n"
        "2. 独立成块的公式：$$…$$ 或 \\[…\\]，可跨多行，居中独立展示；\n"
        "3. 定界符必须成对闭合；公式内不要使用 Markdown 语法；\n"
        "4. 表述普通价格、金额时不要用单个 $ 包裹（会被误认为公式），直接写数字即可。\n"
        "\n"
        "## Mermaid 图表（代码即图）\n"
        "需要生成流程图/时序图/甘特图/状态图/类图/思维导图等结构化图表时，"
        "不要输出 <image> 标签，直接输出 ```mermaid 代码块，前端会用 Mermaid.js 渲染为「代码 / 图片」双视图控件：\n"
        "````\n"
        "```mermaid\n"
        "graph TD\n"
        "    A[开始] --> B{判断}\n"
        "    B -->|是| C[处理]\n"
        "    B -->|否| D[结束]\n"
        "```\n"
        "````\n"
        "要求：\n"
        "1. 语言标签必须是小写 mermaid（```mermaid）；\n"
        "2. 块内只放一份图表定义，语法必须符合 Mermaid 官方规范（节点文本含特殊字符时用引号包裹）；\n"
        "3. 每个代码块独立成段（前后留空行），一行一张图；多张图用多个 ```mermaid 代码块；\n"
        "4. 不要在 mermaid 代码块外输出真实 <svg>/<image> HTML 标签；\n"
        "5. 流式输出时未闭合的代码块先按普通代码展示，闭合后自动渲染为图表。\n"
        "\n"
        "## Canvas 程序块（沙箱运行确认）\n"
        "需要用代码绘制图形/动画/数据可视化，或运行一段演示用 JS 脚本时，"
        "输出 ```canvas 代码块（块内为一段完整 JavaScript，前端提供沙箱与画布）：\n"
        "````\n"
        "```canvas\n"
        "const ctx = stage.getContext('2d');\n"
        "const g = ctx.createLinearGradient(0, 0, 720, 420);\n"
        "g.addColorStop(0, '#1e3a8a');\n"
        "g.addColorStop(1, '#0ea5e9');\n"
        "ctx.fillStyle = g;\n"
        "ctx.fillRect(0, 0, 720, 420);\n"
        "ctx.fillStyle = '#fff';\n"
        "ctx.font = 'bold 36px sans-serif';\n"
        "ctx.textAlign = 'center';\n"
        "ctx.fillText('Hello Canvas', 360, 220);\n"
        "console.log('绘制完成');\n"
        "```\n"
        "````\n"
        "运行环境说明（务必遵守）：\n"
        "1. 画布变量 stage 已预置（720×420 的 canvas 元素），日志用 console.log 输出；\n"
        "2. 默认不自动执行：用户点击「运行」确认后才在隔离沙箱中执行，"
        "脚本顶部可用 1-2 行注释说明将绘制什么；\n"
        "3. 沙箱内可用的只有 stage 与 console：不要使用 window/document/"
        "parent/fetch/localStorage/事件监听等外部能力，动画帧循环（requestAnimationFrame）也不可用；\n"
        "4. 脚本应同步执行并尽快完成（一次性绘制或固定次数的离线计算），"
        "不要编写依赖持续运行的过程；\n"
        "5. 语言标签必须是小写 canvas（```canvas），块内只放一段脚本，"
        "不要嵌套 ``` 栅栏或输出其它语言标签的代码块。\n"
    )


def build_sys_prompt() -> str:
    """构建系统提示词。

    思考过程/历史工具结果的回传长度提示只在配置为正数（截断回传）时出现：
    截断会改变模型看到的内容，必须明确告知边界；全量回传与不回传时不提示。
    MCP 工具执行超时为实时配置（用户可在聊天设置中修改），始终告知模型，
    便于其把长耗时操作拆分/分批，避免单次调用撞上超时被中止。
    """
    reasoning_limit = _load_reasoning_return_max_length()
    tool_result_limit = parse_return_length(
        load_var("HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH", DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
        DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    )
    tool_call_timeout = _load_tool_call_timeout()
    if tool_call_timeout > 0:
        timeout_note = (
            f"MCP 工具单次执行超时为 {tool_call_timeout:g} 秒（含连接/初始化/调用全过程），"
            "超时会被中止并返回错误。耗时可能较长的操作（构建、批量处理、"
            "等待外部服务）请拆分为多次调用或缩小单次范围。"
        )
    else:
        timeout_note = (
            "MCP 工具执行不限制超时：单次调用可能长时间阻塞，"
            "耗时不确定的操作请主动拆分并阶段性反馈进度。"
        )
    notes = [
        "工具由用户选择提供，你只能使用最近一次 user 角色给你的（如果用户提供了）；之前用过的工具不一定能使用。",
        "任务过程中，你的思考过程只保留最近一次，过程中的重要发现需要实时告诉用户，这也是为了后续任务的连贯性。",
        timeout_note,
    ]
    if tool_result_limit > 0:
        # 与 chat_history_format._truncate_tool_result_text 的"前 N 字符"语义一致
        notes.append(
            f"历史轮次中每个工具结果只回传最前面 {tool_result_limit} 字符（超出部分省略）；重要工具结果会由上下文摘要保留。"
        )
    elif tool_result_limit == 0:
        notes.append("后端常规历史上下文只保留工具调用参数；重要工具结果会由上下文摘要保留。")
    # tool_result_limit < 0：工具结果完整回传，无需提示
    if reasoning_limit > 0:
        # 与 full_reasoning[-reasoning_limit:] 的"末尾 N 字符"语义一致
        notes.append(f"你的思考过程（reasoning_content）只会保留最后 {reasoning_limit} 字符。")
    # reasoning_limit <= 0：完整回传或不回传，无需提示
    numbered_notes = "\n".join(f"{index}、{note}" for index, note in enumerate(notes, start=1))
    media_prompt = build_media_tag_prompt()
    return (
        "当前系统已安装基础 py 环境。\n"
        "程序所有工具支持都并发调用（如果有）；工具返回 [] 表示空值而不是失败。\n"
        "注意：\n"
        f"{numbered_notes}\n"
        + media_prompt
    )


def build_runtime_system_text(work_dir: str | None = None) -> str:
    """构造运行时系统提示附加文本（工作路径 + 系统提示）。

    与任务内 runtime_sys_text 同源：真实请求会把它追加到首条 system 消息，
    token 统计接口用它单独估算系统提示词开销。

    work_dir 缺省时取当前进程 cwd：worker 进程内已在任务开始时 chdir 到
    会话目录，天然正确；主进程调用方（token 统计等）应显式传入按会话
    解析的目录（resolve_session_work_dir），避免跨会话读到全局 cwd。
    """
    dir_text = work_dir if isinstance(work_dir, str) and work_dir.strip() else get_current_dir()
    return (
        f"当前工作路径为<{_format_tool_result(dir_text)}>\n"
        + build_sys_prompt()
    )
