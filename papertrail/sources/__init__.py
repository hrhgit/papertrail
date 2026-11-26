from pathlib import Path

from ..cache import ParseCache

from .claude import ClaudeCodeSource
from .codex import CodexSource

SOURCES = {"claude-code": ClaudeCodeSource, "codex": CodexSource}
DEFAULT_LOGS = {"claude-code": ".claude/projects", "codex": ".codex/sessions"}


class ReadProgress:
    """Counts the log files and bytes `load_sources` has read, per source. Counts
    only: no path or content leaves this object."""

    def __init__(self):
        self.sources = {}
        self.sizes = {}
        self.partial = {}

    def begin(self, phase):
        self.sources.clear()
        self.sizes.clear()
        self.partial.clear()

    def stage(self, step, files=None):
        pass

    def selected(self, read, kept):
        pass

    def found(self, name, root, paths):
        sizes = {}
        for path in paths or ():
            try:
                sizes[path] = path.stat().st_size
            except OSError:
                sizes[path] = 0
        self.sizes[name] = sizes
        self.sources[name] = {
            "name": name,
            "root": str(root),
            "missing": paths is None,
            "files": len(sizes),
            "bytes": sum(sizes.values()),
            "read_files": 0,
            "read_bytes": 0,
            "sessions": None,
        }

    def read(self, name, path, nbytes=None):
        """`nbytes` more bytes of `path` were read, or with None, the file is done."""
        source, size = self.sources[name], self.sizes[name].get(path, 0)
        counted = self.partial.get(path, 0)
        if nbytes is None:
            source["read_files"] += 1
            source["read_bytes"] += size - counted
            self.partial.pop(path, None)
        else:
            step = max(0, min(nbytes, size - counted))
            source["read_bytes"] += step
            self.partial[path] = counted + step

    def done(self, name, sessions):
        source = self.sources[name]
        source.update(read_files=source["files"], read_bytes=source["bytes"], sessions=sessions)

    def fraction(self):
        total = sum(s["bytes"] for s in self.sources.values())
        return sum(s["read_bytes"] for s in self.sources.values()) / total if total else 1.0


class SourcePlan:
    """A file inventory and lazy Codex headers shared by filtering and parsing."""

    def __init__(self, roots, cache, check=None):
        self.cache, self.check = cache, check
        self.inventory = {}
        for name, path in roots.items():
            root = Path(path).expanduser().resolve()
            paths = SOURCES[name]().log_files(root) if root.is_dir() else None
            self.inventory[name] = root, paths
        self._headers = None

    def codex_headers(self):
        if self._headers is None:
            _, paths = self.inventory.get("codex", (None, None))
            self._headers = CodexSource().headers(paths or (), self.cache, self.check)
        return self._headers

    def load(self, progress=None, files=None):
        plan = {}
        for name, (root, paths) in self.inventory.items():
            if files is not None and paths is not None:
                paths = [path for path in paths if path in files]
            plan[name] = root, paths
            if progress:
                progress.found(name, root, paths)
        sessions, diagnostics = [], {}
        for name, (root, paths) in plan.items():
            if paths is None:
                diagnostics[name] = {"missing_directory": 1}
                continue

            def tick(path, nbytes=None, name=name):
                progress.read(name, path, nbytes)

            source = SOURCES[name]()
            if name == "codex":
                headers = self.codex_headers()
                unit = {"headers": {path: headers[path] for path in paths}}
            else:
                unit = {"paths": paths}
            found = list(source.sessions(
                root, progress=tick if progress else None, cache=self.cache, check=self.check, **unit
            ))
            sessions.extend(found)
            if progress:
                progress.done(name, len(found))
            diagnostics[name] = {
                "failures": source.failures, "skipped": source.skipped, "counts": source.counts,
            }
        return sessions, diagnostics


def load_sources(roots, progress=None, *, cache=True, project=None):
    """Parse every root and report discovered files, bytes read and session counts."""
    parsed = ParseCache(enabled=cache, project=project or Path.cwd())
    plan = SourcePlan(roots, parsed)
    result = plan.load(progress)
    parsed.prune()
    return result
