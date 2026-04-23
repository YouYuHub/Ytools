import asyncio
import os
import sys
sys.path.insert(0, os.getcwd())
from util.mcp_client import call_mcp_tool


async def main():
    try:
        res = await call_mcp_tool('write_file_lines', {
            'full_file_name': './history_files/lines_write.txt',
            "content": """第一行\n第二行\n第三行\n第四行\n第五行\n第六行\n第七行\n第八行\n第九行\n第十行""",
            "start_line": 5,
            "end_line": 5
        })
        # res = await call_mcp_tool('write_file_lines', {
        #     # 这里是函数的参数字典，比如 'a': 10, b: 20
        # })
        print('RESULT:', res)
    except Exception as e:
        print(f"error: {e}")
        # import traceback
        # traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())
