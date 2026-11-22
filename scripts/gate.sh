#!/usr/bin/env bash
# One release gate for both repos: MCProxy (this repo) and the webapp sibling.
#
#   ./scripts/gate.sh            # both repos
#   ./scripts/gate.sh mcproxy    # this repo only
#   ./scripts/gate.sh webapp     # webapp only
#
# Runs every step even after a failure, writes each step's output to its own log,
# and ends with exactly one line: "GATE: PASS" or "GATE: FAIL (<steps>)", with exit
# code 0 or 1. Read the exit code from this script directly, never through a pipe
# into tail/head (the pipe reports tail's exit code).
#
# CI's `tests` workflow is disabled; this script is the release gate the
# prod-release and dev-release skills require. bash 3.2 compatible (macOS).

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBAPP="${WEBAPP_DIR:-$REPO/../webapp}"
SCOPE="${1:-all}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mcapp-gate.XXXXXX")"

case "$SCOPE" in
  all | mcproxy | webapp) ;;
  *)
    echo "usage: $0 [all|mcproxy|webapp]" >&2
    exit 2
    ;;
esac

FAILED=""

step() {
  # step <name> <dir> <command...>
  local name="$1" dir="$2"
  shift 2
  local log="$LOG_DIR/$name.log" start rc
  start=$(date +%s)
  (cd "$dir" && "$@") >"$log" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    printf 'PASS  %-24s %3ss\n' "$name" "$(($(date +%s) - start))"
  else
    printf 'FAIL  %-24s %3ss  (exit %s, log %s)\n' "$name" "$(($(date +%s) - start))" "$rc" "$log"
    tail -n 15 "$log" | sed 's/^/      | /'
    FAILED="$FAILED $name"
  fi
}

echo "gate: scope=$SCOPE logs=$LOG_DIR"

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "mcproxy" ]; then
  step mcproxy-ruff-check "$REPO" uvx ruff check
  step mcproxy-ruff-format "$REPO" uvx ruff format --check .
  step mcproxy-mypy "$REPO" uv run mypy src/mcapp ble_service/src
  step mcproxy-startup-tests "$REPO" uv run python scripts/run_startup_tests.py
fi

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "webapp" ]; then
  if [ ! -f "$WEBAPP/package.json" ]; then
    echo "FAIL  webapp                   not found at $WEBAPP (set WEBAPP_DIR)"
    FAILED="$FAILED webapp-missing"
  else
    step webapp-typecheck "$WEBAPP" npm run typecheck
    step webapp-lint "$WEBAPP" npm run lint
    step webapp-format "$WEBAPP" npm run format:check
    step webapp-test "$WEBAPP" npm test
  fi
fi

if [ -z "$FAILED" ]; then
  echo "GATE: PASS"
  exit 0
fi
echo "GATE: FAIL (${FAILED# })"
exit 1
