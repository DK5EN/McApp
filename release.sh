#!/bin/bash
# release.sh - Unified release builder for McApp
#
# Fully automatic — just run ./release.sh (no arguments needed).
# Detects the current branch and handles everything:
#
#   main branch       → Production release (bumps version, draft)
#   development branch → Dev pre-release (auto-numbered, published)
#
# Produces a single combined tarball with backend + webapp.
# Includes automatic cleanup on failure (rollback tag, release, artifacts).

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WEBAPP_DIR="$(cd "${SCRIPT_DIR}/../webapp" && pwd)"
readonly GITHUB_REPO="DK5EN/McApp"

# Colors
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

log_info() { echo -e "${GREEN}==>${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

#──────────────────────────────────────────────────────────────────
# CLEANUP STATE (used by trap handler)
#──────────────────────────────────────────────────────────────────

_CLEANUP_TAG=""           # git tag to delete on failure
_CLEANUP_RELEASE=""       # GitHub release to delete on failure
_CLEANUP_TARBALL=""       # tarball file to remove
_CLEANUP_CHECKSUM=""      # checksum file to remove
_CLEANUP_TMPDIR=""        # temp staging dir to remove
_RELEASE_SUCCESS=false    # set to true at the very end

on_failure() {
  if [[ "$_RELEASE_SUCCESS" == true ]]; then
    return
  fi

  echo "" >&2
  log_error "Release failed — rolling back..."

  # Remove local artifacts
  if [[ -n "$_CLEANUP_TARBALL" && -f "${SCRIPT_DIR}/${_CLEANUP_TARBALL}" ]]; then
    rm -f "${SCRIPT_DIR}/${_CLEANUP_TARBALL}"
    log_warn "  Removed ${_CLEANUP_TARBALL}"
  fi
  if [[ -n "$_CLEANUP_CHECKSUM" && -f "${SCRIPT_DIR}/${_CLEANUP_CHECKSUM}" ]]; then
    rm -f "${SCRIPT_DIR}/${_CLEANUP_CHECKSUM}"
    log_warn "  Removed ${_CLEANUP_CHECKSUM}"
  fi

  # Remove temp staging directory
  if [[ -n "$_CLEANUP_TMPDIR" && -d "$_CLEANUP_TMPDIR" ]]; then
    rm -rf "$_CLEANUP_TMPDIR"
    log_warn "  Removed staging directory"
  fi

  # Delete GitHub release (this also removes the remote tag it created)
  if [[ -n "$_CLEANUP_RELEASE" ]]; then
    if gh release view "$_CLEANUP_RELEASE" --repo "$GITHUB_REPO" &>/dev/null; then
      gh release delete "$_CLEANUP_RELEASE" --repo "$GITHUB_REPO" --yes &>/dev/null
      log_warn "  Deleted GitHub release ${_CLEANUP_RELEASE}"
    fi
  fi

  # Delete local git tag
  if [[ -n "$_CLEANUP_TAG" ]]; then
    if git -C "$SCRIPT_DIR" tag -l "$_CLEANUP_TAG" | grep -q .; then
      git -C "$SCRIPT_DIR" tag -d "$_CLEANUP_TAG" &>/dev/null
      log_warn "  Deleted local tag ${_CLEANUP_TAG}"
    fi
    # Delete remote tag (may have been pushed by gh release create)
    if git -C "$SCRIPT_DIR" ls-remote --tags origin "refs/tags/${_CLEANUP_TAG}" | grep -q .; then
      git -C "$SCRIPT_DIR" push origin --delete "$_CLEANUP_TAG" &>/dev/null 2>&1 || true
      log_warn "  Deleted remote tag ${_CLEANUP_TAG}"
    fi
  fi

  log_error "Rollback complete."
}

trap on_failure EXIT

#──────────────────────────────────────────────────────────────────
# PRE-FLIGHT CLEANUP
#──────────────────────────────────────────────────────────────────

cleanup_stale_artifacts() {
  local found=false

  for f in "${SCRIPT_DIR}"/mcapp-*.tar.gz "${SCRIPT_DIR}"/mcapp-*.tar.gz.sha256 \
           "${SCRIPT_DIR}"/mcproxy-*.tar.gz "${SCRIPT_DIR}"/mcproxy-*.tar.gz.sha256 \
           "${SCRIPT_DIR}"/mcadvchat-*.tar.gz "${SCRIPT_DIR}"/mcadvchat-*.tar.gz.sha256; do
    if [[ -f "$f" ]]; then
      rm -f "$f"
      log_warn "Removed stale artifact: $(basename "$f")"
      found=true
    fi
  done

  if [[ "$found" == true ]]; then
    echo "" >&2
  fi
}

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

# Read current version from pyproject.toml (e.g., "1.01.0")
read_pyproject_version() {
  grep -oE '^version = "([^"]+)"' "${SCRIPT_DIR}/pyproject.toml" \
    | head -1 | sed 's/version = "//;s/"//'
}

# Production: auto-bump minor version (1.01.0 → v1.02.0)
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
  echo "$version" > "${WEBAPP_DIR}/dist/version.html"

  log_info "  Webapp built successfully (version.html: ${version})"
}

#──────────────────────────────────────────────────────────────────
# TARBALL BUILD
#──────────────────────────────────────────────────────────────────

build_tarball() {
  local version="$1"
  local tarball_name="mcapp-${version}.tar.gz"
  local prefix="mcapp-${version}"
  local tmp_dir
  tmp_dir=$(mktemp -d)
  local staging="${tmp_dir}/${prefix}"

  # Register for cleanup on failure
  _CLEANUP_TMPDIR="$tmp_dir"
  _CLEANUP_TARBALL="$tarball_name"

  log_info "Building release tarball..."

  mkdir -p "$staging"

  # Copy project files
  cp "${SCRIPT_DIR}/pyproject.toml" "$staging/"
  cp "${SCRIPT_DIR}/uv.lock" "$staging/" 2>/dev/null || log_warn "  No uv.lock found (will be generated by uv sync)"
  cp "${SCRIPT_DIR}/config.sample.json" "$staging/" 2>/dev/null || true

  # src/mcapp/ package
  mkdir -p "${staging}/src/mcapp/commands"
  find "${SCRIPT_DIR}/src/mcapp" -name '*.py' -not -path '*/__pycache__/*' | while read -r f; do
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
  _CLEANUP_TMPDIR=""

  log_info "  Built ${tarball_name}"
  echo "$tarball_name"
}

generate_checksum() {
  local tarball="$1"
  local checksum_file="${tarball}.sha256"

  # Register for cleanup on failure
  _CLEANUP_CHECKSUM="$checksum_file"

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

  _CLEANUP_TAG="$version"

  log_info "  Committed and tagged ${version}"
}

tag_dev() {
  local version="$1"

  log_info "Creating lightweight tag ${version}..."

  git -C "$SCRIPT_DIR" tag "$version"

  _CLEANUP_TAG="$version"

  log_info "  Tagged ${version}"
}

upload_production() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub draft release ${version}..."

  local -a assets=("${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}")

  # gh release create also pushes the tag to the remote
  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "McApp ${version}" \
    --notes "McApp release ${version}" \
    --draft \
    "${assets[@]}"

  _CLEANUP_RELEASE="$version"

  log_info "  Draft release created: ${version}"
}

upload_dev() {
  local version="$1"
  local tarball="$2"
  local checksum="$3"

  log_info "Creating GitHub pre-release ${version}..."

  local -a assets=("${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}")

  # gh release create also pushes the tag to the remote
  gh release create "$version" \
    --repo "$GITHUB_REPO" \
    --title "McApp ${version} (dev)" \
    --notes "McApp development pre-release ${version}" \
    --prerelease \
    "${assets[@]}"

  _CLEANUP_RELEASE="$version"

  log_info "  Pre-release published: ${version}"
}

#──────────────────────────────────────────────────────────────────
# FINAL CLEANUP (success path)
#──────────────────────────────────────────────────────────────────

cleanup_artifacts() {
  local tarball="$1"
  local checksum="$2"

  rm -f "${SCRIPT_DIR}/${tarball}" "${SCRIPT_DIR}/${checksum}"

  # Clear trap state so on_failure doesn't try to remove them again
  _CLEANUP_TARBALL=""
  _CLEANUP_CHECKSUM=""
  _CLEANUP_TAG=""
  _CLEANUP_RELEASE=""

  log_info "  Cleaned up local artifacts"
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────

main() {
  echo ""
  echo "  McApp Release Builder"
  echo "  ====================="
  echo ""

  validate_tools

  local mode
  mode=$(detect_mode)

  log_info "Mode: ${mode} (detected from branch)"
  echo ""

  # Remove stale artifacts from previous (failed) runs
  cleanup_stale_artifacts

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

    _RELEASE_SUCCESS=true

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

    # Step 5: Upload as pre-release (also pushes the tag to remote)
    upload_dev "$version" "$tarball" "$checksum"

    # Step 6: Cleanup
    cleanup_artifacts "$tarball" "$checksum"

    _RELEASE_SUCCESS=true

    echo ""
    log_info "Dev release ${version} published!"
    echo ""
  fi
}

main "$@"
