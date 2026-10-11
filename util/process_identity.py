"""Identify a process incarnation before signalling a durable background job."""
import os
import subprocess
import sys
from pathlib import Path


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    return f"win:{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
            finally:
                kernel.CloseHandle(handle)
        elif sys.platform.startswith("linux"):
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:{boot}:{fields[19]}"
        else:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                                    capture_output=True, text=True, timeout=2)
            value = result.stdout.strip()
            if result.returncode == 0 and value and not value.split()[-1].startswith("Z"):
                return "posix:" + " ".join(value.split()[:-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None
