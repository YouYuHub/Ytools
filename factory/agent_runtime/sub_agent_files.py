"""Coordinate built-in file edits made by children of one parent task."""
import copy
import os
from pathlib import Path
from threading import RLock


class SubAgentFileAccess:
    def __init__(self, snapshot=None, save=None):
        self.files = copy.deepcopy((snapshot or {}).get("files", {}))
        self.save = save
        self.lock = RLock()

    def execute(self, agent, name, args, executor):
        if name == 'apply_patch':
            return self._patch(agent, args, executor)
        if name not in {"read_file", "write_file", "edit_file"}:
            return executor(name, args)
        raw_path = str(args.get("path") or args.get("full_file_name") or "").strip()
        if not raw_path:
            return executor(name, args)
        try:
            path = os.path.normcase(str(Path(raw_path).expanduser().resolve()))
        except (TypeError, ValueError, OSError):
            return executor(name, args)
        # File helpers execute synchronously; the lock covers hash validation and
        # mutation together, including when called by multiple worker threads.
        with self.lock:
            entry = self.files.get(path, {})
            reads = entry.get("reads", {})
            writer = entry.get("writer")
            guarded = dict(args)
            creating = name == "write_file" and (args.get("mode") == "create" or args.get("create_only"))
            if name != "read_file" and not creating:
                expected = str(args.get("expected_hash") or "").strip() or reads.get(agent)
                if writer and writer != agent and not expected:
                    return self._conflict(path, writer)
                if expected:
                    guarded["expected_hash"] = expected
            result = executor(name, guarded)
            if not isinstance(result, dict):
                return result
            if result.get("error"):
                if writer and writer != agent and "FILE_MODIFIED_EXTERNALLY" in str(result["error"]):
                    return self._conflict(path, writer)
                return result
            if not result.get("success"):
                return result
            entry = self.files.setdefault(path, {"reads": {}})
            content_hash = result.get("content_hash")
            if content_hash:
                entry["reads"][agent] = content_hash
            else:
                entry["reads"].pop(agent, None)
            if name != "read_file":
                entry["writer"] = agent
            if self.save:
                self.save({"files": self.files})
            return result

    def _patch(self, agent, args, executor):
        from .context_patch import parse
        try:
            operations = parse(args.get('patch'))
        except ValueError:
            return executor('apply_patch', args)
        with self.lock:
            expected = {}
            for op in operations:
                path = os.path.normcase(str(Path(op['path']).expanduser().resolve()))
                entry = self.files.get(path, {})
                writer = entry.get('writer')
                content_hash = entry.get('reads', {}).get(agent)
                if writer and writer != agent and not content_hash:
                    return self._conflict(path, writer)
                if content_hash:
                    expected[path] = content_hash
            result = executor('apply_patch', {**args, '_expected_hashes': expected})
            if isinstance(result, dict) and result.get('success'):
                for item in result.get('files', []):
                    path = os.path.normcase(str(Path(item['path']).resolve()))
                    entry = self.files.setdefault(path, {'reads': {}})
                    entry['writer'] = agent
                    entry['reads'][agent] = item['content_hash']
                if self.save:
                    self.save({'files': self.files})
            elif isinstance(result, dict):
                for item in result.get('files', []):
                    if item.get('status') == 'rollback_failed':
                        path = os.path.normcase(str(Path(item['path']).resolve()))
                        self.files[path] = {'writer': 'unknown_after_patch', 'reads': {}}
                if self.save:
                    self.save({'files': self.files})
            return result

    @staticmethod
    def _conflict(path, writer):
        return {
            "error": f"SUB_AGENT_FILE_CONFLICT：子任务 {writer} 已修改 {path}；"
                     "本次修改未执行。请先 read_file 读取最新内容，合并已有修改后再编辑。",
            "code": "SUB_AGENT_FILE_CONFLICT", "path": path, "conflicting_agent": writer,
        }
