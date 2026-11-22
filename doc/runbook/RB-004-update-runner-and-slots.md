# RB-004 Update runner, slot activation and converge

| Field | Value |
|---|---|
| ID | RB-004 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 397-514; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Update Runner", "Activation", "System epoch & converge"); `scripts/update-runner.py`; `bootstrap/lib/converge.sh` |

## 1. Purpose and scope

Operating the OTA update runner: the slot layout, the trigger path, activation of an older slot, and converge after a system change. For the acceptance test of a converge see `../sop/SOP-002-converge-acceptance-test.md`.

The update runner (`scripts/update-runner.py`) is a standalone Python HTTP server (stdlib only, no dependencies) that manages OTA deployments and rollbacks from the webapp UI. It runs on **port 2985** and uses a slot-based architecture with 3 independent deployment slots.

**Launch mechanism:** A systemd `.path` trigger watches for `/var/lib/mcapp/update-trigger`. When the frontend calls `POST /api/update/start`, the SSE handler writes an args file and trigger file, systemd detects the trigger and launches `mcapp-update.service`.

**Systemd units** (templates in `bootstrap/templates/`):

- `mcapp-update.path` — Watches for `/var/lib/mcapp/update-trigger`
- `mcapp-update.service` — Oneshot service running `update-runner.py` with 15-minute timeout

**Update runner endpoints (port 2985):**

| Endpoint  | Method | Purpose                                                                             |
| --------- | ------ | ----------------------------------------------------------------------------------- |
| `/stream` | GET    | SSE stream — real-time bootstrap output (`phase`, `log`, `health`, `result` events) |
| `/status` | GET    | JSON — mode, result, slot info, active slot, can_rollback flag                      |
| `/slots`  | GET    | JSON — version, deployed_at, active flags for all 3 slots                           |

**Main API endpoints (port 2981, proxied via port 80):**

| Endpoint               | Method | Purpose                                                          |
| ---------------------- | ------ | ---------------------------------------------------------------- |
| `/api/update/check`    | GET    | Check GitHub for available releases (5-min cache)                |
| `/api/update/start`    | POST   | Launch update runner (optional body: `{"dev": true}`)            |
| `/api/update/activate` | POST   | Activate a specific slot: `{"slot": N}` (400 on an invalid slot) |
| `/api/update/rollback` | POST   | Compat alias — activates the newest non-active slot              |
| `/api/update/slots`    | GET    | Get slot metadata and active status                              |
| `/api/update/converge` | POST   | Launch update runner in converge mode (system epoch)             |

**Slot architecture on Pi:**

```
~/mcapp-slots/
├── current → symlink to active slot-N
├── slot-0/              # Independent deployment slot
│   ├── .venv/           # Python venv for this slot
│   ├── scripts/         # Includes update-runner.py
│   ├── src/mcapp/       # Application code
│   └── webapp/          # Frontend files + version.html
├── slot-1/
├── slot-2/
└── meta/
    ├── slot-N.json      # {"slot": N, "version": "v1.x.y", "deployed_at": "...", "active": true}
    └── slot-N.etc.tar.gz # /etc config snapshot (backup only, never restored automatically)
```

**Update sequence:**

1. **Prepare** — determine active vs target slot
2. **Snapshot** — tar `/etc/mcapp/`, systemd units and lighttpd config into `meta/slot-N.etc.tar.gz` (backup only)
3. **Bootstrap** — run `bootstrap/mcapp.sh --skip [--dev]` into target slot (15-min timeout)
4. **Activate** — atomic symlink swap to the target slot (the bootstrap has already installed the webapp bundle and restarted the services)
5. **Health check** — 8 retries × 3s: mcapp service, lighttpd, webapp HTTP, SSE health, lighttpd proxy. Caddy is the front door on LAN boxes; the check list is in `bootstrap/lib/health.sh` and `scripts/update-runner.py` and is not fully aligned yet (`backlog.md` B15).
6. **Auto-rollback** — on health failure: run the same activation routine against the previous slot (code + webapp + services; the database is never touched)

### Activation

Activating a slot N means: stop `mcapp`, swap the `current` symlink to `slot-N`, re-install that
slot's `webapp/` bundle into `/var/www/html/webapp` (staged as `webapp.new`, the previous bundle
kept as `webapp.old` until the move succeeds), `daemon-reload`, restart `lighttpd` + `mcapp` +
`mcapp-ble`, refresh the slot's `deployed_at`, then run the health checks. It fails
(`invalid_slot`) if the requested slot is already active, empty, out of range, or has no
`pyproject.toml`. Result: `{status: success|warning, version, slot, health_ok}`.

The runner has an `activate` mode (`--slot N`, or an args file
`{"mode": "activate", "dev": false, "slot": N}`) that performs exactly this, driven by
`POST /api/update/activate {"slot": N}`. `rollback` is a compatibility alias that activates the
newest non-active slot; `POST /api/update/rollback` still calls it. The `update` mode's internal
auto-rollback (triggered by a health-check failure) uses the same activation routine against the
previously active slot.

**The database is never restored as part of activation, on any path — deliberately.** The old
rollback overwrote `/var/lib/mcapp/messages.db` with a `meta/slot-N.db` snapshot taken when slot N
was last _left_ through the runner. Deploys made via `mcapp.sh` directly never refreshed that
snapshot, so on mcapp.local the stored snapshots were found to be 4 days to 3 weeks stale
(2026-09-10), before anyone had ever pressed the button in anger. Migrations are forward-only and
additive, and the schema marker is never written downward, so older application code runs fine
against a newer database — there is nothing for a database restore to fix, and every restore was a
strictly regressive, silent data-loss risk. `snapshot_database`, `restore_database`, and
`restore_etc` have been removed. The old code also never re-installed the webapp bundle (it served
from a directory copied once at deploy time, not from the `current` symlink) and never restarted
`mcapp-ble`, so a "rollback" could leave the previous code running against a stale frontend and a
BLE service still running the previous slot's code; activation now handles all three.

### System epoch & converge

`SYSTEM_EPOCH` is a single integer versioning the _system-level_ machine state (packages, firewall, web front door) — independent of the app version and of the database schema version. Current value: `SYSTEM_EPOCH=10` (checked 2026-11-10). Installed state is marked by `/var/lib/mcapp/system-epoch` (single integer line, written by root). Source of truth is `readonly SYSTEM_EPOCH=1` in `bootstrap/mcapp.sh`, mirrored by `REQUIRED_SYSTEM_EPOCH` in `src/mcapp/system_converge.py`; a gated startup test parses `mcapp.sh` and enforces the two stay equal.

**`--converge` runs only what changed.** Each step in `bootstrap/lib/converge.sh` carries the epoch it last changed in and runs only if the installed epoch is below it. When you change a step, raise its row to the new epoch in the same commit. `ensure_web_frontend` always runs. A full `mcapp.sh` run still runs everything.

The update runner (`scripts/update-runner.py`) has four modes:

- `update` — deploy + activate + health check, rolls back (via activation) on failure
- `activate` — activate a specific slot (`--slot N`, or `{"mode": "activate", "slot": N}`); see Activation above
- `rollback` — compat alias for `activate` against the newest non-active slot
- `converge` — re-run the active slot's own `mcapp.sh --converge` to bring system-level state up to date; no slot swap, no rollback on failure

`run_update` runs the newly deployed slot's `mcapp.sh --converge` after health checks pass, so every successful update also converges system state.

**Fleet upgrade story:** pre-epoch boxes (<= v1.6.13) update via the OLD update runner driven by the OLD `bootstrap/mcapp.sh --skip`, which never installs Caddy or opens :443 — the box lands on new code with a degraded, HTTP-only front door. The new app's converge watchdog (`src/mcapp/system_converge.py`) detects the stale epoch, waits for the update runner to go idle (~1-2 min settle), and triggers `converge` mode automatically via the same trigger-file mechanism as the webapp's Update button. It makes one trigger attempt per boot and re-checks every 6 hours; on failure (e.g. no WAN) the box stays degraded-but-working over HTTP rather than retry-looping.

**Troubleshooting:**

```bash
# Installed system epoch on the Pi
ssh mcapp.local "cat /var/lib/mcapp/system-epoch"

# Watchdog activity in the logs
ssh mcapp.local "sudo journalctl -u mcapp.service | grep 'Converge watchdog'"

# Manual trigger via the API (mirrors the webapp Update button)
curl -X POST http://mcapp.local/api/update/converge

# Manual fallback, run directly on the Pi
sudo ~/mcapp-slots/current/bootstrap/mcapp.sh --converge
```

