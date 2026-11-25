#!/usr/bin/env bash
# One-step launcher for the Paper Trail reader.
#
#   ./start.sh                 graph for this repository, built from your own agent logs
#   ./start.sh ~/code/my-app   graph for another project
#   ./start.sh demo            bundled made-up project and sessions (no private logs involved)
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
demo=0
[[ "$target" == "demo" ]] && demo=1

command -v uv >/dev/null || {
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}
uv sync --locked --quiet

mkdir -p .papertrail/ui
if [[ "$demo" == 1 ]]; then
  target="$repo/demo/project"
  store="$target/.papertrail/graph.rl"
  # The repository ships this graph. Its paths are relative to demo/, so it opens anywhere.
  ingest=(--claude-logs demo/logs/claude --codex-logs demo/logs/codex --project "$target" --portable demo)
else
  [[ -d "$target" ]] || {
    echo "Not a directory: $target" >&2
    exit 1
  }
  target="$(cd "$target" && pwd)"
  # The project's own folder, where the command line and the agent skill look too.
  store="$target/.papertrail/graph.rl"
  old="$repo/.papertrail/ui/$(printf '%s' "$target" | tr '/ ' '__').rl"
  if [[ -f "$old" && ! -e "$store" ]]; then
    mkdir -p "$target/.papertrail"
    [[ -e "$target/.papertrail/.gitignore" ]] || echo '*' >"$target/.papertrail/.gitignore"
    mv "$old" "$store"
  fi
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

args=(--store "$store" --project "$target" --port "$port")
[[ -n "${NO_BROWSER:-}" ]] && args+=(--no-browser)
echo "Paper Trail reader: http://127.0.0.1:$port  (Ctrl+C to stop)"
exec uv run --locked papertrail ui "${args[@]}"
