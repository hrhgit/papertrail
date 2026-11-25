# Paper Trail

Point at a line of code to find the agent that wrote it, the instruction it was following, and the conversation you can resume. Paper Trail reconstructs this history from existing Claude Code and Codex CLI transcripts. It runs locally with an embedded Relate database. It needs no hook, server, model or API key.

## Trace a line across two agents

From the repository root, with Python 3.11+ and uv installed:

```sh
uv sync --locked
F=tests/fixtures/project/greeting.py
S=.papertrail/mixed-demo.rl
uv run papertrail ingest --claude-logs tests/fixtures/logs --codex-logs tests/fixtures/codex-sessions --project tests/fixtures/project --store "$S"
uv run papertrail blame "$F" --store "$S"
uv run papertrail trace "$F:2" --store "$S"
uv run papertrail session birch --store "$S"
uv run papertrail crossings "$F" --store "$S"
uv run papertrail stats --store "$S"
```

Line 2 contains the morning greeting. Cedar (Claude Code) wrote it over Maple's sunrise greeting (Codex). Maple had replaced Birch's welcome (Claude Code). The trace continues into Amber's two earlier drafts. Following Birch's session forward reveals a change to a different file. Line 1 was inserted on disk later. Line 3 appeared between two recorded writes. All fixture conversations and files are fictional. The resume commands in this example contain fictional IDs. [Complete example output](docs/example-session.md)

## Read it in the browser

```sh
uv run papertrail ui --store .papertrail/mixed-demo.rl
```

This opens a local reader at `http://127.0.0.1:8765/`. It opens on the busiest crossing: a spot several sessions rewrote. Each session is a colored track beside the line numbers. Where tracks run side by side, those sessions all rewrote that spot; the newest is outermost. Click a line to see its route back through every change, with the instruction behind each one. **Follow session** dims every line that session did not touch and lists its instructions and edits in order. **Cypher** reveals the statements behind the current view. Labels are short; hover one for its full meaning.

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

## Index your own project

Run these commands from the Paper Trail checkout. Set `P` to the absolute path of the project you want to investigate.

```sh
P=/absolute/path/to/project
uv run papertrail ingest --project "$P"
uv run papertrail blame "$P/src/main.py" -L 10,25 --store "$P/.papertrail/graph.rl"
uv run papertrail trace "$P/src/main.py:17" --store "$P/.papertrail/graph.rl" --show-cypher
```

By default ingest scans both `~/.claude/projects` and `~/.codex/sessions`. A missing directory appears in the summary. Use `--source codex --logs DIR` or `--source claude-code --logs DIR` to scan one source. The original `--logs DIR` form selects Claude Code. Set `--claude-logs DIR --codex-logs DIR` to specify both roots. Without `--project`, ingest uses the current directory. A project selects **sessions that edited a file under that directory**. Their other edits stay in the graph so that session navigation works. Ancestors needed for delegation retain their prompts. An explicit move into the project also brings in earlier sessions that edited the old path. Coverage for this store includes all retained files. Use `--all` to include every session in the log root. Source diagnostic counts cover the scanned logs before project selection.

The default store is `.papertrail/graph.rl` in the selected project. Query commands look in the current directory's `.papertrail` unless given `--store`. Ingest builds a private single-file graph beside the selected store, commits in batches and publishes the completed file atomically. Check the store path when switching projects. Queries open it read-only. Run ingest again after changing files or receiving new logs.

**Privacy.** The graph contains private prompts, file paths, log locations and code excerpts. Keep a real graph on your own machine. The default directory and graph files are gitignored here. If you run against another repository, add `.papertrail/` to its ignore file or choose a private store elsewhere. Ingest prints only counts and elapsed time. Queries deliberately reveal details from the indexed conversations.

## Query options

| Command | Result |
| --- | --- |
| `blame FILE [-L START,END]` | Inclusive, one-based disk ranges. Each row includes the writer, model and reasoning effort, time, title, session and prompt preview. |
| `trace FILE:LINE` | The current writer at depth zero and each replacement path. Agent entries include the full prompt, resume command, working directory, log and files edited earlier and later in that session. |
| `session ID` | Human instructions and completed edits in time order. An unambiguous prefix works. A delegated edit can lead back to a parent's instruction. |
| `crossings [FILE]` | Current ranges or historical endpoints whose replacement history contains at least two sessions. A historical endpoint has null line numbers. The JSON fields `agents` and `agent_count` identify crossings between agents. |
| `stats` | Node counts by type, relationships, disk comparison results and line counts by origin. The `by_agent` table counts sessions, edits, unplaced edits and currently attributed lines. |

All five queries accept `--json`, `--limit N` and `--show-cypher`. A successful JSON response has `rows` and `truncated`. Adding `--show-cypher` includes the parameterized statements in `cypher`. Limits count result rows and request one extra row to detect truncation. Omitting the option requests the full result. A command error writes an actionable message to stderr and exits nonzero. With `--json` its shape is `{"error":"..."}`.

```sh
uv run papertrail trace "$F:2" --store "$S" --limit 2 --json --show-cypher
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
with relate.connect(".papertrail/mixed-demo.rl", read_only=True) as db:
    print(db.execute("""
        MATCH (e:Edit)-[:HAS_HUNK]->(h)-[r:REPLACED]->(old:Hunk)
        RETURN e.tool AS tool, h.kind AS writer, old.kind AS predecessor, r.lines AS lines
    """).data())
PY
```

The application API uses the same queries as the CLI. Each method returns plain Python dictionaries and lists, including the executed Cypher.

```python
from papertrail.queries import Queries

with Queries(".papertrail/mixed-demo.rl") as graph:
    result = graph.trace("tests/fixtures/project/greeting.py", 2)
    print(result["rows"][0]["prompt"])
```

## What the evidence can establish

- **Agent** lines come from a successful supported tool result. A verified structured patch supplies positions. With before/after text alone, the replay trims equal ends and compares the remaining lines. Every contiguous changed block gets a hunk. Unchanged lines keep their owner across whole-file writes.
- **Baseline** lines existed in the first available preimage. **External** hunks reconcile a mismatch with a later preimage. Their timestamp is an estimate midway between observations. Baseline timestamps identify first observation.
- **Drift** describes a difference between the final replayed text and the file read at ingest. Its exact time is unknown. Matched lines keep their owners at their new disk positions. Missing files retain their history. Unreadable files have no current ranges.
- **Unknown** lines have insufficient evidence for an owner. A numbered patch can start a file history at any line. Read windows reveal additional positions. Disk comparison preserves uniquely matched known runs and leaves gaps of arbitrary length. A file with an open-ended history or an outstanding unplaced change has status `unresolved`. Unknown lines count in the coverage denominator.
- For a confirmed text-only tool result, the Claude source uses the original Write contents or Edit replacement strings. An earliest full overwrite with an unknown preimage marks its hunk `preimage_unknown`. Replace-all applies to verified occurrences. A later uniquely matching observation can locate a previously unplaced fragment. Read-only observations never acquire AI authorship. Notebook history describes physical JSON lines. Bash output alone does not establish authorship. Even a read-only command can report working-tree hunks in `bashEditDiff.files[].hunks`. [Published tool output showing this case](https://gist.github.com/konard/df59340f303d4aa005843def673b8079)
- Paths resolve against the recorded working directory and follow symlinks as they exist at ingest. A recorded Codex move carries line owners into the destination, including any edits in that same patch. A deletion ends the file's lifetime. A later creation starts a fresh history. Unrecorded moves, multiple checkouts and changes to symlink targets need independent evidence. Line comparisons normalize CRLF to LF and retain the final-newline distinction. Repeated identical lines can yield several equally valid alignments.
- Codex file-change completion events supply per-file diffs. A standalone or literally recoverable `apply_patch` can also use its paired success receipt. A multi-file patch produces one edit per operation, sharing a call ID. A completed nested file change survives a later failure in the wrapper. Claude Bash and Codex shell calls can also prove literal whole-file writes. Paper Trail recognizes cat and tee heredocs, including append mode, when the path, contents and successful execution can be established. Appends with an unknown file ending need a later witness. Scripts, in-place commands and other redirections remain diagnostic counts. Paper Trail never executes a logged command or parses arbitrary terminal output as a file snapshot.
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
