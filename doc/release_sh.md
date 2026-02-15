# release.sh — McApp Unified Release Workflow

> **Note:** The GitHub repo is `DK5EN/McApp` (legacy name kept for compatibility).
> The project is called **McApp**.

Builds a combined tarball (backend + webapp), uploads it to GitHub Releases, and manages version numbers. Runs on Mac (the dev machine), not on the Pi.

**No arguments needed** — the script auto-detects the branch and does the right thing.

## Prerequisites

- `git` — clean working tree required
- `gh` — GitHub CLI, authenticated
- `npm` — for building the Vue.js webapp
- `shasum` — SHA256 checksum (ships with macOS)
- `tar`, `sed`, `jq`
- The webapp repo must be at `../webapp` relative to McApp

## Usage

```bash
./release.sh
```

That's it. Branch detection handles everything.

## Two Modes

### Production (on `main` branch)

```bash
git checkout main
./release.sh
```

1. Auto-bumps minor version: reads `pyproject.toml` (e.g., `0.51.0`) → `v0.52.0`
2. Validates clean tree and required tools
3. Updates `version` in `pyproject.toml` and `ble_service/pyproject.toml`
4. Builds the webapp (`npm run build` in `../webapp`)
5. Writes `version.txt` into webapp dist
6. Commits version bump, creates annotated tag
7. Builds combined tarball, uploads as `--draft`
8. Prints next steps (review, publish, push)

### Dev (on `development` branch)

```bash
git checkout development
./release.sh
```

1. Validates clean tree and required tools
2. Reads current version from `pyproject.toml` (e.g., `0.51.0`)
3. Scans existing tags: `git tag -l "v0.51.0-dev.*"`
4. Auto-generates next tag: `v0.51.0-dev.1`, `v0.51.0-dev.2`, etc.
5. Does **NOT** bump `pyproject.toml`
6. Builds the webapp (`npm run build` in `../webapp`)
7. Creates lightweight tag (no commit needed)
8. Builds combined tarball, uploads as `--prerelease` (published, not draft)
9. Auto-pushes the tag (no review step for dev)

## Tarball Structure (Both Modes)

```
mcapp-v0.52.0/
  pyproject.toml
  uv.lock
  config.sample.json
  src/mcapp/             # Python package
  src/mcapp/commands/    # Commands sub-package
  ble_service/src/       # BLE service source
  ble_service/pyproject.toml
  bootstrap/             # Bootstrap scripts
  webapp/                # Built Vue.js SPA
    index.html
    version.txt          # Contains the release tag
    assets/
    img/
    ...
```

**Excluded** (never in the tarball):
- `.git/`, `.venv/`, `__pycache__/`
- `.old/`, `doc/`, `logs/`
- Dev scripts: `deploy-to-pi.sh`, `release.sh`
- Data files: `mcdump.json`, `config.json`, `sperrliste.json`

## After a Production Release

```bash
# 1. Review the draft at https://github.com/DK5EN/McApp/releases
# 2. Edit release notes if needed
# 3. Publish the release on GitHub
# 4. Push the tag and commit:
git push origin v0.52.0
git push
```

Dev releases are auto-published and auto-pushed — no manual steps required.

## How the Bootstrap Consumes Releases

### Production (default)

```bash
sudo ./mcapp.sh
```

1. Queries `/releases/latest` for the newest stable tag (pre-releases are excluded)
2. Downloads `mcapp-v0.52.0.tar.gz` from the release assets
3. Verifies SHA256 checksum
4. Extracts to `~/mcapp/` with `--strip-components=1`
5. Copies `~/mcapp/webapp/` to `/var/www/html/webapp/` (bundled SPA)
6. Runs `uv sync` to install Python dependencies
7. Renders systemd service from `bootstrap/templates/mcapp.service`

### Dev mode

```bash
sudo ./mcapp.sh --dev
```

1. Queries `/releases` and finds the first pre-release
2. Downloads the pre-release tarball
3. Same extraction and deployment flow as production

### Backward Compatibility

If the tarball doesn't contain a `webapp/` directory (old releases), the bootstrap falls back to downloading the webapp separately from `raw.githubusercontent.com`.

## Version Flow

```
pyproject.toml  ──►  importlib.metadata  ──►  __version__  ──►  VERSION = "v0.52.0"
                     (at runtime)              (__init__.py)      (main.py)
```

Single source of truth is `pyproject.toml`. The Python package reads it at runtime via `importlib.metadata.version("mcapp")`. The bootstrap reads it via `grep` from the installed `pyproject.toml`.

For dev releases, the tag (e.g., `v0.51.0-dev.3`) is written to `webapp/version.txt` but `pyproject.toml` retains the base version (`0.51.0`).
