# Trace a greeting across Claude Code and Codex

Captured from the fictional fixtures. `$ROOT` stands for the absolute checkout directory. Elapsed time varies by machine.

## ingest

```console
$ uv run --locked papertrail ingest --claude-logs tests/fixtures/logs --codex-logs tests/fixtures/codex-sessions --project tests/fixtures/project --store .papertrail/mixed-demo.rl
{
  "relationships": 73,
  "agents": 2,
  "sessions": 5,
  "prompts": 5,
  "edits": 7,
  "hunks": 10,
  "files": 3,
  "ranges": 6,
  "lines": {
    "agent": 3,
    "baseline": 1,
    "external": 1,
    "drift": 1,
    "unknown": 0
  },
  "files_by_status": {
    "drift": 1,
    "exact": 2
  },
  "unapplied_patches": 0,
  "external_events": 1,
  "by_agent": {
    "claude-code": {
      "sessions": 4,
      "edits": 6,
      "lines": 3,
      "unplaced": 0
    },
    "codex": {
      "sessions": 1,
      "edits": 1,
      "lines": 0,
      "unplaced": 0
    }
  },
  "agent_fraction": 0.5,
  "seconds": 0.035,
  "sources": {
    "claude-code": {
      "failures": {},
      "skipped": {
        "unmatched_calls": 0
      }
    },
    "codex": {
      "failures": {},
      "skipped": {},
      "counts": {
        "log_files": 1,
        "replica_files": 0,
        "child_sessions": 0,
        "user_items": 1,
        "calls_apply_patch": 1,
        "literal_apply_patch": 1,
        "route_apply_patch_success": 1,
        "successful_literal_apply_patch": 1,
        "fallback_file_receipts": 1,
        "retained_files": 1
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
$ uv run --locked papertrail blame tests/fixtures/project/greeting.py --store .papertrail/mixed-demo.rl
    1-1     drift          
    2-2     claude-code  demo-model (max)  2025-01-01T00:00:33+00:00  cedar  Morning greeting  Use a morning greeting and keep the local note.
    3-3     external    2025-01-01T00:00:15+00:00
```

## trace

```console
$ uv run --locked papertrail trace tests/fixtures/project/greeting.py:2 --store .papertrail/mixed-demo.rl
[0] claude-code  demo-model (max)  2025-01-01T00:00:33+00:00  Morning greeting
  Lineage #5
  Instruction Use a morning greeting and keep the local note.
  Session cedar in $ROOT/tests/fixtures/project
  claude --resume cedar 
  Log $ROOT/tests/fixtures/logs/demo/cedar.jsonl
  Previous (none) 
  Next (none)
[1] codex  demo-codex (medium)  2025-01-01T00:00:29+00:00  Try a sunrise greeting.
  Lineage #5 <- #4
  Instruction Try a sunrise greeting.
  Session maple in $ROOT/tests/fixtures/project
  codex resume maple 
  Log $ROOT/tests/fixtures/codex-sessions/2025/01/01/rollout-2025-01-01T00-00-26-maple.jsonl
  Previous (none) 
  Next (none)
[2] claude-code  demo-model (high)  2025-01-01T00:00:23+00:00  Welcome visitors
  Lineage #5 <- #4 <- #3
  Instruction Welcome visitors by name.
  Session birch in $ROOT/tests/fixtures/project
  claude --resume birch 
  Log $ROOT/tests/fixtures/logs/demo/birch.jsonl
  Previous (none) 
  Next $ROOT/tests/fixtures/project/settings.py
[3] claude-code  demo-model  2025-01-01T00:00:07+00:00  Choose a greeting
  Lineage #5 <- #4 <- #3 <- #1
  Instruction Make the greeting informal. Add a usage note.
  Session amber in $ROOT/tests/fixtures/project
  claude --resume amber 
  Log $ROOT/tests/fixtures/logs/demo/amber.jsonl
  Previous $ROOT/tests/fixtures/project/greeting.py 
  Next (none)
[4] claude-code  demo-model  2025-01-01T00:00:03+00:00  Choose a greeting
  Lineage #5 <- #4 <- #3 <- #1 <- #0
  Instruction Start with a simple greeting.
  Session amber in $ROOT/tests/fixtures/project
  claude --resume amber 
  Log $ROOT/tests/fixtures/logs/demo/amber.jsonl
  Previous (none) 
  Next $ROOT/tests/fixtures/project/greeting.py
```

## session

```console
$ uv run --locked papertrail session maple --store .papertrail/mixed-demo.rl
2025-01-01T00:00:27+00:00 Try a sunrise greeting.
2025-01-01T00:00:29+00:00 apply_patch $ROOT/tests/fixtures/project/greeting.py
```

## crossings

```console
$ uv run --locked papertrail crossings tests/fixtures/project/greeting.py --store .papertrail/mixed-demo.rl
4 $ROOT/tests/fixtures/project/greeting.py 2-2 cedar, maple, birch, amber
```

## stats

```console
$ uv run --locked papertrail stats --store .papertrail/mixed-demo.rl
{
  "relationships": 73,
  "agents": 2,
  "sessions": 5,
  "prompts": 5,
  "edits": 7,
  "hunks": 10,
  "files": 3,
  "ranges": 6,
  "lines": {
    "agent": 3,
    "baseline": 1,
    "external": 1,
    "drift": 1,
    "unknown": 0
  },
  "files_by_status": {
    "drift": 1,
    "exact": 2
  },
  "unapplied_patches": 0,
  "external_events": 1,
  "by_agent": {
    "claude-code": {
      "sessions": 4,
      "edits": 6,
      "lines": 3,
      "unplaced": 0
    },
    "codex": {
      "sessions": 1,
      "edits": 1,
      "lines": 0,
      "unplaced": 0
    }
  },
  "agent_fraction": 0.5
}
```
