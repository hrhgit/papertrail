"""Command-line views over the public query API."""

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

import relate

from .graph import build, select
from .queries import Queries
from .sources import DEFAULT_LOGS, SOURCES, ReadProgress, load_sources


class TerminalProgress(ReadProgress):
    """One self-erasing stderr line while the logs are read. Counts only."""

    def __init__(self, stream):
        super().__init__()
        self.stream, self.shown = stream, 0.0

    def read(self, name, path, nbytes=None):
        super().read(name, path, nbytes)
        if perf_counter() - self.shown > 0.2:
            self.shown = perf_counter()
            files = sum(s["files"] for s in self.sources.values())
            done = sum(s["read_files"] for s in self.sources.values())
            gb = sum(s["bytes"] for s in self.sources.values()) / 1e9
            self.stream.write(
                f"\rReading agent logs {self.fraction():4.0%}  {done} of {files} files, {gb:.1f} GB"
            )
            self.stream.flush()

    def clear(self):
        if self.shown:
            self.stream.write("\r\033[K")
            self.stream.flush()


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("Use a positive integer.")
    return n


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    app = Parser(prog="papertrail")
    commands = app.add_subparsers(dest="command", required=True)
    for name in ("ingest", "blame", "trace", "session", "crossings", "stats"):
        p = commands.add_parser(name)
        p.add_argument("--store", type=Path)
        p.add_argument("--json", action="store_true")
        if name == "ingest":
            p.add_argument("--source", choices=SOURCES)
            p.add_argument(
                "--logs",
                type=Path,
                help="Log root for --source (defaults to Claude Code with this option).",
            )
            p.add_argument("--claude-logs", type=Path)
            p.add_argument("--codex-logs", type=Path)
            group = p.add_mutually_exclusive_group()
            group.add_argument("--project", type=Path)
            group.add_argument("--all", action="store_true")
        else:
            p.add_argument("--limit", type=positive)
            p.add_argument("--show-cypher", action="store_true")
        if name == "blame":
            p.add_argument("file")
            p.add_argument("-L", metavar="START,END")
        elif name == "trace":
            p.add_argument("location", metavar="FILE:LINE")
        elif name == "crossings":
            p.add_argument("file", nargs="?")
        elif name == "session":
            p.add_argument("id")
    ui = commands.add_parser("ui", help="Open the line-history reader in a browser.")
    ui.add_argument("--store", type=Path)
    ui.add_argument("--project", type=Path, help="Folder to open (defaults to this directory).")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    return app


def model_label(row):
    """Model plus reasoning effort, e.g. ``claude-opus-5-5 (high)``."""
    model, effort = row.get("model") or "", row.get("effort") or ""
    return f"{model} ({effort})" if model and effort else model or effort


def display(command, result):
    last_prompt = None
    for r in result["rows"]:
        if command == "blame":
            print(
                f"{r['start']:>5}-{r['end']:<5} {r['agent'] or r['kind']}  {model_label(r)}  {r['ts']}  "
                f"{(r['session'] or '')[:8]}  {r['title'] or ''}  {' '.join((r['prompt'] or '').split())}"
            )
        elif command == "trace":
            print(
                f"[{r['depth']}] {r['agent'] or r['kind']}  {model_label(r)}  {r['ts']}  {r['title'] or ''}"
            )
            print(
                "  Lineage",
                " <- ".join("#" + key.rsplit("#", 1)[-1] for key in r["chain"]),
            )
            if r["session"]:
                print("  Instruction", r["prompt"] or "(unknown)")
                print("  Session", r["session"], "in", r["resume_cwd"] or r["cwd"])
                print(" ", r["resume"] or "", "\n  Log", r["log_path"])
                print(
                    "  Previous",
                    ", ".join(r["previous_files"]) or "(none)",
                    "\n  Next",
                    ", ".join(r["next_files"]) or "(none)",
                )
        elif command == "session":
            if r["event"] == "HAS_PROMPT":
                last_prompt = r["id"]
                print(r["ts"], r["text"])
            else:
                if r["prompt_id"] and r["prompt_id"] != last_prompt:
                    print("  Instruction", r["instruction"])
                last_prompt = r["prompt_id"]
                print(r["ts"], r["tool"], r["path"])
        elif command == "crossings":
            location = f"{r['start']}-{r['end']}" if r["start"] is not None else "(historical)"
            print(r["session_count"], r["path"], location, ", ".join(r["sessions"]))
        else:
            print(json.dumps(r, indent=2))
    if not result["rows"]:
        print("No matching rows.")
    if result["truncated"]:
        print("More rows available. Increase --limit.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        command = args.command
        project = (getattr(args, "project", None) or Path.cwd()).resolve()
        store = (args.store or project / ".papertrail/graph.rl").expanduser()
        if command == "ui":
            from .web import serve

            if args.store and not store.is_file():
                raise ValueError("Store missing. Run papertrail ingest or supply its --store path.")
            # Without a graph the page opens on this folder and offers to build one.
            folder = project if (args.project or not args.store) else None
            serve(store, port=args.port, open_browser=not args.no_browser, project=folder)
            return 0
        if command == "ingest":
            if not args.all and not project.is_dir():
                raise ValueError(
                    "Project directory missing. Supply an existing --project directory."
                )
            start = perf_counter()
            chosen = args.source or ("claude-code" if args.logs else None)
            overrides = {"claude-code": args.claude_logs, "codex": args.codex_logs}
            if args.logs and overrides[chosen]:
                raise ValueError("Use one log root for the selected source.")
            if chosen and any(path for name, path in overrides.items() if name != chosen):
                raise ValueError("Remove --source to use both per-source log roots.")
            roots = {
                name: args.logs or overrides[name] or Path.home() / DEFAULT_LOGS[name]
                for name in SOURCES
                if not chosen or name == chosen
            }
            progress = TerminalProgress(sys.stderr) if sys.stderr.isatty() else None
            try:
                parsed, diagnostics = load_sources(roots, progress)
            finally:
                if progress:
                    progress.clear()
            sessions = select(parsed, None if args.all else project)
            build(store, sessions)
            with Queries(store) as q:
                result = q.stats()
            result["rows"][0].update(
                seconds=round(perf_counter() - start, 3),
                sources=diagnostics,
                source_failures={
                    k: sum(d.get("failures", {}).get(k, 0) for d in diagnostics.values())
                    for d in diagnostics.values()
                    for k in d.get("failures", {})
                },
                source_skipped={
                    k: sum(d.get("skipped", {}).get(k, 0) for d in diagnostics.values())
                    for d in diagnostics.values()
                    for k in d.get("skipped", {})
                },
            )
        else:
            with Queries(store) as q:
                if command == "blame":
                    bounds = args.L.split(",") if args.L else [None, None]
                    if len(bounds) != 2:
                        raise ValueError("Use -L START,END with two positive line numbers.")
                    result = q.blame(
                        args.file,
                        *(int(x) if x is not None else None for x in bounds),
                        limit=args.limit,
                    )
                elif command == "trace":
                    file, sep, number = args.location.rpartition(":")
                    if not sep or not number.isdecimal():
                        raise ValueError("Use trace FILE:LINE with a positive line number.")
                    result = q.trace(file, int(number), args.limit)
                elif command == "session":
                    result = q.session(args.id, args.limit)
                elif command == "crossings":
                    result = q.crossings(args.file, args.limit)
                else:
                    result = q.stats(args.limit)
        statements = result.pop("cypher")
        if getattr(args, "show_cypher", False):
            if args.json:
                result["cypher"] = statements
            else:
                print("\n\n".join(statements))
        if args.json:
            print(json.dumps(result))
        else:
            display(command, result)
        return 0
    except (ValueError, OSError, relate.Error, NotImplementedError) as exc:
        message = str(exc)
        if isinstance(exc, relate.Error):
            message = (
                f"Relate {type(exc).__name__}. Check store access, version and active writers."
            )
        print(
            json.dumps({"error": message}) if "--json" in argv else message,
            file=sys.stderr,
        )
        return 1
