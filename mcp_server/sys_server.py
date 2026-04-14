# coding: utf-8
"""
系统的 mcp 服务模块
"""
# 标准库
import os
from datetime import datetime

# 第三方库
from mcp.server.fastmcp import FastMCP

# 创建 MCP 服务器实例
sys_mcp_server = FastMCP("sys-mcp-server")

# 自定义模块


# 工具定义
@sys_mcp_server.tool()
async def format_current_time() -> str:
    """ 格式化当前时间
    无参数
    返回：
        格式化 %Y-%m-%d %H:%M:%S 的时间字符串
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@sys_mcp_server.tool()
async def list_dir_item(dir_path=None, search_mode='file', file_type=None, depth: int = 0) -> list[dict]:
    """ 列出指定目录下的文件或目录，返回文件或目录的详细信息列表
    参数：
        dir_path:       目录路径，默认为当前目录
        search_mode:    查询模式，默认为 file，表示查询文件，可选项为 file, dir，all；其中 all 表示所有文件和目录，传入其他参数则只会返回文件
        file_type:      文件类型（例如：.py），默认为 None，表示所有文件以及其子目录下的所有文件
        depth:          整数类型，递归深度，0 表示不递归（即当前目录），1 表示一级子目录，以此类推，默认为 0
    返回：
        文件或目录信息列表，每个元素是一个字典，包含以下字段：
            - path: 完整路径字符串
            - type: 类型，"file" 或 "dir"
            - size: 文件大小（字节），仅文件有此字段
            - name: 文件名或目录名
        没有文件则返回空列表 []
    """
    use_dir_path = dir_path if dir_path else os.getcwd()
    # 检测参数
    if not os.path.exists(use_dir_path):
        raise FileNotFoundError(f"{use_dir_path} 不存在")
    if not os.path.isdir(use_dir_path):
        raise NotADirectoryError(f"{use_dir_path} 不是一个目录")
    # 初始化列表
    rt_li = []
    # 定义递归函数来遍历目录及其子目录
    async def traverse_directory(path, current_depth=0):
        # 如果设置了深度限制且当前深度超过限制，则停止递归
        if current_depth > depth:
            return
        for f in os.listdir(path):
            full_path = os.path.join(path, f).replace('\\', '/')
            is_file = os.path.isfile(full_path)
            is_dir = os.path.isdir(full_path)
            # 根据 search_mode 添加文件或目录到结果列表
            if search_mode == 'file' and is_file:
                if file_type is None or f.endswith(file_type):
                    file_size = os.path.getsize(full_path)
                    rt_li.append({
                        "path": full_path,
                        "type": "file",
                        "size": file_size,
                        "name": f
                    })
            elif search_mode == 'dir' and is_dir:
                rt_li.append({
                    "path": full_path,
                    "type": "dir",
                    "name": f
                })
            elif search_mode == 'all':
                if is_file:
                    if file_type is None or f.endswith(file_type):
                        file_size = os.path.getsize(full_path)
                        rt_li.append({
                            "path": full_path,
                            "type": "file",
                            "size": file_size,
                            "name": f
                        })
                elif is_dir:
                    rt_li.append({
                        "path": full_path,
                        "type": "dir",
                        "name": f
                    })
            # 如果是目录且还需要继续递归，则递归遍历
            if is_dir and current_depth < depth:
                await traverse_directory(full_path, current_depth + 1)
    # 开始遍历，初始深度为 0
    await traverse_directory(use_dir_path)
    return rt_li


@sys_mcp_server.tool()
async def create_dir(dir_path: str) -> str:
    r""" 创建目录，不是创建文件
    参数：
        dir_path: 需要创建的目录，例如D:/test ，注意，路径分割符为 / 不要使用 \ 
    返回：
        True 创建成功，如果目录已经存在也返回 True，失败会抛出异常
    """
    if os.path.exists(dir_path):
        return f"目录<{dir_path}>已经存在"
    else:
        os.makedirs(dir_path)
        return f"目录<{dir_path}>创建成功"


@sys_mcp_server.tool()
async def create_file(full_file_name: str, content: str, coding='utf-8') -> str:
    r"""向文件 full_file_name 中写入 content 内容，编码格式为 coding，会自动创建不存在的文件
    参数：
        full_file_name: 必选参数，文件的完整路径，例如D:/test/test.txt；如果文件不存在，则创建文件；注意，路径分割符为 / 不要使用 \
        content: 必选参数，文件内容，写入内容会覆盖掉原文件内容
        coding: 文件编码，默认为 utf-8
    返回：
        成功返回写入文件信息（content标签内容），失败应该会直接抛出异常
    """
    # 获取文件所在的目录路径
    dir_path = os.path.dirname(full_file_name)
    # 如果目录不存在，则创建目录 使用 exist_ok=True 避免并发时的竞争条件
    if not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)  # ✅ 添加 exist_ok    # 写入文件内容
    with open(full_file_name, 'w', encoding=coding) as f:
        f.write(content)
    return f"<content>{content}</content> 写入文件 {full_file_name} 成功"


@sys_mcp_server.tool()
async def get_file_content(full_file_name: str, startline: int = 1, endline: int = -1, coding='utf-8') -> str:
    r""" 获取指定文件的内容
    参数：
        full_file_name: 文件的完整路径，例如D:/test/test.txt，注意，路径分割符为 / 不要使用 \ 
        startline: 开始行位置，默认为 1，表示从第一行开始
        endline: 结束行位置(包含这行内容)，默认为 -1，表示读取到文件末尾，如果 endline < startline 则返回空字符串
        coding: 文件编码，默认为 utf-8
    返回：
        文件的内容，空文件会返回空值
    """
    # 检查路径是否存在且是否为文件
    if not os.path.exists(full_file_name):
        raise FileNotFoundError(f"{full_file_name} 不存在")
    if not os.path.isfile(full_file_name):
        raise FileNotFoundError(f"{full_file_name} 不是一个文件")
    # 校验 startline 和 endline
    if endline > 0 and endline < startline:
        return ""
    # 读取文件内容
    with open(full_file_name, 'r', encoding=coding) as file:
        lines = file.readlines()
        # 校验 startline 和 endline 是否在有效范围内
        if startline < 1:
            startline = 1
        if endline == -1 or endline > len(lines):
            endline = len(lines)
        # 返回指定行范围的内容
        return ''.join(lines[startline-1 : endline])


@sys_mcp_server.tool()
def over_task():
    """ 结束对话，无参数，无返回值 """
    pass


# 运行服务器
if __name__ == "__main__":
    # 添加错误处理来诊断问题
    try:
        # import sys
        # print("🚀 正在启动 MCP 服务器...", file=sys.stderr)
        # print(f"Python 版本: {sys.version}", file=sys.stderr)
        # print(f"工作目录: {os.getcwd()}", file=sys.stderr)
        # 使用 run() 方法启动 MCP 服务器,指定 stdio 传输方式
        sys_mcp_server.run(transport="stdio")
    except Exception as e:
        import sys
        import traceback
        print(f"❌ MCP 服务器启动失败: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
