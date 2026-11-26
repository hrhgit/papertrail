"""Read-only query API. Every response includes the actual parameterized statements."""

import hashlib
import heapq
from collections import OrderedDict, defaultdict, deque
from functools import lru_cache
from pathlib import Path

import relate

from .graph import ROOT, swap

INFERENCE_OWNER = """
OPTIONAL MATCH (h)-[:INFERRED_FROM]->(evidence)<-[ownership]-(likely:Session)
WHERE type(ownership) IN ['HAS_HINT', 'HAS_EDIT']
OPTIONAL MATCH (instruction:Prompt)-[:LED_TO]->(evidence)
"""
OWNER = """
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h)
OPTIONAL MATCH (p:Prompt)-[:LED_TO]->(e)
""" + INFERENCE_OWNER
FIELDS = """h.kind AS kind, h.basis AS basis, h.was AS was, h.ts AS ts,
coalesce(s.agent, likely.agent) AS agent, coalesce(e.model, evidence.model) AS model,
coalesce(e.effort, evidence.effort) AS effort, coalesce(s.id, likely.id) AS session,
coalesce(s.title, likely.title) AS title, coalesce(p.text, instruction.text) AS prompt,
coalesce(e.tool, evidence.tool) AS tool"""
RANGE = "MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)"
TRACE = (
    RANGE
    + """
WHERE r.start <= $line AND r.end >= $line
MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
"""
    + OWNER
    + """
RETURN length(chain) AS depth, [n IN nodes(chain) | n.id] AS chain,
[edge IN relationships(chain) | edge.lines] AS replaced_lines,
coalesce(e.id, evidence.id) AS edit, coalesce(s.log_path, likely.log_path) AS log_path,
coalesce(s.cwd, likely.cwd) AS cwd, coalesce(s.resume, likely.resume) AS resume,
coalesce(s.resume_cwd, likely.resume_cwd) AS resume_cwd,
[] AS previous_files, [] AS next_files, """
    + FIELDS
    + " ORDER BY depth, chain"
)

SESSION_EDITS = """
UNWIND $sessions AS id
MATCH (s:Session {id:id})-[:HAS_EDIT]->(e:Edit)
OPTIONAL MATCH (e)-[:NEXT]->(following:Edit)
OPTIONAL MATCH (e)<-[:NEXT]-(previous:Edit)
RETURN s.id AS session, e.id AS edit, e.path AS path, e.ts AS ts, e.seq AS seq,
id(e) AS node, id(following) AS following, id(previous) AS previous
ORDER BY ts, seq"""

EDIT_FILES = """
UNWIND $ids AS id
MATCH (e:Edit {id:id})
CALL (e) {
  MATCH previous=(e)<-[:NEXT*1..]-(prev:Edit)
  WITH prev, length(previous) AS hops ORDER BY prev.ts, prev.seq, hops DESC
  RETURN collect(DISTINCT prev.path) AS previous_files
}
CALL (e) {
  MATCH (e)-[:NEXT*1..]->(following:Edit)
  WITH following ORDER BY following.ts, following.seq RETURN collect(DISTINCT following.path) AS next_files
}
RETURN e.id AS edit, previous_files, next_files"""

TRACK_RANGES = (
    RANGE.replace("owner:Hunk", "h:Hunk")
    + """
WHERE ($start IS NULL OR r.end >= $start) AND ($end IS NULL OR r.start <= $end)
"""
    + INFERENCE_OWNER
    + """
RETURN r.start AS start, r.end AS end, h.kind AS kind, h.id AS hunk, h.ts AS ts,
h.basis AS basis, h.was AS was, likely.id AS session, likely.agent AS agent,
left(instruction.text, 80) AS prompt, evidence.model AS model, evidence.tool AS tool
ORDER BY start"""
)

REPLACED_EDGES = """
MATCH (h:Hunk)-[:REPLACED]->(old:Hunk)
RETURN h.id AS hunk, old.id AS old"""

HUNK_SESSIONS = """
MATCH (h:Hunk)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
RETURN h.id AS hunk, s.id AS session, s.agent AS agent"""

CROSSING_TIPS = """
MATCH (f:File{file})-[:HAS_HISTORY]->(tip:Hunk)
WHERE EXISTS {{ MATCH (tip)<-[:FROM]-(:Range)<-[:HAS_RANGE]-(f) }}
   OR (NOT EXISTS {{ MATCH (tip)<-[:REPLACED]-() }}
       AND NOT EXISTS {{ MATCH (tip)<-[:FROM]-(:Range) }})
RETURN DISTINCT f.id AS id, f.path AS path, tip.id AS hunk"""

CROSSING_RANGES = """
UNWIND $tips AS crossing
MATCH (f:File {id:crossing.id}), (tip:Hunk {id:crossing.hunk})
OPTIONAL MATCH (tip)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(f)
RETURN crossing.path AS path, crossing.hunk AS hunk, r.start AS start, r.end AS end,
crossing.sessions AS sessions, size(crossing.sessions) AS session_count,
crossing.agents AS agents, size(crossing.agents) AS agent_count
ORDER BY session_count DESC, path, hunk, start"""


def file_version(stat):
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


@lru_cache(maxsize=128)
def file_fingerprint(path, version):
    digest, newlines, last = hashlib.sha256(), 0, b""
    with Path(path).open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    return digest.hexdigest(), newlines + bool(last and last != b"\n")


def _ordered_edit_chains(edits):
    """Follow NEXT to break ties in the database's (timestamp, sequence) order."""
    ranks = {(edit["ts"], edit["seq"]): index for index, edit in enumerate(edits)}
    pending = {edit["node"]: edit for edit in edits}
    if len(pending) != len(edits):
        return None
    chains = []
    for head in edits:
        if head["previous"] is not None:
            continue
        chain, edit = [], head
        while True:
            chain.append(pending.pop(edit["node"]))
            if edit["following"] is None:
                break
            following = pending.get(edit["following"])
            if (
                following is None
                or following["previous"] != edit["node"]
                or ranks[following["ts"], following["seq"]] < ranks[edit["ts"], edit["seq"]]
            ):
                return None
            edit = following
        chains.append(chain)
    return None if pending else chains


def _chain_files(chain, hits):
    previous, following, files = {}, OrderedDict(), {}
    for edit in chain:
        if edit["edit"] in hits:
            files[edit["edit"]] = {"previous_files": list(previous)}
        if edit["path"] is not None:
            previous[edit["path"]] = None
    for edit in reversed(chain):
        if edit["edit"] in files:
            files[edit["edit"]]["next_files"] = list(following)
        if edit["path"] is not None:
            following[edit["path"]] = None
            # A closer occurrence determines where a repeated path appears.
            following.move_to_end(edit["path"], last=False)
    return files


class Queries:
    def __init__(self, store):
        store = Path(store).resolve()
        if not store.is_file():
            raise ValueError("Store missing. Run papertrail ingest or supply its --store path.")
        self.db = relate.connect(str(store), read_only=True)
        # Resolve the portable folder using this connection's own snapshot.
        try:
            rows = self.db.execute(
                "MATCH (m:PaperTrail) RETURN m.portable AS portable, m.complete AS complete"
            ).data()
            meta = rows[0] if rows else {}
            self.complete = meta.get("complete") is not False
            relative = meta.get("portable")
            self.root = str((store.parent / relative).resolve()) if relative else None
        except BaseException:
            self.db.close()
            raise
        self.statements = []

    def _begin(self):
        self.statements.clear()
        self.validate()

    def validate(self):
        if not self._run("MATCH (:PaperTrail {version:1}) RETURN 1 AS ok"):
            raise ValueError("Unrecognized graph. Run papertrail ingest with a fresh --store.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _run(self, query, **params):
        self.statements.append(query.strip())
        if not self.root:
            return self.db.execute(query, params).data()
        rows = self.db.execute(query, swap(params, self.root, ROOT)).data()
        return swap(rows, ROOT, self.root)

    def _result(self, query, params, limit):
        if limit is not None:
            if limit < 1:
                raise ValueError("Use a positive --limit.")
            query += " LIMIT $cap"
            params["cap"] = limit + 1
        rows = self._run(query, **params)
        return {
            "rows": rows[:limit],
            "truncated": limit is not None and len(rows) > limit,
            "cypher": list(self.statements),
        }

    def _file(self, file):
        path = str(Path(file).expanduser().resolve())
        rows = self._run(
            "MATCH (f:File {id:$path}) RETURN f.lines AS lines, f.status AS status, f.sha AS sha",
            path=path,
        )
        if not rows:
            raise ValueError(
                "File has no recorded edits. Check --store and the file path, or rebuild the graph."
            )
        return path, rows[0]

    @staticmethod
    def _stale(path, info):
        """True when the file is no longer the one ingest compared against.

        Ranges are line numbers in that earlier file, so they may now point elsewhere."""
        if info["status"] in ("missing", "unreadable"):
            return False
        try:
            digest, lines = file_fingerprint(path, file_version(Path(path).stat()))
        except OSError:
            return True
        if info.get("sha"):
            return digest != info["sha"]
        # A graph built before the digest was stored can only compare line counts.
        return lines != info["lines"]

    @staticmethod
    def _bounds(info, start, end):
        if info["status"] in ("missing", "unreadable"):
            raise ValueError(
                f"File was {info['status']} at ingest. Use session or crossings for its history."
            )
        if not info["lines"]:
            raise ValueError("File was empty at ingest. Use session for edit history.")
        if not 1 <= start <= end <= info["lines"]:
            raise ValueError(
                f"Line outside snapshot. Select within 1..{info['lines']} or rerun ingest."
            )

    def blame(self, file, start=None, end=None, limit=None):
        self._begin()
        path, info = self._file(file)
        if (
            start is not None
            or end is not None
            or info["lines"]
            or info["status"] in ("missing", "unreadable")
        ):
            self._bounds(
                info,
                1 if start is None else start,
                info["lines"] if end is None else end,
            )
        query = (
            RANGE.replace("owner:Hunk", "h:Hunk")
            + """
WHERE r.end >= $start AND r.start <= $end
"""
            + OWNER
            + """
RETURN CASE WHEN r.start < $start THEN $start ELSE r.start END AS start,
CASE WHEN r.end > $end THEN $end ELSE r.end END AS end, """
            + FIELDS.replace(
                "coalesce(p.text, instruction.text)", "left(coalesce(p.text, instruction.text), 80)"
            )
            + " ORDER BY start"
        )
        result = self._result(
            query,
            {"path": path, "start": start or 1, "end": end or info["lines"]},
            limit,
        )
        return result | {"path": path, "stale": self._stale(path, info)}

    def trace(self, file, line, limit=None):
        self._begin()
        path, info = self._file(file)
        self._bounds(info, line, line)
        result = self._result(TRACE, {"path": path, "line": line}, limit)
        hits = [row for row in result["rows"] if row["edit"] is not None and row["basis"] != "command"]
        if hits:
            files = self._trace_files(hits)
            for row in result["rows"]:
                row.update(files.get(row["edit"], {}))
            result["cypher"] = list(self.statements)
        return result | {"path": path, "stale": self._stale(path, info)}

    def _trace_files(self, hits):
        ids = dict.fromkeys(row["edit"] for row in hits)
        sessions = list(dict.fromkeys(row["session"] for row in hits if row["session"] is not None))
        edits = defaultdict(list)
        if sessions:
            for row in self._run(SESSION_EDITS, sessions=sessions):
                edits[row["session"]].append(row)

        files = {}
        for session_edits in edits.values():
            for chain in _ordered_edit_chains(session_edits) or ():
                files.update(_chain_files(chain, ids))

        # Keep the old traversal for graphs with links outside a session's
        # ordered edit chains, or edits without an owning session.
        remaining = [edit for edit in ids if edit not in files]
        if remaining:
            files.update((row["edit"], row) for row in self._run(EDIT_FILES, ids=remaining))
        return files

    def _tracks(self, path, start=None, end=None):
        rows = self._run(TRACK_RANGES, path=path, start=start, end=end)
        if not rows:
            return rows
        edges, sessions = self._read_lineage()
        by_owner = {}
        for row in rows:
            owner = row["hunk"]
            if owner not in by_owner:
                by_owner[owner] = self._lanes(owner, edges, sessions)
            row["lanes"] = by_owner[owner]
        return rows

    def _read_lineage(self):
        if not hasattr(self, "_lineage"):
            edges, sessions = defaultdict(list), defaultdict(list)
            for row in self._run(REPLACED_EDGES):
                edges[row["hunk"]].append(row["old"])
            for row in self._run(HUNK_SESSIONS):
                sessions[row["hunk"]].append((row["session"], row["agent"]))
            self._lineage = edges, sessions
        else:
            self.statements.extend((REPLACED_EDGES.strip(), HUNK_SESSIONS.strip()))
        return self._lineage

    @staticmethod
    def _branches(owner, edges):
        # Relate's path expansion visits the most recently added edge first
        # and descends before moving on to the next branch.
        stack, seen = [owner], set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            yield node
            stack.extend(reversed(edges.get(node, ())))

    @classmethod
    def _lanes(cls, owner, edges, sessions):
        depths, queue = {owner: 0}, deque([owner])
        while queue:
            node = queue.popleft()
            for old in edges.get(node, ()):
                if old not in depths:
                    depths[old] = depths[node] + 1
                    queue.append(old)

        # The old path query visited branches depth-first. Keep that tie order
        # for equal session depths while skipping already visited hunks.
        lanes = {}
        for node in cls._branches(owner, edges):
            for session, _ in sessions.get(node, ()):
                if session is not None:
                    lanes[session] = min(lanes.get(session, depths[node]), depths[node])
        return [
            {"session": session, "depth": depth}
            for session, depth in sorted(lanes.items(), key=lambda lane: lane[1])
        ]

    def session(self, session_id, limit=None):
        self._begin()
        hits = self._run(
            "MATCH (s:Session) WHERE s.id STARTS WITH $key RETURN s.id AS id",
            key=session_id,
        )
        if not any(r["id"] == session_id for r in hits):
            if len(hits) != 1:
                raise ValueError(
                    "Session missing or prefix ambiguous. Use the full ID from blame or trace."
                )
            session_id = hits[0]["id"]
        query = """
MATCH (s:Session {id:$id})-[rel]->(event)
WHERE type(rel) IN ['HAS_PROMPT', 'HAS_EDIT']
OPTIONAL MATCH (instruction:Prompt)-[:LED_TO]->(event)
OPTIONAL MATCH (event)-[:TOUCHED]->(file:File)
RETURN type(rel) AS event, event.seq AS seq, event.ts AS ts, event.text AS text,
event.id AS id, instruction.id AS prompt_id, instruction.text AS instruction, file.path AS path, event.tool AS tool ORDER BY ts, seq"""
        return self._result(query, {"id": session_id}, limit)

    def crossings(self, file=None, limit=None):
        self._begin()
        if not file:
            return self._crossings(None, limit)
        path, info = self._file(file)
        return self._crossings(path, limit) | {"path": path, "stale": self._stale(path, info)}

    def _crossings(self, path, limit):
        if limit is not None and limit < 1:
            raise ValueError("Use a positive --limit.")
        tips = self._run(CROSSING_TIPS.format(file=" {id:$path}" if path else ""), path=path)
        if not tips:
            return {"rows": [], "truncated": False, "cypher": list(self.statements)}
        edges, owners = self._read_lineage()

        def shared_tips():
            for tip in tips:
                sessions, agents = {}, {}
                for node in self._branches(tip["hunk"], edges):
                    for session, agent in owners.get(node, ()):
                        if session is not None:
                            sessions[session] = None
                        if agent is not None:
                            agents[agent] = None
                if len(sessions) >= 2:
                    yield tip | {"sessions": list(sessions), "agents": list(agents)}

        candidates = shared_tips()
        if limit is not None:
            # Every tip contributes at least one row. Limit the tips before
            # looking up ranges, then limit the final rows too.
            candidates = heapq.nsmallest(
                limit + 1, candidates, key=lambda tip: (-len(tip["sessions"]), tip["path"], tip["hunk"]),
            )
        return self._result(CROSSING_RANGES, {"tips": list(candidates)}, limit)

    def stats(self, limit=None):
        self._begin()
        counts = self._stats()
        return {"rows": [counts], "complete": self.complete, "truncated": False, "cypher": list(self.statements)}

    def _stats(self):
        counts = self._run("MATCH ()-[r]->() RETURN count(r) AS relationships")[0]
        for label in ("Agent", "Session", "Prompt", "Edit", "Hunk", "File", "Range"):
            counts[label.lower() + "s"] = self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0][
                "n"
            ]
        counts["lines"] = dict.fromkeys(
            ("agent", "inferred", "baseline", "external", "drift", "unknown"), 0
        )
        for row in self._run(
            "MATCH (r:Range)-[:FROM]->(h:Hunk) RETURN h.kind AS kind, sum(r.end-r.start+1) AS n"
        ):
            counts["lines"][row["kind"]] = row["n"]
        counts["files_by_status"] = {
            r["status"]: r["n"]
            for r in self._run("MATCH (f:File) RETURN f.status AS status, count(f) AS n")
        }
        counts.update(
            self._run(
                "MATCH (f:File) RETURN sum(f.unapplied) AS unapplied_patches, sum(f.external_events) AS external_events"
            )[0]
        )
        counts["by_agent"] = agents = {
            row["agent"]: {"sessions": row["n"], "edits": 0, "lines": 0, "unplaced": 0}
            for row in self._run("MATCH (s:Session) RETURN s.agent AS agent, count(s) AS n")
        }
        for row in self._run(
            "MATCH (s:Session)-[:HAS_EDIT]->(e:Edit) RETURN s.agent AS agent, "
            "count(e) AS edits, sum(CASE WHEN e.unplaced THEN 1 ELSE 0 END) AS unplaced"
        ):
            agents[row["agent"]].update(edits=row["edits"], unplaced=row["unplaced"])
        for row in self._run(
            "MATCH (s:Session)-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(:Hunk)<-[:FROM]-(r:Range) "
            "RETURN s.agent AS agent, sum(r.end-r.start+1) AS n"
        ):
            agents[row["agent"]]["lines"] = row["n"]
        total = sum(counts["lines"].values())
        counts["agent_fraction"] = counts["lines"]["agent"] / total if total else None
        attributed = counts["lines"]["agent"] + counts["lines"]["inferred"]
        counts["agent_plus_inferred_fraction"] = attributed / total if total else None
        return counts
