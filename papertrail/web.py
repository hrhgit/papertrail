"""Local web reader over the same query API the CLI uses.

Binds to loopback only and opens the store read-only. File text is served only for
paths that are File nodes in the graph or files inside the open folder. Requests
must name a loopback host, and the two actions that change state (open a folder,
build its history) are POSTs that need a custom header, so other sites can't
trigger them. The same goes for showing the system folder dialog.
"""

import codecs
import io
import json
import os
import re
import secrets
import select as socket_select
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as package_files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import relate

from .graph import portable_root
from .ingest import ingest
from .logfile import open_log
from .queries import FIELDS, INFERENCE_OWNER, OWNER, Queries, file_version
from .sources import DEFAULT_LOGS, SOURCES, ReadProgress
from .transcript import conversation, conversation_version, written_diff

STATIC = package_files("papertrail") / "static"
MAX_FILE_BYTES = 2_000_000
MAX_TREE = 20_000
MAX_DIRS = 1_000
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
# Keep the existing search result and scan limits.
SEARCH_SECONDS = 2.0
SEARCH_HITS = 400
SEARCH_FILE_HITS = 40
SEARCH_ROWS = 60
SNIPPET = 200
LEAD = 24
TREE_CACHE_SECONDS = 1.0
CONVERSATION_CACHE_SIZE = 8
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

# Where each session's lines sit in each file now.
FILE_AUTHORS = """
MATCH (f:File)-[:HAS_RANGE]->(r:Range)-[:FROM]->(:Hunk)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
RETURN f.path AS path, s.id AS id, r.start AS start, r.end AS end ORDER BY path, start"""

# Every session that edited each file, whether or not its lines are still there.
FILE_SESSIONS = """
MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:TOUCHED]->(f:File)
RETURN f.path AS path, s.id AS id, count(e) AS edits ORDER BY path, id"""

# A move only touches where the file went; this is where it came from.
FILE_MOVES = """
MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)
WHERE e.moved_from IS NOT NULL
RETURN e.moved_from AS path, s.id AS id ORDER BY path, id"""

SESSIONS = """
MATCH (s:Session)
OPTIONAL MATCH (s)-[:HAS_EDIT]->(e:Edit)
RETURN s.id AS id, s.agent AS agent, s.model AS model, s.title AS title,
s.started_at AS started_at, s.ended_at AS ended_at, s.cwd AS cwd, null AS parent,
count(e) AS edits ORDER BY started_at, id"""

SESSION_PARENTS = """
MATCH (s:Session)<-[:SPAWNED]-(parent:Session)
RETURN s.id AS id, parent.id AS parent, count(*) AS links"""

# One row per deletion still between two current lines. `after` counts the lines
# above the spot. `wrote` lists who wrote the deleted lines and how many each.
GAPS = ("""
MATCH (f:File {id:$path})-[:HAS_GAP]->(g:Gap)-[:FROM]->(h:Hunk)
""" + OWNER + """
OPTIONAL MATCH (h)-[r:REPLACED]->(old:Hunk)
OPTIONAL MATCH (old)<-[:HAS_HUNK]-(written:Edit)<-[:HAS_EDIT]-(writer:Session)
WITH g, h, e, s, p, likely, evidence, instruction, collect(CASE WHEN old IS NULL THEN NULL
  ELSE {kind: old.kind, session: writer.id, edit: written.id, ts: old.ts, lines: r.lines} END) AS wrote
RETURN g.id AS id, g.after AS after, h.id AS hunk, h.removed AS removed,
h.removed_text AS text, coalesce(e.id, evidence.id) AS edit, wrote,
""" + FIELDS + " ORDER BY after, ts, id")

DELETED_TEXT = """
MATCH (:File {id:$path})-[:HAS_GAP]->(g:Gap {id:$id})-[:FROM]->(h:Hunk)
RETURN h.removed_text AS text"""

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
SPAN = ("""
MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)
WHERE r.end >= $start AND r.start <= $end
MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
""" + OWNER + """
RETURN r.start AS start, r.end AS end, length(chain) AS depth, h.id AS hunk,
coalesce(s.cwd, likely.cwd) AS cwd, coalesce(s.resume, likely.resume) AS resume,
coalesce(s.resume_cwd, likely.resume_cwd) AS resume_cwd,
coalesce(e.id, evidence.id) AS edit, coalesce(p.id, instruction.id) AS prompt_id,
""" + FIELDS)


# Who wrote each current line of the files a search hit, to mark the hits.
SEARCH_OWNERS = ("""
UNWIND $paths AS path
MATCH (f:File {id:path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(h:Hunk)
OPTIONAL MATCH (h)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
""" + INFERENCE_OWNER + """
RETURN f.path AS path, f.lines AS lines, r.start AS start, r.end AS end,
h.kind AS kind, coalesce(s.id, likely.id) AS session""")

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


class SearchCancelled(Exception):
    pass


def search_files(paths, query, case, deadline, word=False, check=None):
    """Lines containing `query` in the readable text files among `paths`.

    Returns one entry per file with hits, and whether the search stopped early.
    With `word`, a hit on a line that defines the name says so, and files that
    define it come first.
    """
    find = finder(query, case, word)
    defines = definer(query) if word else None
    found, total = [], 0

    def expired():
        if check:
            check()
        return time.monotonic() > deadline

    for path in paths:
        if total >= SEARCH_HITS or expired():
            return found, True
        lines, _ = read_lines(path, expired)
        if expired():
            return found, True
        if not lines:
            continue
        hits = []
        for n, text in enumerate(lines, 1):
            if n % 128 == 1 and expired():
                return found, True
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


def search_result(query, case, word, files, truncated, ranges, prompts, removed, statements, check):
    for file in files:
        if check:
            check()
        rows = ranges.get(file["path"], [])
        # After an edit outside the build, the graph's line numbers no longer fit.
        lines = file.pop("lines")
        file["tracked"] = bool(rows)
        file["stale"] = bool(rows) and rows[0]["lines"] != lines
        if not rows or file["stale"]:
            continue
        rows.sort(key=lambda row: row["start"])
        starts = [row["start"] for row in rows]
        for hit in file["hits"]:
            index = bisect_right(starts, hit["line"]) - 1
            if index >= 0 and hit["line"] <= rows[index]["end"]:
                owner = rows[index]
                hit["kind"], hit["session"] = owner["kind"], owner["session"]

    find = finder(query, case, word)
    for row in prompts:
        if check:
            check()
        flat = " ".join(row.pop("text").split())
        at = find(flat)
        # A match across a line break is still a hit; show the start instead.
        row.update(clip(flat, at, len(query)) if at >= 0 else {**clip(flat, 0, 0), "size": 0})

    deleted = []
    for row in removed:
        if check:
            check()
        lines = row.pop("text").split("\n")
        for n, line in enumerate(lines):
            if len(deleted) >= SEARCH_ROWS:
                break
            if check and n % 128 == 0:
                check()
            at = find(line)
            if at >= 0:
                deleted.append({**row, **clip(line, at, len(query))})

    return {
        "query": query,
        "case": case,
        "word": word,
        "files": files,
        "prompts": prompts,
        "deleted": deleted,
        "truncated": truncated,
        "cypher": statements,
    }


def read_lines(path, expired=None):
    """The file's lines for display, or None and the reason it can't be shown."""
    try:
        with Path(path).open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_FILE_BYTES:
                return None, "File is too large to display."
            data = bytearray()
            while True:
                if expired and expired():
                    return None, None
                chunk = stream.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES:
                    return None, "File is too large to display."
        text = data.decode("utf-8")
    except FileNotFoundError:
        return None, "No longer on disk"
    except (OSError, UnicodeError):
        return None, "Not UTF-8 text"
    return display_lines(text), None


def display_lines(text):
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_window(path, start, end):
    """Count lines in blocks while keeping only the requested UTF-8 text."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    picked, number, last = bytearray(), 1, b""
    oversized = False
    try:
        with Path(path).open("rb") as stream:
            while chunk := stream.read(65536):
                decoder.decode(chunk)
                last = chunk[-1:]
                pieces = chunk.split(b"\n")
                lo = max(0, start - number)
                hi = len(pieces) if end is None else min(len(pieces), end - number + 1)
                if not oversized and lo < hi:
                    picked.extend(b"\n".join(pieces[lo:hi]))
                    if hi < len(pieces):
                        picked.extend(b"\n")
                    oversized = len(picked) > MAX_FILE_BYTES
                    if oversized:
                        picked.clear()
                number += len(pieces) - 1
        decoder.decode(b"", final=True)
    except FileNotFoundError:
        return None, None, "No longer on disk"
    except (OSError, UnicodeError):
        return None, None, "Not UTF-8 text"
    total = number - int(last in (b"", b"\n"))
    if oversized:
        return None, total, "File is too large to display."
    return display_lines(picked.decode("utf-8")), total, None


def plain_file_history(file, root):
    """A file in the open folder, shown with no history."""
    path = Path(file).expanduser().resolve()
    if not (root and path.is_relative_to(root) and path.is_file()):
        raise ValueError("No history for this folder yet. Build it first.")
    return {
        "path": str(path),
        "status": "untracked",
        "line_count": None,
        "tracks": [],
        "gaps": [],
        "cypher": [],
    }


def file_view(view, window):
    lines = total = note = None
    status, snapshot = view["status"], view["line_count"]
    if status in ("missing", "unreadable"):
        note = f"{status.capitalize()} when the history was built"
    elif window is None:
        lines, note = read_lines(view["path"])
        total = len(lines) if lines is not None else None
    else:
        start, end = window
        lines, total, note = read_window(view["path"], start, end)
    if total is not None and status != "untracked" and total != snapshot and note is None:
        note = f"Changed since the build: {snapshot} → {total} lines"
    result = {
        "path": view["path"],
        "status": status,
        "line_count": total if status == "untracked" else snapshot,
        "lines": lines,
        "note": note,
        "tracks": view["tracks"],
        "gaps": view["gaps"],
        "cypher": view["cypher"],
    }
    if window is not None:
        result["total_lines"] = total
    return result


def remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    if len(cache) > CONVERSATION_CACHE_SIZE:
        cache.popitem(last=False)


def conversation_view(info, load_hunks):
    session, edits, fate, statements = info
    body = conversation(session, edits)
    if fill_written_diffs(body["items"], load_hunks):
        # This statement explains the graph-backed tool diffs in the response.
        statements = [*statements[:3], EDIT_HUNKS.strip(), *statements[3:]]
    return {"session": session, "edits": edits, **body, **fate, "cypher": statements}


def fill_written_diffs(items, load_hunks):
    bare = [i for i in items if i["kind"] == "tool" and i["edits"] and not i.get("diff")]
    ids = list(dict.fromkeys(e["id"] for i in bare for e in i["edits"]))
    if not ids:
        return False
    hunks = load_hunks(ids)
    for item in bare:
        item["diff"], item["diff_more"] = written_diff(item["edits"], hunks)
        item["diff_from_graph"] = bool(item["diff"])
    return True


class ReaderQueries(Queries):
    """Views the web reader needs on top of the CLI query API."""

    def _overview_sessions(self):
        sessions = self._run(SESSIONS)
        parents = {}
        for row in self._run(SESSION_PARENTS):
            parents.setdefault(row["id"], []).append((row["parent"], row["links"]))
        # A repeated relationship contributed to count(e) once per link.
        return [
            row | {"parent": parent, "edits": row["edits"] * links}
            for row in sessions
            for parent, links in parents.get(row["id"], ((None, 1),))
        ]

    def completeness(self):
        return self.complete

    def overview(self):
        if hasattr(self, "_overview"):
            self.statements[:] = self._overview["cypher"]
            return self._overview
        self._begin()
        stats = self._stats()
        files = self._run(FILES)
        authors = {}
        for row in self._run(FILE_AUTHORS):
            spans = authors.setdefault(row["path"], [])
            # Touching runs by the same session read as one stretch.
            if spans and spans[-1][0] == row["id"] and spans[-1][2] + 1 >= row["start"]:
                spans[-1][2] = max(spans[-1][2], row["end"])
            else:
                spans.append([row["id"], row["start"], row["end"]])
        # A session belongs to a file it edited, moved away, or still has lines
        # in: lines follow a file through a move another session made.
        touched = {}
        for row in self._run(FILE_SESSIONS) + self._run(FILE_MOVES):
            touched.setdefault(row["path"], set()).add(row["id"])
        for path, spans in authors.items():
            touched.setdefault(path, set()).update(span[0] for span in spans)
        for row in files:
            row["authors"] = authors.get(row["path"], [])
            row["touched"] = sorted(touched.get(row["path"], ()))
        sessions = self._overview_sessions()
        written = {row["id"]: row["written"] for row in self._run(WRITTEN)}
        alive = {row["id"]: row["alive"] for row in self._run(ALIVE)}
        for row in sessions:
            row["written"] = written.get(row["id"]) or 0
            row["alive"] = alive.get(row["id"]) or 0
        crossings = self._crossings(None, limit=200)
        self._overview = {
            "stats": stats,
            "complete": self.complete,
            "files": files,
            "sessions": sessions,
            "crossings": crossings["rows"],
            "crossings_truncated": crossings["truncated"],
            "home": str(Path.home()),
            "cypher": crossings["cypher"],
        }
        return self._overview

    def conversation(self, session_id):
        """A session's conversation from its log, each step marked with the edits it made."""
        return conversation_view(self.conversation_info(session_id), self.conversation_hunks)

    def conversation_info(self, session_id):
        cache = self.__dict__.setdefault("conversations", OrderedDict())
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        self._begin()
        rows = self._run(CONVERSATION_SESSION, id=session_id)
        if not rows:
            raise ValueError("Session missing. Use the full ID from blame or trace.")
        session = rows[0]
        edits = self._run(CONVERSATION_EDITS, id=session_id)
        fate = {
            "replaced": self._run(REPLACED_BY, id=session_id),
            "alive_in": self._run(ALIVE_IN, id=session_id),
            "unaccounted": self._run(UNACCOUNTED, id=session_id),
        }
        info = session, edits, fate, list(self.statements)
        remember(cache, session_id, info)
        return info

    def conversation_hunks(self, ids):
        self.statements.clear()
        hunks = {}
        for row in self._run(EDIT_HUNKS, ids=ids):
            hunks.setdefault(row["edit"], []).append(row)
        return hunks

    def _written(self, items):
        fill_written_diffs(items, self.conversation_hunks)

    def span(self, file, start, end):
        """The history of a span of lines, such as one function: one stop per
        instruction that shaped it, newest first, with the lines each still owns."""
        self._begin()
        path, info = self._file(file)
        end = min(end, info["lines"] or 0)
        self._bounds(info, start, end)
        stops, seen = {}, set()
        for row in self._run(SPAN, path=path, start=start, end=end):
            if row["kind"] == "inferred":
                key = (row["kind"], row["basis"], row["was"], row["edit"])
            elif row["kind"] == "agent" and row["session"]:
                key = (row["session"], row["prompt_id"] or row["edit"])
            else:
                key = (row["kind"],)
            stop = stops.get(key)
            if stop is None:
                stop = stops[key] = {
                    k: row[k]
                    for k in ("kind", "basis", "was", "ts", "session", "agent", "title", "cwd", "resume", "resume_cwd", "edit", "model", "effort", "tool", "prompt")
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

    def search_paths(self):
        if hasattr(self, "_search_paths"):
            return self._search_paths
        self._begin()
        rows = self._run("MATCH (f:File) RETURN f.path AS path ORDER BY path")
        self._search_paths = [r["path"] for r in rows], list(self.statements)
        return self._search_paths

    def search_history(self, query, case, word, files, statements, check):
        self.statements[:] = statements
        ranges = {}
        if check:
            check()
        for row in self._run(SEARCH_OWNERS, paths=[f["path"] for f in files]) if files else []:
            ranges.setdefault(row["path"], []).append(row)
        if check:
            check()
        text = "p.text" if case else "toLower(p.text)"
        needle = query if case else query.lower()
        prompts = [] if word else self._run(SEARCH_PROMPTS.format(text=text), q=needle, cap=SEARCH_ROWS)
        if check:
            check()
        text = "h.removed_text" if case else "toLower(h.removed_text)"
        deleted = self._run(SEARCH_DELETED.format(text=text), q=needle, cap=SEARCH_ROWS)
        return ranges, prompts, deleted, list(self.statements)

    def file(self, file, root=None, window=None, deleted=True):
        return file_view(self.file_history(file, root, window, deleted), window)

    def file_history(self, file, root, window, deleted):
        """One file with its tracks. Inside `root`, a file no agent touched is still
        shown, with no tracks, the way an editor opens any file in the folder."""
        self._begin()
        try:
            path, info = self._file(file)
        except ValueError:
            try:
                return plain_file_history(file, root) | {"cypher": list(self.statements)}
            except ValueError:
                pass
            raise
        start, end = window or (None, None)
        tracks = self._tracks(path, start, end)
        gaps_query = GAPS if deleted else GAPS.replace("h.removed_text AS text", "NULL AS text")
        if window:
            gaps_query = gaps_query.replace("OPTIONAL MATCH", """
WHERE g.after >= $start - 1 AND ($end IS NULL OR g.after <= $end)
OPTIONAL MATCH""", 1)
        gaps = self._run(gaps_query, path=path, start=start, end=end)
        return {
            "path": path,
            "status": info["status"],
            "line_count": info["lines"],
            "tracks": tracks,
            "gaps": gaps,
            "cypher": list(self.statements),
        }

    def deleted(self, file, gap_id):
        self._begin()
        path = str(Path(file).expanduser().resolve())
        rows = self._run(DELETED_TEXT, path=path, id=gap_id)
        if not rows:
            raise ValueError("Deletion missing for this file.")
        return {"path": path, "id": gap_id, "text": rows[0]["text"], "cypher": list(self.statements)}


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

    def __init__(self, project, store, roots, *, portable=None, complete=False):
        super().__init__()
        self.requested = Path(project)
        self.project, self.store, self.roots = self.requested.resolve(), store, roots
        self.portable = portable
        self.complete = complete
        self.phase = "full" if complete else "quick"
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.cancelled = False
        self.started = self.pass_started = time.monotonic()
        self.finished = None
        self.step = "find"
        self.read_sessions = self.kept = self.files = None
        self.error = None

    def check(self):
        if self.cancelled:
            raise BuildCancelled

    def cancel(self):
        with self.lock:
            self.cancelled = True

    def begin(self, phase):
        with self.lock:
            self.check()
            super().begin(phase)
            self.phase, self.step = phase, "find"
            self.pass_started = time.monotonic()
            self.read_sessions = self.kept = self.files = None

    def selected(self, read, kept):
        with self.lock:
            self.read_sessions, self.kept = read, kept

    @contextmanager
    def publish(self, complete):
        with self.lock:
            self.check()
            yield
            self.complete = complete

    def found(self, name, root, paths):
        with self.lock:
            super().found(name, root, paths)
            if len(self.sources) == len(self.roots):
                self.step = "read"

    def read(self, name, path, nbytes=None):
        self.check()
        with self.lock:
            self.step = "read"
            super().read(name, path, nbytes)

    def done(self, name, sessions):
        with self.lock:
            super().done(name, sessions)

    def stage(self, step, files=None):
        self.check()
        with self.lock:
            self.step = step
            self.files = files if files is not None else self.files

    @property
    def running(self):
        return not self.ended.is_set()

    def run(self):
        try:
            for _ in ingest(
                self.store, self.roots, self.requested, progress=self, check=self.check,
                portable=self.portable, publish=self.publish,
            ):
                pass
        except Exception as exc:
            if not isinstance(exc, (ValueError, OSError, relate.Error, BuildCancelled)):
                # Parse exceptions can contain private log content.
                print(f"Paper Trail build failed ({type(exc).__name__}).", file=sys.stderr)
            self.error = exc
        finally:
            with self.lock:
                self.phase = (
                    "cancelled" if isinstance(self.error, BuildCancelled)
                    else "failed" if self.error else "done"
                )
                self.finished = time.monotonic()
                self.ended.set()

    def status(self):
        with self.lock:
            return {
                "running": self.running,
                "project": str(self.project),
                "store": str(self.store),
                "elapsed": round((self.finished or time.monotonic()) - self.pass_started, 1),
                "phase": self.phase,
                "stage": self.phase if self.finished else self.step,
                "complete": self.complete,
                "ready": self.store.is_file(),
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
    the CLI shows up on the next request. Before the first publication, files
    can still open as plain text.
    """

    def __init__(self, store=None, project=None):
        self.store = Path(store).expanduser().resolve() if store else None
        self.project = Path(project).expanduser().resolve() if project else None
        self.lock = threading.RLock()
        self.db_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="papertrail-db")
        self.build_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="papertrail-build")
        self.build_options = {}
        self.closed = False
        self.queries = None
        self.graph_version = None
        self.tree_lock = threading.Lock()
        self.tree_cache = None
        self.conversation_bodies = OrderedDict()
        self.folders_cache = None
        self.job = None
        # A store given on the command line stays tied to its folder when the
        # page switches away and back.
        self.known = {self.project: self.store} if self.project and self.store else {}

    @property
    def building(self):
        return bool(self.job and self.job.running and not self.job.cancelled and self.job.project == self.project)

    def call(self, method, *args, **kwargs):
        return self._submit_query(method, args, kwargs).result()[0]

    def _require_history(self):
        if self.closed:
            raise ValueError("The reader is shutting down.")
        if not self._ready():
            if self.building:
                raise ValueError("History is still building. Try again when it publishes.")
            raise ValueError("No history for this folder yet. Build it first.")

    def _submit_query(self, method, args=(), kwargs=None, *, store=None, version=None, check=None):
        with self.lock:
            self._require_history()
            if store is not None and store != self.store:
                raise ValueError("History changed during the request. Try again.")
            return self.db_worker.submit(self._query, self.store, method, args, kwargs or {}, version, check)

    def overview(self):
        with self.lock:
            project = str(self.project) if self.project else None
            task = self._submit_query("overview")
        return task.result()[0] | {"project": project}

    @staticmethod
    def _store_version(store):
        try:
            return store, file_version(store.stat())
        except FileNotFoundError:
            raise ValueError("No history for this folder yet. Build it first.") from None

    def _query(self, store, method, args, kwargs, version=None, check=None):
        if check:
            check()
        current = self._store_version(store)
        if version is not None and current != version:
            raise ValueError("History changed during the request. Try again.")
        if current != self.graph_version:
            self._close()
            self.queries = ReaderQueries(store)
            self.graph_version = current
        return getattr(self.queries, method)(*args, **kwargs), current

    def file(self, path, window=None, deleted=True):
        with self.lock:
            root = self.project
            args = (path, root, window, deleted)
            task = self._submit_query("file_history", args) if self._ready() else None
        history = task.result()[0] if task else plain_file_history(path, root)
        return file_view(history, window)

    def conversation_bytes(self, session_id):
        with self.lock:
            self._require_history()
            key = (self._store_version(self.store), session_id)
            cached = self.conversation_bodies.get(key)
            if cached:
                session, log, body = cached
                if conversation_version(session) == log:
                    self.conversation_bodies.move_to_end(key)
                    return body
                self.conversation_bodies.pop(key)
            task = self._submit_query("conversation_info", (session_id,))
        info, version = task.result()
        session = info[0]
        log = conversation_version(session)

        def load_hunks(ids):
            task = self._submit_query("conversation_hunks", (ids,), store=version[0], version=version)
            return task.result()[0]

        view = conversation_view(info, load_hunks)
        body = json.dumps(view).encode()
        if log is not None and log == conversation_version(session) and view["note"] != "The log for this session could not be read.":
            with self.lock:
                remember(self.conversation_bodies, (version, session_id), (session, log, body))
        return body

    def search(self, query, case=False, word=False, check=None):
        query = query.strip("\r\n")
        if not query.strip():
            raise ValueError("Type something to search for.")
        if len(query) > 200:
            raise ValueError("Search for 200 characters or fewer.")
        if check:
            check()
        with self.lock:
            root, store = self.project, self.store if self._ready() else None
        paths = dict.fromkeys(str(root / name) for name in self._listed(root)[0]) if root else {}
        deadline = time.monotonic() + SEARCH_SECONDS
        statements, history = [], ({}, [], [], [])
        if store:
            task = self._submit_query("search_paths", store=store, check=check)
            (graph_paths, statements), version = task.result()
            for path in graph_paths:
                paths.setdefault(path)
        hits, truncated = search_files(paths, query, case, deadline, word, check)
        if check:
            check()
        if store:
            args = (query, case, word, hits, statements, check)
            task = self._submit_query("search_history", args, store=store, version=version, check=check)
            history, _ = task.result()
        return search_result(query, case, word, hits, truncated, *history, check)

    def _ready(self):
        return bool(self.store and self.store.is_file()) and not self.closed

    def workspace(self):
        with self.lock:
            ready = self._ready()
            return {
                "project": str(self.project) if self.project else None,
                "store": str(self.store) if self.store else None,
                "ready": ready,
                "complete": self.call("completeness") if ready else False,
                "building": self.building,
                "home": str(Path.home()),
            }

    def open(self, folder):
        requested = Path(folder).expanduser()
        with self.lock:
            self._open_folder(requested.resolve())
            return self.workspace()

    def _open_folder(self, folder):
        if not folder.is_dir():
            raise ValueError("Folder missing. Pick an existing folder.")
        if self.closed:
            raise ValueError("The reader is shutting down.")
        if self.building:
            if self.job.status()["phase"] == "quick":
                raise ValueError("Wait for the first pass to finish, then open another folder.")
            self.job.cancel()
        if folder != self.project:
            self.db_worker.submit(self._close)
        self.project, self.store = folder, self.known.get(folder) or store_for(folder)

    def build(self, folder, roots=None):
        """Build a folder and wait for publication."""
        with self.lock:
            job = self._new_job(folder, roots)
            task = self.build_worker.submit(job.run)
        task.result()
        if job.error:
            raise job.error
        return self.build_status()["result"]

    def start_build(self, folder, roots=None, *, portable=None):
        """Start building on a worker. Poll `build_status` for progress."""
        with self.lock:
            if self.building and self.job.status()["phase"] == "quick":
                if Path(folder).expanduser().resolve() == self.job.project:
                    return self.build_status()
                raise ValueError("Another folder's first pass is building. Wait for it to finish.")
            job = self._new_job(folder, roots, portable)
            self.build_worker.submit(job.run)
            return self.build_status()

    def build_status(self):
        with self.lock:
            if self.job is None:
                return {"running": False, "stage": None}
            status = self.job.status()
            if status["phase"] == "done" and self.job.project == self.project:
                status["result"] = {
                    **self.workspace(),
                    "sessions": self.job.kept,
                    "sessions_read": self.job.read_sessions,
                    "seconds": round(self.job.finished - self.job.started, 1),
                }
            return status

    def _new_job(self, folder, roots, portable=None):
        requested = Path(folder).expanduser()
        self._open_folder(requested.resolve())
        if roots is not None or portable is not None:
            self.build_options[self.project] = (roots, portable)
        roots, portable = self.build_options.get(self.project, (None, None))
        if roots is None:
            roots = {name: Path.home() / DEFAULT_LOGS[name] for name in SOURCES}
            # A portable folder carries its own logs, as the bundled demo does.
            shipped = self.db_worker.submit(portable_root, self.store).result()
            if shipped and (shipped / "logs").is_dir():
                roots = {"claude-code": shipped / "logs/claude", "codex": shipped / "logs/codex"}
        complete = self._ready() and self.call("completeness")
        self.job = BuildJob(requested, self.store, roots, portable=portable, complete=complete)
        return self.job

    def tree(self):
        with self.lock:
            root = self.project
        if root is None:
            return {"root": None, "files": [], "truncated": False}
        files, truncated = self._listed(root)
        return {"root": str(root), "files": list(files), "truncated": truncated}

    def _listed(self, root):
        with self.tree_lock:
            now = time.monotonic()
            if self.tree_cache and self.tree_cache[0] == root and now < self.tree_cache[1]:
                return self.tree_cache[2]
            result = list_files(root)
            self.tree_cache = (root, time.monotonic() + TREE_CACHE_SECONDS, result)
            return result

    def folders(self):
        if self.folders_cache is None or time.monotonic() - self.folders_cache[0] > 60:
            self.folders_cache = (time.monotonic(), agent_folders())
        return {"folders": self.folders_cache[1]}

    def _close(self):
        if self.queries is not None:
            self.queries.db.close()
            self.queries = None
            self.graph_version = None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.job and self.job.running:
                self.job.cancel()
            task = self.db_worker.submit(self._close)
        # Wait for cancelled builds to remove their private staging folders.
        try:
            self.build_worker.shutdown()
            task.result()
        finally:
            self.db_worker.shutdown()


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
        with io.TextIOWrapper(open_log(path), encoding="utf-8", errors="replace") as handle:
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
    except Exception:  # unreadable, or a compressed log this machine can't decode
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
        logs = sorted(
            [*codex.rglob("rollout-*.jsonl"), *codex.rglob("rollout-*.jsonl.zst")],
            key=lambda p: p.name,
            reverse=True,
        )[:3000]
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

        def check_search_client(self):
            # The request is fully read. A readable socket with no bytes left
            # means the browser closed it while the search was running.
            try:
                readable, _, _ = socket_select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1, socket.MSG_PEEK):
                    raise SearchCancelled
            except (ConnectionError, OSError):
                raise SearchCancelled from None

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
                    result = reader.overview()
                elif name == "file":
                    result = reader.file(
                        need(params, "path"), file_window(params), deleted=params.get("deleted") != "0",
                    )
                elif name == "deleted":
                    result = reader.call("deleted", need(params, "path"), need(params, "id"))
                elif name == "tree":
                    result = reader.tree()
                elif name == "browse":
                    result = browse(params.get("path"))
                elif name == "folders":
                    result = reader.folders()
                elif name == "build":
                    result = reader.build_status()
                elif name == "search":
                    result = reader.search(
                        need(params, "q"), params.get("case") == "1", params.get("word") == "1",
                        check=self.check_search_client,
                    )
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
                    body = reader.conversation_bytes(need(params, "id"))
                    return self.send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                else:
                    return self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
                self.json(HTTPStatus.OK, result)
            except SearchCancelled:
                self.close_connection = True
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


def file_window(params):
    if "from" not in params and "to" not in params:
        return None
    first, last = params.get("from", "1"), params.get("to")
    if not first.isdecimal() or (last is not None and not last.isdecimal()):
        raise ValueError("Use positive line numbers.")
    start, end = int(first), int(last) if last is not None else None
    if start < 1 or (end is not None and end < start) or (end or start) >= 2**63:
        raise ValueError("Use an ascending range of positive line numbers.")
    return start, end


def serve(store=None, host="127.0.0.1", port=8765, open_browser=True, project=None, *,
          build_on_start=False, roots=None, portable=None):
    reader = Reader(store, project)
    try:
        if reader._ready():
            reader.call("validate")
        with ThreadingHTTPServer((host, port), handler(reader)) as server:
            if build_on_start:
                reader.start_build(project, roots=roots, portable=portable)
            url = f"http://{host}:{server.server_address[1]}/"
            where = f"store: {reader.store}" if reader.workspace()["ready"] else "no history yet, open or build one in the page"
            print(f"Paper Trail reader on {url}  ({where})  Ctrl+C to stop.", flush=True)
            if open_browser:
                threading.Timer(0.3, webbrowser.open, (url,)).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
