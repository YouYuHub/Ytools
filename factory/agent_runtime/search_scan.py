"""Demand-driven content search. A cursor resumes scanning, not a background thread."""
from itertools import islice
import re
import time

from .permission_runtime import guard_path
from .tool_control import check_tool_control
from util.tool_arguments import int_arg, number_arg, string_arg


class Scan:
    def __init__(self, args):
        from . import builtin_tools as bt
        self.bt = bt
        self.root = bt._resolve_dir_path(args.get('dir_path') or '.')
        guard_path(self.root)
        pattern = string_arg(args, 'pattern', required=True, max_length=8192)
        if not pattern:
            raise ValueError('pattern 不能为空')
        self.pattern = pattern
        self.is_regex = bt._optional_bool({'is_regex': args.get('is_regex', args.get('isregex', False))}, 'is_regex')
        ignore_case = bt._optional_bool(args, 'ignore_case')
        if self.is_regex:
            from util.bounded_regex import compile_pattern
            self.regex = compile_pattern(pattern, ignore_case=ignore_case)
        else:
            self.regex = re.compile(re.escape(pattern), re.IGNORECASE if ignore_case else 0)
        self.context = int_arg(args, 'context_lines', 0, maximum=5)
        self.name = string_arg(args, 'file_pattern').strip()
        size = number_arg(args, 'max_file_mb', 2, maximum=16)
        self.max_bytes = min(bt._SEARCH_HARD_FILE_MAX_BYTES, max(1, int(size * 1024 * 1024))) if size else bt._SEARCH_HARD_FILE_MAX_BYTES
        self.maximum = int_arg(args, 'max_scanned_files', 3000, minimum=1, maximum=20000)
        self.remaining = number_arg(args, 'max_scan_seconds', 10, minimum=0.1, maximum=120)
        self.depth = int_arg(args, 'max_depth', 6, maximum=128)
        self.defaults = bt._optional_bool(args, 'no_default_excludes')
        exclusions = args.get('exclude_dirs')
        if exclusions is not None and (not isinstance(exclusions, list) or any(not isinstance(x, str) or not x for x in exclusions)):
            raise ValueError('exclude_dirs 必须为目录名字符串数组')
        self.versions = {self.root: bt._file_version(self.root)}
        self.metadata = dict(success=True, path=bt._display_path(self.root), pattern=self.regex.pattern,
            files_scanned=0, files_checked=0, files_matched=0, total_matches=0, total_blocks=0,
            skipped_large=0, skipped_unreadable=0, skipped_permission=0, scan_complete=False,
            scan_truncated=False, capture_truncated=False, scan_pending=True, total_matches_exact=False,
            max_depth=self.depth, no_default_excludes=self.defaults, exclude_dirs=list(exclusions or []))
        self.walk = bt._walk_entries(self.root, self.depth, exclusions,
            lambda _: self.skip('skipped_unreadable'), lambda _: self.skip('skipped_permission'),
            self.defaults, bt._optional_bool(args, 'respect_gitignore'), self.versions)
        self.iterator = self.generate()
        self.exhausted = False
        self.buffer_chars = 0
        self.deadline = 0

    def skip(self, field):
        self.metadata[field] += 1

    def stopped(self):
        check_tool_control()
        if time.monotonic() >= self.deadline or len(self.versions) >= 20000:
            self.metadata['scan_truncated'] = True
            return True
        return False

    def next(self):
        started = time.monotonic()
        self.deadline = started + self.remaining
        try:
            return next(self.iterator)
        except StopIteration:
            self.exhausted = True
            self.metadata['scan_pending'] = False
            self.metadata['scan_complete'] = not any(self.metadata[x] for x in
                ('scan_truncated', 'skipped_large', 'skipped_unreadable', 'skipped_permission', 'capture_truncated'))
            self.metadata['total_matches_exact'] = self.metadata['scan_complete']
            self.buffer_chars = 0
            return None
        finally:
            self.remaining = max(0, self.remaining - (time.monotonic() - started))
            self.metadata['_prefix'] = ('[search_files] 已扫描 %s 个文本文件，命中 %s 处%s' % (
                self.metadata['files_scanned'], self.metadata['total_matches'],
                '；扫描待续，按 next_cursor 继续' if self.metadata['scan_pending'] else
                '；部分文件未扫描或被跳过，不能判定目标不存在' if not self.metadata['scan_complete'] else '；扫描完整'))
            self.metadata['_prefix'] += '\n%s: %s' % ('正则' if self.is_regex else '文本',self.pattern[:80])
            if self.metadata['skipped_large']:
                self.metadata['_prefix'] += '\n[跳过 %s 个超过单文件大小上限的文件]' % self.metadata['skipped_large']
            if self.metadata['scan_truncated']:
                self.metadata['_prefix'] = '[扫描未完成：达到文件数或时间上限；不能判定不存在。]\n' + self.metadata['_prefix']
            if not self.is_regex and not self.metadata['total_matches'] and any(x in self.pattern for x in ('|','.*','\\d','\\s')):
                self.metadata['_prefix'] += '\n[如需正则匹配，请显式传 is_regex=true]'

    def close(self):
        self.iterator.close()
        self.walk.close()
        self.buffer_chars = 0
        self.exhausted = True

    def generate(self):
        bt = self.bt
        for path, kind in self.walk:
            if self.stopped():
                return
            if kind == 'file' and path.name.startswith('.') and not self.defaults:
                continue
            if not guard_path(path, search=True):
                self.skip('skipped_permission'); continue
            try:
                self.versions.setdefault(path, bt._file_version(path))
            except OSError:
                self.skip('skipped_unreadable'); continue
            if kind != 'file' or self.name and not bt._path_matches(path, self.root, self.name):
                continue
            if self.metadata['files_checked'] >= self.maximum:
                self.metadata['scan_truncated'] = True
                return
            self.metadata['files_checked'] += 1
            try:
                data = bt._read_bytes_with_limit(path, self.max_bytes)
            except ValueError:
                self.skip('skipped_large'); continue
            except OSError:
                self.skip('skipped_unreadable'); continue
            if bt._is_probably_binary(data):
                continue
            decoded, _ = bt._decode_bytes_used(data)
            stamp = self.versions[path]
            bt._assert_file_version(path, stamp['stat'] if isinstance(stamp,dict) else stamp)
            self.metadata['files_scanned'] += 1
            self.buffer_chars = len(decoded)
            lines = bt._iter_text_lines(decoded)
            prefix, ahead, offset, file_matched = [], [], 0, False
            while True:
                core = ahead + list(islice(lines, 200 - len(ahead)))
                if not core:
                    break
                ahead = list(islice(lines, self.context))
                hits = []
                for i, line in enumerate(core):
                    if self.stopped():
                        return
                    try:
                        found = self.regex.search(line, timeout=min(.25, max(.001,self.deadline-time.monotonic()))) if self.is_regex else self.regex.search(line)
                    except TimeoutError:
                        self.metadata['scan_truncated'] = True
                        return
                    if found:
                        hits.append(len(prefix) + i)
                if hits:
                    if not file_matched:
                        self.metadata['files_matched'] += 1
                        file_matched = True
                    self.metadata['total_matches'] += len(hits)
                    blocks = bt._format_match_block(path, prefix + core + ahead, hits, self.context, offset-len(prefix))
                    self.metadata['total_blocks'] += len(blocks)
                    for block in blocks:
                        yield block
                prefix = core[-self.context:] if self.context else []
                offset += len(core)
            self.buffer_chars = 0


def execute(args):
    from . import query_pages
    scan = Scan(args)
    try:
        return query_pages.create(args, [], scan.metadata, scan.versions, producer=scan)
    except BaseException:
        scan.close()
        raise
