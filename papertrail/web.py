"""Local web reader over the same query API the CLI uses.

Binds to loopback only and opens the store read-only. File text is served only for
paths that are File nodes in the graph or files inside the open folder. Requests
must name a loopback host, and the two actions that change state (open a folder,
build its history) are POSTs that need a custom header, so other sites can't
trigger them. The same goes for showing the system folder dialog.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files as package_files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import relate

from .graph import build, portable_root, select
from .queries import Queries
from .sources import DEFAULT_LOGS, SOURCES, ReadProgress, load_sources
from .transcript import conversation, written_diff

STATIC = package_files("papertrail") / "static"
MAX_FILE_BYTES = 2_000_000
MAX_TREE = 20_000
MAX_DIRS = 1_000
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
# Search stops at whichever comes first, so one request can't hold the single
# serving thread for long.
SEARCH_SECONDS = 2.0
SEARCH_HITS = 400
SEARCH_FILE_HITS = 40
SEARCH_ROWS = 60
SNIPPET = 200
LEAD = 24
# Folders an editor's explorer would hide or that hold generated files.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".papertrail", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".uv-cache", ".tox", ".next", "dist",
    "build", "target",
}


def store_for(folder):
    """Where `papertrail ingest` keeps a folder's graph by default."""
    return Path(folder) / ".papertrail" / "graph.rl"

FILES = """
MATCH (f:File)
OPTIONAL MATCH (f)-[:HAS_RANGE]->(r:Range)-[:FROM]->(h:Hunk)
WITH f, sum(CASE WHEN h.kind = 'agent' THEN r.end - r.start + 1 ELSE 0 END) AS agent_lines
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:TOUCHED]->(f)
RETURN f.path AS path, f.status AS status, f.lines AS lines, agent_lines,
count(DISTINCT e) AS edits, count(DISTINCT s) AS sessions ORDER BY path"""

SESSIONS = """
MATCH (s:Session)
OPTIONAL MATCH (s)-[:HAS_EDIT]->(e:Edit)
OPTIONAL MATCH (parent:Session)-[:SPAWNED]->(s)
RETURN s.id AS id, s.agent AS agent, s.model AS model, s.title AS title,
s.started_at AS started_at, s.ended_at AS ended_at, s.cwd AS cwd, parent.id AS parent,
count(e) AS edits ORDER BY started_at, id"""

# One row per current line range. `lanes` lists every session whose edit is on the
# range's REPLACED lineage, nearest first: the lines that cross at this spot.
TRACKS = """
MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)
OPTIONAL MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
OPTIONAL MATCH (h)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
WITH r, owner, s, min(length(chain)) AS depth
ORDER BY r.start, depth
WITH r, owner, collect(CASE WHEN s IS NULL THEN NULL ELSE {session: s.id, depth: depth} END) AS lanes
RETURN r.start AS start, r.end AS end, owner.kind AS kind, owner.id AS hunk, owner.ts AS ts, lanes
ORDER BY start"""

# One row per deletion still between two current lines. `after` counts the lines
# above the spot. `wrote` lists who wrote the deleted lines and how many each.
GAPS = """
MATCH (f:File {id:$path})-[:HAS_GAP]->(g:Gap)-[:FROM]->(h:Hunk)
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h)
OPTIONAL MATCH (h)-[r:REPLACED]->(old:Hunk)
OPTIONAL MATCH (old)<-[:HAS_HUNK]-(written:Edit)<-[:HAS_EDIT]-(writer:Session)
WITH g, h, e, s, collect(CASE WHEN old IS NULL THEN NULL
  ELSE {kind: old.kind, session: writer.id, edit: written.id, ts: old.ts, lines: r.lines} END) AS wrote
RETURN g.id AS id, g.after AS after, h.id AS hunk, h.kind AS kind, h.ts AS ts,
h.removed AS removed, h.removed_text AS text, e.id AS edit, s.id AS session, wrote
ORDER BY after, ts, id"""


CONVERSATION_SESSION = """
MATCH (s:Session {id:$id})
OPTIONAL MATCH (parent:Session)-[:SPAWNED]->(s)
RETURN s.id AS id, s.agent AS agent, s.model AS model, s.title AS title, s.log_path AS log_path,
s.started_at AS started_at, s.ended_at AS ended_at, s.cwd AS cwd, s.resume AS resume,
s.resume_cwd AS resume_cwd, parent.id AS parent"""

EDIT_HUNKS = """
UNWIND $ids AS id
MATCH (e:Edit {id:id})-[:HAS_HUNK]->(h:Hunk)
RETURN e.id AS edit, h.id AS id, h.added AS added, h.removed AS removed, h.text AS text"""
CONVERSATION_EDITS = """
MATCH (s:Session {id:$id})-[:HAS_EDIT]->(e:Edit)-[:TOUCHED]->(f:File)
RETURN e.id AS id, e.call_id AS call_id, e.seq AS seq, e.ts AS ts, e.tool AS tool, f.path AS path
ORDER BY ts, seq"""

# How much of what each session wrote is still in the files.
WRITTEN = """
MATCH (s:Session)-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(h:Hunk)
RETURN s.id AS id, sum(h.added) AS written"""
ALIVE = """
MATCH (s:Session)-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(:Hunk)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(:File)
RETURN s.id AS id, sum(r.end - r.start + 1) AS alive"""
# Who replaced a session's lines, and where the ones still standing are.
REPLACED_BY = """
MATCH (s:Session {id:$id})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(:Hunk)<-[r:REPLACED]-(new:Hunk)
OPTIONAL MATCH (new)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(by:Session)
RETURN by.id AS session, new.kind AS kind, sum(r.lines) AS lines ORDER BY lines DESC"""
ALIVE_IN = """
MATCH (s:Session {id:$id})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(:Hunk)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(f:File)
RETURN f.path AS path, sum(r.end - r.start + 1) AS lines, min(r.start) AS line ORDER BY lines DESC, path"""

# Replay can lose an owner without a REPLACED edge when there is no usable
# snapshot or the old and new lines cannot be linked. Match ranges across all
# paths so owners carried by a move are still counted. Sum each set of edges
# before matching the next one so multiple ranges and replacements don't multiply.
UNACCOUNTED = """
MATCH (:Session {id:$id})-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h:Hunk)
WHERE h.added > 0
OPTIONAL MATCH (h)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(:File)
WITH e, h, coalesce(sum(r.end - r.start + 1), 0) AS alive
OPTIONAL MATCH (h)<-[r:REPLACED]-(:Hunk)
WITH e, h, alive, coalesce(sum(r.lines), 0) AS replaced
WITH e, h.added - alive - replaced AS lines
WHERE lines > 0
OPTIONAL MATCH (e)-[:TOUCHED]->(f:File)
RETURN CASE WHEN f.status IN ['missing', 'unreadable'] THEN f.status ELSE 'unknown' END AS kind,
sum(lines) AS lines ORDER BY lines DESC, kind"""

# Every change on the lineage of a span of lines: one row per range and hunk.
SPAN = """
MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)
WHERE r.end >= $start AND r.start <= $end
MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h)
OPTIONAL MATCH (p:Prompt)-[:LED_TO]->(e)
RETURN r.start AS start, r.end AS end, length(chain) AS depth, h.id AS hunk, h.kind AS kind,
h.ts AS ts, s.id AS session, s.agent AS agent, s.title AS title, s.cwd AS cwd, s.resume AS resume,
s.resume_cwd AS resume_cwd, e.id AS edit, e.model AS model, p.id AS prompt_id, p.text AS prompt"""


# Who wrote each current line of the files a search hit, to mark the hits.
SEARCH_OWNERS = """
UNWIND $paths AS path
MATCH (f:File {id:path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(h:Hunk)
OPTIONAL MATCH (h)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
RETURN f.path AS path, f.lines AS lines, r.start AS start, r.end AS end,
h.kind AS kind, s.id AS session"""

# `{text}` is the property as stored, or lowered when the search ignores case.
SEARCH_PROMPTS = """
MATCH (s:Session)-[:HAS_PROMPT]->(p:Prompt)
WHERE {text} CONTAINS $q
OPTIONAL MATCH (p)-[:LED_TO]->(e:Edit)
RETURN s.id AS session, p.id AS id, p.ts AS ts, p.text AS text, count(e) AS edits
ORDER BY ts DESC LIMIT $cap"""

SEARCH_DELETED = """
MATCH (f:File)-[:HAS_GAP]->(g:Gap)-[:FROM]->(h:Hunk)
WHERE {text} CONTAINS $q
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(h)
RETURN f.path AS path, g.after AS after, h.ts AS ts, h.kind AS kind,
h.removed_text AS text, s.id AS session
ORDER BY ts DESC LIMIT $cap"""


def finder(query, case, word=False):
    """A function giving where `query` starts in a string, or -1. With `word`,
    only where it stands as a whole name."""
    if word:
        whole = re.compile(rf"(?<![\w$]){re.escape(query)}(?![\w$])", 0 if case else re.IGNORECASE)
        return lambda text: m.start() if (m := whole.search(text)) else -1
    if case:
        return lambda text: text.find(query)
    needle = query.lower()
    return lambda text: text.lower().find(needle)


def clip(text, at, size):
    """A long line cut to fit a result row, and where the match sits in the cut."""
    lead = len(text) - len(text.lstrip())
    # Result rows are narrow, so the match has to sit near the start.
    start = lead if at - lead <= LEAD else at - LEAD * 2 // 3
    piece = text[start:start + SNIPPET]
    return {"text": piece, "at": at - start, "size": min(size, len(piece) - (at - start)), "cut": start > lead}


DEFINING = "def|class|function|fn|func|interface|struct|enum|trait|type|module|const|let|var|val|fun"


def definer(name):
    """A function telling whether a line defines `name`, judged from the line
    alone: a defining keyword before it, an assignment to it, or a method body."""
    name = re.escape(name) + r"(?![\w$])"
    defines = re.compile(
        rf"\b(?:{DEFINING})\s+(?:\([^)]*\)\s*)?[*&]?{name}"
        rf"|^\s*(?:(?:export|async|static|pub|public|private|protected)\s+)*(?:[\w.]+\s*,\s*)*{name}"
        rf"\s*(?:(?::[^=]+)?=(?!=)|\([^)]*\)\s*(?::[^{{]+)?\{{\s*$)"
    )
    return lambda text: bool(defines.search(text))


def search_files(paths, query, case, deadline, word=False):
    """Lines containing `query` in the readable text files among `paths`.

    Returns one entry per file with hits, and whether the search stopped early.
    With `word`, a hit on a line that defines the name says so, and files that
    define it come first.
    """
    find = finder(query, case, word)
    defines = definer(query) if word else None
    found, total = [], 0
    for path in paths:
        if total >= SEARCH_HITS or time.monotonic() > deadline:
            return found, True
        lines, _ = read_lines(path)
        if not lines:
            continue
        hits = []
        for n, text in enumerate(lines, 1):
            at = find(text)
            if at < 0:
                continue
            hit = {"line": n, **clip(text, at, len(query))}
            if defines and defines(text):
                hit["def"] = True
            # A definition is shown even in a file with more hits than fit.
            hits.append(hit if len(hits) < SEARCH_FILE_HITS or "def" in hit else None)
        if hits:
            shown = [hit for hit in hits if hit]
            total += len(shown)
            found.append({"path": str(path), "lines": len(lines), "count": len(hits), "hits": shown})
    if word:
        found.sort(key=lambda file: not any("def" in hit for hit in file["hits"]))
    return found, False


def read_lines(path):
    """The file's lines for display, or None and the reason it can't be shown."""
    try:
        data = Path(path).read_bytes()
        if len(data) > MAX_FILE_BYTES:
            return None, "File is too large to display."
        text = data.decode("utf-8")
    except FileNotFoundError:
        return None, "No longer on disk"
    except (OSError, UnicodeError):
        return None, "Not UTF-8 text"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, None


def plain_file(file, root):
    """A file in the open folder, shown with no history."""
    path = Path(file).expanduser().resolve()
    if not (root and path.is_relative_to(root) and path.is_file()):
        raise ValueError("No history for this folder yet. Build it first.")
    lines, note = read_lines(path)
    return {
        "path": str(path),
        "status": "untracked",
        "line_count": len(lines) if lines is not None else None,
        "lines": lines,
        "note": note,
        "tracks": [],
        "gaps": [],
        "cypher": [],
    }


class ReaderQueries(Queries):
    """Views the web reader needs on top of the CLI query API."""

    def overview(self):
        self._begin()
        stats = self._stats()
        files = self._run(FILES)
        sessions = self._run(SESSIONS)
        written = {row["id"]: row["written"] for row in self._run(WRITTEN)}
        alive = {row["id"]: row["alive"] for row in self._run(ALIVE)}
        for row in sessions:
            row["written"] = written.get(row["id"]) or 0
            row["alive"] = alive.get(row["id"]) or 0
        crossings = self._crossings(None, limit=200)
        return {
            "stats": stats,
            "files": files,
            "sessions": sessions,
            "crossings": crossings["rows"],
            "crossings_truncated": crossings["truncated"],
            "home": str(Path.home()),
            "cypher": crossings["cypher"],
        }

    def conversation(self, session_id):
        """A session's conversation from its log, each step marked with the edits it made."""
        self._begin()
        rows = self._run(CONVERSATION_SESSION, id=session_id)
        if not rows:
            raise ValueError("Session missing. Use the full ID from blame or trace.")
        session = rows[0]
        edits = self._run(CONVERSATION_EDITS, id=session_id)
        try:
            stat = Path(session["log_path"]).stat()
            key = (session_id, stat.st_mtime_ns, stat.st_size)
        except (OSError, TypeError):
            key = None
        cache = self.__dict__.setdefault("conversations", {})
        body = cache.get(key) if key else None
        if body is None:
            body = conversation(session, edits)
            self._written(body["items"])
            if key:
                cache[key] = body
                while len(cache) > 8:
                    cache.pop(next(iter(cache)))
        fate = {
            "replaced": self._run(REPLACED_BY, id=session_id),
            "alive_in": self._run(ALIVE_IN, id=session_id),
            "unaccounted": self._run(UNACCOUNTED, id=session_id),
        }
        return {"session": session, "edits": edits, **body, **fate, "cypher": list(self.statements)}

    def span(self, file, start, end):
        """The history of a span of lines, such as one function: one stop per
        instruction that shaped it, newest first, with the lines each still owns."""
        self._begin()
        path, info = self._file(file)
        end = min(end, info["lines"] or 0)
        self._bounds(info, start, end)
        stops, seen = {}, set()
        for row in self._run(SPAN, path=path, start=start, end=end):
            agent = row["kind"] == "agent" and row["session"]
            key = (row["session"], row["prompt_id"] or row["edit"]) if agent else (row["kind"],)
            stop = stops.get(key)
            if stop is None:
                stop = stops[key] = {
                    k: row[k]
                    for k in ("kind", "ts", "session", "agent", "title", "cwd", "resume", "resume_cwd", "edit", "model", "prompt")
                }
                stop.update(lines=0, edits=set(), depth=row["depth"])
            if row["edit"]:
                stop["edits"].add(row["edit"])
            # The newest change under one instruction is the one its stop opens at.
            if (row["ts"] or "") > (stop["ts"] or ""):
                stop.update(ts=row["ts"], edit=row["edit"])
            stop["depth"] = min(stop["depth"], row["depth"])
            if row["depth"] == 0 and (row["start"], row["hunk"]) not in seen:
                seen.add((row["start"], row["hunk"]))
                stop["lines"] += min(row["end"], end) - max(row["start"], start) + 1
        rows = sorted(stops.values(), key=lambda stop: stop["ts"] or "", reverse=True)
        for stop in rows:
            stop["edits"] = len(stop["edits"])
        return {"path": path, "start": start, "end": end, "rows": rows, "cypher": list(self.statements)}

    def search(self, query, case=False, paths=(), word=False):
        """Current lines, instructions and deleted lines that contain `query`.

        `paths` are the open folder's files. Files in the graph outside it are
        searched too. Each current-line hit says who wrote the line. With `word`
        this looks up a name's uses in code, so instructions are left out.
        """
        self._begin()
        deadline = time.monotonic() + SEARCH_SECONDS
        known = dict.fromkeys(str(p) for p in paths)
        for row in self._run("MATCH (f:File) RETURN f.path AS path ORDER BY path"):
            known.setdefault(row["path"])
        files, truncated = search_files(known, query, case, deadline, word)

        ranges = {}
        for row in self._run(SEARCH_OWNERS, paths=[f["path"] for f in files]) if files else []:
            ranges.setdefault(row["path"], []).append(row)
        for file in files:
            rows = ranges.get(file["path"], [])
            # After an edit outside the build, the graph's line numbers no longer fit.
            lines = file.pop("lines")
            file["tracked"] = bool(rows)
            file["stale"] = bool(rows) and rows[0]["lines"] != lines
            if not rows or file["stale"]:
                continue
            for hit in file["hits"]:
                owner = next((r for r in rows if r["start"] <= hit["line"] <= r["end"]), None)
                if owner:
                    hit["kind"], hit["session"] = owner["kind"], owner["session"]

        find = finder(query, case, word)
        needle = query if case else query.lower()
        prompts = []
        text = "p.text" if case else "toLower(p.text)"
        for row in [] if word else self._run(SEARCH_PROMPTS.format(text=text), q=needle, cap=SEARCH_ROWS):
            flat = " ".join(row.pop("text").split())
            at = find(flat)
            # A match across a line break is still a hit; show the start instead.
            row.update(clip(flat, at, len(query)) if at >= 0 else {**clip(flat, 0, 0), "size": 0})
            prompts.append(row)

        deleted = []
        text = "h.removed_text" if case else "toLower(h.removed_text)"
        for row in self._run(SEARCH_DELETED.format(text=text), q=needle, cap=SEARCH_ROWS):
            lines = row.pop("text").split("\n")
            for line in lines:
                at = find(line)
                if at >= 0 and len(deleted) < SEARCH_ROWS:
                    deleted.append({**row, **clip(line, at, len(query))})

        return {
            "query": query,
            "case": case,
            "word": word,
            "files": files,
            "prompts": prompts,
            "deleted": deleted,
            "truncated": truncated,
            "cypher": list(self.statements),
        }

    def _written(self, items):
        """Steps that wrote lines without a diff in the log get one from the graph."""
        bare = [i for i in items if i["kind"] == "tool" and i["edits"] and not i.get("diff")]
        ids = list(dict.fromkeys(e["id"] for i in bare for e in i["edits"]))
        if not ids:
            return
        hunks = {}
        for row in self._run(EDIT_HUNKS, ids=ids):
            hunks.setdefault(row["edit"], []).append(row)
        for item in bare:
            item["diff"], item["diff_more"] = written_diff(item["edits"], hunks)
            item["diff_from_graph"] = bool(item["diff"])

    def file(self, file, root=None):
        """One file with its tracks. Inside `root`, a file no agent touched is still
        shown, with no tracks, the way an editor opens any file in the folder."""
        self._begin()
        try:
            path, info = self._file(file)
        except ValueError:
            path = Path(file).expanduser().resolve()
            if not (root and path.is_relative_to(root) and path.is_file()):
                raise
            path, info = str(path), {"status": "untracked", "lines": None}
        tracked = info["status"] != "untracked"
        tracks = self._run(TRACKS, path=path) if tracked else []
        gaps = self._run(GAPS, path=path) if tracked else []
        lines, note = None, None
        if info["status"] in ("missing", "unreadable"):
            note = f"{info['status'].capitalize()} when the history was built"
        else:
            lines, note = read_lines(path)
        if lines is not None:
            if info["status"] == "untracked":
                info["lines"] = len(lines)
            elif len(lines) != info["lines"]:
                note = f"Changed since the build: {info['lines']} → {len(lines)} lines"
        return {
            "path": path,
            "status": info["status"],
            "line_count": info["lines"],
            "lines": lines,
            "note": note,
            "tracks": tracks,
            "gaps": gaps,
            "cypher": list(self.statements),
        }


class BuildCancelled(Exception):
    pass


def build_error(exc):
    """The message the page shows for a failed build."""
    if isinstance(exc, relate.Error):
        return f"Relate {type(exc).__name__}. Close other writers and try again."
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"Can't write the graph: {exc.strerror or exc}."
    if isinstance(exc, BuildCancelled):
        return "The build was stopped."
    return "Paper Trail could not build the history. Check the terminal and try again."


class BuildJob(ReadProgress):
    """One `papertrail ingest --project FOLDER`, with live progress for the page.

    `run` executes on a worker thread and only touches its own Relate connection.
    The serving thread reads the counts through `status`. Stages, in order:
    find, read, select, replay, write, then done or failed.
    """

    def __init__(self, project, store, roots):
        super().__init__()
        self.project, self.store, self.roots = project, store, roots
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.cancelled = False
        self.started = time.monotonic()
        self.finished = None
        self.step = "find"
        self.read_sessions = self.kept = self.files = None
        self.error = None

    def found(self, name, root, paths):
        with self.lock:
            super().found(name, root, paths)
            if len(self.sources) == len(self.roots):
                self.step = "read"

    def read(self, name, path, nbytes=None):
        if self.cancelled:
            raise BuildCancelled
        with self.lock:
            self.step = "read"
            super().read(name, path, nbytes)

    def done(self, name, sessions):
        with self.lock:
            super().done(name, sessions)

    def stage(self, step, files=None):
        if self.cancelled:
            raise BuildCancelled
        with self.lock:
            self.step = step
            self.files = files if files is not None else self.files

    @property
    def running(self):
        return not self.ended.is_set()

    def run(self):
        try:
            parsed, _ = load_sources(self.roots, progress=self)
            self.stage("select")
            sessions = select(parsed, self.project)
            with self.lock:
                self.read_sessions = len(parsed)
                self.kept = sum(1 for s in sessions if s.edits)
            build(self.store, sessions, stage=self.stage)
        except Exception as exc:
            if not isinstance(exc, (ValueError, OSError, relate.Error, BuildCancelled)):
                traceback.print_exc()
            self.error = exc
        finally:
            self.finished = time.monotonic()
            self.ended.set()

    def status(self):
        with self.lock:
            return {
                "running": self.running,
                "project": str(self.project),
                "store": str(self.store),
                "elapsed": round((self.finished or time.monotonic()) - self.started, 1),
                "stage": ("failed" if self.error else "done") if self.finished else self.step,
                "sources": [dict(s) for s in self.sources.values()],
                "fraction": round(self.fraction(), 4) if self.sources else 0.0,
                "sessions_read": self.read_sessions,
                "sessions_kept": self.kept,
                "files": self.files,
                "error": build_error(self.error) if self.error else None,
            }


class Reader:
    """Holds the open folder and one read-only connection to its graph.

    The connection reopens when the store file changes on disk, so a rebuild from
    the CLI shows up on the next request. While a build runs in the background,
    graph queries wait for it and files open as plain text.
    """

    def __init__(self, store=None, project=None):
        self.store = Path(store).expanduser().resolve() if store else None
        self.project = Path(project).expanduser().resolve() if project else None
        self.lock = threading.Lock()
        self.queries = None
        self.mtime = None
        self.folders_cache = None
        self.job = None
        # A store given on the command line stays tied to its folder when the
        # page switches away and back.
        self.known = {self.project: self.store} if self.project and self.store else {}

    @property
    def building(self):
        return bool(self.job and self.job.running)

    def call(self, method, *args, **kwargs):
        with self.lock:
            if self.building:
                raise ValueError("History is still building. Try again when it finishes.")
            if self.store is None or not self.store.is_file():
                raise ValueError("No history for this folder yet. Build it first.")
            mtime = self.store.stat().st_mtime_ns
            if self.queries is None or mtime != self.mtime:
                self._close()
                self.queries = ReaderQueries(self.store)
                self.mtime = mtime
            return getattr(self.queries, method)(*args, **kwargs)

    def file(self, path):
        if self.building or self.store is None or not self.store.is_file():
            # Before the first build, and during a rebuild, the folder still opens
            # like an editor would, as plain files with no tracks.
            return plain_file(path, self.project)
        return self.call("file", path, root=self.project)

    def search(self, query, case=False, word=False):
        query = query.strip("\r\n")
        if not query.strip():
            raise ValueError("Type something to search for.")
        if len(query) > 200:
            raise ValueError("Search for 200 characters or fewer.")
        paths = [self.project / name for name in list_files(self.project)[0]] if self.project else []
        if self.building or self.store is None or not self.store.is_file():
            # Without a graph this is a plain text search of the folder.
            files, truncated = search_files(paths, query, case, time.monotonic() + SEARCH_SECONDS, word)
            for file in files:
                file.pop("lines")
                file.update(tracked=False, stale=False)
            return {"query": query, "case": case, "word": word, "files": files, "prompts": [], "deleted": [],
                    "truncated": truncated, "cypher": []}
        return self.call("search", query, case, paths, word)

    def workspace(self):
        return {
            "project": str(self.project) if self.project else None,
            "store": str(self.store) if self.store else None,
            "ready": bool(self.store and self.store.is_file()) and not self.building,
            "building": self.building,
            "home": str(Path.home()),
        }

    def open(self, folder):
        folder = Path(folder).expanduser().resolve()
        if self.building:
            raise ValueError("Wait for the history build to finish, then open another folder.")
        if not folder.is_dir():
            raise ValueError("Folder missing. Pick an existing folder.")
        with self.lock:
            self._close()
            self.project, self.store = folder, self.known.get(folder) or store_for(folder)
        return self.workspace()

    def build(self, folder, roots=None):
        """Same as `papertrail ingest --project FOLDER` with the default log roots,
        run to the end on this thread."""
        job = self._new_job(folder, roots)
        job.run()
        if job.error:
            raise job.error
        return self.build_status()["result"]

    def start_build(self, folder, roots=None):
        """Start the same build on a worker thread. Poll `build_status` for progress.
        Asking again for the folder already building returns its progress."""
        if self.building:
            if Path(folder).expanduser().resolve() == self.job.project:
                return self.build_status()
            raise ValueError("Another folder's history is building. Wait for it to finish.")
        job = self._new_job(folder, roots)
        threading.Thread(target=job.run, name="papertrail-build", daemon=True).start()
        return self.build_status()

    def build_status(self):
        if self.job is None:
            return {"running": False, "stage": None}
        status = self.job.status()
        if status["stage"] == "done":
            status["result"] = {
                **self.workspace(),
                "sessions": self.job.kept,
                "sessions_read": self.job.read_sessions,
                "seconds": status["elapsed"],
            }
        return status

    def _new_job(self, folder, roots):
        self.open(folder)
        roots = roots or {name: Path.home() / DEFAULT_LOGS[name] for name in SOURCES}
        # A portable folder carries its own logs, as the bundled demo does.
        shipped = portable_root(self.store) if self.store else None
        if shipped and (shipped / "logs").is_dir():
            roots = {"claude-code": shipped / "logs/claude", "codex": shipped / "logs/codex"}
        with self.lock:
            self._close()
            self.job = BuildJob(self.project, self.store, roots)
        return self.job

    def tree(self):
        if self.project is None:
            return {"root": None, "files": [], "truncated": False}
        files, truncated = list_files(self.project)
        return {"root": str(self.project), "files": files, "truncated": truncated}

    def folders(self):
        if self.folders_cache is None or time.monotonic() - self.folders_cache[0] > 60:
            self.folders_cache = (time.monotonic(), agent_folders())
        return {"folders": self.folders_cache[1]}

    def _close(self):
        if self.queries is not None:
            self.queries.db.close()
            self.queries = None
            self.mtime = None

    def close(self):
        # Stop a build between two log files, so its private staging folder is removed.
        if self.building:
            self.job.cancelled = True
            self.job.ended.wait(15)
        with self.lock:
            self._close()


def list_files(root):
    """Files an editor would show for `root`, as sorted relative paths.

    Uses Git's view (tracked plus untracked, minus ignored) when the folder is in a
    repository, and otherwise walks the folder skipping the usual generated dirs.
    """
    root = Path(root)
    if shutil.which("git"):
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, timeout=10, check=True,
            ).stdout.decode("utf-8", "surrogateescape")
            names = sorted(
                p for p in set(out.split("\0"))
                if p and not p.startswith(".papertrail/") and (root / p).is_file()
            )
            return names[:MAX_TREE], len(names) > MAX_TREE
        except (OSError, subprocess.SubprocessError):
            pass
    names = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(here).relative_to(root)
        for name in sorted(files):
            names.append(str(rel / name) if rel.parts else name)
            if len(names) > MAX_TREE:
                return names[:MAX_TREE], True
    return names, False


PICK_PROMPT = "Choose a folder to open in Paper Trail"
# JavaScript for Automation rather than AppleScript's `choose folder`: osascript
# runs as a background helper, so macOS keeps the app in front (the browser, an
# editor) active and that dialog opens hidden behind it. A panel at modal-panel
# level stays above every normal window.
PICK_SCRIPT = f"""
ObjC.import("AppKit");
function run(argv) {{
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
  const panel = $.NSOpenPanel.openPanel;
  panel.canChooseDirectories = true;
  panel.canChooseFiles = false;
  panel.allowsMultipleSelection = false;
  panel.canCreateDirectories = true;
  panel.message = "{PICK_PROMPT}";
  panel.prompt = "Open";
  panel.directoryURL = $.NSURL.fileURLWithPath(argv[0]);
  panel.level = $.NSModalPanelWindowLevel;
  app.activateIgnoringOtherApps(true);
  panel.orderFrontRegardless;
  return panel.runModal == $.NSModalResponseOK ? panel.URLs.objectAtIndex(0).path.js : "";
}}
"""


def picker_command(start):
    """The native folder dialog for this system, or None if there is none."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-l", "JavaScript", "-e", PICK_SCRIPT, start]
    if sys.platform == "win32" and shutil.which("powershell"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = '{PICK_PROMPT}'; $d.SelectedPath = $args[0];"
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
        return ["powershell", "-NoProfile", "-STA", "-Command", script, start]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", f"--title={PICK_PROMPT}", f"--filename={start}/"]
    if shutil.which("kdialog"):
        return ["kdialog", "--title", PICK_PROMPT, "--getexistingdirectory", start]
    return None


def pick_folder(start=None):
    """Show the system's folder dialog. Returns the chosen path, or None on cancel."""
    start = str(Path(start).expanduser()) if start and Path(start).expanduser().is_dir() else str(Path.home())
    command = picker_command(start)
    if command is None:
        raise ValueError("No system folder dialog on this machine. Type the folder path instead.")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        raise ValueError(f"System folder dialog failed to start: {exc.strerror or exc}.") from None
    path = done.stdout.strip()
    if done.returncode != 0 or not path:
        # Every dialog reports Cancel as a nonzero exit or empty output.
        if done.returncode in (0, 1) or "-128" in done.stderr:
            return None
        raise ValueError("System folder dialog failed. Type the folder path instead.")
    return str(Path(path).resolve())


def browse(path):
    """Subfolders of `path`, for the Open Folder dialog. Lists folders only."""
    folder = Path(path or Path.home()).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Folder missing. Check the path.")
    dirs = []
    try:
        children = sorted(folder.iterdir(), key=lambda c: c.name.lower())
    except OSError:
        raise ValueError("Can't read this folder. Check its permissions.") from None
    for child in children:
        if child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        try:
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child), "graph": store_for(child).is_file()})
        except OSError:
            continue
        if len(dirs) >= MAX_DIRS:
            break
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else None,
        "graph": store_for(folder).is_file(),
        "dirs": dirs,
        "truncated": len(dirs) >= MAX_DIRS,
    }


def first_cwd(path, limit=200):
    """The working directory recorded near the top of one log file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(limit), handle):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                cwd = record.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
                if isinstance(cwd, str) and cwd.startswith("/"):
                    return cwd
    except OSError:
        pass
    return None


def agent_folders(home=None, limit=40):
    """Folders where local agent sessions ran, newest first, from a light scan of
    the log headers (no full parse)."""
    home = Path(home or Path.home())
    seen = {}

    def note(cwd, mtime):
        if cwd and mtime > seen.get(cwd, 0):
            seen[cwd] = mtime

    claude = home / DEFAULT_LOGS["claude-code"]
    if claude.is_dir():
        for project in claude.iterdir():
            logs = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if logs:
                note(first_cwd(logs[0]), logs[0].stat().st_mtime)
    codex = home / DEFAULT_LOGS["codex"]
    if codex.is_dir():
        logs = sorted(codex.rglob("rollout-*.jsonl"), reverse=True)[:3000]
        for log in logs:
            note(first_cwd(log, limit=5), log.stat().st_mtime)
    rows = []
    for cwd, mtime in sorted(seen.items(), key=lambda kv: -kv[1]):
        folder = Path(cwd)
        if folder.is_dir() and folder != home:
            rows.append({"path": cwd, "last_used": mtime, "graph": store_for(folder).is_file()})
        if len(rows) >= limit:
            break
    return rows


def handler(reader):
    # One version per launch, including restarts with the same static files.
    version = secrets.token_hex(8)
    assets = {
        name: ((STATIC / name).read_bytes(), content_type)
        for name, content_type in {
            "index.html": "text/html",
            "app.js": "text/javascript",
            "syntax.js": "text/javascript",
            "app.css": "text/css",
        }.items()
    }
    page, content_type = assets["index.html"]
    assets["index.html"] = (page.replace(b"__PAPER_TRAIL_VERSION__", version.encode()), content_type)

    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperTrail"

        def log_message(self, format, *args):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                traceback.print_exc()
                self.send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Paper Trail could not complete the request. Check the terminal and try again.",
                )

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            self.json(code, {"error": message or HTTPStatus(code).phrase + "."})

        def send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Paper-Trail-Version", version)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status, payload):
            self.send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

        def local(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host in LOOPBACK:
                return True
            self.send_error(HTTPStatus.FORBIDDEN)
            return False

        def current_page(self):
            page_version = self.headers.get("X-Paper-Trail-Version")
            if page_version is None or page_version == version:
                return True
            self.send_error(HTTPStatus.CONFLICT, "The server changed. Reload this page.")
            return False

        def do_POST(self):
            if not self.local():
                return
            if self.headers.get("X-Paper-Trail") != "1":
                return self.send_error(HTTPStatus.FORBIDDEN, "Missing X-Paper-Trail header.")
            if not self.current_page():
                return
            try:
                url = urlparse(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(min(size, 65536)) or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ValueError("Send a valid JSON object.") from None
                if not isinstance(body, dict):
                    raise ValueError("Send a JSON object.")
                if url.path == "/api/open":
                    result = reader.open(need(body, "path"))
                elif url.path == "/api/pick":
                    start = body.get("start") if isinstance(body.get("start"), str) else None
                    result = {"path": pick_folder(start or reader.workspace()["project"])}
                elif url.path == "/api/build":
                    result = reader.start_build(need(body, "path"))
                else:
                    return self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
                self.json(HTTPStatus.OK, result)
            except ValueError as exc:
                self.json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except OSError as exc:
                self.json(HTTPStatus.BAD_REQUEST, {"error": f"Can't write the graph: {exc.strerror or exc}."})
            except relate.Error as exc:
                self.json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"Relate {type(exc).__name__}. Close other writers and try again."},
                )

        def do_GET(self):
            if not self.local() or not self.current_page():
                return
            url = urlparse(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path.startswith("/api/"):
                return self.api(url.path[5:], params)
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            if name not in assets:
                return self.send_error(HTTPStatus.NOT_FOUND)
            body, content_type = assets[name]
            self.send(HTTPStatus.OK, body, content_type + "; charset=utf-8")

        def api(self, name, params):
            try:
                if name == "workspace":
                    result = reader.workspace()
                elif name == "overview":
                    result = reader.call("overview")
                    result["project"] = reader.workspace()["project"]
                elif name == "file":
                    result = reader.file(need(params, "path"))
                elif name == "tree":
                    result = reader.tree()
                elif name == "browse":
                    result = browse(params.get("path"))
                elif name == "folders":
                    result = reader.folders()
                elif name == "build":
                    result = reader.build_status()
                elif name == "search":
                    result = reader.search(need(params, "q"), params.get("case") == "1", params.get("word") == "1")
                elif name == "blame":
                    result = reader.call("blame", need(params, "path"))
                elif name == "trace":
                    line = need(params, "line")
                    if not line.isdecimal():
                        raise ValueError("Use a positive line number.")
                    result = reader.call("trace", need(params, "path"), int(line), 200)
                elif name == "span":
                    start, end = need(params, "start"), need(params, "end")
                    if not (start.isdecimal() and end.isdecimal()):
                        raise ValueError("Use positive line numbers.")
                    result = reader.call("span", need(params, "path"), int(start), int(end))
                elif name == "session":
                    result = reader.call("session", need(params, "id"), 2000)
                elif name == "conversation":
                    result = reader.call("conversation", need(params, "id"))
                else:
                    return self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
                self.json(HTTPStatus.OK, result)
            except ValueError as exc:
                self.json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except relate.Error as exc:
                self.json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"Relate {type(exc).__name__}. Check the store and rerun ingest."},
                )

    return Handler


def need(params, key):
    value = params.get(key)
    if not value or not isinstance(value, str):
        raise ValueError(f"Missing parameter: {key}.")
    return value


def serve(store=None, host="127.0.0.1", port=8765, open_browser=True, project=None):
    reader = Reader(store, project)
    if reader.workspace()["ready"]:
        reader.call("overview")  # Fail fast on a foreign store.
    # Single-threaded on purpose: a Relate connection must stay on the thread that opened it.
    server = HTTPServer((host, port), handler(reader))
    url = f"http://{host}:{server.server_address[1]}/"
    where = f"store: {reader.store}" if reader.workspace()["ready"] else "no history yet, open or build one in the page"
    print(f"Paper Trail reader on {url}  ({where})  Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        reader.close()
