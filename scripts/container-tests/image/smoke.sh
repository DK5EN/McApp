#!/usr/bin/env bash
# Usage: smoke.sh <tag>   e.g. smoke.sh mcapp-spike:arm64
set -euo pipefail
IMG="${1:?usage: smoke.sh <image:tag>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="mcapp-spike-smoke-$$"
VOL="$NAME-data"
BASE=http://127.0.0.1:2981
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; docker logs --tail 30 "$NAME" >&2 || true; exit 1; }

docker volume create "$VOL" >/dev/null
docker run -d --name "$NAME" --read-only --tmpfs /tmp \
  -v "$VOL:/var/lib/mcapp" -v "$HERE/config.example.json:/etc/mcapp/config.json:ro" \
  -p 127.0.0.1:2981:2981 -p 1799:1799/udp "$IMG" >/dev/null

status=""
for _ in $(seq 1 30); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$NAME")
  [ "$status" = healthy ] && break
  [ "$(docker inspect --format '{{.State.Running}}' "$NAME")" = true ] || fail "container exited"
  sleep 2
done
[ "$status" = healthy ] || fail "health status: $status"
echo "health: $status"

curl -fsS "$BASE/health" >/dev/null || fail "/health"
curl -fsS "$BASE/api/status" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("version:", d.get("version"), "features:", d.get("features"))' || fail "/api/status"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/webapp/")
[ "$code" = 200 ] || fail "/webapp/ -> $code"
curl -fsS "$BASE/webapp/" | grep -qi "<html" || fail "/webapp/ is not index.html"
echo "webapp: 200 + html"
sse=$(curl -sN --max-time 3 "$BASE/events" || true)
echo "$sse" | grep -qE '^(event|data):' || fail "/events produced no SSE lines"
echo "sse: $(echo "$sse" | grep -cE '^(event|data):') lines in 3 s"
docker stats --no-stream --format 'memory: {{.MemUsage}}' "$NAME"
echo "SMOKE OK $IMG"
