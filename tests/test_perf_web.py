"""Regression tests for the reader's request scheduling and query caches."""

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest
import relate

from papertrail import web
from papertrail.graph import build
from papertrail.model import Edit, Session
from papertrail.queries import file_fingerprint



@pytest.fixture
def history(tmp_path):
    path = tmp_path / "notes.txt"
    log = tmp_path / "session.jsonl"
    log.write_text("")
    path.write_text("keep one\nkeep two\n")
    store = tmp_path / "graph.rl"

    def write_graph(suffix=""):
        first = Edit("first", 1, "2025-01-01T00:00:01Z", "Edit", str(path), None, "m",
                     "", "keep one\nremove me\nkeep two\n")
        second = Edit("second", 2, "2025-01-01T00:00:02Z", "Edit", str(path), None, "m",
                      first.after, "keep one\nkeep two\n")
        build(store, [Session("writer", "fiction", str(log), cwd=str(tmp_path),
                              title="Writer" + suffix, edits=[first, second])])

    write_graph()
    return store, path, log, write_graph


@pytest.fixture
def running(history):
    store, path, _, _ = history
    reader = web.Reader(store, path.parent)
    with ThreadingHTTPServer(("127.0.0.1", 0), web.handler(reader)) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield reader, server
        finally:
            server.shutdown()
            thread.join()
            reader.close()


def fetch(server, endpoint, **params):
    url = f"http://127.0.0.1:{server.server_port}/api/{endpoint}?{urlencode(params)}"
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def hold_scan(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = web.finder
    scanned = []

    def finder(query, case, word=False):
        match = original(query, case, word)

        def wait(line):
            scanned.append(line)
            entered.set()
            assert release.wait(10)
            return match(line)

        return wait

    monkeypatch.setattr(web, "finder", finder)
    monkeypatch.setattr(web, "SEARCH_SECONDS", float("inf"))
    return entered, release, scanned


def test_slow_search_does_not_block_file_trace_or_progress(running, history, monkeypatch):
    _, server = running
    _, path, _, _ = history
    entered, release, _ = hold_scan(monkeypatch)
    with ThreadPoolExecutor() as clients:
        search = clients.submit(fetch, server, "search", q="keep")
        try:
            assert entered.wait(10)
            assert fetch(server, "file", path=path)["lines"] == ["keep one", "keep two"]
            assert fetch(server, "trace", path=path, line=1)["rows"]
            assert fetch(server, "build") == {"running": False, "stage": None}
            assert not search.done()
        finally:
            release.set()
        assert search.result(timeout=10)["files"]


def test_disconnect_releases_search_inside_a_file(running, history, monkeypatch):
    reader, server = running
    _, path, _, _ = history
    path.write_text("look here\n" * 512)
    entered, release, scanned = hold_scan(monkeypatch)
    returned, graph_phase = threading.Event(), threading.Event()
    search = reader.search

    def observe(*args, **kwargs):
        try:
            return search(*args, **kwargs)
        finally:
            returned.set()

    def unexpected_graph_phase(*args, **kwargs):
        graph_phase.set()
        raise AssertionError("A disconnected search requested more graph data")

    monkeypatch.setattr(reader, "search", observe)
    monkeypatch.setattr(web.ReaderQueries, "search_history", unexpected_graph_phase)
    try:
        with socket.create_connection(server.server_address, timeout=10) as client:
            client.sendall(b"GET /api/search?q=look HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert entered.wait(10)
            client.shutdown(socket.SHUT_RDWR)
    finally:
        release.set()
    assert returned.wait(10)
    assert not graph_phase.is_set()
    assert 0 < len(scanned) < 512
    assert fetch(server, "overview")["stats"]["sessions"] == 1


LEGACY_TRACKS = """
MATCH (f:File {id:$path})-[:HAS_RANGE]->(r:Range)-[:FROM]->(owner:Hunk)
OPTIONAL MATCH chain=(owner)-[:REPLACED*0..]->(h:Hunk)
OPTIONAL MATCH (h)<-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
WITH r, owner, s, min(length(chain)) AS depth
ORDER BY r.start, depth
WITH r, owner, collect(CASE WHEN s IS NULL THEN NULL ELSE {session: s.id, depth: depth} END) AS lanes
RETURN r.start AS start, r.end AS end, owner.kind AS kind, owner.id AS hunk, owner.ts AS ts, lanes
ORDER BY start"""

LEGACY_CROSSINGS = """
MATCH (f:File)-[:HAS_HISTORY]->(tip:Hunk)-[:REPLACED*0..]->(h:Hunk)
      <-[:HAS_HUNK]-(:Edit)<-[:HAS_EDIT]-(s:Session)
WITH f, tip, collect(DISTINCT s.id) AS sessions, collect(DISTINCT s.agent) AS agents
WHERE size(sessions) >= 2 AND (
  EXISTS { MATCH (tip)<-[:FROM]-(:Range)<-[:HAS_RANGE]-(f) }
  OR (NOT EXISTS { MATCH (tip)<-[:REPLACED]-() }
      AND NOT EXISTS { MATCH (tip)<-[:FROM]-(:Range) })
)
OPTIONAL MATCH (tip)<-[:FROM]-(r:Range)<-[:HAS_RANGE]-(f)
RETURN f.path AS path, tip.id AS hunk, r.start AS start, r.end AS end, sessions,
size(sessions) AS session_count, agents, size(agents) AS agent_count
ORDER BY session_count DESC, path, hunk, start"""


def test_tracks_match_legacy_on_forks_joins_and_shortcuts(tmp_path):
    store = tmp_path / "fork.rl"
    with relate.connect(str(store)) as db:
        db.execute("""
CREATE (:PaperTrail {version:1}), (f:File {id:'story', path:'story'}),
(r:Range {start:1, end:2}), (other:Range {start:3, end:3}), (empty:Range {start:4, end:4}),
(again:Range {start:5, end:6}),
(a:Hunk {id:'top', kind:'agent'}), (b:Hunk {id:'left'}), (c:Hunk {id:'right'}),
(d:Hunk {id:'join'}), (e:Hunk {id:'tail'}), (base:Hunk {id:'base', kind:'baseline'}),
(f)-[:HAS_RANGE]->(r)-[:FROM]->(a), (f)-[:HAS_RANGE]->(other)-[:FROM]->(b),
(f)-[:HAS_RANGE]->(empty)-[:FROM]->(base),
(f)-[:HAS_RANGE]->(again)-[:FROM]->(a),
(f)-[:HAS_HISTORY]->(a), (f)-[:HAS_HISTORY]->(b), (f)-[:HAS_HISTORY]->(c),
(a)-[:REPLACED]->(e), (a)-[:REPLACED]->(b), (a)-[:REPLACED]->(c),
(b)-[:REPLACED]->(d), (c)-[:REPLACED]->(d), (d)-[:REPLACED]->(e),
(sa:Session {id:'top', agent:'red'})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(a),
(sb:Session {id:'left', agent:'blue'})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(b),
(sc:Session {id:'right', agent:'red'})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(c),
(sd:Session {id:'join'})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(d),
(se:Session {id:'tail'})-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(e),
(sc)-[:HAS_EDIT]->(:Edit)-[:HAS_HUNK]->(e)
""")
    with web.ReaderQueries(store) as q:
        legacy = q.db.execute(LEGACY_TRACKS, {"path": "story"}).data()
        for _ in range(2):  # Reuses the same edges for the next view.
            tracks = q._tracks("story")
            assert [{k: row[k] for k in legacy[0]} for row in tracks] == legacy
            assert all(row["basis"] is None and row["was"] is None for row in tracks)
        crossings = q.db.execute(LEGACY_CROSSINGS).data()
        assert q.crossings()["rows"] == crossings
        for limit in (1, 2, 3, 4):
            page = q.crossings(limit=limit)
            assert page["rows"] == crossings[:limit]
            assert page["truncated"] == (len(crossings) > limit)
    assert legacy[0]["lanes"][-1] == {"session": "join", "depth": 2}
    assert legacy[2]["lanes"] == []


def test_file_window_and_deletion_expand(running, history):
    _, server = running
    _, path, _, _ = history
    whole = fetch(server, "file", path=path)
    window = fetch(server, "file", path=path, **{"from": 2, "to": 2, "deleted": 0})
    assert window["lines"] == whole["lines"][1:2]
    assert window["total_lines"] == len(whole["lines"]) == window["line_count"]
    assert window["tracks"] == [row for row in whole["tracks"] if row["start"] <= 2 <= row["end"]]
    (gap,) = whole["gaps"]
    assert window["gaps"] == [gap | {"text": None}]
    assert fetch(server, "deleted", path=path, id=gap["id"])["text"] == gap["text"]
    with pytest.raises(HTTPError) as exc:
        fetch(server, "file", path=path, **{"from": 2, "to": 1})
    assert exc.value.code == 400


def test_window_can_read_large_file_without_loading_the_full_text(running, history):
    _, server = running
    _, path, _, _ = history
    with path.open("wb") as stream:
        # Split both a UTF-8 character and a CRLF across block boundaries.
        stream.write(b"a" * 65535 + "中".encode() + b"\n")
        stream.write(b"b" * 65532 + b"\r\n")
        stream.write(b"\r\n" + b"tail\n" * web.MAX_FILE_BYTES)
        stream.write(b"last")
    full = fetch(server, "file", path=path)
    assert full["lines"] is None and full["note"] == "File is too large to display."
    window = fetch(server, "file", path=path, **{"from": 3, "to": 4})
    assert window["lines"] == ["", "tail"]
    assert window["total_lines"] == web.MAX_FILE_BYTES + 4
    end = fetch(server, "file", path=path, **{"from": window["total_lines"]})
    assert end["lines"] == ["last"]


def test_graph_and_log_versions_invalidate_caches(running, history, monkeypatch):
    reader, server = running
    _, _, log, write_graph = history
    statements, parses = [], []
    run, parse = web.ReaderQueries._run, web.conversation

    def track_run(self, query, **params):
        statements.append(query)
        return run(self, query, **params)

    def track_parse(session, edits):
        parses.append(session["id"])
        return parse(session, edits)

    monkeypatch.setattr(web.ReaderQueries, "_run", track_run)
    monkeypatch.setattr(web, "conversation", track_parse)
    first = fetch(server, "overview")
    conv = fetch(server, "conversation", id="writer")
    statements.clear()
    assert fetch(server, "overview") == first
    assert fetch(server, "conversation", id="writer") == conv
    assert reader.conversation_bytes("writer") is reader.conversation_bytes("writer")
    assert statements == [] and parses == ["writer"]

    log.write_text(json.dumps({"type": "user", "message": {"content": "new message"}}) + "\n")
    assert fetch(server, "conversation", id="writer")["items"][0]["text"] == "new message"
    assert statements == [] and parses == ["writer", "writer"]

    write_graph(" rebuilt")
    assert fetch(server, "overview")["sessions"][0]["title"] == "Writer rebuilt"
    assert fetch(server, "conversation", id="writer")["session"]["title"] == "Writer rebuilt"
    assert web.REPLACED_BY in statements and parses == ["writer", "writer", "writer"]


def test_tree_and_search_share_listing_until_expiry(running, monkeypatch):
    _, server = running
    original, roots = web.list_files, []

    def listing(root):
        roots.append(root)
        return original(root)

    monkeypatch.setattr(web, "list_files", listing)
    now = 0
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: now))
    assert fetch(server, "tree")["files"]
    assert fetch(server, "search", q="keep")["files"]
    assert len(roots) == 1
    (roots[0] / "new.txt").write_text("new\n")
    now += web.TREE_CACHE_SECONDS
    assert "new.txt" in fetch(server, "tree")["files"]
    assert len(roots) == 2


def test_trace_reuses_hash_and_notices_changed_text(running, history):
    _, server = running
    _, path, _, _ = history
    file_fingerprint.cache_clear()
    assert not fetch(server, "trace", path=path, line=1)["stale"]
    assert not fetch(server, "trace", path=path, line=2)["stale"]
    assert file_fingerprint.cache_info().hits == 1
    path.write_text("keep one\nlast two\n")
    assert fetch(server, "trace", path=path, line=1)["stale"]
