# release.sh — MCProxy Release Workflow

Builds a release tarball, uploads it to GitHub Releases, and manages version numbers. Runs on Mac (the dev machine), not on the Pi.

## Prerequisites

- `git` — clean working tree required
- `gh` — GitHub CLI, authenticated
- `shasum` — SHA256 checksum (ships with macOS)
- `tar`, `sed`

## Usage

```bash
./release.sh v0.52.0
```

Version format: `v{major}.{minor}.{patch}`

## What It Does

### Step 1: Validation

- Checks version format matches `v#.#.#`
- Checks all required tools are installed
- **Rejects dirty working trees** — commit or stash first

### Step 2: Version Bump

Updates the `version` field in both:
- `pyproject.toml` (main package)
- `ble_service/pyproject.toml` (BLE service)

Uses `sed -i ''` (macOS in-place edit).

### Step 3: Git Commit + Tag

```
git add pyproject.toml ble_service/pyproject.toml
git commit -m "[chore] Bump version to v0.52.0"
git tag -a v0.52.0 -m "Release v0.52.0"
```

The commit and tag are local only — you push them after reviewing the release.

### Step 4: Build Tarball

Creates `mcproxy-v0.52.0.tar.gz` in a temp directory, containing only release files:

```
mcproxy-v0.52.0/
  pyproject.toml
  uv.lock
  config.sample.json
  src/mcproxy/           # Full Python package
  src/mcproxy/commands/  # Commands sub-package
  ble_service/src/       # BLE service source
  ble_service/pyproject.toml
  bootstrap/             # Full bootstrap directory
```

**Excluded** (never in the tarball):
- `.git/`, `.venv/`, `__pycache__/`
- `.old/`, `doc/`, `logs/`
- Dev scripts: `deploy-to-pi.sh`, `release.sh`
- Data files: `mcdump.json`, `config.json`, `sperrliste.json`

### Step 5: SHA256 Checksum

Generates `mcproxy-v0.52.0.tar.gz.sha256` alongside the tarball. The bootstrap installer verifies this on download.

### Step 6: GitHub Release (Draft)

Uploads both files as a **draft** release via `gh release create --draft`. This lets you review before publishing.

### Step 7: Cleanup

Removes the local tarball and checksum file after upload.

## After the Script Finishes

```bash
# 1. Review the draft at https://github.com/DK5EN/McAdvChat/releases
# 2. Edit release notes if needed
# 3. Publish the release on GitHub
# 4. Push the tag and commit:
git push origin v0.52.0
git push
```

## How the Bootstrap Consumes Releases

When a Pi runs the bootstrap installer:

1. Queries `https://api.github.com/repos/DK5EN/McAdvChat/releases/latest` for the newest tag
2. Downloads `mcproxy-v0.52.0.tar.gz` from the release assets
3. Verifies the SHA256 checksum
4. Extracts to `~/mcproxy/` with `--strip-components=1`
5. Runs `uv sync` to install dependencies into `~/mcproxy/.venv/`
6. Renders systemd service from `bootstrap/templates/mcproxy.service`

## Version Flow

```
pyproject.toml  ──►  importlib.metadata  ──►  __version__  ──►  VERSION = "v0.52.0"
                     (at runtime)              (__init__.py)      (main.py)
```

Single source of truth is `pyproject.toml`. The Python package reads it at runtime via `importlib.metadata.version("mcproxy")`. The bootstrap reads it via `grep` from the installed `pyproject.toml`.
