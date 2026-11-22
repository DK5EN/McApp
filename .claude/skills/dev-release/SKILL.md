---
name: dev-release
description: Cut a McApp dev pre-release and install it on mcapp.local, end to end — gate both repos, commit and push, publish with scripts/release.sh, then curl-bash the box and verify the change is actually live. Use for "cut a dev release", "release and deploy", "release.sh", "get this onto the Pi", "deploy to mcapp.local", "ship it".
---

# Dev release → mcapp.local

`scripts/release.sh` tags **MCProxy + webapp** together, builds one combined tarball and publishes it as a GitHub pre-release. `bootstrap/mcapp.sh --dev` then pulls that pre-release onto the Pi into a fresh slot.

`mcapp.local` is the production box and there is no staging host: a dev release goes straight onto the box the operator uses. The slot system is the safety net (a failed deploy leaves the previous slot active).

## The one-screen version

```bash
# 0. both repos on `development`, tags fresh
git fetch --tags && git -C ../webapp fetch --tags

# 1. gate BOTH repos yourself — release.sh does not
./scripts/gate.sh > /tmp/gate.out 2>&1; echo "GATE_EXIT=$?"; cat /tmp/gate.out   # GATE: PASS

# 2. commit + push BOTH repos (explicit paths, never `git add -A`)

# 3. publish (non-interactive: 1 = dev, 2 = production)
./scripts/release.sh 1

# 4. let the asset settle, verify it is fetchable FROM THE PI (curl 56 trap)
ssh mcapp.local 'curl -fsSL -o /tmp/t.tar.gz <asset-url> && sha256sum /tmp/t.tar.gz; rm -f /tmp/t.tar.gz'

# 5. install — scp the LOCAL bootstrap, pin the tag, log on the Pi
scp bootstrap/mcapp.sh mcapp.local:/tmp/mcapp-bootstrap.sh     # compare sha256 on both sides
ssh mcapp.local 'sudo bash /tmp/mcapp-bootstrap.sh --dev --tag <tag> > ~/deploy-<tag>.log 2>&1; \
  echo "EXIT=$?"; tail -45 ~/deploy-<tag>.log'

# 6. verify the change is in the ACTIVE slot, not just on disk
```

## Step 1 — Gate both repos

`release.sh` runs `npm run build:strict` and nothing else: no ruff, mypy, backend suite, eslint or vitest. A red tree releases silently. `./scripts/gate.sh` covers both repos and ends with `GATE: PASS` / `GATE: FAIL (<steps>)`; read its exit code directly, never through `| tail`. Dependency changes count as changes: re-gate after any `uv.lock` / `package-lock.json` movement.

## Step 2 — Commit and push, both repos, push first

Each repo commits independently (`[type] description`), explicit paths only.

**Push before releasing.** `gh release create` pushes nothing, not even the tag; an absent remote tag is created server-side at the default branch's HEAD and diverges from your local one (55 dev tags drifted that way). `release.sh` now pushes tags itself and passes `--verify-tag`; confirm parity afterwards:

```bash
[ "$(git rev-parse <tag>)" = "$(git ls-remote origin refs/tags/<tag> | awk '{print $1}')" ] && echo "tag parity ok"
```

**Expect the push to be rejected**: Dependabot PRs land on `development` while you work.

```bash
git fetch origin development
git log --oneline development..origin/development   # look before you touch anything
git rebase origin/development
```

If the rebase pulled in a dependency change (`uv.lock`, `pyproject.toml`), re-run the whole gate (`uv sync --all-packages` first). A bump touching only `.github/workflows/` does not change the tested tree (`git diff --stat development...origin/development`); a quick `ruff` suffices.

## Step 3 — `./scripts/release.sh 1`

`1` = dev, `2` = production. With an argument the dev path is fully non-interactive.

- Test a webapp feature branch on the box with `WEBAPP_BRANCH=feature/<name> ./scripts/release.sh 1` (webapp checked out on that branch, MCProxy still on `development`). Production refuses the override.
- It refuses to start unless **both** repos are on `development` with clean trees. Run nothing else that builds the webapp (`npm run build*`, a review agent's gate) meanwhile, or inspect `webapp/dist` concurrently: builds rewrite `dist/` under each other. Needs `git gh shasum tar sed npm jq`.
- The next tag is `pyproject.toml`'s version plus the highest **local** `vX.Y.Z-dev.N` tag, so `git fetch --tags` in both repos first or a number gets reused.
- Order: build webapp → tag both repos → build combined tarball → sha256 → push BOTH repos' tags → `gh release create --prerelease --verify-tag` → clean up. A trap rolls back tags, release and artefacts on failure.

## Step 4 — The `curl: (56)` trap

GitHub's asset CDN needs a moment after `gh release create`; deploying immediately gives `curl: (56) Connection died, tried 5 times` / `Failed to download release tarball`. That is transient. Confirm first:

```bash
gh release view <tag> --repo DK5EN/McApp --json assets --jq '.assets[] | "\(.name) \(.size) \(.state)"'
ssh mcapp.local 'curl -fsSL -o /tmp/t.tar.gz https://github.com/DK5EN/McApp/releases/download/<tag>/mcapp-<tag>.tar.gz \
  && sha256sum /tmp/t.tar.gz; rm -f /tmp/t.tar.gz'
```

The sha256 must match what `release.sh` printed. If curl still dies, wait and retry; do not diagnose the Pi's network until both assets show `uploaded`.

## Step 5 — Install on the Pi

```bash
scp bootstrap/mcapp.sh mcapp.local:/tmp/mcapp-bootstrap.sh
shasum -a 256 bootstrap/mcapp.sh                                   # local
ssh mcapp.local 'sha256sum /tmp/mcapp-bootstrap.sh'                # must be identical
ssh mcapp.local 'sudo bash /tmp/mcapp-bootstrap.sh --dev --tag <tag> > ~/deploy-<tag>.log 2>&1; \
  echo "EXIT=$?"; tail -45 ~/deploy-<tag>.log'
```

- **No `curl ... | sudo bash`**: the auto-mode classifier refuses a remote fetch piped into `sudo bash`; copy-and-hash is the same deploy and passes.
- **Always `--tag <tag>`**: GitHub rate-limits the Pi (429 from `raw.githubusercontent.com` and `api.github.com`). Unpinned, the version lookup can fail and the bootstrap **deploys the old version in place and exits 0** ("App version is left unresolved"). `--tag` skips the API, downloads from the release-asset CDN and pins both the app version and the bootstrap tree ref.
- Log to `~` on the Pi, not `/tmp` (tmpfs; an old log looks like a healthy new one).
- **Never pipe the ssh through `tail`/`head`**: it reports tail's exit code and buffers everything. Redirect to a log on the Pi, echo `$?` remotely, tail the file afterwards.
- Run with `run_in_background: true`; the full run takes 5-15 min on a Pi Zero 2W. Watch with `tail -1 ~/deploy-<tag>.log` over a second ssh.
- Other flags: `--check` (dry run), `--skip` (deploy only), `--converge` (system epoch only), `--force`. See `bootstrap/README.md`.

## Step 6 — Verify

A "complete" banner is not verification.

```bash
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current                      # which slot is live
  systemctl is-active mcapp mcapp-ble                    # both active
  systemctl show mcapp -p ActiveEnterTimestamp --value   # restarted just now, not days ago
  curl -s http://127.0.0.1/webapp/version.html           # the TAG
  find ~/mcapp-slots/current/src/mcapp -name "*.json" | wc -l   # == local count on a dev build
  grep -c "<a symbol from your change>" ~/mcapp-slots/current/src/mcapp/<file>.py'
```

- **Grep the ACTIVE slot for a symbol only your change introduces.** Everything else can look healthy while the service runs the old code.
- **`/api/status` `version` is the pyproject version, not the tag** (every `vX.Y.Z-dev.N` builds from the same `X.Y.Z`); a wait loop keyed on it succeeds instantly against old code. Use `/webapp/version.html`.
- **The `.json` count guards packaging.** `build_tarball`'s second argument selects the shape: a dev pre-release ships the test harness (`*_tests.py`, `tests.py`, every `.json` corpus under `src/mcapp/`, `run_startup_tests.py`), production ships runtime code only. A dev slot must hold exactly your checkout's count (`find src/mcapp -name '*.json' | wc -l`, compare locally, never against a remembered number); zero on a dev slot means the predicate regressed. Because production has no harness, `main.py` must never import a test module at import time (it loads `router_tests` lazily, `ImportError` = skipped). Pinned by `scripts/webapp_deploy_tests.py`.
- **A dev build can run the real gate on the box**: `ssh mcapp.local 'cd ~/mcapp-slots/current && ./.venv/bin/python scripts/run_startup_tests.py'`.
- Slot order is not fixed (`get_target_slot` picks the empty-or-oldest), and a re-deploy of the version the active slot holds deploys in place: an unchanged `current` symlink is not evidence of failure.
- The health-check block in the log should end with `webapp version:` at the new tag and `active slot:`.

### When the release adds a schema migration or an opt-in feature

Check on the box (no `sqlite3` CLI: use a python heredoc; timestamps are milliseconds):

```bash
ssh mcapp.local 'python3 - <<"PYEOF"
import sqlite3
c = sqlite3.connect("file:/var/lib/mcapp/messages.db?mode=ro", uri=True)
print(c.execute("select * from schema_version").fetchall())      # the new LATEST_SCHEMA_VERSION
print([r[0] for r in c.execute("select name from sqlite_master where type=\"table\" and name like \"<prefix>%\"")])
PYEOF
sudo journalctl -u mcapp.service --since "<restart time>" -p warning --no-pager | tail     # no entries
sudo journalctl -u mcapp.service --since "<restart time>" --no-pager | grep -i migration | tail -3
curl -s http://127.0.0.1/api/status | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"features\"))"'
```

A feature meant to be always on must show up in `features` (Node Admin: `['node_admin']`) and its read route must answer 200. Never design a feature behind a hand-edited config flag (operator decision): the GUI exposes it, and anything that transmits on air is a separate step with its own go-ahead.

### Then watch live traffic — not optional

A green suite plus a green deploy is not proof. For anything on the mesh-ingest path, watch real rows land and **read the values**, not the row count. Beacons arrive every ~30 min per station; give it 20-30 minutes. A QFE fix once deployed clean with all suites green, and the first live rows showed two rows per station one second apart with different values (962.6 derived vs 966.5 measured): the new code had made an unreachable dedup branch reachable, visible only where two transports of the same beacon met.

## When it fails

A failed deploy is normally safe: the tarball lands in the inactive slot, so the previous slot stays symlinked and running. Confirm instead of assuming:

```bash
ssh mcapp.local 'readlink -f ~/mcapp-slots/current; systemctl is-active mcapp mcapp-ble'
```

Report plainly and retry; do not re-cut a release to work around a transient download. Any populated slot can be activated from the webapp Update page without a re-deploy.

## Pi gotchas

- No `sqlite3` CLI (python3 script, `scp`, run, delete) and no `tcpdump` (write a `socket.AF_PACKET` sniffer and run it under `sudo nohup setsid`; a bare `cmd &` in an ssh one-liner dies with the session).
- Never open a second consumer on the BLE service's `/api/ble/notifications` SSE stream: the queue is single-consumer (`popleft()`), so a debugging client steals frames from production.

## Related

`doc/reference/version-logic.md` (dev/production versions and tags), `bootstrap/README.md` (installer flags), `CLAUDE.md` § System Epoch (bump `SYSTEM_EPOCH` + `REQUIRED_SYSTEM_EPOCH` together).
