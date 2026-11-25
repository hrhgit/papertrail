"""Read-only query API. Every response includes the actual parameterized statements."""

from pathlib import Path

import relate

OWNER = """
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h)
OPTIONAL MATCH (p:Prompt)-[:LED_TO]->(e)
"""
FIELDS = """h.kind AS kind, h.ts AS ts, s.agent AS agent, e.model AS model, e.effort AS effort,
s.id AS session, s.title AS title, p.text AS prompt, e.tool AS tool"""
RANGE = "MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)"
TRACE = (
    RANGE
    + """
WHERE r.start <= $line AND r.end >= $line
MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
"""
    + OWNER
    + """
CALL (e) {
  MATCH previous=(e)<-[:NEXT*1..]-(prev:Edit)
  WITH prev, length(previous) AS hops ORDER BY prev.ts, prev.seq, hops DESC
  RETURN collect(DISTINCT prev.path) AS previous_files
}
CALL (e) {
  MATCH (e)-[:NEXT*1..]->(following:Edit)
  WITH following ORDER BY following.ts, following.seq RETURN collect(DISTINCT following.path) AS next_files
}
RETURN length(chain) AS depth, [n IN nodes(chain) | n.id] AS chain,
[edge IN relationships(chain) | edge.lines] AS replaced_lines,
e.id AS edit, s.log_path AS log_path, s.cwd AS cwd, s.resume AS resume, s.resume_cwd AS resume_cwd, previous_files, next_files, """
    + FIELDS
    + " ORDER BY depth, chain"
)


class Queries:
    def __init__(self, store):
        if not Path(store).is_file():
            raise ValueError("Store missing. Run papertrail ingest or supply its --store path.")
        self.db = relate.connect(str(store), read_only=True)
        self.statements = []

    def _begin(self):
        self.statements.clear()
        if not self._run("MATCH (:PaperTrail {version:1}) RETURN 1 AS ok"):
            raise ValueError("Unrecognized graph. Run papertrail ingest with a fresh --store.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _run(self, query, **params):
        self.statements.append(query.strip())
        return self.db.execute(query, params).data()

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
            "MATCH (f:File {id:$path}) RETURN f.lines AS lines, f.status AS status",
            path=path,
        )
        if not rows:
            raise ValueError(
                "File has no recorded edits. Check --store and the file path, or rebuild the graph."
            )
        return path, rows[0]

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
            + FIELDS.replace("p.text AS prompt", "left(p.text, 80) AS prompt")
            + " ORDER BY start"
        )
        return self._result(
            query,
            {"path": path, "start": start or 1, "end": end or info["lines"]},
            limit,
        )

    def trace(self, file, line, limit=None):
        self._begin()
        path, info = self._file(file)
        self._bounds(info, line, line)
        return self._result(TRACE, {"path": path, "line": line}, limit)

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
        path = self._file(file)[0] if file else None
        return self._crossings(path, limit)

    def _crossings(self, path, limit):
        # A separate REPLACED match makes Relate 1.1.0 count all relationships
        # for each tip. Keep the walk in the same pattern as HAS_HISTORY.
        query = (
            "MATCH (f:File"
            + (" {id:$path}" if path else "")
            + """)-[:HAS_HISTORY]->(tip:Hunk)-[:REPLACED*0..]->(h:Hunk)
      <-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
WITH f, tip, collect(DISTINCT s.id) AS sessions, collect(DISTINCT s.agent) AS agents
WHERE size(sessions) >= 2 AND (
  EXISTS { MATCH (tip)<-[:FROM]-(:Range)<-[:HAS_RANGE]-(f) }
  OR (NOT EXISTS { MATCH (tip)<-[:REPLACED]-() }
      AND NOT EXISTS { MATCH (tip)<-[:FROM]-(:Range) })
)
OPTIONAL MATCH (tip)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(f)
RETURN f.path AS path, tip.id AS hunk, r.start AS start, r.end AS end, sessions,
size(sessions) AS session_count, agents, size(agents) AS agent_count ORDER BY session_count DESC, path, hunk, start"""
        )
        return self._result(query, {"path": path}, limit)

    def stats(self, limit=None):
        self._begin()
        counts = self._stats()
        return {"rows": [counts], "truncated": False, "cypher": list(self.statements)}

    def _stats(self):
        counts = self._run("MATCH ()-[r]->() RETURN count(r) AS relationships")[0]
        for label in ("Agent", "Session", "Prompt", "Edit", "Hunk", "File", "Range"):
            counts[label.lower() + "s"] = self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0][
                "n"
            ]
        counts["lines"] = dict.fromkeys(("agent", "baseline", "external", "drift", "unknown"), 0)
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
        return counts
