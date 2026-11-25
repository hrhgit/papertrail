import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import relate

from papertrail.graph import build, select
from papertrail.model import Edit
from papertrail.queries import Queries
from papertrail.replay import histories, patch_changes, replay
from papertrail.sources import ClaudeCodeSource

FIXTURE = Path(__file__).parent / "fixtures"
PROJECT = (FIXTURE / "project").resolve()
GREETING = PROJECT / "greeting.py"


@pytest.fixture
def sessions():
    return list(ClaudeCodeSource().sessions(FIXTURE / "logs"))


@pytest.fixture
def store(tmp_path, sessions):
    path = tmp_path / "demo.rl"
    build(path, sessions)
    return path


def test_source(sessions):
    assert len(sessions) == 4
    parent = next(s for s in sessions if s.id == "amber")
    child = next(s for s in sessions if s.parent)
    assert len(parent.prompts) == 2
    assert parent.agent == "claude-code"
    assert [e.prompt for e in parent.edits] == [p.id for p in parent.prompts]
    assert parent.edits[0].before == ""
    assert parent.edits[0].path == str(GREETING)
    assert child.parent == parent.id
    assert not child.prompts
    assert child.edits[0].prompt == parent.prompts[-1].id
    assert json.loads(json.dumps(asdict(child)))["edits"][0]["after"] == "Run greeting.py\n"
    assert {s.id for s in select(sessions, PROJECT / "usage.txt")} == {child.id, parent.id}
    assert select(sessions, PROJECT / "absent") == []


def test_source_diagnostics(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('bad json\n{"timestamp":"bad"}\n')
    source = ClaudeCodeSource()
    assert list(source.sessions(tmp_path)) == []
    assert source.failures == {"invalid_json_record": 1, "invalid_timestamp": 1}


def test_multiple_calls_and_late_result(tmp_path):
    records = [json.loads(x) for x in (FIXTURE / "logs/demo/amber.jsonl").read_text().splitlines()]
    first, second = records[2], records[6]
    first["message"]["content"].extend(second["message"]["content"])
    path = tmp_path / "amber.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in [records[1], first, records[5], records[3], records[7]])
    )
    (s,) = ClaudeCodeSource().sessions(tmp_path)
    assert len({e.id for e in s.edits}) == 2
    assert all(e.prompt == s.prompts[0].id for e in s.edits)
    assert all(e.seq == 2 for e in s.edits)


def test_attribution(sessions):
    files = {Path(f.path).name: f for f in histories(sessions)}
    f = files["greeting.py"]
    lookup = {h.id: h for h in f.hunks}
    assert [(a, b, lookup[h].kind) for a, b, h in f.ranges] == [
        (1, 1, "drift"),
        (2, 2, "agent"),
        (3, 3, "external"),
    ]
    assert f.external_events == 1
    assert "00:00:07" < lookup[f.ranges[2][2]].ts.split("T")[1] < "00:00:23"
    assert f.status == "drift"
    assert [h.kind for h in files["settings.py"].hunks] == ["baseline", "agent"]
    assert files["settings.py"].status == "exact"


def test_partial_patch_and_gap(tmp_path):
    path = tmp_path / "rows"
    path.write_text("keep\nlater\n")
    e = Edit(
        "first", 1, "2025-01-01T00:00:00+00:00", "Write", str(path), None, "m", "", "keep\nold\n"
    )
    update = replace(
        e, id="second", seq=2, before=None, after=None, patch="@@\n keep\n-old\n+later"
    )
    f = replay(str(path), [update, e])
    assert f.status == "exact"
    assert f.hunks[-1].replaced == {f.hunks[0].id: 1}
    assert f.hunks[-1].text == "later\n"
    assert f.unapplied == 0
    assert patch_changes(["keep\n", "old\n"], "@@ -8 +8 @@\n-old\n+later") == [(1, 2, ["later\n"])]
    with pytest.raises(ValueError, match="ambiguous"):
        patch_changes(["old\n", "old\n"], "@@\n-old\n+new")
    f = replay(str(path), [update])
    assert f.status == "unresolved" and f.unapplied == 1 and f.hunks[-1].kind == "unknown"
    recovered = replace(e, seq=3, before="keep\ntmp\n", after=path.read_text())
    f = replay(str(path), [update, recovered])
    assert [h.kind for h in f.hunks] == ["unknown", "agent"]
    assert f.status == "exact"
    path.unlink()
    assert replay(str(path), [e]).status == "missing"


def test_repeated_lines_use_verified_patch(tmp_path):
    path = tmp_path / "duplicate"
    path.write_text("x\ny\nx\n")
    before = "x\nx\nx\n"
    e = Edit(
        "change",
        1,
        "2025-01-01T00:00:00Z",
        "Edit",
        str(path),
        None,
        "m",
        before,
        path.read_text(),
        "@@ -2 +2 @@\n-x\n+y",
    )
    f = replay(str(path), [e])
    assert [(a, b) for a, b, _ in f.ranges] == [(1, 1), (2, 2), (3, 3)]
    assert f.ranges[0][2] == f.ranges[2][2]
    assert f.hunks[-1].removed == f.hunks[-1].added == 1


def test_graph_and_queries(store):
    with Queries(store) as q:
        assert q.db.execute(
            "MATCH (:Agent)-[:RAN]->(:Session)-[:SPAWNED]->(:Session) RETURN count(*) AS n"
        ).data() == [{"n": 1}]
        assert q.db.execute(
            "MATCH (:Session)-[:FIRST_PROMPT]->(:Prompt)-[:NEXT]->(:Prompt) RETURN count(*) AS n"
        ).data() == [{"n": 1}]
        trace = q.trace(GREETING, 2)
        assert any("REPLACED*0.." in stmt for stmt in trace["cypher"])
        rows = trace["rows"]
        assert [r["session"] for r in rows] == ["cedar", "birch", "amber", "amber"]
        assert rows[-1]["replaced_lines"] == [1, 1, 1]
        assert rows[1]["next_files"] == [str(PROJECT / "settings.py")]
        assert rows[-1]["prompt"] == "Start with a simple greeting."
        assert rows[-1]["resume"] == "claude --resume amber"
        pseudo = q.trace(GREETING, 1)["rows"][0]
        assert pseudo["session"] is None and pseudo["next_files"] == pseudo["previous_files"] == []
        assert q.trace(PROJECT / "usage.txt", 1)["rows"][0]["resume"] == "claude --resume amber"
        assert q.trace(PROJECT / "usage.txt", 1)["rows"][0]["prompt"] == rows[2]["prompt"]
        assert [r["kind"] for r in q.blame(GREETING, 2, 3)["rows"]] == ["agent", "external"]
        assert q.blame(GREETING, limit=1)["truncated"]
        (c,) = q.crossings()["rows"]
        assert c["session_count"] == 3 and c["start"] == c["end"] == 2
        assert q.crossings(PROJECT / "settings.py")["rows"] == []
        assert [r["event"] for r in q.session("bir")["rows"]] == [
            "HAS_PROMPT",
            "HAS_EDIT",
            "HAS_EDIT",
        ]
        assert q.stats()["rows"][0]["lines"] == {
            "agent": 3,
            "drift": 1,
            "external": 1,
            "baseline": 1,
            "unknown": 0,
        }


def test_trace_lists_tied_edits_in_session_order(tmp_path):
    from papertrail.model import Session

    paths = [tmp_path / name for name in ("z", "b", "current", "x", "a")]
    edits = []
    for path in paths:
        path.write_text("sample\n")
        edits.append(
            Edit(
                path.name, 1, "2025-01-01T00:00:00Z", "Write", str(path), None, "m",
                before="", after="sample\n"
            )
        )
    store = tmp_path / "sessions.rl"
    build(store, [Session("writer", "fiction", str(tmp_path), edits=edits)])

    with Queries(store) as q:
        row = q.trace(paths[2], 1)["rows"][0]
    assert row["previous_files"] == [str(path) for path in paths[:2]]
    assert row["next_files"] == [str(path) for path in paths[3:]]


def test_rebuild_is_atomic(store, sessions):
    build(store, sessions)
    invalid = replace(sessions[0], id=sessions[1].id)
    with pytest.raises(relate.Error):
        build(store, [*sessions, invalid])
    with Queries(store) as q:
        assert q.stats()["rows"][0]["edits"] == 6


def cli(*args):
    return subprocess.run(
        [str(Path(sys.executable).with_name("papertrail")), *map(str, args), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "args",
    [
        ("ingest", "--logs", FIXTURE / "logs", "--all"),
        ("blame", GREETING, "-L", "2,2"),
        ("trace", str(GREETING) + ":2"),
        ("session", "amber"),
        ("crossings", GREETING),
        ("stats",),
    ],
)
def test_cli_json(store, args):
    extra = [] if args[0] == "ingest" else ["--show-cypher", "--limit", "1"]
    result = cli(*args, "--store", store, *extra)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data["rows"]) == 1
    if extra:
        assert data["cypher"]
    if args[0] in ("session", "trace"):
        assert data["truncated"]


@pytest.mark.parametrize(
    "args,hint",
    [
        (("trace", str(GREETING) + ":100"), "1..3"),
        (("blame", PROJECT / "other"), "recorded edits"),
        (("session", "no-such-session"), "full ID"),
        (("blame", GREETING, "--limit", "0"), "positive"),
        (("ingest", "--source", "unregistered"), "invalid choice"),
    ],
)
def test_errors(store, args, hint):
    r = cli(*args, "--store", store)
    assert r.returncode != 0 and hint in json.loads(r.stderr)["error"]


def test_missing_store(tmp_path):
    r = cli("stats", "--store", tmp_path / "missing.rl")
    assert r.returncode != 0 and "ingest" in json.loads(r.stderr)["error"]


def test_branching_trace_and_deleted_crossing(tmp_path):
    from papertrail.model import Session

    path = tmp_path / "branch"
    path.write_text("combined\n")
    before = "left\nright\n"
    sessions = []
    for i, (old, new) in enumerate(
        [(before, "A\nright\n"), ("A\nright\n", "A\nB\n"), ("A\nB\n", "combined\n")]
    ):
        sid = f"author-{i}"
        e = Edit(
            sid + "-edit", 1, f"2025-01-01T00:00:0{i}Z", "Edit", str(path), None, "m", old, new
        )
        sessions.append(Session(sid, "fiction", "/synthetic", edits=[e]))
    store = tmp_path / "branches.rl"
    # The two authors independently changed disjoint baseline lines.
    path.write_text("A\nB\n")
    build(store, sessions[:2])
    with Queries(store) as q:
        assert q.crossings()["rows"] == []
    path.write_text("combined\n")
    build(store, sessions)
    with Queries(store) as q:
        rows = q.trace(path, 1)["rows"]
        assert {r["session"] for r in rows if r["depth"] == 1} == {"author-0", "author-1"}
        assert [r["kind"] for r in rows if r["depth"] == 2] == ["baseline", "baseline"]
        assert len({tuple(r["chain"]) for r in rows}) == len(rows)
    path.unlink()
    build(store, sessions)
    with Queries(store) as q:
        assert q.crossings(path)["rows"][0]["start"] is None
        with pytest.raises(ValueError, match="missing"):
            q.trace(path, 1)


def test_substring_newlines_and_range_clipping(tmp_path):
    from papertrail.model import Session

    path = tmp_path / "unicode"
    content = "a\r\nb\u2028c\r\nd\n"
    path.write_bytes(content.encode())
    e = Edit("edit", 1, "2025-01-01T00:00:00Z", "Write", str(path), None, "m", "", content)
    store = tmp_path / "ranges.rl"
    build(store, [Session("s", "fiction", "/synthetic", edits=[e])])
    with Queries(store) as q:
        (row,) = q.blame(path, 2, 2)["rows"]
        assert row["start"] == row["end"] == 2
        assert q.stats()["rows"][0]["lines"]["agent"] == 3


def test_edit_variants_and_bash_evidence(tmp_path):
    from papertrail.model import Session

    source = ClaudeCodeSource()
    session = Session("demo", "claude-code", "/synthetic")

    def tool(name, result, args=None):
        return source.edit(
            session,
            "2025-01-01T00:00:00Z",
            (1, name, args or {}, None, "m", "id", str(tmp_path)),
            result,
        )

    e = tool(
        "Edit",
        {
            "originalFile": "a a",
            "oldString": "a",
            "newString": "b",
            "filePath": "sample",
            "replaceAll": True,
        },
    )
    assert e.after == "b b"
    e = tool(
        "MultiEdit",
        {
            "originalFile": "a",
            "filePath": "sample",
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "b", "new_string": "c"},
            ],
        },
    )
    assert e.after == "c"
    e = tool(
        "NotebookEdit",
        {"original_file": "{}", "updated_file": "{}\n"},
        {"notebook_path": "sample.ipynb"},
    )
    assert e.before == "{}" and e.after == "{}\n"
    e = tool(
        "Edit",
        {
            "originalFile": None,
            "filePath": "sample",
            "structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}
            ],
        },
    )
    assert e.before is e.after is None and e.patch.startswith("@@ -1,1")
    assert (
        tool(
            "Bash",
            {"bashEditDiff": {"files": [{"filePath": "sample", "hunks": []}]}},
            {"command": "git diff"},
        )
        is None
    )
    assert source.skipped["bash_diff_without_write_evidence"] == 1


def test_effort_is_recorded_per_edit(tmp_path):
    """Reasoning effort travels from the transcript to the graph and the CLI rows."""
    fixtures = Path(__file__).parent / "fixtures"
    store = str(tmp_path / "effort.rl")
    done = cli("ingest", "--logs", str(fixtures / "logs"), "--project", str(fixtures / "project"), "--store", store, "--json")
    assert done.returncode == 0, done.stderr
    traced = cli("trace", f"{fixtures / 'project' / 'greeting.py'}:2", "--store", store, "--json")
    assert traced.returncode == 0, traced.stderr
    by_session = {row["session"]: row["effort"] for row in json.loads(traced.stdout)["rows"]}
    assert by_session["cedar"] == "max"
    assert by_session["birch"] == "high"
    assert by_session["amber"] == ""  # transcript without the field
    from papertrail.cli import model_label

    assert model_label({"model": "demo-model", "effort": "max"}) == "demo-model (max)"
    assert model_label({"model": "demo-model", "effort": None}) == "demo-model"


def steps(path, texts, tool="Edit"):
    """One agent edit per step, turning each text into the next."""
    return [
        Edit(f"step-{i}", i, f"2025-01-01T00:00:{i:02}Z", tool, str(path), None, "m", old, new)
        for i, (old, new) in enumerate(zip(texts, texts[1:]), 1)
    ]


def test_deletion_marks_follow_the_file(tmp_path):
    path = tmp_path / "marks"
    texts = [
        "",
        "a\nb\nc\nd\ne\n",
        "a\nd\ne\n",  # takes out b and c
        "top\na\nd\ne\n",  # a line above pushes the spot down
        "top\na\ne\n",  # takes out d at the same spot
    ]
    path.write_text(texts[-1])
    f = replay(str(path), steps(path, texts))
    by_edit = {h.edit: h for h in f.hunks}
    first, second = by_edit["step-2"], by_edit["step-4"]
    assert f.gaps == [(2, first.id), (2, second.id)]
    assert (first.removed, first.removed_text) == (2, "b\nc\n")
    assert second.removed_text == "d\n"
    assert by_edit["step-1"].removed_text == by_edit["step-3"].removed_text == ""

    # Rewriting both neighbours leaves the deletion no spot.
    path.write_text("x\ny\n")
    f = replay(str(path), steps(path, ["", "a\nb\nc\n", "a\nc\n", "x\ny\n"]))
    assert f.gaps == []

    # A patch deletion moves through the same rows.
    path.write_text("a\nc\n")
    first = Edit("write", 1, "2025-01-01T00:00:00Z", "Write", str(path), None, "m", "", "a\nb\nc\n")
    cut = replace(first, id="cut", seq=2, before=None, after=None, patch="@@\n a\n-b\n c")
    f = replay(str(path), [first, cut])
    (hunk,) = [h for h in f.hunks if h.edit == "cut"]
    assert f.status == "exact" and f.gaps == [(1, hunk.id)] and hunk.removed_text == "b\n"


def text(*args):
    """The command as a person runs it, without --json."""
    exe = str(Path(sys.executable).with_name("papertrail"))
    return subprocess.run([exe, *map(str, args)], check=False, capture_output=True, text=True)


def test_cli_text_and_stale(tmp_path, sessions):
    """Readable rows show the code. A file changed since ingest is flagged, not misquoted."""
    project = tmp_path / "project"
    project.mkdir()
    file = project / "greeting.py"
    file.write_text(GREETING.read_text())
    moved = [
        replace(s, edits=[replace(e, path=e.path.replace(str(PROJECT), str(project))) for e in s.edits])
        for s in sessions
    ]
    store = project / ".papertrail/graph.rl"
    build(store, moved)
    assert (project / ".papertrail/.gitignore").read_text() == "*\n"

    shown = text("blame", file, "--store", store)
    assert shown.returncode == 0 and not shown.stderr
    assert '      2 │ greeting = "morning"' in shown.stdout
    assert "cedar  Morning greeting\n          Use a morning greeting" in shown.stdout
    assert all(len(line) <= 100 for line in shown.stdout.splitlines())
    traced = text("trace", f"{file}:2", "--store", store)
    assert "Asked     Use a morning greeting" in traced.stdout and "claude --resume cedar" in traced.stdout
    fresh = json.loads(cli("blame", file, "--store", store).stdout)
    assert fresh["stale"] is False and "path" not in fresh

    file.write_text("# new top line\n" + file.read_text())
    for args in (("blame", file), ("trace", f"{file}:2"), ("crossings", file)):
        assert json.loads(cli(*args, "--store", store).stdout)["stale"] is True
    shown = text("blame", file, "--store", store)
    assert "changed after this graph was built" in shown.stderr and "│" not in shown.stdout
    assert "stale" not in json.loads(cli("crossings", "--store", store).stdout)


def test_help_explains_every_command():
    top = text("--help").stdout
    for name in ("ingest", "blame", "trace", "session", "crossings", "stats", "skill", "ui"):
        assert name in top and "\n" in text(name, "--help").stdout.split("options:")[0].strip()
    assert "examples:" in top and "Graph file to read" in text("trace", "--help").stdout
