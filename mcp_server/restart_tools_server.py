# -*- coding: utf-8 -*-
"""restart_service 工具的 MCP server：开发期自驱动重启 + 会话续任务（调度侧）。

与 main.py（写 service_state.json 快照）和项目根 restart_helper.py（恢复执行）
三方配合，整体链路：

    模型调用 restart_service（本文件，MCP 子进程）
        1. 校验（follow-up 非空 / service_state 存在 / pid 存活 / 冷却期 / 无残留 pending）
        2. 写 history_files/lock/restart_pending.json（session_id/follow_up/port/
           project_root/python_exe + 时间戳）与 restart_pipeline.cmd
        3. 以 DETACHED+NEW_PROCESS_GROUP+BREAKAWAY 分离进程派发 pipeline 后立即返回
    pipeline（独立 cmd 进程，脱离 Ytools 进程树）：
        ping 等待 delay_seconds（给当前轮次 JSONL 落盘留窗口）
            → taskkill /F /T /PID <主进程>（树杀主进程 + spawn worker，防孤儿）
            → 启动 restart_helper.py（等端口释放 → 分离启动 main.py → 健康轮询
              → POST /chat_with_tool 注入 follow-up 用户消息 → 写结果文件）

为什么必须「先返回、延迟杀」：工具结果在当轮结束才追加进会话 JSONL，此刻
立即杀进程会让本轮（助手文本 + 工具结果）整轮丢失，新进程拼历史时缺失上下文。

为什么 taskkill 必须 /T 树杀：生成循环在每会话独立 worker（multiprocessing
spawn daemon 子进程）里运行，Windows 下不在 Job 内，只杀主进程会留下孤儿
worker，与新进程的「读全量→原子替换」JSONL 快照互相覆盖丢数据。

follow-up 只发一条 user 消息即可：/chat_with_tool 默认 use_backend_history=true，
服务端从会话 JSONL 拼接全部已完成轮次，模型天然继承原任务上下文。

产物文件（都在 history_files/lock/）：
    service_state.json   main.py 启动时写的快照（pid/port/project_root/python_exe）
    restart_pending.json 调度后写入；helper 注入成功后删除（重启成功的权威标志）
    restart_pipeline.cmd 分离执行的命令流水线（幂等派发，重跑无害）
    restart_done.json    helper 写的最终结果（模型可用 read_file 查证）
    restart_helper.log   helper 全程日志；restart_pipeline.log 为 pipeline 日志
    restart_cooldown.json 冷却状态；restart_cancel 可删 pending 撤销未执行的调度
"""
# 标准库
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    # mcp 2.x：FastMCP 更名为 MCPServer（API 兼容，平替改名）
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    # mcp 1.x：旧的 fastmcp 模块
    from mcp.server.fastmcp import FastMCP

# 本工具默认监听端口（与 main.py 的 uvicorn 启动一致；正式入口是 service_state.json）
_DEFAULT_PORT = 48621

# 路径锚点：本文件位于 <Ytools根>/mcp_server/ 下，parents[1] 即项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOCK_DIR = _PROJECT_ROOT / "history_files" / "lock"

# 调度冷却：模型连续调用时拒绝第二次（防反复重启循环），60s 内仅调度一次
_COOLDOWN_SECONDS = 60.0
# 残留 pending 的最大可信年龄：超过则视为上次失败的脏数据，允许覆盖重排
_PENDING_STALE_SECONDS = 300.0
# follow-up 文本长度上限（防模型把整块上下文塞进来）
_FOLLOW_UP_MAX_CHARS = 500
# 延迟下限：再短就接不住"工具结果落盘"这一个必经窗口
_DELAY_MIN_SECONDS = 10.0
_DELAY_MAX_SECONDS = 180.0
_DELAY_DEFAULT_SECONDS = 25.0

restart_mcp_server = FastMCP("restart-mcp-server")


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------

def _read_service_state() -> Optional[dict]:
    """读取 main.py 启动时写的 service_state.json；缺失/损坏返回 None。"""
    path = _LOCK_DIR / "service_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001 - 防御性读取，坏文件按缺失处理
        return None


def _pid_alive(pid: Any) -> bool:
    """探活（OpenProcess 非破坏性探测，不向目标发信号；进程存在返回 True）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # 打开失败但错误码为拒绝访问 → 进程存在（通常是系统保护进程）
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    except Exception:  # noqa: BLE001
        return False


def _cooldown_remaining() -> float:
    """读取冷却剩余秒数（<=0 表示不在冷却期）。"""
    path = _LOCK_DIR / "restart_cooldown.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = float(data.get("last_schedule", 0.0))
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, _COOLDOWN_SECONDS - (time.time() - last))


def _mark_scheduled() -> None:
    """记录本次调度时间（供冷却判断）。"""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOCK_DIR / "restart_cooldown.json"
    try:
        path.write_text(
            json.dumps({"last_schedule": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _write_json_atomic(path: Path, payload: dict) -> None:
    """先写临时文件再 os.replace，避免读到半截 JSON。"""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# 核心调度实现（可单元测试，无副作用入口都在这里）
# ----------------------------------------------------------------------

def _validate_inputs(
    session_id: Any,
    follow_up: Any,
    delay_seconds: Any,
) -> tuple[Optional[float], Optional[str]]:
    """入参校验；返回 (delay_seconds 规整值, 错误消息)。错误时 delay 为 None。"""
    session_text = str(session_id or "").strip()
    if not session_text:
        return None, "session_id 不能为空（重启后需要往该会话注入 follow-up）"
    if len(session_text) > 100:
        return None, f"session_id 过长（{len(session_text)} > 100）"
    if not isinstance(follow_up, str) or not follow_up.strip():
        return None, "follow_up 不能为空：重启成功后将以该文本作为用户消息继续任务"
    if len(follow_up) > _FOLLOW_UP_MAX_CHARS:
        return None, (
            f"follow_up 过长（{len(follow_up)} > {_FOLLOW_UP_MAX_CHARS} 字符）："
            "请精炼为一句可直接开始的续任务指令"
        )
    try:
        delay = float(delay_seconds) if delay_seconds is not None else _DELAY_DEFAULT_SECONDS
    except (TypeError, ValueError):
        return None, f"delay_seconds 必须是数字，收到 {delay_seconds!r}"
    if not (_DELAY_MIN_SECONDS <= delay <= _DELAY_MAX_SECONDS):
        return None, (
            f"delay_seconds 超出范围（{delay:g}s，允许 {_DELAY_MIN_SECONDS:g}-"
            f"{_DELAY_MAX_SECONDS:g}s）：太小则工具结果来不及落盘，太大则用户干等"
        )
    return delay, None


def _build_pending(
    session_text: str,
    follow_up: str,
    delay: float,
) -> tuple[Optional[dict], Optional[str]]:
    """依据 service_state.json 构建 pending 负载；返回 (pending, 错误消息)。"""
    state = _read_service_state()
    if state is None:
        return None, (
            "未找到 service_state.json（history_files/lock/ 下）：主进程可能未写入"
            "启动快照，请确认 main.py 已包含重启快照逻辑并重启过一次服务"
        )
    try:
        pid = int(state["pid"])
        port = int(state.get("port") or _DEFAULT_PORT)
        project_root = str(state["project_root"])
        python_exe = str(state.get("python_exe") or sys.executable)
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"service_state.json 字段损坏（{exc}），无法定位主进程"
    if not _pid_alive(pid):
        return None, (
            f"service_state.json 中的主进程 pid={pid} 已不存活：快照过期"
            "（服务可能已被手动重启过），请在重启快照刷新后再试"
        )
    return {
        "session_id": session_text,
        "follow_up": follow_up,
        "port": port,
        "project_root": project_root,
        "python_exe": python_exe,
        "old_pid": pid,
        "delay_seconds": delay,
        "scheduled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled_epoch": time.time(),
    }, None


def _build_pipeline_cmds(pending_path: Path, pending: dict) -> list[str]:
    """生成 restart_pipeline.cmd 的逐行命令（保持经得起重复派发的幂等性）。"""
    delay = float(pending.get("delay_seconds") or _DELAY_DEFAULT_SECONDS)
    old_pid = int(pending.get("old_pid") or 0)
    # 延迟等待用 ping 实现（timeout /nobreak 在无交互 stdin 的分离进程里不可靠）
    ticks = max(2, int(delay) + 1)
    return [
        "@echo off",
        f"rem ytools restart pipeline / old_pid={old_pid} / port={pending.get('port')}",
        "chcp 65001 >nul",
        f"ping -n {ticks} 127.0.0.1 >nul",
        f"taskkill /F /T /PID {old_pid}",
        f'"{pending.get("python_exe")}" "{Path(__file__).resolve().parent.parent / "restart_helper.py"}"'
        f' --port {pending.get("port")} --pending "{pending_path}"',
    ]


def _write_pipeline_cmd(cmd_path: Path, lines: list[str]) -> None:
    """按 UTF-8（无 BOM）保存：cmd 第 3 行 chcp 65001 后，后续行按 UTF-8 解码，
    与 code page 对齐才能正确携带中文路径（project_root 常含中文）。"""
    cmd_path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def _dispatch_pipeline(cmd_path: Path, log_path: Path) -> tuple[Optional[int], Optional[str]]:
    """以分离进程一次性派发 pipeline cmd；返回 (pid, 错误消息)。

    CREATE_BREAKAWAY_FROM_JOB 在发起进程的 Job 不允许 breakaway 时
    CreateProcess 直接返回 ERROR_ACCESS_DENIED（WinError 5），因此按
    restart_helper._popen_detached 的同一策略逐组降级重试：首选
    BREAKAWAY 组合，失败后退回 DETACHED+NEWPG（仍脱离主进程树，
    只是留在原 Job 里，不再被主进程的 job-kill 连坐）。
    """
    flag_combos = [0]
    if os.name == "nt":
        flag_combos = [
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB,
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        ]
    last_error: Optional[OSError] = None
    for creation_flags in flag_combos:
        try:
            with log_path.open("ab") as handle:
                process = subprocess.Popen(
                    ["cmd", "/c", str(cmd_path)],
                    cwd=str(_PROJECT_ROOT),
                    creationflags=creation_flags,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            return process.pid, None
        except OSError as exc:
            last_error = exc
            continue
    return None, f"派发重启 pipeline 失败: {last_error}"


def _restart_service_impl(
    session_id: Any,
    follow_up: Any,
    delay_seconds: Any = None,
) -> str:
    """restart_service 工具主实现：校验 → 写 pending/cmd → 分离派发 → 返回说明。"""
    delay, error = _validate_inputs(session_id, follow_up, delay_seconds)
    if error:
        return json.dumps({"ok": False, "reason": error}, ensure_ascii=False)
    session_text = str(session_id).strip()

    # 冷却：防模型循环调度
    remaining = _cooldown_remaining()
    if remaining > 0:
        return json.dumps({
            "ok": False,
            "reason": (
                f"距上次重启调度仅 {remaining:.0f}s（冷却期 {_COOLDOWN_SECONDS:g}s）："
                "请等待 服务实际完成重启后再评估是否需要再次调度"
            ),
        }, ensure_ascii=False)

    # 残留 pending：短时间内视为 already_scheduled；太旧视为脏数据直接覆盖
    pending_path = _LOCK_DIR / "restart_pending.json"
    if pending_path.exists():
        try:
            age = time.time() - pending_path.stat().st_mtime
        except OSError:
            age = _PENDING_STALE_SECONDS + 1
        if age <= _PENDING_STALE_SECONDS:
            return json.dumps({
                "ok": False,
                "reason": (
                    f"调度中（already_scheduled）：restart_pending.json 写于 {age:.0f}s 前，"
                    "重启流水线已在执行；如需撤销请调用 restart_cancel"
                ),
            }, ensure_ascii=False)
        # 精确处理：超过 stale 视为脏数据，允许覆盖重排

    pending, error = _build_pending(session_text, str(follow_up).strip(), delay)
    if error:
        return json.dumps({"ok": False, "reason": error}, ensure_ascii=False)

    try:
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return json.dumps({"ok": False, "reason": f"创建锁目录失败: {exc}"}, ensure_ascii=False)

    # 清掉旧结果文件，保证本次调度结果可精确辨新
    for stale in ("restart_done.json",):
        stale_path = _LOCK_DIR / stale
        try:
            stale_path.unlink()
        except OSError:
            pass

    _write_json_atomic(pending_path, pending)
    cmd_path = _LOCK_DIR / "restart_pipeline.cmd"
    _write_pipeline_cmd(cmd_path, _build_pipeline_cmds(pending_path, pending))
    pid, error = _dispatch_pipeline(cmd_path, _LOCK_DIR / "restart_pipeline.log")
    if pid is None:
        # 派发失败立刻撤回 pending/cmd，避免留下假调度态
        # （残留 .cmd 内含 taskkill，被误手跑会错杀主进程）
        for stale_path in (pending_path, cmd_path):
            try:
                stale_path.unlink()
            except OSError:
                pass
        return json.dumps({"ok": False, "reason": error}, ensure_ascii=False)
    _mark_scheduled()

    return json.dumps({
        "ok": True,
        "status": "scheduled",
        "main_pid": pending["old_pid"],
        "new_port": pending["port"],
        "delay_seconds": delay,
        "message": (
            f"重启流水线已派发（pipeline pid={pid}）：{delay:g}s 后将强制终止主进程树"
            "（含会话 worker），随后自动拉起新进程并注入 follow-up 为用户消息；"
            "本次调用即刻返回，请立刻结束本轮响应（不要继续调用其它工具），"
            f"收尾落盘后耐心等待。重启结果可读 {(_LOCK_DIR / 'restart_done.json')}"
        ),
        "pending": str(pending_path),
        "log": str(_LOCK_DIR / "restart_helper.log"),
    }, ensure_ascii=False)


def _restart_cancel_impl() -> str:
    """撤销尚未执行的调度：删 pending 与 pipeline cmd（冷却自然过期）。"""
    results = []
    for name in ("restart_pending.json", "restart_pipeline.cmd"):
        path = _LOCK_DIR / name
        try:
            path.unlink()
            results.append(f"已删除 {name}")
        except FileNotFoundError:
            results.append(f"{name} 不存在")
        except OSError as exc:
            results.append(f"{name} 删除失败: {exc}")
    return json.dumps({
        "ok": True,
        "status": "cancelled",
        "details": results,
    }, ensure_ascii=False)


def _restart_status_impl() -> str:
    """只读查询：当前调度/执行状态汇总（供模型或人工排障）。"""
    info: dict[str, Any] = {
        "cooldown_remaining": round(_cooldown_remaining(), 1),
        "state": _read_service_state(),
    }
    for key, name in (("pending", "restart_pending.json"), ("done", "restart_done.json")):
        path = _LOCK_DIR / name
        if path.exists():
            try:
                info[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                info[key] = f"<读取失败: {path}>"
        else:
            info[key] = None
    return json.dumps(info, ensure_ascii=False)


# ----------------------------------------------------------------------
# MCP 工具注册
# ----------------------------------------------------------------------

@restart_mcp_server.tool()
async def restart_service(
    session_id: str,
    follow_up: str,
    delay_seconds: float = _DELAY_DEFAULT_SECONDS,
) -> str:
    """授权的重启服务工具：强制重启 Ytools 主进程，重启完成后以 follow_up 作为用户消息继续当前会话任务。

    这是用户在工具选择中显式勾选授权的开发运维工具（权限粒度=勾选）。

    工作方式（异步流水线，本调用立即返回）：
        1. 写入调度文件后派发独立的重启流水线进程；
        2. delay_seconds 秒后，Ytools 主进程与其全部会话 worker 被强制终止；
        3. 独立 helper 自动拉起新进程、等健康检查通过后，把 follow_up 作为
           该会话的最新用户消息注入，服务端自动拼接全部历史继续任务。

    使用要求：
        - 仅当代码/依赖/配置修改需要重启才生效时使用（改配置文件走热重载即可，不要重启）；
        - 调用前请把当前进展结论、修改摘要、下一步计划写入 follow_up（<=500 字符），
          这段文本将以用户身份出现在会话里；可写"这是重启后的继续指令：..."；
        - 调用后立即结束本轮：不要再调用任何工具，也不要继续生成长文；
        - 结果查询：read_file 读取 restart_done.json（成功后 pending 文件会被删除）。

    参数：
        session_id:    要继续的会话 ID（重启后注入 follow_up 的目标会话），通常为当前会话
        follow_up:     重启完成后注入的用户消息（一句话续任务指令，<=500 字符）
        delay_seconds: 调度到强制终止的延迟秒数（10-180，默认 25；确认本轮已收尾
                       且无未落盘写入时可适当调小）
    返回：
        JSON：ok=true 表示已派发（此刻进程尚未被杀）；ok=false 给出 reason，请先按
        reason 处理而非直接重试。
    """
    import asyncio
    return await asyncio.to_thread(
        _restart_service_impl, session_id, follow_up, delay_seconds)


@restart_mcp_server.tool()
async def restart_cancel() -> str:
    """撤销尚未执行的重启调度（删除 restart_pending.json 与流水线命令文件）。

    适用：restart_service 返回 ok=true 后、进程尚未被杀前的反悔窗口；
    已进入 helper 恢复阶段（pending 文件已被消费）时无法撤销。

    返回：
        JSON：ok=true 表示清理动作已执行（details 逐项说明）。
    """
    import asyncio
    return await asyncio.to_thread(_restart_cancel_impl)


@restart_mcp_server.tool()
async def restart_status() -> str:
    """查询重启调度与执行状态（只读）：含冷却剩余、pending/done 内容、主进程快照。

    返回：
        JSON：state=service_state.json 快照，pending/done 为相应文件内容或 null。
    """
    import asyncio
    return await asyncio.to_thread(_restart_status_impl)


if __name__ == "__main__":
    try:
        restart_mcp_server.run(transport="stdio")
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"❌ MCP 服务器启动失败: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
