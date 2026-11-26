"""Literal shell evidence and cross-source history in invented transcripts."""

import json
from collections import Counter
from pathlib import Path

import pytest
from test_codex import call, completed, output, patch, rollout

from papertrail.graph import build
from papertrail.queries import Queries
from papertrail.replay import histories
from papertrail.sources import ClaudeCodeSource, CodexSource
from papertrail.sources.shell_write import shell_edits


def parse(cmd, cwd, status="success"):
    counts = Counter()
    edits = shell_edits(
        cmd,
        str(cwd),
        status,
        counts,
        session="fiction",
        key="k",
        seq=1,
        ts="2025-06-01T01:00:00+00:00",
        prompt=None,
        model="demo",
        effort="high",
        tool="Bash",
    )
    return edits, counts


@pytest.mark.parametrize(
    ("header", "op"),
    [
        ("cat > one <<'END'", "overwrite"),
        ('cat <<"END" > one', "overwrite"),
        ("cat >> one <<'END'", "append"),
        ("tee one <<END", "overwrite"),
        ("tee -a one <<-END", "append"),
    ],
)
def test_forms(tmp_path, header, op):
    (e,), _ = parse(header + "\n\tx\n" + ("\t" if "<<-" in header else "") + "END", tmp_path)
    assert e.operation == op and e.path == str(tmp_path / "one")
    assert (e.appended or e.after) == ("x\n" if "<<-" in header else "\tx\n")
    assert e.before is None


@pytest.mark.parametrize(
    ("command", "operation", "value"),
    [
        ("echo codex > one", "overwrite", "codex\n"),
        ("echo 'literal $HOME' >> one", "append", "literal $HOME\n"),
        ('echo "" > one', "overwrite", "\n"),
    ],
)
def test_single_literal_echo(tmp_path, command, operation, value):
    (edit,), _ = parse(command, tmp_path)
    assert edit.path == str(tmp_path / "one")
    assert edit.operation == operation
    assert (edit.after or edit.appended) == value


@pytest.mark.parametrize(
    "command",
    [
        "echo '-n' > one",
        'echo "$HOME" > one',
        r"echo 'a\nb' > one",
        "echo value | tee one",
        "echo value > one; true",
        "echo value > $target",
    ],
)
def test_echo_without_literal_write_evidence(tmp_path, command):
    assert not parse(command, tmp_path)[0]


@pytest.mark.parametrize(
    ("cmd", "reason"),
    [
        ("cat > one <<D\n$HOME\nD", "body_expansion"),
        ("tee a <<D\n`cmd`\nD", "body_expansion"),
        ("cat > $one <<'D'\nx\nD", "dynamic_path"),
        ("cd $DIR\ncat > p <<D\nx\nD", "unknown_cwd"),
        ("cat > f <<'D'\nx", "unclosed_heredoc"),
        ("if false; then\ncat > x <<D\nx\nD\nfi", "shell_context"),
    ],
)
def test_rejections(tmp_path, cmd, reason):
    edits, counts = parse(cmd, tmp_path)
    assert not edits and counts["shell_rejected_" + reason] == 1


def test_order_and_script_body(tmp_path):
    cmd = "cd 'folder' && cat > x <<'D'\n$d\nD\npython - <<'PY'\ncat > fake <<T\nx\nT\nPY\ncat >> x <<D\nmore\nD"
    edits, _ = parse(cmd, tmp_path)
    assert [(e.path, e.operation) for e in edits] == [
        (str(tmp_path / "folder/x"), s) for s in ("overwrite", "append")
    ]
    assert edits[0].after == "$d\n"
    assert not parse(cmd, tmp_path, "failed")[0]


def test_parallel_and_async(tmp_path):
    a = r"tools.exec_command({cmd: `cat > a <<'D'\n\\path\nD`})"
    b = r"tools.exec_command({cmd: `cat > b <<'D'\nfailed\nD`})"
    c = r"tools.exec_command({cmd: `cat > c <<'D'\n${value}\nD`})"

    def wrapper(key, js, payload):
        return [
            call(key, "text(await " + js + ")", "exec"),
            output(key, "Script completed\nOutput:\n" + json.dumps(payload)),
        ]

    rollout(
        tmp_path,
        "parallel",
        [
            *wrapper(
                "first",
                "Promise.all([" + f"{a},{b},{c}" + "])",
                [{"session_id": 7}, {"exit_code": 1}, {"exit_code": 0}],
            ),
            *wrapper("poll", "tools.write_stdin({session_id:7,chars:''})", {"exit_code": 0}),
            *wrapper("ambiguous", "Promise.all([" + f"{a},{b}" + "])", {"exit_code": 0}),
        ],
    )
    src = CodexSource()
    (s,) = src.sessions(tmp_path)
    assert [(Path(e.path).name, e.after) for e in s.edits] == [("a", "\\path\n")]
    assert (s.edits[0].prompt, s.edits[0].effort) == (s.prompts[0].id, "high")


def test_append_unknown_and_partial_last_line(tmp_path):
    from dataclasses import replace

    from papertrail.replay import replay

    path = tmp_path / "one"
    path.write_text("prefix\nmore\n")
    (append,), _ = parse("tee -a one <<D\nmore\nD", tmp_path)
    f = replay(str(path), [append])
    assert [next(h.kind for h in f.hunks if h.id == owner) for _, _, owner in f.ranges] == [
        "unknown",
        "agent",
    ]
    initial = replace(
        append, id="initial", operation="overwrite", after="prefix", appended=None, seq=0
    )
    path.write_text("prefixmore\n")
    f = replay(str(path), [initial, append])
    assert f.hunks[-1].replaced == {f.hunks[0].id: 1}


def test_claude_write_and_codex_child_trace(tmp_path):
    logs = tmp_path / "claude"
    logs.mkdir()
    fake = Path(__file__).parent / "fixtures/logs/demo/cedar.jsonl"
    rows = [json.loads(x) for x in fake.read_text().splitlines()]
    for r in rows:
        r["cwd"] = str(tmp_path)
    block = rows[-2]["message"]["content"][0]
    block.update(
        name="Bash",
        input={
            "command": "cat > story <<'D'\nfirst\nD\npython - <<'PY'\nfrom pathlib import Path\nPath('story').write_text('first\\nlocal\\n')\nPY"
        },
    )
    rows[-1]["toolUseResult"] = {"stdout": "", "stderr": ""}
    (logs / "cedar.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    codex = tmp_path / "codex"
    launch = (
        "response_item",
        {"type": "function_call", "name": "spawn_agent", "call_id": "spawn", "arguments": "{}"},
    )
    rollout(codex, "parent", [launch, output("spawn", json.dumps({"agent_id": "child"}))])
    p = patch("*** Update File: " + str(tmp_path / "story"), "@@", "-first", "+second")
    rollout(codex, "child", [call("edit", p), output("edit")], parent="parent")
    (tmp_path / "story").write_text("second\nlocal\n")
    batch = [*ClaudeCodeSource().sessions(logs), *CodexSource().sessions(codex)]
    (f,) = histories(batch)
    assert next(h for h in f.hunks if h.kind == "agent").preimage_unknown
    store = tmp_path / "fiction.rl"
    build(store, batch)
    with Queries(store) as q:
        a, b = q.trace(tmp_path / "story", 1)["rows"]
        assert (a["agent"], a["session"], a["prompt"]) == ("codex", "child", "Revise the example.")
        assert (b["agent"], b["tool"]) == ("claude-code", "Bash")
        assert q.trace(tmp_path / "story", 2)["rows"][0]["kind"] == "drift"


@pytest.mark.parametrize("cmd", ["cat > x <<D\nx\nD", "echo x > x"])
def test_completion_event_and_receipt(tmp_path, cmd):
    argv = json.dumps({"cmd": cmd})
    native = completed(
        "shell",
        type="CommandExecution",
        cwd=str(tmp_path),
        command=["/bin/sh", "-c", cmd],
        status="completed",
        exit_code=0,
    )
    rollout(
        tmp_path,
        "dedup",
        [
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "shell",
                    "arguments": argv,
                },
            ),
            output("shell", {"exit_code": 0}),
            native,
        ],
    )
    (s,) = CodexSource().sessions(tmp_path)
    (edit,) = s.edits
    assert edit.after == "x\n"


def test_heredoc_guarded_by_literal_mkdir(tmp_path):
    cmd = "mkdir -p 'new folder' && cat > 'new folder/x' <<'D'\nx\nD"
    (edit,), _ = parse(cmd, tmp_path)
    assert (edit.path, edit.after) == (str(tmp_path / "new folder/x"), "x\n")
    assert not parse(cmd + "\ntrue", tmp_path)[0]
    assert not parse(cmd.replace("'new folder'", '"$DIR"', 1), tmp_path)[0]
    assert not parse(cmd, tmp_path, "failed")[0]


@pytest.mark.parametrize(
    ("changes", "competing", "retained"),
    [
        ({}, False, True),
        ({"exit_code": 1}, False, False),
        ({"exit_code": False}, False, False),
        ({"status": "failed"}, False, False),
        ({"source": "user"}, False, False),
        ({"plugin_id": "fiction"}, False, False),
        ({"command": ["/usr/bin/python", "-c", "cat > x <<D\nx\nD"]}, False, False),
        ({"command": ["/tmp/bash", "-c", "cat > x <<D\nx\nD"]}, False, False),
        ({"command": ["/bin/sh", "-c", "cat > x <<D\nx\nD\ntrue"]}, False, False),
        ({"cwd": "relative"}, False, False),
        ({}, True, False),
    ],
)
def test_runtime_command_proves_one_resolved_literal_write(tmp_path, changes, competing, retained):
    attrs = {
        "type": "CommandExecution",
        "source": "unified_exec_startup",
        "cwd": str(tmp_path),
        "command": ["/bin/sh", "-c", "cat > x <<D\nx\nD"],
        "status": "completed",
        "exit_code": 0,
    }
    attrs.update(changes)
    rows = [
        call("wrapper", "text(await tools.exec_command({cmd: getCommand()}))", "exec"),
        completed("inner", **attrs),
    ]
    if competing:
        rows.append(completed("competing", **(attrs | {"plugin_id": "fiction"})))
    rows.append(output("wrapper", "Script completed\nOutput:\n{}"))
    rollout(tmp_path, "resolved", rows)
    source = CodexSource()
    (s,) = source.sessions(tmp_path)
    assert source.counts["runtime_literal_edits"] == int(retained)
    if retained:
        (edit,) = s.edits
        assert (edit.path, edit.after, edit.prompt) == (
            str(tmp_path / "x"), "x\n", s.prompts[0].id,
        )
    else:
        assert not s.edits


def test_event_directory_is_local_file_url(tmp_path):
    cmd = "cat > new <<'D'\ntext\nD"
    native = completed(
        "uri",
        type="CommandExecution",
        cwd=(tmp_path / "a b").as_uri(),
        command=["/bin/sh", "-c", cmd],
        status="completed",
        exit_code=0,
    )
    rollout(tmp_path, "uri", [native])
    (s,) = CodexSource().sessions(tmp_path)
    assert s.edits[0].path == str(tmp_path / "a b/new")


def test_non_shell_event_argument_is_not_a_command(tmp_path):
    native = completed(
        "printed",
        type="CommandExecution",
        cwd=str(tmp_path),
        command=["/bin/echo", "-c", "cat > x <<D\nx\nD"],
        status="completed",
        exit_code=0,
    )
    rollout(tmp_path, "echo-argv", [native])
    (s,) = CodexSource().sessions(tmp_path)
    assert not s.edits


def test_two_heredocs_on_one_header(tmp_path):
    edits, _ = parse("cat > one <<A; tee two <<'B'\nfirst\nA\nsecond\nB", tmp_path)
    assert [e.after for e in edits] == ["first\n", "second\n"]


def test_parallel_competing_writes(tmp_path):
    js = (
        "const calls = await Promise.all(["
        + ",".join(
            "tools.exec_command(" + json.dumps({"cmd": "cat > same <<D\n" + value + "\nD"}) + ")"
            for value in ("first", "second")
        )
        + "]); text(calls)"
    )
    rollout(
        tmp_path,
        "race",
        [
            call("pair", js, "exec"),
            output("pair", 'Script completed\nOutput:\n[{"exit_code":0},{"exit_code":0}]'),
        ],
    )
    src = CodexSource()
    (s,) = src.sessions(tmp_path)
    assert not s.edits and src.counts["shell_rejected_parallel_order"] == 2


def test_output_cannot_override_failed_exit(tmp_path):
    cmd = "cat > a <<D\nx\nD"
    rollout(
        tmp_path,
        "failed",
        [
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "a",
                    "arguments": json.dumps({"cmd": cmd}),
                },
            ),
            output("a", "Process exited with code 1\nOutput:\nProcess exited with code 0\n"),
        ],
    )
    (s,) = CodexSource().sessions(tmp_path)
    assert not s.edits


@pytest.mark.parametrize("launch_count", [1, 2])
def test_child_task_path_requires_unique_parent_launch(tmp_path, launch_count):
    events = []
    for i in range(launch_count):
        events += [
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": str(i),
                    "arguments": "{}",
                },
            ),
            output(str(i), json.dumps({"task_name": "/root/reviewer"})),
        ]
    rollout(tmp_path, "parent", events)
    child = rollout(
        tmp_path,
        "worker",
        [call("create", patch("*** Add File: answer", "+done")), output("create")],
        parent="parent",
    )
    rows = [json.loads(s) for s in child.read_text().splitlines()]
    rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = "/root/reviewer"
    child.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sessions = {s.id: s for s in CodexSource().sessions(tmp_path)}
    assert sessions["worker"].edits[0].prompt == (
        sessions["parent"].prompts[0].id if launch_count == 1 else None
    )


def test_dynamic_cd_cannot_confirm_a_guarded_absolute_write(tmp_path):
    cmd = f"cd $DIR && cat > {tmp_path}/file <<D\nliteral\nD\ntrue"
    assert not parse(cmd, tmp_path)[0]


def test_consecutive_appends_share_eof_witness(tmp_path):
    from papertrail.replay import replay

    edits, _ = parse("cat >> p <<A\na\nA\ncat >> p <<B\nb\nB", tmp_path)
    path = tmp_path / "p"
    path.write_text("initial\na\nb\n")
    f = replay(str(path), edits)
    assert f.unapplied == 0
    assert [h.edit for h in f.hunks if h.kind == "agent"] == [e.id for e in edits]
