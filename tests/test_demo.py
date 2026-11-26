"""The bundled demo: its logs reproduce, replay cleanly, and tell the story the README tells."""

import filecmp
import subprocess
import sys
from pathlib import Path

import pytest

from papertrail.graph import build, select
from papertrail.queries import Queries
from papertrail.sources import load_sources

ROOT = Path(__file__).parent.parent
DEMO = ROOT / "demo"
PROJECT = (DEMO / "project").resolve()
CONFIG = PROJECT / "relay/config.py"


def files(root):
    # The graph file is checked by what it answers. Its bytes differ between builds.
    skip = {"graph.rl", ".pytest_cache", "__pycache__"}
    return sorted(
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and not skip & set(p.relative_to(root).parts)
    )


def test_demo_is_what_the_script_writes(tmp_path):
    made = subprocess.run(
        [sys.executable, ROOT / "scripts/make_demo.py", tmp_path], capture_output=True, text=True
    )
    assert made.returncode == 0, made.stderr
    names = files(tmp_path)
    assert names == files(DEMO) and len(names) == 17
    assert filecmp.cmpfiles(tmp_path, DEMO, names, shallow=False)[1:] == ([], [])


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    roots = {"claude-code": DEMO / "logs/claude", "codex": DEMO / "logs/codex"}
    parsed, diagnostics = load_sources(roots)
    assert not any(d.get("failures") for d in diagnostics.values())
    store = tmp_path_factory.mktemp("demo") / "graph.rl"
    build(store, select(parsed, PROJECT))
    with Queries(store) as q:
        yield q


def test_every_line_has_a_writer(graph):
    stats = graph.stats()["rows"][0]
    assert stats["sessions"] == 8 and stats["agents"] == 2
    assert stats["lines"] == {
        "agent": 199, "inferred": 4, "baseline": 0, "external": 0, "drift": 0, "unknown": 0,
    }
    assert stats["agent_fraction"] == 199 / 203
    assert stats["agent_plus_inferred_fraction"] == 1
    assert stats["unapplied_patches"] == 0
    assert all(agent["unplaced"] == 0 for agent in stats["by_agent"].values())


def test_retry_limit_story(graph):
    """Three sessions and two agents set the limit: 5 to start, 8 for an outage, 3 after an incident."""
    line = CONFIG.read_text().splitlines().index("MAX_ATTEMPTS = 3") + 1
    rows = graph.trace(CONFIG, line)["rows"]
    assert [r["depth"] for r in rows] == [0, 1, 2]
    assert [r["agent"] for r in rows] == ["claude-code", "codex", "claude-code"]
    assert [r["title"] for r in rows] == [
        "Incident 2291: duplicate charges",
        "Raise the retry limit",
        "Scaffold the webhook relay",
    ]
    assert "Cap attempts at 3 until receivers can drop repeats" in rows[0]["prompt"]
    # Following the incident session forward finds the idempotency key it added next.
    assert str(PROJECT / "relay/delivery.py") in rows[0]["next_files"]
    busiest = graph.crossings()["rows"][0]
    assert (busiest["path"], busiest["start"], busiest["session_count"]) == (str(CONFIG), line, 3)


def test_hand_edit_subagent_and_deletion(graph):
    (row,) = [r for r in graph.blame(CONFIG)["rows"] if r["was"] == "external"]
    assert (row["kind"], row["basis"], row["start"], row["end"]) == ("inferred", "nearby", 9, 10)
    # The tests were written by a subagent. Its lines lead back to the parent's instruction.
    row = graph.blame(PROJECT / "tests/test_delivery.py", 1, 1)["rows"][0]
    assert row["prompt"].startswith("Add tests for the retry loop.")
    spawned = graph.db.execute(
        "MATCH (p:Session)-[:SPAWNED]->(c:Session) RETURN p.title AS parent, c.title AS child"
    ).data()
    assert spawned == [{"parent": "Scaffold the webhook relay", "child": "Write delivery tests"}]
    gaps = graph.db.execute(
        "MATCH (f:File)-[:HAS_GAP]->(:Gap)-[:FROM]->(h:Hunk)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s) "
        "RETURN f.path AS path, h.removed_text AS text, s.title AS title"
    ).data()
    assert gaps == [
        {
            "path": str(PROJECT / "relay/delivery.py"),
            "text": '        "X-Signature": sign(body),\n',
            "title": "Rotate signing secrets",
        }
    ]


def test_shipped_graph_opens_wherever_the_folder_is(tmp_path):
    import shutil

    store = PROJECT / ".papertrail/graph.rl"
    assert b"/Users/" not in store.read_bytes() and b"/home/" not in store.read_bytes()
    before = store.stat().st_mtime_ns
    moved = tmp_path / "elsewhere"
    shutil.copytree(DEMO, moved, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    for demo in (DEMO, moved):
        config = (demo / "project/relay/config.py").resolve()
        with Queries(demo / "project/.papertrail/graph.rl") as q:
            assert q.stats()["rows"][0]["sessions"] == 8
            trace = q.trace(config, 6, limit=8)
            assert not trace["stale"]
            assert trace["rows"][0]["title"] == "Incident 2291: duplicate charges"
            assert str(demo.resolve()) in trace["rows"][0]["log_path"]
            assert q.crossings(limit=1)["rows"][0]["path"] == str(config)
    assert store.stat().st_mtime_ns == before
    # A rebuild in place stays portable.
    parsed, _ = load_sources({"claude-code": moved / "logs/claude", "codex": moved / "logs/codex"})
    target = moved / "project/.papertrail/graph.rl"
    build(target, select(parsed, (moved / "project").resolve()))
    assert str(tmp_path).encode() not in target.read_bytes()


def test_demo_project_tests_pass():
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
