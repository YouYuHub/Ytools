import os
import requests
# import json
from datetime import datetime

# 你的 API Key（脱敏：从环境变量 BAIDU_QIANFAN_API_KEY 读取；
# 历史提交 8e6dffa 之前曾内置真实密钥，已占位化，建议到百度千帆控制台轮换）
api_key = os.environ.get("BAIDU_QIANFAN_API_KEY", "your-baidu-api-key")

# 搜索关键词
query = "人工智能最新进展"

# 请求URL和Headers
url = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
headers = {
    "X-Appbuilder-Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}
# 请求体，resource_type_filter里指定了只取前5条网页结果
payload = {
    "messages": [{"role": "user", "content": query}],
    "resource_type_filter": [{"type": "web", "top_k": 5}],
    "search_source": "baidu_search_v1",
    # "model": "ernie-3.5-8k"
    "model": "ernie-4.5-turbo-32k",
    # "search_source": "baidu_search_v2",
    # "resource_type_filter": [{"type": "web","top_k": 10}]
}

try:
    start_time = datetime.now()
    response = requests.post(url, headers=headers, json=payload)
    print(f"请求耗时：{datetime.now() - start_time}")
    result = response.json()

    # 提取搜索结果前5条
    if 'references' in result:
        print(f"【{query}】的搜索结果（前5条）：\n")
        for idx, item in enumerate(result['references'][:5], 1):
            print(f"{idx}. 标题: {item.get('title', '无标题')}")
            print(f"   链接: {item.get('url', '无链接')}")
            print(f"   摘要: {item.get('abstract', '无摘要')[:100]}...")
            print("-" * 50)
    else:
        print("未能获取到搜索结果。", result)

except requests.exceptions.RequestException as e:
    print(f"API 请求失败: {e}")
