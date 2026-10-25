"""Exact totals stay fresh even when host tools mutate files outside upload locks."""
import os
from pathlib import Path

import pytest
from security import storage_usage as usage


def test_exact_scan_counts_overlapping_roots_once_and_observes_mutations(tmp_path, monkeypatch):
    root = tmp_path / "account"
    nested = root / "sessions" / "one"
    nested.mkdir(parents=True)
    file = nested / "history.jsonl"
    file.write_bytes(b"history")
    (nested / "scratch.tmp").write_bytes(b"ignore")
    (nested / "db-shm").write_bytes(b"ignore")
    (nested / "db-wal").write_bytes(b"wal")
    monkeypatch.setattr(usage, "_paths", lambda *a: [root, nested, file, tmp_path / "missing"])
    assert usage.measure(None, "fixture") == 10
    file.write_bytes(b"new history")
    assert usage.measure(None, "fixture") == 14
    file.unlink()
    assert usage.measure(None, "fixture") == 3


def test_scan_never_reads_contents_or_resolves_each_file(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    for number in range(1000):
        (directory / str(number)).write_bytes(b"x")
    monkeypatch.setattr(usage, "_paths", lambda *a: [directory])
    def unexpected(*a, **kw):
        raise AssertionError("quota scan must only use cached directory metadata")
    monkeypatch.setattr(Path, "resolve", unexpected)
    monkeypatch.setattr(Path, "read_bytes", unexpected)
    monkeypatch.setattr(Path, "read_text", unexpected)
    assert usage.measure(None, "fixture") == 1000


def test_symlinks_do_not_charge_or_traverse_another_accounts_files(tmp_path, monkeypatch):
    owned, other = tmp_path / "owned", tmp_path / "other"
    owned.mkdir(); other.mkdir()
    (owned / "mine").write_bytes(b"own")
    (other / "secret").write_bytes(b"foreign")
    try:
        (owned / "foreign").symlink_to(other, target_is_directory=True)
        (owned / "foreign-file").symlink_to(other / "secret")
    except OSError:
        pytest.skip("creating symlinks requires platform permission")
    monkeypatch.setattr(usage, "_paths", lambda *a: [owned])
    assert usage.measure(None, "fixture") == 3


def test_unreadable_directory_is_not_reported_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "_paths", lambda *a: [tmp_path])
    def denied(*a, **kw):
        raise PermissionError("fixture inaccessible")
    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(PermissionError):
        usage.measure(None, "fixture")
