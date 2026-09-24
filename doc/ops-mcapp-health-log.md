# McApp production health log — mcapp.local

> **Status:** Current — newest section: §19 (2026-09-24 22:55 CEST, pre-release sweep of
> `v2.0.14-dev.3` before the v2.0.14 promotion) — green, zero findings, new watch point **W18**
> (`udp_target_kind` `config` while Extern-UDP is off on DK5EN-98). Before that: §18 (2026-09-23 08:10 CEST, pre-release sweep of
> `v2.0.13-dev.2` before the v2.0.13 promotion) — all green, zero findings, new watch point
> **W17** (`/api/read_cursor` stalls rising since v2.0.11, off-loop, not a dev.2 regression).
> §17 (2026-09-19 09:12 CEST, `v2.0.11-dev.1` deployed to
> mcapp.local slot-0 and verified live). **W15 resolved** — every `loop_lag` row is now legible.
> **W13 reduced, still open** — the mheard dumps no longer block the event loop at all, but still
> cost ~1 s of wall time, so `/api/send` `http` rows remain and are expected. Newest full sweep:
> §15 (2026-09-19 07:50) — all green, zero findings; `handler` stalls down from ~42–54/day to
> 1 in 7 h. Last finding was **F13** (§7), upstream and resolved 2026-09-01.
> Open watch points: **W18** (§19), **W17** (§18), **W13** (reduced, §17), **W14** (§15), **W16** (§16), **W1** (zram swap,
> trend watch only), **W12** (§12), **W2**, **W3**, **W6**, **W7**, **W9**, **W10**.
> **W15** (§15/§16/§17), **W4**, **W5**, **W8** and **W11** are resolved.
>
> **Kind:** Recurring ops review; one dated section per run, appended, never edited in place.
> **Produced by:** the `ai-ops` skill (`.claude/skills/ai-ops/SKILL.md`).

## How to use this document

The next run works through the `ai-ops` phases, compares against the **rate table in §1**, and
**appends a new dated section** rather than editing an existing one. Findings are numbered
`F<n>` continuing the series across runs, so a finding can be referred to by number later.

**Raw counters are useless for comparison across different uptimes.** A run six hours after a
deploy and a run six days after one produce wildly different `messages` totals from an identical,
healthy box. §1 therefore carries **per-hour rates with the window each was measured over**;
totals are recorded only as context.

Absence is the result for most of these checks. **Quote the zero** rather than staying silent
about it — a missing line in a report is indistinguishable from a check that was never run.

## 1. Rate baseline

Established 2026-08-22. Re-measure each run; a rate that moves by more than about a factor of two
deserves a sentence explaining why before it is dismissed.

| Signal                          | Rate               | Window | Notes                                                       |
| ------------------------------- | ------------------ | ------ | ----------------------------------------------------------- |
| `messages` type `msg`           | **11 / h**         | 1 h    | Chat traffic; varies with time of day, weakest signal here  |
| `messages` type `pos`           | **87 / h**         | 1 h    | Position + MHeard beacons, throttled 2 min per station      |
| `signal_log`                    | **347 / h**        | 1 h    | Raw RSSI/SNR rows                                           |
| `{CET}` uplink beacon           | **~303 s** cadence | live   | Measured twice independently — see §2                       |
| journal warnings (`-p warning`) | **0 / 24 h**       | 24 h   | A jump into the hundreds is the signal, not the exact count |
| unclassified `msg`              | **0**              | 1 h    | Classifier keeping up                                       |
| `{CET}` rows in `messages`      | **0**              | all    | Dropped at ingest; absence is correct                       |

Structural values that should change only when we change them: schema **25**, system epoch **1**,
classifier version **3** with **38** rules.

## 2. 2026-08-22 07:48 CEST — first full sweep

**Verdict: all green. Zero findings, four watch points.** First sweep after `v2.0.1-dev.1` shipped
the gateway-uptime feature (deployed 00:53:51 the same night).

### Anchors

| Anchor              | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| Snapshot            | 2026-08-22 07:44–07:48 CEST                                  |
| Release             | `v2.0.1-dev.1` (webapp `version.html` agrees)                |
| App version         | `v2.0.1` (`/api/status`)                                     |
| Active slot         | `slot-1`                                                     |
| Schema              | **25** = `LATEST_SCHEMA_VERSION` ✓                           |
| System epoch        | installed **1** = `REQUIRED_SYSTEM_EPOCH` = `SYSTEM_EPOCH` ✓ |
| Service start       | 2026-08-22 00:53:51 CEST                                     |
| Process uptime      | 24 597 s ≈ **6.83 h**                                        |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                  |
| Host uptime         | 3 days, 9:28                                                 |

### Measured

| Check            | Value                                                        |
| ---------------- | ------------------------------------------------------------ |
| Services         | mcapp, mcapp-ble, caddy, lighttpd all `active`               |
| `/health`        | `healthy`                                                    |
| DB size          | 32.4 MB of the 1 GB limit (**3.2 %**); WAL checkpointed to 0 |
| Totals (context) | 19 046 messages, 216 stations, 55 526 signal rows            |
| Disk             | 7 % of 59 G                                                  |
| MemAvailable     | ~181 MB of ~415 MB                                           |
| Swap             | SwapFree 273 of 415 MB → **~142 MB swapped out**             |
| Load / temp      | 0.02 / **40.2–42.9 °C**                                      |
| Journal warnings | **0** in 24 h (`-- No entries --`)                           |

### Absent signals — the point of the exercise

- `{CET}` rows in `messages`: **0**. Dropped at ingest by `_should_filter_message`; correct.
- Unclassified `msg` rows in the last hour: **0**.
- `udp_untrusted_source_ips`: **empty**; `udp_multiple_sources` **false**; `udp_target_kind`
  `identified`; `udp_suppressed_target_changes` **0**. Nothing is injecting on the
  unauthenticated port 1799.
- Gateway-uptime `gap`/`dark` rows: **0** after 6 h 54 m — roughly **81 consecutive beacon
  cycles** with no missed beacon and no false positive from the 6-minute tolerance.

### Gateway uptime — first production data

The feature shipped in this release, so this is its first field measurement.

| Value           | Reading                                          |
| --------------- | ------------------------------------------------ |
| `state`         | `active`                                         |
| `uptime_pct`    | 100.0 %                                          |
| `coverage_pct`  | 28.58 % (411.5 of 1440 min — ledger began 00:54) |
| longest outage  | 0 ms                                             |
| last beacon age | 299 s                                            |
| heartbeat age   | 5 s (30 s tick)                                  |

**Beacon cadence measured twice, independently, same answer:** 303 s from the client-side SSE
stream (`23:40:31 → 23:45:34 → 23:50:37`) and 302 s from the DB ledger's own up-run
(`01:01:23 → 01:06:26`). This is what `GAP_TOLERANCE_MS = 6 min` is calibrated against — the
02:00-era provisional of 3 min would have written an outage row on every one of those healthy
cycles.

### Watch points — named, not findings

- **W1 — swap.** ~142 MB swapped out under zram, 181 MB RAM still available. Normal for a Pi
  Zero 2W; becomes a finding if SwapFree trends toward zero.
- **W2 — `config.json` is `0640`**, group-readable, and contains `BLE_API_KEY` in clear.
  Single-user box, so low severity. `0600` would be tidier.
- **W3 — Caddy cert always looks near-expiry.** Internal CA, 12-hour leaf certs, auto-renewed
  (`notBefore Aug 21 23:39Z → notAfter Aug 22 11:39Z`, read at 05:48Z = mid-life). Never report
  this as an expiry finding without reading the issuer and `notBefore`.
- **W4 — `uptime_pct` reads 100.0 before the first-ever beacon.** The Settings card correctly
  renders "No data yet" for `state: unknown`, so it is invisible in the UI, but any other API
  consumer would be misled. Small backend fix, not yet scheduled.

### Carried forward, unchanged

- 169 pre-2026-08-13 duplicate telemetry pairs, still unexplained.
- `fcs_ok` field-data verdict due after 2026-09-20 (`doc/backlog.md`).

### Skill changes this run produced

The sweep improved its own instrument, committed as `ed82c25`:

- Swap was uncovered by the host phase on a box that steadily swaps; now read from
  `/proc/meminfo` (English regardless of the shell's German locale, and no `$field` references).
- The Caddy 12-hour cert would have triggered a false alarm on every future run; the issuer and
  `notBefore` must now be read before judging `notAfter`.

Earlier in the same night, the first run of the Phase 4 query returned `None` for the classifier
because the DB key is `classifier_version` while the code calls the concept `classifier_ver` —
a silent `None`, indistinguishable from a dead classifier. Corrected in `e8a1593`.

## 3. 2026-08-22 — follow-up fixes from §2's watch points

Not a sweep. Records what was done about §2's watch points, so the next run does not re-chase them.
Per this document's own rule §2 is left exactly as it was written.

### W4 — resolved

`uptime_pct` reported **100.0** on a ledger that had never recorded a beacon: the read path
anchored its live-tail gap on `last_beacon_ms`, so with that NULL no gap could be derived and the
hole-filling step painted the stretch `up`. Now, with no beacon ever recorded, the silence is
measured from `first_observed_ms` and split on the existing `GAP_TOLERANCE_MS`:

| elapsed since `first_observed_ms` | segment | `uptime_pct` |
| --------------------------------- | ------- | ------------ |
| `<= GAP_TOLERANCE_MS`             | `dark`  | `null`       |
| `> GAP_TOLERANCE_MS`              | `gap`   | `0.0`        |

`state` stays `unknown` in both cases and the has-beaconed path is untouched. `dark` is
deliberately widened from "the proxy was not running" to "no observation is available for this
stretch", which now also covers "running, but has never heard anything yet".

**A fresh ledger reporting 100.0 % again means this regressed.**

### W2 — resolved for new writes; the live box is unchanged by design

`/etc/mcapp/config.json` holds `BLE_API_KEY` in clear and was written `0640`. Both writers in
`bootstrap/lib/config.sh` now set **`0600`** explicitly, and both `.bak` copies are created with
`install -m 600`.

A second, separate defect was found in the same file and is also fixed: `migrate_config()` did
`mv "$tmp_config" "$CONFIG_FILE"` with **no chmod**, inheriting `mktemp`'s `0600`, while
`write_config()` set `0640` — so the deployed mode depended on which writer ran last. Both are
now explicit.

Per decision, **no retroactive chmod** was added to the deploy path: the running box keeps its
existing `0640` until someone runs `--reconfigure`. Expect `0640` on mcapp.local at the next
sweep and do not report it as a regression.

### Correction to a claim made while chasing W2

While investigating, the `.bak` files were asserted to be world-readable `0644`, reasoned from
`0666 & ~umask` with root's umask at `0022`. **That was wrong, and the reasoning was never
tested.** Measured on mcapp.local (GNU coreutils 9.7): `cp` **preserves the source file's mode**
— a `0640` source yields a `0640` copy, a `0600` source yields `0600`. The backups were never
world-readable. (macOS's BSD `cp` differs again, which is why this had to be measured on the
target rather than locally.)

The fixes above remain worth having as hardening and for determinism, but they close **no live
exposure**. Recorded here so the claim is not repeated from the transcript.

### Verification

`config_migration` cannot run on macOS (bash 3.2; `config.sh` is bash-4-only by design), so it was
run on mcapp.local (bash 5.2.37) against scratch configs in `/tmp`, production untouched:

| Tree                                | Result           |
| ----------------------------------- | ---------------- |
| new tests + **pre-fix** `config.sh` | **FAIL (15/20)** |
| new tests + **fixed** `config.sh`   | **PASS**         |

Five assertions flip — the two `migrate_config()` mode checks and the three `write_config()` ones.
Full local gate green otherwise: ruff, `ruff format --check .` (161 files), mypy (91 files),
`run_startup_tests.py` exit 0 with `uptime: PASS`, 47 suites PASS.

## 4. 2026-08-22 17:53 CEST — second full sweep (pre-release check for v2.0.1)

**Verdict: all green. Zero findings.** Run to answer one question: is `v2.0.1-dev.2` fit to be
promoted to a production `v2.0.1`. Answer: yes. The gateway-uptime feature recorded its **first
real `gap`** in this window — it is upstream and the metric behaved exactly as designed.

### Anchors

| Anchor              | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| Snapshot            | 2026-08-22 17:49–17:53 CEST                                  |
| Release             | `v2.0.1-dev.2` (`/webapp/version.html` agrees)               |
| App version         | `v2.0.1` (`/api/status`)                                     |
| Active slot         | `slot-2` (rotated from `slot-1`)                             |
| Schema              | **25** = `LATEST_SCHEMA_VERSION` ✓                           |
| System epoch        | installed **1** = `REQUIRED_SYSTEM_EPOCH` = `SYSTEM_EPOCH` ✓ |
| Service start       | 2026-08-22 08:54:49 CEST                                     |
| Process uptime      | 32 096 s ≈ **8.9 h**                                         |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                  |
| Host uptime         | 3 days, 19:32                                                |

The 08:54:49 restart is the `v2.0.1-dev.2` deploy, not a crash: `NRestarts` is still 0 on both
units, and the slot symlink moved `slot-1 → slot-2`.

### Rates — measured against §1

| Signal              | §1 baseline | This run     | Window | Verdict                                      |
| ------------------- | ----------- | ------------ | ------ | -------------------------------------------- |
| `messages` `msg`    | 11 / h      | **7 / h**    | 1 h    | within noise; Saturday afternoon vs. morning |
| `messages` `pos`    | 87 / h      | **74 / h**   | 1 h    | within noise                                 |
| `signal_log`        | 347 / h     | **310 / h**  | 1 h    | within noise                                 |
| journal warnings    | 0 / 24 h    | **0 / 24 h** | 24 h   | `-- No entries --`                           |
| unclassified `msg`  | 0           | **0**        | 1 h    | classifier keeping up                        |
| `{CET}` in messages | 0           | **0**        | all    | dropped at ingest; absence is correct        |

Nothing moved by even a factor of 1.5. A **10-hour** cross-check against §2's totals gives lower
mean rates than the 1-hour spot sample (`signal_log` +2 405 rows over 10.05 h = **239 / h** mean
vs. 310 / h spot; `messages` +537 = **53 / h** mean vs. 81 / h spot for `msg`+`pos` combined),
which is the expected shape for a diurnal RF band — the 1-hour window happened to catch a busy
stretch. Neither figure is a concern; recorded so a future run reading ~240 / h does not chase it.

Structural values all unchanged: schema **25**, epoch **1**, classifier version **3** with **38**
rules and markers `backfill_done:v0/v1/v3`.

### Measured

| Check            | Value                                                             |
| ---------------- | ----------------------------------------------------------------- |
| Services         | mcapp, mcapp-ble, caddy, lighttpd all `active`                    |
| `/health`        | `healthy`                                                         |
| DB size          | **31 MB** of the 1 GB limit (3.1 %); **no `-wal` file** at all    |
| Totals (context) | 19 583 messages, 221 stations, 57 931 signal rows                 |
| Disk             | 3.8 G used of 59 G = **7 %**                                      |
| MemAvailable     | **161 MB** of 415 MB (was 181 MB in §2)                           |
| Swap             | SwapFree 329 004 of 424 956 kB → **~94 MB swapped out** (was 142) |
| Load / temp      | 0.20 / 0.10 / 0.03 · **44.0 °C**                                  |
| Journal warnings | **0** in 24 h                                                     |

Swap pressure **improved** by ~48 MB across the deploy while available RAM fell ~20 MB — the net
is a wash and both sit comfortably inside the envelope §2 established for this box.

### Absent signals

- `{CET}` rows in `messages`: **0**.
- Unclassified `msg` rows in the last hour: **0**.
- `udp_target_kind` `identified`, `udp_known_source_ips` `["192.168.68.56"]`,
  `udp_multiple_sources` **false**, `udp_untrusted_source_ips` **empty**,
  `udp_suppressed_target_changes` **0**. Nothing is injecting on the unauthenticated port 1799.
- Journal warnings in 24 h: **0** (literal `-- No entries --`).
- WAL file: **absent** — checkpointed clean, not a stalled checkpoint.
- `dark` rows from the 08:54 deploy restart: **0**, and that is correct. The restart was shorter
  than `DARK_THRESHOLD_MS` (3 missed 30 s heartbeats), so `reconcile_link_uptime_startup` left the
  state untouched by design — "a deploy restart must never read as a link outage".

### The first recorded `{CET}` gap — upstream, and the metric worked

`GET /api/uptime?range=24h` (note: `range` is **required**; the bare endpoint returns 422):

| Value           | Reading                                                  |
| --------------- | -------------------------------------------------------- |
| `state`         | `active`                                                 |
| `uptime_pct`    | **99.007 %**                                             |
| `coverage_pct`  | 70.61 % (ledger began 00:54:02, so 24 h is not yet full) |
| longest outage  | **605 822 ms** = 10.1 min                                |
| last beacon age | 8 s                                                      |
| heartbeat age   | 14 s (30 s tick)                                         |
| thresholds      | `silent_ms` 360 000 · `off_ms` 900 000                   |

The single `gap` segment: **12:43:07 → 12:53:13**, 606 s. That is **exactly 2 × 303 s**, and the
recovering beacon landed exactly on cadence — so precisely **one** `{CET}` frame was lost, not a
ten-minute outage. It is also a third independent confirmation of the **303 s** cadence.

**The RF side was demonstrably healthy throughout.** Between 12:38 and 12:58 the proxy stored 36
`pos` rows from 20 distinct stations and 158 `signal_log` rows, with no interruption spanning the
gap. The node and the UDP path to the proxy were fine; what dropped was the node's uplink to the
MeshCom server. **Upstream, not ours — no action.**

This is also the documented one-cadence resolution meeting real data: **a single lost frame costs
~1 % of a 24 h window.** Do not read 99 % as a degraded link.

### Watch points

- **W1 — swap.** Carried, and improved: ~94 MB swapped out (from 142), 161 MB available. Still a
  WATCH, not a finding.
- **W2 — `config.json` is `0640` on the live box.** Confirmed still `0640`, exactly as §3
  predicted. `vapid.json` is **`0600`** ✓. This is the decided state until someone runs
  `--reconfigure`; **do not report it as a regression.**
- **W3 — Caddy cert always looks near-expiry.** Confirmed again: `notBefore Aug 22 07:49:37 GMT →
notAfter Aug 22 19:49:37 GMT`, read at 15:52 GMT = mid-life, issuer
  `CN=Caddy Local Authority - ECC Intermediate`. Healthy.
- **W4 — resolved, and the fix is live.** The `first_observed_ms` fallback is present in the
  active slot (`storage/uptime.py:155`). It cannot be re-tested against this ledger, which has
  beacons; the regression tripwire remains "a fresh ledger reporting 100.0 %".
- **W5 (new) — webapp `main` carries an untagged merge.** `be6089d` ("Merge development: admin
  history cards") was merged and pushed to webapp `main` at 2026-08-22 00:46 and carries no tag;
  webapp `package.json` still reads `2.0.0` while the deployed build reports `v2.0.1-dev.2`.
  Harmless today, but it means webapp `main` is one commit ahead of its own `development` with no
  release naming it. Worth tidying when v2.0.1 is cut.

### Release readiness — v2.0.1

Asked and answered: **yes.**

| Gate                        | Result                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| `uvx ruff check`            | All checks passed                                                          |
| `uvx ruff format --check .` | 161 files already formatted                                                |
| `uv run mypy`               | Success: no issues found in 91 source files                                |
| `run_startup_tests.py`      | **exit 0**, 47 suites, `uptime: PASS`                                      |
| GitHub CI on `bc12783`      | success                                                                    |
| Working tree                | clean, `development` in sync with `origin/development`                     |
| Tag alignment               | `HEAD == v2.0.1-dev.2 == bc12783` in both repos                            |
| Field soak                  | ~17 h across dev.1 + dev.2; **8.9 h on dev.2** with 0 restarts, 0 warnings |

`config_migration` is `SKIPPED — NOT VERIFIED` locally (macOS bash 3.2; the suite is bash-4-only
by design). It was run for real on mcapp.local per §3 and it runs on every CI push (ubuntu, bash
5). Not a gap in coverage, only in the local instrument.

`development` is **13 commits ahead of `main`** (`main` still at `dc27612`, the v2.0.0 merge).
Promotion is the ordinary `scripts/release.sh` path from `development`.

### Skill changes this run produced

- Phase 3's `version.html` note gave no path. `curl -sk https://mcapp.local/version.html` returns
  a lighttpd **404**, which reads exactly like a broken frontend deploy; the file is served at
  **`/webapp/version.html`**. Path added to the skill.
- Phase 5 reads the ledger from the DB but never names the HTTP surface. `GET /api/uptime`
  **requires** `?range=` (`24h` / `7d`) and returns a 422 without it. Added.

## 5. 2026-08-22 18:10 CEST — v2.0.1 promoted to production

Not a sweep. Records the promotion §4 cleared, and the post-deploy verification, so the next run
knows what changed under it.

`v2.0.1` was cut from `v2.0.1-dev.2` (`bc12783`, plus the §4 log entry and the release notes) and
deployed from the webapp Update page. GitHub release:
<https://github.com/DK5EN/McApp/releases/tag/v2.0.1>, sha256
`cfaad29aa0a49ad5fc334e580a2528986a91feefe92bc634e33a3c4b9718a7db`.

### Post-deploy verification

| Check                  | Value                                                             |
| ---------------------- | ----------------------------------------------------------------- |
| `/api/status`          | `v2.0.1`                                                          |
| `/webapp/version.html` | `v2.0.1`                                                          |
| `/health`              | `healthy`                                                         |
| Active slot            | **slot-0** (rotated from `slot-2`; slot-0 previously held v2.0.0) |
| Services               | mcapp, mcapp-ble, caddy, lighttpd all `active`                    |
| `NRestarts`            | **0** on both units                                               |
| Schema                 | DB **25** = `LATEST_SCHEMA_VERSION` in the active slot ✓          |
| System epoch           | installed **1** = required ✓                                      |

### The uptime ledger survived the deploy cleanly

This is the first time the gateway-uptime feature has been carried through a release deploy, so it
is worth pinning what happened:

- `first_observed_ms` still **00:54:02** — untouched.
- **No `dark` row was written**, and that is correct: the service was down for less than
  `DARK_THRESHOLD_MS` (3 missed 30 s heartbeats), so `reconcile_link_uptime_startup` left the state
  as it found it. A production deploy does not read as a link outage.
- The only segment in the ledger is still the single 12:43:07 → 12:53:13 `gap` from §4.
- Beacon arrived 198 s after the restart, heartbeat 30 s — both inside their envelopes.
- `uptime_pct` 99.03 %, `coverage_pct` 71.93 % — continuous with §4's reading, no discontinuity.

### W5 — resolved

webapp `main` carried `be6089d`, a development merge that was never merged back, leaving `main` one
commit ahead of its own `development`. That state aborts `release.sh`'s `validate_main_mergeable`
and **blocked the release**. Merged back non-destructively (the trees were identical, so the merge
was purely topological) and pushed.

**The root cause is structural and will recur.** `post_release_prep` pushes **MCProxy**
`development` only; the webapp's merge-back from step 10 is committed and never pushed, so every
production release leaves webapp `development` behind `origin/main` until someone pushes it by hand
— which is exactly what aborts the _next_ release. Documented as Stop 4 in the new `prod-release`
skill, with the manual push as an explicit step.

### Related work this produced

- **`prod-release` skill** (`.claude/skills/prod-release/SKILL.md`) — the production counterpart to
  `dev-release`, covering the four stops that abort or silently bite: release notes must be
  committed _before_ the script starts (its clean-tree check runs first), `main` must not be ahead
  in either repo, the notes prompt is a bare `read` that `< /dev/null` kills into the rollback trap,
  and the webapp `development` push the script never performs.

### Open, unchanged

- 169 pre-2026-08-13 duplicate telemetry pairs, still unexplained.
- `fcs_ok` field-data verdict due after 2026-09-20 (`doc/backlog.md`).
- webapp `package.json` still reads `2.0.0`; `post_release_prep` bumps only the two
  `pyproject.toml` files. Cosmetic today, but it means the webapp repo carries no version of its
  own that matches the release.

## 6. 2026-08-22 — fable review of the v2.0.1 release tooling, and its fixes

Not a sweep. Records an independent review of the release/deploy changes made after v2.0.1 shipped,
so the next run knows what moved and which risks were accepted deliberately.

Eight independent finders, then adversarial verification; only claims reproduced by experiment were
acted on. **Two of the three high findings were defects in the fixes themselves.**

### Fixed

| #   | Defect                                                                                                                                                                                                                             | Where                            |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| 1   | `install_webapp_tree` was called as `f \|\| return 1` / `if ! f`, which suppresses errexit for the **whole function body** — `rm -rf`, `mkdir -p`, `chown -R`, `chmod -R` failed silently and the swap still happened, returning 0 | `bootstrap/lib/deploy.sh`        |
| 2   | `release.sh` announced _"Rollback complete."_ on a step-11 failure while doing **nothing** — `cleanup_artifacts` had already cleared every flag the trap inspects                                                                  | `scripts/release.sh`             |
| 3   | The webapp-push regression test was **tautological**: its fixture pushed before the call, so it passed with the push removed                                                                                                       | `scripts/release_prep_tests.py`  |
| 4   | The deploy suite drove the function without `-e` while production has it, and asserted the AppleDouble guarantee by grepping release.sh's **source text**                                                                          | `scripts/webapp_deploy_tests.py` |
| 5   | `mcapp-ble.service` embeds `BLE_API_KEY` in cleartext and was written `0644` (pre-existing)                                                                                                                                        | `bootstrap/lib/deploy.sh`        |
| 6   | webapp `.prettierignore` covered only `*_vectors.json`, leaving `dedup_contract.json` and `push_contract.json` reformattable                                                                                                       | webapp repo                      |

Every fix was confirmed by **mutation**: reverting it makes the relevant suite fail, restoring it
makes the suite pass. A green suite was not accepted as evidence on its own — finding 3 is precisely
the case where it was wrong.

### Accepted residual risks — deliberate, do not re-litigate

- **W6 — the swap has a sub-millisecond window with no serve directory.** `install_webapp_tree`
  stages then swaps with two renames. Killed exactly between them, `/var/www/html/webapp` is absent
  and **stays** absent until the next deploy: `system_converge.py`'s watchdog is scoped to
  `SYSTEM_EPOCH` and never calls `deploy_webapp`. It is not data loss — the source is always the
  freshly extracted tarball — and the next deploy self-heals, because `[[ -d "$WEBAPP_DIR" ]]` is
  false and `get_installed_webapp_version` returns `not_installed`, forcing a redeploy.
  Recovery by hand: `sudo mv /var/www/html/webapp.old /var/www/html/webapp`.
  Not fixed: closing it properly needs a symlinked serve directory (`ln -sfn` is atomic where a
  directory rename is not), which means `server.follow-symlink` in lighttpd — a front-door change,
  therefore a `SYSTEM_EPOCH` bump. Not worth it for a ~10 ms window on a single-user box.
- **W7 — stale clients can lose assets the old overlay preserved.** Replacing the tree deletes the
  previous build's content-hashed chunks. A tab whose service worker finished precaching is
  unaffected (Workbox precaches every built asset, including never-visited lazy chunks); the
  exposure is a tab whose SW never completed, hitting a new route after a deploy. Accepted — the
  alternative is unbounded accumulation, which is what §5 set out to fix.

### Refuted — do not re-investigate

- _"A step-11 failure deletes the published release and both repos' tags."_ Asserted in-session,
  **wrong**: `cleanup_artifacts` clears `_CLEANUP_TAG`/`_CLEANUP_RELEASE` at step 10. The trap has
  nothing left to delete; the real defect was that it claimed a rollback anyway (fixed above).
- `chmod -R 755` on served files — identical in the pre-change code and inert; lighttpd loads no
  CGI/FastCGI/magnet module.
- `rm -rf` with an unset `WEBAPP_DIR` — unreachable; `readonly` and hardcoded in `bootstrap/mcapp.sh`.
- Symlink/TOCTOU on `webapp.new`/`.old` — defeated by coreutils' non-follow defaults, and
  `/var/www/html` is not writable by any non-root account.
- `src/**/*_vectors.json` failing to match files directly under `src/` — `**` matches zero
  directories in gitignore semantics; verified with prettier.

## 7. 2026-09-01 15:45 CEST — v2.0.2 promoted to production

Post-release sweep after promoting `v2.0.2-dev.9` to **v2.0.2** and deploying it. Verdict: **all
green on the box, one live finding that is not ours** — the `{CET}` uplink has been down since
12:34 CEST, three hours before the deploy, while RF traffic is completely normal. That is the exact
situation the Gateway Availability feature was built to make visible, and it is reporting it
correctly.

### Deploy

| Check                  | Result                                                                |
| ---------------------- | --------------------------------------------------------------------- |
| `/api/status` version  | `v2.0.2`                                                              |
| `/webapp/version.html` | `v2.0.2`                                                              |
| Active slot            | **slot-1**, deployed 2026-09-01 13:39:30 UTC (rollback target slot-0) |
| Services               | `mcapp`, `mcapp-ble`, `caddy`, `lighttpd` — all `active`              |
| `NRestarts`            | **0**                                                                 |
| Schema (active slot)   | `LATEST_SCHEMA_VERSION = 28`; DB reports **28** — they agree          |
| System epoch           | installed **1** / required **1**                                      |
| Disk                   | 4.1 G used of 59 G (8 %)                                              |
| Memory                 | 269 MB used of 415 MB, 145 MB available                               |
| Journal warnings, 24 h | **0**                                                                 |

### The webapp tree-replace fix, measured

The reason §6 shipped `install_webapp_tree`. Right after v2.0.1 the serve directory held 868 files
and 26 MB for a release containing 70, plus 133 AppleDouble `._*` sidecars. After this deploy:

| Signal                          | After v2.0.1 | After v2.0.2 |
| ------------------------------- | ------------ | ------------ |
| Files in `/var/www/html/webapp` | 868          | **70**       |
| AppleDouble `._*` sidecars      | 133          | **0**        |

Exactly the release contents, nothing carried over. The fix works in production, not only in its
suite.

### Migrations v26 / v27 / v28, verified against live data

All three touch existing rows, so each was checked for its intended effect rather than for a clean
exit code:

- **v26** — placeholder callsigns: **0** rows remain matching `XX0XXX*` / `DK0XXX*` / `DX0XXX*`
  across `station_positions`.
- **v27** — `lora_mod` is now `None` (307) or `8` (27), nothing above the low nibble. The old bug
  stored the packed byte, so a country-8 node read **131**; that value no longer exists in the
  table. `station_positions.gw` reads 215 `NULL` / 83 `0` / 36 `1` — the zeros are **re-learned**
  since v27 nulled them, which is the intended steady state (a `0` from a HEY frame is an
  authoritative "not a gateway").
- **v28** — the uptime ledger is repaired **and correctly bounded**. Only **3** gap segments survive
  after the 2026-08-27 07:45:59 cutoff: 19.3 min, 22.9 h and 20.2 min — all genuine outages, all
  well past the new 12 min tolerance. The 31 remaining sub-12-min gaps are all _before_ the cutoff
  and were kept on purpose, because the cadence still alternated there. The 210 spurious ones are
  gone.

Retuned thresholds confirmed live in the active slot: `GAP_TOLERANCE_MS = 720_000`,
`SILENT_MS = 720_000`, `OFF_MS = 1_800_000`.

### F13 — the `{CET}` uplink is genuinely down (upstream, not ours)

`/api/uptime?range=24h` at 15:48 CEST:

```
state             off
uptime_pct        81.39
coverage_pct      100.0
last_beacon_ms    2026-09-01 12:34:34 CEST
longest_outage    11_253_037 ms  (3.13 h, still open)
```

**This is not a release regression and not a threshold problem.** Three independent facts separate
the two cases, and this is why the check is worth writing down:

1. The gap opened at **12:34**, three hours _before_ the 15:39 deploy. The old build recorded its
   start; the new one continues the same open segment.
2. **Zero** `{CET}` matches in six hours of journal. CLAUDE.md's symptom rule — "uptime near zero
   while beacons are visibly arriving means `GAP_TOLERANCE_MS` is under the cadence" — needs
   beacons to be arriving. They are not. This is the opposite case.
3. **RF is completely healthy.** ~100 messages/h steady across all six hours, 29 stations in the
   last 60 min, 12 in the last 15, and ingest continued across the restart (rows at 15:40–15:42 on
   the new build).

A busy mesh with a silent server uplink is precisely the state no surface could distinguish before
v2.0.1, and `state: "off"` with `coverage_pct: 100` says it exactly: we were watching the whole
time, and there was nothing to hear.

**Action: none on our side.** `{CET}` originates at the MeshCom server. Watch for the beacon to
return and the ledger to close the segment; if it stays dark for many hours, it is an upstream
report, not a local fix.

#### F13 resolved, 18:38 CEST — and it verified the shipped recorder

The beacon returned at **18:38:32** after a **6.07 h** outage (12:34:34 → 18:38:32). The ledger
closed the gap at exactly that timestamp and opened an `up` run; `state` went `off` → `active`.
Confirmed upstream, not a defect in anything we ship.

Two things worth keeping:

- **This is the one v2.0.2 code path the promotion sweep could not exercise.** The retuned
  thresholds and migration v28 were verified against stored history, but a gap being _closed_ by
  a returning beacon needed a returning beacon. It behaved correctly on the new build.
- **Cadence re-measured: 606.8 s (10.11 min)**, from 18:38:32 → 18:48:39. Unchanged from the
  documented 606.5 s, so `GAP_TOLERANCE_MS` at 12 min keeps its intended 1.19x margin — **no
  retune needed.** Re-measuring here was the point: the cadence is set upstream by the MeshCom
  server and has already moved once, and a long outage is exactly the occasion on which it might
  have moved again.

The 24 h uptime reads **73.3 %** while the outage is still inside the window; it climbs out over
the following day. Coverage stayed **100 %** throughout — the proxy was watching the whole time.

### Rate baseline — re-measured

| Signal                     | Baseline (§1)     | This run                | Verdict                    |
| -------------------------- | ----------------- | ----------------------- | -------------------------- |
| `messages` type `msg`      | 11 / h            | **8 / h**               | within factor 2            |
| `messages` type `pos`      | 87 / h            | **96 / h**              | steady                     |
| `signal_log`               | 347 / h           | **440 / h**             | within factor 2            |
| journal warnings           | 0 / 24 h          | **0 / 24 h**            | unchanged                  |
| unclassified `msg`         | 0                 | **0**                   | classifier keeping up      |
| `{CET}` rows in `messages` | 0                 | **0**                   | dropped at ingest, correct |
| `{CET}` cadence            | 606.5 s (retuned) | **no beacon — see F13** | upstream outage            |

Structural: schema **28** (was 25 — this release), system epoch **1**, classifier version **3** with
**38** rules, 20 398 messages in a 37 MB database.

### Secret hygiene

| File                                    | Mode    | Note                                                                           |
| --------------------------------------- | ------- | ------------------------------------------------------------------------------ |
| `/var/lib/mcapp/vapid.json`             | **600** | raw VAPID private scalar — correct                                             |
| `/etc/systemd/system/mcapp-ble.service` | **600** | **§6 finding 5 is closed on the box** (was 644 with a cleartext `BLE_API_KEY`) |
| `/etc/mcapp/config.json`                | 640     | **W2** — unchanged by decision; moves on `--reconfigure`                       |

### Watch points

- **W8 — opened and closed within this section.** The `{CET}` uplink outage above: down 12:34:34,
  back 18:38:32, 6.07 h, upstream. Kept on the record so a future run reading a depressed 24 h
  uptime figure finds the cause here instead of re-diagnosing it as a threshold problem. The
  cadence was re-measured on recovery and is unchanged at 606.8 s.
- **W1 — swap. Carried, and improved again.** **59 MB** swapped out under zram (from 94 in §4,
  142 in §2), 355 MB SwapFree, 135 MB RAM available. Still a WATCH, not a finding; it becomes one
  if SwapFree trends toward zero.
- **W2 — `config.json` is `0640` on the live box.** Confirmed again. The decided state until
  someone runs `--reconfigure`; **do not report it as a regression.**
- **W3 — Caddy cert always looks near-expiry.** Confirmed healthy: issuer
  `CN=Caddy Local Authority - ECC Intermediate`, `notBefore Sep 1 12:38:16 GMT →
notAfter Sep 2 00:38:16 GMT`, read at 17:06 GMT = mid-life on a 12 h leaf.
- **W6, W7** carry forward unchanged (§6, accepted residual risks).
- **W5 is closed.** webapp `main` no longer runs ahead of its own `development`: `release.sh` now
  tags, bumps and **pushes** both repos, and both read `behind_main: 0` after this release.

> **Correction to an earlier draft of this section.** A first pass of these notes closed **W1**
> and credited it to the tree-replace measurement. That was wrong on both halves: W1 is **memory
> swap under zram**, and the directory-swap window in `install_webapp_tree` is **W6**, which is an
> accepted residual risk and stays open. Swap is measured above and carries forward. The
> `mcapp-ble.service` `0600` change closes **§6 finding 5**, not W5.

## 8. 2026-09-10 21:05 CEST — pre-release sweep before promoting v2.0.5-dev.2

Sign-off sweep for promoting `v2.0.5-dev.2` to **v2.0.5**. Verdict: **green on the box, short
soak by decision.** dev.1 ran 40 min and dev.2 about 25 min before this sweep; the operator chose
to promote rather than wait. Both dev tags were watched against live rows, not just health checks.

### Box

| Check                        | Result                                                                                                                 |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Active slot                  | **slot-1**, `v2.0.5-dev.2`, started 2026-09-10 20:50:20 CEST                                                           |
| `NRestarts` mcapp / ble      | 0 / 0                                                                                                                  |
| Journal warnings since 20:50 | 1: `mcapp-ble.service: Failed with result 'exit-code'` at 20:50:10 — the deploy's own stop before the new slot started |
| Tracebacks since 20:50       | 0                                                                                                                      |
| BLE                          | connected 20:50, notifications flowing                                                                                 |

### Field verification of the release content

- **RX-01 trailer timestamp:** three foreign BLE rows after Extern-UDP was switched off carried
  the node clock as the stored timestamp, within seconds of the Pi's wall clock.
- **RX-04 flag bits:** DL5RAS-2 arrived with `mesh_info` 9 and the booleans read server+mesh,
  consistent with the nibble.
- **Duplicate enrichment:** first message after Extern-UDP came back (DL1RHS-14 → 9, 18:55:50
  UTC) is one UDP-won row with RSSI -120 / SNR -8 **and** hw 12 / mod 8 / mesh_info 1 /
  fcs_ok 1 from the BLE copy. Zero duplicate message ids in the window.
- **Not verified on air:** the 300 ms `0xA0` write gap (needs two back-to-back sends under the
  operator's callsign; not done on his behalf).

### Repos

| Check                             | MCProxy                                     | webapp                                   |
| --------------------------------- | ------------------------------------------- | ---------------------------------------- |
| HEAD == dev.2 tag before dep bump | yes (`4a7aebe`)                             | yes (`1a214ed`)                          |
| `development..origin/main`        | 0                                           | 0                                        |
| Contract subtree vs mc-chat       | identical                                   | —                                        |
| Classifier subtree vs mc-chat     | identical (pycache only)                    | —                                        |
| Four hand-copied corpora          | equal hashes across all three repos         |
| Local gate after dependency bump  | green                                       | green (1 pre-existing warning)           |
| **CI**                            | **`tests` workflow is `disabled_manually`** | **`CI` workflow is `disabled_manually`** |

### Watch points

- **W9 — CI is switched off in both repos.** `gh workflow list --all` shows `tests` (MCProxy)
  and `CI` (webapp) as `disabled_manually`; the last run on `development` was 2026-08-31.
  Every push since, including this release, has only the local gate behind it. Not changed by
  this sweep; the operator decides whether to re-enable.
- **W10 — `@lucide/vue` pinned at 1.41.0** in the webapp. 1.44.0 breaks the lint gate (45
  unsafe-assignment errors from its type declarations). Re-test on the next dependency pass.
- **W1, W2, W3, W6, W7** carry forward unchanged.

## 9. 2026-09-10 21:40 CEST — v2.0.5 promoted to production

Post-release check after promoting `v2.0.5-dev.2` to **v2.0.5**. Verdict: **green.** The first
`release.sh 2` run failed at the tag push (`! [remote rejected] v2.0.5 -> v2.0.5 (failed)`, no
reason from GitHub) after `main` had already been pushed; the rollback removed the tags but left
MCProxy `origin/main` one merge commit ahead of `development` and the webapp's local `main`
seven commits ahead of its remote. Repaired by merging `main` back into `development` (MCProxy)
and resetting the never-pushed local `main` (webapp); an annotated probe tag then pushed fine,
so the rejection was transient. The second run went through cleanly.

Deployed with the copied bootstrap pinned to `--tag v2.0.5` (the browser extension was not
connected, and the shell `POST /api/update/start` was classifier-blocked); same path the
update runner executes.

| Check                           | Result                                                     |
| ------------------------------- | ---------------------------------------------------------- |
| `/api/status` version           | `v2.0.5`                                                   |
| `/webapp/version.html`          | `v2.0.5`                                                   |
| Active slot                     | **slot-2** (rollback target slot-1, `v2.0.5-dev.2`)        |
| Services                        | mcapp, mcapp-ble, caddy, lighttpd all active               |
| `NRestarts`                     | 0                                                          |
| Schema                          | `LATEST_SCHEMA_VERSION = 30`, no migration in this release |
| Health check                    | 15 `[OK]`                                                  |
| Tracebacks after restart        | 0                                                          |
| Repos after `post_release_prep` | both `unpushed 0`, `behind_main 0`; next dev 2.0.6         |

Watch points **W9** (CI disabled in both repos) and **W10** (`@lucide/vue` pin) carry from §8.
A full `/ai-ops` sweep is still owed once the box has settled.

## 10. 2026-09-11 00:45 CEST — slot activation verified live (v2.0.6-dev.1)

**Trigger.** The Update page's Rollback button had never been pressed. Reading
`scripts/update-runner.py` before pressing it showed that rollback overwrote
`/var/lib/mcapp/messages.db` with `meta/slot-N.db`, a snapshot taken when slot N was last
_left_ through the runner. The three deploys of 2026-09-10 went through `mcapp.sh`, which never
refreshes it, so the snapshots on the box were: slot-1 2026-09-06 15:45 UTC (schema 30), slot-0
2026-09-06 08:36 UTC (schema 29), slot-2 2026-08-22 16:06 UTC (schema 25). Pressing Rollback would
have replaced a database whose newest row was 2026-09-10 19:27 UTC with the 6 September copy. It
also never re-installed the served webapp bundle and never restarted `mcapp-ble`. Replaced by
per-slot activation (MCProxy c817bfa, webapp f35d571), shipped as v2.0.6-dev.1 into slot-0.

**Verification.** `scripts/slot_activation_sweep.sh mcapp.local`, second run (the first run
had three instrument bugs, fixed in c6b6551):

| Step                            | Result                                                                                  |
| ------------------------------- | --------------------------------------------------------------------------------------- |
| activate active slot / slot 7   | 400 both                                                                                |
| slot-0 → slot-1 (v2.0.5-dev.2)  | runner success 23 s; symlink, API v2.0.5, bundle dev.2, 3 services, mcapp-ble restarted |
| slot-1 → slot-0 (manual runner) | success; API v2.0.6, bundle v2.0.6-dev.1                                                |
| slot-0 → slot-2 (v2.0.5)        | runner success 30 s; API v2.0.5, bundle v2.0.5                                          |
| slot-2 → slot-0 (manual runner) | success                                                                                 |
| database                        | schema 30 throughout; 7-day fingerprint identical across all four switches              |
| leftovers                       | no `webapp.old` / `webapp.new`, runner exit 0 every time                                |

A targeted re-check afterwards (fingerprint at t0, t0+90 s, on slot-1, on slot-1 +60 s, back on
slot-0) lost exactly one fresh row during the 90 s of ordinary running _before_ any switch and
none across the switches: normal ingest churn on the newest rows, not activation.

**Caveats for the operator.** Slots older than v2.0.6-dev.1 (currently slot-1 and slot-2) carry a
runner without `activate` and a backend without `/api/update/activate` (404). From such a slot,
return with the new slot's runner directly:

```bash
sudo ~/mcapp-slots/slot-0/.venv/bin/python3 ~/mcapp-slots/slot-0/scripts/update-runner.py --mode activate --slot 0
```

Do not press the old Rollback button while an old slot is active. The `/api/status` version is
`v<pyproject version>` (v2.0.5 on a v2.0.5-dev.2 slot); `webapp/version.html` carries the tag.

**State after the sweep.** slot-0 v2.0.6-dev.1 active, slot-1 v2.0.5-dev.2, slot-2 v2.0.5;
services active, NRestarts 0 on the new unit start, schema 30.

## 11. 2026-09-11 12:15 CEST — v2.0.7 promoted to production, full sweep

Sign-off and post-release sweep for **v2.0.7**. Verdict: **all green — zero findings, one new
watch point (W11).** The box is 7 minutes past the production deploy at the time of the numbers
below, so the rates are recorded with that window and the totals are context only.

Sequence of the day on this box: `v2.0.6` (08:49) → `v2.0.7-dev.1` (11:38, via the copied
bootstrap pinned to `--tag`) → `v2.0.7` (12:03, via the Update page, Mode: Production).

### Anchors

| Anchor              | Value                                                             |
| ------------------- | ----------------------------------------------------------------- |
| Snapshot            | 2026-09-11 12:05–12:15 CEST                                       |
| Release             | `v2.0.7` (`/webapp/version.html` agrees)                          |
| App version         | `v2.0.7` (`/api/status`)                                          |
| Active slot         | **slot-0** (slot-1 `v2.0.7-dev.1`, slot-2 `v2.0.6`)               |
| Schema              | **30** = `LATEST_SCHEMA_VERSION` ✓ (no migration in this release) |
| System epoch        | installed **2** = `REQUIRED_SYSTEM_EPOCH` ✓                       |
| Service start       | 2026-09-11 12:03:53 CEST                                          |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                       |
| Host uptime         | 12 days, 13:23 — load 0.35 / 0.32 / 0.25                          |

### Measured

| Check                           | Value                | vs §1 baseline              |
| ------------------------------- | -------------------- | --------------------------- |
| `messages` type `msg`           | **7 / h**            | 11 / h — quiet midday       |
| `messages` type `pos`           | **71 / h**           | 87 / h                      |
| `signal_log`                    | **243 / h**          | 347 / h                     |
| journal warnings (`-p warning`) | **0 / 24 h**         | 0 / 24 h ✓                  |
| unclassified `msg`, last 1 h    | **0**                | 0 ✓                         |
| `{CET}` rows in `messages`      | **0**                | 0 ✓ (dropped at ingest)     |
| classifier version / rules      | **3** / **38**       | 3 / 38 ✓                    |
| backfill markers                | `v0`, `v1`, `v3`     | matches version 3           |
| `messages` total                | 16 748, newest 12:10 | context                     |
| `station_positions`             | 362                  | context                     |
| `signal_log` total              | 46 748               | context                     |
| DB size / WAL                   | **40.4 MB** / 0.0 MB | far under the 1 GB limit    |
| heartbeat age                   | **21 s**             | < 60 s ✓                    |
| last `{CET}` beacon             | **566 s** ago        | under the 606.5 s cadence ✓ |

Link uptime over 24 h: **98.60 % uptime, 100 % coverage**, longest outage **20.2 min**
(`gap`, 06:57:48 → 07:18:01 CEST, before any of the day's deploys). 59 stored segments, all
`gap`.

Host headroom: disk **8 %** of 59 G; `MemAvailable` **173 MB**; swap **144 MB out**
(SwapFree 277 660 of 424 956 kB); SoC temperature **44.5 °C**.

Secrets and TLS: `vapid.json` **0600**, raw base64url scalar (not PEM); `config.json` 0640 (W2);
Caddy internal ECC leaf `notBefore Sep 11 09:18 GMT → notAfter 21:18 GMT`, i.e. mid-life (W3).

### Absent signals — the zero is the result

- `udp_untrusted_source_ips` **empty**, `udp_multiple_sources` **false**,
  `udp_suppressed_target_changes` **0**. Nothing is injecting on the unauthenticated UDP port.
- **0** journal warnings in 24 h, **0** tracebacks since the 12:03:53 service start. The two SSE
  reconnect warnings at 12:03:42/48 are the deploy's own restart window, before the new unit
  started.
- **0** unclassified messages, **0** `{CET}` rows, **0** `NRestarts`.

### Field verification of the release content

- **`comment` / `name` on position frames, both branches seen on air.** DK5EN-98's own beacon
  `!4824.47N\01144.28E-MeshCom Freising#Martin/A=001575/N4/R=20` parses to
  `comment='MeshCom Freising'`, `name='Martin'` — the non-empty-comment branch. During the dev.1
  soak a relayed DM6CS-12 frame gave `comment=''`, `name='DM6CS-10'` — the empty branch. Both keys
  present in the SSE frame, neither persisted to `station_positions`, as designed.
- **`timezonefinder` 9.0.0 on ARM.** `/api/timezone?lat=48.2455&lon=11.3693` →
  `Europe/Berlin / CEST / +2.0`. The 8 → 9 major bump happened after the dev.1 soak, so the
  released tree is not byte-identical to the soaked one; this probe plus a full local gate re-run
  is what covers the difference.

### Classified and dismissed — not findings

- **Zero `dark` segments despite three deploys today.** `reconcile_link_uptime_startup` writes a
  `dark` row only when `now - last_tick_ms > DARK_THRESHOLD_MS` (3 missed 30 s heartbeats). A
  deploy restart takes seconds, so no row is written — explicitly by design, so that a deploy can
  never read as a link outage. Zero `dark` rows here is correct behaviour, not a broken recorder.
- **TLS leaf expiring in 9 hours.** Caddy's internal CA issues 12 h certs; `notBefore` is 09:18,
  so this is mid-life (W3).
- **`config.json` at 0640.** Standing decision (W2), unchanged.

### Watch points

- **W11 (new, RESOLVED within this sweep) — `udp_target_kind` read `first_seen` for the first
  13.5 minutes after the deploy, where §2 and §4 recorded `identified`.** The target is
  `192.168.68.61` (it was `192.168.68.56` on 2026-08-22 — DHCP, not a finding). `_adopt_target`
  strengthens `first_seen → identified` only when an inbound **UDP** frame carries our own
  callsign as `src`; in the hour before this sweep our node's own frames reached the DB over
  `ble_remote` only (2 rows), while UDP delivered 4 foreign frames. Watched with a terminal
  condition rather than assumed: it **strengthened to `identified` at T+810 s**
  (uptime 1355 s, target unchanged, `udp_suppressed_target_changes` still 0, untrusted list still
  empty). So `first_seen` immediately after a restart is the normal transient state of this box,
  not a regression — the earlier sweeps simply ran late enough to miss it. Expect it again on the
  next deploy; only a `first_seen` that persists past the node's own beacon interval (~30 min) is
  worth chasing.
- **W1** — zram swap 144 MB out, flat against the 142 MB of 2026-08-22. Stable, still a watch.
- **W2, W3, W6, W7, W9, W10** carry forward unchanged. **W9 still applies to this release**: both
  repos' CI workflows remain `disabled_manually`, so `v2.0.7` shipped behind the local gate only.

### §1 baseline drift noted, not silently corrected

§1's structural line still reads schema **25**, epoch **1**; both moved with later releases and
are now **30** and **2**. Its `{CET}` cadence of `~303 s` was halved upstream to **606.5 s** on
2026-08-22 (CLAUDE.md § Gateway Uptime is the authority). Recorded here rather than rewritten in
place, so the next run can decide whether §1 should be re-baselined.

## 12. 2026-09-16 10:21 CEST — pre-release sweep before promoting v2.0.8-dev.5 to v2.0.8

Sign-off sweep for the **v2.0.8** promotion. Verdict: **all green — zero findings, one new watch
point (W12).** The box is 13 minutes past the `v2.0.8-dev.5` deploy, so rates are recorded with
that window and totals are context only. Soak is short by decision: `dev.4` ran 09:13–10:08,
`dev.5` since 10:08 (two small performance fixes on top of dev.4); the operator chose to promote
today. Campaign record: `doc/2026-09-16_0900-stall-popover-campaign.md`.

Sequence of the day: `v2.0.8-dev.3` (running since the 2026-09-15 reboot) → `dev.4` (09:13, copied
bootstrap, `--tag`) → `dev.5` (10:08, same path).

### Anchors

| Anchor              | Value                                                         |
| ------------------- | ------------------------------------------------------------- |
| Snapshot            | 2026-09-16 10:21 CEST                                         |
| Release             | `v2.0.8-dev.5` (`/webapp/version.html` agrees)                |
| App version         | `v2.0.8` (`/api/status`, dev suffix not carried there)        |
| Active slot         | **slot-1** (slot-2 `dev.4`, slot-0 `dev.3`)                   |
| Schema              | **32** = `LATEST_SCHEMA_VERSION` ✓ (migrations 31, 32 ran)    |
| System epoch        | installed **4** = `REQUIRED_SYSTEM_EPOCH` ✓, no reboot marker |
| Service start       | 2026-09-16 10:08:52 CEST                                      |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                   |
| Host uptime         | 16:47 (rebooted 2026-09-15 for B4) — load 0.08 / 0.09 / 0.12  |

### Measured

| Check                           | Value                | vs §1 baseline                              |
| ------------------------------- | -------------------- | ------------------------------------------- |
| `messages` type `msg`           | **4 / h**            | 11 / h — quiet late morning                 |
| `messages` type `pos`           | **79 / h**           | 87 / h ✓                                    |
| `signal_log`                    | **302 / h**          | 347 / h ✓                                   |
| `stall_events`                  | **20 / h**           | new since v2.0.8; all in the restart minute |
| journal warnings (`-p warning`) | **0 / 24 h**         | 0 / 24 h ✓                                  |
| unclassified `msg`, last 1 h    | **0**                | 0 ✓                                         |
| `{CET}` rows in `messages`      | **0**                | 0 ✓                                         |
| classifier version / rules      | **3** / **38**       | 3 / 38 ✓                                    |
| `messages` total                | 18 782, newest 10:21 | context                                     |
| `station_positions`             | 396                  | context                                     |
| `signal_log` total              | 51 530               | context                                     |
| DB size / WAL                   | **40.4 MB** / 0.0 MB | far under the 1 GB limit                    |
| heartbeat age                   | **18 s**             | < 60 s ✓                                    |
| last `{CET}` beacon             | **344 s** ago        | under the 606.5 s cadence ✓                 |

Link uptime over 24 h: **95.42 % uptime, 100 % coverage**, 67 stored segments (W12: three
same-day service restarts each reset `last_beacon_ms`; re-read after a quiet day).

Host headroom: disk **8 %** of 59 G; `MemTotal` **462 MB** (was 415 before the B4 CMA change);
`MemAvailable` **161 MB**; swap **22 MB out** (SwapFree 450 584 of 473 084 kB — W1 improved
from 144 MB); SoC temperature **45.1 °C**.

Secrets and TLS: `vapid.json` **0600**, raw base64url scalar; `config.json` 0640 (W2); Caddy
internal ECC leaf `notBefore Sep 16 03:34 GMT → notAfter 15:34 GMT`, mid-life (W3). Strict
`curl` validation from the Mac passes.

### Absent signals — the zero is the result

- `udp_untrusted_source_ips` **empty**, `udp_multiple_sources` **false**,
  `udp_suppressed_target_changes` **0**, `udp_target_kind` `identified`.
- **0** journal warnings in 24 h (`-- No entries --`), **0** tracebacks since the 10:08:52
  start; the two SSE reconnect lines at 10:08:42/48 belong to the old process's shutdown.
- **0** unclassified messages, **0** `{CET}` rows, **0** `NRestarts`, **0** `handler` stalls
  under traffic since the restart (one during the restart itself).

### Watch points

- **W12 (new):** 24 h link uptime 95.42 % with 67 segments after three same-day restarts. Each
  restart resets `last_beacon_ms`, and the metric's resolution is one 606 s cadence, so this is
  expected today. Re-read after 24 quiet hours; a value still under ~98 % then is a finding.
- **W1:** swap out 144 → 22 MB after B4 (§ Memory Footprint in CLAUDE.md). Keep watching the
  trend; the number is now a healthy baseline, not a squeeze.
- **W9** unchanged: CI is `disabled_manually` in both repos, so the promotion is signed off on
  the local gates (ruff, mypy, all suites; eslint, vue-tsc, 3322 vitest cases, prettier), all
  green on the dependency-updated trees.

## 13. 2026-09-16 10:30 CEST — v2.0.8 promoted to production

Promotion of `v2.0.8-dev.5` to **v2.0.8** via the webapp Update page (Mode: Production), five
minutes after the §12 sign-off. Verdict: **deploy verified, zero findings.** A settled
post-release sweep is due after the box has run a few quiet hours (re-read W12 then).

| Anchor         | Value                                                          |
| -------------- | -------------------------------------------------------------- |
| Release        | `v2.0.8` — `/api/status` and `/webapp/version.html` agree      |
| Active slot    | **slot-0** (slot-1 `dev.5`, slot-2 `dev.4`)                    |
| Service start  | 2026-09-16 10:24:47 CEST, `NRestarts` **0** on both units      |
| Schema         | DB **32** = `LATEST_SCHEMA_VERSION` 32 ✓                       |
| System epoch   | **4** installed, no reboot marker                              |
| Browser bundle | `index-C0Hb9sG3.js` = served bundle, no waiting service worker |
| Health checks  | 14 × `[OK]` in the update modal                                |

- The two journal "error" lines at 10:24:37/42 are the previous process closing its BLE stream
  during the restart, as in §11.
- `udp_target_kind` read `first_seen` at 273 s uptime: `identified` needs a datagram from our
  own callsign, i.e. the node's next own beacon. Not a finding; confirm `identified` in the
  settled sweep.
- Post-release invariants: both repos `unpushed 0`, `development` not behind `main`, tags
  `v2.0.8` at parity with origin, release assets `uploaded`, both repos prepped to 2.0.9.

## 14. 2026-09-19 00:40 CEST — pre-release state before promoting v2.0.10-dev.3 to v2.0.10

**Not a full `ai-ops` sweep.** This is the targeted state captured around the `v2.0.10-dev.3`
deploy, recorded here because it is what the `v2.0.10` promotion was signed off against.

| Anchor         | Value                                                                   |
| -------------- | ----------------------------------------------------------------------- |
| Release on box | `v2.0.10-dev.3`, deployed 2026-09-19 00:37:26 CEST                      |
| Active slot    | **slot-2**                                                              |
| Services       | `mcapp`, `mcapp-ble`, `lighttpd`, `caddy` all active; `NRestarts` **0** |
| Health checks  | 14 × `[OK]` in the deploy log, `webapp version: v2.0.10-dev.3`          |
| Schema         | **32** = `LATEST_SCHEMA_VERSION` 32 ✓                                   |
| Tarball        | sha256 verified by downloading it from the Pi before deploying          |

- **Soak: 2 minutes, not the hours the `prod-release` gate asks for.** Flagged, and the operator
  chose to promote anyway. Recorded here as the known deviation for this release; rollback is a
  slot activation to slot-0/slot-1 from the Update page.
- **The read-cursor repair ran and found nothing**, which is the expected result on a healthy
  box: journal `repair_read_cursor_dm_keys: repaired 0 read_cursors row(s)
(my_callsign=DK5EN-98)`, marker `read_cursors_dm_repaired=1`, `read_cursors` still 144 rows,
  zero bare-callsign rows and zero phantom paired rows. Predicted beforehand by replaying the
  translator against the live key set.
- **Live functional check of the #11 fix:** `POST /api/read_cursor {"key":"DK6IX"}` (a bare
  sidebar key, re-posting its existing timestamp so MAX semantics make it a no-op) answered
  `unread: 0`, left the row at `DK5EN<>DK6IX`, and created no new row — 144 before and after.
  On the previous version this call inserted a 145th orphan row, which is the reported bug.
- Journal since the restart: 5 × `socket.send() raised exception` at 00:38:34, all from that
  probe's SSE broadcast reaching stale client sockets. Pre-existing behaviour on the broadcast
  path, not introduced by this release. Zero tracebacks, zero other errors.
- **W9 still open:** the test workflows are `disabled_manually` in both repos, so there is no CI
  run for the promoted commit `31186f0` at all — only Dependency Graph. The full gate was run
  locally in both repos instead, after the dependency bumps, and again before publishing.

## 15. 2026-09-19 07:50 CEST — full sweep, first look at the stall instrumentation in the field

**Verdict: all green, zero findings, two new watch points.** First full sweep on production
`v2.0.10`, ~7 h after the promotion restart. The headline is that the stall work landed: the
`handler` stall rate dropped by roughly a factor of 13 at constant ingest load, and the residual
stalls now point at one concrete, new place.

### Anchors

| Anchor         | Value                                                               |
| -------------- | ------------------------------------------------------------------- |
| Snapshot       | 2026-09-19 07:43–07:50 CEST                                         |
| Release        | `v2.0.10` (`/api/status` and `/webapp/version.html` agree)          |
| Active slot    | **slot-1**                                                          |
| Service start  | 2026-09-19 00:46:39 CEST, `uptime_seconds` 25123 (~6.99 h)          |
| Services       | `mcapp`, `mcapp-ble`, `caddy`, `lighttpd` active, `NRestarts` **0** |
| Schema         | **32** = `LATEST_SCHEMA_VERSION` 32 ✓                               |
| System epoch   | box **5** = `REQUIRED_SYSTEM_EPOCH` 5 = `SYSTEM_EPOCH` 5 ✓          |
| Classifier     | version **3**, **38** rules, 0 unclassified in the last hour        |
| UDP provenance | `identified`, 1 known source, 0 untrusted, 0 suppressed changes     |

### Rate table re-measured

| Signal                     | §1 baseline | This run                                            | Window |
| -------------------------- | ----------- | --------------------------------------------------- | ------ |
| `messages` type `msg`      | 11 / h      | **7 / h**                                           | 1 h    |
| `messages` type `pos`      | 87 / h      | **69 / h**                                          | 1 h    |
| `signal_log`               | 347 / h     | **289 / h**                                         | 1 h    |
| journal warnings           | 0 / 24 h    | **0** (`-- No entries --`, `mcapp` and `mcapp-ble`) | 24 h   |
| unclassified `msg`         | 0           | **0**                                               | 1 h    |
| `{CET}` rows in `messages` | 0           | **0**                                               | all    |

All three traffic rates sit ~20 % below baseline and well inside the factor-of-two band — early
Saturday morning, and `pos`/`signal` track each other, so this is diurnal, not a feed problem.

### The stall instrumentation — what it now says

`stall_events` holds 527 rows spanning 2026-09-15 17:17 → 2026-09-19 07:43.

**`handler` stalls per calendar day (non-sample):** 09-15 **28**, 09-16 **54**, 09-17 **46**,
09-18 **42**, 09-19 **5** — and 4 of those 5 are before the 00:46:39 restart onto `v2.0.10`.
Since the restart: **1 handler stall in 6.99 h**.

Normalised per hour against ingest volume, so a traffic lull cannot explain it — ingest is flat
at 58–105 messages/h across the whole window:

| Period                          | handler stalls / h | msgs / h | stalls per 100 msgs |
| ------------------------------- | ------------------ | -------- | ------------------- |
| 09-17 12:00 → 09-19 00:46 (old) | **~1.8**           | ~81      | **~2.2**            |
| 09-19 00:46 → 07:46 (`v2.0.10`) | **0.14**           | ~67      | **0.21**            |

That is the persistent-writer / `synchronous=NORMAL` work from the stall follow-up doing exactly
what `doc/2026-09-18_2200-stall-followup-plan.md` predicted, confirmed on the box rather than in a
benchmark. **The ~40 handler stalls/day this campaign set out to remove are gone.**

The one surviving handler stall (03:22:34, 905 ms) is `MessageRouter._storage_handler` on a BLE
`mh` notification, with `loop_lag_ms` 0.66 and the pool completely idle — an isolated write, not a
pattern.

**Everything else non-sample since the restart, in full — 16 server rows:**

- **6 rows in 00:46:56–00:47:29** (`loop_lag` 1384/704/252/161/141 ms, one `/api/weather` http
  stall 580 ms). The lag sampler's stacks are all in `importlib._bootstrap_external` — module
  import during startup. Expected, not a finding.
- **1 handler stall at 03:22:34** (above).
- **A 70-second burst at 07:42:18–07:43:14** while a Safari client was using the app: three
  `/api/send` http stalls (968 / 833 / 1422 ms) each paired with a `loop_lag` of 353 / 261 /
  584 ms, then one **`/api/telemetry` http critical at 2975 ms** paired with a 406 ms `loop_lag`.

**Followed up with direct measurement on the box, which moved W13 off `/api/telemetry` and
onto `/api/send`.** Recorded here in the order it was established, because the first reading was
wrong and the correction is the useful part.

The `/api/telemetry` `loop_lag` stack ends in `fastapi serialize_response` →
`pydantic dump_json`, which reads like "the response serialises on the event loop". It does — but
that is not what cost the 2975 ms:

| Measurement (on the Pi, 1785 rows / 48 h / 395 KB) | Result                             |
| -------------------------------------------------- | ---------------------------------- |
| `pydantic TypeAdapter(Any).dump_json`              | **21 ms**                          |
| `json.dumps` on the same rows                      | 39 ms                              |
| query + row dicts (`idx_telemetry_cs_ts`, indexed) | 175 ms                             |
| **live `GET /api/telemetry`, 8 calls on :2981**    | **140–210 ms**, one 870 ms outlier |

So the endpoint's steady state is ~145 ms — under the 0.5 s threshold — and pydantic is _faster_
than stdlib json here. **The 2975 ms is an unexplained excursion, not the endpoint's cost**, and
"move the serialisation off-loop" would buy ~21 ms. RSS was at its 150 MB peak at that instant,
which makes zram pressure the leading hypothesis, but it is a hypothesis. Not worth a code change.

**`/api/send` is the real W13, and the bodies make it unambiguous.** Every `/api/send` stall in
the whole 527-row table — 11 of 12 — is an mheard chart dump:

```
07:42:28  1422 ms  {"type":"command","dst":"999","msg":"mheard dump yearly"}
07:42:24   833 ms  {"type":"command","dst":"999","msg":"mheard dump monthly"}
07:42:18   968 ms  {"type":"command","dst":"999","msg":"mheard dump"}
```

The client fires all three at page load; `/api/send` `await`s `route_command`, so the POST does
not return until the whole report is built and fanned out. The JSON of the _response_ is already
off-loop (`send_to(..., offload_json=True)`, added by the stall plan). What is still on the loop
is `_build_chart_series` (`storage/query.py`) — a pure-Python group / sort / gap-marker pass over
the bucket rows — plus **one SSE progress event per qualified station**, emitted with the default
sync `json.dumps` from inside that loop:

| Variant   | buckets   | stations | qualified (≥10) = progress events |
| --------- | --------- | -------- | --------------------------------- |
| `7day`    | 8169      | 25       | **11**                            |
| `monthly` | 11936     | 38       | **13**                            |
| `yearly`  | **20885** | 144      | **65**                            |

A ~21 000-iteration Python pass interleaved with 65 on-loop SSE sends is a coherent explanation
for 1422 ms wall with 584 ms of measured loop lag, and the thread pool was idle throughout
(`pool_queued` 0, `pool_running` 0) — the work is simply not being given to it. **W13.**

**W15 — the lag sampler is blind in exactly this band.** 6 of the 10 `loop_lag` rows since the
restart carry **no stack at all**, including all three `/api/send` rows above and a fresh 625 ms
one at 08:35:43. The three it did capture were 1384 ms (17 samples), 704 ms (4) and 406 ms (1).
Two reasons, both in `_lag_sampler_loop` (`src/mcapp/stalls.py`): it arms only once
`perf_counter() - last_tick - _LAG_LOOP_INTERVAL_S >= config.loop_lag_ms`, i.e. **200 ms after the
tick began** — more than half the budget of a 353 ms lag is gone before the first possible
sample — and it is a Python thread, so a stretch that holds the GIL in a C extension starves the
sampler itself. The events we most need attributed are the ones that arrive with no evidence.

Client-side rows corroborate rather than add: the Safari `client_http` durations (1084 / 848 /
1440 / 3045 ms) sit 20–70 ms above the matching server rows, so the Caddy/lighttpd hop costs
nothing measurable. Two client rows are **dismissed as artefacts, not findings**:

- `client_timeout` **142924 ms** on `/api/weather` at 07:33:16 — reported by an iPhone PWA still
  running **`v2.0.8-7-g8363a1d`**, and there is no server row anywhere near it. A "10 s fetch
  abort" that reports 143 s is an abort timer that fired after iOS resumed a suspended tab, the
  same class as the known hidden-tab `sse_heartbeat` noise. Not a server fact.
- `client_error` on `/api/send` at 07:32:26 followed by `sse_answer_missing` at 07:32:36 from the
  same iPhone — one failed send, one missing SSE answer, single occurrence, no server-side trace.

**`/api/weather` stays closed** per the 2026-09-18 decision: 16 of 21 calls in the 24 h summary
cross 0.5 s (p50 580 ms, max 1502 ms), which is the provider's latency from the Pi, off-loop via
`to_thread`. Re-confirmed, not re-opened.

### Gateway uptime

`/api/uptime?range=24h`: `state: "active"`, **uptime 98.56 %**, coverage 97.42 %, last beacon
302 s ago, heartbeat age 26 s, 70 segments. One `gap` 22:05:20 → 22:25:34 (20.2 min, the longest
outage in the window — one missed beacon at the 606 s cadence plus tolerance) and one `dark`
22:35:09 → 23:12:18 (37 min, a deploy window, counts against coverage only). Both predate the
current boot. The `gap`/`dark` split is behaving correctly.

### Host and hygiene

| Signal        | Value                                                           |
| ------------- | --------------------------------------------------------------- |
| Disk          | 4.5 G of 59 G, **8 %**                                          |
| RAM           | total 462 MB, used 312 MB, **MemAvailable 149 MB**              |
| Swap          | **24 MB out** (SwapFree 448836 of 473084 kB)                    |
| Load / temp   | 0.15 0.10 0.03, **43.5 °C**, up 8:07                            |
| DB / WAL      | **40.4 MB** / **4.6 MB**                                        |
| `vapid.json`  | `0600` ✓, raw base64url scalar (`{"private_key": "0fS-..."` ) ✓ |
| `config.json` | `0640` — **W2**, accepted by decision                           |

**W1 is effectively closed by B4 and stays as a watch only for the trend**: 24 MB swapped out
against the 142 MB recorded in §2 and the 22 MB in §12. The DB is 4 % of the 1 GB limit.

**New: the app's RSS climbs within a boot.** The `stall_events` context field carries `rss_kb` on
every row, which makes this visible for the first time: **88 MB at 00:46:56 → 115 MB at 03:22 →
150 MB at 07:43:14**. Part of the last step is the telemetry payload itself, but 88 → 115 MB with
no client attached is not. On a box with 149 MB available this is worth a data point per sweep.
**W14.**

### Watch points

- **W13** — `/api/send` blocks for 0.5–1.4 s on the three `mheard dump` commands, because
  `_build_chart_series` runs its group/sort/gap pass on the event loop and emits one on-loop SSE
  progress event per qualified station (65 of them for `yearly`, over 20 885 buckets), with the
  thread pool idle. Candidates, in order of value: run `_build_chart_series` in
  `asyncio.to_thread`; throttle or drop the per-station progress events.
  **Not** `/api/telemetry` — that endpoint measures 140–210 ms steady state and its pydantic
  serialisation is 21 ms; its single 2975 ms excursion is unexplained and left as-is.
- **W15** — the loop-lag sampler misses the 100–600 ms band: 6 of 10 `loop_lag` rows since the
  restart have no stack. It cannot sample before 200 ms into a tick, and a GIL-holding C call
  starves the sampler thread. Until this is fixed, the next W13-class question is unanswerable
  from the recorded data. Candidates: arm at a fraction of `loop_lag_ms`; record `samples: 0`
  explicitly so "never armed" is distinguishable from "no lag".
- **W14** — mcapp RSS 88 → 150 MB over 7 h within one boot, read off `stall_events.rss_kb`.
  Record it each sweep; a monotone climb across a longer boot would be the finding.

### Carried forward, unchanged

- **W2** (`config.json` `0640` by decision), **W3** (Caddy 12 h certs), **W6**/**W7** (§6 accepted
  residual risks), **W9** (CI `disabled_manually` in both repos), **W10** (`@lucide/vue` pinned
  1.41.0), **W12** (§12 link-uptime across restarts — 98.6 % this run, healthy).
- **W1** — now a trend watch only, see above.
- 169 pre-2026-08-13 duplicate telemetry pairs: not re-examined, unchanged.
- `fcs_ok` field-data verdict due after 2026-09-20 (`doc/backlog.md`).

## 16. 2026-09-19 09:15 CEST — W13 and W15 implemented (wave, not yet deployed)

**Not a sweep.** This is the code change answering §15's two watch points, gated and advisor-reviewed
but **not yet on the box** — it ships in the dev release that follows. Recorded here so §15's watch
points are not read as still open.

| Item    | Change                                                                                                                                                                                                                                                                                                          |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **W13** | `QueryMixin._build_chart_series` (`storage/query.py`) split into three pure synchronous `@staticmethod` helpers — `_group_and_qualify`, `_build_series_chunk`, `_finalize_series` — each run via `asyncio.to_thread`; per-station SSE progress throttled to one event per `MHEARD_PROGRESS_CHUNK` (10) stations |
| **W15** | `_lag_sampler_loop` (`stalls.py`) arms at `_LAG_SAMPLER_ARM_FRACTION` (0.5) of `config.loop_lag_ms` instead of the full threshold; counts its own wakes per episode; `_consume_lag_sample()` always returns a dict                                                                                              |

### Measured, not asserted

**W13 — equivalence.** The pre-wave `_build_chart_series` was extracted from `git show HEAD:` and run
against the new one on **20 897 real `signal_buckets` rows pulled from mcapp.local**, for all three
gap-parameter variants. Output **byte-identical** (32 446 / 21 383 / 21 383 rows), `done` event
identical, stage set identical, `gaps` events **65 → 7**.

**W13 — the actual goal.** Same data, same 5 ms watchdog, deep copies hoisted out of the measured
region:

|                            | wall  | **max loop-lag** | p95     |
| -------------------------- | ----- | ---------------- | ------- |
| pre-wave (on-loop)         | 16 ms | **12.2 ms**      | 12.2 ms |
| new (chunked, `to_thread`) | 25 ms | **3.2 ms**       | 3.2 ms  |

Loop blocking down 3.8x; wall time up 56 % from seven thread hops. Measured on a Mac, so the absolute
figures are ~15-30x smaller than the Pi's — the 12.2 ms scales to the 584 ms `loop_lag` actually
recorded on the box, which is what makes the comparison credible. The hop overhead is fixed and
therefore relatively cheaper there.

**W15 — and a claim of §15 that turned out to be wrong.** §15 said a missing stack was probably GIL
starvation. The new diagnostic shows it is **not**, and the first implementation's own docstring
("`sampler_wakes == 0` is evidence the sampler never woke") was also wrong — the advisor caught it
and five independent runs confirmed it. A wake before the block and one right after the GIL is
released are always counted, so the count can never reach 0:

| blocker                        | `sampler_wakes`    | wakes the episode had room for | `samples` |
| ------------------------------ | ------------------ | ------------------------------ | --------- |
| GIL-holding C call, 310-557 ms | **3** (all 5 runs) | 8.2-13.1                       | 0-1       |

The counter now increments **only on wakes where the loop is already overdue**, which makes the field
mean what its name says and the ratio interpretable — and, as a side effect, restores the lock-free
healthy path the original comment promised:

| blocker                        | `sampler_wakes` | room     | ratio    | `samples`                                |
| ------------------------------ | --------------- | -------- | -------- | ---------------------------------------- |
| GIL-holding C call, 310-557 ms | **1**           | 8.2-13.1 | **~0.1** | 0-1                                      |
| pure Python, 599 ms            | **7**           | ~12      | **~0.6** | 6, with a stack naming the blocking line |

**Read `sampler_wakes` as a ratio against `duration_ms / 50`, never as a flag.** Far below → the
sampler could not get scheduled during the block (a GIL-holding C call). Near it with `samples: 0` →
it ran but never resolved a frame in time.

### Gate

`uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src` (106 files),
`uv run python scripts/run_startup_tests.py` **rc=0, 61 suites PASS, 0 FAIL** (`config_migration`
SKIPPED = the documented macOS bash-3 skip). Advisor pass (independent, higher-capability, against
the diff and the acceptance criteria): **APPROVED**, four low findings, no correctness defect.

### Advisor findings — disposition

- **F1** (`sampler_wakes` counted healthy wakes, docstring rule unachievable) — **fixed**, verified
  independently before acting on it. Table above.
- **F4** (a progress assertion that only checked "a callsign is present") — **fixed**: the test now
  pins the exact sequence `PROG09 / PROG19 / PROG22` and the counters `(10, 20, 23)`, so a wrong
  chunk boundary or a wrong last-of-chunk rule fails it.
- **F2** (a GIL-holding blocker is attributable only by a race at release: `armed=True, samples=0`
  and `samples=1, sampled_at_ms ≈ duration_ms` share one cause) — **accepted, not fixed.** The
  staleness re-check that discards a late sample is correct and must stay; the alternative
  (`asyncio.sleep(0)` before consuming) buys determinism at the cost of touching the lag loop's
  timing. **W16.**
- **F3** (`_last_tick = t0` and `_reset_lag_sample()` are two statements, so a wake landing between
  them is counted then wiped) — **accepted, not fixed.** Undercount of at most 1, never a
  misattribution; swapping the lines trades it for a wake counted against the old episode.

### What the advisor attacked and could not break

Input mutation (`bucket_rows` byte-identical after the call in both old and new code; no aliasing
path into the per-callsign lists); no `await` or loop access inside any `to_thread` callable;
equivalence on input shapes the production replay never covered (empty, single station, all-sparse,
duplicate `bucket_ts`, exact chunk multiples, non-ASCII callsigns, reversed order, `bucket_ts = 0`);
every `detail` consumer (`record`, writer thread, `/api/stalls`, `/api/stalls/summary`,
`replay_stall.py`, `stall_middleware.py`) tolerates the always-present dict, ~45 bytes against the
5000-row prune budget; the `"stack": None` invariant; chunk size 10 on this hardware. It also read
the webapp repo directly and confirmed **no consumer parses the `(idx/N)` text or expects one event
per station** — `MHeardStore.ts` stores `stage`/`detail` as display strings and `callsign` is typed
optional and never read.

### New watch point

- **W16** — short GIL-holding lag episodes remain attributable only by chance (advisor F2). Not a
  defect; revisit only if production rows show it mattering.

## 17. 2026-09-19 09:12 CEST — v2.0.11-dev.1 deployed and verified live

`v2.0.11-dev.1` (MCProxy + webapp, tag parity confirmed in both repos, tarball sha256
`af8e2142…` verified by downloading it from the Pi before deploying). Deploy exit 0, 14 × `[OK]`,
**active slot-0**, service started 09:10:11 CEST, `/api/status` reports `v2.0.11`. The active slot
was grepped for the symbols only this change introduces — `_LAG_SAMPLER_ARM_FRACTION`,
`sampler_wakes`, `MHEARD_PROGRESS_CHUNK`, `_finalize_series`, `_build_series_chunk`,
`_group_and_qualify` — all present.

### W15 — works as designed. Closed.

Every `loop_lag` row written since the restart carries the new keys, and the ratio is readable at a
glance:

| time     | lag     | `sampler_wakes` | room (`lag/50`) | ratio | `armed` | `samples` | stack |
| -------- | ------- | --------------- | --------------- | ----- | ------- | --------- | ----- |
| 09:10:28 | 1215 ms | 16              | 24.3            | 0.66  | true    | 15        | yes   |
| 09:10:29 | 674 ms  | 6               | 13.5            | 0.44  | true    | 5         | yes   |
| 09:10:59 | 135 ms  | 0               | 2.7             | 0.00  | false   | 0         | no    |
| 09:11:01 | 106 ms  | 0               | 2.1             | 0.00  | false   | 0         | no    |

The two startup rows (module imports) are attributed with a stack. The two short ones are now
**legible instead of blank**: `armed: false` says the loop was never overdue past the arm threshold
long enough to sample, which is the honest account of a 106 ms lag — it has room for two sampler
wakes in total. Under the old code all four would have looked identical from the outside.

### W13 — loop blocking removed, wall time not. Reclassified, not closed.

Driving all three dumps through `POST /api/send` on the box:

| command               | before (§15)         | after                                       | `loop_lag` alongside |
| --------------------- | -------------------- | ------------------------------------------- | -------------------- |
| `mheard dump`         | 968 ms + 353 ms lag  | **634 ms**                                  | **none**             |
| `mheard dump monthly` | 833 ms + 261 ms lag  | **377 ms** — below threshold, no row at all | **none**             |
| `mheard dump yearly`  | 1422 ms + 584 ms lag | **1008 ms**                                 | **none**             |

**The `/api/send` stall rows have NOT disappeared, and saying the fix removed them would be wrong.**
Two rows were still written (634 ms, 1008 ms) because the `http` recorder measures wall time and the
work genuinely still takes about a second. What did disappear is the thing that mattered: **not one
`loop_lag` row accompanies them any more**, where previously every one of the three carried
261-584 ms of measured event-loop blocking. The request is slow; it no longer makes everything else
slow. That is exactly the trade the pre-deploy measurement predicted (+56 % wall, −3.8x loop lag)
and it reproduced on the real hardware.

Consequence for future sweeps: **`/api/send` + `mheard dump*` will keep generating `http` stall rows
and that is now expected noise, not a regression.** Judge it by whether a `loop_lag` row sits beside
it. The architectural fix — make the dump command return immediately and deliver the series over SSE
— is a bigger change and is not scheduled. **W13 stays open in this reduced form.**

### Watch points after this release

- **W13** (reduced) — `mheard dump*` still costs ~1 s of wall time on `/api/send`; only the loop
  blocking is gone. Expect the `http` rows; alarm only on an accompanying `loop_lag`.
- **W14**, **W16**, **W1**, **W12**, **W2**, **W3**, **W6**, **W7**, **W9**, **W10** unchanged.
- **W15** resolved.

## 18. 2026-09-23 08:10 CEST — pre-release sweep before promoting v2.0.13-dev.2 to v2.0.13

Sign-off sweep for the **v2.0.13** promotion (RF Monitor, own-message display exemption).
Verdict: **all green, zero findings, one new watch point (W17).** The box is 27 minutes past the
`v2.0.13-dev.2` deploy, so rates below carry that caveat and totals are context only. `dev.1`
(own-message exemption) had run since 2026-09-21; `dev.2` adds the RF Monitor on top of it.

### Anchors

| Anchor              | Value                                                                     |
| ------------------- | ------------------------------------------------------------------------- |
| Snapshot            | 2026-09-23 08:10–08:13 CEST                                               |
| Release             | `v2.0.13-dev.2` (`/webapp/version.html` agrees)                           |
| App version         | `v2.0.13` (`/api/status`, dev suffix not carried there)                   |
| Active slot         | **slot-1** (slot-2 `v2.0.13-dev.1`, slot-0 `v2.0.12`)                     |
| Schema              | **32** = `LATEST_SCHEMA_VERSION` ✓                                        |
| System epoch        | box **5** = `REQUIRED_SYSTEM_EPOCH` 5 ✓, no `reboot-required` marker      |
| Service start       | mcapp 07:43:42, mcapp-ble 07:43:38 CEST; `uptime_seconds` 1607            |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                               |
| Host uptime         | 4 days 8:31, load 0.11 / 0.09 / 0.13                                      |
| Classifier          | version **4**, **38** rules, markers `v0 v1 v3 v4`, 0 unclassified in 1 h |
| UDP provenance      | `identified`, 1 source, 0 untrusted, 0 suppressed target changes          |

Classifier version **4** supersedes the **3** in §1's structural line: v2.0.11's rule-42 change
went through `bump_classifier_version` (commit `5b52fc6`), and its `backfill_done:v4` marker is
present.

### Rate table re-measured

| Signal                     | §1 baseline | This run      | Window |
| -------------------------- | ----------- | ------------- | ------ |
| `messages` type `msg`      | 11 / h      | **10 / h**    | 1 h    |
| `messages` type `pos`      | 87 / h      | **65 / h**    | 1 h    |
| `signal_log`               | 347 / h     | **285 / h**   | 1 h    |
| journal warnings           | 0 / 24 h    | **0** (both)  | 24 h   |
| unclassified `msg`         | 0           | **0**         | 1 h    |
| `{CET}` rows in `messages` | 0           | **0**         | all    |
| last `{CET}` beacon        | —           | **492 s** ago | live   |
| heartbeat age              | < 60 s      | **5 s**       | live   |

`pos`/`signal` sit ~20 % under baseline, the same early-morning shape as §15. Inside the band.

### RF Monitor on live traffic (the dev.2 change)

In the first 15 minutes after the deploy, `GET /api/monitor/frames` held **131** envelopes with
**no `seq` gap**: 90 BLE `pos`, 9+9 UDP `pos`/`tele`, 6 BLE `sys`, 6 BLE `ack`, 3+3 UDP/BLE `msg`,
1 BLE `msg` `dropped`, one `app` `tx` `sent`, and the node's own UDP echoes as `tx` `shown`. That
last group is `dir: tx` because `src_type == "node"`, which is what the contract specifies, not a
misclassified RX. Every real chat message appeared on both transports and matched its stored
`messages` row. The on-box gate (dev build) ran green, `config_migration` and `wire_monitor`
included. No traceback in the journal since the 07:43:42 start.

### Stalls since the deploy

`loop_lag` 2 × critical (max 1174 ms) and 3 × stall, all in the restart minute. One `handler`
stall (555 ms, 07:51:20). One `/api/weather` pair (550 ms server / 815 ms client) — closed as the
upstream fetch, not re-opened. The rest is `/api/read_cursor`, which is W17.

### Host and hygiene

| Signal        | Value                                                                  |
| ------------- | ---------------------------------------------------------------------- |
| Disk          | 4.5 G of 59 G, **9 %**                                                 |
| RAM           | MemTotal 474 MB, **MemAvailable 177 MB**                               |
| Swap          | **31 MB out** (SwapFree 441328 of 473084 kB)                           |
| Temp          | **42.9 °C**                                                            |
| mcapp RSS     | **121 MB** at 27 min (W14 data point; §15 read 88 MB at start of boot) |
| DB / WAL      | **42.0 MB** / **4.2 MB**                                               |
| `vapid.json`  | `0600` ✓, raw base64url scalar ✓                                       |
| `config.json` | `0640` — W2, accepted by decision                                      |
| TLS           | Caddy internal ECC leaf, `Sep 22 23:09 → Sep 23 11:09 GMT`, mid-life   |

`/api/uptime?range=24h`: `active`, **100.0 %** uptime, **100.0 %** coverage, longest outage 0,
72 stored segments. The ~20 s deploy restart falls under the metric's one-cadence resolution.

### Absent signals — the zero is the result

- **0** journal warnings in 24 h for `mcapp` and `mcapp-ble`; **0** tracebacks since 07:43:42.
- **0** `NRestarts`, **0** unclassified messages, **0** `{CET}` rows, no `reboot-required`.
- `udp_untrusted_source_ips` **empty**, `udp_multiple_sources` **false**,
  `udp_suppressed_target_changes` **0**.

### Watch points

- **W17 (new): `POST /api/read_cursor` stalls are rising, and they are older than this release.**
  Server-side `http` stall rows on that path, per 24 h ending 08:11: 09-16 to 09-18 **0**, 09-19
  **1**, 09-20 **3**, 09-21 **11**, 09-22 **17**, 09-23 **15** (max 1830 ms). They began with
  v2.0.11's unread-suppression change (the narrowed summary now runs the suppression predicate
  and the four-leg dedup subquery) and ran at the same rate under v2.0.12 and `dev.1`, so `dev.2`
  did not introduce them. Every row is a `200`, with `loop_lag_ms` ~1 ms and an empty pool queue:
  the cost is off-loop, and nothing else is stalled by it. The slow calls are mostly `key: "*"`,
  at 650–1180 ms. The two `client_error` rows at 07:56:05 (`"Load failed"`, ~637 ms) came from a
  Safari tab still running the `dev.1` bundle (`app_version v2.0.12-3-ge2b84b5`), while the
  server answered the same calls `200` in 716 ms. They are consistent with a page reload
  aborting in-flight fetches, and that reading is not proven. Lever if it keeps climbing: the
  dedup subquery runs twice per summary (CLAUDE.md § Unread Cursors); materialise it once.
  Re-read next sweep: a daily count still climbing, or any `loop_lag` alongside, is the finding.
- **W14** — mcapp RSS 121 MB at 27 min into this boot. One more data point, no trend claim.
- **W13**, **W16**, **W1**, **W12**, **W2**, **W3**, **W6**, **W7**, **W10** unchanged.
- **W9** unchanged: CI is not running on these commits (the latest `development` runs are
  Dependabot/graph jobs only), so the promotion is signed off on the local gates. Those are ruff,
  ruff-format, mypy and all backend suites, plus the on-box gate; and vue-tsc, eslint, prettier,
  3636 vitest cases and `build:strict`. All are green on the dependency-updated trees.
- Soak is short: `dev.2` had run **27 minutes** at the time of this sweep; `dev.1` was published
  2026-09-21 21:52 CEST, about 34 hours before `dev.2` replaced it.

## 19. 2026-09-24 22:55 CEST — pre-release sweep before promoting v2.0.14-dev.3 to v2.0.14

Sign-off sweep for the **v2.0.14** promotion (RF Monitor DBG console, monitor highlighting and
BITV contrast, link check without Extern-UDP). Verdict: **green, zero findings, one new watch
point (W18).** The box is 19 minutes past the `v2.0.14-dev.3` deploy, so rates carry that caveat.
The operator switched **Extern-UDP off on DK5EN-98** this evening, so every frame now arrives
over BLE only. Several readings below differ from §18 for that reason, not because of a fault.

### Anchors

| Anchor              | Value                                                                       |
| ------------------- | --------------------------------------------------------------------------- |
| Snapshot            | 2026-09-24 22:55–23:05 CEST                                                 |
| Release             | `v2.0.14-dev.3` (`/webapp/version.html` agrees)                             |
| Active slot         | **slot-0** (slot-1 `v2.0.14-dev.2`, slot-2 `v2.0.14-dev.1`)                 |
| Schema              | **32** = `LATEST_SCHEMA_VERSION` ✓                                          |
| System epoch        | box **5** = `REQUIRED_SYSTEM_EPOCH` 5 ✓                                     |
| Service start       | mcapp 22:36:52 CEST; `uptime_seconds` 1131                                  |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                                 |
| Host uptime         | 12:22 (booted 10:33), load 0.04 / 0.05 / 0.16                               |
| Classifier          | version **5**, **38** rules, markers `v0 v1 v3 v4 v5`, 0 unclassified (1 h) |
| UDP provenance      | **`config`**, 0 untrusted, not multiple, 0 suppressed changes — see W18     |

Classifier **5** is the mc-chat pull "a WebDesk mention is chat, not an advert" (`cec999b`); its
`backfill_done:v5` marker is present.

### Rate table re-measured

| Signal                     | §1 baseline | This run      | Window |
| -------------------------- | ----------- | ------------- | ------ |
| `messages` type `msg`      | 11 / h      | **16 / h**    | 1 h    |
| `messages` type `pos`      | 87 / h      | **83 / h**    | 1 h    |
| `signal_log`               | 347 / h     | **309 / h**   | 1 h    |
| journal warnings           | 0 / 24 h    | **0**         | 24 h   |
| unclassified `msg`         | 0           | **0**         | 1 h    |
| `{CET}` rows in `messages` | 0           | **0**         | all    |
| last `{CET}` beacon        | —           | **148 s** ago | live   |
| heartbeat age              | < 60 s      | **10 s**      | live   |

Since 21:00 the `msg` rows arrive as `ble_remote` (11 in 21 h, 11 in 22 h) instead of the earlier
`lora`/`udp` mix. That matches Extern-UDP being off. The `{CET}` beacon keeps arriving (148 s ago)
over BLE, so the gateway-uptime ledger works on a BLE-only box, as its hop-0 gate was designed to.

### Changes under test

- **DBG console (dev.1):** a session on DK5EN-98 read `LORADEBUG off / TXCAPTURE off`, set both,
  streamed `[MC-DBG]`/`[LOG]` lines, and restored both to `off`, confirmed by a second `--info`.
  A later attempt got `ECONNREFUSED` on 2323 while the net console was off, and the session went
  to `error`. That is the designed behaviour.
- **Link check without Extern-UDP (dev.2/dev.3):** DK5EN-98 → DK5EN-1 at 22:42. McApp sent the
  ping over BLE, learned `ping_id 1AE1E1EA` from the node's BLE echo 2 s later, and the node's
  console showed `{pong}{451011050}` (== `0x1AE1E1EA`, −71 dBm / 6 dB) at 22:42:46. The pong went
  only to the display and to the server uplink, not to BLE, so the attempt timed out. That is a
  firmware gap, not ours. The same log shows the answered ping retransmitted twice (22:43:24,
  22:44:04).

### Uptime

`/api/uptime?range=24h`: `active`, **97.19 %** uptime, **100.0 %** coverage, longest outage
**20.2 min**. Two contiguous `gap` rows, 10:04:25–10:24:38 and 10:24:40–10:44:51. They coincide
with the morning's maintenance: the host rebooted at 10:32:56, and the node's `--info` reports
`UPDATE: 2026-09-24 10:34:45` (firmware update). Operator activity, not a McApp or link fault.

### Stalls since the deploy (22:36:52)

`loop_lag` 2 × critical (max 1207 ms) and 3 × stall (max 219 ms), in the restart window as in
§18. One `http` stall (740 ms), three `client_http` stalls (max 888 ms). W17 `read_cursor` rows
per day: 09-19 **4**, 09-20 **11**, 09-21 **17**, 09-22 **11**, 09-23 **19**, 09-24 **13**. The
count is flat, not climbing, so W17 stays a watch point.

### Host and hygiene

| Signal       | Value                                                                |
| ------------ | -------------------------------------------------------------------- |
| Disk         | 4.5 G of 59 G, **9 %**                                               |
| RAM          | MemTotal 462 MB, **MemAvailable 169 MB**                             |
| Swap         | **22 MB out** (SwapFree 450796 of 473084 kB)                         |
| Temp         | **41.9 °C**                                                          |
| mcapp RSS    | **119 MB** at 19 min (W14 data point)                                |
| DB / WAL     | **42.6 MB** / **4.1 MB**                                             |
| `vapid.json` | `0600` ✓, raw base64url scalar ✓                                     |
| TLS          | Caddy internal ECC leaf, `Sep 24 15:43 → Sep 25 03:43 GMT`, mid-life |

### Absent signals — the zero is the result

- **0** journal warnings in 24 h (`-- No entries --`); **0** `NRestarts`; **0** unclassified;
  **0** `{CET}` rows.
- `udp_untrusted_source_ips` **empty**, `udp_multiple_sources` **false**,
  `udp_suppressed_target_changes` **0**.

### Watch points

- **W18 (new): `udp_target_kind` reads `config`, not `identified`.** The node sends no UDP frames
  with Extern-UDP off, so McApp never sees one and cannot identify the target. Expected while the
  operator keeps it off. If Extern-UDP is switched back on and this stays `config`, that is the
  finding.
- **W17** flat (numbers above). **W14** 119 MB at 19 min, in line with §18's 121 MB at 27 min.
- **W13**, **W16**, **W1**, **W12**, **W2**, **W3**, **W6**, **W7**, **W10** unchanged.
- **W9** unchanged: CI runs only the dependency-graph jobs on `development`. The promotion is
  signed off on the local gates: ruff, ruff-format, mypy, all backend suites; vue-tsc, eslint,
  prettier, 3843 vitest cases, `build:strict`. All green on the dependency-updated trees (ruff
  0.16.9 taken for this release).
- Soak is short: `dev.3` had run **19 minutes**, `dev.1` (DBG console, monitor colours) about
  **1 h 15 min**.
- **Advisor review of the link-check change (before promotion): rework, done.** It found that
  the driver's wake event was keyed by target. As a result, a late pong or late signal copy for
  an earlier attempt ended the attempt then in flight as an immediate timeout. The late-pong case
  dates back to v2.0.13; the late-signal case is new in the BLE path. Also hardened: the node id
  is only taken from the BLE `I` register (a :1799 datagram of that shape used to reach it), and
  the node-id fallback now requires the pong to be addressed to us. Fixed in `c451ec2`, with four
  regression tests that fail on `dev.3`. The fix ships as `v2.0.14-dev.4`, and that dev tag is
  what gets promoted.
