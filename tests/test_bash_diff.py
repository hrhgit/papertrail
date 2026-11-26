"""Claude Code records a `bashEditDiff` for Bash commands that change files. A named
file in a script's diff is the session's edit; other writers' changes are not."""

import json
from pathlib import Path

import pytest

from papertrail.graph import build
from papertrail.queries import Queries
from papertrail.replay import histories
from papertrail.sources import ClaudeCodeSource

FAKE = Path(__file__).parent / "fixtures/logs/demo/cedar.jsonl"
SCRIPT = (
    "python3 - <<'PY'\nfrom pathlib import Path\n"
    "p = Path('notes.txt'); p.write_text(p.read_text().replace('old', 'new'))\nPY"
)


def hunk(old_start, old, new, context=()):
    rows = [" " + c for c in context] + ["-" + r for r in old] + ["+" + r for r in new]
    return {
        "oldStart": old_start,
        "oldLines": len(context) + len(old),
        "newStart": old_start,
        "newLines": len(context) + len(new),
        "lines": rows,
    }


def session(tmp_path, command, files, error=False, extra=None):
    logs = tmp_path / "claude"
    logs.mkdir(exist_ok=True)
    rows = [json.loads(x) for x in FAKE.read_text().splitlines()]
    for r in rows:
        if "cwd" in r:
            r["cwd"] = str(tmp_path)
    rows[-2]["message"]["content"][0].update(name="Bash", input={"command": command})
    result = rows[-1]["message"]["content"][0]
    if error:
        result["is_error"] = True
    diff = {"files": files, "moreFiles": 0, "changedFiles": [f["filePath"] for f in files]}
    diff.update(extra or {})
    rows[-1]["toolUseResult"] = {
        "stdout": "",
        "stderr": "",
        "exitCode": 1 if error else 0,
        "bashEditDiff": diff,
    }
    (logs / "cedar.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    source = ClaudeCodeSource()
    (s,) = source.sessions(logs)
    return s, source


def notes(tmp_path):
    path = tmp_path / "notes.txt"
    return {"filePath": str(path), "hunks": [hunk(2, ["old"], ["new"], context=["keep"])]}


def test_named_file_in_script_diff_is_an_edit(tmp_path):
    (tmp_path / "notes.txt").write_text("keep\nnew\ntail\n")
    s, source = session(tmp_path, SCRIPT, [notes(tmp_path)])
    bash = [e for e in s.edits if e.tool == "Bash"]
    assert len(bash) == 1 and bash[0].path == str((tmp_path / "notes.txt").resolve())
    assert bash[0].patch.splitlines()[0] == "@@ -2,2 +2,2 @@"
    assert source.counts["bash_diff_edits"] == 1
    (f,) = [f for f in histories([s]) if f.path.endswith("notes.txt")]
    owners = {h.id: h for h in f.hunks}
    kinds = [owners[o].kind for _, _, o in f.ranges]
    # Line 1 is context the diff showed, line 2 the command's write, line 3 never seen.
    assert kinds == ["baseline", "agent", "unknown"]
    store = tmp_path / "graph.rl"
    build(store, [s])
    with Queries(store) as q:
        row = q.trace(tmp_path / "notes.txt", 2)["rows"][0]
        assert (row["agent"], row["tool"], row["session"]) == ("claude-code", "Bash", "cedar")
        assert row["prompt"] == "Use a morning greeting and keep the local note."


@pytest.mark.parametrize(
    "command",
    [
        "python3 ~/.claude/skills/use-ai/scripts/ai_run.py --cli codex --prompt-file notes.txt",
        "codex exec 'fix notes.txt'",
        "cd x && claude -p 'edit notes.txt'",
        "git merge feature  # notes.txt",
        "git -C repo checkout main -- notes.txt",
        "git stash pop && cat notes.txt",
    ],
)
def test_other_writers_are_not_this_session(tmp_path, command):
    s, source = session(tmp_path, command, [notes(tmp_path)])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_other_writer"] == 1


def test_names_that_merely_contain_an_agent_name_are_fine(tmp_path):
    command = "sed -i '' 's/old/new/' sources/codex.py notes.txt ~/.claude/x"
    s, _ = session(tmp_path, command, [notes(tmp_path)])
    assert [e.tool for e in s.edits if e.tool == "Bash"] == ["Bash"]


def test_file_the_command_does_not_name_is_skipped(tmp_path):
    other = {"filePath": str(tmp_path / "elsewhere.py"), "hunks": [hunk(1, ["a"], ["b"])]}
    s, source = session(tmp_path, SCRIPT, [notes(tmp_path), other])
    assert [Path(e.path).name for e in s.edits if e.tool == "Bash"] == ["notes.txt"]
    assert source.skipped["bash_diff_unnamed_file"] == 1


def test_truncated_hunk_is_skipped(tmp_path):
    entry = notes(tmp_path)
    entry["hunks"][0]["newLines"] += 5
    s, source = session(tmp_path, SCRIPT, [entry])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_incomplete_hunks"] == 1


def test_failed_command_is_not_evidence(tmp_path):
    s, _ = session(tmp_path, SCRIPT, [notes(tmp_path)], error=True)
    assert not [e for e in s.edits if e.tool == "Bash"]


def test_new_file_and_unavailable_flag(tmp_path):
    created = {
        "filePath": str(tmp_path / "notes.txt"),
        "hunks": [{"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": 2, "lines": ["+a", "+b"]}],
    }
    (tmp_path / "notes.txt").write_text("a\nb\n")
    s, _ = session(tmp_path, SCRIPT, [created], extra={"unavailable": True, "moreFiles": 3})
    (f,) = [f for f in histories([s]) if f.path.endswith("notes.txt")]
    owners = {h.id: h for h in f.hunks}
    assert [owners[o].kind for _, _, o in f.ranges] == ["agent"]
