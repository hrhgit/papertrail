"""Invented before/after evidence for sparse files and delegated writes."""

import json

from papertrail.graph import build
from papertrail.model import Edit, Read, Replacement
from papertrail.queries import Queries
from papertrail.replay import replay
from papertrail.sources import ClaudeCodeSource


def edit(file, n, **change):
    return Edit(
        str(n), n, f"2025-01-01T00:00:{n:02}+00:00", "Edit", str(file), None, "demo", **change
    )


def read(file, n, start, text, complete=False):
    return Read(
        str(file), f"2025-01-01T00:00:{n:02}+00:00", n, start, text.splitlines(True), complete
    )


def owners(f):
    hunks = {h.id: h for h in f.hunks}
    return [hunks[h].edit or hunks[h].kind for a, b, h in f.ranges for _ in range(a, b + 1)]


def test_first_and_consecutive_numbered_edits(tmp_path):
    p = tmp_path / "sample"
    p.write_text("opaque\nopaque\ncontext\nthird\nmore\ntail\n")
    a = edit(p, 1, patch="@@ -3,2 +3,3 @@\n context\n-first\n+second\n+more")
    b = edit(p, 2, patch="@@ -4,2 +4,2 @@\n-second\n+third\n more")
    f = replay(str(p), [a, b])
    assert owners(f) == ["unknown", "unknown", "baseline", "2", "1", "unknown"]
    first, second = [h for h in f.hunks if h.edit]
    assert second.replaced == {first.id: 1}
    assert f.unapplied == 0


def test_unknown_before_overwrite(tmp_path):
    p = tmp_path / "sample"
    p.write_text("same\nsecond\n")
    a = edit(p, 1, after="same\nfirst\n")
    b = edit(p, 2, after=p.read_text())
    f = replay(str(p), [a, b])
    assert owners(f) == ["1", "2"]
    assert f.hunks[0].preimage_unknown
    assert f.hunks[1].replaced == {f.hunks[0].id: 1}


def test_reads_fill_holes_and_expose_external_change(tmp_path):
    p = tmp_path / "sample"
    p.write_text("human\nsecond\noriginal\n")
    a = edit(p, 2, patch="@@ -2 +2 @@\n-prior\n+second")
    reads = [read(p, 1, 1, "first\n"), read(p, 3, 1, p.read_text(), True)]
    f = replay(str(p), [a], reads)
    assert owners(f) == ["external", "2", "unknown"]
    assert f.external_events == 1 and f.status == "exact"


def test_ambiguous_and_all_occurrences(tmp_path):
    p = tmp_path / "sample"
    original = "x x\nstable\nx\n"
    a = edit(p, 1, before="", after=original)
    p.write_text(original)
    b = edit(p, 2, replacement=Replacement("x", "y"))
    f = replay(str(p), [a, b])
    assert f.reasons == {"ambiguous_replacement": 1}
    assert owners(f)[1] == "1" and "2" not in owners(f)
    b.replacement = Replacement("x", "y", True)
    p.write_text("y y\nstable\ny\n")
    f = replay(str(p), [a, b])
    assert owners(f) == ["2", "1", "2"]
    assert f.unapplied == 0


def test_deferred_lands_on_read_patch_and_disk(tmp_path):
    p = tmp_path / "sample"
    a = edit(p, 1, replacement=Replacement("cold", "warm"))
    text = "unseen\nroom: warm\nlast\n"
    p.write_text(text)
    for reads in ([], [read(p, 2, 2, "room: warm\n")]):
        f = replay(str(p), [a], reads)
        assert owners(f) == ["unknown", "1", "unknown"]
        assert f.unapplied == 0
    b = edit(p, 2, patch="@@ -2 +2 @@\n-room: warm\n+room: warmer")
    p.write_text(text.replace("warm", "warmer"))
    f = replay(str(p), [a, b])
    first, second = [h for h in f.hunks if h.edit]
    assert owners(f)[1] == "2" and second.replaced == {first.id: 1}


def test_duplicate_disk_witness_has_no_owner(tmp_path):
    p = tmp_path / "sample"
    p.write_text("same\nsame\n")
    f = replay(str(p), [edit(p, 1, replacement=Replacement("old", "same"))])
    assert f.unapplied == 1 and owners(f) == ["unknown", "unknown"]


def test_unanchored_disk_insert_and_ambiguous_known_lines(tmp_path):
    p = tmp_path / "sample"
    e = edit(p, 1, patch="@@ -4 +4 @@\n-old\n+unique")
    p.write_text("head\nunique\ntail\n")
    assert owners(replay(str(p), [e])) == ["unknown", "1", "unknown"]
    p.write_text("unique\ngap\nunique\n")
    assert owners(replay(str(p), [e])) == ["unknown"] * 3


def test_delegated_transcript_and_graph(tmp_path):
    # The result strings, log records and disk files below are all fictional.
    logs = tmp_path / "logs" / "demo"
    side = logs / "parent" / "subagents"
    side.mkdir(parents=True)
    target = tmp_path / "message.txt"
    extra = tmp_path / "note.txt"
    target.write_text("blue blue\n")
    extra.write_text("v2\n")

    def transcript(path, calls, child=False):
        rows = []
        for n, name, args, result, structured in calls:
            tid = f"tool-{n}"
            for phase in (0, 1):
                block = (
                    {"type": "tool_use", "id": tid, "name": name, "input": args}
                    if phase == 0
                    else {"type": "tool_result", "tool_use_id": tid, "content": result}
                )
                row = {
                    "type": "user" if phase else "assistant",
                    "sessionId": "parent",
                    "uuid": f"{n}-{phase}",
                    "timestamp": f"2025-01-01T00:00:{2 * n + phase:02}Z",
                    "cwd": str(tmp_path),
                    "isSidechain": child,
                    "message": {"content": [block]},
                }
                if phase and structured is not None:
                    row["toolUseResult"] = structured
                rows.append(row)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    transcript(
        logs / "parent.jsonl",
        [
            (
                1,
                "Write",
                {"file_path": str(target), "content": "red red\n"},
                "ok",
                {
                    "type": "create",
                    "filePath": str(target),
                    "content": "red red\n",
                    "originalFile": None,
                },
            )
        ],
    )
    transcript(
        side / "agent-child.jsonl",
        [
            (2, "Read", {"file_path": str(target)}, "1\tred red", None),
            (
                3,
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "red",
                    "new_string": "blue",
                    "replace_all": True,
                },
                f"The file {target} has been updated. All occurrences were replaced.",
                None,
            ),
            (
                4,
                "Write",
                {"file_path": str(extra), "content": "v1\n"},
                f"File created successfully at: {extra} (saved)",
                None,
            ),
            (
                5,
                "Write",
                {"file_path": str(extra), "content": "v2\n"},
                f"The file {extra} has been updated successfully.",
                None,
            ),
            (6, "Read", {"file_path": str(extra)}, "1\tv2\n3\toops", None),
            (
                7,
                "Read",
                {"file_path": str(extra)},
                "ok",
                {
                    "file": {
                        "filePath": str(extra),
                        "startLine": 1,
                        "numLines": 2,
                        "totalLines": 2,
                        "content": "v2\n",
                    }
                },
            ),
        ],
        True,
    )
    source = ClaudeCodeSource()
    sessions = list(source.sessions(logs))
    child = next(s for s in sessions if s.parent)
    assert not source.failures and source.skipped["unusable_read"] == 1
    assert [r.complete for r in child.reads] == [False, True]
    assert [e.before for e in child.edits] == [None, "", None]
    assert child.edits[0].replacement == Replacement("red", "blue", True)
    store = tmp_path / "test.rl"
    build(store, sessions)
    with Queries(store) as q:
        assert [r["session"] for r in q.trace(target, 1)["rows"]] == [child.id, "parent"]
        assert q.crossings(target)["rows"][0]["session_count"] == 2


def test_deferred_ambiguous_old_located_by_full_read(tmp_path):
    p = tmp_path / "sample"
    p.write_text("slot\nkeep\ndone\n")
    a = edit(p, 1, before="", after="slot\nkeep\nslot\n")
    b = edit(p, 2, replacement=Replacement("slot", "done"))
    f = replay(str(p), [a, b], [read(p, 3, 1, p.read_text(), True)])
    assert owners(f) == ["1", "1", "2"]
    assert f.hunks[-1].replaced == {f.hunks[0].id: 1}


def test_deferred_cannot_claim_a_future_writer(tmp_path):
    p = tmp_path / "sample"
    p.write_text("taken\n")
    a = edit(p, 1, replacement=Replacement("old", "taken"))
    b = edit(p, 2, patch="@@ -1 +1 @@\n-something else\n+taken")
    f = replay(str(p), [a, b])
    assert owners(f) == ["2"] and f.unapplied == 1
    assert all(not h.replaced for h in f.hunks if h.edit == "2")


def test_late_witness_can_cross_a_disjoint_numbered_insertion(tmp_path):
    p = tmp_path / "sample"
    p.write_text("inserted\nheader\nupdated\n")
    a = edit(p, 1, replacement=Replacement("former", "updated"))
    b = edit(p, 2, patch="@@ -1 +1,2 @@\n+inserted\n header")
    f = replay(str(p), [a, b])
    assert f.unapplied == 0 and owners(f)[0] == "2" and owners(f)[2] == "1"


def test_disk_only_ambiguous_additions_stay_unknown(tmp_path):
    p = tmp_path / "sample"
    p.write_text("keep\nnew\nnew\n")
    a = edit(p, 1, before="", after="keep\n")
    b = edit(p, 2, replacement=Replacement("unseen", "new"))
    f = replay(str(p), [a, b])
    assert owners(f) == ["1", "unknown", "unknown"]


def test_replace_all_across_lines_and_same_line(tmp_path):
    p = tmp_path / "sample"
    p.write_text("bb\nkeep\nb\n")
    a = edit(p, 1, before="", after="a a a a\nkeep\na a\n")
    b = edit(p, 2, replacement=Replacement("a a", "b", True))
    p.write_text(a.after.replace("a a", "b"))
    f = replay(str(p), [a, b])
    assert f.status == "exact" and owners(f) == ["2", "1", "2"]
    p.write_text("prefixjoined\n")
    a.after = "prefixold\njoined\n"
    b.replacement = Replacement("old\n", "")
    f = replay(str(p), [a, b])
    assert f.status == "exact" and owners(f) == ["2"]


def test_partial_read_preserves_known_eof_and_indentation(tmp_path):
    from papertrail.sources.claude import read_snapshot

    p = tmp_path / "sample"
    p.write_text("  same")
    call = (2, "Read", {"file_path": str(p)}, None, "demo", "call", str(tmp_path))
    snap = read_snapshot("2025-01-01T00:00:02+00:00", call, None, "  1\t  same")
    assert snap.lines == ["  same\n"] and not snap.complete
    a = edit(p, 1, before="", after=p.read_text())
    f = replay(str(p), [a], [snap])
    assert owners(f) == ["1"] and f.status == "exact" and not f.external_events


def test_numbered_multiple_hunks_and_unknown_predecessors(tmp_path):
    p = tmp_path / "mixed"
    p.write_text("opaque\nknown\na\nb\nlast\n")
    a = edit(p, 1, patch="@@ -2 +2 @@\n-old\n+known")
    b = edit(p, 2, patch="@@ -2,0 +3,2 @@\n+a\n+b\n@@ -3 +5 @@\n-unseen\n+last")
    f = replay(str(p), [a, b])
    assert owners(f) == ["unknown", "1", "2", "2", "2"]
    assert all(not h.replaced for h in f.hunks if h.edit == "2")


def test_replacement_joins_unknown_next_line(tmp_path):
    p = tmp_path / "join"
    p.write_text("joined:contents\n")
    a = edit(p, 1, patch="@@ -1 +1 @@\n-original\n+previous")
    b = edit(p, 2, replacement=Replacement("previous\n", "joined:"))
    f = replay(str(p), [a, b])
    assert f.unapplied == 0 and owners(f) == ["2"]
    assert (
        next(h for h in f.hunks if h.edit == "1").id
        in next(h for h in f.hunks if h.edit == "2").replaced
    )


def test_copy_suppression_requires_the_original_child(tmp_path):
    root = tmp_path / "logs" / "project"
    side = root / "s" / "subagents"
    side.mkdir(parents=True)
    p = tmp_path / "same"
    call = {
        "type": "tool_use",
        "name": "Write",
        "id": "one",
        "input": {"file_path": str(p), "content": "new"},
    }
    answer = {
        "type": "tool_result",
        "tool_use_id": "one",
        "content": f"File created successfully at: {p}",
    }
    records = [
        {
            "type": role,
            "agentId": "real",
            "sessionId": "s",
            "timestamp": f"2025-01-01T00:00:0{i}Z",
            "message": {"content": [block]},
        }
        for i, (role, block) in enumerate([("assistant", call), ("user", answer)])
    ]
    copy = side / "agent-copy.jsonl"
    copy.write_text("".join(json.dumps(r) + "\n" for r in records))
    (orphan,) = ClaudeCodeSource().sessions(root)
    assert len(orphan.edits) == 1
    (side / "agent-real.jsonl").write_bytes(copy.read_bytes())
    source = ClaudeCodeSource()
    sessions = list(source.sessions(root))
    assert [(s.id, len(s.edits)) for s in sessions] == [("s/agent-copy", 0), ("s/agent-real", 1)]
    assert source.skipped["copied_child_Write"] == 1


def test_deferred_chain_uses_last_disk_witness(tmp_path):
    p = tmp_path / "sample"
    p.write_text("keep\nlabel=third\n")
    a = edit(p, 1, replacement=Replacement("first", "second"))
    b = edit(p, 2, replacement=Replacement("second", "third"))
    f = replay(str(p), [a, b])
    assert owners(f) == ["unknown", "2"] and f.unapplied == 0
    first, second = [h for h in f.hunks if h.edit]
    assert second.replaced == {first.id: 1}


def test_adjacent_newline_replacements_join_physical_rows(tmp_path):
    p = tmp_path / "join"
    p.write_text("ab")
    a = edit(p, 1, before="", after="a\n\nb\n")
    b = edit(p, 2, replacement=Replacement("\n", "", True))
    f = replay(str(p), [a, b])
    assert f.status == "exact" and owners(f) == ["2"]


def test_partial_read_preserves_authors_across_external_insertion(tmp_path):
    p = tmp_path / "sample"
    p.write_text("outside\na\nb\nc\n")
    a = edit(p, 1, before="", after="a\nb\nc\n")
    f = replay(str(p), [a], [read(p, 2, 1, "outside\na\nb\n")])
    assert owners(f) == ["external", "1", "1", "1"]
    assert f.status == "exact" and f.external_events == 1


def test_full_read_contradicts_a_single_known_position(tmp_path):
    p = tmp_path / "sample"
    p.write_text("opaque\nexternal\n")
    a = edit(p, 1, patch="@@ -2 +2 @@\n-old\n+written")
    f = replay(str(p), [a], [read(p, 2, 1, p.read_text(), True)])
    assert owners(f) == ["unknown", "external"]
    assert f.external_events == 1


def test_postimage_survives_unusable_patch_and_flag_is_queryable(tmp_path):
    from papertrail.model import Session

    p = tmp_path / "file"
    p.write_text("out\n")
    e = edit(p, 1, after=p.read_text(), patch="malformed")
    f = replay(str(p), [e])
    assert f.unapplied == 0 and owners(f) == ["1"]
    store = tmp_path / "graph.rl"
    build(store, [Session("example", "demo", "/example", edits=[e])])
    with Queries(store) as q:
        assert q.db.execute(
            "MATCH (h:Hunk {kind:'agent'}) RETURN h.preimage_unknown AS unknown"
        ).data() == [{"unknown": True}]


def test_crlf_literal_uses_same_physical_lines_as_snapshots(tmp_path):
    p = tmp_path / "sample"
    p.write_bytes(b"base\r\nnew\r\n")
    a = edit(p, 1, before="", after="base\r\nold\r\n")
    b = edit(p, 2, replacement=Replacement("old\r\n", "new\r\n"))
    f = replay(str(p), [a, b])
    assert f.unapplied == 0 and f.status == "exact" and owners(f) == ["1", "2"]
