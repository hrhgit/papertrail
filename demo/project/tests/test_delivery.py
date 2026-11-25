"""Delivery retries, without touching the network."""

import pytest

from relay import config
from relay.delivery import DeliveryFailed, backoff_delay, deliver

EVENT = {"id": "evt_1", "type": "invoice.paid"}
URL = "https://example.test/hook"


def sender(*statuses):
    """A fake transport that answers with the given statuses in order."""
    answers = iter(statuses)

    def send(url, body, headers):
        send.calls.append(headers)
        return next(answers)

    send.calls = []
    return send


def quiet(seconds):
    """Stand in for time.sleep."""


def test_first_attempt_succeeds():
    assert deliver(EVENT, URL, send=sender(200), sleep=quiet) == 1


def test_retries_until_success():
    assert deliver(EVENT, URL, send=sender(500, 503, 200), sleep=quiet) == 3


def test_gives_up_after_max_attempts():
    send = sender(*[500] * config.MAX_ATTEMPTS)
    with pytest.raises(DeliveryFailed):
        deliver(EVENT, URL, send=send, sleep=quiet)
    assert len(send.calls) == config.MAX_ATTEMPTS


def test_backoff_stays_under_its_ceiling():
    assert all(0 <= backoff_delay(3) <= 8 for _ in range(200))
    assert all(backoff_delay(30) <= config.BACKOFF_CAP_SECONDS for _ in range(200))


def test_every_attempt_carries_the_same_idempotency_key():
    send = sender(500, 200)
    deliver(EVENT, URL, send=send, sleep=quiet)
    assert [h["Idempotency-Key"] for h in send.calls] == ["evt_1", "evt_1"]


def test_does_not_retry_a_refusal():
    send = sender(410, 200)
    with pytest.raises(DeliveryFailed):
        deliver(EVENT, URL, send=send, sleep=quiet)
    assert len(send.calls) == 1


def test_retries_when_asked_to_slow_down():
    assert deliver(EVENT, URL, send=sender(429, 408, 200), sleep=quiet) == 3
