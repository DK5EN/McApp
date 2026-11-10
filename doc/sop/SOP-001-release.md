# SOP-001: Release a dev pre-release or a production build

| Field | Value |
|---|---|
| ID | SOP-001 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 (critical: review after every change to `release.sh`) |
| Applies to (hosts) | `mcapp` (production target); the build runs on the development Mac |
| Applies to (variants) | `debian-13-trixie` (mcapp); the build itself is variant-neutral |
| Sources | `scripts/release.sh` (header), `scripts/gate.sh` (header and steps), `CLAUDE.md` ("Development Commands", "Code Quality", "Schema Migrations", "Node Admin"), `../reference/version-logic.md`, `doc/backlog.md` B22 and B23, `doc/release-history.md` |

## 1. Purpose and scope

Builds one combined tarball (backend and webapp) and publishes it as a dev pre-release or a production
release. Governs the tag, the branch, the gate and the record. Does **not** cover the deploy to the box;
that is `../runbook/RB-004-update-runner-and-slots.md`.

- Applies when: a change is ready for the box, or a production release is due.
- Does **not** apply when: only a local test is needed, or the change is docs-only (commit normally, the
  gate's ruff format check still applies, POL-001).

## 2. Variant notes

- Build: n/a per variant. The tarball is the same for every box.
- Box update: see RB-004. The Update page can drive it; B22 says the runner path is not yet proven.

## 3. Inputs

- Access: the development Mac, with `gh` authenticated for `DK5EN/McApp`.
- Branch: `development` for both repos. `main` only through the production path of `release.sh`.
- Sibling repo: `../webapp` checked out next to MCProxy (`scripts/release.sh` reads `../webapp`).
- Preconditions:
  - Both working trees clean.
  - The change is committed with the format `[type] description` (`CLAUDE.md`).
  - `pyproject.toml` carries the next target version, bare (for example `2.1.28`) (`../reference/version-logic.md` §1).

## 4. Steps

1. **Gate.** Run `./scripts/gate.sh` and read the exit code directly. Expect `GATE: PASS`. On `FAIL`, fix the named
   step and rerun; never release past a failed step.
2. **Choose the channel.** Run `./scripts/release.sh` from `development` and pick:
   - *Dev pre-release:* tags `vX.Y.Z-dev.N` on `development`, builds, publishes a pre-release.
   - *Production:* merges to `main`, tags `vX.Y.Z`, builds, publishes, and prepares the next version on `development`.
3. **Production only: group labels.** The production release runs `scripts/refresh_group_labels.py` (step 2b of the
   prod-release procedure, `CLAUDE.md` "Group Labels"). Its snapshot ships in the tarball.
4. **Production only: classifier contract.** `release.sh` ships `classifier_contract.json` explicitly (`CLAUDE.md`
   "Classifier"). Check it is in the tarball list.
5. **Release notes.** `release.sh` asks you to add a `## vX.Y.Z (date)` section at the **top** of
   `doc/release-history.md` and waits for Enter. Older sections move to `doc/archive/release-history-full.md`
   (the prod-release skill describes it). `scripts/release_notes_tests.py` pins the commit side of this.
6. **Verify the artefact.** The tarball lists `src/mcapp`, the webapp bundle and `bootstrap/`. Its version string
   matches the tag.
7. **Deploy** on mcapp.local (RB-004), then run the health checks (RB-001).

## 5. Definition of success

- `./scripts/gate.sh` ended with `GATE: PASS` for the release commit.
- The GitHub release exists with the expected tag, and `doc/release-history.md` has the new section.
- On the box, `/api/status` reports the new version, `cat /var/lib/mcapp/system-epoch` shows the expected epoch,
  and RB-001 reports no failed check.
- Production only: the production tag is on `main` and `development` carries the next version.

## 6. Troubleshooting

| Symptom (exact message) | Cause | Action |
|---|---|---|
| `GATE: FAIL (mcproxy-mypy)` | strict typing regression | run `uv run mypy src/mcapp ble_service/src`, fix, rerun |
| `GATE: FAIL (mcproxy-ruff-format)` | a Markdown `python` block or new file not formatted | `uvx ruff format --check .`, format, rerun |
| release aborts after a tag was created | cleanup handler ran | check the tags and the pre-release list, delete leftovers manually (`release.sh` traps and rolls back) |
| auto mode still prompts for `./scripts/release.sh` | the allow rule does not match the command | open B23 (`doc/backlog.md`) and check the rule pattern |

## 7. Feedback and changes

- If a step is wrong when you run it, fix this file the same day or add a line to `../review-log.md`.

## 8. HITL review (owner)

- [ ] Confirmed: the steps match how releases are run
- [ ] Refuted: see comments
- Comments:
- Reviewer / date:
