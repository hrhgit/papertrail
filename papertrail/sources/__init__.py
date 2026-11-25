from pathlib import Path

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


def load_sources(roots, progress=None):
    """Parse every root. `progress`, if given, hears `found(name, root, paths)` for
    each source before any parsing (paths is None for a missing directory), then
    `read(name, path, nbytes)` while each log file is read, `read(name, path)` when
    it is finished, and `done(name, sessions)`."""
    plan = {}
    for name, root in roots.items():
        root = Path(root).expanduser()
        source = SOURCES[name]() if root.is_dir() else None
        plan[name] = (source, root)
        if progress:
            progress.found(name, root, source.log_files(root) if source else None)
    sessions, diagnostics = [], {}
    for name, (source, root) in plan.items():
        if source is None:
            diagnostics[name] = {"missing_directory": 1}
            continue
        tick = None
        if progress:
            def tick(path, nbytes=None, name=name):
                progress.read(name, path, nbytes)
        found = list(source.sessions(root, progress=tick))
        sessions.extend(found)
        if progress:
            progress.done(name, len(found))
        diagnostics[name] = {"failures": source.failures, "skipped": source.skipped}
        if hasattr(source, "counts"):
            diagnostics[name]["counts"] = source.counts
    return sessions, diagnostics
