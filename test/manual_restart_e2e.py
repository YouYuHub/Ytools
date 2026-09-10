# -*- coding: utf-8 -*-
"""restart_service 全链路人工验证脚本（不自动执行破坏性动作）。

用法：
    python test/manual_restart_e2e.py            # 检查模式：链路健康检查
    python test/manual_restart_e2e.py --dry-run  # 干跑模式：走到 pipeline 前一步停止
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 按文件路径加载（与单测一致的隔离方式）
import importlib.util

_MOD_PATH = PROJECT_ROOT / "mcp_server" / "restart_tools_server.py"
_SPEC = importlib.util.spec_from_file_location("restart_e2e_mod", _MOD_PATH)
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

LOCK_DIR = PROJECT_ROOT / "history_files" / "lock"


def _step(title: str, ok: bool, detail: str = "") -> bool:
    mark = "OK " if ok else "FAIL"
    line = f"[{mark}] {title}" + (f" - {detail}" if detail else "")
    print(line)
    return ok


def main() -> int:
    all_ok = True
    print("=" * 70)
    print("restart_service 全链路检查（不执行任何杀/启动作）")
    print("=" * 70)

    # 1. service_state.json 存在且 pid 存活
    state = _mod._read_service_state()
    if state is None:
        all_ok = _step("service_state.json 存在", False,
                       "main.py 未写快照：请先启动一次服务（python main.py）")
    else:
        all_ok = _step(
            "service_state.json 存在", True,
            f"pid={state.get('pid')} port={state.get('port')} root={state.get('project_root')}")
        alive = _mod._pid_alive(int(state.get("pid") or 0))
        all_ok = _step("快照 pid 存活", alive) and all_ok

    # 2. helper 存在且可执行（语法层面已由本脚本 import 主模块验证）
    helper = PROJECT_ROOT / "restart_helper.py"
    all_ok = _step("restart_helper.py 存在", helper.exists()) and all_ok
    server = PROJECT_ROOT / "mcp_server" / "restart_tools_server.py"
    all_ok = _step("restart_tools_server.py 存在", server.exists()) and all_ok

    # 3. 冷却 / pending 状态摘要
    remaining = _mod._cooldown_remaining()
    pending = LOCK_DIR / "restart_pending.json"
    done = LOCK_DIR / "restart_done.json"
    print()
    print(f"当前冷却剩余: {remaining:.0f}s")
    print(f"调度中(pending): {'是（注入未完成或流水线未消费）' if pending.exists() else '否'}")
    if done.exists():
        try:
            print(f"上次结果(done): {done.read_text(encoding='utf-8')}")
        except OSError:
            pass
    print()
    print("人工验证步骤（需在真机上做，脚本不自动执行）：")
    print("  1. 启动服务: python main.py（确认打印 '服务快照已写入'）")
    print("  2. 用户在工具选择中勾选 RestartMcp 分组（restart_service 等 3 个工具）")
    print("  3. 发消息让模型调用 restart_service（follow_up 写一句下一步任务）")
    print("  4. 工具返回 scheduled 后静置 30-60s：")
    print("     - 前端刷新可见会话收到 follow-up 用户消息并自动开新轮")
    print("     - read history_files/lock/restart_done.json 应 ok=true 且 injected=true")
    print("     - history_files/lock/restart_pending.json 应已删除")
    print("  5. 异常通道观察: history_files/lock/restart_helper.log 与 restart_pipeline.log")
    print("  6. 反悔窗口: 派发后 25s 内可让模型调用 restart_cancel 撤销")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
