# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome.

## B2 — delivery status flags and their representation (no due date; design pass needed)

**Problem:** three different facts share one check mark, and two of them are not stored at all,
so "did my message go out?" is unanswerable after the fact. Full analysis and the evidence:
`doc/2026-09-03_2300-delivery-status-rca.md`.

- MCProxy folds the firmware's Node ACK (`0x00`, my node queued it) and Gateway ACK (`0x01`, a
  gateway took it onto the backbone) into `send_success = 1` and drops `ack_kind`, which is
  published on SSE and then lost (`src/mcapp/storage/ingest.py:770`, `833-848`). No ack arrival
  instant is stored either, and a second ACK overwrites nothing — the UPDATE is idempotent.
- The webapp sets `msg_www` for every own **local** echo (`messageProcessor.ts:305`), so the ✓
  lights up ~100 ms after the send regardless of delivery, and the one real end-to-end proof it
  has — the same message coming back from the oevsv.at firehose (`messages.ts:791`) — can never
  contribute anything because the flag is already true.

**Why deferred rather than patched now:** it spans MCProxy (schema + ingest + SSE), the webapp
(processor, store, ChatBubble) and the existing ✓ / ✓✓ semantics that the 2026-08 ctcping bug
already pinned (`storage/ack_status_tests.py`). Two independent local fixes would very likely
re-diverge the two sides. Wanted is one design pass that decides the state model first.

**Scope to decide in that pass:**

- What is stored: `ack_kind` as a column vs. widening `send_success` to an enum; whether the ack
  arrival instant is worth a column; whether a later Gateway ACK must be able to upgrade an
  earlier Node ACK (it must, if the distinction is to be useful).
- Migration + `LATEST_SCHEMA_VERSION` bump, plus what the snapshot builder ships to clients.
- Rendering: how many distinct states the bubble shows, and what each promises. Candidate set is
  queued / gateway-confirmed / seen-on-WWW / peer-acked — four facts, currently one and a half
  glyphs.
- `msg_www` gets its name back: internet-sourced duplicates only.
- mc-chat parity: `msg_status` is a shared wire shape, so any new field is a contract change.

## B3 — show release notes on the Update page (webapp-only, no due date)

**Problem:** the System Update confirm dialog names version numbers and nothing else — an operator
deciding whether to promote has no way to see what a release actually changed without leaving the
app to read the GitHub release page by hand.

**Why it's tracked here too:** `doc/release-history.md` in this repo is the source of the release
body text (`release.sh` publishes it verbatim via `gh release create --notes-file`), so anyone
touching the notes format should know the webapp is meant to start rendering them. The fix itself
is entirely webapp-side — the GitHub releases API response the webapp already fetches
(`useVersionCheck.ts`'s `fetchReleases()`) carries the release `body`; it's just discarded today.
No MCProxy change, no new endpoint, no contract.

**Tracked as B5 in the webapp repo:** `webapp/docs/backlog.md`, full design brief in
`webapp/docs/change-log-update-disp.md` (shared-modal vs. page-card tradeoff, markdown-safety,
rate-limit and long-body constraints, what's out of scope).

## B4 — reduce the memory footprint on mcapp.local (no due date; measured 2026-09-15)

**Problem:** the Pi Zero 2 W has 512 MB physical, Linux sees 415 MB, and only ~144 MB of that
is usable for the kernel's own non-movable allocations because the stock boot config reserves
64 MB for the GPU and a 256 MB CMA pool on a headless box (`dmesg`: "Memory: 144696K/458752K
available (... 262144K cma-reserved)"). Slab alone is 62 MB. After 16 days of uptime 116 MB sit
in zram swap (24 MB physical, zstd), 0 OOM kills, load 0.08 — healthy, but with no headroom for a
larger history query or a second client burst. Measured with `smaps_rollup` PSS per process and
per-package import RSS in the slot venv on the Pi itself.

| Consumer                                             | RSS   | swapped | note                                  |
| ---------------------------------------------------- | ----- | ------- | ------------------------------------- |
| mcapp                                                | 45 MB | 40 MB   | 67 MB right after import, peak 95 MB  |
| mcapp-ble (uvicorn)                                  | 43 MB | 4.5 MB  | 45 MB right after import              |
| caddy                                                | 21 MB | 3 MB    | Go runtime, 14 threads                |
| journal-upload + journald + journal-remote           | 21 MB | 23 MB   | Pi-to-Pi log shipping                 |
| unattended-upgrade-shutdown                          | 7 MB  | 13.5 MB | only the shutdown hook, not the timer |
| 2 x `uv run` wrappers                                | 3 MB  | 18 MB   | idle parents of the real processes    |
| tmpfs logs (`/run/log/journal`, `/run/journalxship`) | 26 MB |         | config ceiling 20M + 32M              |
| kernel slab                                          | 62 MB |         | 34 MB reclaimable, ext4 inodes 16 MB  |

Import cost, cumulative in the mcapp venv: bare interpreter 9 MB; asyncio/sqlite3/logging
+11 MB; pydantic +4 MB; **fastapi +17 MB**; uvicorn +3 MB; httpx +2 MB; **cryptography +7 MB**;
**pywebpush (aiohttp + requests + py_vapid) +12.5 MB**; `mcapp.main` itself +1.3 MB. The BLE
service pays the same fastapi/uvicorn/pydantic bill (36 MB) a second time. `timezonefinder` is
already lazy and numpy is not loaded in production — leave that alone.

**Items, ranked by MB per effort.** Realistic total from 1–6: 100–150 MB, i.e. used memory from
245 MB to roughly 120–150 MB with no swap pressure. Measure before and after each with the same
PSS script; the numbers above are the baseline.

1. **Boot config (largest win, no code).** `gpu_mem=16` returns ~48 MB to MemTotal
   (`vcgencmd get_mem arm` reads 448M today). Shrink CMA: `dtoverlay=vc4-kms-v3d,cma-64`, or drop
   the overlay and `dtparam=audio=on` entirely — nothing on the box drives HDMI or audio. This is
   what actually pushes processes into zram today. Belongs in `bootstrap/lib/system.sh` with a
   `SYSTEM_EPOCH` / `REQUIRED_SYSTEM_EPOCH` bump; needs a reboot, so the converge path must not
   assume it applies live.
2. **Slim or fold in the BLE service (30–45 MB).** Its API surface is small (REST + SSE + API
   key, see `ble_service/README.md`). Options: run BLE in-process when mcapp is on the same box
   (full 45 MB, loses crash isolation and the remote-brain topology the service exists for), or
   rewrite the service without FastAPI on bare asyncio/starlette (~30 MB). At minimum drop the
   `uvicorn[standard]` extras in **both** `pyproject.toml`s — uvloop, httptools, websockets and
   watchfiles are loaded for nothing (watchfiles is reload-only, websockets is unused).
3. **Cut the pywebpush chain in mcapp (12–20 MB).** Replace `pywebpush` with VAPID JWT +
   aes128gcm over the already-loaded httpx; `requests` and `aiohttp` (and their CJK codec
   imports via charset_normalizer) disappear. `cryptography` stays (7 MB) — it does the actual
   ECDH/HKDF. Lazy-importing pywebpush on first dispatch only helps boxes with no subscribers,
   which mcapp.local is not. Contract semantics (`push_contract.json` v7+) are untouched; the
   mocked `webpush_fn` seam in `push_delivery.py` is where the swap happens.
4. **Malloc tuning, cheap experiment.** `Environment="MALLOC_ARENA_MAX=2"` in both unit
   templates. mcapp shows 40 anonymous rw regions across 4 threads, consistent with glibc arena
   sprawl; typical gain 5–15 MB on long-running Python. Optionally
   `MALLOC_TRIM_THRESHOLD_=131072`. Verify, do not assume.
5. **Logs in RAM (10–15 MB).** Logs ship to rpizero via journal-upload, so lower journald
   `RuntimeMaxUse` from 20M to 8M (`bootstrap/lib/system.sh`, journald drop-in) and cap the
   32 MB `/run/journalxship` tmpfs that journal-remote fills to 8–16 MB.
6. **Disable `unattended-upgrades.service` (7 MB RSS).** It is only the
   `unattended-upgrade-shutdown --wait-for-signal` hook; `apt-daily-upgrade.timer` keeps doing
   the upgrades. Lost: finishing an in-flight upgrade on shutdown. `configure_unattended_upgrades`
   in `bootstrap/lib/system.sh` is the place.
7. **Exec the venv directly.** `ExecStart={{HOME}}/mcapp-slots/current/.venv/bin/mcapp` (and the
   uvicorn equivalent) in `bootstrap/templates/*.service` instead of `uv run` — removes two idle
   wrapper processes and the uv resolution step on every restart. `uv sync --all-packages` in
   `deploy.sh` still owns the environment; the runner does not need `uv run` at exec time.
8. **Caddy: `GOMEMLIMIT=24MiB` or `GOGC=50`** in `bootstrap/templates/caddy/caddy.service`,
   3–5 MB. Replacing lighttpd with Caddy's `file_server` saves 2.4 MB and is not worth the
   change.

**Open question, needs one more sample:** mcapp grows from 67 MB at import to 85–95 MB live. No
unbounded cache found in the code (`PushDedup`'s `OrderedDict` and the meteo cache are small),
so this is most likely SQLite page caches on the history queries plus arena fragmentation, not a
leak. Re-measure PSS a day or two after the 2026-09-14 17:39 restart before treating it as one.
Also note: `memory.current` reads 0 for every cgroup because `cgroup_enable=memory` is missing
from `cmdline.txt` — add it alongside item 1 if per-service accounting is wanted.
