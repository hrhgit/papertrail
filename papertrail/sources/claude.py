"""Read transcripts locally. Diagnostics contain category counts only."""

import json
import re
import shlex
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from ..model import Edit, Prompt, Read, Replacement, Session
from .shell_write import shell_edits, shell_status

# Report bytes read within a large log this often, so progress keeps moving.
READ_CHUNK = 1 << 22

# A Bash result's `bashEditDiff` lists what the command changed on disk. It is not
# this session's writing when the command ran another agent (that agent's own log
# records the edit) or moved existing content in with git.
OTHER_AGENT = re.compile(
    r"(?:^|[\s;&|(`/])(?:codex|claude|kimi|dsh|aider|gemini|cursor-agent|opencode"
    r"|ai_run\.py|codex_app\.py)(?=$|[\s;&|)`'\"])"
)
GIT_CONTENT = re.compile(
    r"\bgit\s+(?:-[Cc]\s+\S+\s+)*(?:checkout|switch|merge|pull|rebase|stash|reset|restore"
    r"|cherry-pick|revert|am|apply|clone|worktree)\b"
)


def hunk_counts_match(hunk):
    rows = hunk.get("lines")
    if not isinstance(rows, list) or not all(isinstance(r, str) for r in rows):
        return False
    old = sum(not r.startswith("+") and not r.startswith("\\") for r in rows)
    new = sum(not r.startswith("-") and not r.startswith("\\") for r in rows)
    return (old, new) == (hunk.get("oldLines"), hunk.get("newLines"))


def timestamp(value):
    return datetime.fromisoformat(value).astimezone(UTC).isoformat()


def user_text(record, blocks):
    if any(record.get(k) for k in ("isMeta", "isCompactSummary", "sourceToolAssistantUUID")):
        return ""
    origin = record.get("origin")
    if origin and (not isinstance(origin, dict) or origin.get("kind") != "human"):
        return ""
    if any(b.get("type") == "tool_result" for b in blocks):
        return ""
    text = "\n".join(
        b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
    )
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
    if text.startswith(
        (
            "<local-command",
            "<command-name>",
            "<task-notification",
            "<teammate-message",
            "[Request interrupted by user",
        )
    ):
        return ""
    return text


def unified(hunks):
    parts = []
    delta = 0
    for h in hunks or []:
        old, new = h["oldStart"], h["newStart"]
        removed, added = h["oldLines"], h["newLines"]
        # Structured jsdiff positions are one-based even for an empty side.
        if new - old == delta:
            old -= int(not removed)
            new -= int(not added)
        parts.append(f"@@ -{old},{removed} +{new},{added} @@")
        delta += added - removed
        parts.extend(h["lines"])
    return "\n".join(parts) or None


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") == "text" for b in content
    ):
        return "\n".join(b["text"] for b in content)
    return ""


def read_snapshot(ts, call, result, text):
    seq, _, args, _, _, _, cwd, *_ = call
    file = result.get("file") if isinstance(result, dict) else None
    filename = (file or {}).get("filePath") if isinstance(file, dict) else None
    path = Path(filename or args["file_path"]).expanduser()
    if not path.is_absolute() and not cwd:
        return None
    path = str((Path(cwd) / path).resolve())
    if isinstance(file, dict) and isinstance(file.get("content"), str):
        first, count, total = (file.get(k) for k in ("startLine", "numLines", "totalLines"))
        if (
            not all(type(n) is int for n in (first, count, total))
            or first < 1
            or count < 0
            or total < 0
        ):
            return None
        content = file["content"].replace("\r\n", "\n")
        rows = content.split("\n")
        if len(rows) != count:
            if rows[-1] or len(rows) - 1 != count:
                return None
            rows.pop()
        complete = first == 1 and count == total
        if first + count - 1 > total:
            return None
        observed = [line + "\n" for line in rows]
        if rows and first + count - 1 == total:
            if rows[-1] == "":
                observed.pop()
            elif not content.endswith("\n"):
                observed[-1] = rows[-1]
        return Read(path, ts, seq, first, observed, complete)
    if result is not None:
        return None
    rows = []
    first = None
    for line in text.split("\n"):
        match = re.fullmatch(r" *([0-9]+)(?:\t|→)(.*)", line)
        if not match:
            return None
        number = int(match[1])
        if first is None:
            first = number
        if first < 1 or number != first + len(rows):
            return None
        rows.append(match[2] + "\n")
    return Read(path, ts, seq, first, rows, False, final_ending_unspecified=True)


class ClaudeCodeSource:
    def __init__(self):
        self.counts = Counter()
        self.failures = Counter()
        self.skipped = Counter()
        self.launches = {}
        self.native_child_calls = set()
        self.foreign_child_calls = {}
        self.tick = None

    def log_files(self, root):
        """Transcripts `sessions` reads: one per project folder, plus subagent files."""
        root = Path(root).expanduser().resolve()
        return [
            path
            for path in sorted(root.rglob("*.jsonl"))
            if path.parent.name == "subagents" or root in (path.parent, path.parent.parent)
        ]

    def sessions(self, root, progress=None):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Log directory missing. Supply an existing --logs directory.")
        self.native_child_calls.clear()
        self.foreign_child_calls.clear()
        batch = []
        inherited = {}
        self.tick = progress
        for path in self.log_files(root):
            side = path.parent.name == "subagents"
            session = self.parse(path, side)
            if progress:
                progress(path)
            if session.started_at:
                batch.append(session)
                meta = path.with_suffix(".meta.json")
                if side and meta.is_file():
                    try:
                        record = json.loads(meta.read_bytes())
                        inherited[session.id] = (session.parent, record["toolUseId"])
                    except (KeyError, ValueError, TypeError):
                        self.skipped["missing_launch_metadata"] += 1
        # Forked child transcripts can repeat the original tool result verbatim.
        # Suppress a foreign appearance only when its source transcript is also present.
        for session in batch:
            retained = []
            for edit in session.edits:
                signature = self.foreign_child_calls.get(edit.id)
                if signature is not None and signature in self.native_child_calls:
                    self.skipped["copied_child_" + edit.tool] += 1
                else:
                    retained.append(edit)
            session.edits = retained
        parents = {}
        for child, launch in inherited.items():
            if launch in self.launches:
                parents[child] = self.launches[launch]
        directories = {session.id: session.resume_cwd for session in batch}
        for session in batch:
            if session.parent:
                session.resume_cwd = directories.get(session.parent) or session.resume_cwd
            node, prompt, visited = session.id, None, set()
            if node in parents:
                session.parent = parents[node][0]
            while not prompt and node in parents and node not in visited:
                visited.add(node)
                node, prompt = parents[node]
            if prompt:
                for edit in session.edits:
                    edit.prompt = edit.prompt or prompt
            yield session

    def retain_edit(self, session, edit, child_copy, native):
        session.edits.append(edit)
        if child_copy:
            signature = (
                edit.call_id or edit.id.split(":e:", 1)[-1],
                edit.ts,
                edit.path,
                edit.tool,
                edit.before,
                edit.after,
                edit.replacement,
                edit.appended,
            )
            if native:
                self.native_child_calls.add(signature)
            else:
                self.foreign_child_calls[edit.id] = signature

    def parse(self, path, side):
        parent = path.parent.parent.name if side else None
        sid = f"{parent}/{path.stem}" if side else path.stem
        session = Session(
            sid,
            "claude-code",
            str(path),
            parent=parent,
            resume=shlex.join(["claude", "--resume", parent or sid]),
        )
        pending, ancestry, seen = {}, {}, set()
        prompt = None
        slug = ""
        unreported = 0
        with path.open("rb") as stream:
            for seq, line in enumerate(stream, 1):
                if self.tick:
                    unreported += len(line)
                    if unreported >= READ_CHUNK:
                        self.tick(path, unreported)
                        unreported = 0
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError()
                except (TypeError, ValueError, UnicodeError):
                    self.failures["invalid_json_record"] += 1
                    continue
                if record.get("type") == "custom-title":
                    session.title = record.get("customTitle") or session.title
                if not record.get("timestamp"):
                    continue
                if not side and record.get("sessionId") not in (None, sid):
                    continue
                uid = record.get("uuid")
                if uid is not None and not isinstance(uid, str):
                    self.failures["invalid_record_id"] += 1
                    continue
                if uid and uid in seen:
                    continue
                seen.add(uid)
                try:
                    ts = timestamp(record["timestamp"])
                except (ValueError, TypeError, AttributeError):
                    self.failures["invalid_timestamp"] += 1
                    continue
                session.started_at = min(session.started_at or ts, ts)
                session.ended_at = max(session.ended_at, ts)
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd and cwd != session.cwd:
                    session.cwd = str((path.parent / cwd).resolve())
                    session.resume_cwd = session.resume_cwd or session.cwd
                slug = record.get("slug") or slug
                session.branch = record.get("gitBranch") or session.branch
                msg = record.get("message")
                if not isinstance(msg, dict):
                    if uid:
                        ancestry[uid] = ancestry.get(record.get("parentUuid"), prompt)
                    continue
                raw = msg.get("content", [])
                blocks = [{"type": "text", "text": raw}] if isinstance(raw, str) else raw
                if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
                    self.failures["invalid_content"] += 1
                    continue
                prompt = ancestry.get(record.get("parentUuid"), prompt)
                if record.get("type") == "user":
                    text = user_text(record, blocks)
                    # A sidechain starts with an agent assignment, not a human turn.
                    if text and not side and not record.get("isSidechain"):
                        prompt = f"{sid}:p{seq}"
                        session.prompts.append(Prompt(prompt, seq, ts, text))
                if uid:
                    ancestry[uid] = prompt
                model = msg.get("model")
                if model and model != "<synthetic>":
                    session.model = model
                effort = record.get("effort") or record.get("perTurnEffort")
                effort = effort if isinstance(effort, str) else ""
                for block in blocks:
                    if block.get("type") == "tool_use":
                        if not isinstance(block.get("id"), str):
                            self.failures["invalid_tool_id"] += 1
                            continue
                        name = block.get("name")
                        if name in ("Agent", "Task"):
                            self.launches[(sid.split("/")[0], block.get("id"))] = (sid, prompt)
                        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Read"):
                            pending[block["id"]] = (
                                seq,
                                name,
                                block.get("input"),
                                prompt,
                                model,
                                block["id"],
                                session.cwd,
                                effort,
                            )
                    elif block.get("type") == "tool_result":
                        call = pending.pop(block.get("tool_use_id"), None)
                        if call is None:
                            continue
                        child_copy = side and record.get("toolUseResult") is None
                        native = (
                            child_copy
                            and record.get("agentId") == path.stem.removeprefix("agent-")
                            and record.get("sessionId") == parent
                        )
                        if call[1] == "Bash":
                            seq0, tool, args, instruction, model0, key, cwd, effort0 = call
                            if not isinstance(args, dict):
                                self.failures["incomplete_Bash"] += 1
                                continue
                            payload = record.get("toolUseResult") or {}
                            status = "failed" if block.get("is_error") else "success"
                            if isinstance(payload, dict) and (
                                payload.get("backgroundTaskId")
                                or payload.get("interrupted")
                                or args.get("run_in_background")
                            ):
                                status = "unconfirmed"
                            if isinstance(payload, dict):
                                status = shell_status(
                                    status,
                                    payload.get("stderr") or result_text(block.get("content")),
                                )
                            if isinstance(payload, dict) and payload.get("exitCode", 0) != 0:
                                status = "failed"
                            edits = shell_edits(
                                args.get("command"),
                                cwd,
                                status,
                                self.counts,
                                session=sid,
                                key=key,
                                seq=seq0,
                                ts=ts,
                                prompt=instruction,
                                model=model0 or "",
                                effort=effort0,
                                tool=tool,
                            )
                            if edits:
                                for edit in edits:
                                    self.retain_edit(session, edit, child_copy, native)
                                continue
                            if status == "success" and isinstance(payload, dict):
                                edits = self.bash_diff_edits(session, call, payload, ts)
                                if edits:
                                    for edit in edits:
                                        self.retain_edit(session, edit, child_copy, native)
                                    continue
                        if block.get("is_error"):
                            self.skipped["failed_" + ("read" if call[1] == "Read" else "tool")] += 1
                            continue
                        try:
                            payload = record.get("toolUseResult")
                            if (
                                payload is not None
                                and sum(b.get("type") == "tool_result" for b in blocks) > 1
                            ):
                                self.failures["ambiguous_shared_result"] += 1
                                continue
                            text = result_text(block.get("content"))
                            if call[1] == "Read":
                                snapshot = read_snapshot(ts, call, payload, text)
                                if snapshot is None:
                                    self.skipped["unusable_read"] += 1
                                else:
                                    session.reads.append(snapshot)
                            else:
                                edit = self.edit(session, ts, call, payload, text)
                                if edit:
                                    self.retain_edit(session, edit, child_copy, native)
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            AttributeError,
                            OSError,
                            RuntimeError,
                        ):
                            self.failures["incomplete_" + call[1]] += 1
        self.skipped["unmatched_calls"] += len(pending)
        title_file = path.with_suffix("") / "custom-title.json"
        if title_file.is_file() and not session.title:
            try:
                title = json.loads(title_file.read_bytes())
                session.title = title if isinstance(title, str) else title.get("customTitle", "")
            except (ValueError, AttributeError):
                self.failures["invalid_title"] += 1
        session.title = session.title or slug
        return session

    def bash_diff_edits(self, session, call, result, ts):
        """Edits from the diff Claude Code records for a Bash command. A file counts
        only when the command names it, and the command neither runs another agent nor
        brings content in with git. Each hunk is a numbered patch, so replay checks
        its context against what the file held at that point."""
        seq, _, args, prompt, model, tool_id, cwd, *rest = call
        diff = result.get("bashEditDiff")
        if not isinstance(diff, dict) or not isinstance(diff.get("files"), list):
            return []
        command = args.get("command") if isinstance(args.get("command"), str) else ""
        if OTHER_AGENT.search(command) or GIT_CONTENT.search(command):
            self.skipped["bash_diff_other_writer"] += len(diff["files"])
            return []
        edits = []
        for i, entry in enumerate(diff["files"]):
            filename = entry.get("filePath") if isinstance(entry, dict) else None
            hunks = entry.get("hunks") if isinstance(entry, dict) else None
            if not isinstance(filename, str) or not filename or not isinstance(hunks, list):
                self.failures["incomplete_bash_diff"] += 1
                continue
            path = Path(filename).expanduser()
            if not path.is_absolute():
                if not cwd:
                    self.failures["relative_path_without_cwd"] += 1
                    continue
                path = Path(cwd) / path
            if path.name not in command:
                self.skipped["bash_diff_unnamed_file"] += 1
                continue
            if not hunks or not all(isinstance(h, dict) and hunk_counts_match(h) for h in hunks):
                self.skipped["bash_diff_incomplete_hunks"] += 1
                continue
            try:
                patch = unified(hunks)
            except (KeyError, TypeError):
                self.failures["incomplete_bash_diff"] += 1
                continue
            edits.append(
                Edit(
                    f"{session.id}:e:{tool_id}:d{i}",
                    seq,
                    ts,
                    "Bash",
                    str(path.resolve()),
                    prompt,
                    model or session.model,
                    patch=patch,
                    effort=rest[0] if rest else "",
                    call_id=tool_id,
                )
            )
        self.counts["bash_diff_edits"] += len(edits)
        return edits

    def edit(self, session, ts, call, result, text=""):
        seq, name, args, prompt, model, tool_id, cwd, *rest = call
        effort = rest[0] if rest else ""
        if name == "Bash":
            key = (
                "bash_diff_without_write_evidence"
                if isinstance(result, dict) and result.get("bashEditDiff")
                else "bash_without_diff"
            )
            self.skipped[key] += 1
            return
        structured = isinstance(result, dict)
        result = result if structured else {}
        filename = (
            (result.get("notebook_path") or args.get("notebook_path"))
            if name == "NotebookEdit"
            else (result.get("filePath") or args.get("file_path"))
        )
        if not isinstance(filename, str) or not filename:
            raise ValueError()
        path = Path(filename).expanduser()
        if not path.is_absolute():
            if not cwd:
                self.failures["relative_path_without_cwd"] += 1
                return
            path = Path(cwd) / path
        event = Edit(
            f"{session.id}:e:{tool_id}",
            seq,
            ts,
            name,
            str(path.resolve()),
            prompt,
            model or session.model,
            effort=effort,
        )
        if not structured:
            names = {filename, event.path}
            created = any(
                re.match(r"^File created successfully at: " + re.escape(f) + r"(?:$| \(|\n)", text)
                for f in names
            )
            updated = any(
                re.match(
                    r"^The file " + re.escape(f) + r" has been updated(?: successfully)?(?:[.!]|$)",
                    text,
                )
                for f in names
            )
            if name == "Write" and (created or updated) and isinstance(args.get("content"), str):
                event.before = "" if created else None
                event.after = args["content"]
                return event
            if name == "Edit" and updated:
                old, new = args.get("old_string"), args.get("new_string")
                if isinstance(old, str) and isinstance(new, str):
                    event.replacement = Replacement(old, new, args.get("replace_all") is True)
                    return event
            self.failures["unconfirmed_" + name] += 1
            return
        if name == "NotebookEdit":
            event.before, event.after = result["original_file"], result["updated_file"]
            if not all(isinstance(v, str) for v in (event.before, event.after)):
                raise ValueError()
            return event
        event.patch = unified(result.get("structuredPatch"))
        before = result.get("originalFile")
        if before is None:
            if name == "Write" and result.get("type") == "create":
                before = ""
            elif name == "Write" and isinstance(result.get("content"), str):
                event.after = result["content"]
                return event
            elif event.patch:
                return event
            elif name == "Write" and "originalFile" in result and result.get("type") is None:
                before = ""
            else:
                self.failures["missing_preimage_" + name] += 1
                return
        if not isinstance(before, str):
            raise TypeError()
        if name == "Write":
            after = result["content"]
        else:
            after = before
            edits = result.get("edits", args.get("edits")) if name == "MultiEdit" else [result]
            for edit in edits:
                old = edit.get("oldString", edit.get("old_string"))
                new = edit.get("newString", edit.get("new_string"))
                if not old and not after:
                    after = new
                elif (
                    isinstance(old, str)
                    and old
                    and not edit.get("replaceAll", edit.get("replace_all"))
                    and after.find(old, after.find(old) + 1) != -1
                ):
                    event.before = before
                    if not event.patch and name == "Edit":
                        event.replacement = Replacement(old, new)
                    return event
                elif not old or old not in after:
                    if event.patch:
                        event.before = before
                        return event
                    raise ValueError()
                else:
                    after = after.replace(
                        old, new, -1 if edit.get("replaceAll", edit.get("replace_all")) else 1
                    )
        if not isinstance(after, str):
            raise TypeError()
        event.before, event.after = before, after
        return event
