"""Keep the faster query and cache paths compatible with existing stores."""

import json

import pytest
import relate

from papertrail import transcript, web
from papertrail.graph import build
from papertrail.model import Edit, Session
from papertrail.queries import EDIT_FILES, SESSION_EDITS, Queries

OLD_SESSIONS = """
MATCH (s:Session)
OPTIONAL MATCH (s)-[:HAS_EDIT]->(e:Edit)
OPTIONAL MATCH (parent:Session)-[:SPAWNED]->(s)
RETURN s.id AS id, s.agent AS agent, s.model AS model, s.title AS title,
s.started_at AS started_at, s.ended_at AS ended_at, s.cwd AS cwd, parent.id AS parent,
count(e) AS edits ORDER BY started_at, id"""


@pytest.mark.parametrize("spawned", [False, True])
def test_overview_sessions_match_old_query(tmp_path, spawned):
    store = tmp_path / "graph.rl"
    with relate.connect(str(store)) as db:
        db.execute("""
            CREATE (:PaperTrail {version:1}), (a:Session {id:'a', started_at:'start'}),
            (z:Session {id:'z', started_at:'start'}), (b:Session {id:'b', started_at:'end'}),
            (a)-[:HAS_EDIT]->(:Edit {id:'e1'}), (a)-[:HAS_EDIT]->(:Edit {id:'e2'})
        """)
        if spawned:
            db.execute("""
                MATCH (a:Session {id:'a'}), (b:Session {id:'b'}), (z:Session {id:'z'})
                CREATE (z)-[:SPAWNED]->(a), (b)-[:SPAWNED]->(a), (z)-[:SPAWNED]->(a),
                (a)-[:SPAWNED]->(b)
            """)
    with web.ReaderQueries(store) as q:
        expected = [row | {"written": 0, "alive": 0} for row in q._run(OLD_SESSIONS)]
        view = q.overview()
    assert view["sessions"] == expected


@pytest.mark.parametrize("legacy", [False, True])
def test_trace_uses_one_session_read_for_ties_and_session_ends(tmp_path, legacy):
    names = ("first", "repeat", "z", "a", "target", "repeat", "b", "repeat", "last")
    paths = {name: str(tmp_path / name) for name in names}
    ts = "2025-01-01T00:00:00Z"
    previous, edits = {}, []
    for index, name in enumerate(names):
        new_text = f"sample {index}\n"
        edits.append(Edit(
            f"edit-{len(names) - index}", index, ts, "Write", paths[name], None, "m",
            before=previous.get(name, ""), after=new_text, call_id=f"call-{index}",
        ))
        previous[name] = new_text
    for name, text in previous.items():
        (tmp_path / name).write_text(text)
    store = tmp_path / "graph.rl"
    build(store, [Session("writer", "claude-code", "", edits=edits)])
    # Replay also sorts by edit ID if sequences tie, so add the ties after
    # building the replacement history in the intended order.
    with relate.connect(str(store)) as db:
        if legacy:
            db.execute("MATCH (e:Edit) REMOVE e.call_id, e.seq")
        else:
            db.execute("MATCH (e:Edit) SET e.seq = 1")

    with Queries(store) as q:
        for name, before, after in (
            ("first", [], ["repeat", "z", "a", "target", "b", "last"]),
            ("target", ["first", "repeat", "z", "a"], ["repeat", "b", "last"]),
            ("last", ["first", "repeat", "z", "a", "target", "b"], []),
        ):
            result = q.trace(paths[name], 1)
            row, = result["rows"]
            assert row["previous_files"] == [paths[name] for name in before]
            assert row["next_files"] == [paths[name] for name in after]
            assert result["cypher"].count(SESSION_EDITS.strip()) == 1
            assert EDIT_FILES.strip() not in result["cypher"]

        full = q.trace(paths["repeat"], 1)
        capped = q.trace(paths["repeat"], 1, limit=2)
        assert [row["edit"] for row in full["rows"]] == ["edit-2", "edit-4", "edit-8"]
        assert full["truncated"] is False
        assert capped["rows"] == full["rows"][:2]
        assert capped["truncated"] is True


@pytest.fixture
def cached_conversation(tmp_path, monkeypatch):
    log = tmp_path / "main.jsonl"
    log.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "launch"}]},
    }) + "\n")
    store = tmp_path / "graph.rl"
    build(store, [Session("main", "claude-code", str(log))])
    folder = log.with_suffix("") / "subagents"
    parses = []
    parse = web.conversation

    def count_parse(session, edits):
        parses.append(session["id"])
        return parse(session, edits)

    monkeypatch.setattr(web, "conversation", count_parse)
    reader = web.Reader(store, tmp_path)
    try:
        yield reader, folder, parses
    finally:
        reader.close()


def test_conversation_cache_tracks_child_files(cached_conversation):
    reader, folder, parses = cached_conversation

    def child():
        return json.loads(reader.conversation_bytes("main"))["items"][0]["child"]

    assert child() is None
    assert child() is None
    assert parses == ["main"]

    folder.mkdir(parents=True)
    meta = folder / "agent-helper.meta.json"
    meta.write_text('{"toolUseId":"launch"}')
    assert child() == "main/agent-helper"
    assert child() == "main/agent-helper"
    assert parses == ["main"] * 2

    log = folder / "agent-helper.jsonl"
    log.write_text("{}\n")
    assert child() == "main/agent-helper"
    assert parses == ["main"] * 3
    log.write_text("{}\n{}\n")
    assert child() == "main/agent-helper"
    assert parses == ["main"] * 4

    meta.write_text('{"toolUseId":"other"}')
    assert child() is None
    meta.write_text('{"toolUseId":"launch"}')
    assert child() == "main/agent-helper"
    assert parses == ["main"] * 6

    log.unlink()
    meta.unlink()
    assert child() is None
    assert child() is None
    assert parses == ["main"] * 7


def test_large_child_folder_is_read_without_caching(cached_conversation, monkeypatch):
    reader, folder, parses = cached_conversation
    monkeypatch.setattr(transcript, "CONVERSATION_DEPENDENCY_LIMIT", 1)
    folder.mkdir(parents=True)
    (folder / "agent-helper.jsonl").write_text("{}\n")
    (folder / "agent-helper.meta.json").write_text('{"toolUseId":"launch"}')
    for _ in range(2):
        view = json.loads(reader.conversation_bytes("main"))
        assert view["items"][0]["child"] == "main/agent-helper"
    assert parses == ["main"] * 2
