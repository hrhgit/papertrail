"""Aggregate-only provenance audit. Never print transcript values or file paths."""

import argparse
import json
from collections import Counter
from pathlib import Path
from time import perf_counter

from papertrail.graph import select
from papertrail.replay import histories
from papertrail.sources import ClaudeCodeSource
from papertrail.sources.claude import timestamp


def summary(sessions, folder=None, replay=histories):
    status, coverage, reasons = Counter(), Counter(), Counter()
    skipped = crossings = external = 0
    unknown_by_status = Counter()
    unplaced_by_status = Counter()
    unanchored = {
        e.id
        for session in sessions
        for e in session.edits
        if e.before is None and e.after is None and e.patch
    }
    positioned = set()
    authors = {e.id: s.id for s in sessions for e in s.edits}
    for f in replay(sessions):
        if folder and not Path(f.path).is_relative_to(folder):
            continue
        status[f.status] += 1
        positioned.update(h.edit for h in f.hunks if h.edit in unanchored)
        skipped += f.unapplied
        if f.unapplied:
            unplaced_by_status[f.status] += f.unapplied
        reasons.update(getattr(f, "reasons", {}))
        external += f.external_events
        kinds = {h.id: h.kind for h in f.hunks}
        ranges = Counter()
        for a, b, h in f.ranges:
            coverage[kinds[h]] += b - a + 1
            if kinds[h] == "unknown":
                unknown_by_status[f.status] += b - a + 1
            ranges[h] += 1
        # Same endpoints and projected range rows as Queries.crossings, without
        # materializing every distinct path just to count the matching rows.
        ancestry, replaced = {}, set()
        for h in f.hunks:
            writers = {authors[h.edit]} if h.edit else set()
            for old in h.replaced:
                writers.update(ancestry[old])
                replaced.add(old)
            ancestry[h.id] = writers
        crossings += sum(
            ranges[h] or int(h not in replaced) for h, v in ancestry.items() if len(v) >= 2
        )
    return {
        "files": status.total(),
        "status": status,
        "lines": coverage,
        "unapplied_patches": skipped,
        "agent_fraction": coverage["agent"] / coverage.total() if coverage.total() else None,
        "reasons": reasons,
        "patch_only_edits_with_hunks": len(positioned),
        "unknown_by_status": unknown_by_status,
        "unplaced_by_status": unplaced_by_status,
        "external_events": external,
        "crossings": crossings,
    }


def receipts(root, sessions):
    """Independently check every timestamped successful Write/Edit against retained calls."""
    identities = {
        (e.tool, e.id.split(":e:", 1)[1], e.ts)
        for s in sessions
        for e in s.edits
        if e.tool in ("Write", "Edit")
    }
    represented_ids = {(name, key) for name, key, _ in identities}
    totals, missing, filters, undated = Counter(), Counter(), Counter(), Counter()
    for path in root.expanduser().rglob("*.jsonl"):
        side = path.parent.name == "subagents"
        calls, seen = {}, set()
        with path.open("rb") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                uid = record.get("uuid")
                duplicate = uid in seen
                if uid:
                    seen.add(uid)
                content = (record.get("message") or {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") in ("Write", "Edit"):
                        calls[block.get("id")] = block["name"]
                    key = block.get("tool_use_id")
                    if (
                        block.get("type") != "tool_result"
                        or block.get("is_error")
                        or key not in calls
                    ):
                        continue
                    name = calls[key]
                    if not record.get("timestamp"):
                        known = (name, key) in represented_ids
                        undated["matched_call_id" if known else "missing_call_id"] += 1
                        continue
                    totals[("child_" if side else "main_") + name] += 1
                    if (name, key, timestamp(record["timestamp"])) not in identities:
                        missing[name] += 1
                    if not side and record.get("sessionId") not in (None, path.stem):
                        filters["foreign_session_" + name] += 1
                    elif duplicate:
                        filters["repeated_record_" + name] += 1
    return {
        "timestamped_success": totals,
        "without_retained_call": missing,
        "main_filters": filters,
        "undated": undated,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument("--project", type=Path)
    parser.add_argument(
        "--receipts", action="store_true", help="Check raw Write/Edit receipt coverage separately."
    )
    args = parser.parse_args()
    start = perf_counter()
    source = ClaudeCodeSource()
    sessions = list(source.sessions(args.logs))
    print(
        json.dumps(
            {
                "sessions": len(sessions),
                "prompts": sum(len(s.prompts) for s in sessions),
                "edits": Counter(e.tool for s in sessions for e in s.edits),
                "edits_with_prompt": sum(bool(e.prompt) for s in sessions for e in s.edits),
                "edits_by_session": Counter(
                    ("child_" if s.parent else "main_") + e.tool for s in sessions for e in s.edits
                ),
                "reads": Counter(
                    "child" if s.parent else "main" for s in sessions for _ in s.reads
                ),
                "failures": source.failures,
                "skipped": source.skipped,
                "seconds": round(perf_counter() - start, 3),
            }
        ),
        flush=True,
    )
    print(
        json.dumps({**summary(sessions), "seconds": round(perf_counter() - start, 3)}),
        flush=True,
    )
    if args.project:
        selected = select(sessions, args.project)
        print(
            json.dumps(
                {"selected": summary(selected), "within": summary(selected, args.project.resolve())}
            )
        )

    if args.receipts:
        print(json.dumps(receipts(args.logs, sessions)))


if __name__ == "__main__":
    main()
