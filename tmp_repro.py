import asyncio
import os
import sys
sys.path.insert(0, os.getcwd())
from util.mcp_client import call_mcp_tool


async def repro_main():
    try:
        # res = await call_mcp_tool('write_file_lines', {
        #     'full_file_name': './history_files/lines_write.txt',
        #     "content": """第一行\n第二行\n第三行\n第四行\n第五行\n第六行\n第七行\n第八行\n第九行\n第十行""",
        #     "start_line": 5,
        #     "end_line": 5
        # })
        # res = await call_mcp_tool('write_file_lines', {
        #     # 这里是函数的参数字典，比如 'a': 10, b: 20
        # })
        # res = await call_mcp_tool('execute_command', {
        #     # 这里是函数的参数字典，比如 'a': 10, b: 20
        #     "command": "dir"
        # }, mcp_service="mcp_server/RunCmd.exe")
        # res = await call_mcp_tool('setup_pipe', {
        res = await call_mcp_tool('run_pipe_command', {
            # setup_pipe 工具：启动命名管道服务并创建终端会话
            # 参数说明：
            #   pipe_name     - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
            #   terminal_mode - 终端命令风格，如 "cmd.exe /k chcp 65001" 或 "powershell.exe"，默认 "cmd.exe /k chcp 65001"
            #   first_command - 可选，首条要执行的命令，如 "dir" 或 "ls"
            "pipe_name": r"\\.\pipe\default_server",
            "terminal_mode": r"cmd.exe /k chcp 65001",
            # "first_command": "for /l %i in (1,1,10) do (echo count %i & timeout /t 1 /nobreak >nul)",
            "command": "for /l %i in (1,1,10) do (echo count %i￥ pts & timeout /t 1 /nobreak >nul)",
            # "wait_milliseconds": 80,
            "prompt": "pts4|6￥",
            # "first_command": "ls -lh",
        }, mcp_service=r"C:\Users\Administrator\Desktop\C++学习录\MCP\MCPshell\x64\Release\PipeIpcMCP.exe")
        # res = await call_mcp_tool('read_pipe_output', {
        #     # setup_pipe 工具：启动命名管道服务并创建终端会话
        #     # 参数说明：
        #     #   pipe_name     - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
        #     #   terminal_mode - 终端命令风格，如 "cmd.exe /k chcp 65001" 或 "powershell.exe"，默认 "cmd.exe /k chcp 65001"
        #     #   first_command - 可选，首条要执行的命令，如 "dir" 或 "ls"
        #     "pipe_name": r"\\.\pipe\default_test_server",
        #     # "terminal_mode": r"cmd.exe /k chcp 65001",
        #     # "first_command": "for /l %i in (1,1,10) do (echo count %i & timeout /t 1 /nobreak >nul)",
        #     # "command": "for /l %i in (1,1,10) do (echo count %i￥ pts & timeout /t 1 /nobreak >nul)",
        #     "max_length": 15000,
        #     "offset": 1500,
        #     # "wait_milliseconds": 5000,
        #     # "prompt": "pts|6￥",
        #     # "first_command": "ls -lh",
        # }, mcp_service=r"mcp_server\PipeIpcMCP.exe")
        # res = await call_mcp_tool('setup_pipe', {
        #     # setup_pipe 工具：启动命名管道服务并创建终端会话
        #     # 参数说明：
        #     #   pipe_name     - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
        #     #   terminal_mode - 终端命令风格，如 "cmd.exe /k chcp 65001" 或 "powershell.exe"，默认 "cmd.exe /k chcp 65001"
        #     #   first_command - 可选，首条要执行的命令，如 "dir" 或 "ls"
        #     "pipe_name": r"\\.\pipe\default_test_server",
        #     # "terminal_mode": r"powershell",
        #     "terminal_mode": r"cmd",
        #     "first_command": "for /l %i in (1,1,10) do (echo count %i & timeout /t 1 /nobreak >nul)",
        #     # "command": "for /l %i in (1,1,10) do (echo count %i￥ pts & timeout /t 1 /nobreak >nul)",
        #     # "max_length": 15000,
        #     # "offset": 1500,
        #     # "wait_milliseconds": 0,
        #     # "prompt": "pts|6￥",
        #     # "first_command": "ls -lh",
        # }, mcp_service=r"C:\Users\Administrator\Desktop\C++学习录\MCP\MCPshell\x64\Release\PipeIpcMCP.exe")
        print('RESULT:', res)
    except Exception as e:
        print(f"error: {e}")
        # import traceback
        # traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(repro_main())
