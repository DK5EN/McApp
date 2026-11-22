# RB-007 Stalls: query, replay and memory checks

| Field | Value |
|---|---|
| ID | RB-007 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 515-586, 730-735; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Stall Tracking", "Uptime / API feels slow", "Boot memory settings"); `scripts/replay_stall.py` |

## 1. Purpose and scope

Reading recorded stalls through `/api/stalls`, reproducing one with `replay_stall.py`, and checking the boot memory settings after a boot-config change. The method for the investigation is `../sop/SOP-006-stall-investigation.md`.

Records every server- and client-observed stall (HTTP request, SSE round trip, event-loop drift,
thread-pool wait, publish-loop handler) into `stall_events`, retrievable by a coding agent through
one endpoint — no UI. Design, kinds/thresholds and the record schema:
`../archive/2026-09-15_1530-stall-tracking-plan.md`; the table itself: `../reference/database-reference.md`;
load-bearing implementation facts: `CLAUDE.md` "Stall Tracking".

**Endpoints:**

```bash
# Full records, newest first — what a coding agent reads to diagnose a specific slow call
curl -s 'http://mcapp.local/api/stalls?limit=50' | python3 -m json.tool

# Only critical HTTP stalls in the last hour
curl -s "http://mcapp.local/api/stalls?kind=http&severity=critical&since=$(python3 -c 'import time; print(int(time.time()*1000) - 3600000)')"

# Per-path p50/p95/p99/max baseline over recorded `http` rows
curl -s 'http://mcapp.local/api/stalls/summary' | python3 -m json.tool

# Webapp client upload (one record or an array); the endpoint this feeds, not a manual tool
curl -s -X POST http://mcapp.local/api/stalls/client -H 'Content-Type: application/json' \
  -d '{"kind":"client_error","severity":"critical","path":"/api/messages"}'
```

**Config** — `stalls` key in `/etc/mcapp/config.json` (nested object; absent key or absent
sub-key = default):

```json
{
  "stalls": {
    "stall_ms": 500,
    "critical_ms": 2000,
    "sample_every": 50,
    "loop_lag_ms": 100,
    "pool_wait_ms": 100,
    "handler_ms": 500,
    "body_cap_bytes": 8192,
    "max_rows": 5000
  }
}
```

**Replay a recorded stall** against a running instance:

```bash
# Re-issue row 42's exact request 10x, print p50/p95/max next to the recorded duration
uv run python scripts/replay_stall.py --base http://mcapp.local --id 42

# More iterations, or replay from a row saved locally (e.g. from a curl above)
uv run python scripts/replay_stall.py --base http://mcapp.local --json saved_row.json --repeat 20

# Just the per-path baseline table
uv run python scripts/replay_stall.py --base http://mcapp.local --summary
```

**Boot memory settings (B4) — reboot required, check it landed:**

```bash
# Was a boot-config change written but not yet applied? (marker cleared by a reboot)
ssh mcapp.local "test -f /var/lib/mcapp/reboot-required && echo 'reboot required' || echo 'up to date'"

# GPU memory actually reserved (should read 16M after gpu_mem=16 + a reboot)
ssh mcapp.local "vcgencmd get_mem arm; vcgencmd get_mem gpu"

# CMA pool size actually in effect (should read ~64 MB after cma-64 + a reboot)
ssh mcapp.local "grep -i cma /proc/meminfo"

# cmdline.txt must be exactly one line, with cgroup_enable=memory present
ssh mcapp.local "cat /proc/cmdline"
```


### Uptime / API feels slow


`GET /api/stalls/summary` first — it gives per-path p50/p95/p99/max without digging through
journal logs. Follow up with `GET /api/stalls?severity=critical` for the specific rows, then
`scripts/replay_stall.py --id <row>` to check whether it reproduces. See "Stall Tracking" above.
