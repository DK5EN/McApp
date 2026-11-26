---
name: prod-release
description: Promote a soaked dev pre-release to a McApp production release and roll it onto mcapp.local — pre-flight both repos, write and commit the release notes, publish with scripts/release.sh 2, deploy from the webapp Update page, verify. Use for "cut a production release", "prod release", "release vX.Y.Z", "promote to production", "ship v2.0.1 to prod", "release.sh 2".
---

# Production release → mcapp.local

A production release is a **promotion, not a build**: the tree was already cut as `vX.Y.Z-dev.N`, deployed by `/dev-release` and soaked on the box. This skill turns that exact tree into `vX.Y.Z`, merges it to `main` in **both** repos, publishes a stable release and rolls it onto the Pi.

`scripts/release.sh 2` does the mechanical part. It never gates the tree, it aborts on two pre-conditions you must arrange, and (historically) left a push undone that broke the next release.

## The one-screen version

```bash
# 0. RESUME.md soak checklist (if present) + health: read recent commits, doc/backlog.md, the health log banner
git log --oneline -5
/ai-ops                                            # then read doc/ops-mcapp-health-log.md

# 1. update dependencies in BOTH repos — mandatory
uv lock --upgrade && uv sync --all-packages        # + the standalone ble_service lock (see Step 1)
cd ../webapp && npm update

# 2. re-sync shared code with ../mc-chat; check corpora
diff -rq src/mcapp/contract    ../mc-chat/contract
diff -rq src/mcapp/classifier  ../mc-chat/meshcom_mock/classifier

# 2b. refresh the ICSSW group label snapshot, review the diff
uv run python scripts/refresh_group_labels.py && git diff --stat src/mcapp/group_labels_icssw.json

# 3. gate BOTH repos — step 1 invalidated your last run
./scripts/gate.sh > /tmp/gate.out 2>&1; echo "GATE_EXIT=$?"; cat /tmp/gate.out   # GATE: PASS

# 4. pre-conditions (Stops 1 and 2), commit the dependency bumps and the notes
for d in . ../webapp; do git -C $d rev-list --count development..origin/main; done   # both 0
$EDITOR doc/release-history.md doc/archive/release-history-full.md   # new section at the TOP
git add doc/release-history.md doc/archive/release-history-full.md
git commit -m "[docs] Add release notes for vX.Y.Z" && git push origin development

# 5. publish — newline on stdin, output to a file, read the exit code
printf '\n' | ./scripts/release.sh 2 > /tmp/release.log 2>&1; echo "EXIT=$?"

# 6. deploy: webapp → version chip (bottom right) → Update to vX.Y.Z → Start Update; check the runner's Finished line
# 7. verify against the ACTIVE slot, not the banner; /ai-ops + health-log entry (mandatory)
```

## Step 0 — Decide whether it is fit to promote

If `RESUME.md` exists it holds this release's soak checklist: run every check in it before anything else, and delete it together with the post-release health-log entry once nothing is open. Stop if any soak check fails (fix, cut a new dev release, the soak restarts). On 2026-11-08 the v2.1.26 promotion ran five of seven soak checks only after the release. Then run `/ai-ops` and read its verdict. A production cut needs more than a green suite:

- the dev tag has **soaked on the box** (hours, not minutes, `NRestarts` still 0) with zero journal warnings over the window
- `./scripts/gate.sh` ends `GATE: PASS` on the exact tree you promote (Step 3); the gate script is the release gate (operator decision 2026-11-08); CI's `tests` workflow, re-enabled 2026-11-22, gates and auto-merges Dependabot PRs but signs off no promotion. A dev pre-release cut by `dev-release.yml` after dependency updates is promoted like any other: soak first
- the working trees are the tag: `git rev-parse HEAD` == `git rev-parse vX.Y.Z-dev.N` in both repos

Append the sweep to `doc/ops-mcapp-health-log.md` **before** releasing: it is the record of what the release was signed off against.

## Step 1 — Update dependencies, in both repos

Every production release ships current dependencies: a patch release is the moment the fleet takes the new tree.

```bash
uv lock --upgrade && uv sync --all-packages       # workspace root: mcapp + ble_service member
cd ../webapp && npm update                         # npm outdated shows direct deps only; update refreshes the shipped transitive pins
```

**The `ble_service` standalone lock does not update itself.** `ble_service` is a workspace member AND ships its own `ble_service/uv.lock` (used when `mcapp-ble.service` deploys independently). `uv lock --upgrade` run inside `ble_service/` walks up to the root workspace and updates the ROOT lock while its dry run reports the bump you wanted. Regenerate it outside the workspace:

```bash
tmp=$(mktemp -d)
cp ble_service/pyproject.toml ble_service/uv.lock "$tmp"/
uv lock --upgrade --directory "$tmp"
uv sync --directory "$tmp" --no-install-project     # prove it installs
cp "$tmp"/uv.lock ble_service/uv.lock
```

Dependabot edits `ble_service/pyproject.toml` and never regenerates this lock; diff the `specifier =` lines, not just versions.

A bump voids the previous green run: Step 3 runs AFTER this. Commit the bumps in each repo independently, before the release notes.

## Step 2 — Re-sync the shared code with `../mc-chat`

Silence from the two `diff -rq` commands is the pass. Output is drift, and **the direction decides**:

- **mc-chat is ahead** (normal): split and pull per `CLAUDE.md` § Vendored Subtrees (prefix and split-branch table there).
- **MCProxy is ahead**: someone edited a subtree in place. Do NOT pull (it would revert the local improvement). Port the change into mc-chat, verify it THERE (the other branch of any host-repo conditional runs there), commit, then split and pull back.

**Hand-copied corpora** (canonical here, no subtree to move them): see `CLAUDE.md` § Key Gotchas for the six and which sibling carries which. Compare hashes:

```bash
for f in group_dst_vectors conversation_key_vectors hashtag_dst_vectors blocklist_decision_vectors ack_match_vectors rm_blocklist_vectors; do
  find . ../mc-chat ../webapp -name "$f.json" -not -path "*/node_modules/*" -exec shasum -a 256 {} +
done
```

Equal hashes per corpus, or fix it (copy to every carrier and bump the webapp's `EXPECTED_SHA256`). Both siblings carry a `.prettierignore` for these files, since a formatter run breaks byte equality while leaving content identical.

## Step 2b — Refresh the ICSSW group label snapshot

`src/mcapp/group_labels_icssw.json` ships in the package; the Pi never fetches it, so every production release refreshes it here:

```bash
uv run python scripts/refresh_group_labels.py      # rewrites the JSON; exits non-zero on < 50 rows
git diff src/mcapp/group_labels_icssw.json         # read it: renamed groups, new ranges, removals
```

- No diff is normal. A diff goes into the dependency-bump commit (`fetched_at` alone is not worth a commit: `git checkout src/mcapp/group_labels_icssw.json`).
- Non-zero exit = the page layout changed. Do not hand-edit the JSON; release with the shipped snapshot and fix the parser afterwards (`group_labels_tests.py`).
- Upstream removals never touch operator overrides (`group_label_overrides` in the DB).

## Step 3 — Gate both repos

`release.sh` runs `npm run build:strict` and nothing else. `./scripts/gate.sh > /tmp/gate.out 2>&1; echo "GATE_EXIT=$?"; cat /tmp/gate.out` runs every step in both repos even after a failure. Read `GATE_EXIT`, never an exit code through `| tail`. `./scripts/gate.sh mcproxy` or `webapp` scopes it while iterating; the release needs the full run.

`config_migration` reports `SKIPPED — NOT VERIFIED` on macOS (bash-4-only suite, macOS ships 3.2): an instrument gap, not coverage. When a release touches `bootstrap/` config migration, run it on the Pi against the dev tag (`/dev-release` "A dev build can run the real gate on the box").

## The version is not a parameter

`release.sh` releases `v${pyproject version}`; there is no override. The value is what the previous release's `post_release_prep` left behind (patch bump, commit `[chore] Prep vX.Y.Z for next dev cycle`). For a minor or major, edit both `pyproject.toml` and `ble_service/pyproject.toml`, commit and push, THEN release.

## Stop 1 — Release notes must be committed BEFORE you start the script

`release.sh` offers to take the notes at a prompt, but `validate_repos_clean` runs at the top of `main()`, so a repo with uncommitted notes aborts with "MCProxy has uncommitted changes" before the prompt. Write the notes, commit, push, THEN run the script; `commit_release_notes` then logs `release-history.md / release-history-full.md unchanged (already committed)`, which is the success path.

**Top-section guard**: after Enter at the prompt, `validate_release_notes_top` aborts (before any commit, merge or tag) unless the first `## ` heading is `## vX.Y.Z` (a date suffix is fine; `## v2.0.1` for 2.0.17 does not match) and the section is non-empty.

**What the notes look like.** `upload_production` publishes ONLY the top `## vX.Y.Z` section (plus a footer link) as the GitHub release body, and the webapp's Update page renders the same section per release (`ReleaseNotesCard` in `src/views/Update.vue`). So it must stand alone: `## vX.Y.Z (YYYY-MM-DD)`, a lead paragraph, `### Highlights`, `### Backend (MCProxy)`, `### Frontend (webapp)`, `### Upgrade notes`, written for the operator, not as a commit transcript. Only the newest release keeps full notes; when adding the new section:

1. Fetch the previous top section **as published**: `git show v<prev>:doc/release-history.md` (the working copy may carry post-publication edits).
2. Prepend it unchanged to `doc/archive/release-history-full.md` (top, under its intro, newest first).
3. Replace it in `release-history.md` with a 1-3 bullet `### vX.Y.Z (date)` entry under `## Earlier releases, in brief`'s intro paragraph (blank line after the intro).
4. The brief entry keeps schema, `SYSTEM_EPOCH` and contract changes and one-off operator actions (reboot, firmware minimum); measurements and field numbers belong in the full section only.

Material: `git log v<prev>..HEAD --oneline --no-merges` in both repos. Then `npx --yes prettier@3 --write doc/release-history.md doc/archive/release-history-full.md` followed by `uvx ruff format --check .` (ruff also formats fenced `python` blocks in `.md`).

## Stop 2 — `main` must not be ahead of `development`, in EITHER repo

`validate_main_mergeable` aborts if `development..origin/main` is non-empty in either repo (`[ERROR] webapp: main has 1 commit(s) not in development (diverged)`). It should always pass now; a non-zero count means something outside the release flow touched `main`.

```bash
for d in . ../webapp; do git -C $d fetch origin --quiet; echo "$d behind: $(git -C $d rev-list --count development..origin/main)"; done   # must be 0
```

Fix with the same merge-back `release.sh` performs (never rewrite a pushed `main`):

```bash
git -C ../webapp merge main --no-ff -m "[chore] Merge main back into development after <what>"
git -C ../webapp push origin development
```

## Stop 3 — The script blocks on a bare `read`; `< /dev/null` kills it

`./scripts/release.sh 2` skips the menu, but the production path still calls `wait_for_release_notes` (a bare `read -r`). Under `set -euo pipefail` a `read` at EOF returns 1 and the rollback trap fires, so `< /dev/null` makes it fail. Feed a newline:

```bash
printf '\n' | ./scripts/release.sh 2 > /tmp/release.log 2>&1; echo "EXIT=$?"
```

**Never pipe the script through `tail`/`head`** (the exit code becomes tail's; a rolled-back release reads as clean).

## What the script does

After `validate_tools` / `validate_on_development` / `validate_repos_clean`: resolve `v${version}` (refuse if the tag exists in either repo) → `validate_main_mergeable` → notes prompt, `validate_release_notes_top`, commit notes if dirty → merge `development` → `main` in both repos → `npm run build:strict` (writes `version.html`) → combined tarball → annotated tag in both repos → push `main` + tag in both repos BEFORE the release exists → sha256, `gh release create --verify-tag` (stable) and upload → verify, clean up, set `_RELEASE_SUCCESS=true` (published and irreversible from here) → housekeeping: back to `development`, merge `main` back in both repos, `post_release_prep` (bump both `pyproject.toml`s, the local-package lines in `uv.lock` and `ble_service/uv.lock`, the webapp's `package.json`/`package-lock.json`, commit in each, push BOTH repos' `development`).

**The rollback covers only the steps before `_RELEASE_SUCCESS`**: the trap removes local and remote tags in both repos, the GitHub release and artefacts, and restores the checkout. A failed run leaves nothing behind: diagnose and re-run.

## If housekeeping fails — the release is already published

The script prints a block headed **"Release vX.Y.Z IS PUBLISHED"** and exits non-zero. **Do NOT re-run `./scripts/release.sh 2`**: there is no resume, and `post_release_prep` may already have bumped `pyproject.toml`, so a re-run resolves the NEXT version and cuts a second, unreviewed production release. The block lists each sub-step as `[done]`/`[pending]` and prints the commands to finish by hand. Then confirm:

```bash
for d in . ../webapp; do
  git -C $d fetch origin --quiet
  echo "$d unpushed: $(git -C $d rev-list --count origin/development..development)"   # both 0
  echo "$d behind main: $(git -C $d rev-list --count development..origin/main)"       # both 0
done
```

An unpushed webapp `development` aborts the NEXT release at Stop 2 (this is what blocked v2.0.1: `post_release_prep` once pushed MCProxy only). Check these counts after every release; `scripts/release_prep_tests.py` pins the fix against the remotes.

## Step 6 — Deploy from the webapp Update page

**Not `curl | sudo bash`** (classifier-blocked, 429 rate limits, an unresolved version "deploys" the old slot green). The self-converging update path is the supported one.

1. Open `https://mcapp.local/` in Chrome, click the **version chip** (bottom-right) → `/webapp/update`.
2. **Version Status** should read `LATEST RELEASE vX.Y.Z — Update available`; if it shows the old release GitHub's API has not caught up: wait, do not re-cut.
3. **Update to vX.Y.Z** → confirm (`Mode: Production`) → **Start Update**; the modal streams `PREPARE → DEPLOY → ACTIVATE → HEALTH → DONE`.

The deploy takes several minutes. Do not poll turn by turn: arm ONE background wait (Bash `run_in_background: true`, never chained `sleep`). **Record the old state BEFORE clicking Start Update**, because the terminal condition is "moved off it":

```bash
ssh mcapp.local 'readlink -f ~/mcapp-slots/current; systemctl show mcapp -p ActiveEnterTimestampMonotonic --value'   # -> OLD_SLOT / OLD_START
```

```bash
ssh mcapp.local 'OLD_SLOT=/home/martin/mcapp-slots/slot-N OLD_START=<monotonic>
for i in $(seq 1 80); do
  tag=$(curl -s --max-time 10 http://127.0.0.1/webapp/version.html | tr -d "[:space:]")
  cur=$(readlink -f ~/mcapp-slots/current)
  start=$(systemctl show mcapp -p ActiveEnterTimestampMonotonic --value)
  if [ "$tag" = "vX.Y.Z" ] && [ "$cur" != "$OLD_SLOT" ] && [ "$start" != "$OLD_START" ] \
     && systemctl is-active --quiet mcapp; then
    echo "DONE $cur"; exit 0
  fi
  sleep 15
done; echo "TIMEOUT: not on vX.Y.Z in a new slot after 20 min"; exit 1'
```

- **`version.html` alone fires too early**: the runner copies the webapp bundle during DEPLOY, before ACTIVATE switches `current` and restarts services. All three conditions are needed (new tag, different `current`, changed `ActiveEnterTimestampMonotonic`). On an in-place re-deploy of the same version `current` does not move: drop the slot condition and rely on the restart.
- **Poll from the Pi, not the Mac**: from the Mac each request first spends ~5 s on the mDNS lookup and a `--max-time 5` loop never matched. On the Pi `127.0.0.1` needs no lookup. The 80 x 15 s bound turns a deploy that never lands into `TIMEOUT`.
- **Never key it on `/api/status`**: its `version` is the pyproject version, identical for a dev tag and the release it becomes (it read `v2.0.11` before the deploy started). `version.html` is the only field telling them apart.
- Auto-rollback is armed: if health checks fail the previous slot stays active.

### Read the runner's own result — "Update completed successfully" is not evidence

```bash
ssh mcapp.local 'sudo journalctl -u mcapp-update --since "-30min" --no-pager | grep -E "Finished:|WARN.*(unresolved|Cannot determine)"'
# want: [UPDATE-RUNNER] Finished: {"status": "success", "version": "vX.Y.Z", "slot": N, ...}
```

**`version` must be the tag you released and `slot` the slot `current` points to.** Anything else stops the flow: do not start Step 7 or call it deployed. On 2026-11-08 (F14, health log §59) the Pi could not reach the GitHub API, the bootstrap rebuilt the old slot in place, and the runner logged `"status": "success", "version": "v2.1.26-dev.3"` while the modal showed DONE. The fix (`"status": "failed", "reason": "version_unresolved"`) only takes effect once a slot carrying it drives an update, because the ACTIVE slot's runner and bootstrap run (backlog B22). `failed` / `version_unresolved` means nothing was deployed: wait for GitHub, start the update again.

## Step 7 — Verify

The modal's DONE is not verification.

```bash
curl -sk https://mcapp.local/webapp/version.html                   # THE tag check; NOT /version.html (404)
curl -sk https://mcapp.local/api/status | python3 -m json.tool     # pyproject version, not the tag
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current
  systemctl is-active mcapp mcapp-ble caddy lighttpd
  systemctl show mcapp -p NRestarts --value          # still 0
  grep -h "^LATEST_SCHEMA_VERSION" ~/mcapp-slots/current/src/mcapp/storage/constants.py
  find ~/mcapp-slots/current/src/mcapp -name "*_tests.py" -o -name "*.json" | wc -l'   # exactly 3 on production
```

- **A production tarball carries no test harness, by design**: no `*_tests.py`, `tests.py`, `run_startup_tests.py` and none of the `.json` corpora except three runtime data files (`contract/classifier_contract.json`, `contract/axes_contract.json`, `group_labels_icssw.json`). The count is 3 on production and far larger on dev; more on a production slot means the predicate regressed. `main.py` must never import a test module at import time (a module-level import makes every production build refuse to start); pinned by `scripts/webapp_deploy_tests.py`.
- `version.html` reports what was deployed, not what the browser runs: reload past the service worker ("Update available — Reload") before believing a frontend change is live.
- If the release changed the schema, the DB's `schema_version` must equal `LATEST_SCHEMA_VERSION` in the **active slot**; a mismatch is the loudest finding there is.

### Post-release sweep — mandatory

Run `/ai-ops` once the box has settled and append the sweep to `doc/ops-mcapp-health-log.md` as a new numbered section (banner updated) before reporting the release done. Record anything the deploy got wrong as an `F<n>` finding even if a second run fixed it.

## Rollback

The update page has an **Activate** button on every non-active populated slot; the previous release is still in its slot, so reverting is a slot activation (code + webapp bundle + services), not a re-deploy. Use it before considering a hotfix release. Activation never touches the database (forward-only additive migrations; see `CLAUDE.md` § Deployment).

Never delete or move a published production tag to "fix" a release; cut `vX.Y.Z+1` (the version is already bumped in `pyproject.toml`).

## Related

`/dev-release` (the `-dev.N` this promotes), `/ai-ops` (the health check and its log), `doc/reference/version-logic.md`, `doc/release-history.md` / `doc/archive/release-history-full.md`, `bootstrap/README.md`.
