"""Small process adapter: ConPTY on Windows, controlling PTY on POSIX."""
import os
import signal
import subprocess
import sys
import threading
import time


def resolve_stdin_codec(*, shell_kind='', console_forced_utf8: bool, child_self_forces_utf8: bool) -> str:
    """交互式 stdin 字节编码：与子进程实际解码口径对齐，禁止写死。

    - cmd：永远走地区传统 OEM 页。cmd 在 65001 下读管道 stdin 会把非 ASCII
      字节逐个变成 U+FFFD（实测硬伤），因此 cmd 会话控制台固定传统 OEM 页，
      stdin 同口径编码；超集字符发不出去，由 write() 报明确错误（指引
      powershell 会话/tty 模式），而不是静默发成乱码。
    - 其余 shell：UTF-8 优先（监督进程把共享控制台切到 65001 并校验；
      lease ps1 自带 UTF-8 prelude 自强制）。两者都不成立时
      （如无控制台监督进程拉起的常驻 PowerShell，子进程新建 OEM 控制台），
      回退系统 OEM 代码页编码。
    """
    if os.name != 'nt':
        return 'utf-8'
    from util.text_encoding import oem_console_codec, traditional_oem_codec
    if (shell_kind or '') == 'cmd':
        return traditional_oem_codec()
    if console_forced_utf8 or child_self_forces_utf8:
        return 'utf-8'
    return oem_console_codec()


def stdin_env_vars(codec: str) -> dict:
    """交互式子进程的 stdin 编解码环境（setdefault 语义，不覆盖用户显式配置）。

    只设 PYTHONIOENCODING、不设 PYTHONUTF8：前者精确跟随 stdin 字节编码
    （cmd 会话是 GBK 等 OEM 页、PS 会话是 UTF-8），后者会强制 UTF-8 而破坏
    cmd 会话的一致性。python 读管道 stdin 用 locale/ANSI 而非控制台代码页，
    非 UTF-8 地区不对齐就是乱码。
    """
    return {'PYTHONIOENCODING': codec or 'utf-8'}


class Terminal:
    def __init__(self, argv, cwd, rows=30, cols=120):
        self.output_error = None
        self.last_output = time.monotonic()
        if os.name == 'nt':
            try:
                from winpty import PtyProcess
            except ImportError as exc:
                raise ValueError('TTY 需要 pywinpty；请安装项目依赖后重启服务') from exc
            # A blocking native read can prevent Python's lifetime guard from
            # running for a completely silent child. Poll inside pywinpty's
            # reader thread; our socket reader still blocks without busy loops.
            os.environ['PYWINPTY_BLOCK'] = '0'
            self.child = PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols))
        else:
            import pty
            import fcntl
            import termios
            import struct
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            try:
                self.child = subprocess.Popen(argv, cwd=cwd, stdin=slave, stdout=slave,
                    stderr=slave, preexec_fn=controlling_terminal, close_fds=True)
                self.master = master
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
        self.pid = self.child.pid
        self.reader = threading.Thread(target=self._copy_output, daemon=True)
        self.reader.start()

    def _copy_output(self):
        try:
            while True:
                data = self.child.read(4096).encode('utf-8') if os.name == 'nt' else os.read(self.master, 16384)
                if not data:
                    if os.name == 'nt':
                        continue  # pywinpty's nonblocking no-data marker.
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                self.last_output = time.monotonic()
        except (OSError, EOFError):
            pass
        except Exception as exc:
            self.output_error = str(exc)

    def poll(self):
        if os.name == 'nt':
            return None if self.child.isalive() else self.child.exitstatus
        return self.child.poll()

    def write(self, chars):
        if os.name == 'nt':
            self.child.write(chars)
        else:
            data = memoryview(chars.encode('utf-8'))
            while data:
                data = data[os.write(self.master, data):]

    def close_input(self):
        raise ValueError('PTY 不支持关闭单独的 stdin')

    def terminate(self):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(self.pid), '/T', '/F'], capture_output=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def finish(self):
        deadline = time.monotonic() + 2
        while self.reader.is_alive() and time.monotonic() < deadline:
            self.reader.join(0.05)
            if time.monotonic() - self.last_output > 0.25 and self.poll() is not None:
                break
        if os.name == 'nt':
            import socket
            try:
                self.child.fileobj.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.child.fileobj.close()
            self.child._server.close()
            self.child.closed = True
        else:
            os.close(self.master)
        self.reader.join(0.25)


class Pipe:
    def __init__(self, argv, cwd, interactive, stdin_codec='utf-8'):
        self.child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL, bufsize=0)
        self.pid = self.child.pid
        self.stdin_codec = stdin_codec or 'utf-8'

    def poll(self):
        return self.child.poll()

    def write(self, chars):
        try:
            data = memoryview(chars.encode(self.stdin_codec))
        except (UnicodeEncodeError, LookupError) as exc:
            raise ValueError(
                f'当前终端代码页({self.stdin_codec})无法表示输入字符，发送中止：{exc}') from exc
        while data:
            data = data[self.child.stdin.write(data):]
        self.child.stdin.flush()

    def close_input(self):
        self.child.stdin.close()

    def finish(self):
        if self.child.stdin:
            try:
                self.child.stdin.close()
            except OSError:
                pass
