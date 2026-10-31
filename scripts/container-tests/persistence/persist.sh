#!/usr/bin/env bash
# Persistence tests P1/P3/P4 for the mcapp container image. See README.md.
# Usage: persist.sh [image:tag]   (default mcapp-spike:arm64)
set -euo pipefail
IMG="${1:-mcapp-spike:arm64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
CFG="$ROOT/scripts/container-tests/image/config.example.json"
BACKUP="${MCAPP_BACKUP_DIR:-/Users/martinwerner/WebDev/mcapp-local-backup-2026-10-20/mcapp-backup-2026-10-20_pre-slot-test}"
TAG="mcapp-ct-p$$"
PORT=2992
BASE="http://127.0.0.1:$PORT"
TMP="$(mktemp -d)"
CONTAINERS=()
VOLUMES=()

cleanup() {
  local c v
  for c in "${CONTAINERS[@]:-}"; do [ -n "$c" ] && docker rm -f "$c" >/dev/null 2>&1 || true; done
  for v in "${VOLUMES[@]:-}"; do [ -n "$v" ] && docker volume rm "$v" >/dev/null 2>&1 || true; done
  rm -rf "$TMP"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; [ -n "${LAST:-}" ] && docker logs --tail 30 "$LAST" >&2 || true; exit 1; }
t0=$SECONDS
step() { echo "[$((SECONDS - t0))s] $*"; }

mkvol() { docker volume create "$1" >/dev/null; VOLUMES+=("$1"); }

# start_app <name> <volume> [extra docker run args...]
start_app() {
  local name="$1" vol="$2"; shift 2
  CONTAINERS+=("$name"); LAST="$name"
  docker run -d --name "$name" --read-only --tmpfs /tmp \
    -v "$vol:/var/lib/mcapp" -v "$CFG:/etc/mcapp/config.json:ro" \
    -p "127.0.0.1:$PORT:2981" "$@" "$IMG" >/dev/null
  local s=""
  for _ in $(seq 1 45); do
    s=$(docker inspect --format '{{.State.Health.Status}}' "$name")
    [ "$s" = healthy ] && return 0
    [ "$(docker inspect --format '{{.State.Running}}' "$name")" = true ] || fail "$name exited"
    sleep 2
  done
  fail "$name health: $s"
}

# total message count from the SSE connect burst (sum of proxy:conversations counts)
msg_count() {
  { curl -sN --max-time 5 "$BASE/events" || true; } | python3 -c '
import json, sys
ev = None
for line in sys.stdin:
    line = line.rstrip("\n")
    if line.startswith("event: "):
        ev = line[7:]
    elif line.startswith("data: ") and ev == "proxy:conversations":
        print(sum(v.get("count", 0) for v in json.loads(line[6:])["data"].values()))
        break
else:
    print(0)'
}

sha() { docker exec "$1" sha256sum "/var/lib/mcapp/$2" 2>/dev/null | cut -d' ' -f1; }

# ---------------------------------------------------------------- P1
step "P1: fresh volume, inject datagram, recreate container"
V1="$TAG-p1"; mkvol "$V1"
start_app "$TAG-p1a" "$V1"
step "P1: container healthy"
BEFORE=$(msg_count)
# Inject from a sibling container (private non-loopback source) so the target is learned
# and runtime.json is written; the learned target is the injector, never the real node.
IP=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$TAG-p1a")
CONTAINERS+=("$TAG-p1inj")
docker run --rm --name "$TAG-p1inj" --entrypoint python3 "$IMG" -c '
import json, socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(json.dumps({"type": "msg", "src": "DK9XX-1", "dst": "*", "msg": "ct persist",
  "msg_id": "1AE1E0AA", "src_type": "lora"}).encode(), (sys.argv[1], 1799))' "$IP"
sleep 3
AFTER=$(msg_count)
[ "$AFTER" -gt "$BEFORE" ] || fail "P1 injected message not stored ($BEFORE -> $AFTER)"
echo "P1 messages: $BEFORE -> $AFTER"
curl -fsS "$BASE/api/status" >/dev/null || fail "P1 /api/status"
docker exec "$TAG-p1a" ls -l /var/lib/mcapp | awk 'NR>1{print "  " $5, $9}'
docker exec "$TAG-p1a" sync 2>/dev/null || true
H_VAPID=$(sha "$TAG-p1a" vapid.json)
H_RT=$(sha "$TAG-p1a" runtime.json)
[ -n "$H_VAPID" ] || fail "P1 vapid.json missing before restart"
[ -n "$H_RT" ] || echo "NOTE P1: runtime.json not written (no target learned)"
docker stop -t 20 "$TAG-p1a" >/dev/null
docker rm "$TAG-p1a" >/dev/null
step "P1: container removed, volume kept; starting new container"
start_app "$TAG-p1b" "$V1"
N=$(msg_count)
[ "$N" -ge "$AFTER" ] || fail "P1 messages lost after recreate ($AFTER -> $N)"
[ "$(sha "$TAG-p1b" vapid.json)" = "$H_VAPID" ] || fail "P1 vapid.json sha256 changed"
[ -z "$H_RT" ] || [ "$(sha "$TAG-p1b" runtime.json)" = "$H_RT" ] || fail "P1 runtime.json sha256 changed"
echo "P1 PASS: messages=$N, vapid.json sha256 match, runtime.json $([ -n "$H_RT" ] && echo "sha256 match" || echo absent)"
docker rm -f "$TAG-p1b" >/dev/null

# ---------------------------------------------------------------- P3
step "P3: bare-metal backup DB into fresh volume"
[ -f "$BACKUP/messages.db" ] || fail "backup DB not found at $BACKUP"
mkdir "$TMP/db"
for f in messages.db messages.db-wal messages.db-shm; do
  [ -f "$BACKUP/$f" ] && cp "$BACKUP/$f" "$TMP/db/$f"
done
V3="$TAG-p3"; mkvol "$V3"
CONTAINERS+=("$TAG-p3load")
docker run --rm --name "$TAG-p3load" --user 0 --entrypoint sh -v "$V3:/data" -v "$TMP/db:/src:ro" "$IMG" \
  -c 'cp /src/* /data/ && chown -R 10001:10001 /data' || fail "P3 copy into volume"
echo "  secrets (secret.key etc.) deliberately NOT copied: QRZ / Node Admin / feed credentials need re-entry"
start_app "$TAG-p3" "$V3"
step "P3: healthy"
LATEST=$(sed -n 's/^LATEST_SCHEMA_VERSION *= *\([0-9]*\).*/\1/p' "$ROOT/src/mcapp/storage/constants.py")
MIGS=$(docker logs "$TAG-p3" 2>&1 | grep -c "Migration" || true)
[ "$MIGS" -gt 0 ] || fail "P3 no 'Migration' lines in logs"
SV=$(docker exec "$TAG-p3" python3 -I -c '
import sqlite3
c = sqlite3.connect("file:/var/lib/mcapp/messages.db?mode=ro", uri=True)
print(c.execute("PRAGMA user_version").fetchone()[0])' 2>/dev/null || echo "?")
if [ "$SV" = "0" ] || [ "$SV" = "?" ]; then
  SV=$(docker exec "$TAG-p3" python3 -I -c '
import sqlite3
c = sqlite3.connect("file:/var/lib/mcapp/messages.db?mode=ro", uri=True)
t = [r[0] for r in c.execute("select name from sqlite_master where type=\"table\" and name like \"%version%\"")]
print(max(c.execute("select max(version) from %s" % t[0]).fetchone()[0], 0) if t else "?")' 2>/dev/null || echo "?")
fi
[ "$SV" = "$LATEST" ] || fail "P3 schema version $SV != LATEST $LATEST"
N3=$(msg_count)
[ "$N3" -gt 0 ] || fail "P3 API shows 0 messages"
echo "P3 PASS: healthy, migration log lines=$MIGS, schema=$SV (LATEST=$LATEST), messages via API=$N3"
docker rm -f "$TAG-p3" >/dev/null

# ---------------------------------------------------------------- P4
step "P4: json-file log rotation config"
V4="$TAG-p4"; mkvol "$V4"
start_app "$TAG-p4" "$V4" --log-opt max-size=1m --log-opt max-file=2
CFGLOG=$(docker inspect --format '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$TAG-p4")
[ "$CFGLOG" = "json-file 1m 2" ] || fail "P4 log config: $CFGLOG"
echo "P4 PASS: LogConfig = $CFGLOG (rotation not exercised)"
step "ALL OK"
