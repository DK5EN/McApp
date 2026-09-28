# Backlog items closed 2026-09-28

Outcome of the 2026-09-28 backlog review (`doc/2026-09-28_1035-backlog-review-verdict-and-plan.md`).
Each entry below was re-checked against the current code and, where it applies, against
mcapp.local before it was closed.

## B2 — delivery status flags

The original item (written 2026-09-03, RCA `doc/2026-09-03_2300-delivery-status-rca.md`) was
mostly resolved by later work; the remaining `msg_www` split shipped as webapp `6972088`
(2026-09-28): `msg_echo` carries the local echo, `msg_www` means the oevsv.at firehose only, the
firehose branch now marks BLE-sourced own messages too (it was `'node'`-only), and the ✓ title
names the strongest signal.

- **Node vs Gateway ACK and the arrival instant** — stored per station in the `message_acks` ledger
  (schema v29, `_record_message_ack` in `storage/ingest.py`), with `kind`, `from_call`, `via` and
  `timestamp`; served by `GET /api/messages/{msg_id}/acks` and shown in the webapp's ack popover.
- **A later Gateway ACK upgrading an earlier Node ACK** — moot: the two are separate ledger rows,
  not one overwritten flag.
- **Migration** — v29 (`message_acks`), v31 (`delivery_status`, `holder`); `LATEST_SCHEMA_VERSION`
  is 32.
- **✓✓ from a node's binary ack** — fixed in webapp `a7a6698` (2026-08-19): ✓✓ requires `msg_ack`,
  i.e. a real peer ack.
- **mc-chat parity** — the `msg_status` wire shape including `ack_kind`, `from` and `via` is
  carried by `meshcom_mock/wire.py`.
- **Four distinct bubble glyphs** — not pursued. The per-station detail already lives in the ack
  popover; a design pass for glyphs alone is not worth it.

## B3 — release notes on the Update page

A duplicate of webapp backlog B5, which owns all of the work. MCProxy's part already exists:
`scripts/release.sh` publishes `doc/release-history.md`'s section verbatim as the GitHub release
body. Keep that file real Markdown — the webapp renders it.

## B4 — memory footprint (shipped and closed items)

Live on mcapp.local 2026-09-28: `vcgencmd get_mem arm` 496M, `CmaTotal` 64 MB, `/proc/cmdline`
carries `cgroup_enable=memory cgroup_memory=1` after the stock `cgroup_disable=memory` (the later
one wins; `memory.current` reads real values).

- **1. Boot config** — shipped 2026-09-15 (`configure_boot_memory`, `bootstrap/lib/system.sh`),
  including the `cgroup_enable=memory` follow-up (`_mcapp_configure_cmdline_txt`).
- **2b. Fold the BLE service in-process or rewrite it without FastAPI** — closed, not done. The
  separate unit exists for BlueZ/D-Bus adapter ownership and crash isolation; the payoff is one
  interpreter plus a FastAPI stack, and the real pressure (the CMA reservation) is already gone.
- **2a. Unused uvicorn extras** — dropped in `a81ea8b` (2026-09-28): `uvicorn` + explicit
  `uvloop`/`httptools`, `ws="none"`; `websockets`, `watchfiles`, `pyyaml` and `python-dotenv` left
  both envs. `health.sh`'s venv probe had to follow. Pinned by `server_imports` and `health_probe`.
- **3. pywebpush** — replaced in `3d960d9` (2026-09-28) by `push_send.py`: `http_ece` +
  `py_vapid` unchanged, POST via httpx; `aiohttp`, `requests` and their deps left the env
  (+13.4 MB import RSS on the dev Mac). Live push delivery to iOS and Chrome is checked on the
  next deploy.
- **4. `MALLOC_ARENA_MAX=2`** — shipped 2026-09-15 in both unit templates;
  `MALLOC_TRIM_THRESHOLD_` deliberately not added.
- **5. Logs in RAM** — journald `RuntimeMaxUse=8M` shipped 2026-09-15. The `/run/journalxship`
  tmpfs cap is not this repo's: no repo under `~/WebDev` references it outside docs; it belongs to
  the AIOps units on the box.
- **6. `unattended-upgrades.service`** — disabled 2026-09-15, timers kept.
- **7. Exec the venv directly** — shipped 2026-09-15 in both unit templates.
- **8. Caddy memory limit** — shipped 2026-09-15 as `GOMEMLIMIT=48MiB` + `GOGC=50`. The original
  item named `bootstrap/templates/caddy/caddy.service`; that template is never installed on
  mcapp.local, which runs the distro unit. The live mechanism is the drop-in
  `/etc/systemd/system/caddy.service.d/memory.conf` written by `configure_caddy_sudo`
  (`bootstrap/lib/packages.sh`).
