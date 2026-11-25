"""Signatures stay valid while a secret is being rotated."""

from relay import config
from relay.signing import sign, verify

BODY = b'{"id": "evt_1"}'


def test_current_secret_verifies(monkeypatch):
    monkeypatch.setattr(config, "SIGNING_SECRET", "new")
    assert verify(BODY, sign(BODY))


def test_previous_secret_still_verifies(monkeypatch):
    monkeypatch.setattr(config, "SIGNING_SECRET", "new")
    monkeypatch.setattr(config, "SIGNING_SECRET_PREVIOUS", "old")
    assert verify(BODY, sign(BODY, "old"))
    assert not verify(BODY, sign(BODY, "someone-else"))
