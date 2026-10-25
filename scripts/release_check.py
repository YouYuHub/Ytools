"""One local/CI validation gate. Uses temporary test accounts, never live deployment.

python scripts/release_check.py --profile quick
python scripts/release_check.py --profile release
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
QUICK_TESTS = ["test/test_storage_usage_scan.py", "test/test_history_pages.py", "test/test_account_storage.py",
               "test/test_share_account_isolation.py", "test/test_security_resources.py", "test/test_security_quotas.py",
               "test/test_api_send_performance.py", "test/test_frontend_asset_versions.py", "test/test_release_check.py"]


def source_digest(root=ROOT):
    items = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in {".git", ".vs", "node_modules", "__pycache__", ".pytest_cache",
                                                      "history_files", "tmp", "log", "setting", "browser", ".venv", ".venv-security", "venv", ".playwright-mcp"})
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix in {".py", ".js", ".cjs", ".html", ".css", ".scss", ".json", ".yml", ".yaml"} or name.startswith("requirements"):
                items.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    for relative in ("browser/package.json", "browser/package-lock.json"):
        path = root / relative
        if path.is_file():
            items.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(items, ensure_ascii=True).encode()).hexdigest()


def prerequisites():
    if importlib.util.find_spec("pytest") is None:
        raise RuntimeError("Install test dependencies: python -m pip install -r requirements-dev.txt")
    node, npm = shutil.which("node"), shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not node or not npm:
        raise RuntimeError("Node.js and npm are required; browser checks cannot be skipped")
    probe = "const fs=require('fs');require('./H5/node_modules/jsdom');const p=require('./browser/node_modules/playwright');if(!fs.existsSync(p.chromium.executablePath()))process.exit(2);"
    result = subprocess.run([node, "-e", probe], cwd=ROOT, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Install H5/browser dependencies and Playwright Chromium; see docs/release_validation.md")
    return node, npm


def plan(profile, report, *, python=sys.executable, npm="npm"):
    backend = [python, "-m", "pytest", *(QUICK_TESTS if profile == "quick" else ["test"]),
               "--ignore=test/test_web_page_flows.py", "-q", "--tb=short", "-o", "faulthandler_timeout=90",
               "--junitxml=" + str(report / "backend.xml")]
    browser = [python, "-m", "pytest", "test/test_web_page_flows.py", "-q", "--tb=short",
               "--junitxml=" + str(report / "browser.xml")]
    if profile == "quick":
        browser += ["-k", "history_pages or account_switch or stream_reload or account_return"]
    return [("backend", backend), ("frontend", [npm, "test", "--prefix", "H5"]),
            ("browser", browser), ("storage-benchmark", [python, "scripts/benchmark_storage_usage.py", "--files", "1000", "--reference"])]


def test_summary(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    return {"tests": len(cases), "skipped": sum(case.find("skipped") is not None for case in cases),
            "failed": sum(case.find("failure") is not None or case.find("error") is not None for case in cases),
            "backup_restore_verified": any(case.get("name") == "test_restored_fixture_account_can_login_and_read_history_without_source"
                                           and all(case.find(tag) is None for tag in ("failure", "error", "skipped")) for case in cases)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("quick", "release"), default="quick")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = (args.report_dir or ROOT / "tmp" / "validation" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    if args.dry_run:
        print(json.dumps({"profile": args.profile, "stages": plan(args.profile, report)}, ensure_ascii=True))
        return 0
    report.mkdir(parents=True, exist_ok=False)
    result = {"format": 1, "profile": args.profile, "full_validation": args.profile == "release", "passed": False,
              "platform": sys.platform, "python": sys.version.split()[0],
              "started": datetime.now(timezone.utc).isoformat(), "stages": []}
    try:
        _, npm = prerequisites()
        before = source_digest()
        environment = dict(os.environ, YTOOLS_REQUIRE_BROWSER_TESTS="1", PYTHONIOENCODING="utf-8")
        for name, command in plan(args.profile, report, npm=npm):
            print("Validating " + name + " ...", flush=True)
            started = time.monotonic()
            with (report / (name + ".log")).open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            stage = {"name": name, "passed": completed.returncode == 0, "exit_code": completed.returncode,
                     "seconds": round(time.monotonic()-started, 3), "log": name + ".log"}
            result["stages"].append(stage)
            if name in {"backend", "browser"} and not (report / (name + ".xml")).exists():
                stage["passed"] = False
                stage["error"] = "Required test report was not generated"
            if name in {"backend", "browser"} and (report / (name + ".xml")).exists():
                stage.update(test_summary(report / (name + ".xml")))
                if name == "browser" and (stage["skipped"] or stage["tests"] < (4 if args.profile == "quick" else 19)):
                    stage["passed"] = False
                if name == "backend" and not stage["backup_restore_verified"]:
                    stage["passed"] = False
            if name == "storage-benchmark" and stage["passed"]:
                stage["measurement"] = json.loads((report / stage["log"]).read_text(encoding="utf-8"))
            print(name + (": PASS" if stage["passed"] else ": FAIL"), flush=True)
        result["source_unchanged"] = before == source_digest()
        result["source_digest"] = before
        result["passed"] = result["source_unchanged"] and all(stage["passed"] for stage in result["stages"])
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, ET.ParseError) as error:
        result["error"] = str(error)
    finally:
        (report / "summary.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(("PASS " if result["passed"] else "FAIL ") + str(report / "summary.json"), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
