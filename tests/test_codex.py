"""Invented rollouts exercising paired receipts, file lifetimes and shared graph paths."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from papertrail.cli import main
from papertrail.graph import build
from papertrail.queries import Queries
from papertrail.replay import histories
from papertrail.sources import CodexSource
from papertrail.sources.codex_patch import parse_patch, shell_patch

FIXTURES = Path(__file__).parent / "fixtures"


def call(key, patch, name="apply_patch"):
    return "response_item", {
        "type": "custom_tool_call",
        "name": name,
        "call_id": key,
        "input": patch,
    }


def output(key, value="Success. Updated the following files:"):
    return "response_item", {
        "type": "custom_tool_call_output",
        "call_id": key,
        "output": value,
    }


def completed(key, **attrs):
    return "event_msg", {
        "type": "item_completed",
        "turn_id": "first",
        "item": dict(id=key, **attrs),
    }


def rollout(root, sid, records, parent=None):
    root.mkdir(exist_ok=True)
    src = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
    records = [
        (
            "session_meta",
            {"id": sid, "cwd": str(root), "source": src, "git": {"branch": "fiction"}},
        ),
        (
            "turn_context",
            {"turn_id": "first", "model": "alpha", "effort": "high", "cwd": str(root)},
        ),
        (
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"text": "# AGENTS.md instructions\n<INSTRUCTIONS>"}],
            },
        ),
        completed(
            "human",
            type="UserMessage",
            content=[{"type": "text", "text": "Revise the example."}],
        ),
        *records,
    ]
    path = root / f"rollout-demo-{sid}.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "timestamp": f"2025-06-01T12:00:{i:02}Z",
                    "ordinal": i,
                    "type": t,
                    "payload": p,
                }
            )
            + "\n"
            for i, (t, p) in enumerate(records)
        )
    )
    return path


def patch(*body):
    return "\n".join(("*** Begin Patch", *body, "*** End Patch"))


def test_mixed_demo(tmp_path, capsys):
    f = FIXTURES / "project/greeting.py"
    store = tmp_path / "demo.rl"
    assert (
        main(
            [
                "ingest",
                "--claude-logs",
                str(FIXTURES / "logs"),
                "--codex-logs",
                str(FIXTURES / "codex-sessions"),
                "--project",
                str(f.parent),
                "--store",
                str(store),
                "--json",
            ]
        )
        == 0
    )
    stats = json.loads(capsys.readouterr().out)["rows"][0]
    assert stats["by_agent"]["codex"] == {
        "sessions": 1,
        "edits": 1,
        "lines": 0,
        "unplaced": 0,
    }
    with Queries(store) as q:
        trace = q.trace(f, 2)["rows"][:3]
        assert [(r["agent"], r["model"], r["effort"], r["resume"]) for r in trace] == [
            ("claude-code", "demo-model", "max", "claude --resume cedar"),
            ("codex", "demo-codex", "medium", "codex resume maple"),
            ("claude-code", "demo-model", "high", "claude --resume birch"),
        ]
        assert trace[1]["prompt"] == "Try a sunrise greeting."
        assert q.crossings(f)["rows"][0]["agent_count"] == 2
        assert q.session("maple")["rows"][-1]["path"] == str(f)


def test_move_delete_recreate_and_eof(tmp_path):
    p = patch(
        "*** Add File: old",
        "+keep",
        "+repeat",
        "+repeat",
        "*** Add File: other",
        "+only",
    )
    m = patch(
        "*** Update File: old",
        "*** Move to: new",
        "@@",
        "-repeat",
        "+end",
        "*** End of File",
    )
    d = patch("*** Delete File: new")
    a = patch("*** Add File: new", "+reborn")
    rollout(tmp_path, "origin", [call("create", p), output("create")])
    rollout(
        tmp_path,
        "child",
        [("event_msg", {"type": "ignored"})] * 2 + [call("move", m), output("move")],
        "origin",
    )
    (tmp_path / "new").write_text("keep\nrepeat\nend\n")
    source = CodexSource()
    batch = list(source.sessions(tmp_path))
    assert len(batch) == 2
    parent = next(s for s in batch if s.id == "origin")
    assert [e.call_id for e in parent.edits] == ["create", "create"]
    assert len({e.id for e in parent.edits}) == 2
    child = next(s for s in batch if s.parent)
    assert not child.prompts
    assert json.loads(json.dumps(asdict(child)))["edits"][0]["moved_from"].endswith("/old")
    db = tmp_path / "mixed.rl"
    build(db, batch)
    with Queries(db) as q:
        assert q.blame(tmp_path / "new", 1, 1)["rows"][0]["session"] == "origin"
        assert [r["session"] for r in q.trace(tmp_path / "new", 3)["rows"]] == [
            "child",
            "origin",
        ]
        assert (
            q.db.execute("MATCH (:Session)-[:SPAWNED]->(:Session) RETURN count(*) AS n").data()[0][
                "n"
            ]
            == 1
        )
        assert q.crossings(tmp_path / "new")["rows"][0]["start"] == 3
    rollout(
        tmp_path,
        "rebuilder",
        [("event_msg", {"type": "ignored"})] * 6
        + [call("delete", d), output("delete"), call("again", a), output("again")],
    )
    (tmp_path / "new").write_text("reborn\n")
    build(db, list(CodexSource().sessions(tmp_path)))
    with Queries(db) as q:
        assert [r["session"] for r in q.trace(tmp_path / "new", 1)["rows"]] == ["rebuilder"]


def test_receipts_turn_binding_and_duplicates(tmp_path):
    direct = patch("*** Add File: one", "+sample")
    event = completed(
        "confirmed",
        type="FileChange",
        status="completed",
        changes={"one": {"type": "add", "content": "sample\n"}},
    )
    rollout(
        tmp_path,
        "late",
        [
            call("confirmed", direct),
            (
                "turn_context",
                {
                    "turn_id": "second",
                    "cwd": str(tmp_path),
                    "model": "beta",
                    "effort": "low",
                },
            ),
            (
                "event_msg",
                {
                    "type": "user_message",
                    "turn_id": "second",
                    "message": "Start another turn.",
                },
            ),
            output("confirmed"),
            event,
            event,
            call("bad", direct),
            output("bad", "apply_patch verification failed"),
            call("missing", direct),
            call("failed-event", direct),
            completed("failed-event", type="FileChange", status="failed", changes={}),
            output("failed-event"),
        ],
    )
    source = CodexSource()
    (s,) = source.sessions(tmp_path)
    (e,) = s.edits
    assert (e.model, e.effort, e.prompt) == ("alpha", "high", s.prompts[0].id)
    assert source.counts["duplicate_events"] == 1
    assert source.counts["missing_receipt_literal_apply_patch"] == 1
    assert source.counts["failed_literal_apply_patch"] == 1
    assert source.counts["failed_patch_events"] == 1


def test_shell_workdir_nested_and_opaque(tmp_path):
    body = patch("*** Add File: a", "+yes")
    cmd = "apply_patch <<'PATCH'\n" + body + "\nPATCH"
    shell = (
        "response_item",
        {
            "type": "function_call",
            "call_id": "sh",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": "one"}),
        },
    )

    def nested(key, dir):
        args = json.dumps({"cmd": cmd, "workdir": dir})
        return call(key, "text(await tools.exec_command(" + args + "))", "exec")

    opaque = "sed -i 's/a/b/' a > saved"
    rollout(
        tmp_path,
        "shell",
        [
            shell,
            output("sh", {"exit_code": 0}),
            nested("wrapper", "two"),
            output("wrapper", 'Script completed\nOutput:\n{"exit_code":0}'),
            nested("unproven", "three"),
            output("unproven", "Script completed\nOutput:\n{}"),
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "opaque",
                    "arguments": json.dumps({"cmd": opaque}),
                },
            ),
            output("opaque", {"exit_code": 0}),
            call(
                "example",
                "if (false) await tools.apply_patch(" + json.dumps(body) + ")",
                "exec",
            ),
            output("example", "Script completed\nOutput:\n{}"),
        ],
    )
    source = CodexSource()
    (s,) = source.sessions(tmp_path)
    assert [e.path for e in s.edits] == [str(tmp_path / d / "a") for d in ("one", "two")]
    assert source.counts["opaque_redirect"] == source.counts["opaque_sed_inplace"] == 1
    assert shell_patch(cmd + "\ntrue") is None
    assert shell_patch("cat <<'PATCH'\n" + body + "\nPATCH") is None


def test_event_only_and_dynamic_wrapper(tmp_path):
    event = completed(
        "inner",
        type="FileChange",
        status="completed",
        changes={
            "a": {
                "type": "update",
                "unified_diff": "@@ -4 +4 @@\n-old\n+new",
                "move_path": "b",
            },
            "c": {"type": "add", "content": "exact"},
            "d": {"type": "delete", "content": "gone\n"},
        },
    )
    rollout(
        tmp_path,
        "dynamic",
        [
            call("outer", "await tools.apply_patch(getPatch())", "exec"),
            event,
            output("outer", "Script failed after the change"),
            ("event_msg", {"type": "turn_diff", "diff": "opaque"}),
        ],
    )
    source = CodexSource()
    (s,) = source.sessions(tmp_path)
    assert [e.operation for e in s.edits] == ["move", "add", "delete"]
    assert all(e.call_id == "outer" for e in s.edits)
    assert s.edits[0].patch.startswith("@@ -4")
    assert s.edits[1].after == "exact"
    assert source.counts["turn_diff_events"] == 1


def test_ambiguous_context_and_later_position(tmp_path):
    initial = patch("*** Add File: a", "+old", "+old")
    change = patch("*** Update File: a", "@@", "-old", "+new")
    rollout(
        tmp_path,
        "context",
        [call("add", initial), output("add"), call("update", change), output("update")],
    )
    (s,) = CodexSource().sessions(tmp_path)
    path = tmp_path / "a"
    path.write_text("new\nnew\n")
    (history,) = histories([s])
    assert history.unplaced_edits == {s.edits[1].id}
    owners = {h.id: h for h in history.hunks}
    for _, _, owner in history.ranges:
        h = owners[owner]
        assert (h.kind, h.was, h.basis, h.inferred_from) == (
            "inferred", "unknown", "nearby", s.edits[0].id,
        )
    path.write_text("old\nnew\n")
    (history,) = histories([s])
    assert not history.unplaced_edits
    assert [
        next(h.edit for h in history.hunks if h.id == owner) for _, _, owner in history.ranges
    ] == [e.id for e in s.edits]


def test_named_context_and_multiple_hunks(tmp_path):
    body = patch(
        "*** Update File: a",
        "@@ group",
        "-same",
        "+first",
        "@@ next",
        "-same",
        "+second",
        "*** End of File",
    )
    rollout(tmp_path, "scope", [call("u", body), output("u")])
    path = tmp_path / "a"
    path.write_text("same\ngroup\nfirst\nnext\nsecond\n")
    (s,) = CodexSource().sessions(tmp_path)
    (f,) = histories([s])
    agent_ids = {h.id for h in f.hunks if h.edit == s.edits[0].id}
    assert [(a, b) for a, b, h in f.ranges if h in agent_ids] == [(3, 3), (5, 5)]
    with pytest.raises(ValueError):
        parse_patch(patch("*** Add File: a", "bad"), str(tmp_path))


def test_missing_source_and_codex_only(tmp_path, capsys):
    args = ["ingest", "--all", "--store", str(tmp_path / "missing.rl"), "--json"]
    assert (
        main(
            [
                *args,
                "--claude-logs",
                str(tmp_path / "absent"),
                "--codex-logs",
                str(FIXTURES / "codex-sessions"),
            ]
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["sources"]["claude-code"] == {"missing_directory": 1}
    assert row["agents"] == row["sessions"] == 1
    assert main([*args, "--source", "codex", "--logs", str(FIXTURES / "codex-sessions")]) == 0
    assert list(json.loads(capsys.readouterr().out)["rows"][0]["sources"]) == ["codex"]


def test_replica_suffixes_and_injected_user_text(tmp_path):
    primary = rollout(
        tmp_path,
        "same",
        [
            (
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": "Unmatched system context"}],
                },
            ),
            call("first", patch("*** Add File: f", "+one")),
            output("first"),
        ],
    )
    replica = tmp_path / "rollout-copy.jsonl"
    replica.write_text(
        primary.read_text()
        + json.dumps(
            {
                "timestamp": "2025-06-01T13:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "second",
                    "turn_id": "first",
                    "success": True,
                    "changes": {
                        "f": {
                            "type": "update",
                            "unified_diff": "@@ -1 +1 @@\n-one\n+two",
                        }
                    },
                },
            }
        )
        + "\n"
    )
    src = CodexSource()
    (s,) = src.sessions(tmp_path)
    assert [p.text for p in s.prompts] == ["Revise the example."]
    assert [e.call_id for e in s.edits] == ["first", "second"]
    assert src.counts["replica_files"] == 1
    assert src.counts["replicated_records"] > 0


def test_move_carries_crossing_to_new_project(tmp_path):
    from papertrail.graph import select

    old = tmp_path / "old"
    new = tmp_path / "sub/new"
    new.parent.mkdir()
    new.write_text("two\n")
    a = patch("*** Add File: old", "+one")
    u = patch("*** Update File: old", "@@", "-one", "+two")
    m = patch("*** Update File: old", "*** Move to: sub/new")

    def after(n):
        return [("event_msg", {"type": "ignored"})] * n

    rollout(tmp_path, "a", [call("a", a), output("a")])
    rollout(tmp_path, "b", after(2) + [call("u", u), output("u")])
    rollout(tmp_path, "c", after(4) + [call("m", m), output("m")])
    selected = select(list(CodexSource().sessions(tmp_path)), new.parent)
    assert len(selected) == 3
    store = tmp_path / "moved.rl"
    build(store, selected)
    with Queries(store) as q:
        assert [r["session"] for r in q.trace(new, 1)["rows"]] == ["b", "a"]
        assert q.crossings(old)["rows"] == []
        assert q.crossings(new)["rows"][0]["start"] == 1


def test_event_and_shell_in_same_wrapper(tmp_path):
    body = patch("*** Add File: second", "+b")
    command = "apply_patch <<'PATCH'\n" + body + "\nPATCH"
    shell_event = completed(
        "shell-item",
        type="CommandExecution",
        cwd=str(tmp_path),
        command=["/bin/sh", "-c", command],
        status="completed",
        exit_code=0,
    )
    rollout(
        tmp_path,
        "several",
        [
            call("outer", "await doSeveralThings()", "exec"),
            completed(
                "first-item",
                type="FileChange",
                status="completed",
                changes={"first": {"type": "add", "content": "a\n"}},
            ),
            shell_event,
            output("outer", "Script completed\nOutput:\n{}"),
        ],
    )
    (s,) = CodexSource().sessions(tmp_path)
    assert {Path(e.path).name for e in s.edits} == {"first", "second"}
    assert all(e.call_id == "outer" for e in s.edits)


def test_legacy_begin_end_and_terminal_receipt(tmp_path):
    begin = (
        "event_msg",
        {
            "type": "patch_apply_begin",
            "call_id": "legacy",
            "changes": {"old": {"type": "delete"}},
        },
    )
    end = (
        "event_msg",
        {"type": "patch_apply_end", "call_id": "legacy", "success": True},
    )
    direct = call("new", patch("*** Add File: created", "+ok"))
    terminal = (
        "event_msg",
        {"type": "patch_apply_end", "call_id": "new", "success": True},
    )
    rollout(tmp_path, "legacy", [begin, end, direct, terminal])
    src = CodexSource()
    (s,) = src.sessions(tmp_path)
    assert [e.operation for e in s.edits] == ["delete", "add"]
    assert src.counts["event_file_receipts"] + src.counts["fallback_file_receipts"] == len(s.edits)


def test_late_nested_event_and_shell_cd(tmp_path):
    body = patch("*** Add File: a", "+ok")
    native = completed(
        "nested",
        type="FileChange",
        status="completed",
        changes={"a": {"type": "add", "content": "ok\n"}},
    )
    shell = "cd 'some dir' && apply_patch <<'END'\n" + body + "\nEND"
    rollout(
        tmp_path,
        "different-ids",
        [
            call("first", body),
            output("first"),
            native,
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "shell",
                    "arguments": json.dumps({"cmd": shell}),
                },
            ),
            output("shell", {"exit_code": 0}),
        ],
    )
    (s,) = CodexSource().sessions(tmp_path)
    assert [e.call_id for e in s.edits] == ["first", "shell"]
    assert [e.path for e in s.edits] == [str(tmp_path / "a"), str(tmp_path / "some dir/a")]


def test_initial_user_before_named_turn(tmp_path):
    path = rollout(tmp_path, "older", [call("a", patch("*** Add File: x", "+x")), output("a")])
    rows = [json.loads(r) for r in path.read_text().splitlines()]
    rows[3]["type"] = "response_item"
    rows[3]["payload"] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Create it."}],
    }
    rows = [rows[0], rows[3], rows[1], *rows[4:]]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (s,) = CodexSource().sessions(tmp_path)
    assert s.edits[0].prompt == s.prompts[0].id
