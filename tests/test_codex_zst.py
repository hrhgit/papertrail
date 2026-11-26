"""Codex compresses older rollouts to `.jsonl.zst`. They must read like the plain ones."""

import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from papertrail import logfile
from papertrail.sources import CodexSource
from papertrail.transcript import conversation

FIXTURE = next((Path(__file__).parent / "fixtures" / "codex-sessions").rglob("rollout-*.jsonl"))


def compress(data):
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd:
        return zstd.compress(data)
    if shutil.which("zstd"):
        return subprocess.run(["zstd", "-qc"], input=data, capture_output=True, check=True).stdout
    pytest.skip("no zstd encoder")


def layout(root, packed):
    folder = root / "2025" / "01" / "01"
    folder.mkdir(parents=True)
    data = FIXTURE.read_bytes()
    if packed:
        path = folder / (FIXTURE.name + ".zst")
        path.write_bytes(compress(data))
    else:
        path = folder / FIXTURE.name
        path.write_bytes(data)
    return path


def parsed(root):
    source = CodexSource()
    sessions = [asdict(s) for s in source.sessions(root)]
    for s in sessions:
        s.pop("log_path", None)
    return sessions, source


@pytest.fixture(params=["stdlib", "package", "command"])
def decoder(request, monkeypatch):
    if request.param == "stdlib" and not logfile._stdlib():
        pytest.skip("compression.zstd needs Python 3.14")
    if request.param == "package":
        if not logfile._package():
            pytest.skip("zstandard is not installed")
        monkeypatch.setattr(logfile, "_stdlib", lambda: None)
    if request.param == "command":
        if not shutil.which("zstd"):
            pytest.skip("no zstd command")
        monkeypatch.setattr(logfile, "_stdlib", lambda: None)
        monkeypatch.setattr(logfile, "_package", lambda: None)
    return request.param


def test_compressed_rollout_parses_like_plain(tmp_path, decoder):
    plain, _ = parsed(layout(tmp_path / "plain", False).parents[3])
    root = tmp_path / "packed"
    path = layout(root, True)
    packed, source = parsed(root)
    assert plain and packed == plain
    assert source.counts["log_files"] == 1
    assert not source.failures
    session = next(s for s in CodexSource().sessions(root))
    assert session.log_path == str(path)


def test_plain_copy_wins_over_compressed_replica(tmp_path):
    root = tmp_path / "both"
    layout(root, False)
    packed = FIXTURE.name + ".zst"
    (root / "2025" / "01" / "01" / packed).write_bytes(compress(FIXTURE.read_bytes()))
    plain, _ = parsed(layout(tmp_path / "plain", False).parents[3])
    both, source = parsed(root)
    assert both == plain
    session = next(s for s in CodexSource().sessions(root))
    assert session.log_path.endswith(".jsonl")
    assert source.counts["replica_files"] == 1


def test_without_decoder_is_counted_not_fatal(tmp_path, monkeypatch):
    root = tmp_path / "packed"
    layout(root, True)
    monkeypatch.setattr(logfile, "_stdlib", lambda: None)
    monkeypatch.setattr(logfile, "_package", lambda: None)
    monkeypatch.setattr(logfile.shutil, "which", lambda name: None)
    sessions, source = parsed(root)
    assert sessions == []
    assert source.failures["compressed_rollout_without_zstd"] >= 1


def test_corrupt_compressed_rollout_is_unreadable(tmp_path):
    folder = tmp_path / "bad" / "2025"
    folder.mkdir(parents=True)
    (folder / (FIXTURE.name + ".zst")).write_bytes(b"not zstd at all")
    if not logfile.zstd_available():
        pytest.skip("no zstd decoder")
    sessions, source = parsed(tmp_path / "bad")
    assert sessions == []
    assert source.failures["unreadable_rollout"] >= 1


def test_conversation_follows_log_compressed_after_build(tmp_path):
    root = tmp_path / "logs"
    plain = layout(root, False)
    session = next(s for s in CodexSource().sessions(root))
    meta = {"id": session.id, "agent": "codex", "log_path": session.log_path}
    before = conversation(meta, [])
    packed = plain.with_name(plain.name + ".zst")
    packed.write_bytes(compress(plain.read_bytes()))
    plain.unlink()
    after = conversation(meta, [])
    assert before["items"] and after == before


def test_truncated_compressed_rollout_is_not_a_partial_session(tmp_path, decoder):
    folder = tmp_path / "cut" / "2025"
    folder.mkdir(parents=True)
    data = FIXTURE.read_bytes()
    # A complete first frame guarantees the parser sees session metadata.
    packed = compress(data) + compress(b"truncated frame\n")[:-1]
    (folder / (FIXTURE.name + ".zst")).write_bytes(packed)
    sessions, source = parsed(tmp_path / "cut")
    assert sessions == []
    assert source.failures["unreadable_rollout"] == 1
    assert source.skipped["partial_rollout"] == 1


def test_multi_frame_rollout_reads_whole(tmp_path, decoder):
    data = FIXTURE.read_bytes()
    cut = data.index(b"\n", len(data) // 2) + 1
    root = tmp_path / "frames"
    folder = root / "2025" / "01" / "01"
    folder.mkdir(parents=True)
    (folder / (FIXTURE.name + ".zst")).write_bytes(compress(data[:cut]) + compress(data[cut:]))
    plain, _ = parsed(layout(tmp_path / "plain", False).parents[3])
    packed, _ = parsed(root)
    assert packed == plain


def test_cache_reuses_compressed_replica_group_and_prefilter_finds_packed_path(tmp_path, monkeypatch):
    from papertrail.ingest import prefilter
    from papertrail.sources import load_sources

    root = tmp_path / "logs"
    plain = layout(root, False)
    packed = plain.with_name(plain.name + ".zst")
    marker = tmp_path / "project"
    packed.write_bytes(compress(plain.read_bytes() + b'{"type":"note","payload":{"path":"' +
                                str(marker).encode() + b'"}}\n'))
    roots = {"codex": root}
    assert prefilter(roots, marker) == {plain, packed}
    expected = load_sources(roots, cache=False)
    assert load_sources(roots) == expected

    def fail(*args, **kwargs):
        pytest.fail("Warm ingest reopened a rollout replica")

    monkeypatch.setattr(CodexSource, "raw_lines", fail)
    assert load_sources(roots) == expected


def test_compressed_rollout_reports_progress_while_reading(tmp_path, decoder, monkeypatch):
    import os

    from papertrail.sources import codex as codex_module

    if decoder == "command":
        pytest.skip("the zstd command does not say how much input it consumed")
    folder = tmp_path / "big" / "2025"
    folder.mkdir(parents=True)
    filler = b"".join(
        b'{"type":"note","payload":{"text":"' + os.urandom(512).hex().encode() + b'"}}\n'
        for _ in range(2000)
    )
    path = folder / (FIXTURE.name + ".zst")
    path.write_bytes(compress(FIXTURE.read_bytes() + filler))
    monkeypatch.setattr(codex_module, "READ_CHUNK", 64 * 1024)
    ticks = []
    list(CodexSource().sessions(tmp_path / "big", progress=lambda p, n=None: ticks.append(n)))
    partial = [n for n in ticks if n is not None]
    assert len(partial) > 1
    assert sum(partial) <= path.stat().st_size


@pytest.mark.parametrize("header_scan", [False, True])
@pytest.mark.parametrize("packed", [False, True])
def test_cancel_inside_one_codex_file(tmp_path, monkeypatch, header_scan, packed):
    from papertrail.sources import codex as codex_module

    path = tmp_path / ("rollout-example.jsonl.zst" if packed else "rollout-example.jsonl")
    filler = b'{"type":"note","payload":{"text":"' + b"x" * 2048 + b'"}}\n'
    contents = filler * 4096
    path.write_bytes(compress(contents) if packed else contents)
    monkeypatch.setattr(codex_module, "READ_CHUNK", 4096)

    class Interrupted(Exception):
        pass

    reads = 0
    checks = 0
    original = codex_module.open_log

    class Stream:
        def __enter__(self):
            self.inner = original(path)
            return self

        def __exit__(self, *args):
            self.inner.close()

        def readline(self, limit):
            nonlocal reads
            reads += 1
            return self.inner.readline(limit)

    def check():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise Interrupted

    monkeypatch.setattr(codex_module, "open_log", lambda _: Stream())
    source = CodexSource()
    source.log_sizes = {path: path.stat().st_size}
    source.check = check
    with pytest.raises(Interrupted):
        if header_scan:
            source.headers([path], check=check)
        else:
            list(source.records(path))
    assert 0 < reads < 4096
    assert not source.broken
