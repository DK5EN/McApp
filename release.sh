#!/bin/bash
# release.sh - Unified release builder for MCProxy
#
# Fully automatic — just run ./release.sh (no arguments needed).
# Detects the current branch and handles everything:
#
#   main branch       → Production release (bumps version, draft)
#   development branch → Dev pre-release (auto-numbered, published)
#
# Produces a single combined tarball with backend + webapp.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WEBAPP_DIR="$(cd "${SCRIPT_DIR}/../webapp" && pwd)"
readonly GITHUB_REPO="DK5EN/McAdvChat"

# Colors
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

log_info() { echo -e "${GREEN}==>${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

#──────────────────────────────────────────────────────────────────
# BRANCH / MODE DETECTION
#──────────────────────────────────────────────────────────────────

detect_mode() {
  local branch
  branch=$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)

  case "$branch" in
    main|master)
      echo "production"
      ;;
    development)
      echo "dev"
      ;;
    *)
      log_error "Releases can only be created from 'main' or 'development' branch."
      log_error "Current branch: ${branch}"
      exit 1
      ;;
  esac
}

#──────────────────────────────────────────────────────────────────
# VALIDATION
#──────────────────────────────────────────────────────────────────

validate_clean_tree() {
  if [[ -n "$(git -C "$SCRIPT_DIR" status --porcelain)" ]]; then
    log_error "Working tree is not clean. Commit or stash changes first."
    git -C "$SCRIPT_DIR" status --short
    exit 1
  fi
}

validate_tools() {
  local -a required=("git" "gh" "shasum" "tar" "sed" "npm" "jq")
  for tool in "${required[@]}"; do
    if ! command -v "$tool" &>/dev/null; then
      log_error "Required tool not found: ${tool}"
      exit 1
    fi
  done
}

#──────────────────────────────────────────────────────────────────
# VERSION HELPERS
#──────────────────────────────────────────────────────────────────

# Read current version from pyproject.toml (e.g., "0.51.0")
read_pyproject_version() {
  grep -oE '^version = "([^"]+)"' "${SCRIPT_DIR}/pyproject.toml" \
    | head -1 | sed 's/version = "//;s/"//'
}

# Production: auto-bump minor version (0.51.0 → v0.52.0)
auto_version() {
  local current
  current=$(read_pyproject_version)

  local major minor patch
  IFS='.' read -r major minor patch <<< "$current"
  minor=$((minor + 1))
  patch=0

  echo "v${major}.${minor}.${patch}"
}

# Dev: find the next -dev.N tag for the current version
get_next_dev_tag() {
  local current
  current=$(read_pyproject_version)
  local prefix="v${current}-dev"

  # Find existing dev tags for this version
  local highest=0
  while IFS= read -r tag; do
    [[ -z "$tag" ]] && continue
    local num="${tag##*.}"
    if [[ "$num" =~ ^[0-9]+$ ]] && (( num > highest )); then
      highest=$num
    fi
  done < <(git -C "$SCRIPT_DIR" tag -l "${prefix}.*")

  local next=$((highest + 1))
  echo "${prefix}.${next}"
}

#──────────────────────────────────────────────────────────────────
# VERSION BUMP (production only)
#──────────────────────────────────────────────────────────────────

bump_version() {
  local version="$1"
  local bare_version="${version#v}"

  log_info "Bumping version to ${version}..."

  sed -i '' "s/^version = \".*\"/version = \"${bare_version}\"/" \
    "${SCRIPT_DIR}/pyproject.toml"

  sed -i '' "s/^version = \".*\"/version = \"${bare_version}\"/" \
    "${SCRIPT_DIR}/ble_service/pyproject.toml"

  log_info "  Updated pyproject.toml files"
}

#──────────────────────────────────────────────────────────────────
# WEBAPP BUILD
#──────────────────────────────────────────────────────────────────

build_webapp() {
  local version="$1"

  log_info "Building webapp..."

  if [[ ! -d "$WEBAPP_DIR" ]]; then
    log_error "Webapp directory not found: ${WEBAPP_DIR}"
    exit 1
  fi

  if [[ ! -f "${WEBAPP_DIR}/package.json" ]]; then
    log_error "No package.json found in ${WEBAPP_DIR}"
    exit 1
  fi

  # Install dependencies if node_modules is missing
  if [[ ! -d "${WEBAPP_DIR}/node_modules" ]]; then
    log_info "  Installing npm dependencies..."
    (cd "$WEBAPP_DIR" && npm install)
  fi

  # Build
  (cd "$WEBAPP_DIR" && npm run build)

  # Validate output
  if [[ ! -d "${WEBAPP_DIR}/dist" ]] || [[ ! -f "${WEBAPP_DIR}/dist/index.html" ]]; then
    log_error "Webapp build failed — no dist/index.html found"
    exit 1
  fi

  # Write version.txt into dist for bootstrap version detection
  echo "$version" > "${WEBAPP_DIR}/dist/version.txt"

  log_info "  Webapp built successfully (version.txt: ${version})"
}

#──────────────────────────────────────────────────────────────────
# TARBALL BUILD
#──────────────────────────────────────────────────────────────────

build_tarball() {
  local version="$1"
  local tarball_name="mcproxy-${version}.tar.gz"
  local prefix="mcproxy-${version}"
  local tmp_dir
  tmp_dir=$(mktemp -d)
  local staging="${tmp_dir}/${prefix}"

  log_info "Building release tarball..."

  mkdir -p "$staging"

  # Copy project files
  cp "${SCRIPT_DIR}/pyproject.toml" "$staging/"
  cp "${SCRIPT_DIR}/uv.lock" "$staging/" 2>/dev/null || log_warn "  No uv.lock found (will be generated by uv sync)"
  cp "${SCRIPT_DIR}/config.sample.json" "$staging/" 2>/dev/null || true

  # src/mcproxy/ package
  mkdir -p "${staging}/src/mcproxy/commands"
  find "${SCRIPT_DIR}/src/mcproxy" -name '*.py' -not -path '*/__pycache__/*' | while read -r f; do
    local rel="${f#${SCRIPT_DIR}/}"
    mkdir -p "${staging}/$(dirname "$rel")"
    cp "$f" "${staging}/${rel}"
  done

  # ble_service/
  mkdir -p "${staging}/ble_service/src"
  cp "${SCRIPT_DIR}/ble_service/pyproject.toml" "${staging}/ble_service/"
  find "${SCRIPT_DIR}/ble_service/src" -name '*.py' -not -path '*/__pycache__/*' | while read -r f; do
    local rel="${f#${SCRIPT_DIR}/}"
    mkdir -p "${staging}/$(dirname "$rel")"
    cp "$f" "${staging}/${rel}"
  done

  # bootstrap/ directory
  cp -r "${SCRIPT_DIR}/bootstrap" "${staging}/bootstrap"
  find "${staging}/bootstrap" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

  # webapp/ — copy built SPA from ../webapp/dist
  if [[ -d "${WEBAPP_DIR}/dist" ]]; then
    # Use tar to copy, excluding macOS metadata
    mkdir -p "${staging}/webapp"
    tar -cf - -C "${WEBAPP_DIR}/dist" --exclude='.DS_Store' . | tar -xf - -C "${staging}/webapp"
    log_info "  Included webapp ($(find "${staging}/webapp" -type f | wc -l | tr -d ' ') files)"
  else
    log_warn "  No webapp dist found — tarball will not include webapp"
  fi

  # Build tarball
  tar -czf "${SCRIPT_DIR}/${tarball_name}" -C "$tmp_dir" "$prefix"

  # Cleanup staging
  rm -rf "$tmp_dir"

  log_info "  Built ${tarball_name}"
  echo "$tarball_name"
}

generate_checksum() {
  local tarball="$1"
  local checksum_file="${tarball}.sha256"

  log_info "Generating SHA256 checksum..."

  (cd "${SCRIPT_DIR}" && shasum -a 256 "$tarball" > "$checksum_file")

  log_info "  $(cat "${SCRIPT_DIR}/${checksum_file}")"
  echo "$checksum_file"
}

#──────────────────────────────────────────────────────────────────
# GIT + GITHUB RELEASE
#──────────────────────────────────────────────────────────────────

commit_and_tag_production() {
  local version="$1"

  log_info "Creating git commit and annotated tag..."

  git -C "$SCRIPT_DIR" add pyproject.toml ble_service/pyproject.toml
  git -C "$SCRIPT_DIR" commit -m "[chore] Bump version to ${version}"
  git -C "$SCRIPT_DIR" tag -a "$version" -m "Release ${version}"

  log_info "  Committed and tagged ${version}"
}

tag_dev() {
  local version="$1"

  log_info "Creating lightweight tag ${version}..."

  git -C "$SCRIPT_DIR" tag "$version"

  log_info "  Tagged ${version}"
}

upload_production() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub draft release ${version}..."

  local -a assets=("${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}")

  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "MCProxy ${version}" \
    --notes "Release ${version}" \
    --draft \
    "${assets[@]}"

  log_info "  Draft release created: ${version}"
}

upload_dev() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub pre-release ${version}..."

  local -a assets=("${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}")

  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "MCProxy ${version} (dev)" \
    --notes "Development pre-release ${version}" \
    --prerelease \
    "${assets[@]}"

  log_info "  Pre-release published: ${version}"
}

#──────────────────────────────────────────────────────────────────
# CLEANUP
#──────────────────────────────────────────────────────────────────

cleanup_artifacts() {
  local tarball="$1"
  local checksum="$2"

  rm -f "${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}"
  log_info "  Cleaned up local artifacts"
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────

main() {
  echo ""
  echo "  MCProxy Release Builder"
  echo "  ========================"
  echo ""

  validate_tools

  local mode
  mode=$(detect_mode)

  log_info "Mode: ${mode} (detected from branch)"
  echo ""

  if [[ "$mode" == "production" ]]; then
    #── Production release ────────────────────────────────────────
    validate_clean_tree

    local version
    version=$(auto_version)
    local current
    current=$(read_pyproject_version)

    log_info "Version: ${current} -> ${version}"
    echo ""

    # Step 1: Bump version in pyproject.toml files
    bump_version "$version"

    # Step 2: Build webapp
    build_webapp "$version"

    # Step 3: Commit and tag
    commit_and_tag_production "$version"

    # Step 4: Build tarball
    local tarball
    tarball=$(build_tarball "$version")

    # Step 5: Generate checksum
    local checksum
    checksum=$(generate_checksum "$tarball")

    # Step 6: Upload as draft
    upload_production "$version" "$tarball" "$checksum"

    # Step 7: Cleanup
    cleanup_artifacts "$tarball" "$checksum"

    echo ""
    log_info "Release ${version} created successfully!"
    echo ""
    echo "  Next steps:"
    echo "  1. Review the draft release on GitHub"
    echo "  2. Edit release notes if needed"
    echo "  3. Publish the release"
    echo "  4. Push the tag: git push origin ${version}"
    echo "  5. Push the commit: git push"
    echo ""

  else
    #── Dev pre-release ───────────────────────────────────────────
    validate_clean_tree

    local version
    version=$(get_next_dev_tag)
    local current
    current=$(read_pyproject_version)

    log_info "Base version: ${current}"
    log_info "Dev tag: ${version}"
    echo ""

    # Step 1: Build webapp
    build_webapp "$version"

    # Step 2: Create lightweight tag (no commit — pyproject.toml unchanged)
    tag_dev "$version"

    # Step 3: Build tarball
    local tarball
    tarball=$(build_tarball "$version")

    # Step 4: Generate checksum
    local checksum
    checksum=$(generate_checksum "$tarball")

    # Step 5: Upload as pre-release (published, not draft)
    upload_dev "$version" "$tarball" "$checksum"

    # Step 6: Push the tag immediately (no review step for dev)
    log_info "Pushing tag ${version}..."
    git -C "$SCRIPT_DIR" push origin "$version"

    # Step 7: Cleanup
    cleanup_artifacts "$tarball" "$checksum"

    echo ""
    log_info "Dev release ${version} published!"
    echo ""
  fi
}

main "$@"
