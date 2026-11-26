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
