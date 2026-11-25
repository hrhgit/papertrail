"""Report only aggregate Codex protocol and receipt counts. No graph or transcript output."""

import argparse
import json
from collections import Counter
from pathlib import Path

from papertrail.sources import CodexSource


def reconcile(source, sessions):
    """Independently compare raw completed per-file receipts to retained event IDs."""
    retained = Counter(e.id.rsplit(":", 1)[0] for s in sessions for e in s.edits)
    counts, seen = Counter(), set()
    for path in source.log_sizes:
        thread = None
        for line in source.raw_lines(path):
            try:
                record = json.loads(line)
                p = record["payload"]
                if record["type"] == "session_meta" and not thread:
                    thread = p["id"]
                if p.get("type") != "item_completed":
                    continue
                item = p["item"]
                if item.get("type") != "FileChange" or item.get("status") != "completed":
                    continue
                key = f"{p.get('thread_id', thread)}:e:{item['id']}"
                n = len(item["changes"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            counts["raw_file_receipts"] += n
            if key in seen:
                counts["replicated_file_receipts"] += n
                continue
            seen.add(key)
            counts["unique_file_receipts"] += n
            counts["matched_files"] += min(n, retained[key])
            counts["unmatched_files"] += max(0, n - retained[key])
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument(
        "--receipts",
        action="store_true",
        help="Cross-check completed file events in a second scan.",
    )
    args = parser.parse_args()
    source = CodexSource()
    sessions = list(source.sessions(args.logs))
    print(
        json.dumps(
            {
                **({"reconciliation": reconcile(source, sessions)} if args.receipts else {}),
                "sessions": len(sessions),
                "prompts": sum(len(s.prompts) for s in sessions),
                "counts": source.counts,
                "failures": source.failures,
                "skipped": source.skipped,
            }
        )
    )


if __name__ == "__main__":
    main()
