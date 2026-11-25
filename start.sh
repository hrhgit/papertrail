#!/usr/bin/env bash
# One-step launcher for the Paper Trail reader.
#
#   ./start.sh                 graph for this repository, built from your own agent logs
#   ./start.sh ~/code/my-app   graph for another project
#   ./start.sh demo            bundled made-up sessions (no private logs involved)
#   ./start.sh -r [target]     rebuild the graph first (after new sessions or edits)
#
# Environment: PORT (default 8765, next free port is used if taken), NO_BROWSER=1.
set -euo pipefail

cd "$(dirname "$0")"
repo="$PWD"

refresh=0
if [[ "${1:-}" == "-r" || "${1:-}" == "--refresh" ]]; then
  refresh=1
  shift
fi
target="${1:-$repo}"

command -v uv >/dev/null || {
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}
uv sync --locked --quiet

mkdir -p .papertrail/ui
if [[ "$target" == "demo" ]]; then
  store="$repo/.papertrail/ui/demo.rl"
  ingest=(--claude-logs tests/fixtures/logs --codex-logs tests/fixtures/codex-sessions
    --project tests/fixtures/project)
else
  [[ -d "$target" ]] || {
    echo "Not a directory: $target" >&2
    exit 1
  }
  target="$(cd "$target" && pwd)"
  # One store per project, kept inside this repository's ignored folder.
  store="$repo/.papertrail/ui/$(printf '%s' "$target" | tr '/ ' '__').rl"
  ingest=(--project "$target")
fi

if [[ "$refresh" == 1 || ! -f "$store" ]]; then
  echo "Building the graph for: $target (reads local logs, can take up to a minute)"
  uv run --locked papertrail ingest "${ingest[@]}" --store "$store" --json >/dev/null
else
  echo "Using the existing graph. Run './start.sh -r' to rebuild it from current logs."
fi

port="$(uv run --locked python - "${PORT:-8765}" <<'PY'
import socket, sys
port = int(sys.argv[1])
while True:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)):
            print(port)
            break
    port += 1
PY
)"

args=(--store "$store" --port "$port")
[[ -n "${NO_BROWSER:-}" ]] && args+=(--no-browser)
echo "Paper Trail reader: http://127.0.0.1:$port  (Ctrl+C to stop)"
exec uv run --locked papertrail ui "${args[@]}"
