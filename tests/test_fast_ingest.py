import json
import os
import shlex
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from papertrail.cli import main
from papertrail.graph import build, select
from papertrail.ingest import ingest, prefilter
from papertrail.queries import Queries
from papertrail.sources import ClaudeCodeSource, CodexSource, ReadProgress, load_sources

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures"
DEMO = ROOT / "demo"
DEMO_ROOTS = {"claude-code": DEMO / "logs/claude", "codex": DEMO / "logs/codex"}
FIXTURE_ROOTS = {"claude-code": FIXTURE / "logs", "codex": FIXTURE / "codex-sessions"}


def snapshot(roots, **options):
    sessions, diagnostics = load_sources(roots, **options)
    return [asdict(s) for s in sessions], diagnostics


@pytest.mark.parametrize("roots", [FIXTURE_ROOTS, DEMO_ROOTS], ids=["fixtures", "demo"])
def test_cached_parse_matches_uncached(roots, monkeypatch):
    expected = snapshot(roots, cache=False)
    assert snapshot(roots) == expected

    def fail(*args, **kwargs):
        pytest.fail("Warm ingest reopened a transcript")

    monkeypatch.setattr(ClaudeCodeSource, "parse", fail)
    monkeypatch.setattr(CodexSource, "raw_lines", fail)
    progress = ReadProgress()
    assert snapshot(roots, progress=progress) == expected
    for source in progress.sources.values():
        assert source["read_files"] == source["files"]
        assert source["read_bytes"] == source["bytes"] > 0


@pytest.mark.parametrize("name,source,method,pattern", [
    ("claude-code", ClaudeCodeSource, "parse", "*.jsonl"),
    ("codex", CodexSource, "parse_group", "rollout-*.jsonl"),
])
def test_parse_cache_invalidation_and_pruning(tmp_path, monkeypatch, name, source, method, pattern):
    folder = tmp_path / "logs"
    folder.mkdir()
    example = next(FIXTURE_ROOTS[name].rglob(pattern))
    path = folder / example.name
    shutil.copy(example, path)
    roots = {name: folder}
    parse = getattr(source, method)
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return parse(*args, **kwargs)

    monkeypatch.setattr(source, method, counted)
    snapshot(roots)
    snapshot(roots)
    assert len(calls) == 1
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    snapshot(roots)
    assert len(calls) == 2
    info = path.stat()
    with path.open("ab") as stream:
        stream.write(b"{}\n")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    expected = snapshot(roots, cache=False)
    assert snapshot(roots) == expected
    assert len(calls) == 4
    cache = Path(os.environ["PAPERTRAIL_CACHE"])
    for entry in cache.iterdir():
        original = entry.read_bytes()
        entry.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    assert snapshot(roots) == expected
    assert len(calls) == 5
    assert cache.stat().st_mode & 0o777 == 0o700
    path.unlink()
    snapshot(roots)
    assert list(cache.iterdir()) == []


def test_cache_refuses_project_and_repository(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    cache = project / "cache"
    monkeypatch.setenv("PAPERTRAIL_CACHE", str(cache))
    snapshot(FIXTURE_ROOTS, project=project)
    assert not cache.exists()
    (project / ".git").mkdir()
    snapshot(FIXTURE_ROOTS)
    assert not cache.exists()


def test_cache_checks_resolved_paths(tmp_path):
    logs = tmp_path / "logs"
    shutil.copytree(FIXTURE / "logs", logs)
    project = tmp_path / "project"
    project.mkdir()
    original = snapshot({"claude-code": logs})
    replacement = tmp_path / "other"
    replacement.mkdir()
    project.rmdir()
    project.symlink_to(replacement, target_is_directory=True)
    changed = snapshot({"claude-code": logs}, cache=False)
    assert changed != original
    assert snapshot({"claude-code": logs}) == changed


def write_header(path, sid, cwd, parent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": sid, "cwd": str(cwd)}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    path.write_text(json.dumps({
        "type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": payload,
    }) + "\n")
    return path


def test_prefilter_closes_over_claude_parent_and_siblings(tmp_path):
    logs = tmp_path / "logs"
    child = logs / "unrelated" / "parent.with.dot" / "subagents" / "agent-child.jsonl"
    child.parent.mkdir(parents=True)
    parent = logs / "unrelated" / "parent.with.dot.jsonl"
    sibling = child.with_name("agent-sibling.jsonl")
    unrelated = parent.with_name("different.jsonl")
    project = tmp_path / "project"
    child.write_text(json.dumps({"cwd": str(project)}))
    for file in (parent, sibling, unrelated):
        file.write_text("{}\n")
    assert prefilter({"claude-code": logs}, project) == {child, sibling, parent}


def test_prefilter_closes_over_codex_replicas_and_ancestors(tmp_path):
    logs = tmp_path / "logs"
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    child = write_header(logs / "rollout-child.jsonl", "child", project, "parent")
    replica = write_header(logs / "copy/rollout-replica.jsonl", "child", elsewhere, "parent")
    parent = write_header(logs / "rollout-parent.jsonl", "parent", elsewhere, "grandparent")
    grandparent = write_header(logs / "rollout-grandparent.jsonl", "grandparent", elsewhere)
    write_header(logs / "rollout-other.jsonl", "other", elsewhere)
    assert prefilter({"codex": logs}, project) == {child, replica, parent, grandparent}


def views(store):
    with Queries(store) as q:
        files = q.db.execute("MATCH (f:File) RETURN f.path AS path ORDER BY f.path").data()
        return q.stats(), [q.blame(row["path"]) for row in files]


def test_demo_quick_trace_and_complete_graph(tmp_path, capsys):
    # The real demo log tree is compact and every log is relevant. Add an
    # unrelated log to exercise both passes without changing the demo inputs.
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "elsewhere.jsonl").write_bytes(b"{}\n" * 50_000)
    # Keep the demo logs at their original relative paths for portable cwd
    # resolution. Copy the demo tree and add the unrelated file to it.
    demo = tmp_path / "demo"
    shutil.copytree(DEMO, demo)
    shutil.copy(claude / "elsewhere.jsonl", demo / "logs/claude/elsewhere.jsonl")
    project = demo / "project"
    config = project / "relay/config.py"
    roots = {"claude-code": demo / "logs/claude", "codex": demo / "logs/codex"}
    store = tmp_path / "two-pass.rl"
    raw = tmp_path / "single-pass.rl"
    sessions, _ = load_sources(roots, cache=False)
    build(raw, select(sessions, project))
    command = [
        "ingest", "--project", str(project), "--store", str(store),
        "--claude-logs", str(roots["claude-code"]), "--codex-logs", str(roots["codex"]),
        "--json",
    ]
    assert main([*command, "--quick-only", "--no-cache"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["complete"] is False and output["passes"] == ["quick"]
    with Queries(store) as quick, Queries(raw) as full:
        assert quick.trace(config, 6) == full.trace(config, 6)
    for action in (["stats"], ["trace", f"{config}:6"], ["blame", str(config)], ["crossings"]):
        assert main([*action, "--store", str(store), "--json"]) == 0
        output = capsys.readouterr()
        assert json.loads(output.out)["complete"] is False
        assert output.err.splitlines() == [
            "History is still incomplete; run papertrail ingest again to finish.",
        ]
    assert main(command) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["complete"] is True and result["passes"] == ["quick", "full"]
    assert list(result["pass_seconds"]) == result["passes"]
    assert len(output.err.splitlines()) == 1
    assert views(store) == views(raw)
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["passes"] == ["full"]


def test_interrupt_during_full_write_leaves_quick_graph(tmp_path, monkeypatch, capsys):
    from papertrail import graph

    claude = tmp_path / "logs"
    claude.mkdir()
    shutil.copytree(FIXTURE / "project", tmp_path / "project")
    for path in (FIXTURE / "logs/demo").iterdir():
        if path.is_file():
            shutil.copy(path, claude / path.name)
    (claude / "older.jsonl").write_bytes(b"{}\n" * 50_000)
    store = tmp_path / "graph.rl"
    publish = graph.publish_store
    published = []

    def interrupt(staged, target):
        if published:
            raise KeyboardInterrupt
        publish(staged, target)
        published.append(True)

    monkeypatch.setattr(graph, "publish_store", interrupt)
    assert main([
        "ingest", "--store", str(store), "--project", str(tmp_path / "project"),
        "--claude-logs", str(claude), "--source", "claude-code", "--no-cache", "--json",
    ]) == 130
    assert published and not list(tmp_path.glob(".papertrail-build-*"))
    assert not capsys.readouterr().out
    with Queries(store) as q:
        assert q.stats()["complete"] is False


def test_ingest_skips_preview_for_most_uncached_bytes_and_all(tmp_path):
    store = tmp_path / "graph.rl"
    for project in (DEMO / "project", None):
        assert [name for name, _, _ in ingest(store, DEMO_ROOTS, project, cache=False)] == ["full"]


def test_ui_build_accepts_missing_store_and_keeps_explicit_log_roots(tmp_path, monkeypatch):
    from papertrail import web

    seen = {}

    def serve(store, **options):
        seen.update(store=store, **options)

    monkeypatch.setattr(web, "serve", serve)
    project = tmp_path / "project"
    assert main([
        "ui", "--build", "--no-browser", "--project", str(project), "--store", str(tmp_path / "graph.rl"),
        "--claude-logs", str(DEMO_ROOTS["claude-code"]), "--codex-logs", str(DEMO_ROOTS["codex"]),
        "--portable", str(DEMO),
    ]) == 0
    assert seen["build_on_start"] and not seen["open_browser"] and seen["roots"] == DEMO_ROOTS
    assert seen["portable"] == DEMO and seen["project"] == project
    assert not seen["store"].exists()


def test_full_pass_finds_edit_via_other_symlink(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    path = project / "added.txt"
    path.write_text("first\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    transcript = logs / "session.jsonl"
    records = [
        {"message": {"content": [{
            "type": "tool_use", "name": "Write", "id": "write",
            "input": {"file_path": "added.txt", "content": "first\n"},
        }]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "write"}]},
         "toolUseResult": {"type": "create", "filePath": "added.txt", "content": "first\n"}},
    ]
    transcript.write_text("".join(
        json.dumps({"timestamp": f"2026-01-01T00:00:0{i}Z", "cwd": str(alias), **row}) + "\n"
        for i, row in enumerate(records)
    ))
    roots = {"claude-code": logs}
    assert prefilter(roots, project) == set()
    store = tmp_path / "graph.rl"
    passes = ingest(store, roots, project, cache=False)
    assert next(passes)[0] == "quick"
    with Queries(store) as q:
        assert not q.complete and q.stats()["rows"][0]["files"] == 0
    assert [phase for phase, _, _ in passes] == ["full"]
    with Queries(store) as q:
        assert q.complete and q.stats()["rows"][0]["files"] == 1
        assert q.blame(path)["rows"][0]["kind"] == "agent"


def test_cache_reparses_same_size_rewrite_with_restored_mtime(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"custom-title","customTitle":"First","timestamp":"2026-01-01T00:00:00Z"}\n')
    roots = {"claude-code": tmp_path}
    original = snapshot(roots)
    before = path.stat()
    path.write_text(path.read_text().replace("First", "Other"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (before.st_size, before.st_mtime_ns, before.st_ino)
    expected = snapshot(roots, cache=False)
    assert original != expected
    assert snapshot(roots) == expected


def test_cache_version_is_captured_before_source_changes(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location

    from papertrail import cache

    module_path = tmp_path / "cache.py"
    shutil.copyfile(cache.__file__, module_path)
    source = tmp_path / "model.py"
    for name in ("model.py", "logfile.py", "patches.py", "fragments.py"):
        (tmp_path / name).touch()

    def import_cache():
        spec = spec_from_file_location("synthetic_cache", module_path)
        loaded = module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded

    old = import_cache()
    source.write_text("# Synthetic parser revision\n")
    new = import_cache()
    assert old.ParseCache().version == old.parser_version()
    assert old.ParseCache().version != new.ParseCache().version


def test_session_warns_when_history_is_incomplete(tmp_path, capsys):
    store = tmp_path / "graph.rl"
    project = DEMO / "project"
    sessions = select(load_sources(DEMO_ROOTS, cache=False)[0], project)
    build(store, sessions, complete=False)
    assert main(["session", sessions[0].id, "--store", str(store), "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["complete"] is False
    assert output.err.splitlines() == [
        "History is still incomplete; run papertrail ingest again to finish.",
    ]


def test_ingest_keeps_complete_graph_published(tmp_path):
    from contextlib import contextmanager

    roots = {"claude-code": tmp_path / "logs"}
    project = tmp_path / "project"
    project.mkdir()
    logs = roots["claude-code"]
    logs.mkdir()
    store = tmp_path / "graph.rl"
    build(store, [])
    # Make preview significantly cheaper so the normal heuristic would use it.
    (logs / "matching.jsonl").write_text(json.dumps({"cwd": str(project)}) + "\n")
    (logs / "other.jsonl").write_bytes(b"{}\n" * 1000)
    published = []

    @contextmanager
    def publish(complete):
        with Queries(store) as graph:
            assert graph.complete
        yield
        published.append(complete)

    assert [name for name, _, _ in ingest(store, roots, project, cache=False, publish=publish)] == ["full"]
    assert published == [True]
    with pytest.raises(ValueError, match="cannot replace a complete graph"):
        list(ingest(store, roots, project, cache=False, quick_only=True, publish=publish))
    assert published == [True]
    with Queries(store) as graph:
        assert graph.complete


@pytest.mark.parametrize("complete", [False, True])
def test_launcher_finishes_an_incomplete_store(tmp_path, complete):
    import subprocess
    import sys

    project = tmp_path / "project"
    store = project / ".papertrail/graph.rl"
    build(store, [], complete=complete)
    uv = tmp_path / "uv"
    # Run the metadata probe for real, and record the UI arguments in place of
    # starting the server or opening private log roots.
    uv.write_text(f"""#!/usr/bin/env bash
case "$1" in
  sync) exit 0 ;;
  run) shift 2 ;;
  *) exit 2 ;;
esac
if [[ "$1" == python && "$2" == - && "$#" == 3 && "$3" == 8765 ]]; then
  cat >/dev/null
  echo 8765
elif [[ "$1" == papertrail && "$2" == ui ]]; then
  printf 'UI %s\\n' "$*"
elif [[ "$1" == python ]]; then
  shift
  exec {shlex.quote(sys.executable)} "$@"
else
  exit 2
fi
""")
    uv.chmod(0o700)
    launch = tmp_path / "start.sh"
    shutil.copyfile(ROOT / "start.sh", launch)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "NO_BROWSER": "1", "PORT": "8765", "PYTHONPATH": str(ROOT)}
    result = subprocess.run(["bash", str(launch), str(project)], env=env,
                            capture_output=True, text=True, check=True)
    ui = next(line for line in result.stdout.splitlines() if line.startswith("UI "))
    assert ("--build" in ui.split()) is not complete
