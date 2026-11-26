# AGENTS.md

## Harness

- Attribution prefers coverage over caution: when the logs show an agent session changing a file but the evidence is incomplete (a script write without a recorded diff, a replay whose context no longer matches), attribute the affected lines to the most likely session instead of leaving them unknown or drift. Mark such attributions as inferred, so the UI, queries and stats can tell proven from inferred ownership. Proven ownership still needs proof, and a change made by another agent CLI, git or the user is never marked proven for a session.
