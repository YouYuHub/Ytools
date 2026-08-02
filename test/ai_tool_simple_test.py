"""
测试 ai_tool 装饰器的简化版本 - 直接存储 OpenAI API 格式
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from tool_decorator import ai_tool, get_tools, clear_tools


# 测试 1: 最简单的用法
# @ai_tool
def add(a: int, b: int) -> int:
    """两个数相加"""
    return a + b


# 测试 2: 自定义工具名称
# @ai_tool("custom_multiply")
def multiply(x: int, y: int) -> int:
    """两个数相乘"""
    return x * y


# 测试 3: 带默认值的参数
# @ai_tool
def greet(name: str, greeting: str = "你好") -> str:
    """向某人打招呼"""
    return f"{greeting}, {name}!"


# 测试 4: 返回列表的函数
# @ai_tool
def get_user_list(limit: int = 5) -> list:
    """获取用户列表"""
    return [f"用户{i}" for i in range(limit)]


def test_tools():
    """测试所有工具"""
    print("=" * 60)
    print("测试 ai_tool 装饰器 - OpenAI API 格式存储")
    print("=" * 60)
    
    # 获取工具列表
    print("\n1️⃣ 获取工具列表 (get_tools()):")
    print("-" * 60)
    tools_list = get_tools()
    
    import json
    print(json.dumps(tools_list, indent=2, ensure_ascii=False))
    
    # 打印工具数量
    print(f"\n工具总数：{len(tools_list)}")
    
    # 验证格式
    print("\n2️⃣ 验证工具格式:")
    print("-" * 60)
    for i, tool in enumerate(tools_list, 1):
        print(f"工具 {i}:")
        print(f"  type: {tool.get('type')}")
        print(f"  function.name: {tool.get('function', {}).get('name')}")
        print(f"  function.description: {tool.get('function', {}).get('description')}")
        print(f"  function.parameters: {json.dumps(tool.get('function', {}).get('parameters'), ensure_ascii=False, indent=4)}")
        print()
    
    # 测试原函数调用
    print("\n3️⃣ 测试原函数调用:")
    print("-" * 60)
    print(f"add(5, 3) = {add(5, 3)}")
    print(f"multiply(4, 6) = {multiply(4, 6)}")
    print(f"greet('张三') = {greet('张三')}")
    print(f"greet('李四', 'Hello') = {greet('李四', 'Hello')}")
    print(f"get_user_list(3) = {get_user_list(3)}")
    
    # 清空工具
    print("\n4️⃣ 测试清空工具:")
    print("-" * 60)
    print(f"清空前工具数量：{len(get_tools())}")
    clear_tools()
    print(f"清空后工具数量：{len(get_tools())}")
    
    print("\n" + "=" * 60)
    print("✅ 所有测试完成!")
    print("=" * 60)


if __name__ == "__main__":
    test_tools()
