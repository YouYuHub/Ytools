"""Disposable metadata-only quota benchmark; never reads real account data."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--reference", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.files <= 100000 or not 1 <= args.repeats <= 10:
        parser.error("files=1..100000; repeats=1..10")
    from security import storage_usage
    with tempfile.TemporaryDirectory(prefix="ytools-quota-bench-") as temporary:
        directory = Path(temporary)
        for number in range(args.files):
            (directory / str(number)).write_bytes(b"benchmark")
        def reference():
            total, seen = 0, set()
            for entry in directory.rglob("*"):
                if entry.is_symlink():
                    continue
                resolved = entry.resolve()
                if resolved in seen or not entry.is_file() or entry.name.endswith((".tmp", "-shm")):
                    continue
                seen.add(resolved)
                total += entry.stat().st_size
            return total
        result = {"files": args.files}
        with patch.object(storage_usage, "_paths", lambda *a: [directory]):
            for label, run in [("optimized", lambda: storage_usage.measure(None, "fixture")),
                               *([("reference", reference)] if args.reference else [])]:
                durations = []
                for _ in range(args.repeats):
                    started = time.perf_counter()
                    assert run() == args.files * len(b"benchmark")
                    durations.append((time.perf_counter()-started)*1000)
                result[label+"_median_ms"] = round(statistics.median(durations), 3)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
