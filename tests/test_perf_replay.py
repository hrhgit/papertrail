from difflib import SequenceMatcher
from itertools import islice

import pytest

from papertrail import replay
from papertrail.model import Edit, Read, Replacement
from papertrail.patches import changes, positions, sections


def test_replay_seeks_directly_to_the_next_observation(tmp_path, monkeypatch):
    path = tmp_path / "repeated.py"
    rows = ["same\n"] * 16
    path.write_text("".join(rows) + "after\n")
    ts = "2026-01-01T00:00:00Z"
    edits = [
        Edit(
            f"e{i}", 2 * i, ts, "edit", str(path), None, "",
            replacement=Replacement("before\n", "after\n"),
        )
        for i in range(1, 1501)
    ]
    reads = [Read(str(path), ts, 3, 1, rows + ["after\n"], True)]
    reads.extend(
        Read(str(path), ts, 2 * i - 1, 1, rows + ["before\n"], False)
        for i in range(3, len(edits) + 1)
    )
    reads.append(Read(str(path), ts, 2 * len(edits) + 1, 1, rows + ["after\n"], True))
    actual = replay.replay(str(path), edits, reads)
    monkeypatch.setattr(replay, "following_events", lambda events, i: islice(events, i + 1, None))
    assert actual == replay.replay(str(path), edits, reads)
    assert any(h.edit == edits[0].id for h in actual.hunks)


def test_following_events_only_accesses_requested_items():
    class Segment:
        def __len__(self):
            return 20_000

        def __getitem__(self, i):
            assert i == 15_001
            return i

    assert next(replay.following_events(Segment(), 15_000)) == 15_001


def test_patch_context_with_repeated_lines_matches_full_scan():
    context = ["anchor\n", *["repeat\n"] * 300, "before\n"]
    patch = "@@\n " + " ".join(context[:-1]) + "-before\n+after\n"
    (part,) = sections(patch)
    rows = ["repeat\n"] * 3_000 + context
    for old in (rows, rows + context, rows[:-1]):
        expected = [
            i for i in range(len(old) - len(context) + 1) if old[i : i + len(context)] == context
        ]
        if len(expected) == 1:
            assert list(positions(old, [part])) == [(part, expected[0])]
        else:
            with pytest.raises(ValueError, match="Patch context missing or ambiguous"):
                list(positions(old, [part]))


def naive_changes(old, new):
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    end_a, end_b = len(old), len(new)
    while min(end_a, end_b) > prefix and old[end_a - 1] == new[end_b - 1]:
        end_a -= 1
        end_b -= 1
    return [
        (prefix + i, prefix + j, new[prefix + k : prefix + l])
        for tag, i, j, k, l in SequenceMatcher(
            None, old[prefix:end_a], new[prefix:end_b], autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]


def test_repeated_line_diff_matches_original_alignment():
    repeated = ["repeat a\n", "repeat b\n"] * 800
    old = ["head\n", "before\n", *repeated, "old end\n", "tail\n"]
    for middle in (repeated, [*repeated, "repeat a\n"], ["repeat b\n", *repeated]):
        new = ["head\n", "after\n", *middle, "new end\n", "tail\n"]
        assert list(changes(old, new)) == naive_changes(old, new)
