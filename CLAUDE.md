# CLAUDE.md

## Project Overview

McApp is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via SSE/REST (FastAPI), supporting both UDP and Bluetooth Low Energy (BLE) connections. Runs on Raspberry Pi; Caddy terminates TLS on :80/:443 and lighttpd (backend on :8082) serves the Vue.js SPA and reverse-proxies the API.

Companion frontend: `/Users/martinwerner/WebDev/webapp` (separate git repo — commit each repo independently).

## Architecture

Entry point: `src/mcapp/main.py` → `MessageRouter` (central pub/sub hub connecting UDP, BLE, SSE, and command handlers). All source in `src/mcapp/`. The `commands/` package uses mixin-based architecture assembled in `handler.py`.

Detail lives in `doc/`, not here — start with `architecture-reference.md`, `dataflow.md` (flow diagrams), `database-reference.md` (schema/queries), and `operations-reference.md` (deploy, config, health, troubleshooting).

## Development Commands

**Python: `uv` only — NEVER `pip` or `venv`.** Frontend (webapp repo): `npm`.

```bash
export MCAPP_ENV=dev                            # verbose logging + /etc/mcapp/config.dev.json
uv run mcapp                                    # run locally
uvx ruff check [--fix]                          # lint
uvx ruff format [--check .]                     # format
uv run mypy src/mcapp ble_service/src           # types
uv run python scripts/run_startup_tests.py      # tests
./scripts/release.sh                            # release (interactive, from development branch)
```

All four are enforced by CI (`.github/workflows/tests.yml`, Python 3.11) and must be clean before committing.

## Code Quality

- `uvx ruff check` and `uvx ruff format --check .` are mandatory — zero tolerance for errors and warnings
- **`ruff format` also formats ` ```python ` blocks inside `.md` files.** A docs-only commit can and
  did turn CI red (twice, 2026-08-14) because a fenced example in `doc/` had non-ruff spacing.
  Run `uvx ruff format --check .` — not just on `*.py` — before committing **any** file, docs
  included. Note prettier and ruff both touch markdown, so run prettier first, then ruff-format check.
- **Ruff config** lives in `pyproject.toml` — see `[tool.ruff]` / `[tool.ruff.lint]` for line length, target version, the full rule set and documented ignores
- **Keep all `[tool.ruff*]` sections identical** across `pyproject.toml`, `ble_service/pyproject.toml` and mc-chat's `pyproject.toml` — the classifier subtree must lint clean under the same rules in both repos
- New `# noqa` markers need a trailing reason comment and should stay rare — prefer a real fix
- **Git branches**: `development` (default), `main` (production)
- **Commit format**: `[type] description` — types: feat, fix, perf, refactor, chore, docs, test

## Testing

No pytest. **The canonical, authoritative test runner is `scripts/run_startup_tests.py`** — it runs every suite with isolated/ephemeral state, is exit-code gated (0 = all passed), and is fully offline (the command suite stubs the weather fetch). It needs no TTY and no `/etc/mcapp`. CI and releases must trust this runner, not the in-app run.

Suites are registered in `main()` of that script. **Add new suites there** — a suite not wired into that `main()` is not gated by anything.

The in-app startup path (when `has_console()` is true) runs only a **non-fatal smoke check**: the suppression suite (read-only, pure `router.validator` logic). It proceeds on failure because the service is a resilient always-on proxy. The command suite is deliberately **not** run in-app — `run_all_tests()` mutates the live handler (blocked_callsigns, group responses, active pings, beacons) while UDP/BLE are already listening, so it belongs only in the isolated headless runner.

## Type Checking (mypy --strict)

The whole workspace is `mypy --strict` clean — **both source roots must stay at zero errors** (no WIP baseline; regressions are failures, not warnings). `uv run mypy src/mcapp ble_service/src` must print "Success: no issues found".

- **Run it through the project env (`uv run mypy`), NEVER `uvx mypy`/`pipx run mypy`.** mypy parses with the _running interpreter's_ grammar; an ephemeral runner can pull a different Python and emit bogus `[syntax]` errors on version-gated stubs (e.g. numpy's `type` statements). In a workspace the env must contain every member's deps — run `uv sync --all-packages` first.
- Config in root `pyproject.toml` `[tool.mypy]`. Untyped third-party libs (`pywebpush`, `py_vapid`, `timezonefinder`) are silenced via `ignore_missing_imports`; numpy (transitive) uses `follow_imports = "skip"` because its py.typed stubs need 3.12+ grammar. **Prefer an `ignore_missing_imports` override over installing `*-stubs` for libs you don't control.**
- Test files are strict-clean too and stay that way.
- `# type: ignore` is a documented last resort: always `# type: ignore[code]  # reason` (ruff `PGH` enforces this).

## Vendored Subtrees (do not edit in place)

Two directories are `git subtree`s from mc-chat. **Edits belong in mc-chat and are synced here** — editing them locally guarantees drift. The `mc-chat` remote is a local path remote already configured in this repo.

| Path                    | Upstream prefix           | Split branch       |
| ----------------------- | ------------------------- | ------------------ |
| `src/mcapp/classifier/` | `meshcom_mock/classifier` | `classifier`       |
| `src/mcapp/contract/`   | `contract`                | `contract-subtree` |

```bash
cd /Users/martinwerner/WebDev/mc-chat
git subtree split --prefix=<upstream prefix> -b <split branch>

cd /Users/martinwerner/WebDev/MCProxy
git subtree pull --prefix=<path> mc-chat <split branch> --squash
```

Both `command_contract.json` and `push_contract.json` are inside mc-chat's `contract/` prefix,
so the pull above carries both. `push_contract.json` was **outside** it (at mc-chat's
`tests/fixtures/`) until 2026-07-26, which meant every `contract` subtree pull silently
**deleted** MCProxy's copy and broke `push_tests.py` (`_CONTRACT_PATH`) along with the whole
gated `run_startup_tests.py`. **All three** contract suites now pin a sha256 of their local copy —
`push_tests.py`, `dedup_contract_tests.py` and, since 2026-08-20, `contract_parity_tests.py`
(`command_contract.json` was the last one unpinned, which mattered because that suite runs
production against whatever the corpus contains: a local edit would have made production pass
against the edited corpus and silently stop testing parity with mc-chat at all). mc-chat is
upstream, so a contract edit starts there and reaches this repo by split + pull — and the pull and
the re-captured hash belong in the same commit.

**Classifier** — every inbound message is annotated inline in `store_message()` with a primary `category`, free-form `tags` (JSON array), `info_score ∈ [0, 1]`, and a 12-char `template_hash`. Messages are never dropped; the webapp decides what to hide. Three layers: data-driven regex rules (`rules.py`/`seed.py`, `classifier_rules` table, first match by `(priority, id)` wins), template fingerprinting (`template.py`, `beacon_templates`), and scoring (`score.py`), combined by `Classifier.classify()` in `classify.py` — which never blocks ingestion. Rule mutations must bump `classifier_ver` via `storage.bump_classifier_version()` + `classifier.load()`; startup auto-backfills once per version via a `backfill_done:v{N}` marker in `classifier_meta`. Design detail: `doc/spam-filter-BE.md`.

**Command contract** (`contract/command_contract.json`) — the shared parity corpus (target extraction, suppression decisions, `format_for_lora`) that both implementations must satisfy. `contract_parity_tests.py` runs production against it; mc-chat's `tests/test_contract_parity.py` runs the mock against the same corpus. When you change command routing, suppression, or weather formatting, update the corpus in mc-chat and re-sync — otherwise one side fails its parity test.

## Schema Migrations

Add columns/tables via a `current_version < N` block in the chain in `storage/migrations.py` (driven from `sqlite_storage.initialize()`) and bump `LATEST_SCHEMA_VERSION` in `storage/constants.py` in the same commit — that constant is the single production source for the migration terminus and both `migration_chain_tests.py` and `connection_lifecycle_tests.py` assert against it (the latter via a `FINAL_SCHEMA_VERSION` alias re-exported from `migration_chain_tests.py`, kept only for import-site stability). The step numbers in `migrations.py` are independent literals, not derived from the constant, so forgetting either half fails the migration-chain suite loudly rather than silently drifting.

## System Epoch

System-level machine state (packages, firewall, web front door) is versioned by `SYSTEM_EPOCH` in `bootstrap/mcapp.sh` and mirrored by `REQUIRED_SYSTEM_EPOCH` in `src/mcapp/system_converge.py` — bump both together, a startup test enforces parity. Installed state is marked at `/var/lib/mcapp/system-epoch`; `mcapp.sh --converge` runs `setup_system` + `install_packages` idempotently to bring a box up to date. The update runner converges the newly deployed slot after every successful update, and the app's converge watchdog self-heals boxes whose update was driven by a pre-epoch runner.

## Memory Footprint (B4)

Reduced the usable-RAM squeeze on mcapp.local's Pi Zero 2W (512 MB physical, 415 MB seen by
Linux, only ~144 MB usable for kernel non-movable allocations before this work) — shipped
2026-09-15 alongside stall tracking in the same campaign. Plan and the PSS measurements:
`doc/backlog.md` B4, `doc/2026-09-15_1530-stall-tracking-plan.md` §8.

- **The 512/415/144 MB budget was never a RAM shortage — it was a CMA reservation.** The stock
  boot config reserved 64 MB for the GPU plus a 256 MB CMA pool on a headless box with no HDMI
  or audio load, which is what actually pushed processes into zram swap under normal operation.
  `gpu_mem=16` and shrinking the KMS overlay's CMA to `cma-64` (`dtoverlay=vc4-kms-v3d,cma-64`,
  `dtparam=audio=off`) are the largest single win, in `bootstrap/lib/system.sh`'s
  `configure_boot_memory` / `_mcapp_configure_config_txt`.
- **Boot-config changes need a reboot and are marked, never assumed applied.** A change writes
  `/var/lib/mcapp/reboot-required`; the converge path must check that marker rather than assume
  the new `gpu_mem`/`cma-64`/`cgroup_enable=memory` values are already live.
- **`gpu_mem` must land in `[all]` or before any section header — a Pi Zero 2 W trap.**
  `_mcapp_configure_config_txt` appends a `[all]` block with a begin/end marker comment pair so a
  re-run can find and update it idempotently instead of duplicating it.
- **`cmdline.txt` must stay exactly ONE line.** `_mcapp_configure_cmdline_txt` appends
  `cgroup_enable=memory cgroup_memory=1` to the existing single line rather than writing a new
  one — a second line makes the bootloader ignore the file.
- **`MALLOC_ARENA_MAX=2` plus a direct venv `ExecStart`** in both `bootstrap/templates/*.service`
  — `uv run` is no longer used at exec time (still used by `deploy.sh` to build the venv), which
  removes an idle wrapper process and a resolver step per restart. `WorkingDirectory` is
  load-bearing for the BLE service specifically: uvicorn's CLI inserts the CWD into `sys.path` via
  its `--app-dir` default, which is how `ble_service.src.main:app` resolves at all.
- **journald `RuntimeMaxUse=8M`** (`configure_journald`, was 20M) and **Caddy
  `GOMEMLIMIT=48MiB` + `GOGC=50` via the drop-in `/etc/systemd/system/caddy.service.d/memory.conf`**
  written by `configure_caddy_sudo` in `bootstrap/lib/packages.sh`. mcapp.local runs the DISTRO
  caddy unit, so `bootstrap/templates/caddy/caddy.service` is never installed there — an edit to
  that template reaches no running box (found 2026-09-15 when the first attempt did exactly that).
- **`unattended-upgrades.service` disabled, its timers kept.** `configure_unattended_upgrades`
  disables only the `unattended-upgrade-shutdown --wait-for-signal` shutdown hook (7 MB RSS);
  `apt-daily-upgrade.timer` keeps running the actual upgrades on schedule. Do not re-enable the
  service as a "fix" for missed updates — the timer is what does the work, this unit only trims a
  shutdown-time nicety.
- **`SYSTEM_EPOCH` bumped to 3, then 4 for the Caddy drop-in** (`bootstrap/mcapp.sh` / `REQUIRED_SYSTEM_EPOCH` in
  `system_converge.py`) alongside these changes, per the existing System Epoch convergence
  contract above.

## Stall Tracking (`stall_events`, `/api/stalls`)

Every stall between the webapp and the API — server- and client-observed — recorded with the
parameters needed to reproduce it, keyed by one correlation id, retrievable by a coding agent
through `/api/stalls`. No UI; this is capture-and-upload only. Design and the interface contract:
`doc/2026-09-15_1530-stall-tracking-plan.md`.

- **A stall is any call that crosses a duration threshold, recorded by `kind` into
  `stall_events`.** `http` (server ASGI middleware), `client_http` (webapp fetch wrapper,
  includes the Caddy/lighttpd hop), `sse_answer` (`page_request` → SSE `response`, missing after
  10 s → `sse_answer_missing`) and `handler` (one `MessageRouter.publish` subscriber) are
  **0.5 s = stall, 2 s = critical**, plus a **1-in-50 sample of every sub-threshold call**
  (`severity="sample"`) so there is a healthy baseline to diff against. `loop_lag` (event-loop
  drift watchdog) and `pool_wait` (queue wait before a `to_thread` job ran) are far tighter —
  **100 ms stall / 500 ms critical** — because they measure infrastructure health, not a
  user-facing SLO: a 100 ms event-loop hesitation is already abnormal. `client_timeout` (10 s
  fetch abort), `client_error` (network error / HTTP ≥ 400) and `sse_heartbeat` (90 s reconnect
  watchdog) are recorded unconditionally — there is no sub-threshold case for "it failed." All
  thresholds live under the `stalls` key in `config.json` (`StallsConfig`,
  `src/mcapp/config_loader.py`); an absent key means the defaults above.
- **Correlation is `X-Request-Id`, minted by the client and echoed by the server** on the
  response and stored on both ends' rows; `X-Session-Id` (per browser tab) rides alongside it.
  `mcapp.stalls.current_request_id` is a `ContextVar[str | None]` set by the middleware for the
  request's lifetime, so a `handler` stall triggered by that request can still report the id. The
  `page_request` path is the one exception — it already carried its own `request_id`
  (webapp `messages.ts`, echoed on `proxy:messages_page`) before this work existed, and the
  client's `sse_answer` timer keys on that pre-existing id instead of minting a new one.
- **The middleware is PURE ASGI, never `BaseHTTPMiddleware`.** `BaseHTTPMiddleware` buffers the
  whole response before the downstream body is visible, which would turn `/events` and
  `/update-stream` — long-lived SSE streams — into "one very slow request" or break them outright.
  `StallMiddleware` (`src/mcapp/stall_middleware.py`) taps `receive`/`send` chunk by chunk instead,
  and passes `/events*`, `/update-stream` and `/health` through completely untouched: no id, no
  header, no timing.
- **Added after `CORSMiddleware` so it is the OUTERMOST layer** (`sse_handler.py`,
  `_create_app`) — Starlette applies middleware innermost-first in registration order, so timing
  from outside CORS covers the whole stack, not just what CORS lets through.
- **The recorder has its OWN writer thread and never touches the shared executor.** `record()`
  and `ingest_client()` are non-blocking: they push onto a bounded `queue.Queue(maxsize=1000)` and
  return, drained by one dedicated `mcapp-stalls-writer` thread with its own SQLite connection. A
  starved thread pool must not make the stall logger itself stall, or stall reporting would fail
  exactly when it is most needed; a full queue drops the row and counts the drop rather than
  blocking.
- **The counting executor is installed BEFORE any `to_thread` call.** `install_executor()` swaps
  in a `ThreadPoolExecutor` subclass as the loop's default executor, in `build_app()` before
  `create_sqlite_storage` runs — installed any later and the first `to_thread` calls would already
  be running on the un-instrumented default, invisibly to `pool_wait`.
- **The recorder's own DDL duplicates migration 32 on purpose.** `StallRecorder.start()` issues
  the identical `CREATE TABLE/INDEX IF NOT EXISTS stall_events` statements as a defensive
  fallback; migration 32 (`storage/migrations.py`) is the canonical, schema-versioned source.
  Keep the two texts byte-identical if either changes.
- **Redaction replaces VALUES, not keys**, for push-subscription fields (`endpoint`, `keys`,
  `p256dh`, `auth`) and anything named `*api_key*`/`authorization` (`redact()` in
  `src/mcapp/stalls.py`) — applied to `body`/`detail`/`context` on both server records and
  client-submitted ones (redacted again server-side in `ingest_client`, never trusted from the
  client). **Message text is deliberately kept** — it is ham radio traffic, not a secret, and is
  often exactly what a coding agent needs to reproduce a slow handler.
- **`/api/stalls` (full records, coding-agent interface) and `/api/stalls/summary`
  (per-path p50/p95/p99/max baseline) are read surfaces; `POST /api/stalls/client` is how the
  webapp uploads its own observations** (`src/mcapp/sse_routes/stalls.py`, one or an array of
  records, capped at 100/call). `scripts/replay_stall.py --id <row>` re-issues the recorded
  `method path?query`+`body` against a running instance and prints new p50/p95/max next to the
  recorded duration and server context — the reproduce tool, not a dashboard.
- **Row cap is `max_rows` (default 5000)**, enforced by the writer thread pruning the oldest rows
  every 200 inserts — not a nightly job, and not schema-enforced.
- **`db_write` runs at `synchronous=NORMAL`, and the first stall data proved why.** The
  0.5-1.25 s `handler` rows of v2.0.8-dev.3 were NOT many DB calls (a real message makes 2-6)
  but the WAL fsync every commit does at the default `FULL` on the SD card: 15 ms typical,
  1.7 s outliers, measured 2026-09-16 against a copy of the live DB. NORMAL keeps WAL
  crash-safe (fsync at checkpoints) and trades the last transactions on power loss. Measure on
  the Pi's ext4 root, never in `/tmp` — it is tmpfs there and hides the whole effect.
- **`loop_lag` rows carry the blocker's stack** (`detail.stack`, `samples`, `sampled_at_ms`).
  The lag loop runs ON the loop and cannot see what blocked it, so a daemon thread
  (`mcapp-stalls-lagsampler`) compares a heartbeat every 50 ms and reads the loop thread's frame
  through `sys._current_frames()` only once overdue. An unsampled lag omits the key entirely.
- **`sse_heartbeat` from a hidden tab is a `sample`, not `critical`** (webapp
  `recordSseHeartbeatTimeout`): iOS suspends the PWA and wakes it hourly while still hidden.
- **`/api/weather` at 0.5-1.5 s is the upstream fetch and is CLOSED as a non-issue** (decision
  2026-09-18, `doc/2026-09-18_2200-stall-followup-plan.md`). It runs off-loop via `to_thread`,
  stalls nothing else, and 8 of 12 calls crossing the 0.5 s threshold is the weather provider's
  latency from the Pi, not ours. Do not re-open it from a stall report; a webapp-side cache is the
  only lever, and it is a frontend change.
- **Per-call write connections were the second half of the handler stalls.** Closing the LAST
  connection to a WAL database checkpoints and deletes the WAL, a DB-file fsync per write:
  23.7 ms vs 0.2 ms on a persistent connection, measured 2026-09-18 on the Pi's ext4 root. The
  query plans were never the problem (every ingest-path query is `SEARCH ... USING INDEX`, < 1 ms
  on the live DB), so a "full table scan" hypothesis for `_storage_handler` stalls has already
  been checked and rejected; look at fsync counts first.
- **`ble_connected` handles property-vs-method.** `is_connected` is a property on the remote BLE
  client (and on every current client); the gauge (`_ble_connected` in `main.py`) tolerates a method too and calls it
  only when callable, mirroring the same pattern the BLE code already uses elsewhere.

## Bootstrap Network Safety (system epoch 5)

Rules that keep a `mcapp.sh` run from taking the box off the network, born from the 2026-09-18
outage (`doc/2026-09-18_2320-wifi-outage-postmortem.md`, raspberrypi/linux#7634): the bootstrap's
in-session `apt-get upgrade` replaced wpasupplicant with Raspberry Pi's `2:2.10-24+rpt1`, whose
one patch makes the supplicant advertise WPA3-SAE, NetworkManager 1.52 then forces SAE for every
`wpa-psk` profile, and the Zero 2 W's brcmfmac43436 never completes it against a WPA2/WPA3
transition-mode AP. Design: `doc/2026-09-18_2330-bootstrap-network-safety-plan.md`.

- **wpasupplicant and network-manager are never upgraded inline.** `hold_network_packages` puts
  them on `apt-mark hold` for the apt phase and `report_deferred_network_upgrades` prints what was
  kept back with the console command. An operator's pre-existing hold is never released. Do not
  "simplify" this into skipping the upgrade: the rest of the system still upgrades.
- **wpasupplicant is pinned to Debian `2:2.10-24` on trixie** by `configure_wpasupplicant_pin`
  (`/etc/apt/preferences.d/mcapp-wpasupplicant`, priority 1001). trixie's NetworkManager has NO
  configuration-level opt-out from SAE for `wpa-psk` in station mode (the upstream fix b00c6749 is
  in 1.56+), and Raspberry Pi's own `rpi-brcmfmac.conf` mask (`feature_disable=0x282000`) clears
  SAE but not `SAE_EXT` (bit 25 in the 6.18 driver), which is the bit that sets
  `NL80211_FEATURE_SAE`. Removal criteria are in the plan; until one holds, the pin stays.
- **apt runs under `systemd-run --wait --collect`** (`run_apt_detached`) so a dropped SSH session
  cannot interrupt dpkg mid-transaction. Exit status is captured with `|| rc=$?`; reading `$?`
  after an `if` returns 0 and silently swallowed failures in the first version.
- **Every run is mirrored to `/var/lib/mcapp/bootstrap.log`** via `tee -p` (`start_bootstrap_log`).
  `-p` is load-bearing: a plain `tee` dies of SIGPIPE when the ssh side goes away and takes the
  bootstrap down with it. `/tmp` and `/var/log` are tmpfs, so this is the only run output that
  survives a reboot.
- **The default route must be back after the apt phase** (`verify_link_state`, 45 s) or the run
  stops with the last NetworkManager/wpa_supplicant lines. A changed `key_mgmt` is warned about:
  that one line would have named the 2026-09-18 cause.
- **The journal is persistent, 16 MB, on `/var/lib/mcapp/journal` bind-mounted to
  `/var/log/journal`** while `/var/log` stays tmpfs (`configure_journald`, fstab line inside the
  McApp tmpfs block). Before this the box had no on-disk evidence at all; the only record of the
  outage was rpizero's shipped copy, which ended at the second the link dropped.
- **Symptom to cause:** ssh drops during a bootstrap run and the box never comes back on WiFi
  means a network-critical package was replaced. Read rpizero's peer journal first
  (`sudo -n journalctl -D /run/journalxship`), then `/var/lib/mcapp/bootstrap.log` on the box.

## Link Check (`{ping}` / `{pong}`)

Probes whether a station answers on **direct RF**, using the firmware's `v4.35p.07.24.2` ping
feature. Design and the on-air measurements: `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md`.

- **Correlation has two representations and both are load-bearing.** The Extern-UDP `msg_id` field
  is an 8-digit **hex string** (`"1AE1E057"`); the pong payload embeds the same 32-bit value in
  **decimal**, and roughly half the fleet emits it **negative** (`{pong}{-427408969}` — real
  traffic, `SendPong()` formats an `unsigned int` with the signed `%i`). Normalise both with
  `& 0xFFFFFFFF` — `linkcheck.normalise_id()`. A `\d+` pattern silently never matches half the
  stations.
- **Prefix-match the ping, never equality.** `sendMessage()` appends an unterminated ACK suffix, so
  ours reads `{ping}{087` on the wire.
- **The routing hook must come BEFORE the echo/ACK branches** in `commands/routing.py`. ctcping's
  `_ECHO_SUFFIX_RE` (`\{\d{3}$`) also matches `{ping}{087`; wired after it, ctcping swallows every
  echo, the session never learns its `msg_id`, and every attempt times out with no visible cause.
- **The ingest guard sits before `_insert_message_row`, NOT in `_should_filter_message`.** The
  latter returns before `_ingest_signal`, so a guard there deletes the pong's signal ingestion,
  which already works. `linkcheck_ingest_tests.py` case 1 pins exactly that pair.
- **It does not measure a round-trip time.** Measured 21-43 s on air, dominated by the node's TX
  queue and the firmware's 40 s retransmit steps. Report reachability + reply RSSI/SNR; never label
  a number RTT. Attempt timeout is 90 s for that reason, and attempts are sequential.
- **RSSI/SNR belongs to the target only when the pong arrives with no via-path** (`hops == 0`);
  relayed pongs are the observed norm and carry the last hop's signal.
- **Nothing we send survives the round trip** — `getExtern()` reads only `dst` and `msg`, so our
  own echo cannot be tagged and the echo-claim can only be narrowed, never closed.
- A proxy-originated ping is **~4 keyings over 2 minutes** (retransmission is armed for any DM not
  starting `{CET}`/`{MCP}`/`{SET}`). Caps are enforced server-side; the endpoint has no auth.
- **We cannot ping ourselves** — the firmware refuses a DM to its own callsign.
- **The ping goes over BLE when BLE is connected, else UDP** (ADR §8). With EXTUDP off the node
  never reads UDP, so a UDP-sent ping is silently never transmitted. The node echoes a BLE-sent
  ping back over BLE (`src` == our callsign) with its real msg_id. Live BLE frames carry
  `src_type:"ble_remote"`, not `"ble"` — test fixtures must use it.
- **A pong counts from either transport; the echo is optional** (ADR §8, 2026-09-24). BLE copy
  without `msg_server` = RF, with it = internet path. Without the Extern-UDP echo the pong is
  matched through our node's id — the top 22 bits of every firmware msg_id, learned from the BLE
  `I` register's `ID`. **The firmware does not forward a `{pong}` for us to BLE** (display only),
  so a box whose EXT IP points elsewhere (MeshCom WebDesk) still sees no pong until it does.
  Symptom: link check always times out while the node's console shows `[PONG]` → check EXT IP.

## Gateway Uptime (`{CET}` link)

Availability of the `{CET}` time-beacon link (node uplink → MeshCom server), charted by the
webapp's Gateway Availability card in Settings. Design and the on-air measurements:
`doc/2026-08-21_2350-gateway-uptime-plan.md`.

- **The beacon is never persisted, so the hook must sit BEFORE `_should_filter_message`.** That
  guard drops `{CET}` before any INSERT and returns early (`storage/ingest.py`), so a recorder
  placed after it never fires — the identical trap the link-check ingest guard documents above.
  The hook only observes; it must never change what gets filtered.
- **The gate is hop-count 0, NOT `not via`.** The same beacon arrives in up to three copies:
  `udp` (no `via`), `ble_remote` (`via == src`, because `split_path` strips our own callsign and
  leaves the originator behind), and a **foreign** gateway's multi-hop `lora` relay that must not
  count. The webapp watchdog's `!element.via` rule rejects the BLE copy, which would break a
  BLE-only box — do not copy it into the backend. Contract: `is_uplink_time_beacon` in
  `storage/uptime.py`, pinned by `storage/uptime_tests.py` against all three real captured frames.
- **`GAP_TOLERANCE_MS` must stay above the beacon cadence, and the cadence is NOT a constant.** It
  is set upstream by the MeshCom server, not by our node, and OE1KBC has already halved it once:
  **303 s** until 2026-08-22 (`23:40:31 → 23:45:34 → 23:50:37`), **606.5 s** since (measured
  2026-08-28 over 12 consecutive intervals, all 10.11 min). A tolerance at or below the cadence
  records a gap on every healthy cycle — at 6 min against the new 606.5 s the card read **0.0%
  uptime for six days while beacons were arriving normally**, and the footer showed "No time sync"
  for ~45% of every cycle. It is **12 min** for that reason (same 1.19x margin 6 min had over
  303 s). **Symptom → cause:** uptime near zero while beacons are visibly arriving means this value
  is under the cadence — re-measure before suspecting the link.
- **Retune all four thresholds together.** `GAP_TOLERANCE_MS` and `SILENT_MS` (12 min) and `OFF_MS`
  (30 min, ~3 cadences) in `storage/constants.py`, plus the webapp's `WATCHDOG_TIMEOUT_MS`
  (`src/constants/index.ts`, 12 min). `GAP_TOLERANCE_MS` is the **only** one baked into stored
  history — the amber/red split is applied at read time and stays retunable — so raising it does
  NOT repair rows already written. Migration 28 scrubbed the 210 spurious gaps recorded between
  2026-08-27 07:45:59 (where they became contiguous) and the retune, and deliberately KEPT the 37
  earlier ones: the cadence still alternated there, so those are genuinely ambiguous.
- **`gap` and `dark` are different claims and must never be conflated.** `gap` = proxy running, no
  beacon → counts against UPTIME. `dark` = proxy not running, nothing observed → counts against
  COVERAGE only. Startup reconciliation writes the `dark` row and resets `last_beacon_ms`, so a
  deploy restart can never look like a link outage — and it must run before the heartbeat task
  starts, or the first tick papers over the very downtime it exists to record.
- **The metric's resolution is one cadence.** A 20-minute outage between beacons reads as 20 min +
  606 s, and nothing shorter than the tolerance (~12 min) is visible at all. Inherent to a
  heartbeat-driven metric, and it got twice as coarse when the cadence halved.

## MHeard Register (`SRC` / `GW` / `PP`)

The BLE `TYP: "MH"` register. **`SRC`, `GW` and `PP` were added upstream on 2026-08-27 and
REVERTED on 2026-08-28** (fork-main `dc7d56d7` for `SRC`/`GW`, `17d1796e` for `PP`; the dead
clamp code went on 2026-09-01). The live builder emits exactly 13 keys: `TYP CALL DATE TIME PLT
HW MOD RSSI SNR DIST PL MESH NCNT`. The parser is deliberately RETAINED and inert: all three are
read with `.get()`, every coercer is `None`-safe, so a firmware that re-adds them is picked up
without a code change. Against current firmware `hey_path.py`, the `"heard"` upsert and the
BLE-path `gw` write never fire, and on a BLE-only box `station_positions.gw` is not set from
MHeard. Do not chase a missing `SRC`/`GW`/`PP` as a bug. The rules below describe the reverted
wire contract and stay authoritative for the parser (audit: RX-07 in
`doc/2026-09-10_1900-ble-protocol-parity-audit.md`; adoption plan and field evidence:
`doc/2026-08-28_0900-firmware-4.35p.08.28-adoption.md`).

- **`CALL` is the LAST HOP, `SRC` is the ORIGINATOR, and they are different claims.** `CALL` is the
  station whose transmission the frame's own `RSSI`/`SNR` measured; roughly two thirds of HEY
  observations are relayed, so `SRC != CALL` is the common case. `transform_mh` therefore keeps
  `src = CALL` and the signal write stays on that row — rekeying it onto `SRC` re-creates exactly the
  bug migration v22 fixed. `SRC` gets a signal-free `"heard"` upsert (`last_seen` + `gw` only).
  `hw_id`/`lora_mod`/`mesh` also describe the heard transmission and must never land on `SRC`'s row.
- **The `"heard"` upsert must run BEFORE `_store_mheard`'s early return** (`storage/ingest.py`).
  `_store_mheard` returns True on a throttle hit and `store_message` returns on that same line, so
  code one branch later is skipped for every throttled frame — most of them under real traffic. Third
  instance of this trap in this repo; see Link Check and Gateway Uptime above.
- **`GW` describes `SRC`, never `CALL` — but only on a HEY frame.** It comes from the beacon's
  destination path (`"HG"` vs `"H"`), which the originator sets and relays never modify. That path
  is only a gateway claim when the payload type is `'@'`; on a text, position or ACK frame the
  destination is something else entirely and the firmware's `GW: 0` is not a claim about anything.
  `transform_mh` therefore gates `gw` on `PLT == 0x40` and emits `None` otherwise (fail closed when
  `PLT` is absent). Within that gate the old rule stands: `0` is authoritative and correctly
  overwrites a stored `1`; absent (`None`) leaves it alone. Ungating this — reading `GW` on every
  frame, as we did until schema v27 — makes a relay's non-HEY traffic overwrite a real gateway flag,
  which is exactly the 2026-08-28 `DF2SI-12` flip. Feeds the pre-existing `station_positions.gw`
  column; migration 27 nulled the zeros stored under the old rule, because a wrong `0` and a real
  one are indistinguishable after the fact.
- **`MOD` is a packed byte, not a number.** `msg_source_mod = (getMOD() & 0xF) | (node_country << 4)`
  (`aprs_functions.cpp:113`): low nibble modulation (3..8), high nibble country index (0..15). It
  arrives on two paths — the binary GATT footer and the MH register's `MOD` — and both are masked
  with `& 0x0F` in `ble_protocol.py`. Storing the raw byte made every non-EU node's "modulation"
  wrong (country 8 → `0x83` → 131). The country nibble is deliberately not persisted: `0xF` is both
  country `PL` and the firmware's "modulation not from the last hop" marker
  (`lora_functions.cpp:587`), so it is ambiguous on the wire — a handover asking for that to be
  separated is with the firmware maintainers
  (`doc/2026-08-28_1700-firmware-mod-nibble-handover.md`).
- **`PP` carries RSSI as a POSITIVE MAGNITUDE.** `appendHeySignalReport()` emits
  `String(rssi*-1.0, 0)`, so `-101 dBm` is on the wire as `101`. `hey_path.parse_hey_chain()` negates
  it. A parser that trusts the sign inverts every reading and still looks plausible.
- **Legacy-shape detection is by COMMA COUNT in the leading token**, mirroring the firmware
  (`mheard_functions.cpp:436-451`): 0 commas (`R99;`) and 2 (`R99,99,99;`) are valid, **1 comma
  (`R99,99;`) is invalid**. Do not invent a different rule.
- **An absent `PP` says nothing about the hop count.** The firmware drops it (then `DIST`) once the
  register JSON would exceed 244 chars, which starts at ~5 relay hops — precisely the deep chains
  where it would be most interesting. Never read "no chain" as "no relays".
- **`PP` is deliberately NOT persisted.** It carries no callsigns, so it identifies the POSITION of a
  weak link, never the station, and it self-censors at depth. Parsed and passed through for the live
  view only. Revisit if hop identities ever reach the wire.
- **Two schemas share `TYP: "MH"`.** The live builder used to send `SRC`/`GW`/`PP`; those keys were
  reverted upstream 2026-08-28 (`dc7d56d7`, `17d1796e`) and the current `MH` register carries 13 keys
  instead: `TYP CALL DATE TIME PLT HW MOD RSSI SNR DIST PL MESH NCNT`. The `--mheard` table dump
  (`mheard_functions.cpp:651`) never sent `SRC`/`GW`/`PP` either, because it reconstructs from a
  stored `|`-separated string that never held them. MCProxy's parser for the three reverted keys
  stays in the tree — inert against current firmware, ready if an older node is still in the field or
  the fields are re-adopted — and all three stay optional — never subscript them.

## ACK Attribution (`message_acks`, "who acknowledged?")

Attribution behind the single-flag `send_success` / `acked`: which station sent the Node,
Gateway or Peer ACK. Plan and the compatibility matrix: `doc/2026-09-05_1545-ack-attribution-plan.md`;
firmware side: `MeshCom-Firmware-DEV-Main/docs/ack-wer-hat-quittiert.md`.

- **Vocabulary is `node` / `gateway` / `peer`** (`ack_kind` on `msg:status`, `ACK_KIND_BY_TYPE`
  in `ble_protocol.py`). The proposal's "heard" / "server reached" are explanations, not
  identifiers. Do not rename on the wire: the webapp's ctcping ordering guard keys on `sent`.
- **The BLE appendix is length-prefixed at GATT byte 7**, not separator-terminated. Old firmware
  sends `0x00` there, which IS the legacy format. A bad appendix drops the appendix, never the ACK
  (`parse_ack_appendix`). The 4-byte timestamp is never read; `transform_ack` stamps arrival.
- **`from` / `via` are on `msg_status` ONLY when known.** Legacy payloads are byte-identical and
  pinned by `ack_status_tests` cases 1-6; adding `from: None` breaks them and mc-chat parity.
- **`message_acks.from_call` is `''`, never NULL, for an unattributed frame** so the
  `(msg_id, kind, from_call)` key collapses repeats once firmware stops gating "first ACK only".
- **`echo_id` identifies NOTHING on its own — the inline `:ackNNN` match needs the ack's addressing
  AND a 1-hour window.** It is the firmware's `{NNN` counter: three digits, minted per sender,
  unique only within that sender and roughly an hour. The lookup was `WHERE echo_id = ? ORDER BY
timestamp DESC LIMIT 1`, so any station's ack marked whichever message last used that number —
  on mcapp.local an own DM to DK1TCP-77 rendered ✓✓ Delivered because an unrelated DH6MAV pair
  reused 201 forty-four minutes later (5 of 82 acked rows mis-attributed; 25 of 200 live counter
  values already shared by >1 sender). Same user-visible failure as the 2026-08-19 ctcping bug.
  `_inline_ack_original` now requires the original's sender to be who the ack is addressed TO and
  its target to be who it came FROM, inside `DEDUP_WINDOW_MS`. **Both halves are load-bearing and
  each is mutation-pinned.** Do NOT widen the window to cover a store-and-forward hold: past the
  counter's horizon a "match" is not evidence, and a held DM's real ack still arrives exactly as a
  binary `0x02` frame carrying the true msg_id — **except for a message already at
  `delivery_status = 'held'`**, which gets `HELD_ACK_WINDOW_MS` (168 h, the firmware's
  `--storetime` max) instead. That exception is not optional: a held DM legitimately sits in a
  mailbox until the destination reappears, so a flat 1 h window refuses every late ack and strands
  it at `held` forever. Leaning on `0x02` alone does not cover it — the extUDP path has no binary
  ack, and neither does mc-chat, so for them the text IS the only signal. Widening it only for
  already-held rows keeps the ambiguity small: a false match needs the same pair, the same
  counter, AND the older message still held — and since the counter is ours, reusing it means
  1000 messages to that station in between.
- **A `msg_id` identifies a message only for 4 hours — `ACK_MSG_ID_WINDOW_MS`.** It is
  `((_GW_ID & 0x3FFFFF) << 10) | node_msgid` with `node_msgid` wrapping at 999
  (`msgid_counter.h`), so it is unique across STATIONS but repeats every ~1000 frames one node
  originates — a frame count, not a period: median **24.8 h** on DK5EN-98 (min 24.75 h, max
  499 h), and it shortens as traffic grows. Every binary-ack binding goes through
  `_resolve_ack_target` (`storage/ingest.py`), which clamps the `msg_id` lookup to 4 h, and
  `_write_delivery_status` now takes a REQUIRED `row_id` so nothing re-resolves independently.
  Unclamped, a group-20 broadcast displayed "Acknowledged by OE5HWN-12" from the peer ack of an
  unrelated DM sent 24.75 h earlier under the same msg_id 1AE1E066 (2026-09-21; 13 of 85 ledger
  ids on the live DB matched more than one message row). Same carve-out as the inline path: a row
  at `delivery_status = 'held'` keeps `HELD_ACK_WINDOW_MS`.
- **The `message_acks` key carries no message identity, so a reused msg_id SWALLOWS the new
  message's acks.** `(msg_id, kind, from_call)` has no timestamp, so the previous owner of the
  counter is still sitting under the key and `INSERT OR IGNORE` drops the new rows as duplicates —
  the 2026-09-21 message lost all three of its node acks that way. `_prune_stale_message_acks`
  runs once per ack frame in `_handle_ack`, before any branch records, and only when the ack bound
  INSIDE the window (a held-carve-out match's older rows are that message's own). `get_message_acks`
  applies the same clamp on READ, anchored on the newest ack for the id, which is what makes rows
  written before the prune existed read correctly without a migration or backfill.
- **Never key the inline match on the ack payload's padded callsign.** `%-9.9s:ack%03i` TRUNCATES
  at 9 chars (`OE1ABCD-12` arrives as `OE1ABCD-1`) and real traffic shows the no-separator case
  (`DK1TCP-77:ack622`). The frame's `src`/`dst` carry the same identities untruncated. The padded
  field holds the ORIGINAL SENDER, not the acking station — a test fixture said otherwise until
  2026-09-14.
- **The extUDP `{"type":"ack"}` datagram has no `msg` key** and must be claimed in
  `_handle_non_chat_frame` before the DEBUG-only non-chat log, which is where it used to vanish.

## Store-and-Forward DM Status (`0x03 failed` / `0x04 held`)

Delivery states a store-and-forward node reports for a DM, on top of the three ACK kinds above.
Plan and the decisions: `doc/2026-09-14_1153-store-forward-dm-status-plan.md`; firmware side:
`MeshCom-Firmware-DEV-Main/docs/client-integration-store-forward.md` (fork-main `150b0a4a`).

- **`failed` must NOT set `send_success`, and that is the whole point of the branch.** `_handle_ack`
  set it unconditionally for every ack type; `0x03` means every retry was exhausted and nobody
  acked, so the pre-existing write would have marked the message transport-confirmed — the exact
  inversion of the frame. The `0x03` path runs a read-only existence `SELECT` instead, so `rows`
  still means "a matching original exists" for the record/publish gates. `0x04 held` KEEPS the
  write: a store node demonstrably took the frame off the air.
- **Precedence is one monotone rank, enforced in the UPDATE's own WHERE clause.**
  `sent/node/gateway = 1 < held = 2 < failed = 3 < acked = 4`, NULL = 0 via the SQL `CASE`'s `ELSE`;
  `_DELIVERY_STATUS_RANK` in `storage/ingest.py` is the single source and the SQL is built from it.
  Encoding the test in SQL rather than read-then-write is what makes two out-of-order acks
  race-proof — the loser matches zero rows instead of clobbering a higher rank from a stale read.
  This reproduces every rule in the spec's §2 and every sequence in its §6 with no special case.
- **Equal rank does not overwrite, so `held(A)` then `held(B)` keeps A.** The spec says "show the
  most recent holder"; a strict `>` is what makes the scheme race-proof, so the deviation is
  deliberate. Both holders survive in `message_acks`, whose `(msg_id, kind, from_call)` key also
  collapses the firmware's hourly per-holder repeat for free. `holder` is written through
  `COALESCE(?, holder)` so an unattributed frame never blanks a known holder.
- **`acked: false` on the `failed` event is a compatibility floor, not decoration.** The webapp's
  `msg:status` handler renders ANY event with no `sent` key and no `acked === false` as a peer
  acknowledgement — ✓✓ Delivered. Without that key a `failed` event renders the one thing it
  exists to deny. Do not remove it once the webapp learns `ack_kind: "failed"`. `held` needs no
  such guard: its `sent: true` takes the transport branch, which is already the honest rendering.
- **`holder` is a deliberate duplicate of `from` on the `held` event.** The spec names it; the
  webapp reads it without knowing this repo's attribution convention.
- **Both "the addressee answered" paths write the `message_acks` ledger, and the inline one does
  NOT extend its published payload.** The inline `:ackNNN` branch recorded no ledger row until
  2026-09-21, so a text peer ack rendered ✓✓ Delivered with an empty "Acknowledged by" — and text
  is the only form of a peer ack on the extUDP path and in mc-chat, neither of which has a binary
  `0x02` frame. It now records `kind="peer"` attributed to the ack frame's own sender (through
  `normalise_ack_callsign`, the same grammar the BLE appendix uses) with `via` = the ack's
  `src_type` through `_coerce_ack_via` (BLE yields None). The `msg_status` event stays
  `{msg_id, acked, ack_kind}` with no `from`/`via`: it is byte-pinned by `ack_status_tests` and
  shared with mc-chat, and the popover reads the ledger, not the event.
- **There are TWO paths that mean "the addressee answered", and both must write the rank.** The
  binary `0x02` branch in `_handle_ack` and the inline `:ackNNN` TEXT match further down
  `store_message` are independent; wiring only the first left a `held` message acked by text
  sitting at `delivery_status='held'` AND `acked=1`, with history contradicting the live event.
  The inline path passes `row_id=` explicitly, because it locates the original by `echo_id` while
  `_write_delivery_status`'s own lookup takes the newest row for the msg_id — and one msg_id
  legitimately has two transport copies, so resolving separately can mark `acked` on one and
  `delivery_status` on the other.
- **Do not fold this into the existing `send_failed` event.** `_publish_send_failed` (`main.py`) is
  a LOCAL send failure, emitted before a msg_id exists, which is why the webapp matches it by
  `dst` + `msg`. `0x03` has a msg_id and is a different fact. Same display fields, distinct events.
- **`:sto` is push-silent but history-VISIBLE, and the asymmetry with `:ack` is intended.** Push
  contract **v10** widens the noise clause to `:ack` / `:rej` / `:sto`; `query.py`'s exclusion
  stays `msg NOT GLOB '*:ack[0-9]*'` so the text keeps showing, because behind a node without the
  `0x41` frame it is the only signal the operator gets that the DM is held (spec §3 forbids
  filtering it silently). Never "fix" this into symmetry.
- **No `held` is synthesised from that `:sto` text**, and no push is emitted for `failed` or for
  `acked`-after-`held` — both deliberate, reasons in the plan's §6.1/§6.2.
- **The frame decoder needed no change and still needs none.** `parse_ack_appendix` walks by the
  length byte and `_ACK_APPENDIX_MAX_LEN = 10` already covers the spec's `n <= 9`. An unknown
  status byte is reported as `unknown(...)`, never an error — the spec is explicit about that.

## Unread Cursors (`read_cursors`, sidebar badges)

Server-authoritative "what has the operator seen" state behind the webapp's sidebar badges and
the PWA app-icon badge. Plan and the field evidence: `doc/2026-09-06_1200-unread-cursor-plan.md`.

- **A cursor is a timestamp, never a count.** `read_cursors(key, ts)` holds the ingest `timestamp`
  of the newest message seen; `unread = COUNT(timestamp > cursor AND base(src) != base(me)
AND NOT suppressed)`.
  The previous scheme (`read_counts`, v7: "the total was N when I looked") broke every time the
  count shrank under retention, the blocklist filter or the webapp's 2000-row cap, and was stale
  on every device except the one that did the reading. `read_counts` is still emitted and served
  for one release (v2.0.4) and is dead weight after that.
- **`unread` excludes what the client would never render; `count` does not.** The read cursor only
  advances over RENDERED bubbles, so a conversation whose NEWEST message the webapp hides had a
  badge no client action could clear (group 20 on mcapp.local, stuck at +1 against a `node_advert`
  link — `doc/2026-09-19_2140-unread-suppression-plan.md`). `get_conversation_summary` therefore
  runs `storage/suppression.py`, a server-side mirror of the webapp's `isSpamByClassifier` plus the
  unconditional `isTextBlocked` half of `passesBaseGuards` (`enabled: false` disables the classifier
  half, NEVER the blocklist). `count`/`last_ts` stay unfiltered on purpose — a hidden message still
  belongs to its conversation. The predicate is pinned by `suppression_vectors.json`, canonical
  here and hand-copied to the webapp with a sha256 on both sides; it is a **fifth** corpus on top of
  the four in Key Gotchas, and nothing syncs it for you. The aggregate and candidate queries in
  `query.py` MUST keep sharing `_conv_dedup_subquery` / `_CONV_NEWER_EXPR` / `_CONV_NEWER_SPAM_EXPR`
  — if their predicates disagree the subtraction silently corrupts the count, and three mutations
  that break it are pinned by `unread_suppression_tests.py`.
- **The live broadcast carries the classifier fields, and it does so by MUTATING the shared dict.**
  `MessageRouter.publish` hands ONE `data` object to every subscriber in subscription order;
  `_storage_handler` is subscribed in `MessageRouter.__init__` and therefore runs before
  `SSEManager._broadcast_handler`, so `store_message` annotating `message` in place is what puts
  `category`/`tags`/`info_score`/`template_hash`/`classifier_ver` on the live SSE payload. Both that
  annotation and `_build_message_dict`'s history reconstruction go through `_classifier_fields`, so
  live and reloaded views cannot drift. **That ordering is load-bearing** — invert it and the live
  payload silently loses the fields again; `live_classification_tests.py` asserts the two
  subscribers' indices. `_broadcast_handler`'s own "shallow COPY, never the shared dict" comment is
  about the `dst` rebucketing, a different and non-additive mutation, and still stands. Until
  2026-09-20 the fields were INSERT parameters only, so a hidden message rendered live, the read
  marker advanced over it, and the badge resolved itself — self-healing, but live and history
  disagreed about the same message.
- **`tags` is presence-gated, not truthiness-gated, and that is not a style choice.** An EMPTY tag
  list is the common case — 19644 of 21048 classified rows on the live DB carry `tags = '[]'` — and
  mc-chat's `meshcom_mock/wire.py` emits `tags: []` for exactly those rows. Dropping the key on `[]`
  changes the history payload of 93% of classified messages and diverges the two backends on a wire
  contract they share. A NULL or undecodable column still omits the key.
- **A distinct message is FOUR legs, not just `msg_id`.** `_conv_dedup_subquery` groups by the
  msg_id-or-rowid group AND the resolved sender base AND the conversation key AND a
  `DEDUP_WINDOW_MS` time fence, mirroring the ingest rule (`_find_duplicate_row_id`); both call
  `sender_base_sql` (`storage/constants.py`) so the ingest and read-time boundaries cannot drift. A
  firmware `msg_id` is a node-local counter that gets REUSED: on the live DB 572 multi-row groups
  split cleanly into 454 real transport pairs (≤ 172 ms apart, max size 2, never spanning a sender,
  dst or key) and 118 reuse groups (≥ 3.33 h apart, 71 across more than one conversation key).
  Grouping on `msg_id` alone made narrowed and full-scan `count` disagree for 10 keys AND hid real
  unread messages — a reuse group took its OLDEST copy's timestamp, so a recent message under an
  11-day-old msg_id sat before the read cursor and never lit the badge. The time fence is anchored
  on the data (`_CONV_ANCHOR_SQL`), NEVER a `timestamp / W` bucket: a fixed boundary falling between
  a 172 ms pair splits it and recreates the unclearable `+1`. **Cost, measured end-to-end through
  `get_conversation_summary` on mcapp.local against the live DB: 460 ms → 990 ms** for the full
  scan (once per client connect, off-loop; `/events` is exempt from `StallMiddleware`) and
  178 ms → 266 ms narrowed. The dedup subquery alone is 313 ms and **both** the aggregate and the
  candidate query execute it, so its cost is paid TWICE per call — measuring the subquery in
  isolation understates the real figure by about half, which is how the first recorded number
  (517 ms) came out low. Materialising it once is the obvious lever if this needs to come down.
- **Keys are `conversation_key`, on both ends of the wire.** DMs are `A<>B` (sorted base
  callsigns), groups/hashtags/`*` verbatim. The webapp translates to its sidebar key at exactly
  one boundary (`translateServerSummaryKey` / `serverKeyForSidebarKey`). `read_counts.dst` stored
  whatever the client sent, which is why the one-shot seed at startup
  (`seed_read_cursors_from_counts`) has to translate `A~B` pairs and bare partner calls itself.
- **Writes are `MAX(existing, incoming)`, and the write returns the stored value.** A second
  device or a delayed retry must never move the mark backwards. `POST /api/read_cursor` answers
  `{ts, unread}` and broadcasts `proxy:read_cursor {key, ts, unread}` to every client: the
  `unread` rides along because the webapp's local window is capped and cannot recompute it once
  the cursor moves.
- **Own traffic is excluded by BASE callsign**, not exact SSID: `DK5EN-98` and `DK5EN-14` are
  both the operator. A message you send from another node must not light a badge here.
- **The own-message whitelist (spam filter, blocked texts, blocklist verdict) is a webapp
  display-side rule, not an unread-count rule.** It exempts a self-sent message from
  `isSpamByClassifier`/`isTextBlocked`/the blocklist verdict at the webapp's call sites only, so it
  never silently vanishes from the chat view. `storage/suppression.py` mirrors those two predicate
  functions verbatim and is deliberately unaware of the exemption — `unread` already excludes own
  traffic by base callsign above. Do not add it to `suppression.py` or to
  `suppression_vectors.json`.
- **`proxy:read_cursors` is emitted unconditionally, `{}` included**, for the same reason as
  `blocked_callsigns`: the client max-merges, so an empty burst is harmless and a gated one leaves
  a reconnecting client stuck with stale local cursors.
- **The webapp marks read on render while the tab is visible, in every mode.** The old scheme
  marked on conversation switch only, so "All / No Filter" never marked anything and badges grew
  while the messages were on screen.
- **Unread is counted per DISTINCT message, judged by its EARLIEST stored copy.** The same
  message lands as two `messages` rows with the same `msg_id` about 100 ms apart (UDP datagram
  plus the BLE copy; the v3 migration dropped the `msg_id` UNIQUE constraint on purpose), while
  the webapp dedups to the first copy and marks read with that copy's timestamp. A per-row count
  leaves the later sibling "newer than the cursor" forever: on mcapp.local every conversation
  whose newest message arrived over two transports sat at **+1** with nothing a client could do
  (v2.0.4-dev.1). `get_conversation_summary` therefore groups by `msg_id` (rowid fallback for
  id-less rows) and takes `MIN(timestamp)` before joining the cursors. Symptom → cause: a badge
  stuck at exactly 1 per conversation means a query is counting transport copies, not messages.
  The pairs themselves were an ingest RACE, fixed the same day: the dedup gate was a
  check-then-insert against `messages` with classification and the SQLite write between the
  two awaits, and the two copies arrive as separate router tasks 40-170 ms apart. 984 pairs in
  one week, none slower than 172 ms. `_claim_recent_ingest` now claims `(sender, msg_id)` in
  memory synchronously before the first await; the DB lookup stays as the restart backstop.
  Rows written before v2.0.4-dev.3 still hold the pairs, so the per-message aggregation stays.

## Blocklist (`sperrliste.json`)

The curated global blocklist, maintained in this repo and fetched by every node from
`raw.githubusercontent.com/DK5EN/McApp/main/sperrliste.json` (branch **main**) (`commands/handler.py`). It is
merged with admin `!kb` kickbans into `CommandHandler.blocked_callsigns` and pushed to clients over
SSE. Design notes for the retroactive fix: `doc/2026-08-30_0930-blocklist-retroactive-plan.md`.

- **The URL is pinned to `main`.** A commit that only reaches `development` blocks nobody. This has
  already cost one debugging session.
- **`blocklist_decision` is an INGEST gate, so blocking used to be forward-only.** It ran on ingest
  (`main.py`) and on the live broadcast (`sse_handler._broadcast_handler`) and nowhere else, which
  left every row a station had deposited _before_ it was blocked in `messages.db`, replayed to
  every client on every reload. The sperrliste is curated centrally and lands on boxes we do not
  administer, so a per-host `DELETE` is not a fix. `MessageRouter.filter_history_row` is now applied
  on the way **out** of storage — `get_smart_initial_with_summary` and `get_messages_page` take a
  `blocklist_filter` — and that is the only thing making an entry retroactive.
- **The summary counts must be filtered with the same predicate as the messages.** They drive the
  sidebar badges; an unfiltered summary keeps advertising a conversation whose messages the filter
  just removed. `has_more` is the opposite case: it stays keyed on the **raw** row count, or a page
  that filters to empty reads as "start of history" and the client stops paging backwards.
- **`blocked_callsigns` must be emitted BEFORE `smart_initial` in the SSE burst.** The webapp applies
  the set at one ingest chokepoint (`messageProcessor`), so anything delivered ahead of it is
  admitted against an _empty_ blocklist and stays on screen. Emitting history first is exactly why a
  blocked station survived every reload even with a correct list on both ends. Order is load-bearing.
- **Offline-cache hydration is the one door into the webapp's store that bypasses
  `processDataElement`.** `source === 'hydrate'` routes rows straight into `msgData`, so cached rows
  were immune to the blocklist forever. Gated now, plus `purgeBlockedCallsigns` (memory + IndexedDB
  - positions) on every `proxy:blocked_callsigns` snapshot. All three sites share
    `blocklistVerdict()` so they cannot drift.
- **The refresh is 15 min with a conditional GET, not 24 h.** An unchanged list costs a 304.
  `_apply_sperrliste` REPLACES the curated portion instead of unioning it, so an upstream removal
  un-blocks without a restart — but an entry an admin also kickbanned locally is protected from
  that removal (provenance comes from the persisted kickban table; `blocked_callsigns` is a flat
  union and knows none). The union runs after the subtraction and unconditionally, because
  `!kb delall` clears the whole set and the next refresh has to restore the curated entries.
- **The ETag is only stored for a payload that validated.** Caching the tag of a malformed list
  turns every later refresh into a 304 and pins the node to its last good list forever.

## RF Monitor (`wire:frame`, `/api/monitor/frames`)

`wire_monitor.py` keeps an in-memory ring (2000 envelopes, per process, not persisted) of every
frame this backend saw or sent, and broadcasts each one as the bare SSE event `wire:frame`. The
contract is canonical in the webapp repo (`docs/rf-monitor-plan.md`); mc-chat implements the same
one, so change both together.

- **One verdict source.** `sse_handler.broadcast_verdict()` decides what `_broadcast_handler`
  delivers (link-check drop, command-echo drop, blocklist drop/redirect), and the monitor calls the
  same function, so the live stream and the monitor can never disagree. Add a new SSE filter there,
  never inline in `_broadcast_handler`.
- **RX** subscribes to `mesh_message` (source `udp` only), `ble_notification` (mesh `type`s
  msg/pos/tele/ack only; register frames and the synthetic `source == "self"` echo are skipped)
  and `ble_status` (captured as SYS; `type: "sys"` is forced last). The envelope carries the
  ORIGINAL frame, before any spam-group `dst` rewrite.
- **TX has exactly one capture point**: `MessageRouter._handle_outbound`, one envelope per attempt
  (`sent` / `failed` / `suppressed`), link `app`. `_send_via_udp` / `_send_via_ble` return `None` on
  success or a short failure reason for that purpose. Command replies travel through the same point,
  so a resolved `!wx` is `suppressed` (the raw command) plus `sent` (the reply).
- `capture()` deep-copies and never raises into the publish path.

## Web Push

Web Push to browser / iOS-PWA clients, sharing one wire contract with mc-chat so both backends behave identically.

- **Contract:** `src/mcapp/contract/push_contract.json` (**v7**) — defines the three `/api/push/*` endpoints, the filter `{ dm, groups[], broadcast }`, and match/eligibility/dedup/coalesce/payload semantics. `push_tests.py` runs every vector and pins the corpus sha256; mc-chat runs the same corpus. Inline `contract vN` references in the source name the version that _introduced_ a clause — they are provenance, not staleness, and must not be bumped on a sync.
- **A subscribe POST replaces the stored filter wholesale, and that is load-bearing in both directions.** Normative since **contract v6** — read `endpoints.subscribe.semantics`, which is the authority; this bullet only summarises it. The request body is the complete new filter state, never a patch, so the backend cannot distinguish "the user cleared their groups" from "the client POSTed before its own settings finished loading" — which is exactly how the webapp silently wiped a live subscription's groups on 2026-08-17 (fixed client-side in webapp v1.6.14-dev.42, see its `docs/backlog.md` B2). v6 therefore puts the ordering obligation on the **client** ("resolve stored prefs first, POST second") and forbids the server-side workaround: **do not add a heuristic** that ignores a default-looking filter or merges it into the stored one — that would break clearing groups on purpose and diverge the two backends.
- **The delivered payload text is stripped of the firmware ack-request suffix; the gates are not.** Normative since **contract v7** — read `payload_ack_suffix_semantics`, which is the authority. `build_push_payload` strips the $-anchored `\{[0-9]+$`(**strict: no closing brace** — there is no`{NNN}`on the wire and there never will be, so a trailing`{NNN}`is ordinary chat text) and trims, **before** the 120-char truncation, so the cap carries 120 chars of real text and a truncation can never split the suffix into a bare`{`. `handle_mesh_message`gates eligibility/blocklist/dedup on`_build_gate_view`(**unstripped**) and builds the delivered payload only after every gate passes — **do not reorder**: stripping first widens dedup's msg_id-less`(src, dst, text)`fallback key so two messages differing only in their ack counter collapse into one, and it makes clause (d) depend on ping recognition being a prefix check. Both builders share`_payload_fields`so they cannot drift. Do **not** reuse mc-chat's`strip_ack_request`/ the webapp's`stripAckRequestSuffix`here — those are the looser`\{\d+\}?$`echo-matching variant and would strip`{pong}{451010884}`to`{pong}`, reopening the v5 bug. `_test_ack_suffix_stripped_after_gates` pins the ordering via dedup (verified by mutation; a link-check vector does **not** discriminate it).
- **Routes:** `src/mcapp/sse_routes/push.py`. **Delivery:** `src/mcapp/push_delivery.py` — pure `matches()`/`is_eligible()` (resolve via-routed dst to the **last** comma-component; exclude non-chat frames and own-src), `PushCoalescer` (5 s window), `PushDedup`, and a background dispatcher calling `pywebpush` via `asyncio.to_thread` with timeouts. **The mesh-ingest path never awaits delivery** — a no-internet Pi must not stall the event loop / SSE heartbeats.
- **Storage:** `push_subscriptions`, upsert by endpoint. Prune on pywebpush **401/403/404/410**.
- **VAPID (two gotchas, both hit on first real delivery):** the keypair is generated once and persisted as the **raw base64url 32-byte scalar**, NOT PEM (pywebpush's `Vapid.from_string` base64-decodes it and dies on a PEM), at `/var/lib/mcapp/vapid.json` — never committed, and kept `0600` (a readable raw private scalar lets any local account forge VAPID JWTs as this node; `load_or_create_vapid` re-tightens a wider pre-existing file on load). JWT `sub` must be a valid FQDN (`mailto:admin@example.com`); Apple returns **403 `BadJwtToken`** for a no-TLD/`localhost` sub. Override via `MESHCOM_VAPID_SUB` — applied on **load**, so an existing install can fix its `sub` without regenerating a key and invalidating every subscription.
- **VAPID path resolution** is per call, not import-time: `MESHCOM_VAPID_PATH` wins, else `MCAPP_ENV=dev` writes under `$XDG_STATE_HOME`/`~/.local/state/mcapp`, else `/var/lib/mcapp`. If the chosen directory is unwritable the key falls back to the user state dir rather than going **ephemeral** — an ephemeral key rotates on every restart and silently kills every stored subscription.
- Delivery needs outbound internet from the Pi and degrades silently without it. `/api/push/*` is covered by the existing `^/api/` proxy rules — no Caddy change.

## Configuration

`/etc/mcapp/config.json` (dev: `/etc/mcapp/config.dev.json`, auto-selected via `MCAPP_ENV=dev`).
BLE mode: `remote` or `disabled` (`MCAPP_BLE_MODE` env override). See `ble_service/README.md` for the BLE service API.

## Inbound Charset (`text_decode.py`)

One policy, one module, both ingest routes. Firmware background: CHR-03
(`MeshCom-Firmware` fork-main `16670de9` + `094636b2`), `docs/BACKLOG.md` there.

- **The payload is no longer guaranteed valid UTF-8, by firmware design.** Senders like
  PinPoint put umlauts on the wire as single CP1252 bytes (`ü` = `0xFC`, not `C3 BC`); since
  CHR-03 the firmware relays them unchanged instead of dropping them, so the deciding is ours.
  `decode_text` re-reads every UTF-8-rejected byte as CP1252. `errors="ignore"` — what both
  paths used before — deletes the character silently. Only the five bytes undefined in CP1252
  (`0x81 0x8D 0x8F 0x90 0x9D`) become `U+FFFD`.
- **The filter is a BLACKLIST and must stay one.** It rejects Unicode categories
  `Cc Cf Cs Co Cn` and nothing else. Its predecessor, `is_allowed_char`, was a whitelist that
  had to be extended by hand for every legitimate character nobody had enumerated, and it lost
  that race repeatedly: joined emoji sequences (2026-08-30), `Ç` and `Ñ` while `ç` and `ñ` were
  listed, the entire Nordic/Icelandic set, and every decomposed accent (`u` + `U+0308`, a mark,
  therefore neither symbol nor punctuation). Adding characters back to an allow-list is the
  wrong repair for the next report of this shape.
- **`U+200D` and the tag range `U+E0020..U+E007F` are the only `Cf` exceptions.** They carry no
  glyph and only bind neighbours into ONE grapheme, so dropping one SPLITS a sequence rather
  than removing a character. The variation selectors (`Mn`) and the enclosing keycap (`Me`) need
  no exception — marks are not a rejected category.
- **`Cn` is judged against the RUNNING Python's Unicode tables.** A codepoint assigned after that
  release reads as unassigned and is dropped. It is the one way this filter can still be wrong
  about a legitimate character, and it self-heals on a Python upgrade.
- **Both transports must use it, and that is the point.** The UDP path ran the whitelist over the
  whole datagram; the BLE path ran no character filter at all. The same message arrives on both
  (~100 ms apart) and `_claim_recent_ingest` keeps whichever copy lands FIRST, so the stored text
  of any message with an unusual character was decided by a transport race. In `ble_protocol.py`
  this applies to the message BODY only — `path` and `dest` are callsign fields and stay ASCII.
- **`ble_service/src/main.py`'s `D{` decode stays strict on purpose.** There the
  `UnicodeDecodeError` IS the evidence of the firmware's 244-byte register clamp cutting
  mid-codepoint. A CP1252 fallback there would destroy the truncation detector.
- mc-chat carries the same `decode_text` semantics in `meshcom_mock/decoder.py`. Ported, never
  imported — separate repos.

## Key Gotchas

- **A `#TAG` destination is a hashtag channel, not a callsign — and `is_group()` stays numeric.** The MeshCom FW 4.36 RfC puts a `#OE-SOTA` token in the destination field. All three repos independently misclassified it as a personal DM, which sent it into `compute_conversation_key`'s DM branch where it was **split on its first hyphen** (`"#OE-SOTA"` → key `"#OE<>DK5EN"`), collapsing distinct tags and fragmenting one tag per sender. Fixed in `ea15511` by adding **sibling** predicates `is_hashtag()` / `dst_kind()` / `resolve_dst_target()` beside `is_group()` in `commands/parsing.py` — `is_group` was deliberately NOT widened, because it is pinned by a corpus mirrored in mc-chat and the webapp. Two invariants look like oversights and are load-bearing: classification is **case-insensitive** and **NOT length-bounded** — a tag failing either would fall straight back into the DM branch, which is the defect. The RfC's 9-char cap is send-side grammar, enforced at the API boundary, never in classification. `dst_kind` returns `"unknown"` (never `"direct"`) for a `#`-prefixed value that fails the tag charset: it addresses nobody, and is the shape most likely to arrive from a buggy or hostile sender. Contract: `commands/hashtag_dst_vectors.json` (32 vectors, sha256-pinned by `commands/hashtag_dst_tests.py`). **No prefix/subscription matching exists** (RfC US-3) — its stated rule contradicts its own worked examples, so implementing it would encode a guess. Background: `MeshCom-Hashtag-prep.md`.
- **Four vector corpora are hand-copied to the sibling repos, and nothing syncs them for you.** `commands/group_dst_vectors.json` (v2), `storage/conversation_key_vectors.json` (v4), `blocklist_decision_vectors.json` (v2) and `commands/hashtag_dst_vectors.json` (v1) are canonical **here**. The first three go to **both** mc-chat (`tests/fixtures/`) and the webapp; `blocklist_decision_vectors.json` goes to the **webapp only** (`src/services/__tests__/`) — mc-chat has its own `sperrliste.py` and never reads this corpus, so do not go looking for a copy there. mc-chat asserts parse-equality against the paths it does carry; the webapp pins a sha256 of the conversation-key corpus and runs drift checks against both siblings. Change one and you must copy it to every repo that carries it **and** bump the webapp's `EXPECTED_SHA256`, or their suites fail the moment anyone runs them with siblings checked out. Unlike `contract/`, these are not a git subtree — there is no `subtree pull` that will do it for you.
- **Two different ACKs, never conflate them.** `send_success` is the firmware's 7-byte **binary** ack (`ack_type` 0x00 Node / 0x01 Gateway, `ble_protocol.py`) — "my node or a gateway took the frame". `acked` is a matched inline `:ackNNN` text frame — "the addressee answered". `_handle_ack` publishes `msg_status` `{sent, ack_kind: node|gateway}`, the inline path publishes `{acked, ack_kind: "peer"}` with the ORIGINAL message's msg_id; the webapp renders only the latter as ✓✓ Delivered. Wiring the webapp's `msg_ack` to `send_success` is exactly the 2026-08-19 bug where three unanswered `!ctcping` probes all showed as delivered. `ack_status_tests.py` pins both payloads.
- **A BLE `D{` register frame carries at most 244 chars of JSON.** `addBLEComToOutBuffer` clamps at
  245 bytes, minus the `0x44` type byte; the firmware names it `BLE_JSON_PAYLOAD_MAX`. Over that it
  cuts **mid-value**, so the app gets an unparseable object, not a shortened one — every field is
  lost, not just the last. The builders in `command_functions.cpp` check against
  `MAX_MSG_LEN_PHONE - 2` (298), which looks like the limit but never binds; that mismatch is how a
  one-day `FWDATE` regression took the whole `I` register down on mcapp.local for 9 hours
  (2026-08-27). `ble_service` salvages such a frame by trimming to the last COMPLETE member — whole
  members only, never a coerced partial value. A node with all six `GCB` slots filled still
  overflows, so the salvage stays load-bearing.
- **All DB timestamps are in milliseconds** (not seconds). Divide by 1000 for `datetime.fromtimestamp()`. Forgetting this causes `ValueError: year 58089 is out of range`.
- **SSH + `python3 -c` quoting**: single-quote the Python code, `\"` for strings inside. Never use f-strings with dict key access — use `%` formatting, or write a temp script with `cat > /tmp/q.py << 'PYEOF'`.
- **MHeard beacons** (RSSI/SNR, no coordinates) and **position beacons** (lat/lon, no signal) used to be disjoint packet types. Since firmware `c4ad78bb`, an Extern-UDP `pos` packet with `src_type=="lora"` carries **both** — `store_message()` then updates both `station_positions` field groups. See the 2026-07-05 amendment in `doc/2026-02-11_1400-position-signal-architecture-ADR.md` and `doc/UDP-2.0-impl.md`.
- **Extern-UDP wire format** (node → proxy, JSON, port 1799, bidirectional): `rssi`/`snr` appear only on `pos`/`msg` packets and only since firmware `c4ad78bb` (2026-03-01) — detect by key presence, there is no protocol version field. Same rule for the hardware fields: `hw_id` rides on `pos` frames since forever and `hw_id`/`lora_mod`/`max_hop` on `msg` (text) frames since the 2026-09-16 firmware handover (`MeshCom-Firmware-DEV-Main/docs/2026-09-16_firmware-extudp-hw-id-on-text-frames.md`); an older node omits them and the proxy stores NULL. `udp_handler._coerce_hardware_fields` normalises them at the ingress choke point — ints, `lora_mod` masked to its low nibble exactly like the BLE path — and DROPS a present key that does not coerce rather than the frame. Never subscript them. Both are already final values: RSSI is dBm as-is, SNR is already ÷4 in firmware — **never re-scale either**. Only `src_type=="lora"` carries real signal; `"node"`/`"udp"` send a `0/0` sentinel and must be excluded by an explicit `src_type` check, not a range check.

## Deployment

`mcapp.local` (Raspberry Pi Zero 2W) is **the** production target and currently the only host running
MCProxy. `rpizero.local` used to be the integration target but no longer runs it at all — verified
2026-07-25: `mcapp.service` is absent there, `mcproxy.service` is masked, and the box runs mc-chat.

On-device layout:

- Slots: `~/mcapp-slots/slot-{0,1,2}`, with `~/mcapp-slots/current` symlinked to the active one
- Service: `systemctl status mcapp` — `ExecStart=/home/martin/.local/bin/uv run mcapp`; logs via `sudo journalctl -u mcapp.service -f`
- DB: `/var/lib/mcapp/messages.db` (SQLite, WAL)
- Deploy installs deps with `uv sync --all-packages` (pulls `pywebpush` + the BLE workspace member) — see `bootstrap/lib/deploy.sh`
- **Slot activation never restores the database, and must never again.** The Update page's
  Activate button (`POST /api/update/activate {slot}`, runner mode `activate`) switches code +
  webapp bundle + services to an already-deployed slot and keeps `/var/lib/mcapp/messages.db`
  as it is. Its predecessor, the Rollback button, overwrote the DB with `meta/slot-N.db` — a
  snapshot taken when slot N was last LEFT through the runner, which `mcapp.sh` deploys never
  refreshed — so on mcapp.local it would have replaced the live DB with a copy 4 days to 3 weeks
  old (found 2026-09-10, never pressed). Migrations are forward-only additive and the schema
  marker is never written downward, so older code on a newer DB is fine; a "restore the DB on
  rollback" feature is a data-loss bug, not a safety net. The served webapp is a COPIED directory
  (`/var/www/html/webapp`), not a symlink: a slot switch that only moves `current` leaves the old
  bundle on screen.

See `bootstrap/README.md` for installation, `doc/tls-architecture.md` for TLS setup, `doc/tls-maintenance-SOP.md` for maintenance.
