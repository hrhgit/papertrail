# Paper Trail

Point at a line of code to find the agent that wrote it, the instruction it was following, and the conversation you can resume. Paper Trail reconstructs this history from existing Claude Code and Codex CLI transcripts. It runs locally with an embedded Relate database. It needs no hook, server, model or API key.

## Who it is for

A developer who maintains code that coding agents wrote over many sessions, and who is about to change a line without knowing why it is the way it is. `git blame` names a commit and the person who made it. The request that produced the line sits in an agent transcript on the same disk, and nothing links the line to it.

This is now an everyday situation, and the cost is time spent working out what code is for:

- In Stack Overflow's 2026 Developer Survey (more than 30,000 respondents), 73% of the developers who use AI coding assistants or agents use them daily. Claude Code (66%) and GitHub Copilot (59%) are the most used. [Results](https://stackoverflow.blog/2026/10/06/the-results-of-the-2026-developer-survey-are-here/)
- In the 2025 survey, 66% of developers named "AI solutions that are almost right, but not quite" as a frustration, and 45% said debugging AI-generated code takes more time. [AI section](https://survey.stackoverflow.co/2025/ai)
- Reading code to understand it was already most of the job before agents. A field study of 78 professional developers over 3,148 working hours found that about 58% of their time went to program comprehension. [Xia et al., IEEE TSE 2018](https://xin-xia.github.io/publication/TSE17.pdf)

Paper Trail shortens one part of that work: finding out what a person asked for when a line was written, and what it replaced. It does not measure how much time that saves.

Recording this as code is written is being standardized. Cursor published [Agent Trace](https://github.com/cursor/agent-trace), a draft format for attributing code ranges to agents and conversations. That covers edits made after an agent adopts it. Paper Trail works backwards from the transcripts Claude Code and Codex have already left on disk, so it also covers code written last month.

The second user is the agent itself. An agent that is asked to change a line can look up the earlier instruction first and say so when the new request contradicts it. [Let an agent look before it edits](#let-an-agent-look-before-it-edits)

## How it uses Relate

- **Mode:** single file (`relate-db==1.1.0`, embedded in the Python process).
- **Why this mode:** one closed `.rl` file is the whole history of a project, so it can sit in `.papertrail/` beside the code and be copied or committed like any other file. The CLI, the reader and the agent skill all open the same file read-only. Ingest builds a private file next to it and swaps it in when it is complete, which fits Relate's one-writer-per-store rule. A real graph holds private prompts, and one file is easy to keep out of git. In-memory would mean replaying every transcript on each query. A directory store would spread those prompts over many JSON checkpoints.
- **What the graph models:** sessions, the instructions a person gave in them, the edits that followed, and the hunks of lines each edit wrote. The relationship that matters is `REPLACED`: a hunk points at the earlier hunks whose lines it overwrote, with the number of lines taken from each. Following it with `REPLACED*0..` gives a line's whole history across agents and sessions, however many rewrites there were, and where two sessions' chains meet is a crossing. `SPAWNED` leads a subagent's edits back to the instruction given to its parent, and `NEXT` orders each session's prompts and edits. [Full schema](#how-the-graph-answers-the-question)

**Where the data lives:** [`demo/project/.papertrail/graph.rl`](demo/project/.papertrail/graph.rl) is the committed graph of the demo below (8 sessions, 23 edits, 25 hunks, 239 relationships). For your own project, ingest writes `.papertrail/graph.rl` in that project and keeps it out of git. [Open the store](#open-the-store) shows how to query it with the `relate` package directly.

## Why is the retry limit 3?

The repository includes a made-up project and the agent sessions that built it. `demo/project` is a small worker that delivers webhooks. `demo/logs` holds the transcripts of seven sessions spread over three weeks: four in Claude Code, one of which used a subagent, and three in Codex. Nothing in them is real. With Python 3.11+ and uv installed:

```sh
./start.sh demo
```

This opens the reader on `relay/config.py` line 6, `MAX_ATTEMPTS = 3`. The graph is already in the repository at `demo/project/.papertrail/graph.rl`, so nothing is built first.

![The reader on relay/config.py line 6. The left column lists the lines several sessions changed, the middle shows the file with one coloured bar per writing session, and the right shows the instruction behind line 6 and the two sessions it replaced.](docs/images/reader.png)

The code does not say why the limit is 3. The line's history does:

1. The scaffold session (Claude Code) set it to 5.
2. A Codex session raised it to 8 after a customer's endpoint was down for 20 minutes and 312 events were parked.
3. A Claude Code session cut it to 3 two days later. Incident 2291: an endpoint timed out after it had already processed the event, so each of the 8 attempts created another charge. The instruction was to cap attempts at 3 "until receivers can drop repeats".

**Follow session** on the last one shows what it did next: it added an `Idempotency-Key` header in `relay/delivery.py`, a test, and a note for receivers in the README. Anyone who wants 8 again now knows what to check first.

The same history from the command line:

```sh
uv sync --locked
cd demo/project
uv run papertrail blame relay/config.py
uv run papertrail trace relay/config.py:6
uv run papertrail session b27e5f90
uv run papertrail crossings
uv run papertrail stats
```

These read the committed graph, the same file `./start.sh demo` opens. [Complete example output](docs/example-session.md)

To build it again from the transcripts, run this in `demo/project`:

```sh
uv run papertrail ingest --claude-logs ../logs/claude --codex-logs ../logs/codex --project . --portable ..
```

A graph normally records absolute paths. `--portable DIR` writes the path of `DIR` as a placeholder and records where the store sits inside it. Queries work out the folder's current location from the store's own, so the demo answers the same on any machine and the committed file names no home directory.

Other things to look at in the demo:

- `relay/config.py` lines 9 and 10 were changed by hand between two sessions. Paper Trail marks them as inferred from a nearby edit, with `was: external`. The demo shows how a best guess can point to the wrong writer.
- `backoff_delay` in `relay/delivery.py` was written by Claude Code, rewritten by Codex and rewritten again by Claude Code.
- `relay/delivery.py` has a red triangle where a Codex session deleted the legacy `X-Signature` header.
- `tests/test_delivery.py` was written by a subagent. Its lines lead back to the instruction the person gave the parent session.
- The last line of `README.md` was added on disk after the final session.

`scripts/make_demo.py` writes the whole demo, graph included. Tests check that running it reproduces the committed project and transcripts, and that the committed graph gives the answers above after the folder is copied somewhere else.

## Read it in the browser

```sh
uv run papertrail ui --project demo/project
```

This opens a local reader at `http://127.0.0.1:8765/`. The sidebar opens on the Files tab, with the code pane showing the busiest crossing: a spot several sessions rewrote. Each session is a colored track beside the line numbers. Where tracks run side by side, those sessions all rewrote that spot; the newest is outermost. Click a line to see its route back through every change, with the instruction behind each one. **Follow session** dims every line that session did not touch and lists its instructions and edits in order. **Cypher** reveals the statements behind the current view. Labels are short; hover one for its full meaning.

The code shows the file as it is now, so deleted lines leave no line of their own. A small red triangle between two lines marks a spot where lines were taken out. Click it to unfold the removed text in place; the panel names who deleted it, shows that step of the conversation, and lists the sessions that wrote the lines in the first place. **Deleted** beside the file name unfolds every spot in the file at once. Rewriting the lines around a spot later removes its mark, because the deletion is then part of that rewrite's history.

The reader works like a small code editor. **File > Open Folder…** (⌘O) shows the system's folder dialog (macOS, Windows, or Linux with zenity or kdialog). **Open Recent…** (Ctrl+R) lists recent folders and folders where agents worked, or takes a typed path. The dialog opens above other windows, and the page shows a reminder until you choose or cancel. A folder with no graph yet still lists its files, which open as plain text, and gets a **Build History** button, which does the same as `papertrail ingest --project FOLDER`. **Open File…** (⌘P) searches every file in the folder, and `name:42` jumps to line 42. **Go to Line…** (Ctrl+G) moves within the open file. The Files tab is a folder tree; files no agent edited are greyed, and the filter button beside the folder name hides them. Opened files stay as tabs above the code. Run `papertrail ui` inside a project to open it directly, or pass `--project`.

**Search** (⇧⌘F, or the magnifier tab) looks for the same words in three places. Current lines come back grouped by file, each marked with the session that wrote it. Instructions you gave open their conversation at that message. Lines since deleted open the file at the spot they were removed from. A file changed after the build still shows its hits, without writers. **Find in File…** (⌘F) highlights matches in the open file; Enter and Shift+Enter step through them and select each line, so the panel shows who wrote it. While you follow a session, the arrows beside its name step through the places it wrote in the open file.

**⌘-click a name** in the code (Ctrl-click elsewhere) to list every place the folder uses it, as a whole word, each with who wrote the line. Clicked on a use, it also jumps to the definition when the folder has exactly one. The definition is found by reading each line on its own (`def name`, `function name`, `name = …` and the like), not by parsing the language, so it follows names, not scopes: two unrelated functions with the same name both appear.

**Back and Forward** (the arrows beside File, the browser's own buttons, or Ctrl+- and Ctrl+Shift+-) restore the file position and its history panel. Moving to another file or ten or more lines away adds a stop. Nearer moves and changes to the panel update the current stop.

The **Outline** tab (the list icon) shows the open file's functions and classes, or a Markdown file's headings. Each has a dot for every session that owns lines in it now, most lines first, and a faded dot for a session whose lines there were rewritten since. While you follow a session, the items it never touched are dimmed. The outline uses declaration patterns and matches braces in JavaScript, TypeScript, Go and Java. It uses indentation for Python. Click an item and the history panel shows the whole function's history, with one stop per instruction that shaped its lines, newest first, and the number of lines each still owns there.

The **Sessions** tab shows, for each session, how much of what it wrote is still in the files. Open a session and expand the line count under its name to see which files hold the remaining lines, which changes replaced or deleted lines, and how many lines have no recorded outcome. The last group shows when the original file was missing or unreadable at build time. The copy button in a line's, a deletion's or a function's history copies the place, who wrote it and when, the instruction, and the command that resumes the session as Markdown.

With the history panel last clicked, **⌘F** searches the conversation instead of the code: messages, and each step's command, diff and output. Stepping to a match opens the group and the step that hold it. The edges of the sidebar and the history panel drag to resize; double-click an edge to reset it.

Code is colored by a small built-in tokenizer (`static/syntax.js`, no dependencies) that knows Python, JavaScript and TypeScript, JSON, CSS, HTML, Markdown, shell, YAML, TOML, SQL and Cypher, and the common C-family languages. The colors are kept quieter than the session tracks. Other files show as plain text.

The reader uses the same read-only query layer as the CLI. It listens on loopback only and refuses requests that name another host. It serves file text only for files in the graph or inside the open folder, and reopens the store when ingest rebuilds it. Showing the folder dialog, opening a folder and building its history are POST requests that need a custom header, so another site can't trigger them. Use `--port` to change the port and `--no-browser` to skip opening a tab.

The reader loads its page files when the server starts, so restart `papertrail ui` to pick up changes. If a tab is still open after a restart, it offers a **Reload** button the next time it contacts the server.

## Let an agent look before it edits

```sh
uv run papertrail skill --project /absolute/path/to/project
```

This writes a skill to `.claude/skills/papertrail/SKILL.md` in that project. The skill tells Claude Code to run `blame` and `trace` before it changes code it did not write, and to treat the instruction behind a line as a constraint the human may still hold. Asked to raise `MAX_ATTEMPTS` back to 8 in the demo project, Claude Code with the skill made no edit. It reported the three instructions behind the line, pointed out that the limit was cut after incident 2291 and that nothing shows the receiver drops repeats yet, asked which way to go, and offered the command that reopens the incident conversation.

| Option | Effect |
| --- | --- |
| `--agent codex` | Installs to `~/.codex/skills/papertrail/`. Codex skills always go to the home folder. `--agent all` installs both. |
| `--global` | Installs the Claude Code skill to `~/.claude/skills/papertrail/` for every project. |
| `--store PATH` | Adds `--store PATH` to every command in the skill. Without it the agent uses `.papertrail/graph.rl` in the project. |
| `--print` | Prints the skill and writes nothing. |

The skill contains the command that starts this installation: `papertrail` when it is on `PATH`, otherwise `uv run --project` with this checkout's path. Run the command again after moving the checkout. The agent only reads the graph. The skill tells it to keep past prompts out of commits and comments.

## Index your own project

Run these commands from the Paper Trail checkout. Set `P` to the absolute path of the project you want to investigate.

```sh
P=/absolute/path/to/project
uv run papertrail ingest --project "$P"
uv run papertrail blame "$P/src/main.py" -L 10,25 --store "$P/.papertrail/graph.rl"
uv run papertrail trace "$P/src/main.py:17" --store "$P/.papertrail/graph.rl" --show-cypher
```

By default ingest scans both `~/.claude/projects` and `~/.codex/sessions`. A missing directory appears in the summary. Use `--source codex --logs DIR` or `--source claude-code --logs DIR` to scan one source. The original `--logs DIR` form selects Claude Code. Set `--claude-logs DIR --codex-logs DIR` to specify both roots. Codex compresses older rollouts to `rollout-*.jsonl.zst`. Ingest reads those too, with the first decoder it finds: `compression.zstd` (Python 3.14+), the `zstandard` package, or the `zstd` command. Without any of them the compressed rollouts are skipped and counted as `compressed_rollout_without_zstd`. Decompressed history can be many gigabytes, so the first ingest of a long Codex history takes minutes rather than seconds. Without `--project`, ingest uses the current directory. A project selects **sessions that edited a file under that directory**. Their other edits stay in the graph so that session navigation works. Ancestors needed for delegation retain their prompts. An explicit move into the project also brings in earlier sessions that edited the old path. Coverage for this store includes all retained files. Use `--all` to include every session in the log root. Source diagnostic counts cover the scanned logs before project selection.

The default store is `.papertrail/graph.rl` in the selected project. `./start.sh PROJECT`, the reader's **Build History** button and `ingest --project` all write to that file, so a graph built by one is found by the others and by the agent skill. Ingest also writes a `.gitignore` containing `*` into a new `.papertrail` folder. Query commands look in the current directory's `.papertrail` unless given `--store`. Ingest builds a private single-file graph beside the selected store, commits in batches and publishes the completed file atomically. Check the store path when switching projects. Queries open it read-only. Run ingest again after changing files or receiving new logs.

**Privacy.** The graph contains private prompts, file paths, log locations and code excerpts. Keep a real graph on your own machine. The default directory and graph files are gitignored here, except the demo's graph, which holds only the made-up sessions. The `.gitignore` that ingest writes keeps the folder out of another repository's history. If you choose a store outside a `.papertrail` folder, keep it private yourself. Ingest prints only counts and elapsed time. Queries deliberately reveal details from the indexed conversations.

## Query options

| Command | Result |
| --- | --- |
| `blame FILE [-L START,END]` | Inclusive, one-based disk ranges. Each row includes the writer, model and reasoning effort, time, title, session and prompt preview. The readable form prints the code under each writer. |
| `trace FILE:LINE` | The current writer at depth zero and each replacement path. Agent entries include the full prompt, resume command, working directory, log and files edited earlier and later in that session. |
| `session ID` | Human instructions and completed edits in time order. An unambiguous prefix works. A delegated edit can lead back to a parent's instruction. |
| `crossings [FILE]` | Current ranges or historical endpoints whose replacement history contains at least two sessions. A historical endpoint has null line numbers. The JSON fields `agents` and `agent_count` identify crossings between agents. |
| `stats` | Node counts by type, relationships, disk comparison results and line counts by origin. The `by_agent` table counts sessions, edits, unplaced edits and currently attributed lines. |

All five queries accept `--json`, `--limit N` and `--show-cypher`. A successful JSON response has `rows` and `truncated`. Adding `--show-cypher` includes the parameterized statements in `cypher`. Limits count result rows and request one extra row to detect truncation. Omitting the option requests the full result. A command error writes an actionable message to stderr and exits nonzero. With `--json` its shape is `{"error":"..."}`. `papertrail --help` lists the commands with examples, and each command has its own `--help`.

Ranges are line numbers in the file as ingest read it. Ingest stores a SHA-256 of each file, and `blame`, `trace` and `crossings FILE` compare it with the file on disk. When they differ, the JSON response has `"stale": true`, and the readable form prints a warning on stderr and leaves the code out. Run ingest again to clear it.

```sh
uv run papertrail trace demo/project/relay/config.py:6 --store demo/project/.papertrail/graph.rl --limit 2 --json --show-cypher
```

## How the graph answers the question

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

### Open the store

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

## What the evidence can establish

- **Agent** lines come from a successful supported tool result. A verified structured patch supplies positions. With before/after text alone, the replay trims equal ends and compares the remaining lines. Every contiguous changed block gets a hunk. Unchanged lines keep their owner across whole-file writes.
- **Inferred** lines are a best guess at who wrote a previously unowned line. The `command` basis uses the latest eligible successful command that named the file in the relevant window. The `nearby` basis uses the nearest preceding proven edit to the file, or the earliest following edit if there is no preceding one. `was` records the original kind, and the linked prompt provides context for the guess. Stats report both proven coverage and coverage including inferred lines.
- **Baseline** lines existed in the first available preimage. **External** hunks reconcile a mismatch with a later preimage. Their timestamp is an estimate midway between observations. Baseline timestamps identify first observation.
- **Drift** describes a difference between the final replayed text and the file read at ingest. Its exact time is unknown. Matched lines keep their owners at their new disk positions. Missing files retain their history. Unreadable files have no current ranges.
- **Unknown** lines have insufficient evidence for an owner. A numbered patch can start a file history at any line. Read windows reveal additional positions. Disk comparison preserves uniquely matched known runs and leaves gaps of arbitrary length. A file with an open-ended history or an outstanding unplaced change has status `unresolved`. Unknown lines count in the coverage denominator.
- For a confirmed text-only tool result, the Claude source uses the original Write contents or Edit replacement strings. An earliest full overwrite with an unknown preimage marks its hunk `preimage_unknown`. Replace-all applies to verified occurrences. A later uniquely matching observation can locate a previously unplaced fragment. Read-only observations never acquire AI authorship. Notebook history describes physical JSON lines. Bash output alone does not establish authorship. Even a read-only command can report working-tree hunks in `bashEditDiff.files[].hunks`. [Published tool output showing this case](https://gist.github.com/konard/df59340f303d4aa005843def673b8079) A file in a successful command's `bashEditDiff` counts as that session's edit only when the command names the file. It must not run another agent CLI (`codex`, `claude`, `kimi`, `aider` and similar) or a git command that rewrites the working tree (`checkout`, `merge`, `stash`, `reset` and similar). Every hunk's line counts must match its rows. The rest are diagnostic counts (`bash_diff_other_writer`, `bash_diff_unnamed_file`, `bash_diff_incomplete_hunks`). The hunks replay like an Edit's patch, so a context line that disagrees with the known text marks the lines external rather than the command's.
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

The probe exercises the installed engine's paths, parameters, indexes, constraints, transactions, read-only mode and batch writes. The audit prints aggregate parsing and replay results. The Codex audit counts call routes, committed file operations and excluded command signatures. The combined audit compares a Claude-only replay and a combined replay against the same parsed Claude sessions. Its project section counts only files under the requested directory. See the source guide to add another agent. [Adding a source](docs/adding-a-source.md)
