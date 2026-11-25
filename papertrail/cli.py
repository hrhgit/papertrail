"""Command-line views over the public query API."""

import argparse
import json
import shlex
import shutil
import sys
from importlib.resources import files as package_files
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


SKILL_HOMES = {"claude-code": ".claude/skills", "codex": ".codex/skills"}


def own_command():
    """How an agent in another folder starts this program."""
    found = shutil.which("papertrail")
    # A copy inside this environment is only on PATH while the environment is active.
    if found and not Path(found).absolute().is_relative_to(Path(sys.prefix).absolute()):
        return "papertrail"
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "pyproject.toml").is_file() and shutil.which("uv"):
        return f"uv run --project {shlex.quote(str(checkout))} papertrail"
    return f"{shlex.quote(sys.executable)} -m papertrail"


def skill_text(store=None):
    """The agent skill, with this installation's command and store filled in."""
    text = (package_files("papertrail") / "skill" / "SKILL.md").read_text(encoding="utf-8")
    option = f" --store {shlex.quote(str(store))}" if store else ""
    return text.replace("{cmd}", own_command()).replace("{store}", option)


def skill_path(agent, project, everywhere):
    """Claude Code reads skills from the project or from home; Codex from home."""
    root = Path.home() if everywhere or agent == "codex" else project
    return root / SKILL_HOMES[agent] / "papertrail" / "SKILL.md"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


HELP = {
    "ingest": "Build the graph from local Claude Code and Codex logs.",
    "blame": "Show who wrote each line of a file, and the instruction behind it.",
    "trace": "Follow one line back through every change that replaced it.",
    "session": "List one session's instructions and edits in order.",
    "crossings": "List the spots that two or more sessions rewrote.",
    "stats": "Count what the graph holds and how many lines have a known writer.",
}
EXAMPLES = """examples:
  papertrail ingest --project .          build .papertrail/graph.rl for this folder
  papertrail blame src/app.py -L 10,25   who wrote lines 10 to 25, and why
  papertrail trace src/app.py:17         everything line 17 replaced, newest first
  papertrail session 3f9a                what else that session did
  papertrail ui                          read it in the browser

Queries read .papertrail/graph.rl in the current folder unless --store names another
graph. Add --json for machine-readable rows and --show-cypher to see the statements.
"""


def parser():
    app = Parser(
        prog="papertrail",
        description="Find which coding-agent session wrote a line of code, "
        "and the instruction it was following.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = app.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in HELP.items():
        p = commands.add_parser(name, help=text, description=text)
        p.add_argument(
            "--store",
            type=Path,
            metavar="PATH",
            help="Graph file to write."
            if name == "ingest"
            else "Graph file to read (default: .papertrail/graph.rl in this folder).",
        )
        p.add_argument("--json", action="store_true", help="Print one JSON object.")
        if name == "ingest":
            p.add_argument(
                "--source", choices=SOURCES, help="Read only this agent's logs (default: all)."
            )
            p.add_argument(
                "--logs",
                type=Path,
                metavar="DIR",
                help="Log root for --source (defaults to Claude Code with this option).",
            )
            p.add_argument(
                "--claude-logs",
                type=Path,
                metavar="DIR",
                help="Claude Code log root (default: ~/.claude/projects).",
            )
            p.add_argument(
                "--codex-logs",
                type=Path,
                metavar="DIR",
                help="Codex log root (default: ~/.codex/sessions).",
            )
            group = p.add_mutually_exclusive_group()
            group.add_argument(
                "--project",
                type=Path,
                metavar="DIR",
                help="Keep sessions that edited a file under DIR (default: this folder). "
                "The graph goes to DIR/.papertrail/graph.rl unless --store is given.",
            )
            group.add_argument(
                "--all", action="store_true", help="Keep every session in the logs."
            )
            p.add_argument(
                "--portable",
                type=Path,
                metavar="DIR",
                help="Write paths relative to DIR, a folder holding the project, the logs and "
                "the graph, so the folder can be copied or committed and queried elsewhere.",
            )
        else:
            p.add_argument(
                "--limit",
                type=positive,
                metavar="N",
                help="Return at most N rows and report whether more exist.",
            )
            p.add_argument(
                "--show-cypher", action="store_true", help="Also print the Cypher that ran."
            )
        if name == "blame":
            p.add_argument("file", metavar="FILE", help="A file agents edited.")
            p.add_argument(
                "-L", metavar="START,END", help="Only these lines, one-based and inclusive."
            )
        elif name == "trace":
            p.add_argument(
                "location", metavar="FILE:LINE", help="A current line, for example src/app.py:17."
            )
        elif name == "crossings":
            p.add_argument("file", nargs="?", metavar="FILE", help="Limit to one file.")
        elif name == "session":
            p.add_argument(
                "id", metavar="ID", help="Session ID from blame or trace. A unique prefix works."
            )
    skill = commands.add_parser(
        "skill", help="Teach a coding agent to look up line history before it edits."
    )
    skill.add_argument("--agent", choices=[*SKILL_HOMES, "all"], default="claude-code")
    skill.add_argument("--project", type=Path, help="Project to install into (this directory).")
    skill.add_argument(
        "--global", dest="everywhere", action="store_true", help="Install for every project."
    )
    skill.add_argument("--store", type=Path, help="Graph the agent should query.")
    skill.add_argument("--print", dest="show", action="store_true", help="Write nothing.")
    skill.add_argument("--json", action="store_true")
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


# What a row means when no session wrote the lines.
KINDS = {
    "baseline": "already there when agents first read the file",
    "external": "changed between two recorded edits, outside any session",
    "drift": "changed on disk after the last recorded edit",
    "unknown": "no recorded writer",
}


def when(ts):
    """Recorded times are UTC."""
    return f"{ts[:19].replace('T', ' ')} UTC" if ts else ""


def clip(text, width):
    """One line of at most `width` characters."""
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def short(path):
    """A path as the reader would type it from here."""
    for base, prefix in ((Path.cwd(), ""), (Path.home(), "~/")):
        try:
            return prefix + str(Path(path).relative_to(base))
        except ValueError:
            pass
    return str(path)


def heading(r):
    """Who wrote it. A session without a title repeats its first instruction, so skip that."""
    if not r["session"]:
        return "  ".join(x for x in (r["kind"], when(r["ts"]), KINDS.get(r["kind"], "")) if x)
    title = " ".join((r["title"] or "").split())
    if title and " ".join((r["prompt"] or "").split()).startswith(title[:40]):
        title = ""
    parts = (r["agent"], model_label(r), when(r["ts"]), r["session"][:8], clip(title, 50))
    return "  ".join(x for x in parts if x)


def display(command, result):
    last_prompt = None
    width = shutil.get_terminal_size((100, 24)).columns
    if result.get("stale"):
        print(
            f"{short(result['path'])} changed after this graph was built, so these line numbers "
            "may point at other lines. Run papertrail ingest again.",
            file=sys.stderr,
        )
    code = []
    if command == "blame" and not result.get("stale"):
        try:
            code = Path(result["path"]).read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            pass
    for r in result["rows"]:
        if command == "blame":
            span = str(r["start"]) if r["start"] == r["end"] else f"{r['start']}-{r['end']}"
            print(f"{span:<9} {heading(r)}")
            if r["session"]:
                # The query returns the first 80 characters.
                asked = r["prompt"] or "(instruction unknown)"
                print(" " * 10 + clip(asked + ("…" if len(asked) == 80 else ""), width - 10))
            for n in range(r["start"], r["end"] + 1) if code else ():
                print(f"{n:>7} │ {code[n - 1]}" if n <= len(code) else f"{n:>7} │")
        elif command == "trace":
            print(f"[{r['depth']}] {heading(r)}")
            print(
                "    Lineage  ",
                " <- ".join("#" + key.rsplit("#", 1)[-1] for key in r["chain"]),
            )
            if r["session"]:
                room = (width - 13) * 3
                asked = " ".join((r["prompt"] or "(unknown)").split())
                if len(asked) > room:
                    asked = clip(asked, room - 30) + " (--json has the full text)"
                print("    Asked    ", asked)
                print("    Session  ", r["session"], "in", short(r["resume_cwd"] or r["cwd"]))
                if r["resume"]:
                    print("    Resume   ", r["resume"])
                print("    Log      ", short(r["log_path"]))
                for label, files in (("Before", r["previous_files"]), ("After", r["next_files"])):
                    if files:
                        more = f" and {len(files) - 6} more" if len(files) > 6 else ""
                        print(f"    {label:<9}", ", ".join(short(f) for f in files[:6]) + more)
        elif command == "session":
            if r["event"] == "HAS_PROMPT":
                last_prompt = r["id"]
                print(when(r["ts"]), clip(r["text"], (width - 25) * 3))
            else:
                if r["prompt_id"] and r["prompt_id"] != last_prompt:
                    print("    from", clip(r["instruction"], width - 9))
                last_prompt = r["prompt_id"]
                print(when(r["ts"]), f"  {r['tool']}  {short(r['path'])}")
        elif command == "crossings":
            location = f"{r['start']}-{r['end']}" if r["start"] is not None else "(historical)"
            who = ", ".join(x[:8] for x in r["sessions"])
            print(f"{r['session_count']} sessions  {short(r['path'])}  {location}  {who}")
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
        if command == "skill":
            text = skill_text(args.store and store.resolve())
            if args.show:
                print(text, end="")
                return 0
            if not args.everywhere and not project.is_dir():
                raise ValueError(
                    "Project directory missing. Supply an existing --project directory."
                )
            agents = list(SKILL_HOMES) if args.agent == "all" else [args.agent]
            paths = [skill_path(agent, project, args.everywhere) for agent in agents]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            if args.json:
                rows = [{"agent": a, "path": str(p)} for a, p in zip(agents, paths)]
                print(json.dumps({"rows": rows}))
            else:
                for agent, path in zip(agents, paths):
                    print(f"Installed for {agent}: {path}")
                print("Start a new agent session to load it.")
            return 0
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
            build(store, sessions, portable=args.portable)
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
            result.pop("path", None)
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
