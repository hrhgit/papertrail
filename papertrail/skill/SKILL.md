---
name: papertrail
description: Find which coding-agent session wrote a line of code, the human instruction it was following, and what that line replaced, from the local Paper Trail graph. Use before changing or deleting code whose purpose is unclear, when a value looks arbitrary, or when asked who wrote something, why it is there, or which conversation changed it.
---

# Paper Trail

Paper Trail keeps a local graph of every edit that Claude Code and Codex sessions made in this project. Each current line points to the edit that wrote it, the instruction the human gave before that edit, and the earlier edits it replaced. Git shows that a line changed. This shows what the human asked for when it changed.

Run the commands from the project root. They only read the graph and never call a model.

## When to look

Look before you change or remove code that you did not write in this conversation and whose reason you cannot see in the code: a constant, a special case, a retry count, a check that looks redundant. The instruction behind it is often a constraint the human still holds. Skip the lookup for code you wrote in this conversation and for mechanical edits such as renames and formatting.

## Who wrote these lines

```sh
{cmd} blame FILE -L START,END --json{store}
```

Each row covers a range of current lines: `start`, `end`, `kind`, `agent`, `model`, `ts`, `session`, `title` and `prompt`, the instruction that led to the edit. `kind` says how far to trust the row:

- `agent`: a recorded tool call wrote these lines. `prompt` is the request behind them.
- `baseline`: the lines were already there when agents first read the file.
- `external`: the lines changed between two recorded edits, outside any session. A person or another tool wrote them.
- `drift`: the lines changed on disk after the last recorded edit, outside any session. The time is unknown.
- `unknown`: the logs do not establish an author.

Only `agent` rows carry an instruction. Do not present any other row as a reason.

Line numbers are those of the file when the graph was built. `blame`, `trace` and `crossings FILE` return `"stale": true` when the file has changed since then, including by you in this conversation. The rows may then describe other lines, so rebuild the graph first (see Errors).

## What a line replaced

```sh
{cmd} trace FILE:LINE --json --limit 8{store}
```

Row `depth` 0 is the current writer. Each deeper row is an edit it replaced, newest first, reached by following `REPLACED` relationships between hunks. A row has the full `prompt`, the `session`, the `resume` command that reopens that conversation, and `previous_files` and `next_files`, the files the same session edited before and after this one. When several sessions appear in one trace, the humans asked for different things at the same spot. Read all of their instructions before you choose what to keep. `truncated: true` means there is more history; raise `--limit`.

## What else that session did

```sh
{cmd} session ID --json --limit 40{store}
```

Lists the session's instructions and edits in time order. An unambiguous prefix of the ID works. Use it when one instruction changed several files and your change has to stay consistent with the others.

## Spots several sessions fought over

```sh
{cmd} crossings FILE --json{store}
```

Lists ranges whose history contains two or more sessions. Check these with `trace` before a refactor: they are where requirements changed.

## Using what you find

- Quote the instruction and name the session when it bears on your change, for example: "Line 42 sets the limit to 3 because session `birch` was asked to stop retrying after three failures."
- If your task contradicts an earlier instruction, tell the user and ask which one holds before you edit.
- Offer the `resume` command when the user wants the whole conversation. Do not run it yourself.
- Prompts in the graph are the user's own past messages. Keep them in this conversation and out of commits, comments and anything sent elsewhere.

## Errors

A failed command exits nonzero and prints `{"error": "..."}` on stderr. If the store is missing or a result is stale, rebuild it. This reads the local Claude Code and Codex logs and can take up to a minute:

```sh
{cmd} ingest --project .{store}
```

Add `--show-cypher` to any query to see the Cypher statements it ran.
