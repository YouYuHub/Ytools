"""Atomic private JSON writes resilient to short Windows file occupation."""
import json
import os
from pathlib import Path
import time
import uuid

RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.5, 2.0, 3.0)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                return
            except OSError as exc:
                occupied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                if not occupied or attempt == len(RETRY_DELAYS):
                    raise
                time.sleep(RETRY_DELAYS[attempt])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup must not turn a committed write into an ambiguous failure.
            pass
