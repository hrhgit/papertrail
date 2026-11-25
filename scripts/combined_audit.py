"""Count replay results without emitting transcript strings, commands or paths."""

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from time import perf_counter

from papertrail.graph import select
from papertrail.replay import histories
from papertrail.sources import DEFAULT_LOGS, load_sources


def summary(sessions, folder=None):
    agents = {s.id: s.agent for s in sessions}
    writers = {e.id: s.id for s in sessions for e in s.edits}
    by_agent = {
        a: Counter(sessions=sum(s.agent == a for s in sessions)) for a in set(agents.values())
    }
    for s in sessions:
        by_agent[s.agent]["edits"] += sum(
            not folder or Path(e.path).is_relative_to(folder) for e in s.edits
        )
    kinds, parents, owners = {}, {}, {}
    ranges, status, reasons = Counter(), Counter(), Counter()
    unknown_status = Counter()
    included = set()
    unknown_reasons = {}
    all_range_owners = set()
    replay_seconds = 0.0
    since = perf_counter()
    for f in histories(sessions):
        replay_seconds += perf_counter() - since
        inside = not folder or Path(f.path).is_relative_to(folder)
        for h in f.hunks:
            kinds[h.id], parents[h.id], owners[h.id] = (
                h.kind,
                h.replaced,
                writers.get(h.edit),
            )
            if inside:
                included.add(h.id)
        unknown_reasons.update(f.unknown_reasons)
        all_range_owners.update(h for _, _, h in f.ranges)
        if inside:
            status[f.status] += 1
            reasons.update(f.reasons)
            for key in f.unplaced_edits:
                by_agent[agents[writers[key]]]["unplaced"] += 1
            for a, b, key in f.ranges:
                ranges[key] += 1
                unknown_status[key, f.status] += b - a + 1
        since = perf_counter()
    replay_seconds += perf_counter() - since
    coverage, unknown, causes = Counter(), Counter(), Counter()
    for (key, state), count in unknown_status.items():
        coverage[kinds[key]] += count
        if owners[key]:
            by_agent[agents[owners[key]]]["lines"] += count
        if kinds[key] == "unknown":
            unknown[state] += count
            causes[unknown_reasons[key]] += count
    # Count the same terminal hunks and range rows as the crossings query.
    # Process shared predecessors once even when there are many possible paths.
    children, degree = defaultdict(list), {}
    for key, old in parents.items():
        degree[key] = len(old)
        for parent in old:
            children[parent].append(key)
    ancestry = {}
    queue = deque(k for k, n in degree.items() if not n)
    crossings = mixed = 0
    while queue:
        key = queue.popleft()
        writers_here = {owners[key]} if owners[key] else set()
        for parent in parents[key]:
            writers_here.update(ancestry[parent])
        ancestry[key] = writers_here
        if len(writers_here) >= 2:
            rows = ranges[key] or int(
                key in included and not children[key] and key not in all_range_owners
            )
            crossings += rows
            participants = {agents[s] for s in writers_here}
            for agent in participants:
                by_agent[agent]["crossings"] += rows
                if len(participants) > 1:
                    by_agent[agent]["cross_agent"] += rows
            if len(participants) >= 2:
                mixed += rows
        for child in children[key]:
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    return {
        "replay_seconds": round(replay_seconds, 3),
        "by_agent": by_agent,
        "lines": coverage,
        "files": status,
        "unknown_by_file_status": unknown,
        "unknown_reasons": causes,
        "unplaced_reasons": reasons,
        "crossings": crossings,
        "cross_agent": mixed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    parsed = []
    parse_time = 0
    for name, directory in DEFAULT_LOGS.items():
        start = perf_counter()
        batch, diagnostics = load_sources({name: Path.home() / directory})
        parse_time += perf_counter() - start
        parsed.extend(batch)
        result = summary(parsed)
        print(
            json.dumps(
                {
                    "sources": len({s.agent for s in parsed}),
                    **result,
                    "parse_replay_seconds": round(parse_time + result["replay_seconds"], 3),
                    "diagnostics": diagnostics,
                }
            ),
            flush=True,
        )
    selected = select(parsed, args.project)
    print(
        json.dumps({"project_files": summary(selected, args.project.resolve())}),
        flush=True,
    )


if __name__ == "__main__":
    main()
