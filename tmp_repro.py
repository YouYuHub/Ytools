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
        # }, mcp_service_file="mcp_server/RunCmd.exe")
        res = await call_mcp_tool('setup_pipe', {
            # setup_pipe 工具：启动命名管道服务并创建终端会话
            # 参数说明：
            #   pipe_name     - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
            #   terminal_mode - 终端命令风格，如 "cmd.exe /k chcp 65001" 或 "powershell.exe"，默认 "cmd.exe /k chcp 65001"
            #   first_command - 可选，首条要执行的命令，如 "dir" 或 "ls"
            "pipe_name": r"\\.\pipe\default_server",
            "terminal_mode": r"cmd.exe /k chcp 65001",
            "first_command": "ssh root@120.48.43.229",
            "wait_milliseconds": 10000,
            "prompt": "",
            # "first_command": "ls -lh",
        }, mcp_service_file=r"mcp_server\PipeCmdMCP.exe")
        # res = await call_mcp_tool('run_command', {
        #     # run_command 工具：在命名管道终端中执行命令，延续之前命令的 Shell 会话
        #     # 参数说明：
        #     #   command   - 要执行的命令（必需），如 "dir" 或 "ls"
        #     #   pipe_name - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
        #     "pipe_name": r"\\.\pipe\default_server",
        #     # "terminal_mode": r"cmd.exe /k chcp 65001",
        #     # "command": "ssh root@120.48.43.229",
        #     "command": "ls",
        #     # "first_command": "ls",
        # }, mcp_service_file="mcp_server/PipeCmdMCP.exe")
        # res = await call_mcp_tool('get_command_history', {
        #     # get_command_history 工具：获取指定命名管道的命令历史记录
        #     # 参数说明：
        #     #   pipe_name   - 命名管道路径，默认 "\\\\.\\pipe\\default_server"
        #     #   max_history - 最大获取字节数，默认 4096
        #     "pipe_name": r"\\.\pipe\default_server",
        #     # "terminal_mode": r"cmd.exe /k chcp 65001",
        #     "max_history": 500,
        #     # "next_command": "123",
        # }, mcp_service_file="mcp_server/PipeCmdMCP.exe")
        print('RESULT:', res)
    except Exception as e:
        print(f"error: {e}")
        # import traceback
        # traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(repro_main())
