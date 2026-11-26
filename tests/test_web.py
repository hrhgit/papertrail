import json
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from papertrail import web
from papertrail.graph import build
from papertrail.model import Edit, Read, Replacement, Session
from papertrail.sources import ClaudeCodeSource
from papertrail.web import Reader, ReaderQueries, browse, handler

FIXTURE = Path(__file__).parent / "fixtures"
GREETING = (FIXTURE / "project/greeting.py").resolve()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "demo.rl"
    build(path, list(ClaudeCodeSource().sessions(FIXTURE / "logs")))
    return path


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    folder = tmp_path / "static"
    shutil.copytree(web.STATIC, folder)
    monkeypatch.setattr(web, "STATIC", folder)
    return folder


@pytest.fixture
def server(store, static_dir):
    ready = threading.Event()
    box = {}

    def run():
        # The Relate connection must live on the serving thread.
        reader = Reader(store)
        httpd = HTTPServer(("127.0.0.1", 0), handler(reader))
        box["httpd"], box["url"] = httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
        ready.set()
        httpd.serve_forever()
        httpd.server_close()
        reader.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(5)
    yield box["url"]
    box["httpd"].shutdown()
    thread.join(5)


def get(url):
    try:
        with urllib.request.urlopen(url) as res:
            return res.status, res.headers.get("Content-Type"), res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type"), err.read()


def test_tracks_show_every_session_crossing_a_line(store):
    with ReaderQueries(store) as q:
        view = q.file(GREETING)
    assert view["lines"] == GREETING.read_text().splitlines()
    by_start = {r["start"]: r for r in view["tracks"]}
    assert by_start[2]["kind"] == "agent"
    assert [lane["session"] for lane in by_start[2]["lanes"]][:3] == ["cedar", "birch", "amber"]
    assert by_start[2]["lanes"][0]["depth"] == 0
    assert (by_start[1]["kind"], by_start[1]["was"]) == ("inferred", "drift")
    assert (by_start[3]["kind"], by_start[3]["was"]) == ("inferred", "external")
    assert any("-[:REPLACED]->" in c for c in view["cypher"])


def test_overview(store):
    with ReaderQueries(store) as q:
        view = q.overview()
        assert view["cypher"] == q.statements
    assert "MATCH ()-[r]->() RETURN count(r) AS relationships" in view["cypher"]
    assert view["stats"]["sessions"] == 4
    assert {f["path"] for f in view["files"]} >= {str(GREETING)}
    helper = next(s for s in view["sessions"] if s["parent"])
    assert helper["parent"] == "amber"
    assert view["crossings"][0]["session_count"] == 3


def test_http_api_and_static(server):
    status, kind, body = get(server + "/")
    assert status == 200 and kind.startswith("text/html") and b"Paper Trail" in body
    for name in ("app.js", "syntax.js"):
        status, kind, _ = get(server + "/" + name)
        assert status == 200 and "javascript" in kind

    status, _, body = get(server + "/api/trace?" + urllib.parse.urlencode({"path": GREETING, "line": 2}))
    rows = json.loads(body)["rows"]
    assert status == 200 and [r["session"] for r in rows][:3] == ["cedar", "birch", "amber"]

    status, _, body = get(server + "/api/session?id=birch")
    assert status == 200 and json.loads(body)["rows"]

    status, _, body = get(server + "/api/conversation?id=birch")
    conv = json.loads(body)
    assert status == 200 and conv["items"][0]["kind"] == "user" and conv["cypher"]
    assert get(server + "/api/conversation?id=nope")[0] == 400


@pytest.mark.parametrize(
    "path, code",
    [
        ("/api/file?path=/etc/hosts", 400),  # only files that are in the graph
        ("/api/trace?path=x&line=abc", 400),
        ("/api/nope", 404),
        ("/../pyproject.toml", 404),
        ("/%2e%2e/web.py", 404),
    ],
)
def test_http_refuses(server, path, code):
    status, kind, body = get(server + path)
    assert status == code
    assert kind.startswith("application/json") and json.loads(body)["error"]


def request(url, body=None, headers=None, *, method=None):
    req = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(), headers=headers or {}, method=method,
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}") if "json" in (err.headers.get("Content-Type") or "") else {}


@pytest.mark.parametrize("method, code", [("GET", 404), ("POST", 404), ("PUT", 501)])
def test_http_unknown_endpoint_and_method(server, method, code):
    status, body = request(server + "/api/nope", headers={"X-Paper-Trail": "1"}, method=method)
    assert status == code and set(body) == {"error"} and body["error"]
    if code == 404:
        assert body["error"] == "Unknown endpoint."


@pytest.mark.parametrize("path, reader_method, method", [
    ("/api/workspace", "workspace", "GET"),
    ("/api/open", "open", "POST"),
])
def test_http_unexpected_error_is_json(server, monkeypatch, path, reader_method, method):
    def fail(*args):
        raise RuntimeError("Fictional failure")

    monkeypatch.setattr(Reader, reader_method, fail)
    status, body = request(
        server + path, {"path": "/fictional/folder"} if method == "POST" else None,
        {"X-Paper-Trail": "1"}, method=method,
    )
    assert status == 500 and set(body) == {"error"} and "Check the terminal" in body["error"]


def test_static_snapshot_stays_the_same_after_disk_edits(server, static_dir):
    original = {}
    for name in ("index.html", "app.js", "syntax.js", "app.css"):
        original[name] = (static_dir / name).read_bytes()
        (static_dir / name).write_bytes(b"Edited after the server started.")

    with urllib.request.urlopen(server + "/") as res:
        version = res.headers["X-Paper-Trail-Version"]
        assert version and res.headers["Cache-Control"] == "no-store"
        assert res.read() == original["index.html"].replace(b"__PAPER_TRAIL_VERSION__", version.encode())
    for name in ("app.js", "syntax.js", "app.css"):
        with urllib.request.urlopen(server + "/" + name) as res:
            assert res.headers["X-Paper-Trail-Version"] == version
            assert res.headers["Cache-Control"] == "no-store"
            assert res.read() == original[name]
    with urllib.request.urlopen(server + "/api/workspace") as res:
        assert res.headers["X-Paper-Trail-Version"] == version


def test_stale_page_cannot_open_folder(server, tmp_path):
    status, body = request(server + "/api/open", {"path": str(tmp_path)}, {
        "X-Paper-Trail": "1",
        "X-Paper-Trail-Version": "earlier-version",
    })
    assert status == 409 and body == {"error": "The server changed. Reload this page."}
    status, body = request(server + "/api/workspace")
    assert status == 200 and body["project"] is None


def test_untracked_file_in_folder_opens_without_tracks(store):
    usage = (FIXTURE / "project/usage.txt").resolve()
    other = FIXTURE / "project/notes.md"
    other.write_text("one\ntwo\n")
    try:
        with ReaderQueries(store) as q:
            view = q.file(other, root=(FIXTURE / "project").resolve())
            assert view["status"] == "untracked" and view["tracks"] == []
            assert view["lines"] == ["one", "two"] and view["line_count"] == 2
            with pytest.raises(ValueError):
                q.file(other)  # outside a folder, only graph files open
            with pytest.raises(ValueError):
                q.file("/etc/hosts", root=(FIXTURE / "project").resolve())
            assert q.file(usage, root=(FIXTURE / "project").resolve())["status"] != "untracked"
    finally:
        other.unlink()


def test_reader_builds_a_folder_from_logs(tmp_path):
    shutil.copytree(FIXTURE / "logs", tmp_path / "logs")
    shutil.copytree(FIXTURE / "project", tmp_path / "project")
    reader = Reader(project=tmp_path / "project")
    assert reader.workspace()["ready"] is False
    with pytest.raises(ValueError):
        reader.call("overview")
    result = reader.build(tmp_path / "project", roots={"claude-code": tmp_path / "logs"})
    assert result["ready"] and result["sessions"] >= 3
    assert Path(result["store"]) == (tmp_path / "project/.papertrail/graph.rl").resolve()
    view = reader.call("overview")
    assert view["stats"]["sessions"] >= 3
    elsewhere = tmp_path / "elsewhere.rl"
    shutil.copy(result["store"], elsewhere)
    pinned = Reader(elsewhere, tmp_path / "project")
    pinned.open(tmp_path / "logs")
    assert pinned.open(tmp_path / "project")["store"] == str(elsewhere.resolve())
    pinned.close()
    tree = reader.tree()
    assert "greeting.py" in tree["files"] and not any(f.startswith(".papertrail") for f in tree["files"])
    assert reader.file(tmp_path / "project/settings.py")["path"].endswith("settings.py")
    reader.close()


def test_browse_lists_folders_only(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "a/.papertrail").mkdir()
    (tmp_path / "a/.papertrail/graph.rl").write_text("")
    listing = browse(str(tmp_path))
    assert [d["name"] for d in listing["dirs"]] == ["a"] and listing["dirs"][0]["graph"]
    with pytest.raises(ValueError):
        browse(str(tmp_path / "file.txt"))


def test_http_workspace_actions_need_header_and_loopback(server):
    status, ws = request(server + "/api/workspace")
    assert status == 200 and ws["ready"] and ws["project"] is None
    project = str((FIXTURE / "project").resolve())
    status, body = request(server + "/api/open", {"path": project})
    assert status == 403 and body["error"]
    status, body = request(server + "/api/folders", headers={"Host": "evil.example:80"})
    assert status == 403 and body["error"]
    status, ws = request(server + "/api/open", {"path": project}, {"X-Paper-Trail": "1", "Content-Type": "application/json"})
    assert status == 200 and ws["project"] == project and ws["ready"] is False
    status, body = request(server + "/api/overview")
    assert status == 400 and "Build" in body["error"]
    status, tree = request(server + "/api/tree")
    assert status == 200 and "greeting.py" in tree["files"]
    status, body = request(server + "/api/open", {"path": "/no/such/dir"}, {"X-Paper-Trail": "1"})
    assert status == 400 and body["error"]


def test_pick_folder_uses_system_dialog(monkeypatch, tmp_path):
    import subprocess

    from papertrail import web

    calls = []

    def fake(code, out="", err=""):
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, code, out, err)

        return run

    monkeypatch.setattr(web, "picker_command", lambda start: ["dialog", start])
    monkeypatch.setattr(web.subprocess, "run", fake(0, str(tmp_path) + "/\n"))
    assert web.pick_folder(tmp_path) == str(tmp_path.resolve())
    assert calls[-1] == ["dialog", str(tmp_path)]
    web.pick_folder("/no/such/dir")
    assert calls[-1][-1] == str(Path.home())  # falls back to home
    monkeypatch.setattr(web.subprocess, "run", fake(1, err="execution error: User canceled. (-128)"))
    assert web.pick_folder() is None
    monkeypatch.setattr(web.subprocess, "run", fake(2, err="boom"))
    with pytest.raises(ValueError):
        web.pick_folder()
    monkeypatch.setattr(web, "picker_command", lambda start: None)
    with pytest.raises(ValueError):
        web.pick_folder()


def test_macos_picker_passes_path_as_argument(monkeypatch):
    from papertrail import web

    monkeypatch.setattr(web.sys, "platform", "darwin")
    monkeypatch.setattr(web.shutil, "which", lambda name: "/usr/bin/" + name)
    command = web.picker_command('/tmp/a "quoted" dir')
    assert command[0] == "osascript" and command[-1] == '/tmp/a "quoted" dir'
    assert not any('quoted' in part for part in command[:-1])


def test_http_pick_needs_header(server):
    status, _ = request(server + "/api/pick", {})
    assert status == 403


def test_reader_opens_plain_files_before_a_build(tmp_path):
    from papertrail import web

    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "a.py").write_text("one\ntwo\n")
    reader = web.Reader(project=folder)
    assert not reader.workspace()["ready"]
    shown = reader.file(str(folder / "a.py"))
    assert shown["status"] == "untracked" and shown["lines"] == ["one", "two"] and shown["tracks"] == []
    with pytest.raises(ValueError):
        reader.file(str(tmp_path / "elsewhere.py"))
    assert reader.tree()["files"] == ["a.py"]


def test_macos_picker_stays_above_other_windows():
    from papertrail import web

    assert "NSModalPanelWindowLevel" in web.PICK_SCRIPT and "orderFrontRegardless" in web.PICK_SCRIPT


def held_build(monkeypatch, phase):
    """Hold the selected pass before it reads any files."""
    from papertrail.sources import SourcePlan

    arrived, release = threading.Event(), threading.Event()
    real = SourcePlan.load

    def waiting(self, progress=None, files=None):
        if progress.phase == phase and not arrived.is_set():
            arrived.set()
            assert release.wait(10)
        return real(self, progress, files)

    monkeypatch.setattr(SourcePlan, "load", waiting)
    return arrived, release


def preview_roots(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "fixtures")
    base = tmp_path / "fixtures"
    (base / "logs/other.jsonl").write_bytes(b"{}\n" * 50_000)
    roots = {"claude-code": base / "logs", "codex": base / "codex-sessions"}
    return base / "project", roots


def finished(reader, seconds=30):
    import time

    deadline = time.monotonic() + seconds
    while (status := reader.build_status())["running"]:
        assert time.monotonic() < deadline, status
        time.sleep(0.02)
    return status


def test_background_build_reports_progress(tmp_path, monkeypatch):
    project, roots = preview_roots(tmp_path)
    greeting = project / "greeting.py"
    reader = Reader(tmp_path / "graph.rl", project)
    assert reader.build_status() == {"running": False, "stage": None}
    arrived, release = held_build(monkeypatch, "quick")
    status = reader.start_build(project, roots=roots)
    assert arrived.wait(10)
    assert status["running"] and status["phase"] == "quick" and status["error"] is None
    ws = reader.workspace()
    assert ws["building"] and not ws["ready"]
    # While it runs, files open as plain text and nothing switches folders.
    assert reader.file(str(greeting))["tracks"] == []
    with pytest.raises(ValueError, match="still building"):
        reader.call("overview")
    with pytest.raises(ValueError, match="Wait"):
        reader.open(FIXTURE)
    with pytest.raises(ValueError, match="Another folder"):
        reader.start_build(FIXTURE, roots=roots)
    job = reader.job
    assert reader.start_build(project, roots=roots)["running"] and reader.job is job
    release.set()
    status = finished(reader)
    assert status["phase"] == "done" and status["complete"] and status["fraction"] == 1.0
    assert [s["name"] for s in status["sources"]] == ["claude-code", "codex"]
    for source in status["sources"]:
        assert source["files"] and source["read_files"] == source["files"]
        assert source["read_bytes"] == source["bytes"] > 0 and source["sessions"] >= 1
    assert status["sessions_read"] >= status["sessions_kept"] >= 3 and status["files"] >= 1
    result = status["result"]
    assert result["ready"] and not result["building"] and result["sessions"] == status["sessions_kept"]
    assert Path(result["store"]) == tmp_path / "graph.rl"
    assert reader.call("overview")["stats"]["sessions"] >= 3
    assert reader.file(str(greeting))["tracks"]
    reader.close()


def test_background_build_failure_is_reported(tmp_path, monkeypatch):
    from papertrail import ingest

    (tmp_path / "project").mkdir()
    reader = Reader(project=tmp_path / "project")

    def broken(store, sessions, **kwargs):
        raise ValueError("Disk said no.")

    monkeypatch.setattr(ingest, "build", broken)
    reader.start_build(tmp_path / "project", roots={"claude-code": FIXTURE / "logs"})
    status = finished(reader)
    assert status["stage"] == "failed" and status["error"] == "Disk said no." and "result" not in status
    assert not reader.workspace()["ready"]
    reader.close()


def test_unexpected_build_failure_logs_frames_without_message(tmp_path, monkeypatch, capsys):
    from papertrail import ingest

    (tmp_path / "project").mkdir()
    reader = Reader(project=tmp_path / "project")

    def broken(store, sessions, **kwargs):
        raise KeyError("-".join(["private", "prompt"]))

    monkeypatch.setattr(ingest, "build", broken)
    reader.start_build(tmp_path / "project", roots={"claude-code": FIXTURE / "logs"})
    assert finished(reader)["stage"] == "failed"
    reader.close()
    err = capsys.readouterr().err
    assert "Paper Trail build failed (KeyError)." in err and "in broken" in err
    assert "private-prompt" not in err


def test_closing_the_reader_stops_a_running_build(tmp_path, monkeypatch):
    (tmp_path / "project").mkdir()
    reader = Reader(project=tmp_path / "project")
    arrived, release = held_build(monkeypatch, "full")
    reader.start_build(tmp_path / "project", roots={"claude-code": FIXTURE / "logs"})
    assert arrived.wait(10)
    threading.Timer(0.1, release.set).start()
    reader.close()
    assert not reader.building and reader.build_status()["phase"] == "cancelled"
    store_dir = tmp_path / "project/.papertrail"
    with ReaderQueries(store_dir / "graph.rl") as graph:
        assert not graph.complete
    assert not list(store_dir.glob(".papertrail-build-*"))


@pytest.mark.parametrize("action", ["finish", "switch", "rebuild", "reopen"])
def test_reader_uses_quick_graph_during_full_pass(tmp_path, monkeypatch, action):
    project, roots = preview_roots(tmp_path)
    arrived, release = held_build(monkeypatch, "full")
    reader = Reader(project=project)
    try:
        reader.start_build(project, roots=roots)
        assert arrived.wait(10)
        first = reader.job
        status = first.status()
        assert status["running"] and status["phase"] == "full"
        assert status["ready"] and not status["complete"]
        assert status["sessions_read"] is None and not status["sources"]
        assert reader.workspace()["ready"]
        assert not reader.overview()["complete"]
        assert reader.file(project / "greeting.py")["tracks"]
        preview_version = reader.graph_version
        if action == "switch":
            assert not reader.open(tmp_path)["building"]
        elif action == "rebuild":
            reader.start_build(project, roots=roots)
            assert reader.job is not first
        elif action == "reopen":
            assert reader.open(project)["building"]
            assert reader.job is first and first.running
        release.set()
        if action not in ("finish", "reopen"):
            assert first.ended.wait(10) and first.status()["phase"] == "cancelled"
        if action == "switch":
            assert not reader.open(project)["complete"]
            assert not reader.overview()["complete"]
            reader.start_build(project)
        assert finished(reader)["complete"]
        assert reader.overview()["complete"]
        assert reader.graph_version != preview_version
        assert not list((project / ".papertrail").glob(".papertrail-build-*"))
    finally:
        release.set()
        reader.close()


def test_load_sources_reports_every_file():
    from papertrail.sources import ReadProgress, load_sources

    progress = ReadProgress()
    sessions, _ = load_sources({"claude-code": FIXTURE / "logs", "codex": FIXTURE / "codex-sessions"}, progress)
    assert progress.fraction() == 1.0
    assert sum(s["sessions"] for s in progress.sources.values()) == len(sessions)
    nothing = ReadProgress()
    load_sources({"codex": FIXTURE / "no-such-dir"}, nothing)
    assert nothing.sources["codex"]["missing"] and nothing.fraction() == 1.0


def test_http_build_status_without_a_build(server):
    status, body = request(server + "/api/build")
    assert status == 200 and body == {"running": False, "stage": None}


def test_progress_moves_inside_large_logs(monkeypatch):
    from papertrail.sources import ReadProgress, claude, codex, load_sources

    monkeypatch.setattr(claude, "READ_CHUNK", 1)
    monkeypatch.setattr(codex, "READ_CHUNK", 1)

    class Seen(ReadProgress):
        partial_calls = 0

        def read(self, name, path, nbytes=None):
            self.partial_calls += nbytes is not None
            super().read(name, path, nbytes)
            source = self.sources[name]
            assert source["read_bytes"] <= source["bytes"]

    progress = Seen()
    load_sources({"claude-code": FIXTURE / "logs", "codex": FIXTURE / "codex-sessions"}, progress)
    assert progress.partial_calls > 10 and not progress.partial
    for source in progress.sources.values():
        assert source["read_bytes"] == source["bytes"] and source["read_files"] == source["files"]


def test_file_lists_deleted_spots_with_their_writers(tmp_path):
    from papertrail.model import Edit, Session

    path = (tmp_path / "cut.py").resolve()
    texts = ["", "a\nb\nc\n", "a\nc\n"]
    path.write_text(texts[-1])
    sessions = []
    for i, (old, new) in enumerate(zip(texts, texts[1:])):
        e = Edit(f"e{i}", 1, f"2025-01-01T00:00:0{i}Z", "Edit", str(path), None, "m", old, new)
        sessions.append(Session(f"s{i}", "fiction", "/synthetic", edits=[e]))
    store = tmp_path / "cut.rl"
    build(store, sessions)
    with ReaderQueries(store) as q:
        view = q.file(path)
    (gap,) = view["gaps"]
    assert gap["after"] == 1 and gap["session"] == "s1" and gap["edit"] == "e1"
    assert gap["kind"] == "agent" and gap["removed"] == 1 and gap["text"] == "b\n"
    assert [(w["session"], w["edit"], w["lines"]) for w in gap["wrote"]] == [("s0", "e0", 1)]


def test_search_marks_each_line_with_its_writer(store):
    project = (FIXTURE / "project").resolve()
    reader = Reader(store, project)
    try:
        found = reader.search("GREET")
        files = {Path(f["path"]).name: f for f in found["files"]}
        (hit,) = files["greeting.py"]["hits"]
        assert (hit["line"], hit["kind"], hit["session"]) == (2, "agent", "cedar")
        assert hit["text"][hit["at"]:hit["at"] + hit["size"]] == "greet"
        assert files["usage.txt"]["hits"][0]["session"] == "amber/agent-helper"
        # Instructions that mention it, newest first, each with the edits it led to.
        assert [(p["session"], p["edits"]) for p in found["prompts"]] == [("cedar", 1), ("amber", 2), ("amber", 1)]
        assert all(p["text"][p["at"]:p["at"] + p["size"]].lower() == "greet" for p in found["prompts"])
        assert any("CONTAINS" in statement for statement in found["cypher"])

        exact = reader.search("GREET", case=True)
        assert exact["files"] == [] and exact["prompts"] == []
        assert reader.search("Welcome visitors")["prompts"][0]["session"] == "birch"
        with pytest.raises(ValueError):
            reader.search("  ")
    finally:
        reader.close()


def test_search_finds_deleted_lines_and_skips_stale_owners(tmp_path):
    from papertrail.model import Edit, Session

    path = (tmp_path / "cut.py").resolve()
    texts = ["", "keep one\nremove_me = 1\nkeep two\n", "keep one\nkeep two\n"]
    path.write_text(texts[-1])
    sessions = []
    for i, (old, new) in enumerate(zip(texts, texts[1:])):
        e = Edit(f"e{i}", 1, f"2025-01-01T00:00:0{i}Z", "Edit", str(path), None, "m", old, new)
        sessions.append(Session(f"s{i}", "fiction", "/synthetic", edits=[e]))
    store = tmp_path / "cut.rl"
    build(store, sessions)
    reader = Reader(store, tmp_path)
    try:
        found = reader.search("remove_me")
        assert found["files"] == []
        (gone,) = found["deleted"]
        assert (gone["path"], gone["after"], gone["session"]) == (str(path), 1, "s1")
        assert gone["text"] == "remove_me = 1"

        assert reader.search("keep two")["files"][0]["hits"][0]["session"] == "s0"
        # Once the file changes on disk, the graph's line numbers can't be trusted.
        path.write_text("new top\nkeep one\nkeep two\n")
        (file,) = reader.search("keep two")["files"]
        assert file["stale"] and "session" not in file["hits"][0]
    finally:
        reader.close()


def test_search_without_a_graph_is_a_plain_folder_search(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n" + "pad " * 30 + "needle here\n" + "needle\n" * 60)
    (tmp_path / "b.bin").write_bytes(b"\xff\xfeneedle")
    reader = Reader(project=tmp_path)
    (file,) = reader.search("needle")["files"]
    assert file["count"] == 61 and len(file["hits"]) == web.SEARCH_FILE_HITS and not file["tracked"]
    first = file["hits"][0]
    assert first["line"] == 2 and first["cut"] and first["text"][first["at"]:].startswith("needle here")

    monkeypatch.setattr(web, "SEARCH_HITS", 1)
    (tmp_path / "c.txt").write_text("needle\n")
    assert reader.search("needle")["truncated"]


def test_http_search(server):
    status, _, raw = get(server + "/api/search?q=greeting")
    body = json.loads(raw)
    assert status == 200 and body["query"] == "greeting" and body["prompts"] and body["files"]
    status, _, raw = get(server + "/api/search")
    assert status == 400 and "q" in json.loads(raw)["error"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's tokenizer")
def test_syntax_tokens():
    import subprocess

    cases = {
        "a.py": ['@cache', 'def greet(name):  # hi', '    """Doc', '    still doc"""', "    return f'{name}' + str(42)"],
        "a.js": ["const re = /a\\/b/g, half = n / 2; // c", "function go() { return `x", "y`; }"],
        "a.json": ['{"key": "value", "n": 1.5, "ok": true}'],
        "a.css": ["a:hover { color: #fff; } /* c */"],
        "a.html": ['<script src="x.js">if (a<b) {}</script><p class="k">'],
        "a.unknown": ["plain text"],
    }
    script = (
        "const S = require(process.argv[1]); const cases = JSON.parse(process.argv[2]); const out = {};"
        "for (const [path, lines] of Object.entries(cases)) { const t = S.tokens(lines, path);"
        "out[path] = t && t.map((list, i) => list.map(([a, b, type]) => [lines[i].slice(a, b), type])); }"
        "console.log(JSON.stringify(out));"
    )
    run = subprocess.run(
        ["node", "-e", script, str(web.STATIC / "syntax.js"), json.dumps(cases)],
        capture_output=True, text=True, check=True,
    )
    out = {path: [[tuple(token) for token in line] for line in lines] if lines else lines
           for path, lines in json.loads(run.stdout).items()}
    assert out["a.unknown"] is None
    assert out["a.py"] == [
        [("@cache", "meta")],
        [("def", "keyword"), ("greet", "def"), ("# hi", "comment")],
        [('"""Doc', "string")],
        [('    still doc"""', "string")],
        [("return", "keyword"), ("f'{name}'", "string"), ("str", "call"), ("42", "number")],
    ]
    assert out["a.js"] == [
        [("const", "keyword"), ("/a\\/b/g", "string"), ("2", "number"), ("// c", "comment")],
        [("function", "keyword"), ("go", "def"), ("return", "keyword"), ("`x", "string")],
        [("y`", "string")],
    ]
    assert out["a.json"] == [[('"key"', "prop"), ('"value"', "string"), ('"n"', "prop"), ("1.5", "number"),
                              ('"ok"', "prop"), ("true", "const")]]
    assert out["a.css"] == [[("color", "prop"), ("#fff", "number"), ("/* c */", "comment")]]
    assert [text for text, kind in out["a.html"][0] if kind == "tag"] == ["<script", ">", "</script", ">", "<p", ">"]


def test_search_for_a_name_lists_whole_word_uses_and_marks_the_definition(tmp_path):
    (tmp_path / "a.py").write_text("from b import load\n\nloaded = load(1)\nreload(loaded)\n")
    (tmp_path / "b.py").write_text("def preload():\n    pass\n\n\ndef load(n):\n    return n\n")
    reader = Reader(project=tmp_path)
    found = reader.search("load", case=True, word=True)
    assert found["word"] and [Path(f["path"]).name for f in found["files"]] == ["b.py", "a.py"]
    by_file = {Path(f["path"]).name: [(h["line"], h.get("def", False)) for h in f["hits"]] for f in found["files"]}
    assert by_file == {"b.py": [(5, True)], "a.py": [(1, False), (3, False)]}
    assert reader.search("LOAD", case=True, word=True)["files"] == []

    defines = web.definer("foo")
    for line in ["def foo(x):", "function foo() {", "const foo = 1", "  foo: int = 3", "func (r *T) foo(a int) {", "  async foo(a) {"]:
        assert defines(line), line
    for line in ["foo(1)", "x = foo", "if foo == 3:", "bar.foo = 3", "def food():", "if (foo(x)) {"]:
        assert not defines(line), line


def test_every_element_the_page_script_looks_up_exists():
    page = (web.STATIC / "index.html").read_text()
    script = (web.STATIC / "app.js").read_text()
    made = set(re.findall(r'\bid: "([\w-]+)"', script))
    for name in set(re.findall(r'\$\("([\w-]+)"\)', script)):
        assert f'id="{name}"' in page or name in made, name


def test_overview_counts_the_lines_each_session_still_owns(store):
    with ReaderQueries(store) as q:
        sessions = {s["id"]: (s["written"], s["alive"]) for s in q.overview()["sessions"]}
        assert sessions == {"amber": (2, 0), "amber/agent-helper": (1, 1), "birch": (2, 1), "cedar": (1, 1)}
        amber = q.conversation("amber")
        assert [(r["session"], r["lines"]) for r in amber["replaced"]] == [("birch", 1), ("amber", 1)]
        assert amber["alive_in"] == []
        (kept,) = q.conversation("cedar")["alive_in"]
        assert (kept["path"], kept["lines"], kept["line"]) == (str(GREETING), 1, 2)


def test_session_line_fates_account_for_every_written_line(tmp_path):
    def edit(n, file, **change):
        return Edit(str(n), n, f"2025-01-01T00:00:{n:02}Z", "Edit", str(file), None, "demo", **change)

    main = tmp_path / "main.txt"
    original = "keep\nself\nother\nexternal\ndrift\nother-delete\n"
    self_changed = original.replace("self\n", "self-v2\n")
    other_changed = self_changed.replace("other\n", "other-v2\n").replace("other-delete\n", "")
    external = other_changed.replace("external\n", "ext-v2\n")
    main.write_text(external.replace("drift\n", "drift-v2\n"))

    missing = tmp_path / "missing.txt"
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_bytes(b"\xff")
    ambiguous = tmp_path / "ambiguous.txt"
    ambiguous.write_text("unique\ngap\nunique\n")
    old = tmp_path / "old.txt"
    moved = tmp_path / "moved.txt"
    moved.write_text("moves\nreplacement\n")
    deleted = tmp_path / "deleted.txt"
    unplaced = tmp_path / "unplaced.txt"
    unplaced.write_text("same\nsame\n")

    first = Session("first", "demo", str(tmp_path / "first.jsonl"), edits=[
        edit(1, main, before="", after=original),
        edit(2, main, before=original, after=self_changed),
        edit(5, missing, before="", after="one\ntwo\n"),
        edit(6, unreadable, before="", after="one\ntwo\nthree\n"),
        edit(7, ambiguous, patch="@@ -4 +4 @@\n-old\n+unique"),
        edit(8, old, before="", after="moves\nreplaced there\n"),
        edit(11, deleted, before="", after="agent-delete\n"),
        edit(13, unplaced, replacement=Replacement("old", "same")),
    ], reads=[Read(str(main), "2025-01-01T00:00:04Z", 4, 1, external.splitlines(True), True)])
    other = Session("other", "demo", str(tmp_path / "other.jsonl"), edits=[
        edit(3, main, before=self_changed, after=other_changed),
        edit(9, moved, operation="move", moved_from=str(old)),
        edit(10, moved, before="moves\nreplaced there\n", after=moved.read_text()),
        edit(12, deleted, operation="delete"),
    ])
    path = tmp_path / "fate.rl"
    build(path, [first, other])
    with ReaderQueries(path) as q:
        (summary,) = [row for row in q.overview()["sessions"] if row["id"] == "first"]
        touched = {row["path"]: row["touched"] for row in q.overview()["files"]}
        conv = q.conversation("first")
        assert q._run("MATCH (e:Edit {id:'13', unplaced:true}) RETURN e.id AS id") == [{"id": "13"}]

    assert (summary["written"], summary["alive"]) == (16, 3)
    # The mover belongs to the file it moved away; the first writer follows its line to the new path.
    assert touched[str(old)] == ["first", "other"]
    assert touched[str(moved)] == ["first", "other"]
    assert touched[str(deleted)] == ["first", "other"]
    assert {row["path"]: row["lines"] for row in conv["alive_in"]} == {str(main): 2, str(moved): 1}
    assert {(r["session"], r["kind"]): r["lines"] for r in conv["replaced"]} == {
        ("first", "agent"): 1, ("other", "agent"): 4, (None, "inferred"): 2,
    }
    assert {r["kind"]: r["lines"] for r in conv["unaccounted"]} == {
        "missing": 2, "unreadable": 3, "unknown": 1,
    }
    assert summary["written"] == summary["alive"] + sum(r["lines"] for r in conv["replaced"] + conv["unaccounted"])


def test_span_lists_each_instruction_behind_a_run_of_lines(store, server):
    with ReaderQueries(store) as q:
        rows = q.span(GREETING, 1, 3)["rows"]
        # Newest first; only the stops at depth 0 still own lines in the span.
        assert [(r["kind"], r["was"], r["session"], r["lines"]) for r in rows] == [
            ("agent", None, "cedar", 1),
            ("agent", None, "birch", 0),
            ("inferred", "external", "amber", 1),
            ("agent", None, "amber", 0),
            ("agent", None, "amber", 0),
            ("inferred", "drift", "cedar", 1),
        ]
        assert rows[0]["prompt"].startswith("Use a morning greeting") and rows[0]["edits"] == 1
        assert q.span(GREETING, 2, 99)["end"] == 3
        with pytest.raises(ValueError):
            q.span(GREETING, 9, 12)
    query = urllib.parse.urlencode({"path": GREETING, "start": 2, "end": 2})
    status, _, body = get(server + "/api/span?" + query)
    assert status == 200 and [r["session"] for r in json.loads(body)["rows"]][:2] == ["cedar", "birch"]


def test_reader_answers_from_complete_graph_throughout_rebuild(tmp_path, monkeypatch):
    from papertrail.graph import select

    project, roots = preview_roots(tmp_path)
    path = project / ".papertrail/graph.rl"
    build(path, select(list(ClaudeCodeSource().sessions(roots["claude-code"])), project))
    reader = Reader(path, project)
    arrived, release = held_build(monkeypatch, "full")
    greeting = project / "greeting.py"
    try:
        before = reader.overview()
        original = reader.file(greeting)["tracks"]
        version = reader.graph_version
        reader.start_build(project, roots=roots)
        assert arrived.wait(10)
        status = reader.build_status()
        assert status["running"] and status["phase"] == "full" and status["complete"]
        assert reader.overview() == before and reader.file(greeting)["tracks"] == original
        assert reader.graph_version == version
        release.set()
        assert finished(reader)["complete"]
        assert reader.overview()["complete"]
        assert reader.graph_version != version
    finally:
        release.set()
        reader.close()
