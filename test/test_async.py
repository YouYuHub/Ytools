import asyncio
import random
import time

# 模拟一个异步任务，例如网络请求或数据库查询
async def fetch_data(task_id: int):
    delay = random.uniform(0.5, 2.0)
    print(f"任务 {task_id} 开始，预计耗时 {delay.__round__(3)} 秒")
    await asyncio.sleep(delay)          # 模拟异步 I/O
    result = f"任务 {task_id} 的结果 (耗时 {delay:.3f}s)"
    print(f"任务 {task_id} 完成")
    return result


async def main():
    # 并发启动 5 个任务
    tasks = [fetch_data(i) for i in range(1, 6)]
    # gather 会并发执行所有任务，并等待全部完成，返回结果列表（顺序与传入顺序一致）
    results = await asyncio.gather(*tasks)
    print("\n=== 所有结果 ===")
    for res in results:
        print(res)



# 运行主协程
if __name__ == "__main__":
    # 基准测试，运行时间应该不超过 2.5 秒
    start_time = time.perf_counter()
    asyncio.run(main())
    print(f"总耗时 {time.perf_counter() - start_time:.3f} 秒")