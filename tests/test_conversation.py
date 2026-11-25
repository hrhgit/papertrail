"""The reader's conversation view: a session's log read back, steps joined to edits."""

import json
import shutil
from pathlib import Path

import pytest

from papertrail.graph import build
from papertrail.sources import ClaudeCodeSource, CodexSource
from papertrail.transcript import Steps, attach, codex_items, receipt_owner
from papertrail.web import ReaderQueries

FIXTURE = Path(__file__).parent / "fixtures"
GREETING = (FIXTURE / "project/greeting.py").resolve()


@pytest.fixture
def mixed(tmp_path):
    path = tmp_path / "mixed.rl"
    sessions = list(ClaudeCodeSource().sessions(FIXTURE / "logs"))
    sessions += list(CodexSource().sessions(FIXTURE / "codex-sessions"))
    build(path, sessions)
    return path


def tools(conv):
    return [i for i in conv["items"] if i["kind"] == "tool"]


def test_claude_conversation_marks_the_step_that_wrote_the_line(mixed):
    q = ReaderQueries(mixed)
    rows = q.trace(str(GREETING), 2)["rows"]
    birch = next(r for r in rows if r["session"] == "birch")
    conv = q.conversation("birch")
    assert conv["items"][0] == {**conv["items"][0], "kind": "user", "text": "Welcome visitors by name."}
    step = next(t for t in tools(conv) if any(e["id"] == birch["edit"] for e in t["edits"]))
    assert step["name"] == "Edit" and step["call_id"] == "call-13"
    assert "+welcome" in step["diff"] and "-hi" in step["diff"]
    assert {e["id"] for t in tools(conv) for e in t["edits"]} == {e["id"] for e in conv["edits"]}


def test_codex_conversation_joins_patch_to_its_call(mixed):
    q = ReaderQueries(mixed)
    conv = q.conversation("maple")
    assert [i["kind"] for i in conv["items"]][:2] == ["user", "tool"]
    assert conv["items"][0]["text"] == "Try a sunrise greeting."
    (step,) = [t for t in tools(conv) if t["edits"]]
    assert step["name"] == "apply_patch" and step["summary"] == "greeting.py"
    assert "+greeting = \"sunrise\"" in step["diff"] and step["output"].startswith("Success.")


def test_missing_log_is_a_note_not_an_error(mixed, tmp_path):
    q = ReaderQueries(mixed)
    log = Path(q.conversation("cedar")["session"]["log_path"])
    moved = tmp_path / "cedar.jsonl"
    shutil.move(log, moved)
    try:
        conv = q.conversation("cedar")
    finally:
        shutil.move(moved, log)
    assert conv["items"] == [] and "no longer on disk" in conv["note"]
    with pytest.raises(ValueError):
        q.conversation("no-such-session")


def test_wrapper_index_and_receipt_ids_find_their_call():
    steps = Steps()
    run = steps.tool(3, "", "exec", "call_a", "tools.exec_command({cmd:`cat > a.py <<'EOF'\nx\nEOF`})", "")
    steps.tool(5, "", "exec", "call_b", "tools.exec_command({cmd:`ls`})", "")
    # A shell receipt can arrive after the call returned; its command names the call.
    owner = receipt_owner(steps, {"command": ["/bin/zsh", "-lc", "cat > a.py <<'EOF'\nx\nEOF"]})
    assert owner is run
    steps.calls["exec-1"] = owner
    attach(
        steps,
        [
            {"id": "s:sh:call_a:0:00000000", "call_id": "call_a:0", "seq": 9, "path": "/a.py"},
            {"id": "s:sh:exec-1:00000000", "call_id": "exec-1", "seq": 9, "path": "/a.py"},
            {"id": "s:e:zzz", "call_id": "", "seq": 4, "path": "/b.py"},
        ],
    )
    assert [e["path"] for e in run["edits"]] == ["/a.py", "/a.py", "/b.py"]


def test_codex_output_unwraps_script_results(tmp_path):
    log = tmp_path / "rollout-x.jsonl"
    records = [
        {"timestamp": "2025-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "s", "cwd": "/"}},
        {"timestamp": "2025-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": 'tools.exec_command({cmd:"pytest -q"})'}},
        {
            "timestamp": "2025-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c",
                "output": [
                    {"type": "input_text", "text": "Script completed\nOutput:\n"},
                    {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "3 passed"})},
                ],
            },
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    (step,) = codex_items(log, "s").items
    assert step["summary"] == "pytest -q" and step["output"] == "3 passed" and not step["error"]


def test_js_wrapped_patch_reads_as_a_patch(tmp_path):
    log = tmp_path / "rollout-y.jsonl"
    code = "text(await tools.apply_patch(`*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n+x = 2\n*** End Patch`));\n"
    records = [
        {"timestamp": "2025-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "s", "cwd": "/"}},
        {"timestamp": "2025-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": code}},
    ]
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    (step,) = codex_items(log, "s").items
    assert step["name"] == "apply_patch" and step["summary"] == "a.py"
    assert "+x = 2" in step["diff"] and "-x = 1" in step["diff"]


def test_step_without_a_diff_shows_the_lines_it_left(mixed):
    q = ReaderQueries(mixed)
    conv = q.conversation("birch")
    step = next(t for t in tools(conv) if t["edits"])
    step["diff"] = None
    q._written([step])
    assert step["diff_from_graph"] and "+" in step["diff"] and step["diff"].startswith("@@ greeting.py")
