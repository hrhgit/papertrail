"""Deliver one webhook event to a customer endpoint, retrying on failure."""

import json
import random
import time
import urllib.error
import urllib.request

from relay import config
from relay.signing import sign


class DeliveryFailed(Exception):
    """The event was not accepted. It is parked for manual replay."""


def post(url: str, body: bytes, headers: dict) -> int:
    """Send one request and return the HTTP status. 0 means no response."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def should_retry(status: int) -> bool:
    """A 4xx means the receiver refused this event, so sending it again cannot help.

    408 and 429 are the exceptions: the receiver is asking us to come back later.
    """
    if status in (408, 429):
        return True
    return not 400 <= status < 500


def backoff_delay(attempt: int) -> float:
    """Seconds to wait after the given attempt.

    A random point below the exponential ceiling, so events parked by the same
    outage do not all come back at once.
    """
    ceiling = min(config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), config.BACKOFF_CAP_SECONDS)
    return random.uniform(0, ceiling)


def deliver(event: dict, url: str, send=post, sleep=time.sleep) -> int:
    """Send the event. Returns the number of attempts it took."""
    body = json.dumps(event, sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        # The same key on every attempt, so a receiver can drop repeats.
        "Idempotency-Key": event["id"],
        "X-Relay-Signature": sign(body),
    }
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        status = send(url, body, headers)
        if 200 <= status < 300:
            return attempt
        if not should_retry(status):
            break
        if attempt < config.MAX_ATTEMPTS:
            sleep(backoff_delay(attempt))
    raise DeliveryFailed(f"{event['id']}: gave up after {attempt} attempts, last status {status}")
