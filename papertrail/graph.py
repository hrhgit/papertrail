"""Build and verify a private generation before publishing the single-file graph."""

import os
import pickle
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

import relate

from .replay import hint_names, histories


def select(sessions, project):
    if project is None:
        return sessions
    project = project.resolve()
    by_id = {s.id: s for s in sessions}
    selected = {
        s.id
        for s in sessions
        if any(
            Path(p).is_relative_to(project) for e in s.edits for p in (e.path, e.moved_from) if p
        )
    }
    # Carry earlier writers across a move into this project, even if the
    # source path was outside the project at the time of their edits.
    preceding = defaultdict(list)
    for s in sessions:
        for e in s.edits:
            preceding[e.path].append((s.id, e))
    pending = [
        (e.moved_from, e.ts) for s in sessions if s.id in selected for e in s.edits if e.moved_from
    ]
    visited = {}
    while pending:
        path, until = pending.pop()
        if visited.get(path, "") >= until:
            continue
        visited[path] = until
        for sid, earlier in preceding[path]:
            if earlier.ts <= until:
                selected.add(sid)
                if earlier.moved_from:
                    pending.append((earlier.moved_from, earlier.ts))
    paths = {p for sid in selected for e in by_id[sid].edits for p in (e.path, e.moved_from) if p}
    hinted = {
        s.id for s in sessions if s.id not in selected
        and any(hint_names(h, path) for h in s.hints for path in paths)
    }
    def with_parents(keys):
        keep = set(keys)
        for key in keys:
            parent = by_id[key].parent
            while parent in by_id and parent not in keep:
                keep.add(parent)
                parent = by_id[parent].parent
        return keep

    original_keep = with_parents(selected)
    keep = with_parents(original_keep | hinted)
    result = []
    for s in sessions:
        if s.id in selected:
            result.append(s)
        elif s.id in keep:
            # Preserve the replay inputs that project selection used before
            # hints. Newly included sessions only supply attribution evidence.
            result.append(replace(s, edits=[], reads=s.reads if s.id in original_keep else []))
    return result


# Stands for the folder a portable graph was built in. See `build`.
ROOT = "/$PAPERTRAIL_ROOT"


def swap(value, old, new):
    """Replace one folder's path with another in every string of a nested value."""
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [swap(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: swap(v, old, new) for k, v in value.items()}
    return value


def portable_root(store):
    """The folder a portable graph belongs to, found from where the store is now."""
    store = Path(store).resolve()
    if not store.is_file():
        return None
    try:
        with relate.connect(str(store), read_only=True) as db:
            rows = db.execute("MATCH (m:PaperTrail) RETURN m.portable AS portable").data()
    except relate.Error:
        return None
    relative = rows[0]["portable"] if rows else None
    return (store.parent / relative).resolve() if relative else None


def build(store, sessions, stage=None, portable=None):
    """Write the graph. `stage`, if given, hears ("replay") and ("write", files=N).

    `portable` names a folder that holds the project, its logs and the store. Its path
    is written as a placeholder, and the graph records where the store sits inside it,
    so the folder can be copied to another machine and the queries still find the files.
    Rebuilding a portable graph keeps it portable.
    """
    store = Path(store)
    portable = portable or portable_root(store)
    if store.suffix != ".rl":
        raise ValueError("Choose a single-file --store ending in .rl.")
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The graph holds private prompts. Keep a project's own folder out of its git history.
    ignore = store.parent / ".gitignore"
    if store.parent.name == ".papertrail" and not ignore.exists():
        ignore.write_text("*\n")
    store = store.resolve()
    meta = {"version": 1}
    base = str(Path(portable).resolve()) if portable else None
    if base:
        meta["portable"] = os.path.relpath(base, store.parent)
    with TemporaryDirectory(prefix=".papertrail-build-", dir=store.parent) as tmp, ExitStack() as spill:
        def batches():
            return RowBatches(tmp, spill, base)

        nodes, edges = defaultdict(batches), defaultdict(batches)
        edit_rows, unplaced = collect_rows(sessions, nodes, edges, stage)
        if base:
            unplaced = {swap(key, base, ROOT) for key in unplaced}
        if stage:
            stage("write", files=nodes["File"].count)
        staged = Path(tmp) / "graph.rl"
        staged.touch(mode=0o600)
        with relate.connect(str(staged)) as db:
            db.autocommit = True
            db.execute("CREATE (m:PaperTrail) SET m = $meta", {"meta": meta})
            for label, rows in nodes.items():
                db.execute(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                )
                for batch in rows.batches():
                    if rows is edit_rows and unplaced:
                        for row in batch:
                            if row["id"] in unplaced:
                                row["unplaced"] = True
                    write_rows(db, f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", batch)
            for (a, kind, b), rows in edges.items():
                query = (
                    f"UNWIND $rows AS row MATCH (a:{a} {{id:row.a}}), (b:{b} {{id:row.b}}) "
                    f"CREATE (a)-[r:{kind}]->(b) SET r = row.props"
                )
                for batch in rows.batches():
                    write_rows(db, query, batch, kind)
        try:
            os.link(staged, store)
        except FileExistsError:
            # Pin the previous generation read-only while checking its identity.
            # An active Relate writer prevents opening it. Existing readers
            # keep their old inode until they reopen the published generation.
            with relate.connect(str(store), read_only=True) as previous:
                if (
                    previous.execute("MATCH (n) RETURN count(n) AS n").data()[0]["n"]
                    and not previous.execute(
                        "MATCH (:PaperTrail {version:1}) RETURN 1 AS ok"
                    ).data()
                ):
                    raise ValueError(
                        "Existing store belongs to another application. Choose a fresh --store."
                    )
                os.replace(staged, store)


def collect_rows(sessions, nodes, edges, stage):
    def link(a, kind, b, left, right, **attrs):
        edges[a, kind, b].append({"a": left, "b": right, "props": attrs})

    for agent in sorted({s.agent for s in sessions}):
        nodes["Agent"].append({"id": agent, "name": agent})
    ids = {s.id for s in sessions}
    for s in sessions:
        nodes["Session"].append(
            {k: v for k, v in vars(s).items() if k not in ("edits", "prompts", "reads", "hints")}
        )
        link("Agent", "RAN", "Session", s.agent, s.id)
        if s.parent in ids:
            link("Session", "SPAWNED", "Session", s.parent, s.id)
        for p in s.prompts:
            nodes["Prompt"].append(asdict(p))
            link("Session", "HAS_PROMPT", "Prompt", s.id, p.id)
        if s.prompts:
            link("Session", "FIRST_PROMPT", "Prompt", s.id, s.prompts[0].id)
        for a, b in pairwise(s.prompts):
            link("Prompt", "NEXT", "Prompt", a.id, b.id)
        ordered = sorted(s.edits, key=lambda e: (e.ts, e.seq))
        for e in ordered:
            nodes["Edit"].append(
                {
                    k: v
                    for k, v in vars(e).items()
                    if k not in ("prompt", "before", "after", "patch", "replacement", "appended")
                }
            )
            link("Session", "HAS_EDIT", "Edit", s.id, e.id)
            if e.prompt:
                link("Prompt", "LED_TO", "Edit", e.prompt, e.id)
            link("Edit", "TOUCHED", "File", e.id, e.path)
        for a, b in pairwise(ordered):
            link("Edit", "NEXT", "Edit", a.id, b.id)
    edit_rows, unplaced = nodes["Edit"], set()
    hints = {h.id: (s.id, h) for s in sessions for h in s.hints}
    used_hints = set()
    if stage:
        stage("replay")
    for f in histories(sessions):
        unplaced.update(f.unplaced_edits)
        nodes["File"].append(
            {
                "id": f.path,
                "path": f.path,
                "status": f.status,
                "lines": f.lines,
                "sha": f.sha,
                "unapplied": f.unapplied,
                "external_events": f.external_events,
            }
        )
        for h in f.hunks:
            row = {
                k: v for k, v in vars(h).items()
                if k not in ("edit", "replaced", "inferred_from", "basis", "was")
            }
            if h.kind == "inferred":
                row.update(basis=h.basis, was=h.was)
            nodes["Hunk"].append(row)
            link("File", "HAS_HISTORY", "Hunk", f.path, h.id)
            if h.basis == "command":
                sid, hint = hints[h.inferred_from]
                if hint.id not in used_hints:
                    nodes["Hint"].append({
                        k: getattr(hint, k) for k in ("id", "seq", "ts", "tool", "model", "effort")
                    })
                    link("Session", "HAS_HINT", "Hint", sid, hint.id)
                    if hint.prompt:
                        link("Prompt", "LED_TO", "Hint", hint.prompt, hint.id)
                    used_hints.add(hint.id)
                link("Hunk", "INFERRED_FROM", "Hint", h.id, hint.id)
            elif h.basis == "nearby":
                link("Hunk", "INFERRED_FROM", "Edit", h.id, h.inferred_from)
            elif h.edit:
                link("Edit", "HAS_HUNK", "Hunk", h.edit, h.id)
            for old, count in h.replaced.items():
                link("Hunk", "REPLACED", "Hunk", h.id, old, lines=count)
        carried = {origin for _, _, origin in f.ranges} - {h.id for h in f.hunks}
        for origin in carried:
            link("File", "HAS_HISTORY", "Hunk", f.path, origin)
        for start, end, origin in f.ranges:
            key = f"{f.path}#{start}"
            nodes["Range"].append({"id": key, "start": start, "end": end})
            link("File", "HAS_RANGE", "Range", f.path, key)
            link("Range", "FROM", "Hunk", key, origin)
        # A deletion has no line left to point at, so it gets a spot between two.
        for i, (after, origin) in enumerate(f.gaps):
            key = f"{f.path}@{i}"
            nodes["Gap"].append({"id": key, "after": after})
            link("File", "HAS_GAP", "Gap", f.path, key)
            link("Gap", "FROM", "Hunk", key, origin)
    return edit_rows, unplaced


# Leave room for Relate's WAL overhead. The exact limit is enforced by write_rows.
BATCH_BYTES = 8 * 1024 * 1024


def estimated_bytes(value):
    if isinstance(value, str):
        return 8 + len(value) * (1 if value.isascii() else 4)
    if isinstance(value, dict):
        return 16 + sum(estimated_bytes(k) + estimated_bytes(v) for k, v in value.items())
    if isinstance(value, list):
        return 16 + sum(map(estimated_bytes, value))
    return 16


class RowBatches:
    """Buffer one batch per row type and spill full batches into the private build directory."""

    def __init__(self, directory, files, base):
        self.directory = directory
        self.files = files
        self.base = base
        self.pending = []
        self.size = self.count = self.spilled = 0
        self.spool = None

    def append(self, row):
        if self.base:
            row = swap(row, self.base, ROOT)
        size = estimated_bytes(row)
        if self.pending and self.size + size > BATCH_BYTES:
            if self.spool is None:
                self.spool = self.files.enter_context(TemporaryFile(dir=self.directory))
            pickle.dump(self.pending, self.spool, protocol=pickle.HIGHEST_PROTOCOL)
            self.spilled += 1
            self.pending = []
            self.size = 0
        self.pending.append(row)
        self.size += size
        self.count += 1

    def batches(self):
        if self.spool is not None:
            self.spool.seek(0)
            for _ in range(self.spilled):
                yield pickle.load(self.spool)
        yield self.pending


def write_rows(db, query, rows, relationship=None):
    try:
        result = db.execute(query, {"rows": rows})
    except relate.DataError as exc:
        if "WAL record" not in str(exc) or len(rows) <= 1:
            raise
        middle = len(rows) // 2
        write_rows(db, query, rows[:middle], relationship)
        write_rows(db, query, rows[middle:], relationship)
        return
    if relationship and result.stats["relationships_created"] != len(rows):
        raise ValueError(f"Incomplete {relationship} relationships. Check source identities.")
