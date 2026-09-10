#!/usr/bin/env bash
# Live sweep of slot activation against a running box (default: mcapp.local).
#
# For every populated non-active slot it activates the slot through the API,
# waits for the update runner to finish, and asserts that code, webapp bundle
# and services switched while the database did not change. It then returns to
# the starting slot the same way. Run it after deploying a release whose runner
# knows `--mode activate`; older slots carry older runners, so the sweep
# returns from such a slot by invoking the NEW slot's runner directly over ssh
# (the documented manual escape hatch, see doc/operations-reference.md).
#
# Usage: scripts/slot_activation_sweep.sh [host]      # exit 0 = every assertion held
set -euo pipefail

HOST="${1:-mcapp.local}"
API="https://${HOST}"
RUNNER="http://${HOST}:2985"
CURL=(curl -sk --max-time 10)
FAILS=0
LOG="${SWEEP_LOG:-/tmp/slot-sweep-$(date +%Y%m%d-%H%M%S).log}"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG"; }
ok() { log "  [OK]   $*"; }
fail() { log "  [FAIL] $*"; FAILS=$((FAILS + 1)); }
assert_eq() { # label expected actual
  if [[ "$2" == "$3" ]]; then ok "$1: $3"; else fail "$1: expected '$2', got '$3'"; fi
}

remote() { ssh "$HOST" "$@"; }

active_slot() { remote 'basename "$(readlink -f ~/mcapp-slots/current)"' | sed 's/slot-//'; }
slot_version() { remote "python3 -c 'import json;print(json.load(open(\"/home/martin/mcapp-slots/meta/slot-$1.json\")).get(\"version\") or \"\")'"; }
api_version() { "${CURL[@]}" "${API}/api/status" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))'; }
served_version() { "${CURL[@]}" "${API}/webapp/version.html" | tr -d '[:space:]'; }
runner_has_activate() { remote "grep -q '\"activate\"' ~/mcapp-slots/slot-$1/scripts/update-runner.py && echo yes || echo no"; }

# DB fingerprint: rows older than the sweep start must stay exactly as they were.
db_fingerprint() {
  remote "python3 - '$1' << 'PYEOF'
import sqlite3, sys
cut = int(sys.argv[1])
c = sqlite3.connect('file:/var/lib/mcapp/messages.db?mode=ro', uri=True)
v = c.execute('select version from schema_version').fetchone()[0]
n, s = c.execute('select count(*), coalesce(sum(rowid),0) from messages where timestamp < ?', (cut,)).fetchone()
p = c.execute('select count(*) from station_positions').fetchone()[0]
print(f'schema={v} rows_before_cut={n} rowid_sum={s} positions>={p}')
PYEOF"
}

wait_runner_result() { # -> prints the result JSON, or 'timeout'
  local _i
  for _i in $(seq 1 90); do
    local body
    body=$("${CURL[@]}" "${RUNNER}/status" 2>/dev/null || true)
    if [[ -n "$body" ]] && python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("result") else 1)' <<< "$body" 2>/dev/null; then
      python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["result"]))' <<< "$body"
      return
    fi
    sleep 5
  done
  echo timeout
}

wait_service_settled() { # wait for the runner port to close and the API to answer again
  local _i
  for _i in $(seq 1 60); do
    if ! "${CURL[@]}" "${RUNNER}/status" >/dev/null 2>&1 && [[ -n "$(api_version 2>/dev/null || true)" ]]; then
      return
    fi
    sleep 5
  done
}

verify_slot() { # slot expected_version step_start_epoch cut_ms baseline_fp
  local slot="$1" want="$2" t0="$3" cut="$4" base="$5"
  assert_eq "current symlink" "$slot" "$(active_slot)"
  assert_eq "/api/status version" "$want" "$(api_version)"
  assert_eq "served webapp version" "$want" "$(served_version)"
  assert_eq "services active" "active active active" "$(remote 'systemctl is-active mcapp mcapp-ble lighttpd | xargs')"
  local ble_start
  ble_start=$(remote 'date -d "$(systemctl show mcapp-ble -p ActiveEnterTimestamp --value)" +%s')
  if (( ble_start >= t0 )); then ok "mcapp-ble restarted"; else fail "mcapp-ble not restarted (started $ble_start < $t0)"; fi
  assert_eq "database untouched" "$base" "$(db_fingerprint "$cut")"
  assert_eq "no webapp.old/.new left" "" "$(remote 'ls -d /var/www/html/webapp.old /var/www/html/webapp.new 2>/dev/null | xargs')"
  assert_eq "runner exited clean" "0" "$(remote 'systemctl show mcapp-update -p ExecMainStatus --value')"
}

activate_via_api() { # slot
  local code
  code=$("${CURL[@]}" -o /tmp/sweep-activate.json -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' -d "{\"slot\": $1}" "${API}/api/update/activate")
  assert_eq "POST /api/update/activate slot $1" "200" "$code"
  local result
  result=$(wait_runner_result)
  log "  runner result: $result"
  if python3 -c 'import sys,json;d=json.loads(sys.argv[1]);sys.exit(0 if d.get("status") in ("success","warning") else 1)' "$result"; then
    ok "runner status accepted"
  else
    fail "runner status: $result"
  fi
  wait_service_settled
}

activate_via_runner() { # slot runner_slot  (manual escape hatch: run the NEW slot's runner)
  remote "sudo /home/martin/mcapp-slots/slot-$2/.venv/bin/python3 /home/martin/mcapp-slots/slot-$2/scripts/update-runner.py --mode activate --slot $1 > /tmp/sweep-runner.log 2>&1; echo EXIT=\$?" | tee -a "$LOG"
  wait_service_settled
}

# ── main ────────────────────────────────────────────────────────────────
log "slot activation sweep against ${HOST} (log: ${LOG})"
START_SLOT=$(active_slot)
START_VERSION=$(slot_version "$START_SLOT")
CUT_MS=$(( $(date +%s) * 1000 ))
BASE_FP=$(db_fingerprint "$CUT_MS")
log "start: slot-${START_SLOT} ${START_VERSION}; db ${BASE_FP}"
assert_eq "starting slot runner knows activate" "yes" "$(runner_has_activate "$START_SLOT")"

# 400 for the active slot and for an out-of-range slot, before touching anything
for bad in "$START_SLOT" 7; do
  code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d "{\"slot\": $bad}" "${API}/api/update/activate")
  assert_eq "activate slot $bad rejected" "400" "$code"
done

for target in 0 1 2; do
  [[ "$target" == "$START_SLOT" ]] && continue
  ver=$(slot_version "$target")
  if [[ -z "$ver" ]]; then log "slot-${target} empty, skipped"; continue; fi
  log "── activate slot-${target} (${ver}) via API"
  T0=$(date +%s)
  activate_via_api "$target"
  verify_slot "$target" "$ver" "$T0" "$CUT_MS" "$BASE_FP"

  log "── return to slot-${START_SLOT} (${START_VERSION})"
  T0=$(date +%s)
  if [[ "$(runner_has_activate "$target")" == "yes" ]]; then
    activate_via_api "$START_SLOT"
  else
    log "  slot-${target} carries a pre-activate runner; using slot-${START_SLOT}'s runner directly"
    activate_via_runner "$START_SLOT" "$START_SLOT"
  fi
  verify_slot "$START_SLOT" "$START_VERSION" "$T0" "$CUT_MS" "$BASE_FP"
done

log "sweep finished with ${FAILS} failed assertion(s)"
exit $(( FAILS > 0 ))
