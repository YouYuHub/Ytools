"""Scoped gitignore rules. Never read ignore files outside the authorized search tree."""
from pathlib import Path
import hashlib


def unchanged(path, version):
    """Ignore files are small; hash them too on coarse-timestamp filesystems."""
    from .builtin_tools import _file_version
    if not isinstance(version, dict):
        return _file_version(path) == version
    if _file_version(path) != version['stat']:
        return False
    with path.open('rb') as handle:
        raw = handle.read(65537)
    return (len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == version['ignore_hash']
            and _file_version(path) == version['stat'])


class IgnoreRules:
    def __init__(self, root, versions, onerror, onpermission):
        from pathspec import GitIgnoreSpec
        self.spec_type = GitIgnoreSpec
        self.root = root
        self.versions = versions
        self.onerror = onerror
        self.onpermission = onpermission
        self.rules = {}
        self.bytes = 0
        self.skipped = 0

    def enter(self, directory):
        from .builtin_tools import _file_version
        from .permission_runtime import guard_path
        parent = self.rules.get(directory.parent, ()) if directory != self.root else ()
        path = directory / '.gitignore'
        local = None
        if path.exists():
            if not guard_path(path, search=True):
                if self.onpermission:
                    self.onpermission(path)
                return False
            try:
                version = _file_version(path)
                with path.open('rb') as handle:
                    raw = handle.read(65537)
                self.bytes += len(raw)
                if len(raw) > 65536 or self.bytes > 1024 * 1024:
                    raise ValueError('ignore rules exceed the search budget')
                lines = raw.decode('utf-8-sig').splitlines()
                if len(lines) > 2000:
                    raise ValueError('too many ignore rules')
                local = self.spec_type.from_lines(lines)
                if _file_version(path) != version:
                    raise ValueError('ignore file changed during read')
                if self.versions is not None:
                    self.versions[path] = {'stat': version, 'ignore_hash': hashlib.sha256(raw).hexdigest()}
            except (OSError, ValueError, UnicodeError) as exc:
                if self.onerror:
                    self.onerror(exc)
                return False
        self.rules[directory] = (*parent, (directory, local)) if local else parent
        return True

    def ignored(self, path, is_dir=False):
        decision = False
        for base, spec in self.rules.get(path.parent, ()):
            relative = path.relative_to(base).as_posix() + ('/' if is_dir else '')
            result = spec.check_file(relative)
            if result.include is not None:
                decision = result.include
        self.skipped += bool(decision)
        return decision
