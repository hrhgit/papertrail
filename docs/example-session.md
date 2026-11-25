# Why is the retry limit 3?

Captured from the bundled demo, run from `demo/project`. Every conversation, name and file in it is made up. Paths under that folder are shown relative to it, and `~/papertrail` stands for the checkout. Elapsed time varies by machine.

## ingest

```console
$ uv run papertrail ingest --claude-logs ../logs/claude --codex-logs ../logs/codex --project . --portable ..
{
  "relationships": 239,
  "agents": 2,
  "sessions": 8,
  "prompts": 9,
  "edits": 23,
  "hunks": 25,
  "files": 6,
  "ranges": 33,
  "lines": {
    "agent": 199,
    "baseline": 0,
    "external": 2,
    "drift": 2,
    "unknown": 0
  },
  "files_by_status": {
    "drift": 1,
    "exact": 5
  },
  "unapplied_patches": 0,
  "external_events": 1,
  "by_agent": {
    "claude-code": {
      "sessions": 5,
      "edits": 16,
      "lines": 173,
      "unplaced": 0
    },
    "codex": {
      "sessions": 3,
      "edits": 7,
      "lines": 26,
      "unplaced": 0
    }
  },
  "agent_fraction": 0.9802955665024631,
  "seconds": 0.04,
  "sources": {
    "claude-code": {
      "failures": {},
      "skipped": {
        "unmatched_calls": 0
      },
      "counts": {}
    },
    "codex": {
      "failures": {},
      "skipped": {},
      "counts": {
        "log_files": 3,
        "replica_files": 0,
        "child_sessions": 0,
        "user_items": 3,
        "title_events": 3,
        "calls_apply_patch": 7,
        "literal_apply_patch": 7,
        "route_apply_patch_success": 7,
        "successful_literal_apply_patch": 7,
        "fallback_file_receipts": 7,
        "retained_files": 7
      }
    }
  },
  "source_failures": {},
  "source_skipped": {
    "unmatched_calls": 0
  }
}
```

## blame

```console
$ uv run papertrail blame relay/config.py
1-5       claude-code  claude-opus-5-5  2026-03-02 09:15:08 UTC  3f9a1c52  Scaffold the webhook relay
          Build a small worker that delivers webhook events to customer endpoints. Sign th…
      1 │ """Settings for the webhook relay worker."""
      2 │ 
      3 │ import os
      4 │ 
      5 │ # How many times one event is sent before it is parked for manual replay.
6         claude-code  claude-opus-5-5 (high)  2026-03-12 16:06:08 UTC  b27e5f90  Incident 2291: duplicate charges
          Incident 2291: Fabrikam's endpoint timed out after it had already processed the …
      6 │ MAX_ATTEMPTS = 3
7-8       claude-code  claude-opus-5-5  2026-03-02 09:15:08 UTC  3f9a1c52  Scaffold the webhook relay
          Build a small worker that delivers webhook events to customer endpoints. Sign th…
      7 │ 
      8 │ # Seconds to wait for a customer endpoint to answer.
9-10      external  2026-03-11 12:14:15 UTC  changed between two recorded edits, outside any session
      9 │ # The load balancer drops idle connections at 15 seconds.
     10 │ TIMEOUT_SECONDS = 10
11        claude-code  claude-opus-5-5  2026-03-02 09:15:08 UTC  3f9a1c52  Scaffold the webhook relay
          Build a small worker that delivers webhook events to customer endpoints. Sign th…
     11 │ 
12-14     codex  gpt-6.2-astra (medium)  2026-03-04 14:03:22 UTC  019560a1  Exponential backoff
          Retries go out one second apart, which is useless when an endpoint is down for m…
     12 │ # The wait doubles after every failed attempt, up to the cap.
     13 │ BACKOFF_BASE_SECONDS = 2
     14 │ BACKOFF_CAP_SECONDS = 300
15-16     claude-code  claude-opus-5-5  2026-03-02 09:15:08 UTC  3f9a1c52  Scaffold the webhook relay
          Build a small worker that delivers webhook events to customer endpoints. Sign th…
     15 │ 
     16 │ SIGNING_SECRET = os.environ.get("RELAY_SIGNING_SECRET", "")
17-18     codex  gpt-6.2-astra (high)  2026-03-19 13:48:22 UTC  01958f24  Rotate signing secrets
          Security wants signing secrets rotated without downtime. Sign with the current s…
     17 │ # Still accepted by verify() while receivers pick up the new secret.
     18 │ SIGNING_SECRET_PREVIOUS = os.environ.get("RELAY_SIGNING_SECRET_PREVIOUS", "")
```

## trace

```console
$ uv run papertrail trace relay/config.py:6
[0] claude-code  claude-opus-5-5 (high)  2026-03-12 16:06:08 UTC  b27e5f90  Incident 2291: duplicate charges
    Lineage   #4
    Asked     Incident 2291: Fabrikam's endpoint timed out after it had already processed the event, so each of our 8 attempts created another charge. Cap attempts at 3 until receivers can drop repeats. Leave the timeout alone, someone tuned it by hand.
    Session   b27e5f90-61c3-4d8a-a4f2-9e0c7d135b68 in .
    Resume    claude --resume b27e5f90-61c3-4d8a-a4f2-9e0c7d135b68
    Log       ~/papertrail/demo/logs/claude/relay/b27e5f90-61c3-4d8a-a4f2-9e0c7d135b68.jsonl
    After     README.md, relay/delivery.py, tests/test_delivery.py
[1] codex  gpt-6.2-astra (high)  2026-03-10 08:22:22 UTC  019573e8  Raise the retry limit
    Lineage   #4 <- #2
    Asked     Northwind was down for 20 minutes last night and 312 events were parked because we gave up after 5 tries. Raise the limit to 8 so the backoff covers an outage that long.
    Session   019573e8-9b1f-7a42-8c05-d4e6a1b7f390 in .
    Resume    codex resume 019573e8-9b1f-7a42-8c05-d4e6a1b7f390
    Log       ~/papertrail/demo/logs/codex/2026/03/10/rollout-2026-03-10T08-21-00-019573e8-9b1f-7a42-8c05-d4e6a1b7f390.jsonl
[2] claude-code  claude-opus-5-5  2026-03-02 09:15:08 UTC  3f9a1c52  Scaffold the webhook relay
    Lineage   #4 <- #2 <- #0
    Asked     Build a small worker that delivers webhook events to customer endpoints. Sign the body with HMAC-SHA256, POST it, and retry when the endpoint does not accept it. Keep the settings in one module. Standard library only.
    Session   3f9a1c52-7b1e-4c6a-9d2f-0a61c5e8b701 in .
    Resume    claude --resume 3f9a1c52-7b1e-4c6a-9d2f-0a61c5e8b701
    Log       ~/papertrail/demo/logs/claude/relay/3f9a1c52-7b1e-4c6a-9d2f-0a61c5e8b701.jsonl
    After     relay/signing.py, relay/delivery.py, README.md
```

## session

```console
$ uv run papertrail session b27e5f90
2026-03-12 16:05:47 UTC Incident 2291: Fabrikam's endpoint timed out after it had already processed the event, so each of our 8 attempts created another charge. Cap attempts at 3 until receivers can drop repeats. Leave the timeout alone, someone tu…
2026-03-12 16:06:08 UTC   Edit  relay/config.py
2026-03-12 16:06:22 UTC   Edit  README.md
2026-03-12 16:07:09 UTC Send the event id as an Idempotency-Key header so receivers can drop repeats. Document it for them and add a test.
2026-03-12 16:07:23 UTC   Edit  relay/delivery.py
2026-03-12 16:07:37 UTC   Edit  tests/test_delivery.py
2026-03-12 16:07:51 UTC   Edit  README.md
```

## crossings

```console
$ uv run papertrail crossings
3 sessions  relay/config.py  6-6  b27e5f90, 019573e8, 3f9a1c52
3 sessions  relay/delivery.py  40-46  e5d09a37, 019560a1, 3f9a1c52
2 sessions  README.md  8-8  b27e5f90, 3f9a1c52
2 sessions  relay/config.py  12-14  019560a1, 3f9a1c52
2 sessions  relay/delivery.py  30-36  8c41d7e0, 3f9a1c52
2 sessions  relay/delivery.py  (historical)  01958f24, 3f9a1c52
2 sessions  relay/signing.py  16-18  01958f24, 3f9a1c52
2 sessions  tests/test_delivery.py  6-6  e5d09a37, 3f9a1c52
```

## stats

```console
$ uv run papertrail stats
{
  "relationships": 239,
  "agents": 2,
  "sessions": 8,
  "prompts": 9,
  "edits": 23,
  "hunks": 25,
  "files": 6,
  "ranges": 33,
  "lines": {
    "agent": 199,
    "baseline": 0,
    "external": 2,
    "drift": 2,
    "unknown": 0
  },
  "files_by_status": {
    "drift": 1,
    "exact": 5
  },
  "unapplied_patches": 0,
  "external_events": 1,
  "by_agent": {
    "claude-code": {
      "sessions": 5,
      "edits": 16,
      "lines": 173,
      "unplaced": 0
    },
    "codex": {
      "sessions": 3,
      "edits": 7,
      "lines": 26,
      "unplaced": 0
    }
  },
  "agent_fraction": 0.9802955665024631
}
```
