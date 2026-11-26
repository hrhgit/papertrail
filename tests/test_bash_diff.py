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
        "git --no-pager -C 'with spaces' restore notes.txt",
        "/usr/bin/git --work-tree=project checkout-index -- notes.txt",
        "codex<input.txt && ls notes.txt",
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


@pytest.mark.parametrize(
    "command",
    [
        "git diff notes.txt",
        "cat notes.txt",
        "/bin/cat notes.txt",
        "cd sub && grep -n old notes.txt | head -5",
        "sed -n 1,5p notes.txt 2>&1",
        "git -C repo log -p -- notes.txt > /dev/null",
        "git --no-pager -C 'with spaces' diff -- notes.txt",
        "rg -n old notes.txt",
        "find . -name notes.txt",
        "sort notes.txt",
        "xxd notes.txt",
    ],
)
def test_read_only_command_is_not_an_edit(tmp_path, command):
    s, source = session(tmp_path, command, [notes(tmp_path)])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_read_only"] == 1


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' 's/old/new/' notes.txt",
        "cat notes.txt > notes.txt.tmp; mv notes.txt.tmp notes.txt",
        "sed -n 'w notes.txt' input.txt",
        "sed -ni 'p' notes.txt",
        "find . -name notes.txt -exec touch {} +",
        "sort --output=notes.txt notes.txt",
        "uniq input.txt notes.txt",
        "xxd -r input.txt notes.txt",
        "git diff --output=notes.txt",
        "./cat notes.txt",
    ],
)
def test_writing_commands_still_count(tmp_path, command):
    s, _ = session(tmp_path, command, [notes(tmp_path)])
    assert [e.tool for e in s.edits if e.tool == "Bash"] == ["Bash"]


def test_dev_null_prefix_is_not_a_discarded_redirect(tmp_path):
    entry = {"filePath": "/dev/null-dir/notes.txt", "hunks": [hunk(1, ["old"], ["new"])]}
    s, _ = session(tmp_path, "cat input.txt >/dev/null-dir/notes.txt", [entry])
    assert [e.path for e in s.edits if e.tool == "Bash"] == [entry["filePath"]]


def test_quoted_agent_invocation_is_another_writer(tmp_path):
    s, source = session(tmp_path, "bash -c 'claude -p fix' notes.txt", [notes(tmp_path)])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_other_writer"] == 1


def test_literal_echo_of_agent_name_with_or_without_diff(tmp_path):
    for files in ([], [notes(tmp_path)]):
        s, source = session(tmp_path, "echo codex > notes.txt", files)
        (edit,) = [e for e in s.edits if e.tool == "Bash"]
        assert edit.after == "codex\n"
        assert source.counts["bash_diff_edits"] == 0


def test_name_inside_another_name_is_not_named(tmp_path):
    entry = {"filePath": str(tmp_path / "a.py"), "hunks": [hunk(1, ["a"], ["b"])]}
    s, source = session(tmp_path, "python3 fix.py data.py", [entry])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_unnamed_file"] == 1


def test_same_filename_in_another_directory_is_not_named(tmp_path):
    entry = {
        "filePath": str(tmp_path / "docs" / "notes.txt"),
        "hunks": [hunk(1, ["old"], ["new"])],
    }
    s, source = session(tmp_path, "python rewrite.py other/notes.txt", [entry])
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.skipped["bash_diff_unnamed_file"] == 1
    for target in ("docs/notes.txt", "./docs/notes.txt", entry["filePath"]):
        s, _ = session(tmp_path, f"python rewrite.py '{target}'", [entry])
        assert [e.path for e in s.edits if e.tool == "Bash"] == [entry["filePath"]]
    for separator in (" && ", "; ", "\n"):
        command = "cd docs" + separator + "python rewrite.py notes.txt"
        s, _ = session(tmp_path, command, [entry, notes(tmp_path)])
        assert [e.path for e in s.edits if e.tool == "Bash"] == [entry["filePath"]]


def test_shared_result_record_is_ambiguous(tmp_path):
    logs = tmp_path / "claude"
    s, _ = session(tmp_path, SCRIPT, [notes(tmp_path)])
    rows = [json.loads(x) for x in (logs / "cedar.jsonl").read_text().splitlines()]
    use = rows[-2]["message"]["content"][0]
    twin = dict(use, id=use["id"] + "b")
    rows[-2]["message"]["content"].append(twin)
    result = rows[-1]["message"]["content"][0]
    rows[-1]["message"]["content"].append(dict(result, tool_use_id=twin["id"]))
    (logs / "cedar.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    source = ClaudeCodeSource()
    (s,) = source.sessions(logs)
    assert not [e for e in s.edits if e.tool == "Bash"]
    assert source.failures["ambiguous_shared_result"] >= 1


@pytest.mark.parametrize("exit_code", [0, 1])
def test_shared_payload_cannot_confirm_literal_shell_write(tmp_path, exit_code):
    logs = tmp_path / "claude"
    session(tmp_path, "cat > notes.txt <<'END'\nnew\nEND", [notes(tmp_path)])
    path = logs / "cedar.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    result = rows[-1]["message"]["content"][0]
    rows[-1]["message"]["content"].append(dict(result, tool_use_id="unrelated"))
    rows[-1]["toolUseResult"]["exitCode"] = exit_code
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    source = ClaudeCodeSource()
    (parsed,) = source.sessions(logs)
    assert not [e for e in parsed.edits if e.tool == "Bash"]
    assert source.failures["ambiguous_shared_result"] == 1
