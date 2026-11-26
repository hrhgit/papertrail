"""Synthetic commands and file observations exercise the inference window."""

from papertrail.graph import build, select
from papertrail.model import Edit, Hint, Prompt, Read, Replacement, Session
from papertrail.replay import histories
from papertrail.web import ReaderQueries


def ts(second):
    return f"2025-01-01T00:00:{second:02}+00:00"


def test_inference_uses_latest_named_hint_between_observations(tmp_path):
    path = str(tmp_path / "example.txt")
    (tmp_path / "example.txt").write_text("baseline\nscript\nstill here\n")
    proven = Edit(
        "first", 0, ts(1), "Write", path, None, "demo",
        before="baseline\noriginal\nstill here\n", after="baseline\nrecorded\nstill here\n",
    )
    observations = [
        Read(path, ts(5), 0, 1, ["baseline\n", "recorded\n", "still here\n"], True),
        Read(path, ts(15), 0, 1, ["baseline\n", "script\n", "still here\n"], True),
    ]
    recorded = Session("recorded", "claude-code", "", edits=[proven], reads=observations)
    named = "python3 rewrite.py example.txt --marker=FICTITIOUS_COMMAND_MARKER"
    guessed = Session(
        "guessed", "codex", "",
        prompts=[Prompt("request", 0, ts(3), "Revise the example.")],
        hints=[
            Hint(key, 0, ts(second), "exec_command", str(tmp_path), command, "request", "demo", "high")
            for key, second, command in [
                ("too-early", 4, named),
                ("earlier", 7, named),
                ("chosen", 13, named),
                ("different", 14, "python3 rewrite.py other/example.txt"),
                ("too-late", 16, named),
            ]
        ],
    )
    sessions = select([recorded, guessed], tmp_path)
    (history,) = histories(sessions)
    baseline, current, _ = [next(h for h in history.hunks if h.id == owner)
                            for _, _, owner in history.ranges]
    assert baseline.kind == "baseline"
    assert (current.kind, current.basis, current.was, current.inferred_from) == (
        "inferred", "command", "external", "chosen",
    )
    assert current.text == "script\n"
    assert list(current.replaced.values()) == [1]

    store = tmp_path / "graph.rl"
    build(store, sessions)
    assert b"FICTITIOUS_COMMAND_MARKER" not in store.read_bytes()
    with ReaderQueries(store) as q:
        for row in (q.trace(path, 2)["rows"][0], q.blame(path)["rows"][1],
                    q._tracks(path)[1], q.span(path, 2, 2)["rows"][0]):
            assert (row["kind"], row["basis"], row["was"]) == ("inferred", "command", "external")
            assert (row["session"], row["prompt"], row["tool"], row["model"]) == (
                "guessed", "Revise the example.", "exec_command", "demo",
            )
        assert q.trace(path, 2)["rows"][0]["effort"] == "high"
        assert [row["kind"] for row in q.trace(path, 2)["rows"]] == [
            "inferred", "agent", "baseline",
        ]
        assert q.db.execute("MATCH (h:Hint) RETURN h.id AS id, h.command AS command").data() == [
            {"id": "chosen", "command": None},
        ]
        stats = q.stats()["rows"][0]
        assert stats["edits"] == 1 and stats["lines"]["inferred"] == 1
        assert stats["agent_fraction"] == 0
        assert stats["agent_plus_inferred_fraction"] == 1 / 3


def test_drift_uses_preceding_proven_edit_and_discards_old_hint(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("prior\ndisk\n")
    edit = Edit("edit", 0, ts(5), "Edit", str(path), None, "demo",
                before="prior\noriginal\n", after="prior\nrecorded\n")
    hint = Hint("old", 0, ts(2), "Bash", str(tmp_path), "python3 rewrite.py example.txt",
                None, "demo", "")
    session = Session("writer", "claude-code", "", edits=[edit], hints=[hint])
    (f,) = histories([session])
    baseline, current = [next(h for h in f.hunks if h.id == owner) for _, _, owner in f.ranges]
    assert baseline.kind == "baseline"
    assert (current.kind, current.basis, current.was, current.inferred_from) == (
        "inferred", "nearby", "drift", "edit",
    )


def test_unknown_without_candidate_stays_unknown(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("unobserved\n")
    edit = Edit("unplaced", 0, ts(2), "Edit", str(path), None, "demo",
                replacement=Replacement("absent", "new"))
    (f,) = histories([Session("writer", "claude-code", "", edits=[edit])])
    assert f.unplaced_edits == {"unplaced"}
    (h,) = f.hunks
    assert (h.kind, h.text, h.edit) == ("unknown", "", None)
