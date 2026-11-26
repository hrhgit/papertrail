# How Paper Trail works

The [README](../README.md) covers what Paper Trail does and how to use it. This page covers how it builds and queries the graph, what the transcripts can and cannot prove, and how to check it.

## The graph

```text
Agent    -RAN->          Session -SPAWNED-> Session
Session  -FIRST_PROMPT-> Prompt  -NEXT->    Prompt
Session  -HAS_PROMPT->   Prompt  -LED_TO->  Edit
Session  -HAS_EDIT->     Edit    -NEXT->    Edit
Edit     -TOUCHED->      File    -HAS_RANGE-> Range -FROM-> Hunk
Edit     -HAS_HUNK->     Hunk    -REPLACED {lines}-> Hunk
File     -HAS_HISTORY->  Hunk
File     -HAS_GAP->      Gap     -FROM->    Hunk
```

A session has its own prompt and edit sequences. Independent sessions meet when a hunk replaces lines owned by an earlier hunk. Each edge records how many lines it removed from that writer. A replacement that removes lines from multiple writers has multiple outgoing edges. A file can contain edits from several sessions without any of them crossing. A hunk that only removes lines owns no range, so a `Gap` keeps its spot: `after` counts the current lines above it, and the hunk keeps the removed text in `removed_text` when every removed line was observed.

The heart of `trace` is a single Cypher statement. It starts at the current writer (zero hops) and follows `REPLACED` to every predecessor. For each hunk with an edit, it follows `NEXT` directly from that edit to collect the earlier and later files in session order. Run with `--show-cypher` to see the full statement and the conversation fields it returns.

```cypher
MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)
WHERE r.start <= $line AND r.end >= $line
MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
OPTIONAL MATCH (s:Session)-[:HAS_EDIT]->(e:Edit)-[:HAS_HUNK]->(h)
CALL (e) {
  MATCH previous=(e)<-[:NEXT*1..]-(prev:Edit)
  WITH prev, length(previous) AS hops ORDER BY prev.ts, prev.seq, hops DESC
  RETURN collect(DISTINCT prev.path) AS previous_files
}
CALL (e) {
  MATCH (e)-[:NEXT*1..]->(following:Edit)
  WITH following ORDER BY following.ts, following.seq
  RETURN collect(DISTINCT following.path) AS next_files
}
RETURN length(chain) AS depth, [n IN nodes(chain) | n.id] AS chain, previous_files, next_files
ORDER BY depth, chain
```

Shared ancestors appear once for each distinct path to them. The stored line counts describe the hunk as a whole. A many-line rewrite can have several plausible predecessors for one particular line. The traversal preserves all of them.

## Open the store

Paper Trail uses Relate's **single-file** mode. One closed `.rl` file identifies the complete graph and can be inspected by a judge using the same PyPI package. Both parsing and queries run offline after installation. The only runtime dependency is `relate-db==1.1.0`.

```sh
uv run python - <<'PY'
import relate
with relate.connect("demo/project/.papertrail/graph.rl", read_only=True) as db:
    print(db.execute("""
        MATCH (e:Edit)-[:HAS_HUNK]->(h)-[r:REPLACED]->(old:Hunk)
        RETURN e.tool AS tool, h.kind AS writer, old.kind AS predecessor, r.lines AS lines
    """).data())
PY
```

The application API uses the same queries as the CLI. Each method returns plain Python dictionaries and lists, including the executed Cypher.

```python
from papertrail.queries import Queries

with Queries("demo/project/.papertrail/graph.rl") as graph:
    result = graph.trace("demo/project/relay/config.py", 6)
    print(result["rows"][0]["prompt"])
```

## Ingest details

By default ingest scans both `~/.claude/projects` and `~/.codex/sessions`. A missing directory appears in the summary. Use `--source codex --logs DIR` or `--source claude-code --logs DIR` to scan one source. The original `--logs DIR` form selects Claude Code. Set `--claude-logs DIR --codex-logs DIR` to specify both roots. Codex compresses older rollouts to `rollout-*.jsonl.zst`. Ingest reads those too, with the first decoder it finds: `compression.zstd` (Python 3.14+), the `zstandard` package, or the `zstd` command. Without any of them the compressed rollouts are skipped and counted as `compressed_rollout_without_zstd`. Decompressed history can be many gigabytes, so the first ingest of a long Codex history takes minutes rather than seconds. Without `--project`, ingest uses the current directory. A project selects **sessions that edited a file under that directory**. Their other edits stay in the graph so that session navigation works. Ancestors needed for delegation retain their prompts. An explicit move into the project also brings in earlier sessions that edited the old path. Coverage for this store includes all retained files. Use `--all` to include every session in the log root. Source diagnostic counts cover the scanned logs before project selection.

The default store is `.papertrail/graph.rl` in the selected project. `./start.sh PROJECT`, the reader's **Build History** button and `ingest --project` all write to that file, so a graph built by one is found by the others and by the agent skill. Ingest also writes a `.gitignore` containing `*` into a new `.papertrail` folder. Query commands look in the current directory's `.papertrail` unless given `--store`. Ingest builds a private single-file graph beside the selected store, commits in batches and publishes the completed file atomically. Check the store path when switching projects. Queries open it read-only. Run ingest again after changing files or receiving new logs.

A graph normally records absolute paths. `--portable DIR` writes the path of `DIR` as a placeholder and records where the store sits inside it. Queries work out the folder's current location from the store's own, so the demo answers the same on any machine and the committed file names no home directory. `scripts/make_demo.py` writes the whole demo, graph included. Tests check that running it reproduces the committed project and transcripts, and that the committed graph gives the same answers after the folder is copied somewhere else.

## Query output

All five queries accept `--json`, `--limit N` and `--show-cypher`. A successful JSON response has `rows` and `truncated`. Adding `--show-cypher` includes the parameterized statements in `cypher`. Limits count result rows and request one extra row to detect truncation. Omitting the option requests the full result. A command error writes an actionable message to stderr and exits nonzero. With `--json` its shape is `{"error":"..."}`. `papertrail --help` lists the commands with examples, and each command has its own `--help`.

Ranges are line numbers in the file as ingest read it. Ingest stores a SHA-256 of each file, and `blame`, `trace` and `crossings FILE` compare it with the file on disk. When they differ, the JSON response has `"stale": true`, and the readable form prints a warning on stderr and leaves the code out. Run ingest again to clear it.

```sh
uv run papertrail trace demo/project/relay/config.py:6 --store demo/project/.papertrail/graph.rl --limit 2 --json --show-cypher
```

## Reader internals

The definition search reads each line on its own (`def name`, `function name`, `name = …` and the like) instead of parsing the language, so it follows names, not scopes: two unrelated functions with the same name both appear. The outline uses declaration patterns and matches braces in JavaScript, TypeScript, Go and Java, and uses indentation for Python.

Code is colored by a small built-in tokenizer (`static/syntax.js`, no dependencies) that knows Python, JavaScript and TypeScript, JSON, CSS, HTML, Markdown, shell, YAML, TOML, SQL and Cypher, and the common C-family languages. The colors are kept quieter than the session tracks. Other files show as plain text.

The reader uses the same read-only query layer as the CLI. It listens on loopback only and refuses requests that name another host. It serves file text only for files in the graph or inside the open folder, and reopens the store when ingest rebuilds it. Showing the folder dialog, opening a folder and building its history are POST requests that need a custom header, so another site can't trigger them. Use `--port` to change the port and `--no-browser` to skip opening a tab.

The reader loads its page files when the server starts, so restart `papertrail ui` to pick up changes. If a tab is still open after a restart, it offers a **Reload** button the next time it contacts the server.

## What the evidence can establish

Every line gets one of two kinds of owner. A **proven** owner comes from a tool result that shows the change itself. Agents often change files with scripts, such as `python3 - <<EOF`, `sed -i` or a Codex `exec` script, and the logs record that the command ran but not what it wrote. For those lines Paper Trail still names the most likely session and marks the line **inferred**, because a likely owner is more useful than none. The two are kept apart everywhere:

- The reader draws an inferred line lighter, labelled "Likely <agent>".
- `trace` and `blame` return the inference's `basis` and `was`.
- `stats` reports coverage both with and without inferred lines.

This repository was written entirely by Claude Code and Codex. 40% of its current lines have a proven owner, and almost all of the rest are inferred. 84% of the inferred lines come from a command that named the file.

- **Agent** lines come from a successful supported tool result. A verified structured patch supplies positions. With before/after text alone, the replay trims equal ends and compares the remaining lines. Every contiguous changed block gets a hunk. Unchanged lines keep their owner across whole-file writes.
- **Inferred** lines are a best guess at who wrote a previously unowned line. The `command` basis uses the latest eligible successful command that named the file in the relevant window. The `nearby` basis uses the nearest preceding proven edit to the file, or the earliest following edit if there is no preceding one. `was` records the original kind, and the linked prompt provides context for the guess. Stats report both proven coverage and coverage including inferred lines.
- The kinds below are what replay assigns before inference. Inference takes over external, drift and unknown hunks whenever it finds a candidate, and keeps the original kind in `was`. Baseline lines stay baseline.
- **Baseline** lines existed in the first available preimage. **External** hunks reconcile a mismatch with a later preimage. Their timestamp is an estimate midway between observations. Baseline timestamps identify first observation.
- **Drift** describes a difference between the final replayed text and the file read at ingest. Its exact time is unknown. Matched lines keep their owners at their new disk positions. Missing files retain their history. Unreadable files have no current ranges.
- **Unknown** lines have insufficient evidence for an owner, and inference found no candidate session either. A numbered patch can start a file history at any line. Read windows reveal additional positions. Disk comparison preserves uniquely matched known runs and leaves gaps of arbitrary length. A file with an open-ended history or an outstanding unplaced change has status `unresolved`. Unknown lines count in the coverage denominator.
- For a confirmed text-only tool result, the Claude source uses the original Write contents or Edit replacement strings. An earliest full overwrite with an unknown preimage marks its hunk `preimage_unknown`. Replace-all applies to verified occurrences. A later uniquely matching observation can locate a previously unplaced fragment. Read-only observations never acquire AI authorship. Notebook history describes physical JSON lines. Bash output alone does not establish authorship. Even a read-only command can report working-tree hunks in `bashEditDiff.files[].hunks`. [Published tool output showing this case](https://gist.github.com/konard/df59340f303d4aa005843def673b8079) A file in a successful command's `bashEditDiff` counts as that session's edit only when the command names the file. It must not run another agent CLI (`codex`, `claude`, `kimi`, `aider` and similar) or a git command that rewrites the working tree (`checkout`, `merge`, `stash`, `reset` and similar). Every hunk's line counts must match its rows. The rest prove no edit and are counted (`bash_diff_other_writer`, `bash_diff_unnamed_file`, `bash_diff_incomplete_hunks`). A command that is not another writer can still supply a write hint for inference. The hunks replay like an Edit's patch, so a context line that disagrees with the known text marks the lines external rather than the command's.
- Paths resolve against the recorded working directory and follow symlinks as they exist at ingest. A recorded Codex move carries line owners into the destination, including any edits in that same patch. A deletion ends the file's lifetime. A later creation starts a fresh history. Unrecorded moves, multiple checkouts and changes to symlink targets need independent evidence. Line comparisons normalize CRLF to LF and retain the final-newline distinction. Repeated identical lines can yield several equally valid alignments.
- Codex file-change completion events supply per-file diffs. A standalone or literally recoverable `apply_patch` can also use its paired success receipt. A multi-file patch produces one edit per operation, sharing a call ID. A completed nested file change survives a later failure in the wrapper. Claude Bash and Codex shell calls can also prove literal whole-file writes. Paper Trail recognizes cat and tee heredocs, including append mode, when the path, contents and successful execution can be established. Appends with an unknown file ending need a later witness. Successful commands without a proven edit can supply a write hint. Paper Trail uses the command text in memory and keeps the tool and session context in the graph. Paper Trail never executes a logged command or parses arbitrary terminal output as a file snapshot.
- For numbered diffs, context reveals lines at their recorded positions. Unnumbered patches require a unique matching context or a uniquely reversible later observation. Gaps, ambiguous matches and unavailable disk files limit coverage. A pure move changes the path and preserves line ownership. Session history includes the move.
- Codex uses the issuing turn's model, effort and working directory. A completed user-message event takes precedence over a response message for that turn. Without a recorded title the first human instruction supplies an 80-character preview. Child rollouts with an explicit parent thread ID link to that thread. Their own thread ID is used by `codex resume`. An unrecorded child instruction stays unknown.
- Real edit times come from successful result records. Equal timestamps use record sequence and edit ID as a deterministic tie-break. Human instructions follow transcript ancestry where available. System reminders, tool results, command wrappers, compaction summaries, delegated tasks and non-human origins are filtered. Older unmarked injections may still resemble user text. A missing prompt or model stays unknown.
- Child transcripts in `<parent-id>/subagents/` retain their own log paths. When the metadata identifies the launching tool call, edits inherit its user instruction. A child resumes through the main Claude conversation. Session IDs and logs can outlive Claude's ability to resume them.
- One process writes a store at a time. The working graph and replay inputs must fit in memory. A heavily branching history can produce many distinct paths. `--limit` controls returned rows. Query work can still grow with the number of paths. [Relate limitations](https://relatedb.ai/docs/reference/limitations.md)

## Verification and extension

```sh
uv run pytest -q
uv run python scripts/probe.py
uv run python scripts/audit.py --logs tests/fixtures/logs
uv run python scripts/codex_audit.py --receipts --logs tests/fixtures/codex-sessions
uv run python scripts/combined_audit.py --project .
```

The probe exercises the installed engine's paths, parameters, indexes, constraints, transactions, read-only mode and batch writes. The audit prints aggregate parsing and replay results. The Codex audit counts call routes, committed file operations and excluded command signatures. The combined audit compares a Claude-only replay and a combined replay against the same parsed Claude sessions. Its project section counts only files under the requested directory. See the source guide to add another agent. [Adding a source](adding-a-source.md)
