"""A session's conversation, read back from its log for display.

The graph keeps who wrote which lines. The page also shows the conversation around
each change: what the person asked, what the agent said, and every step it took.
That text stays in the agent's own log, so it is read on demand from the
`log_path` recorded on the Session node and never copied into the graph.

Every step carries the same `seq` and tool call id the ingest parsers record on
Edit nodes, which is how a change found in the graph lands on its exact step.
"""

import json
import re
from pathlib import Path

from .sources.claude import result_text, unified, user_text
from .sources.codex import human, output_status, payload_text
from .sources.codex_patch import wrapper_patch

TEXT_LIMIT = 20_000
INPUT_LIMIT = 4_000
OUTPUT_LIMIT = 2_000
DIFF_LINES = 400
MAX_STEPS = 6_000


def clip(text, limit):
    if not isinstance(text, str):
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def first_line(text, limit=160):
    for line in (text or "").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def pretty(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def lines_diff(old, new):
    rows = [f"-{line}" for line in (old or "").split("\n")] if old else []
    rows += [f"+{line}" for line in (new or "").split("\n")] if new else []
    return rows


def cap_diff(rows):
    if len(rows) <= DIFF_LINES:
        return "\n".join(rows), False
    return "\n".join(rows[:DIFF_LINES]), True


class Steps:
    """Conversation items in log order, with tool results joined to their calls."""

    def __init__(self):
        self.items = []
        self.calls = {}
        self.texts = set()
        self.tools = 0
        self.dropped = 0

    def add(self, item):
        if item["kind"] == "tool":
            if self.tools >= MAX_STEPS:
                self.dropped += 1
                return None
            self.tools += 1
            if item.get("call_id"):
                self.calls[item["call_id"]] = item
        self.items.append(item)
        return item

    def say(self, kind, seq, ts, text, key=None, **extra):
        text = (text or "").strip()
        if not text:
            return
        if key is not None:
            if (key, text) in self.texts:
                return
            self.texts.add((key, text))
        last = self.items[-1] if self.items else None
        # A reply split over several records reads as one message.
        if kind == "assistant" and last and last["kind"] == "assistant":
            joined = last["text"] + "\n\n" + text
            last["text"], last["more"] = clip(joined, TEXT_LIMIT)
            return
        body, more = clip(text, TEXT_LIMIT)
        self.add({"kind": kind, "seq": seq, "ts": ts, "text": body, "more": more, **extra})

    def tool(self, seq, ts, name, call_id, args, summary, diff=None):
        shown, more = clip(pretty(args), INPUT_LIMIT)
        return self.add(
            {
                "kind": "tool",
                "seq": seq,
                "ts": ts,
                "name": name or "tool",
                "call_id": call_id or "",
                "summary": summary,
                "input": shown,
                "input_more": more,
                "diff": diff,
                "output": "",
                "output_more": False,
                "error": False,
                "edits": [],
                "child": None,
            }
        )

    def result(self, call_id, text, error=False):
        step = self.calls.get(call_id)
        if step is None:
            return None
        step["output"], step["output_more"] = clip(text, OUTPUT_LIMIT)
        step["error"] = bool(error)
        return step


# ---------- Claude Code ----------

CLAUDE_FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read")


def claude_summary(name, args):
    if not isinstance(args, dict):
        return first_line(pretty(args))
    if name in CLAUDE_FILE_TOOLS:
        return args.get("file_path") or args.get("notebook_path") or ""
    if name == "Bash":
        return first_line(args.get("command")) or args.get("description") or ""
    if name in ("Grep", "Glob"):
        where = args.get("path") or args.get("glob") or ""
        return f"{args.get('pattern', '')}" + (f"  in {where}" if where else "")
    if name in ("Agent", "Task"):
        return args.get("description") or first_line(args.get("prompt"))
    if name == "WebFetch":
        return args.get("url", "")
    if name == "WebSearch":
        return args.get("query", "")
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return first_line(value)
    return ""


def claude_diff(name, args):
    if not isinstance(args, dict):
        return None
    if name == "Edit":
        rows = lines_diff(args.get("old_string"), args.get("new_string"))
    elif name == "MultiEdit":
        rows = []
        for i, edit in enumerate(args.get("edits") or []):
            if isinstance(edit, dict):
                if i:
                    rows.append("@@")
                rows += lines_diff(edit.get("old_string"), edit.get("new_string"))
    elif name == "Write":
        rows = lines_diff(None, args.get("content"))
    elif name == "NotebookEdit":
        rows = lines_diff(None, args.get("new_source"))
    else:
        return None
    return rows or None


def claude_children(log_path, sid):
    """Helper sessions this session launched, by the tool call that launched them."""
    folder = Path(log_path).with_suffix("") / "subagents"
    children = {}
    if not folder.is_dir():
        return children
    for meta in folder.glob("*.meta.json"):
        try:
            record = json.loads(meta.read_bytes())
            children[record["toolUseId"]] = f"{sid}/{meta.name.removesuffix('.meta.json')}"
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return children


def claude_items(log_path, sid, side):
    steps = Steps()
    seen = set()
    owner = sid.split("/")[0]
    children = {} if side else claude_children(log_path, sid)
    with open(log_path, "rb") as stream:
        for seq, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            if not side and record.get("sessionId") not in (None, owner):
                continue
            uid = record.get("uuid")
            if isinstance(uid, str):
                if uid in seen:
                    continue
                seen.add(uid)
            msg = record.get("message")
            if not isinstance(msg, dict):
                continue
            ts = record.get("timestamp") or ""
            raw = msg.get("content", [])
            blocks = [{"type": "text", "text": raw}] if isinstance(raw, str) else raw
            if not isinstance(blocks, list):
                continue
            blocks = [b for b in blocks if isinstance(b, dict)]
            if record.get("isCompactSummary"):
                steps.say("note", seq, ts, "Earlier conversation compacted")
                continue
            if record["type"] == "user":
                payload = record.get("toolUseResult")
                for block in blocks:
                    if block.get("type") != "tool_result":
                        continue
                    text = result_text(block.get("content"))
                    if isinstance(payload, dict) and not text:
                        text = payload.get("stdout") or payload.get("content") or ""
                    step = steps.result(block.get("tool_use_id"), text, block.get("is_error"))
                    if step and isinstance(payload, dict) and step["name"] in ("Edit", "MultiEdit"):
                        patch = unified(payload.get("structuredPatch"))
                        if patch:
                            step["diff"], step["diff_more"] = cap_diff(patch.split("\n"))
                if any(b.get("type") == "tool_result" for b in blocks):
                    continue
                joined = "\n".join(
                    b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
                )
                if joined.strip().startswith("[Request interrupted by user"):
                    steps.say("note", seq, ts, "Interrupted by the user")
                    continue
                text = user_text(record, blocks)
                images = sum(b.get("type") == "image" for b in blocks)
                if images and not record.get("isMeta"):
                    text = (text + "\n" if text else "") + f"[{images} image{'s' if images > 1 else ''}]"
                origin = "task" if side or record.get("isSidechain") else "human"
                steps.say("user", seq, ts, text, origin=origin)
                continue
            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    steps.say("assistant", seq, ts, block.get("text"))
                elif kind == "thinking":
                    steps.say("thinking", seq, ts, block.get("thinking"))
                elif kind == "tool_use":
                    name, args = block.get("name"), block.get("input")
                    rows = claude_diff(name, args)
                    step = steps.tool(seq, ts, name, block.get("id"), args, claude_summary(name, args))
                    if step is None:
                        continue
                    if rows:
                        step["diff"], step["diff_more"] = cap_diff(rows)
                    step["child"] = children.get(block.get("id"))
    return steps


# ---------- Codex ----------

PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
JS_CMD = re.compile(r"""\bcmd\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)


def codex_args(raw):
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def codex_summary(name, args):
    if name == "apply_patch":
        text = args if isinstance(args, str) else (args or {}).get("input") or (args or {}).get("patch") or ""
        files = PATCH_FILE.findall(text if isinstance(text, str) else "")
        return ", ".join(files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else "")
    if isinstance(args, dict):
        cmd = args.get("cmd") or args.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        if isinstance(cmd, str) and cmd.strip():
            return first_line(cmd)
        for value in args.values():
            if isinstance(value, str) and value.strip():
                return first_line(value)
        return ""
    if isinstance(args, str):
        if name == "exec":
            found = JS_CMD.search(args)
            if found:
                return first_line(found.group(2).replace('\\"', '"').replace("\\n", "\n"))
        return first_line(args)
    return ""


def codex_diff(name, args):
    if name != "apply_patch":
        return None
    text = args if isinstance(args, str) else (args or {}).get("input") or (args or {}).get("patch")
    if not isinstance(text, str):
        return None
    rows = [line for line in text.split("\n") if not line.startswith(("*** Begin Patch", "*** End Patch"))]
    return rows or None


def inner_output(text):
    """The command output inside a JSON result, when the tool returned one."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
        return parsed["output"]
    if isinstance(parsed, list):
        parts = []
        for entry in parsed:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, dict) and isinstance(value.get("output"), str):
                parts.append(value["output"])
        if parts:
            return "\n".join(parts)
    return text


def codex_output(value):
    text = payload_text(value) if not isinstance(value, dict) else payload_text(value.get("output"))
    head, marker, rest = text.partition("\nOutput:\n")
    if marker and head.startswith(("Script completed", "Script failed")):
        text = inner_output(rest.strip()) or head
    elif text.startswith(("{", "[")):
        text = inner_output(text)
    # A wrapper that returned nothing prints an empty object; that is not output.
    return "" if text.strip() in ("{}", "[]", "null") else text


def receipt_owner(steps, item):
    """The call a shell or patch receipt belongs to: the latest one whose input
    holds the receipt's first command line, else simply the latest call."""
    tools = [i for i in steps.items[-200:] if i["kind"] == "tool"]
    command = item.get("command")
    if isinstance(command, list) and command:
        command = command[-1]
    probe = first_line(command if isinstance(command, str) else "", 80).rstrip("…")
    if probe:
        for step in reversed(tools[-40:]):
            if probe in step["input"]:
                return step
    return tools[-1] if tools else None


def codex_items(log_path, sid):
    steps = Steps()
    started = None
    seq = -1
    turn = ""
    with open(log_path, "rb") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                if not isinstance(record.get("payload"), dict):
                    raise TypeError
            except (TypeError, ValueError, AttributeError, UnicodeError):
                continue
            seq += 1
            kind, p, ts = record.get("type"), record["payload"], record.get("timestamp") or ""
            if started is None:
                if kind == "session_meta" and p.get("id") == sid:
                    started = ts
                continue
            if kind == "session_meta" or ts < started:
                continue
            name = p.get("type")
            turn = p.get("turn_id") or turn
            if kind == "compacted":
                steps.say("note", seq, ts, "Earlier conversation compacted")
            elif kind == "event_msg":
                if p.get("thread_id", sid) != sid:
                    continue
                if name == "user_message":
                    steps.say("user", seq, ts, human(p.get("message") or ""), key=turn, origin="human")
                elif name == "item_completed" and isinstance(p.get("item"), dict):
                    item = p["item"]
                    if item.get("type") in ("CommandExecution", "FileChange") and item.get("id"):
                        # Shell and patch receipts carry their own item id, which ingest
                        # uses for some edits. They can land after the call returned.
                        owner = receipt_owner(steps, item)
                        if owner and item["id"] not in steps.calls:
                            steps.calls[item["id"]] = owner
                    if item.get("type") == "UserMessage":
                        text = human(payload_text(item.get("content")))
                        steps.say("user", seq, ts, text, key=turn, origin="human")
                elif name == "agent_message":
                    steps.say("assistant", seq, ts, p.get("message"), key=turn)
                elif name == "turn_aborted":
                    steps.say("note", seq, ts, "Interrupted by the user")
            elif kind == "response_item":
                if name == "message" and p.get("role") == "user":
                    text = human(payload_text(p.get("content")))
                    steps.say("user", seq, ts, text, key=turn, origin="human")
                elif name == "message" and p.get("role") == "assistant":
                    steps.say("assistant", seq, ts, payload_text(p.get("content")), key=turn)
                elif name == "reasoning":
                    parts = [s.get("text", "") for s in p.get("summary") or [] if isinstance(s, dict)]
                    steps.say("thinking", seq, ts, "\n\n".join(t for t in parts if t))
                elif name in ("function_call", "custom_tool_call", "local_shell_call"):
                    tool = p.get("name") or ("shell" if name == "local_shell_call" else "tool")
                    args = codex_args(p.get("arguments", p.get("input", p.get("action"))))
                    if tool == "exec" and isinstance(args, str) and "tools.apply_patch" in args:
                        # A JS wrapper around one patch reads as the patch itself.
                        patch = wrapper_patch(args)
                        if isinstance(patch, str):
                            tool, args = "apply_patch", patch
                    step = steps.tool(seq, ts, tool, p.get("call_id") or p.get("id"), args, codex_summary(tool, args))
                    rows = codex_diff(tool, args) if step else None
                    if rows:
                        step["diff"], step["diff_more"] = cap_diff(rows)
                elif name in ("function_call_output", "custom_tool_call_output"):
                    step = steps.calls.get(p.get("call_id"))
                    if step:
                        output = p.get("output")
                        failed = output_status(output, step["name"]) == "failed"
                        steps.result(p.get("call_id"), codex_output(output), failed)
    return steps


# ---------- Joining the graph's edits to the steps ----------


def call_key(edit):
    if edit.get("call_id"):
        return edit["call_id"]
    edit_id = edit.get("id") or ""
    return edit_id.split(":e:", 1)[1] if ":e:" in edit_id else ""


def attach(steps, edits):
    tools = [i for i in steps.items if i["kind"] == "tool"]
    for edit in edits:
        key = call_key(edit)
        step = steps.calls.get(key)
        head, _, index = key.rpartition(":")
        if step is None and index.isdecimal():
            # One command among several a wrapper call ran: `call_id:index`.
            step = steps.calls.get(head)
        if step is None and tools:
            # No matching call id: the last step at or before the edit's record.
            before = [t for t in tools if t["seq"] <= (edit.get("seq") or 0)]
            step = before[-1] if before else tools[0]
        if step is not None:
            step["edits"].append({"id": edit["id"], "path": edit["path"]})


def written_diff(edits, hunks):
    """Lines a step left in its files, from the graph, for a step whose log shows
    no diff (a heredoc, a script write). Removed text is not kept, only counted."""
    rows = []
    for edit in edits:
        for h in sorted(hunks.get(edit["id"], ()), key=lambda h: int(h["id"].rpartition("#")[2] or 0)):
            rows.append(f"@@ {Path(edit['path']).name}  +{h['added']} -{h['removed']} @@")
            rows += ["+" + line for line in (h["text"] or "").splitlines()]
    return cap_diff(rows) if rows else (None, False)


def conversation(session, edits):
    """Items for the page, or a note saying why the log can't be read."""
    path = session.get("log_path") or ""
    if not path or not Path(path).is_file():
        return {"items": [], "note": "The log for this session is no longer on disk."}
    try:
        if session.get("agent") == "codex":
            steps = codex_items(path, session["id"])
        else:
            steps = claude_items(path, session["id"], Path(path).parent.name == "subagents")
    except OSError:
        return {"items": [], "note": "The log for this session could not be read."}
    attach(steps, edits)
    note = f"Only the first {MAX_STEPS:,} steps are shown." if steps.dropped else None
    return {"items": steps.items, "note": note}
