"""生成轮次写令牌（write lease）：让「停止」不必等旧任务真正退出。

设计背景
--------
旧实现的安全模型是「等旧任务退出，所以它不会写」（`_wait_worker_idle` 的
15 秒门 + `require_dead_writer` 的 PID 存活判断）。这带来一个硬约束：停止
接口的响应时间被最慢的资源回收（内置工具 20s 陪等、MCP 工具 300s 超时、
worker 收尾）绑死，用户感知几秒到几十秒。

新模型是「旧任务无权写，所以不用等」（权限保证）：

- 每次生成本持有一个 `RoundLease`，随 round 起始创建；
- 所有历史写入口在写前校验：lease 已撤销 → 丢弃写入（不落盘、不报错）；
- 用户停止 = `revoke()`，一瞬间完成，不等待任何东西；
- 之后旧任务迟到返回（例如 300 秒后才回来的 MCP 工具）拿不到写权限，
  物理上无法污染新轮次，也就不需要主进程阻塞等它。

跨进程
------
生成跑在 spawn 出的 worker 子进程（`CHAT_WORKER_MODE=process` 为默认），
内存锁/事件不共享。因此撤销状态需要落盘：

- 每个会话一个侧车 `<session>_chat.jsonl.lease`，含 epoch、revoked、stamped_by；
- **派发戳**：主进程在 `proxy.start_generation()` 之前写入一次
  （`stamped_by="dispatch"`），覆盖 pre-run 窗口（进程启动、工具发现、
  前置压缩）——这段里点的停止也要能拦住后续写入；
- **收养**：worker 进到生成循环时把派发戳收养为自己的通行证
  （改写 `stamped_by="round"`，epoch 不变）；
- **撤销**：主进程只读侧车里的 epoch 并置 revoked=1；
- **inline 模式**：无派发戳，直接用 `stamp_round_lease()` 新建一轮的戳。

所以 worker 进程写历史前（`_assert_lease_writable`）读侧车即可知道本轮是否
已被撤销；主进程停止时不需要知道 worker 里任何对象。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RoundAbandoned(RuntimeError):
    """本轮写令牌已撤销（用户停止 / 被新任务接管）：本次写入应被丢弃。

    这是**控制流信号**，不是错误：调用方（工具线程、子 agent、收尾逻辑）
    捕获后安静退出即可，不得写历史、不得提示用户。
    """


# ---------- 跨进程侧车 ----------

_LEASE_SUFFIX = ".lease"
# stamped_by 取值
_STAMP_DISPATCH = "dispatch"   # 主进程派发前写入，等 worker 收养
_STAMP_ROUND = "round"         # 已被某一轮认领（worker 收养后 / inline 自建）


def lease_path_for(chat_file: Path) -> Path:
    """会话 JSONL → lease 侧车路径（与 .pending / .outbox 同目录同风格）。"""
    chat_file = Path(chat_file)
    return chat_file.with_name(chat_file.name + _LEASE_SUFFIX)


def _write_lease_file(path: Path, epoch: str, *, revoked: bool,
                      reason: str = "", stamped_by: str = _STAMP_ROUND,
                      created: float | None = None) -> None:
    """原子写 lease 侧车（临时文件 + os.replace）。写失败不影响主流程。"""
    payload = {
        "epoch": epoch,
        "revoked": bool(revoked),
        "reason": reason,
        "stamped_by": stamped_by,
        "created": float(created if created is not None else time.time()),
        "updated": time.time(),
        "pid": os.getpid(),
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_lease_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clear_lease_file(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


@dataclass
class RoundLease:
    """一轮生成的历史写权限。

    - `epoch`：轮次代次（uuid），用于幂等/对账与跨进程识别；
    - `revoked`：撤销标记（线程安全，可跨 worker 线程检查）；
    - `revoked_reason`：便于日志定位（user_stop / superseded / worker_exit）；
    - `path`：跨进程侧车路径（worker 模式下必需，inline 模式允许为 None）。
    """

    session_id: str
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    revoked_reason: str = ""
    path: Path | None = None
    _created: float = field(default_factory=time.time)
    _revoked: threading.Event = field(default_factory=threading.Event)

    # ---- 生命周期 ----
    def activate(self) -> None:
        """把本轮登记为「当前可写」（覆盖上一轮遗留的撤销标记）。"""
        self._revoked.clear()
        self.revoked_reason = ""
        self._created = time.time()
        if self.path is not None:
            _write_lease_file(self.path, self.epoch, revoked=False,
                              stamped_by=_STAMP_ROUND, created=self._created)

    # ---- 状态 ----
    def revoked(self) -> bool:
        return self._revoked.is_set()

    def assert_writable(self) -> None:
        """写前校验；已撤销则抛 RoundAbandoned（调用方应安静丢弃）。"""
        if self._revoked.is_set():
            raise RoundAbandoned(
                f"轮次 {self.epoch} 写令牌已撤销（{self.revoked_reason or 'unknown'}），写入被丢弃"
            )

    def revoke(self, reason: str = "user_stop") -> bool:
        """撤销写权限。返回是否由本次调用完成撤销（幂等）。"""
        if self._revoked.is_set():
            return False
        self.revoked_reason = reason
        self._revoked.set()
        if self.path is not None:
            _write_lease_file(self.path, self.epoch, revoked=True,
                              reason=reason, created=self._created)
        return True


def check_lease_file(path: Path, epoch: str | None) -> bool:
    """跨进程判定：该 epoch 是否已被撤销（worker 侧写前调用）。

    返回 True 表示已被撤销（禁止写入）。epoch 为 None（未启用 lease 的旧路径）
    或侧车不存在时返回 False，保持旧行为。
    """
    if not epoch:
        return False
    data = read_lease_file(path)
    if not data:
        return False
    if data.get("epoch") != epoch:
        # 侧车属于其它轮次（新轮已接管）：本纪元写入同样应该禁止，
        # 否则旧任务迟到返回会写进新轮的历史
        return True
    return bool(data.get("revoked"))


def revoke_lease_file(path: Path, reason: str = "user_stop") -> bool:
    """跨进程撤销：读侧车现有 epoch 并置 revoked=1（主进程停止接口用）。

    worker 模式下 lease 对象不共享（各进程独立），主进程只知道会话 ID。
    派发/开轮时 epoch 已写入侧车，这里读回来再标记撤销即可——epoch 保持一致，
    worker 下次写前就会看到 revoked。

    返回 True 表示侧车存在撤销标记（含本来就已撤销的幂等场景）。
    """
    data = read_lease_file(path)
    epoch = (data or {}).get("epoch")
    if not epoch:
        # 还没派发过（没有侧车/无 epoch）：无可撤销
        return False
    if data and data.get("revoked"):
        return True  # 已撤销（幂等）
    _write_lease_file(
        path, epoch, revoked=True, reason=reason,
        stamped_by=str((data or {}).get("stamped_by") or _STAMP_ROUND),
        created=(data or {}).get("created"),
    )
    return True


def stamp_dispatch_lease(path: Path, session_id: str = "") -> RoundLease:
    """主进程派发前打戳：写入全新、未撤销、标为 dispatch 的 epoch。"""
    lease = RoundLease(session_id=session_id, path=path)
    _write_lease_file(path, lease.epoch, revoked=False,
                      stamped_by=_STAMP_DISPATCH, created=lease._created)
    return lease


def stamp_round_lease(path: Path, session_id: str = "") -> RoundLease:
    """开新轮时打戳（inline 模式 / 无派发戳可收养时的兜底）。

    注意与 dispatch 戳的区分：只有 `stamped_by == "dispatch"` 的侧车才会被
    worker 收养。否则上一轮遗留的撤销标记会被误当成“本轮已被停止”，
    导致停止后下一轮无法生成。
    """
    lease = RoundLease(session_id=session_id, path=path)
    _write_lease_file(path, lease.epoch, revoked=False,
                      stamped_by=_STAMP_ROUND, created=lease._created)
    return lease


def adopt_dispatch_lease(path: Path, session_id: str = "") -> RoundLease | None:
    """worker 开跑时收养主进程的派发戳（仅限 stamped_by=dispatch）。

    - 无侧车 / 不是 dispatch 戳 → None（调用方自建新轮）；
    - dispatch 戳且未撤销 → 收养（epoch 不变），并改写为 round 消费掉；
    - dispatch 戳但已撤销 → 返回**已撤销** lease，调用方应直接结束生成。
    """
    data = read_lease_file(path)
    if not data or data.get("stamped_by") != _STAMP_DISPATCH:
        return None
    epoch = str(data.get("epoch") or "")
    if not epoch:
        return None
    created = data.get("created")
    lease = RoundLease(session_id=session_id, epoch=epoch, path=path)
    try:
        lease._created = float(created) if created is not None else time.time()
    except (TypeError, ValueError):
        lease._created = time.time()
    if data.get("revoked"):
        lease.revoked_reason = str(data.get("reason") or "user_stop")
        lease._revoked.set()
        return lease
    # 消费派发戳，避免被下一轮误收养
    _write_lease_file(path, lease.epoch, revoked=False,
                      stamped_by=_STAMP_ROUND, created=lease._created)
    return lease


def new_lease(session_id: str, path: Path | None = None) -> RoundLease:
    return RoundLease(session_id=session_id, path=path)
