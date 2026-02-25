# CLAUDE.md

## Project Overview

McApp is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via SSE/REST (FastAPI), supporting both UDP and Bluetooth Low Energy (BLE) connections. Runs on Raspberry Pi, serves a Vue.js web app through lighttpd.

Companion frontend: `/Users/martinwerner/WebDev/webapp` (separate git repo — commit each repo independently).

## Architecture

Entry point: `src/mcapp/main.py` → `MessageRouter` (central pub/sub hub connecting UDP, BLE, SSE, and command handlers). All source in `src/mcapp/`. The `commands/` package uses mixin-based architecture assembled in `handler.py`.

See `doc/dataflow.md` for data flow diagrams and `doc/2026-02-11_1400-position-signal-architecture-ADR.md` for position/signal table design.

## Package Management

- **Python**: `uv` only — NEVER use `pip` or `venv`
- **Frontend** (webapp repo): `npm`

## Development Commands

```bash
export MCAPP_ENV=dev       # Enables verbose logging
uv run mcapp               # Run locally
uvx ruff check             # Lint (must pass before committing)
uvx ruff check --fix       # Auto-fix
./scripts/release.sh       # Create release (interactive, run from development branch)
```

## Code Quality

- `uvx ruff check` is mandatory — zero tolerance for errors and warnings
- **Ruff config** in `pyproject.toml`: `line-length = 100`, `target-version = "py311"`, rules: `["E", "F", "I", "W"]`
- **Git branches**: `development` (default), `main` (production)
- **Commit format**: `[type] description` — types: feat, fix, perf, refactor, chore, docs, test

## Testing

No pytest — tests are built into the app and run at startup when `has_console()` is true:
- `message_router.test_suppression_logic()`
- `command_handler.run_all_tests()` (in `src/mcapp/commands/tests.py`)

## Configuration

Config file: `/etc/mcapp/config.json` (dev: `/etc/mcapp/config.dev.json`, auto-selected via `MCAPP_ENV=dev`).
BLE mode: `remote` or `disabled` (`MCAPP_BLE_MODE` env override). See `ble_service/README.md` for BLE service API.

## Key Gotchas

- **All DB timestamps are in milliseconds** (not seconds). Divide by 1000 for `datetime.fromtimestamp()`. Forgetting this causes `ValueError: year 58089 is out of range`.
- **SSH + python3 -c quoting**: Use single quotes for Python code, `\"` for strings inside. Never use f-strings with dict key access — use `%` formatting. Or write a temp script with `cat > /tmp/q.py << 'PYEOF'`.
- **MHeard beacons** (RSSI/SNR, no coordinates) and **position beacons** (lat/lon, no signal) are disjoint packet types. `station_positions` merges them per callsign with independent field-group updates.

## Deployment

Two Raspberry Pi Zero 2W targets: `mcapp.local` (production) and `rpizero.local` (integration).

**On-device layout (both Pis, same structure):**
- Slot system: `~/mcapp-slots/slot-0`, `slot-1`, `slot-2`; `~/mcapp-slots/current` symlink points to active slot
- Service: `systemctl status mcapp` — `ExecStart=/home/martin/.local/bin/uv run mcapp`
- Source: `~/mcapp-slots/current/src/mcapp/`
- Config: `/etc/mcapp/config.json`
- DB: `/var/lib/mcapp/messages.db` (SQLite, WAL mode, schema v10)
- Logs: `sudo journalctl -u mcapp.service -f`

See `bootstrap/README.md` for installation, `doc/tls-architecture.md` for TLS setup, `doc/tls-maintenance-SOP.md` for maintenance.
