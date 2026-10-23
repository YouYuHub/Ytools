"""Small process adapter: ConPTY on Windows, controlling PTY on POSIX."""
import os
import signal
import subprocess
import sys
import threading
import time


class Terminal:
    def __init__(self, argv, cwd):
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
            self.child = PtyProcess.spawn(argv, cwd=cwd, dimensions=(30, 120))
        else:
            import pty
            import fcntl
            import termios
            import struct
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 120, 0, 0))
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
    def __init__(self, argv, cwd, interactive):
        self.child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL, bufsize=0)
        self.pid = self.child.pid

    def poll(self):
        return self.child.poll()

    def write(self, chars):
        data = memoryview(chars.encode('utf-8'))
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
