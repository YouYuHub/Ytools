"""worker 进程机制冒烟：spawn → ping/pong 事件回传 → 优雅 shutdown。

验证 IPC 管道、reader 线程、进程生命周期管理，不依赖真实模型；
完整生成链路（含 chdir）需配合服务端真实请求验证。
"""
import asyncio
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

import factory.session_worker as sw


async def main():
    proxy = sw.SessionWorkerProxy("worker_smoke_test")
    events = []
    original_loop = asyncio.get_running_loop()
    proxy.set_loop(original_loop)

    def on_event(evt):
        events.append(evt)

    proxy.add_handler(on_event)
    try:
        started_at = time.time()
        proxy._ensure_process()
        assert proxy.is_alive(), "worker 进程应存活"
        print(f"worker 已启动（{time.time() - started_at:.1f}s）")

        # ping/pong 回环：命令经 cmd Pipe 进入 worker，事件经 evt Pipe 回到 reader
        assert proxy._send({"type": "ping"}), "ping 命令应发送成功"
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(e.get("type") == "pong" for e in events):
                break
            await asyncio.sleep(0.1)
        assert any(e.get("type") == "pong" for e in events), "应收到 worker 的 pong 事件"
        print("PASS: ping/pong IPC 回环")

        # stop 无任务时也应回 task_done(no_task)，供主进程收尾等待
        events.clear()
        proxy.request_stop(reason="user")
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(e.get("type") == "task_done" for e in events):
                break
            await asyncio.sleep(0.1)
        assert any(e.get("type") == "task_done" for e in events), "无任务 stop 也应回 task_done"
        print("PASS: 空闲 stop 合成收尾事件")

        # 优雅关停
        proxy.shutdown()
        assert not proxy.is_alive(), "shutdown 后进程应退出"
        print("PASS: 优雅关停")
    finally:
        proxy.terminate("smoke cleanup")
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
