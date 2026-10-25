"""Write an offline bootstrap credential with a private OS file ACL."""
import csv
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def private_file(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        # Restrict the empty file before writing any secret. No shell receives
        # the password, and an existing credential is never overwritten.
        if os.name == "nt":
            options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "check": True,
                       "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
            output = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options).stdout
            sid = next(csv.reader(output.strip().splitlines()))[-1]
            if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
                raise RuntimeError("无法确定当前 Windows 用户 SID，未写入凭证")
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid + ":F"], **options)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            yield stream, path
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
