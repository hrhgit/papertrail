"""Local Codex rollout reader. Evidence comes from committed changes or paired receipts."""

import hashlib
import json
import re
import shlex
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..cache import stamp
from ..logfile import ZstdUnavailable, compressed, compressed_read, log_stem, open_log, zstd_available
from ..model import Edit, Hint, Prompt, Session
from .claude import READ_CHUNK, git_content, other_agent, read_only
from .codex_patch import (
    absolute,
    command_string,
    event_change,
    opaque_categories,
    parse_patch,
    shell_calls,
    shell_invocation,
    wrapper_command,
    wrapper_patch,
)
from .paths import cached_parse, observe_paths
from .shell_write import ECHO_WRITE, shell_edits, shell_status


@dataclass(frozen=True)
class Turn:
    model: str
    effort: str
    cwd: str
    prompt: str | None
    turn_id: str


@dataclass
class Call:
    id: str
    name: str
    turn: Turn
    patch: str | None
    command: str = ""
    events: set = field(default_factory=set)
    command_events: int = 0
    marker: bool = False
    shell: bool = False
    route: str = ""
    failed_event: bool = False
    event_paths: list = field(default_factory=list)


def payload_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            x["text"] for x in value if isinstance(x, dict) and isinstance(x.get("text"), str)
        )
    return ""


def human(text):
    stripped = text.strip()
    if stripped.startswith(
        (
            "<environment_context",
            "<INSTRUCTIONS",
            "# AGENTS.md instructions",
            "<subagent_",
            "<teammate",
            "<turn_aborted",
            "<user_shell_command",
            "<permissions instructions",
        )
    ):
        return ""
    return stripped


def output_status(value, tool):
    text = payload_text(value)
    if isinstance(value, dict):
        text = payload_text(value.get("output"))
        exit_code = (value.get("metadata") or {}).get("exit_code", value.get("exit_code"))
    else:
        exit_code = None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return output_status(parsed, tool)
            except ValueError:
                pass
    if re.search(
        r"(?im)^(?:Script failed|apply_patch verification failed|Failed to |error:|Process exited with code [1-9])",
        text,
    ):
        return "failed"
    if exit_code is not None:
        return "success" if exit_code == 0 else "failed"
    if re.search(r"(?m)^Success\. Updated the following files:", text):
        return "success"
    if tool == "exec_command":
        # A JS wrapper may print the returned exec_command object as JSON.
        inner = text.partition("\nOutput:\n")[2]
        if inner.startswith("{"):
            try:
                return output_status(json.loads(inner), tool)
            except ValueError:
                pass
        return (
            "success" if re.search(r"(?m)^Process exited with code 0\s*$", text) else "unconfirmed"
        )
    return (
        "success"
        if tool == "exec" and text.startswith("Script completed") and "\nOutput:" in text
        else "unconfirmed"
    )


class Rollout:
    def __init__(self, session, source):
        self.s = session
        self.source = source
        self.count = source.counts
        self.turn = Turn("", "", session.cwd, None, "")
        self.turns = {}
        self.calls = {}
        self.shell_inputs = {}
        self.processes = {}
        self.spawn_calls = {}
        self.launches = []
        self.event_commands = Counter()
        self.native_commands = set()
        self.hint_candidates = []
        self.shell_results = []
        self.runtime_shell = {}
        self.finished_shell_calls = set()
        self.seen = set()
        self.prompt_by_text = {}
        self.seq = 0
        self.shell_receipts = {}
        self.patch_starts = {}
        self.closed = {}
        self.closed_at = defaultdict(dict)
        self.outputs = {}
        self.native_turns = set()
        self.prompt_info = {}

    def prompt(self, text, ts, turn_id, native=False):
        text = human(text)
        if not text or self.s.parent:
            return
        key = (turn_id, text)
        # A response item and its completed item describe the same user input.
        prior = self.prompt_by_text.get(key)
        if native:
            self.native_turns.add(turn_id)
        if prior and prior[1] != native:
            self.prompt_info[prior[0]] = (turn_id, True)
            return
        p = Prompt(f"{self.s.id}:p:{self.seq}", self.seq, ts, text)
        self.s.prompts.append(p)
        self.prompt_info[p.id] = (turn_id, native)
        self.prompt_by_text[key] = (p.id, native)
        current = self.turns.get(turn_id, self.turn)
        updated = Turn(current.model, current.effort, current.cwd, p.id, turn_id)
        self.turns[turn_id] = updated
        if turn_id == self.turn.turn_id:
            self.turn = updated

    def emit(self, changes, context, call_id, event_id, ts):
        for index, change in enumerate(changes):
            self.s.edits.append(
                Edit(
                    f"{self.s.id}:e:{event_id}:{index:04d}",
                    self.seq,
                    ts,
                    "apply_patch",
                    change.path,
                    context.prompt,
                    context.model,
                    before=change.before,
                    after=change.after,
                    patch=change.patch,
                    effort=context.effort,
                    call_id=call_id,
                    moved_from=change.moved_from,
                    operation=change.operation,
                )
            )
            self.count["retained_files"] += 1

    def owner(self, key, turn_id, changes=None):
        if key in self.calls or key in self.closed:
            return self.calls.get(key) or self.closed[key]
        eligible = [c for c in self.calls.values() if c.turn.turn_id == turn_id]
        if len(eligible) == 1 and eligible[0].name == "exec":
            return eligible[0]
        if changes:
            # A delayed nested receipt may use an item ID that differs from the
            # call ID. Only reconcile an adjacent closed call with matching data.
            adjacent = self.closed_at.get((self.seq - 1, turn_id))
            if adjacent:
                eligible.extend(adjacent.values())
            matches = []
            for candidate in eligible:
                if candidate.patch:
                    try:
                        ops = parse_patch(candidate.patch, candidate.turn.cwd)
                        actual = [
                            event_change(p, d, candidate.turn.cwd) for p, d in changes.items()
                        ]
                    except (ValueError, OSError, AttributeError):
                        continue
                    if [op.identity() for op in ops] == [op.identity() for op in actual]:
                        matches.append(candidate)
            if len(matches) == 1:
                return matches[0]
        elif len(eligible) == 1:
            return eligible[0]
        return None

    def file_event(self, item, ts, turn_id):
        key = item.get("id")
        if not key or ("file", key) in self.seen:
            self.count["duplicate_events"] += 1
            return
        self.seen.add(("file", key))
        call = self.owner(item.get("call_id") or key, turn_id, item.get("changes"))
        if call:
            call.events.add(key)
        if item.get("status") != "completed":
            if call:
                call.failed_event = True
            self.count["failed_patch_events"] += 1
            return
        self.count["successful_patch_events"] += 1
        context = call.turn if call else self.turns.get(turn_id, Turn("", "", "", None, turn_id))
        if not call:
            self.count["event_without_call"] += 1
        changes = []
        for filename, data in (item.get("changes") or {}).items():
            self.count["event_file_receipts"] += 1
            try:
                change = event_change(filename, data, context.cwd)
            except (ValueError, AttributeError, OSError, RuntimeError):
                self.source.skipped["unsupported_event_file"] += 1
            else:
                self.count["event_file_" + change.operation] += 1
                if change.operation == "update" and not change.patch:
                    self.count["empty_update"] += 1
                changes.append(change)
        if not item.get("changes") and call and call.patch:
            try:
                changes = parse_patch(call.patch, call.turn.cwd)
            except ValueError:
                self.source.skipped["invalid_literal_patch"] += 1
            else:
                self.count["fallback_file_receipts"] += len(changes)
        if call:
            call.event_paths.append({change.path for change in changes})
        self.emit(changes, context, call.id if call else key, key, ts)

    def start_call(self, p):
        name, key = p.get("name"), p.get("call_id")
        if name == "spawn_agent" and key:
            self.spawn_calls[key] = self.turn.prompt
        if name in ("exec_command", "write_stdin") and key:
            raw_args = p.get("arguments", {})
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            self.shell_inputs[key] = (
                (self.turn, [(name, args)]) if isinstance(args, dict) else (self.turn, [])
            )
        elif name == "exec" and key:
            code = p.get("input", "")
            if "exec_command" in code or "write_stdin" in code:
                inputs = shell_calls(code)
                self.shell_inputs[key] = (self.turn, inputs)
                if re.search(r"(?:cat|tee)\s+[^\n]*<<", code) and (
                    not inputs
                    or any(
                        not isinstance(a.get("cmd"), str) for n, a in inputs if n == "exec_command"
                    )
                ):
                    self.count["shell_unparsed_js_candidates"] += 1
        if name not in ("exec", "exec_command", "apply_patch") or not key:
            return
        raw = p.get("input", p.get("arguments", ""))
        turn = self.turn
        command = ""
        patch = None
        shell_args = None
        marker = isinstance(raw, str) and "*** Begin Patch" in raw
        route = name
        self.count["calls_" + name] += 1
        if name == "apply_patch":
            patch = raw
            if (
                p.get("type") == "function_call"
                and isinstance(raw, str)
                and raw.lstrip().startswith("{")
            ):
                args = json.loads(raw)
                patch = args.get("patch", args.get("input"))
        elif name == "exec_command":
            shell_args = json.loads(raw) if isinstance(raw, str) else raw
        elif isinstance(raw, str):
            route = "exec_other"
            if marker:
                self.count["exec_with_marker"] += 1
            if re.search(r"\btools\.apply_patch\s*\(", raw):
                route = "exec_apply_patch"
                if marker:
                    self.count["exec_with_patch_invocation"] += 1
                    patch = wrapper_patch(raw)
            if "exec_command" in raw and not patch:
                shell_args = wrapper_command(raw)
        if shell_args:
            command = shell_args["cmd"]
            cwd = (
                absolute(shell_args["workdir"], turn.cwd) if shell_args.get("workdir") else turn.cwd
            )
            patch, cwd = shell_invocation(command, cwd)
            turn = replace(turn, cwd=cwd)
            if name == "exec" and patch:
                route = "exec_shell_patch"
        if patch:
            self.count["literal_" + name] += 1
        self.calls[key] = Call(
            key, name, turn, patch, command, marker=marker, shell=bool(shell_args), route=route
        )

    def command(self, cmd):
        for category in opaque_categories(cmd):
            self.count["opaque_" + category] += 1

    @staticmethod
    def command_directory(raw, fallback):
        if not raw:
            return fallback
        if raw.startswith("file:"):
            uri = urlsplit(raw)
            if uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
                return ""
            path = unquote(uri.path, errors="strict")
            return path if Path(path).is_absolute() else ""
        if "://" in raw:
            return ""
        return absolute(raw, fallback)

    def command_event(self, item, ts, turn_id):
        key = item.get("id")
        if not key or ("shell", key) in self.seen:
            return
        self.seen.add(("shell", key))
        call = self.owner(key, turn_id)
        if call:
            call.command_events += 1
        cmd = command_string(item.get("command"))
        context = call.turn if call else self.turns.get(turn_id, Turn("", "", "", None, turn_id))
        cwd = self.command_directory(item.get("cwd"), context.cwd)
        outer = call.id if call else key
        inputs = self.shell_inputs.get(outer)
        choices = (
            [a for n, a in inputs[1] if n == "exec_command" and a.get("cmd") == cmd]
            if inputs
            else []
        )
        receipt_cwd = cwd
        if len(choices) == 1 and isinstance(choices[0].get("workdir", ""), str):
            receipt_cwd = self.command_directory(choices[0].get("workdir"), inputs[0].cwd)
        self.native_commands.add((outer, cmd, receipt_cwd))
        origin = item.get("source")
        if (
            origin not in (None, "agent", "unified_exec_startup")
            or item.get("plugin_id")
            or item.get("script_path")
        ):
            self.source.skipped["non_agent_command_event"] += 1
            return
        self.event_commands[outer, cmd, receipt_cwd] += 1
        exit_code = item.get("exit_code")
        success = type(exit_code) is int and exit_code == 0 and item.get("status") == "completed"
        status = "success" if success else "failed"
        static = inputs is None or any(
            a.get("cmd") == cmd for n, a in inputs[1] if n == "exec_command"
        )
        status = shell_status(status, item.get("aggregated_output"))
        tool = call.name if call else "exec_command"
        self.hint_command(cmd, cwd, context, key, ts, status, tool, outer, native=True)
        argv = item.get("command")
        runtime_literal = (
            not static
            and not inputs[1]
            and call is not None
            and call.name == "exec"
            and origin == "unified_exec_startup"
            and status == "success"
            and isinstance(argv, list)
            and len(argv) == 3
            and isinstance(argv[0], str)
            and Path(argv[0]).parent in (Path("/bin"), Path("/usr/bin"), Path("/opt/homebrew/bin"))
            and Path(argv[0]).name in {"bash", "sh", "zsh"}
            and argv[1] in ("-c", "-lc")
            and ("<<" in cmd or ECHO_WRITE.fullmatch(cmd))
            and isinstance(item.get("cwd"), str)
            and item["cwd"].startswith(("/", "file:"))
            and cwd
        )
        if runtime_literal:
            # A completed native record supplies the actual command after
            # JavaScript evaluation. Wait until we know how many it launched.
            self.runtime_shell[outer] = (self.seq, cmd, cwd, context, key, ts)
        else:
            self.s.edits.extend(
                self.write_command(
                    cmd,
                    cwd,
                    context,
                    key,
                    ts,
                    status if static else "js_expression",
                    tool,
                )
            )
        patch, cwd = shell_invocation(cmd, cwd)
        if not patch:
            self.command(cmd)
            return
        self.count["shell_patch_events"] += 1
        # Prefer FileChange if the execution also emitted one. Settle this
        # shell receipt at outer call completion, after all nested items.
        if not success:
            self.count["shell_patch_failed"] += 1
        if call:
            self.shell_receipts.setdefault(call.id, []).append((key, patch, cwd, success, ts))
        elif success:
            self.emit_literal(key, key, context, patch, cwd, ts)

    def emit_literal(self, key, event, context, patch, cwd, ts):
        try:
            changes = parse_patch(patch, cwd)
        except (ValueError, OSError, RuntimeError):
            self.source.skipped["invalid_literal_patch"] += 1
            return
        self.count["fallback_file_receipts"] += len(changes)
        self.emit(changes, context, key, event, ts)

    def write_command(self, cmd, cwd, turn, key, ts, status, tool, *, standalone=False):
        edits = shell_edits(
            cmd,
            cwd,
            status,
            self.count,
            session=self.s.id,
            key=key,
            seq=self.seq,
            ts=ts,
            prompt=turn.prompt,
            model=turn.model,
            effort=turn.effort,
            tool=tool,
            standalone=standalone,
        )
        return edits

    def hint_command(self, cmd, cwd, turn, key, ts, status, tool, outer, *, native=False):
        if status != "success" or read_only(cmd) or other_agent(cmd) or git_content(cmd):
            return
        hint = Hint(
            f"{self.s.id}:hint:{key}", self.seq, ts, tool, cwd, cmd,
            turn.prompt, turn.model, turn.effort,
        )
        self.hint_candidates.append((hint, key, outer, native))

    def finish_hints(self):
        proven = {e.call_id for e in self.s.edits}
        for hint, key, outer, native in self.hint_candidates:
            if not native and (outer, hint.command, hint.cwd) in self.native_commands:
                continue
            if key in proven or (
                outer in proven
                and (hint.tool == "exec_command" or shell_invocation(hint.command, hint.cwd)[0])
            ):
                continue
            self.s.hints.append(hint)
        self.count["write_hints"] += len(self.s.hints)

    def shell_result(self, key, value, ts, wrapped):
        info = self.shell_inputs.get(key)
        if not info:
            return
        turn, inputs = info
        text = payload_text(value)
        if wrapped:
            raw = text.partition("\nOutput:\n")[2] if text.startswith("Script completed") else ""
        else:
            raw = value if isinstance(value, dict) else text
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data = None
        results = data if isinstance(data, list) else [data]
        if len(results) != len(inputs):
            results = [None] * len(inputs)
        resolved = []
        writers = defaultdict(set)
        for ix, ((name, args), result) in enumerate(zip(inputs, results)):
            if isinstance(result, dict) and result.get("status") in ("fulfilled", "rejected"):
                result = (
                    result.get("value") if result["status"] == "fulfilled" else {"exit_code": 1}
                )
            result = result if isinstance(result, dict) else {}
            cmd = args.get("cmd")
            if name == "exec_command" and isinstance(cmd, str):
                if ("workdir" in args and not isinstance(args["workdir"], str)) or (
                    "shell" in args
                    and Path(args.get("shell") or "").name not in {"zsh", "bash", "sh"}
                ):
                    self.count["shell_rejected_unknown_cwd"] += int("<<" in cmd)
                    continue
                cwd = absolute(args["workdir"], turn.cwd) if args.get("workdir") else turn.cwd
                if self.event_commands[key, cmd, cwd]:
                    self.event_commands[key, cmd, cwd] -= 1
                    continue
                origin = (cmd, cwd, turn, f"{key}:{ix}", "exec" if wrapped else "exec_command")
            elif name == "write_stdin":
                origin = self.processes.pop(args.get("session_id"), None)
                if not origin:
                    continue
            else:
                continue
            code = result.get("exit_code", (result.get("metadata") or {}).get("exit_code"))
            status = "unconfirmed"
            if type(code) is int:
                status = "success" if code == 0 else "failed"
            elif result.get("session_id") is not None:
                self.processes[result["session_id"]] = origin
                continue
            elif not wrapped:
                header = re.split(r"(?im)^(?:final )?output:\s*$", text, maxsplit=1)[0]
                receipt = re.search(r"(?m)^Process exited with code (\d+)\s*$", header)
                if receipt:
                    status = "success" if receipt[1] == "0" else "failed"
            status = shell_status(status, result.get("output", text))
            cmd, cwd, context, origin_key, tool = origin
            outer = origin_key.rsplit(":", 1)[0]
            self.hint_command(cmd, cwd, context, origin_key, ts, status, tool, outer)
            for edit in self.write_command(*origin[:4], ts, status, origin[4]):
                resolved.append(edit)
                writers[edit.path].add(edit.call_id)
        for edit in resolved:
            if len(writers[edit.path]) > 1:
                # Array order does not establish the order of concurrent writes.
                self.count["shell_" + edit.operation] -= 1
                self.count["shell_rejected_parallel_order"] += 1
            else:
                self.s.edits.append(edit)

    def finish_call(self, p, ts):
        key = p.get("call_id")
        if key in self.spawn_calls:
            try:
                response = json.loads(payload_text(p.get("output")))
                for field in ("agent_id", "task_name"):
                    if isinstance(response.get(field), str):
                        self.launches.append((field, response[field], self.spawn_calls[key]))
            except (ValueError, AttributeError):
                pass
        if key in self.shell_inputs and key not in self.finished_shell_calls:
            self.finished_shell_calls.add(key)
            _turn, items = self.shell_inputs[key]
            if any(
                name == "write_stdin"
                or isinstance(cmd := args.get("cmd"), str)
                and ("<<" in cmd or ECHO_WRITE.fullmatch(cmd))
                for name, args in items
            ):
                self.shell_results.append(
                    (
                        key,
                        p.get("output"),
                        ts,
                        self.seq,
                        key in self.calls and self.calls[key].name == "exec",
                    )
                )
            else:
                self.shell_result(
                    key, p.get("output"), ts, key in self.calls and self.calls[key].name == "exec"
                )
        call = self.calls.pop(key, None)
        if not call or not (
            call.patch or call.events or call.command or call.command_events or call.marker
        ):
            return
        # Some rollouts serialize the tool output before the completed event.
        # Defer fallback until both streams have been consumed.
        if key in self.closed:
            previous = self.outputs[key][0], self.closed[key].turn.turn_id
            bucket = self.closed_at[previous]
            del bucket[key]
            if not bucket:
                del self.closed_at[previous]
        self.closed[key] = call
        self.outputs[key] = (
            self.seq,
            ts,
            output_status(p.get("output"), "exec_command" if call.shell else call.name),
        )
        self.closed_at[self.seq, call.turn.turn_id][key] = call

    def settle(self, call, ts, status):
        if call.command and not call.command_events and not call.patch:
            self.command(call.command)
        shells = self.shell_receipts.pop(call.id, [])
        if call.events or call.marker or call.patch or shells:
            outcome = status
            if call.events:
                outcome = "success" if call.event_paths else "failed"
            elif shells:
                outcome = "success" if any(ok for _, _, _, ok, _ in shells) else "failed"
            elif not call.patch and status == "success":
                outcome = "marker_only"
            self.count["route_" + call.route + "_" + outcome] += 1
        if call.events:
            self.count["calls_with_patch_event_" + call.name] += 1
        if shells:
            for key, patch, cwd, ok, when in shells:
                if ok:
                    workdir = cwd or call.turn.cwd
                    try:
                        paths = {change.path for change in parse_patch(patch, workdir)}
                    except ValueError:
                        self.source.skipped["invalid_literal_patch"] += 1
                        continue
                    if paths in call.event_paths:
                        call.event_paths.remove(paths)
                    elif not call.failed_event:
                        self.emit_literal(call.id, key, call.turn, patch, workdir, when)
            return
        if call.events:
            return
        if call.patch:
            if status == "success":
                self.count["successful_literal_" + call.name] += 1
                self.emit_literal(call.id, call.id, call.turn, call.patch, call.turn.cwd, ts)
            else:
                self.count[status + "_literal_" + call.name] += 1

        elif call.marker:
            self.count[status + "_nonliteral_marker"] += 1

    def finish(self):
        for outer, (seq, cmd, cwd, turn, key, ts) in self.runtime_shell.items():
            call = self.calls.get(outer) or self.closed.get(outer)
            if call is None or call.command_events != 1 or call.events:
                self.count["shell_rejected_runtime_ambiguous"] += 1
                continue
            self.seq = seq
            edits = self.write_command(cmd, cwd, turn, key, ts, "success", "exec", standalone=True)
            self.count["runtime_literal_edits"] += len(edits)
            self.s.edits.extend(edits)
        for key, value, ts, seq, wrapped in self.shell_results:
            self.seq = seq
            self.shell_result(key, value, ts, wrapped)
        for key in self.shell_inputs.keys() - self.finished_shell_calls:
            self.shell_result(key, None, self.s.ended_at, False)
        for cmd, cwd, turn, key, tool in self.processes.values():
            self.write_command(cmd, cwd, turn, key, self.s.ended_at, "unconfirmed", tool)
        if self.patch_starts:
            self.source.skipped["missing_patch_end"] += len(self.patch_starts)
        for key, call in self.closed.items():
            self.seq, ts, status = self.outputs[key]
            self.settle(call, ts, status)
        for call in self.calls.values():
            self.settle(call, self.s.ended_at, "missing_receipt")
        self.finish_hints()
        # The protocol's user event disambiguates human input from injected
        # response items. An unmatched response in that turn is not a prompt.
        rejected = {
            key
            for key, (turn, native) in self.prompt_info.items()
            if turn in self.native_turns and not native
        }
        previous, redirects = {}, {}
        for prompt in self.s.prompts:
            tid = self.prompt_info[prompt.id][0]
            if prompt.id in rejected:
                redirects[prompt.id] = previous.get(tid)
            else:
                previous[tid] = prompt.id
        self.s.prompts = [p for p in self.s.prompts if p.id not in rejected]
        for record in (*self.s.edits, *self.s.hints):
            record.prompt = redirects.get(record.prompt, record.prompt)
        self.launches = [(f, v, redirects.get(p, p)) for f, v, p in self.launches]
        self.s.edits.sort(key=lambda e: (e.ts, e.seq))
        if not self.s.title and self.s.prompts:
            self.s.title = " ".join(self.s.prompts[0].text.split())[:80]

    def begin_turn(self, tid, root):
        if tid in self.turns:
            return self.turns[tid]
        prompt = None
        if root in self.turns:
            prompt = self.turns[root].prompt
        if tid and not self.turn.turn_id and self.turn.prompt:
            # Older rollouts can place the initial human message before the
            # first turn_context. Adopt it once, into that first named turn.
            prompt = self.turn.prompt
            _, native = self.prompt_info[prompt]
            self.prompt_info[prompt] = (tid, native)
            text = self.s.prompts[-1].text
            self.prompt_by_text[tid, text] = (prompt, native)
            if native:
                self.native_turns.add(tid)
        return Turn("", "", self.s.cwd, prompt, tid)

    def record(self, kind, p, ts):
        name = p.get("type")
        tid = p.get("turn_id", self.turn.turn_id)
        if kind == "turn_context":
            prev = self.begin_turn(tid, p.get("root_turn_id"))
            self.turn = Turn(
                p.get("model") or "",
                p.get("effort") or "",
                absolute(p.get("cwd") or self.s.cwd, self.s.cwd),
                prev.prompt,
                tid,
            )
            self.turns[tid] = self.turn
            self.s.model = self.turn.model or self.s.model
        elif kind == "event_msg":
            if p.get("thread_id", self.s.id) != self.s.id:
                return
            if name == "item_completed":
                item = p["item"]
                if item["type"] == "FileChange":
                    self.file_event(item, ts, tid)
                elif item["type"] == "CommandExecution":
                    self.command_event(item, ts, tid)
                elif item["type"] == "UserMessage" and item.get("id") not in self.seen:
                    self.seen.add(item.get("id"))
                    self.count["user_items"] += 1
                    self.prompt(payload_text(item["content"]), ts, tid, True)
            elif name == "task_started":
                origin = p.get("turn_attribution") or {}
                parent = (
                    origin.get("parent_turn_id")
                    or origin.get("root_turn_id")
                    or p.get("root_turn_id")
                )
                self.turn = self.turns[tid] = self.begin_turn(tid, parent)
            elif name == "patch_apply_begin":
                self.count["patch_begin_events"] += 1
                if p.get("call_id"):
                    self.patch_starts[p["call_id"]] = (p.get("changes"), tid)
            elif name == "patch_apply_end":
                self.count["patch_end_events"] += 1
                begin, first_turn = self.patch_starts.pop(p.get("call_id"), (None, tid))
                self.file_event(
                    {
                        "id": p.get("call_id"),
                        "changes": p.get("changes") or begin,
                        "status": "completed" if p.get("success") is True else "failed",
                    },
                    ts,
                    first_turn,
                )
            elif name == "user_message":
                self.count["user_message_events"] += 1
                self.prompt(p.get("message", ""), ts, tid, True)
            elif name == "turn_diff":
                self.count["turn_diff_events"] += 1
            elif name == "thread_name_updated" and isinstance(p.get("thread_name"), str):
                self.s.title = p["thread_name"]
                self.count["title_events"] += 1
        elif kind == "response_item":
            if name in ("function_call", "custom_tool_call"):
                self.start_call(p)
            elif name in ("function_call_output", "custom_tool_call_output"):
                self.finish_call(p, ts)
            elif name == "message" and p.get("role") == "user":
                self.prompt(payload_text(p["content"]), ts, tid)


@dataclass
class RolloutHeader:
    stamp: tuple
    sid: str | None
    parent: str | None
    failures: Counter
    broken: bool


def thread_spawn(payload):
    source = payload.get("source")
    subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
    return subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}


class CodexSource:
    def __init__(self):
        self.counts = Counter()
        self.failures = Counter()
        self.skipped = Counter()
        self.tick = None
        self.broken = set()
        self.check = None

    def raw_lines(self, path):
        if self.check:
            self.check()
        # Rollouts are append-only. Use the same byte boundary in the metadata
        # pass, the parsing pass and the independent receipt audit. A compressed
        # rollout is finished, so it is read to the end, and progress counts the
        # compressed bytes the decoder has consumed.
        packed = compressed(path)
        unreported = reported = 0
        with closing(self._decoded_lines(path, packed)) as lines:
            for stream, line in lines:
                if self.check or self.tick:
                    unreported += len(line)
                    if unreported >= READ_CHUNK:
                        if self.check:
                            self.check()
                        if self.tick:
                            if not packed:
                                self.tick(path, unreported)
                            elif (done := compressed_read(stream)) is not None and done > reported:
                                self.tick(path, done - reported)
                                reported = done
                        unreported = 0
                yield line

    def _decoded_lines(self, path, packed):
        # Keep cancellation and progress callbacks outside decoder error handling.
        remaining = -1 if packed else self.log_sizes[path]
        try:
            with open_log(path) as stream:
                while remaining and (line := stream.readline(remaining)):
                    remaining -= len(line) if remaining > 0 else 0
                    yield stream, line
        except ZstdUnavailable:
            self.failures["compressed_rollout_without_zstd"] += 1
        except Exception as error:  # OSError, or a decoder's own error type
            if not packed and not isinstance(error, OSError):
                raise
            self.failures["unreadable_rollout"] += 1
            # What was read before the failure is a prefix, not the session.
            self.broken.add(path)

    def records(self, path):
        for line in self.raw_lines(path):
            try:
                r = json.loads(line)
                if not isinstance(r.get("payload"), dict):
                    raise TypeError
                yield r
            except (TypeError, ValueError, AttributeError):
                self.failures["malformed_record"] += 1

    def merged(self, paths):
        if len(paths) == 1:
            yield from self.records(paths[0])
            return
        # Replicas may be incomplete or divergent. Keep both suffixes, dropping
        # the common records without exposing their contents in diagnostics.
        unique = {}
        for path in paths:
            for record in self.records(path):
                fields = [record.get(k) for k in ("timestamp", "type", "payload")]
                key = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).digest()
                if key in unique:
                    self.counts["replicated_records"] += 1
                else:
                    unique[key] = record
        yield from sorted(unique.values(), key=lambda r: r.get("timestamp", ""))

    def log_files(self, root):
        root = Path(root).expanduser().resolve()
        return sorted([*root.rglob("rollout-*.jsonl"), *root.rglob("rollout-*.jsonl.zst")])

    def headers(self, paths, cache=None, check=None):
        headers = {}
        packed_cache = cache and cache.directory and any(map(compressed, paths)) and zstd_available()
        for path in paths:
            snapshot = stamp(path)
            if snapshot is None:
                if check:
                    check()
                headers[path] = None
                continue
            key = None
            if cache and (packed_cache or not compressed(path)):
                key = cache.entry("codex", [path], kind="header", stamps=(snapshot,))
            header = cache.get(key) if key else None
            if header is None:
                unit = CodexSource()
                unit.check = check
                unit.log_sizes = {path: snapshot[1]}
                sid, parent = None, None
                for record in unit.records(path):
                    payload = record["payload"]
                    if record.get("type") == "session_meta" and isinstance(payload.get("id"), str):
                        sid = payload["id"]
                        spawn = thread_spawn(payload)
                        value = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
                        parent = value if isinstance(value, str) else None
                        break
                header = RolloutHeader(snapshot, sid, parent, unit.failures, path in unit.broken)
                if key and not header.broken and not unit.failures["compressed_rollout_without_zstd"]:
                    if snapshot == stamp(path):
                        cache.put(key, header)
            elif check:
                check()
            headers[path] = header
        return headers

    @staticmethod
    def groups(headers):
        groups = defaultdict(list)
        for path, header in headers.items():
            if header and header.sid is not None:
                groups[header.sid].append(path)
        for sid, paths in groups.items():
            paths.sort(key=lambda p: (not log_stem(p).endswith(sid), compressed(p)))
        return groups

    def cache_entry(self, cache, paths, headers):
        return cache.entry("codex", paths, stamps=tuple(headers[path].stamp for path in paths))

    def parse_group(self, sid, paths):
        child_path = None
        run = None
        for seq, r in enumerate(self.merged(paths)):
            try:
                p, kind = r["payload"], r["type"]
                ts = datetime.fromisoformat(r["timestamp"]).astimezone(UTC).isoformat()
                if run is None:
                    if kind != "session_meta" or p.get("id") != sid:
                        continue
                    cwd = absolute(p["cwd"], str(paths[0].parent))
                    spawn = thread_spawn(p)
                    parent = spawn.get("parent_thread_id")
                    s = Session(
                        sid,
                        "codex",
                        str(paths[0]),
                        cwd=cwd,
                        branch=(p.get("git") or {}).get("branch", ""),
                        started_at=ts,
                        parent=parent,
                        resume="codex resume " + shlex.quote(sid),
                        resume_cwd=cwd,
                    )
                    if parent:
                        child_path = spawn.get("agent_path")
                    run = Rollout(s, self)
                    self.counts["child_sessions"] += int(bool(parent))
                elif kind == "session_meta" or ts < run.s.started_at:
                    self.counts["inherited_records"] += 1
                    continue
                run.seq = seq
                run.s.ended_at = ts
                run.record(kind, p, ts)
            except (
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
                OSError,
                RuntimeError,
            ):
                self.failures["malformed_record"] += 1
        if run and any(path in self.broken for path in paths):
            self.skipped["partial_rollout"] += 1
            run = None
        if run:
            run.finish()
        return run, child_path

    def sessions(self, root, progress=None, *, headers=None, cache=None, check=None):
        batch, child_paths, launches = {}, {}, defaultdict(list)
        if headers is None:
            headers = self.headers(self.log_files(root), cache, check)
        self.log_sizes = {}
        self.broken = set()
        for path, header in headers.items():
            if header is None:
                self.failures["unreadable_rollout"] += 1
                continue
            self.log_sizes[path] = header.stamp[1]
            self.counts["log_files"] += 1
            self.failures.update(header.failures)
            if header.broken:
                self.broken.add(path)
            if header.sid is None:
                self.skipped["missing_session_meta"] += 1
                if progress:
                    progress(path)
        for sid, paths in self.groups(headers).items():
            if check:
                check()
            self.counts["replica_files"] += len(paths) - 1
            key = self.cache_entry(cache, paths, headers) if cache else None
            cached = cached_parse(cache, key)
            if cached is None:
                unit = CodexSource()
                unit.log_sizes = self.log_sizes
                unit.broken.update(self.broken.intersection(paths))
                unit.tick, unit.check = progress, check
                with observe_paths(key) as observed:
                    run, child_path = unit.parse_group(sid, paths)
                cached = (
                    run.s if run else None, run.launches if run else (), child_path,
                    unit.counts, unit.skipped, unit.failures, unit.broken,
                )
                if key and not unit.broken and not unit.failures["compressed_rollout_without_zstd"]:
                    if all(stamp(path) == headers[path].stamp for path in paths):
                        cache.put(key, (cached, observed))
            session, unit_launches, child_path, counts, skipped, failures, broken = cached
            self.counts.update(counts)
            self.skipped.update(skipped)
            self.failures.update(failures)
            self.broken.update(broken)
            if session:
                batch[sid] = session
                child_paths[sid] = child_path
                for field, value, prompt in unit_launches:
                    launches[sid, field, value].append(prompt)
            if progress:
                for path in paths:
                    progress(path)
        path_counts = Counter((s.parent, child_paths.get(s.id)) for s in batch.values() if s.parent)
        for s in batch.values():
            node, seen, prompt = s, set(), None
            missing_reason = "launch_without_human_prompt"
            while node.parent in batch and node.id not in seen and prompt is None:
                seen.add(node.id)
                matches = launches[node.parent, "agent_id", node.id]
                if not matches and path_counts[node.parent, child_paths.get(node.id)] == 1:
                    matches = launches[node.parent, "task_name", child_paths.get(node.id)]
                if len(matches) != 1:
                    missing_reason = "launch_unmatched_or_ambiguous"
                    break
                prompt = matches[0]
                node = batch[node.parent]
            if prompt:
                for edit in s.edits:
                    if not edit.prompt:
                        edit.prompt = prompt
                        self.counts["inherited_prompt_edits"] += 1
                for hint in s.hints:
                    hint.prompt = hint.prompt or prompt
            if s.parent and not prompt:
                self.counts["child_missing_" + missing_reason] += sum(
                    e.prompt is None for e in s.edits
                )
            yield s
