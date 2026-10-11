"""Standalone POSIX supervisor. No application imports; survives worker exit."""
import json
import subprocess
import sys
from pathlib import Path


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    code = 127
    with open(spec["output"], "ab", buffering=0) as output:
        try:
            child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdout=output,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            code = child.wait()
        except Exception as exc:
            output.write((str(exc) + "\n").encode("utf-8", errors="replace"))
        finally:
            Path(spec["done"]).write_text(str(code), encoding="utf-8")


if __name__ == "__main__":
    main()
