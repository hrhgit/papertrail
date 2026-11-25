# Adding a transcript source

Implement `sessions(root: Path) -> Iterator[Session]` in `papertrail/sources/` and register the class and its default log root under its CLI name in `SOURCES` and `DEFAULT_LOGS`. The entry point creates a fresh source for each ingest. Claude Code and Codex both implement this interface. Sources are combined before project selection and chronological replay. For the reader's build progress, also implement `log_files(root) -> list[Path]`, the files `sessions` will read, and accept `sessions(root, progress=None)`: call `progress(path, nbytes)` every few megabytes while reading a file and `progress(path)` when it is finished.

## Normalized records

Import the dataclasses from `papertrail.model`. All records serialize with `json.dumps(dataclasses.asdict(session))`. A source owns knowledge of its transcript format, human-turn markers, tool status and resume command.

| Record | Supply |
| --- | --- |
| Session | Globally distinct ID, agent name, absolute log path, model, title, cwd, branch, first and last UTC timestamps. Set the parent session ID for a child. Optional `resume` and `resume_cwd` are displayed to the user. |
| Prompt | ID, stable position in its transcript, UTC timestamp and the complete human instruction. Keep prompts in conversation order. |
| Edit | Unique ID for the tool call, stable sequence, completion time, tool, canonical absolute file path, initiating prompt ID, the model used by this call and its reasoning effort (`effort`, the source's own label such as `high`; leave empty when the transcript does not record it). Preserve the prompt captured at call time even if a result arrives after the next human turn. |
| Read | Store these observations in `Session.reads`. Supply the path, UTC time, sequence, one-based start line, list of physical lines (with their line endings), and `complete`. Set `final_ending_unspecified` for a display that cannot reveal the final newline. A complete snapshot must establish both ends of the file. |

Use a consistent UTC ISO 8601 representation so times sort chronologically (e.g. `2025-01-01T12:00:00+00:00`). IDs must remain distinct within each label across sessions, including parallel calls in one message. Correlate calls with successful results by tool ID. For a child, a known parent instruction may be referenced directly. Emit the parent session too. Use `None` for an unknown prompt and an empty string for unknown descriptive metadata. Diagnostics may be supplied as numeric mappings named `failures` and `skipped`. Keep their keys categorical.

### Describing a change

**Snapshots.** Set `before` and `after` to the exact text. The empty string represents a known empty file. A verified unified patch in `patch` can disambiguate identical lines. Replay checks its output against the full postimage and falls back to comparing the two snapshots if they disagree.

**Patch only.** Leave both snapshots as `None` and supply one file's unified diff, or an update body with `@@` followed by space-prefixed context, deletions and additions. If a preimage exists but only the patch describes the result, set `before` and `patch`. Numbered sections supply the old range and context even for a first edit. Use standard unified coordinates for zero-length ranges. Unnumbered sections need unique matching context. A unified creation patch can declare `--- /dev/null`. Separate multi-file operations into edits with distinct IDs.

**Full output only.** For a confirmed create, supply `before=""` and `after=content`. For a confirmed overwrite with an unknown preimage, use `before=None` and the entire output in `after`. Replay retains equal known lines. When this is the first evidence about the file, the whole emitted text belongs to the write and its hunk has `preimage_unknown=True`.

**Literal replacement.** Set `replacement=Replacement(old, new, all=False)`. Supply exact fragments including whitespace and line breaks. The two fragments may start or end inside a line. Set `all=True` only when the tool confirms replace-all semantics. A missing or ambiguous position can be recovered from unique post-edit text in a later numbered patch, read, full preimage or disk snapshot. A complete later observation can also anchor a chain of literal edits when each intermediate change can be inverted uniquely. An overwrite without recoverable earlier text ends that search. Unplaced calls remain in the session.

**File lifetimes and shared calls.** Codex supplies `operation` as `add`, `update`, `delete` or `move`. Other adapters can leave this field empty for ordinary writes. A delete can carry its preimage. A move sets `path` to the destination and `moved_from` to the old absolute path. It can include an update patch. An empty committed update or a pure move needs no hunk. An add begins a new lifetime. Split a multi-file change into separately identified edits with the same `call_id`. Keep the call's prompt, working directory, model and effort until the confirming receipt arrives.

**Codex example.** Read date-partitioned `rollout-*.jsonl`. The session metadata provides the native thread ID. A child has `source.subagent.thread_spawn.parent_thread_id`. Capture the turn context when the call starts. Prefer `item_completed/FileChange` for committed changes and their numbered diffs. Legacy patch begin/end pairs can provide changes and confirmation separately. A literal tool input or shell heredoc can fill in when an independent success receipt exists. Accept only mechanically decoded static arguments. A wrapper completing does not establish a shell command's exit status. Skip interpolated inputs without a committed event. An unpaired cumulative turn diff has no unique writer. Use the completed user-message event when available. A missing title falls back to the first human message. The resume command uses the native thread ID.

**Literal shell writes.** Use the shared shell reader only with a command, its working directory and its own completion evidence. A whole-file overwrite supplies `after` and `operation="overwrite"`. An append supplies the literal suffix in `appended` and `operation="append"`. Keep `before=None` unless a separate record proves the preimage. Replay needs a known end of file or an immediate complete observation to place an append. Supply the launching human instruction for a child only when its parent thread and a unique successful launch agree on the child's ID or task path.

**Reads.** Include successful observations with reliable line numbering. Strip only a verified display prefix, preserve indentation and reject discontinuous numbering or mixed tool output. Numbered display text by itself gives a window with unknown EOF. Reads establish text and may expose external changes. They create no edit or Read node. Unknown positions carry no replacement edge.

```python
from papertrail.model import Edit

edit = Edit(
    id="example/session-1/tool-2", seq=4, ts="2025-01-01T12:00:00+00:00",
    tool="apply_patch", path="/example/settings.py",
    prompt="example/session-1/prompt-1", model="example-model",
    patch="@@ -1,2 +1,2 @@\n # Settings\n-level = 1\n+level = 2",
)
```

The source should resolve paths against the call's cwd before replay. Absolute paths provide file identity. The engine handles cross-session ordering, line ownership, disk comparison and replacement counts. The graph builder supplies the same relationships and queries for each source.

## Verify the adapter

Create invented transcripts with successful and failed calls, injected text, parallel calls and late results. Assert the normalized records and their JSON serialization. Replay a creation and a replacement from different sessions, then check the line and both instructions via `Queries.trace`. Add a test where a patch cannot be placed. Use only aggregate counts for diagnostics over private data.

```sh
uv run papertrail ingest --source example --logs /path/to/fictional/logs --all --store .papertrail/example.rl
uv run papertrail stats --store .papertrail/example.rl --json
```
