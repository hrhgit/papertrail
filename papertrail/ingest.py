"""Plan and publish a project preview, followed by a complete graph."""

import json
import os
from collections import defaultdict
from pathlib import Path
from time import perf_counter

from .cache import ParseCache, stamp
from .graph import build, select
from .logfile import compressed, open_log
from .queries import Queries
from .sources import SourcePlan
from .sources.claude import ClaudeCodeSource
from .sources.codex import CodexSource


def _scan_path(path, needles):
    # Logs are UTF-8. Keep enough bytes between chunks to find a split path.
    overlap = max(map(len, needles)) - 1
    try:
        with open_log(path) as stream:
            read = getattr(stream, "read1", stream.read)
            tail = b""
            while chunk := read(256 * 1024):
                data = tail + chunk
                if any(needle in data for needle in needles):
                    return True
                tail = data[-overlap:] if overlap else b""
    except Exception as exc:
        # Optional decoders can raise their own exception. The full parser
        # reports unreadable logs in its diagnostics.
        if not compressed(path) and not isinstance(exc, OSError):
            raise
        return None
    return False


def _contains_path(name, path, spellings, needles, cache):
    # Portable logs can use a cwd relative to the transcript's folder.
    relatives = {os.path.relpath(project, path.parent) for project in spellings}
    needles += tuple(json.dumps(value)[:-1].encode() for value in sorted(relatives - {"."}))
    if cache is None or cache.directory is None:
        return _scan_path(path, needles)
    key = cache.entry(name, [path], kind="path-match", context=spellings)
    matched = cache.get(key)
    if matched is None:
        matched = _scan_path(path, needles)
        if matched is not None and key == cache.entry(name, [path], kind="path-match", context=spellings):
            cache.put(key, matched)
    return matched


def prefilter(roots, project, *, plan=None):
    """Find raw path matches, likely Claude folders, replicas and ancestors."""
    plan = plan or SourcePlan(roots, None)
    unresolved = Path(project).expanduser().absolute()
    spellings = {str(unresolved), str(unresolved.resolve())}
    spellings = sorted(spellings)
    needles = tuple({
        needle for path in spellings for needle in (path.encode(), json.dumps(path)[1:-1].encode())
    })
    encoded = {path.replace("/", "-").replace(".", "-") for path in spellings}
    matched = set()
    claude_links = defaultdict(set)
    for name, (root, paths) in plan.inventory.items():
        for path in paths or ():
            if plan.check:
                plan.check()
            if name == "claude-code":
                parts = path.relative_to(root).parts
                if len(parts) > 1 and any(
                    parts[0] == folder or parts[0].startswith(folder + "-") for folder in encoded
                ):
                    matched.add(path)
                if path.parent.name == "subagents":
                    session_dir = path.parent.parent
                    parent = session_dir.with_name(session_dir.name + ".jsonl")
                    claude_links[parent].add(path)
                    claude_links[path].add(parent)
            if path not in matched and _contains_path(name, path, spellings, needles, plan.cache):
                matched.add(path)
    headers = plan.codex_headers()
    groups = CodexSource.groups(headers)
    available = {path for _, paths in plan.inventory.values() for path in paths or ()}
    pending = list(matched)
    expanded = set()
    while pending:
        path = pending.pop()
        related = claude_links.get(path, set())
        header = headers.get(path)
        if header and header.sid not in expanded:
            expanded.add(header.sid)
            replicas = groups.get(header.sid, ())
            related = {
                *replicas,
                *(parent for replica in replicas for parent in groups.get(headers[replica].parent, ())),
            }
        for link in (related & available) - matched:
            matched.add(link)
            pending.append(link)
    return matched


def _uncached_bytes(plan):
    cache = plan.cache
    uncached = {}
    claude = ClaudeCodeSource()
    for path in plan.inventory.get("claude-code", (None, None))[1] or ():
        if plan.check:
            plan.check()
        if not cache.get(claude.cache_entry(cache, path), probe=True):
            info = stamp(path)
            uncached[path] = info[1] if info else 0
    codex = CodexSource()
    headers = plan.codex_headers()
    for paths in codex.groups(headers).values():
        if plan.check:
            plan.check()
        if not cache.get(codex.cache_entry(cache, paths, headers), probe=True):
            uncached.update((path, headers[path].stamp[1]) for path in paths)
    return uncached


def ingest(store, roots, project, *, portable=None, cache=True, quick_only=False,
           progress=None, check=None, publish=None):
    """Yield a pass name, duration and diagnostics after each publication."""
    if quick_only and project is None:
        raise ValueError("--quick-only requires a project.")
    preview = project is not None
    if preview and Path(store).is_file():
        with Queries(store) as existing:
            preview = not existing.complete
        if quick_only and not preview:
            raise ValueError("--quick-only cannot replace a complete graph. Use a different --store.")
    parsed = ParseCache(enabled=cache, project=project or Path.cwd())
    phase = "quick" if preview else "full"
    started = perf_counter()
    if progress:
        progress.begin(phase)
    plan = SourcePlan(roots, parsed, check)
    files = None
    if preview:
        uncached = _uncached_bytes(plan) if not quick_only else {}
        if quick_only or uncached:
            files = prefilter(roots, project, plan=plan)
            candidate_bytes = sum(uncached.get(path, 0) for path in files)
            if not quick_only and candidate_bytes * 2 > sum(uncached.values()):
                files = None
    if files is None and phase != "full":
        phase = "full"
        if progress:
            progress.begin(phase)
    while True:
        sessions, diagnostics = plan.load(progress, files)
        if progress:
            progress.stage("select")
        kept = select(sessions, project)
        if progress:
            progress.selected(len(sessions), sum(1 for session in kept if session.edits))
        build(store, kept, stage=progress.stage if progress else None, portable=portable,
              complete=phase == "full", publish=publish)
        if phase == "full":
            parsed.prune()
        yield phase, round(perf_counter() - started, 3), diagnostics
        if phase == "full" or quick_only:
            break
        if check:
            check()
        # Fresh inventory and sizes for logs appended or replaced during the preview.
        phase, files, started = "full", None, perf_counter()
        if progress:
            progress.begin(phase)
        plan = SourcePlan(roots, parsed, check)
