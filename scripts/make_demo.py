"""Write the bundled demo: a small webhook relay and the agent sessions that built it.

    uv run python scripts/make_demo.py [OUTPUT_DIR]

Everything here is made up. The script plays seven sessions in order against an
in-memory copy of the project and writes what each agent would have logged, in
the Claude Code and Codex CLI transcript formats, so the logs and the final
files always agree. The output is committed under demo/ and a test checks that
running this again reproduces it byte for byte.
"""

import difflib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG = '''"""Settings for the webhook relay worker."""

import os

# How many times one event is sent before it is parked for manual replay.
MAX_ATTEMPTS = 5

# Seconds to wait for a customer endpoint to answer.
TIMEOUT_SECONDS = 30

# Seconds between two attempts.
RETRY_DELAY_SECONDS = 1

SIGNING_SECRET = os.environ.get("RELAY_SIGNING_SECRET", "")
'''

SIGNING = '''"""HMAC signatures so receivers can check that an event came from us."""

import hashlib
import hmac

from relay import config


def sign(body: bytes, secret: str | None = None) -> str:
    """Hex HMAC-SHA256 of the request body."""
    key = (secret or config.SIGNING_SECRET).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str) -> bool:
    """Constant-time check, for receivers and for our own tests."""
    return hmac.compare_digest(sign(body), signature)
'''

DELIVERY = '''"""Deliver one webhook event to a customer endpoint, retrying on failure."""

import json
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
    """Every failure is worth another try."""
    return True


def backoff_delay(attempt: int) -> float:
    """Seconds to wait after the given attempt."""
    return config.RETRY_DELAY_SECONDS


def deliver(event: dict, url: str, send=post, sleep=time.sleep) -> int:
    """Send the event. Returns the number of attempts it took."""
    body = json.dumps(event, sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Signature": sign(body),
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
'''

README = """# relay

Delivers webhook events to customer endpoints. Each event is signed, sent with
a POST, and retried when the endpoint does not accept it.

| Setting | Value | Meaning |
| --- | --- | --- |
| `MAX_ATTEMPTS` | 5 | Sends per event before it is parked |
| `TIMEOUT_SECONDS` | 30 | Wait for the endpoint to answer |

## Run the tests

    python -m pytest
"""

TESTS = '''"""Delivery retries, without touching the network."""

import pytest

from relay import config
from relay.delivery import DeliveryFailed, deliver

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
'''

TEST_SIGNING = '''"""Signatures stay valid while a secret is being rotated."""

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
'''


class World:
    """The project's files as they stand, and where the logs go."""

    def __init__(self, out):
        self.out = Path(out)
        self.files = {}
        self.clock = None

    def at(self, stamp):
        self.clock = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)

    def tick(self, seconds=7):
        self.clock += timedelta(seconds=seconds)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def replace(self, path, old, new):
        before = self.files[path]
        if before.count(old) != 1:
            raise ValueError(f"{path}: expected exactly one match for {old!r}")
        self.files[path] = before.replace(old, new)
        return before

    def save(self, path, records):
        path = self.out / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))


class Claude:
    """One Claude Code transcript."""

    ROOT = "logs/claude/relay"

    def __init__(self, world, sid, title, model, effort="", side=None):
        self.w, self.sid, self.model, self.effort = world, sid, model, effort
        self.side, self.n, self.last = side, 0, None
        self.records = [{"type": "custom-title", "customTitle": title}]
        # Relative to the transcript's folder, so the checkout can live anywhere.
        self.cwd = "../../../project" if not side else "../../../../../project"

    def record(self, kind, message, **extra):
        self.n += 1
        prefix = f"{self.side or self.sid[:8]}"
        row = {
            "type": kind,
            "sessionId": self.sid,
            "uuid": f"{prefix}-{self.n:03}",
            "parentUuid": self.last,
            "timestamp": self.w.tick(),
            "cwd": self.cwd,
            "gitBranch": "main",
            "isSidechain": bool(self.side),
            "message": message,
            **extra,
        }
        self.last = row["uuid"]
        self.records.append(row)
        return row

    def ask(self, text):
        self.w.tick(40)
        self.record("user", {"role": "user", "content": text}, origin={"kind": "human"})

    def say(self, text):
        self.record(
            "assistant",
            {"role": "assistant", "model": self.model, "content": [{"type": "text", "text": text}]},
        )

    def call(self, name, args):
        extra = {"effort": self.effort, "perTurnEffort": self.effort} if self.effort else {}
        key = f"toolu_{self.side or self.sid[:8]}_{self.n + 1:03}"
        block = {"type": "tool_use", "id": key, "name": name, "input": args}
        self.record(
            "assistant", {"role": "assistant", "model": self.model, "content": [block]}, **extra
        )
        return key

    def result(self, key, text, payload):
        block = {"type": "tool_result", "tool_use_id": key, "content": text}
        self.record("user", {"role": "user", "content": [block]}, toolUseResult=payload)

    def write(self, path, content):
        before = self.w.files.get(path)
        key = self.call("Write", {"file_path": path, "content": content})
        self.w.files[path] = content
        self.result(
            key,
            f"File {'created' if before is None else 'updated'} successfully at: {path}",
            {
                "type": "create" if before is None else "update",
                "filePath": path,
                "originalFile": before,
                "content": content,
                "structuredPatch": [],
                "userModified": False,
            },
        )

    def edit(self, path, old, new):
        args = {"file_path": path, "old_string": old, "new_string": new, "replace_all": False}
        key = self.call("Edit", args)
        before = self.w.replace(path, old, new)
        self.result(
            key,
            f"The file {path} has been updated successfully.",
            {
                "filePath": path,
                "oldString": old,
                "newString": new,
                "replaceAll": False,
                "originalFile": before,
                "structuredPatch": [],
                "userModified": False,
            },
        )

    def delegate(self, name, title, task, work):
        """Hand a task to a subagent, which keeps its own transcript."""
        key = self.call("Agent", {"description": title, "prompt": task})
        child = Claude(self.w, self.sid, title, self.model, self.effort, side=name)
        child.record("user", {"role": "user", "content": task})
        work(child)
        base = f"{self.ROOT}/{self.sid}/subagents/agent-{name}"
        self.w.save(base + ".jsonl", child.records)
        (self.w.out / (base + ".meta.json")).write_text(json.dumps({"toolUseId": key}) + "\n")
        self.result(key, "Done", {})

    def close(self):
        self.w.save(f"{self.ROOT}/{self.sid}.jsonl", self.records)


class Codex:
    """One Codex CLI rollout."""

    def __init__(self, world, sid, title, model, effort):
        self.w, self.sid, self.title, self.model, self.effort = world, sid, title, model, effort
        self.turn = self.calls = 0
        self.started = world.clock
        self.records = [
            self.row(
                "session_meta",
                {"id": sid, "cwd": "../../../../../project", "git": {"branch": "main"}},
            )
        ]

    def row(self, kind, payload):
        return {"timestamp": self.w.tick(), "type": kind, "payload": payload}

    def ask(self, text):
        self.w.tick(40)
        self.turn += 1
        tid = f"turn-{self.turn}"
        self.records.append(
            self.row("turn_context", {"turn_id": tid, "model": self.model, "effort": self.effort})
        )
        item = {
            "type": "UserMessage",
            "id": f"{self.sid[:8]}-u{self.turn}",
            "content": [{"type": "text", "text": text}],
        }
        self.event({"type": "item_completed", "item": item})
        if self.turn == 1:
            self.event({"type": "thread_name_updated", "thread_name": self.title})

    def event(self, payload):
        payload = {**payload, "turn_id": f"turn-{self.turn}", "thread_id": self.sid}
        self.records.append(self.row("event_msg", payload))

    def patch(self, path, body, mark):
        self.calls += 1
        key = f"call_{self.sid[:8]}_{self.calls}"
        text = f"*** Begin Patch\n{body}\n*** End Patch"
        call = {"type": "custom_tool_call", "name": "apply_patch", "call_id": key, "input": text}
        self.records.append(self.row("response_item", call))
        done = {
            "type": "custom_tool_call_output",
            "call_id": key,
            "output": f"Success. Updated the following files:\n{mark} {path}",
        }
        self.records.append(self.row("response_item", done))

    def add(self, path, content):
        self.w.files[path] = content
        rows = "\n".join("+" + line for line in content.splitlines())
        self.patch(path, f"*** Add File: {path}\n{rows}", "A")

    def edit(self, path, old, new):
        before = self.w.replace(path, old, new).splitlines()
        after = self.w.files[path].splitlines()
        rows = [f"*** Update File: {path}"]
        for group in difflib.SequenceMatcher(None, before, after).get_grouped_opcodes(3):
            rows.append("@@")
            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    rows += [" " + line for line in before[i1:i2]]
                else:
                    rows += ["-" + line for line in before[i1:i2]]
                    rows += ["+" + line for line in after[j1:j2]]
        self.patch(path, "\n".join(rows), "M")

    def close(self):
        day = self.started.strftime("%Y/%m/%d")
        stamp = self.started.strftime("%Y-%m-%dT%H-%M-%S")
        self.w.save(f"logs/codex/{day}/rollout-{stamp}-{self.sid}.jsonl", self.records)


def scaffold(w):
    w.at("2026-03-02T09:14:00")
    s = Claude(w, "3f9a1c52-7b1e-4c6a-9d2f-0a61c5e8b701", "Scaffold the webhook relay",
               "claude-opus-5-5")  # fmt: skip
    s.ask(
        "Build a small worker that delivers webhook events to customer endpoints. "
        "Sign the body with HMAC-SHA256, POST it, and retry when the endpoint does not "
        "accept it. Keep the settings in one module. Standard library only."
    )
    s.say("I'll put the settings in relay/config.py, signing in its own module, and the "
          "retry loop in relay/delivery.py.")  # fmt: skip
    s.write("relay/config.py", CONFIG)
    s.write("relay/signing.py", SIGNING)
    s.write("relay/delivery.py", DELIVERY)
    s.write("README.md", README)
    s.say("The worker sends each event up to five times, one second apart, and parks it "
          "after that.")  # fmt: skip
    s.ask("Add tests for the retry loop. They must not open a network connection.")
    s.delegate(
        "tests",
        "Write delivery tests",
        "Write pytest tests for relay.delivery.deliver in tests/test_delivery.py. Pass a fake "
        "`send` and a no-op `sleep` so nothing touches the network. Cover first-try success, "
        "success after failures, and giving up after config.MAX_ATTEMPTS.",
        lambda child: child.write("tests/test_delivery.py", TESTS),
    )
    s.say("The tests are in tests/test_delivery.py. They inject a fake transport.")
    s.close()


def backoff(w):
    w.at("2026-03-04T14:02:00")
    s = Codex(w, "019560a1-4c2e-7d10-b3a8-5e2f9c7d1a04", "Exponential backoff", "gpt-6.2-astra",
              "medium")  # fmt: skip
    s.ask(
        "Retries go out one second apart, which is useless when an endpoint is down for "
        "minutes. Back off exponentially instead: 2s, 4s, 8s and so on, capped at 5 minutes."
    )
    s.edit(
        "relay/config.py",
        "# Seconds between two attempts.\nRETRY_DELAY_SECONDS = 1\n",
        "# The wait doubles after every failed attempt, up to the cap.\n"
        "BACKOFF_BASE_SECONDS = 2\nBACKOFF_CAP_SECONDS = 300\n",
    )
    s.edit(
        "relay/delivery.py",
        "    return config.RETRY_DELAY_SECONDS\n",
        "    delay = config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)\n"
        "    return min(delay, config.BACKOFF_CAP_SECONDS)\n",
    )
    s.close()


def client_errors(w):
    w.at("2026-03-06T10:40:00")
    s = Claude(w, "8c41d7e0-2a95-4f3b-8e17-6b0d3a9f2c55", "Stop retrying client errors",
               "claude-sonnet-5-5", "medium")  # fmt: skip
    s.ask(
        "Contoso's endpoint answers 410 for subscriptions they deleted, and we still send "
        "each of those events 5 times. Stop retrying 4xx responses, except 408 and 429."
    )
    s.edit(
        "relay/delivery.py",
        '    """Every failure is worth another try."""\n    return True\n',
        '    """A 4xx means the receiver refused this event, so sending it again cannot help.\n\n'
        '    408 and 429 are the exceptions: the receiver is asking us to come back later.\n'
        '    """\n'
        "    if status in (408, 429):\n"
        "        return True\n"
        "    return not 400 <= status < 500\n",
    )
    s.edit(
        "tests/test_delivery.py",
        "    assert len(send.calls) == config.MAX_ATTEMPTS\n",
        "    assert len(send.calls) == config.MAX_ATTEMPTS\n"
        "\n\n"
        "def test_does_not_retry_a_refusal():\n"
        "    send = sender(410, 200)\n"
        "    with pytest.raises(DeliveryFailed):\n"
        "        deliver(EVENT, URL, send=send, sleep=quiet)\n"
        "    assert len(send.calls) == 1\n"
        "\n\n"
        "def test_retries_when_asked_to_slow_down():\n"
        "    assert deliver(EVENT, URL, send=sender(429, 408, 200), sleep=quiet) == 3\n",
    )
    s.say("A 4xx now parks the event after one attempt. 408 and 429 are still retried.")
    s.close()


def outage(w):
    w.at("2026-03-10T08:21:00")
    s = Codex(w, "019573e8-9b1f-7a42-8c05-d4e6a1b7f390", "Raise the retry limit", "gpt-6.2-astra",
              "high")  # fmt: skip
    s.ask(
        "Northwind was down for 20 minutes last night and 312 events were parked because we "
        "gave up after 5 tries. Raise the limit to 8 so the backoff covers an outage that long."
    )
    s.edit("relay/config.py", "MAX_ATTEMPTS = 5\n", "MAX_ATTEMPTS = 8\n")
    s.close()


def by_hand(w):
    """Someone edits the timeout in their editor. No agent logs this."""
    w.replace(
        "relay/config.py",
        "TIMEOUT_SECONDS = 30\n",
        "# The load balancer drops idle connections at 15 seconds.\nTIMEOUT_SECONDS = 10\n",
    )


def incident(w):
    w.at("2026-03-12T16:05:00")
    s = Claude(w, "b27e5f90-61c3-4d8a-a4f2-9e0c7d135b68", "Incident 2291: duplicate charges",
               "claude-opus-5-5", "high")  # fmt: skip
    s.ask(
        "Incident 2291: Fabrikam's endpoint timed out after it had already processed the "
        "event, so each of our 8 attempts created another charge. Cap attempts at 3 until "
        "receivers can drop repeats. Leave the timeout alone, someone tuned it by hand."
    )
    s.say("A timeout looks like a failure to us even when the receiver did the work, so "
          "every retry is a possible duplicate. I'll cap the attempts and update the table.")  # fmt: skip
    s.edit("relay/config.py", "MAX_ATTEMPTS = 8\n", "MAX_ATTEMPTS = 3\n")
    s.edit("README.md", "| `MAX_ATTEMPTS` | 5 |", "| `MAX_ATTEMPTS` | 3 |")
    s.ask(
        "Send the event id as an Idempotency-Key header so receivers can drop repeats. "
        "Document it for them and add a test."
    )
    s.edit(
        "relay/delivery.py",
        '        "Content-Type": "application/json",\n',
        '        "Content-Type": "application/json",\n'
        "        # The same key on every attempt, so a receiver can drop repeats.\n"
        '        "Idempotency-Key": event["id"],\n',
    )
    s.edit(
        "tests/test_delivery.py",
        "\n\ndef test_does_not_retry_a_refusal():\n",
        "\n\n"
        "def test_every_attempt_carries_the_same_idempotency_key():\n"
        "    send = sender(500, 200)\n"
        "    deliver(EVENT, URL, send=send, sleep=quiet)\n"
        '    assert [h["Idempotency-Key"] for h in send.calls] == ["evt_1", "evt_1"]\n'
        "\n\ndef test_does_not_retry_a_refusal():\n",
    )
    s.edit(
        "README.md",
        "## Run the tests\n",
        "## For receivers\n\n"
        "A request that times out on our side may still have reached you, and we will send\n"
        "it again. Every attempt for one event carries the same `Idempotency-Key` header.\n"
        "Store the key and ignore an event you have already processed.\n\n"
        "## Run the tests\n",
    )
    s.say("The cap is 3. Every attempt now carries the event id as Idempotency-Key, and the "
          "README tells receivers to dedupe on it. I left TIMEOUT_SECONDS as it was.")  # fmt: skip
    s.close()


def jitter(w):
    w.at("2026-03-16T11:30:00")
    s = Claude(w, "e5d09a37-c8b4-4126-9f6e-21a7b4c80d93", "Jitter the backoff",
               "claude-sonnet-5-5", "medium")  # fmt: skip
    s.ask(
        "When Northwind came back up, every parked event retried in the same second and "
        "knocked them over again. Add full jitter to the backoff."
    )
    s.edit("relay/delivery.py", "import json\nimport time\n", "import json\nimport random\nimport time\n")
    s.edit(
        "relay/delivery.py",
        '    """Seconds to wait after the given attempt."""\n'
        "    delay = config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)\n"
        "    return min(delay, config.BACKOFF_CAP_SECONDS)\n",
        '    """Seconds to wait after the given attempt.\n\n'
        "    A random point below the exponential ceiling, so events parked by the same\n"
        "    outage do not all come back at once.\n"
        '    """\n'
        "    ceiling = min(config.BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), config.BACKOFF_CAP_SECONDS)\n"
        "    return random.uniform(0, ceiling)\n",
    )
    s.edit(
        "tests/test_delivery.py",
        "from relay.delivery import DeliveryFailed, deliver\n",
        "from relay.delivery import DeliveryFailed, backoff_delay, deliver\n",
    )
    s.edit(
        "tests/test_delivery.py",
        "\n\ndef test_every_attempt_carries_the_same_idempotency_key():\n",
        "\n\n"
        "def test_backoff_stays_under_its_ceiling():\n"
        "    assert all(0 <= backoff_delay(3) <= 8 for _ in range(200))\n"
        "    assert all(backoff_delay(30) <= config.BACKOFF_CAP_SECONDS for _ in range(200))\n"
        "\n\ndef test_every_attempt_carries_the_same_idempotency_key():\n",
    )
    s.close()


def rotation(w):
    w.at("2026-03-19T13:47:00")
    s = Codex(w, "01958f24-e7a3-7c61-9d48-0b3c5f8e2a17", "Rotate signing secrets", "gpt-6.2-astra",
              "high")  # fmt: skip
    s.ask(
        "Security wants signing secrets rotated without downtime. Sign with the current "
        "secret and let verify accept the previous one too. Drop the legacy X-Signature "
        "header while you are there: every receiver moved to X-Relay-Signature in January."
    )
    s.edit(
        "relay/config.py",
        'SIGNING_SECRET = os.environ.get("RELAY_SIGNING_SECRET", "")\n',
        'SIGNING_SECRET = os.environ.get("RELAY_SIGNING_SECRET", "")\n'
        "# Still accepted by verify() while receivers pick up the new secret.\n"
        'SIGNING_SECRET_PREVIOUS = os.environ.get("RELAY_SIGNING_SECRET_PREVIOUS", "")\n',
    )
    s.edit(
        "relay/signing.py",
        '    """Constant-time check, for receivers and for our own tests."""\n'
        "    return hmac.compare_digest(sign(body), signature)\n",
        '    """Constant-time check against the current secret, then the previous one."""\n'
        "    secrets = [config.SIGNING_SECRET, config.SIGNING_SECRET_PREVIOUS]\n"
        "    return any(hmac.compare_digest(sign(body, s), signature) for s in secrets if s)\n",
    )
    s.edit("relay/delivery.py", '        "X-Signature": sign(body),\n', "")
    s.add("tests/test_signing.py", TEST_SIGNING)
    s.close()


def main(out):
    out = Path(out)
    for name in ("project", "logs"):
        shutil.rmtree(out / name, ignore_errors=True)
    world = World(out)
    for step in (scaffold, backoff, client_errors, outage, by_hand, incident, jitter, rotation):
        step(world)
    # A note added on disk after the last session. The logs never see it.
    world.files["README.md"] += "\nOwner: payments platform team.\n"
    world.files["relay/__init__.py"] = ""
    for path, text in world.files.items():
        target = out / "project" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    graph(out)


def graph(out):
    """Build the graph the repository ships, with paths relative to the demo folder."""
    from papertrail.graph import build, select
    from papertrail.sources import load_sources

    project = (out / "project").resolve()
    folder = project / ".papertrail"
    folder.mkdir(exist_ok=True)
    # Ingest ignores this folder everywhere else. Here the graph is part of the demo.
    (folder / ".gitignore").write_text("*\n!.gitignore\n!graph.rl\n")
    roots = {"claude-code": out / "logs/claude", "codex": out / "logs/codex"}
    sessions, _ = load_sources(roots)
    (folder / "graph.rl").unlink(missing_ok=True)
    build(folder / "graph.rl", select(sessions, project), portable=out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "demo")
