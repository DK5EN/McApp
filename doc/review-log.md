# Review log

Decisions, fixes, verification runs and open owner questions for `doc/`. A reader who finds a wrong step
logs it here the same day (or fixes the document). Newest entries first within each section.

## Verification run 2026-11-10

**Scope:** every file in `doc/` (122 at the start), the documents referenced by `CLAUDE.md`, and the code
facts they state. **Method:** file-by-file triage (status, date, what the code shows); code checks with
`grep`, `sed` and `git log`; path checks for every `src/`, `scripts/`, `bootstrap/` and `ble_service/` reference;
migration-chain and epoch comparison; reading of the operations and architecture references against the code.

### Findings

| # | Finding | Evidence | Action | Status |
|---|---------|----------|--------|--------|
| F1 | Database reference says schema v32 / v35 / "v25" in three places. Code: `LATEST_SCHEMA_VERSION = 42`. Tables v29 to v42 missing. | `storage/constants.py`, `storage/migrations.py` | Rewritten: current v42, table list v29 to v42, indexes | fixed |
| F2 | Architecture and dataflow describe FastAPI on :2981 and lighttpd on :80 as the front door. Code: `weblite` on Starlette, Caddy on :80/:443, lighttpd backend on :8082. | `pyproject.toml` dependency comment, `src/mcapp/weblite.py`, `bootstrap/templates/caddy/` | Corrected in `reference/architecture-reference.md` and `reference/dataflow.md`; ports table added | fixed |
| F3 | Operations reference: `SYSTEM_EPOCH=1` (code: 10); `FINAL_SCHEMA_VERSION` (removed from code); SSE "0.0.0.0:2981" (code: `127.0.0.1`); keys `SSE_ENABLED`, `SSE_HOST`, `BLE_REMOTE_URL`, `BLE_DEVICE_NAME`, `BLE_DEVICE_ADDRESS`, `MAX_STORAGE_SIZE_MB`, `UDP_PORT_list/send` do not exist in code. | `bootstrap/mcapp.sh`, `src/mcapp/config_loader.py` | Configuration moved to `reference/configuration.md`, corrected; original kept in archive | fixed |
| F4 | BLE service start command `uvicorn src.main:app` from `ble_service/`. The unit file runs `ble_service.src.main:app` from the repo root. | `ble_service/mcapp-ble.service` line 16 | Corrected in RB-008 | fixed |
| F5 | `ssl-tunnel-setup.sh` documented as `scripts/`. It lives in `bootstrap/`. | `ls bootstrap/` | Corrected in SOP-003, RB-011. Backlog B20 confirmed, stale-docs item closed | fixed (B20 open) |
| F6 | Backlog B18 (full run ignores failed apt phase) is fixed in code. | `bootstrap/mcapp.sh` line 843: `if ! install_packages; then ... exit 1` | Moved to `archive/2026-11-10-backlog-closed.md` | closed |
| F7 | Backlog B20 `TEMPLATE_DIR=${SCRIPT_DIR}/bootstrap/templates`, but `SCRIPT_DIR` is already `bootstrap/`. Still broken. | `bootstrap/ssl-tunnel-setup.sh` line 25 and 34 | Stays open | open |
| F8 | Backlog B21: `bootstrap/templates/nftables.conf` is not referenced anywhere. | `grep -rn templates/nftables.conf` returns nothing | Stays open | open |
| F9 | Backlog B15 (drop lighttpd): lighttpd still installed, in the template, and in both health checks. | `bootstrap/lib/health.sh`, `scripts/update-runner.py` | Stays open; health-check alignment noted in RB-004 | open |
| F10 | Plans with stale status: internet feed "nothing built" (feed is in code, migration v36); mention push "not started" (push contract v11 has `mentions`); macOS BLE design (bleak backend exists). | `storage/internet_feed_state.py`, `push_contract.json`, `ble_service/src/ble_backend.py` | Status-check notes added to those plans | annotated |
| F11 | Two identical copies of `2026-10-04_1842-webapp-ack-matcher-issue.md` (root and archive). | `diff` returned no output | Root copy removed; archive copy kept | fixed |
| F12 | 159 files referenced moved documents by path: code comments, error text, `CLAUDE.md`, scripts, skills. | `scripts/` search, `git diff --stat` | Path-only rewrite (`doc/<old>` to `doc/<new>`, relative links inside `doc/`). No behaviour change intended | fixed; see Checks |
| F13 | `bootstrap/lib/packages.sh` log message quotes a document path. Text changed to the archive path. | `packages.sh` line 231 | Same text, new path. Bootstrap suites run in Checks | fixed |
| F14 | `release-history.md` holds only v2.1.26. Older history is in `archive/release-history-full.md`. `release_notes_tests.py` pins this layout. | `scripts/release_notes_tests.py` | Left as it is (the layout is pinned) | noted |
| F15 | `websocket_message` is subscribed in `sse_handler.py` but no publisher was found. | `grep -rn websocket_message src/mcapp` | Noted in architecture reference; candidate for removal | open |
| F16 | Webapp documents link to `MCProxy/archive/operations-reference-pre-split-2026-11-10.md` (`docs/node-admin-FE.md`, `docs/implementation-send-failed.md`). | `webapp/docs/` | Redirect page kept at the old path. Webapp repo not edited | open (cross-repo, Q5) |
| F17 | Dependency list in architecture reference was from before the FastAPI and pywebpush removal. | `pyproject.toml` | Rewritten, with the reasons | fixed |
| F18 | `runbook/RB-011-tls-remote-access-setup.md` (German user guide) pictures show lighttpd on :80 as the front door. | RB-011 text vs ADR-0022 | Banner added, rewrite open | open (Q6) |
| F19 | The epoch parity rule (`SYSTEM_EPOCH` in `mcapp.sh` equals `REQUIRED_SYSTEM_EPOCH`) and the schema rule were only in `CLAUDE.md`, with no verify step in any operator document. | `CLAUDE.md` "System Epoch", "Schema Migrations" | POL-005 §3 verify commands added | fixed |

### Changes made in this run

- **New structure** (see `README.md`): `adr/` (22 decisions), `policy/` (7), `sop/` (6), `runbook/` (11), `reference/`,
  `plans/` (open work), `archive/` (closed work, with `archive/INDEX.md`).
- **116 documents moved** with `git mv` (history kept). 1 duplicate deleted. The old operations reference is archived, and
  `archive/operations-reference-pre-split-2026-11-10.md` is now a redirect.
- **Path references updated** in code comments, messages, `CLAUDE.md`, scripts and skills (159 files). Only the path
  changed. `release-history.md` and `archive/release-history-full.md` stay where `release_notes_tests.py` expects them.
- **Created:** 22 ADRs (5 converted from the old ADR format, originals in `archive/`; 17 new, sourced from `CLAUDE.md` and
  the plans), 7 policies, 3 SOPs (SOP-001, 005, 006), 9 runbooks extracted from the operations reference and corrected,
  `reference/configuration.md`, `reference/firewall.md`.
- **Corrected:** `reference/architecture-reference.md`, `reference/database-reference.md`, `reference/dataflow.md`,
  `backlog.md` (B18 closed, B20 docs item closed, verification note), status notes on four plans.

### Checks run

Results are recorded at the end of this section once the run completed (see "Check results").

### Not verified in this run (candidates for the next run)

- `tech-debt.md` (dated 2026-07-06, not checked line by line)
- `ops-mcapp-health-log.md` (3342 lines, a living log; only its header was read)
- `reference/a0-commands.md` (1002 lines, firmware command reference)
- `reference/telemetry.md`, `reference/MeshCom-ACK.md`, `reference/ble-state-machine.md`, `reference/ble-pin.md`,
  `reference/tls-architecture.md` (read for structure only)
- `reference/version-logic.md` (spot-checked: `pyproject.toml` version and tag format match)
- `reference/McApp-Monitor-Help.html` (user help, not checked against the webapp)
- `sop/SOP-002`, `sop/SOP-004`, `runbook/RB-010`, `runbook/RB-011` bodies (moved, metadata added, text not re-verified)
- The `plans/` folder beyond the four annotated plans

### Open questions for the owner

- **Q1.** RESOLVED 2026-11-10: ADRs, policies and SOPs approved by the owner. Runbooks remain DRAFT.
- **Q2.** RESOLVED 2026-11-10: ADR-0016 (homegrown test runner) approved as recorded.
- **Q3.** `plans/charts-wrong.md` (2026-06): "root cause not proven". Still open, or close?
- **Q4.** `plans/2026-09-19_1200-mention-push-plan.md`: the mention part shipped (contract v11). Is the ALL/TIME key work still wanted?
- **Q5.** Update the two webapp documents that point at `MCProxy/archive/operations-reference-pre-split-2026-11-10.md` to the new runbooks, then remove the redirect.
- **Q6.** DONE 2026-11-10: RB-011 rewritten in English against the script and templates. Two defects are documented in its section 2 (B20 template path; firewall edits ignore the operator's file). Not yet run on a box.
- **Q7.** Procedures that do not exist yet: disaster recovery from an off-host backup (POL-006 §4 refers to it), incident handling, and a PIN-pairing procedure for the BLE node (`reference/ble-pin.md` is the reference only).
- **Q8.** B19 (lighttpd binds beyond loopback): the shipped template binds `127.0.0.1`, so the field report is not reproduced from the template. Close, or test on a box?
- **Q9.** POL-004 §1.2 and §1.7 quote rules from `~/.claude/skills/pi-ops/SKILL.md` and `~/homeautomation/rpizero/...`, which are outside this repo. Confirm these are the source of truth.
- **Q10.** The German documents (`ssl-tunnel`, `charts-wrong`, `neue-firmware`, `WebIF-alternative`, `2026-02-21` analysis, and the Winlink plans) keep their language. Confirm that is acceptable for the new layout.

### Gaps (procedures the handbook pattern would expect, not in the repo)

- Disaster recovery from an off-host backup (see Q7)
- Incident triage and communication (no SOP; only `RB-001` and `SOP-006`)
- Firmware upgrade of the paired node (the remote flash runbook covers the SD card, not the node)

---

## Follow-up 2026-11-10: owner approvals, webapp links, subagent cleanup

**Done**

- ADRs, policies and SOPs approved by the owner (status `REVIEWED`). Q1 and Q2 resolved.
- RB-011 rewritten in English against `bootstrap/ssl-tunnel-setup.sh` and the templates. German original kept in `archive/ssl-tunnel-german-2026-11-10.md`. Still DRAFT and not run on a box.
- Webapp: `docs/node-admin-FE.md` points to RB-006, `docs/implementation-send-failed.md` points to `reference/configuration.md`. The NAT section that the webapp links to was missing after the split and is restored in `reference/configuration.md` (section 7). Webapp changes are uncommitted.
- Haiku workers verified and corrected these files. Each carries a `Verifiziert 2026-11-10` block:
  - `tech-debt.md`: stale function names, line counts and locations corrected. Two migration functions, 161 and 130 lines, were not re-judged.
  - `ops-mcapp-health-log.md`: only a verification block and one header fix. Nothing deleted.
  - `reference/a0-commands.md`: length bytes, case, ranges (`--txpower`, `--txbw`, `--txsf`) and SN/SN1/SE/S1/S2/I fields corrected against the firmware source. `--compress`, `--softser test`, `--softser xml` not found in code.
  - `reference/telemetry.md`, `MeshCom-ACK.md`, `ble-state-machine.md`, `ble-pin.md`, `tls-architecture.md`, `version-logic.md`: corrected or annotated. In `ble-pin.md` one of three Python snippets was replaced by prose (the startup code block), and the webapp section now records the built state.

**Open risks (found while verifying, not fixed)**

- **R1. The update runner listens on all interfaces.** `scripts/update-runner.py:1294` binds `0.0.0.0:2985` (noqa: "LAN service binds all interfaces by design"). The Caddy templates expect `127.0.0.1:2985`. The firewall opens 2985 for all traffic (`bootstrap/templates/nftables.conf`, RB-005 "Allowed Ports"). On public-TLS boxes the runner is therefore reachable from anywhere the router forwards to. Decide: bind loopback, or restrict the firewall rule. Owner decision needed.
- **R2.** `ssl-tunnel-setup.sh` header still says lighttpd on 8082 is required. The current templates do not need it. Header only; the preflight does not check it.
- **R3.** `useVersionCheck.ts` (webapp) raises a dev-install alert when a newer stable exists. Logic not reviewed here.
- **R4.** `ops-mcapp-health-log.md`: the code epoch is 10 (`bootstrap/mcapp.sh:49`, `REQUIRED_SYSTEM_EPOCH` in `src/mcapp/system_converge.py:53`), the log's §59 records 9 (live reading 2026-11-08 14:34, not dated against the bump). The §1 header now carries the code values (epoch 10, schema 42) and the §59 note. The structural block (~line 101 onward, schema 25 / epoch 1) is still stale. F14 is partly fixed in code (fail-closed in `update-runner.py`), still open: no `--tag` is passed (`update-runner.py:455`). W10 is stale (the webapp lock resolves 1.70.0). W22 was resolved in §24 but still listed as open in the header.
- **R5.** `a0-commands.md` is verified against firmware source 4.40a (`MeshCom-Firmware-DEV-Main`, SOURCE_VERSION 4.40a, 2026-11-10). Its header keeps the document version **4.35k** on purpose; the source lines refer to 4.40a. Still open: the firmware sends `BPIN` (BLE PIN in clear text) in `I`, which the backend does not read; the backend does not know `IS2`; the semantics of NOPMOTHER, CHGLED and UV/WIND are not verified; `--compress`, `--softser test` and `--softser xml` are not in 4.40a.
- **R6.** `ble-pin.md`: the PIN is also the BlueZ pairing passkey (`MeshComPairingAgent`). The earlier text covered only the hello. Corrected in the file.
- **R7 (tech-debt).** `_message_handler` is about twice the length the refactoring entry records. Whether the target still applies is an owner question.

**Still not verified:** the bodies of `SOP-002` to `SOP-004` and of `RB-010`. Firmware-side claims in `ble-pin.md` sections 2-6 (firmware `bf05f9de`, not in this repo), `reference/McApp-Monitor-Help.html`.

## Check results

Run 2026-11-10, after the first set of edits (repeated after the follow-up edits, see the line below):

| Check | Command | Result |
|-------|---------|--------|
| Markdown links inside `doc/` | script: every markdown link target resolved on disk | 0 broken (was 1: RB-005, fixed) |
| `doc/` paths named by code, scripts and `CLAUDE.md` | script: 212 references | all resolve, except 3 to `2026-10-20_classifier-unification.md`, which lives in mc-chat (intentional) |
| Lint | `uvx ruff check` | passed after fixing 7 line-length errors that the longer `archive/` paths caused (comments and docstrings only) |
| Format | `uvx ruff format --check .` | 409 files already formatted |
| Gate, MCProxy scope | `./scripts/gate.sh mcproxy` | `GATE: PASS` (ruff check, ruff format, mypy, startup tests in 190 s) |

**Bug found and fixed during the checks:** the first path rewrite did not catch references followed by a sentence
period (`doc/….md.`). The rewrite pattern now allows a trailing period. The pattern change was applied again and is
idempotent. The 12 references it fixed were the ones the path check had flagged.

**Not run:** the webapp gate (the webapp repo was not touched, but it links to `operations-reference.md`, see F16),
and any box-side check. Nothing was deployed.

