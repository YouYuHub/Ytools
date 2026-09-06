# coding: utf-8
"""跨进程文件锁：基于 OS 级文件字节范围锁，进程崩溃时由操作系统自动释放。

用途：会话 JSONL 采用「读全量 → 内存改 → os.replace 原子替换」的写入方式，
生成任务迁入独立 worker 进程后，主进程（标题/删除/导入/上传记录）与
worker 进程（轮次/压缩/usage）会并发写同一文件，必须用跨进程锁互斥，
否则两个进程各自的快照会互相覆盖（丢更新）。

- Windows：msvcrt.locking（LK_NBLCK 非阻塞重试）；锁绑定在打开的文件句柄上，
  进程退出/句柄关闭自动解锁，无需担心崩溃后残留死锁。
- POSIX：fcntl.flock（LOCK_EX | LOCK_NB）。
- 锁文件本身只创建不删除：删除会引入「A 拿到旧 inode、B 新建同名文件」的竞态。
"""
# 标准库
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None  # POSIX

try:
    import fcntl
except ImportError:
    fcntl = None


@contextmanager
def cross_process_lock(lock_path: str | Path, timeout: float = 30.0, poll_interval: float = 0.02):
    """获取跨进程互斥锁；超时抛 TimeoutError。

    Args:
        lock_path: 锁文件路径（自动创建父目录与文件）
        timeout: 最长等待秒数
        poll_interval: 非阻塞重试间隔秒数
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                if msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    # LK_NBLCK：非阻塞尝试锁定 1 字节；被占用时抛 OSError
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    # 无可用锁原语的极端环境：退化为无锁（与历史行为一致）
                    break
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"获取文件锁超时（{timeout}s）：{path}"
                    )
                time.sleep(poll_interval)
        yield
    finally:
        if acquired:
            try:
                if msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


if __name__ == "__main__":
    # 自测：同进程重入两次（第二次应超时失败），随后释放可再获取
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        lock_file = Path(tmp) / "t.lock"
        with cross_process_lock(lock_file, timeout=0.3):
            try:
                with cross_process_lock(lock_file, timeout=0.3):
                    print("ERROR: 应当互斥")
            except TimeoutError:
                print("OK: 同进程二次加锁被拒绝")
        with cross_process_lock(lock_file, timeout=0.3):
            print("OK: 释放后可重新获取")
