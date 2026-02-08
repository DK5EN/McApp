#!/bin/bash
# deploy.sh - Application deployment for McApp bootstrap
# Handles: release tarball download, webapp, version management, systemd

#──────────────────────────────────────────────────────────────────
# MAIN DEPLOYMENT FUNCTION
#──────────────────────────────────────────────────────────────────

deploy_app() {
  local force="${1:-false}"
  local dev_mode="${2:-false}"

  deploy_release "$force" "$dev_mode"
  deploy_webapp "$force"
  setup_python_env
  migrate_config
}

#──────────────────────────────────────────────────────────────────
# RELEASE TARBALL DEPLOYMENT
#──────────────────────────────────────────────────────────────────

# Query GitHub Releases API for the latest stable release tag
get_latest_release_version() {
  local tag
  tag=$(curl -fsSL --connect-timeout 5 \
    "${GITHUB_API_BASE}/releases/latest" 2>/dev/null \
    | jq -r '.tag_name // empty' 2>/dev/null)

  if [[ -n "$tag" ]]; then
    echo "$tag"
  else
    echo "unknown"
  fi
}

# Query GitHub Releases API for the latest pre-release tag
get_latest_prerelease_version() {
  local tag
  tag=$(curl -fsSL --connect-timeout 5 \
    "${GITHUB_API_BASE}/releases" 2>/dev/null \
    | jq -r '[.[] | select(.prerelease)][0].tag_name // empty' 2>/dev/null)

  if [[ -n "$tag" ]]; then
    echo "$tag"
  else
    echo "unknown"
  fi
}

# Read installed version from pyproject.toml in INSTALL_DIR
get_installed_mcapp_version() {
  local pyproject="${INSTALL_DIR}/pyproject.toml"

  if [[ -f "$pyproject" ]]; then
    grep -oP '^version\s*=\s*"\K[^"]+' "$pyproject" 2>/dev/null || echo "not_installed"
  else
    echo "not_installed"
  fi
}

deploy_release() {
  local force="${1:-false}"
  local dev_mode="${2:-false}"

  log_info "Checking McApp release deployment..."

  local installed_version
  local remote_version

  installed_version=$(get_installed_mcapp_version)

  if [[ "$dev_mode" == "true" ]]; then
    remote_version=$(get_latest_prerelease_version)
    log_info "  Mode: development (pre-release)"
  else
    remote_version=$(get_latest_release_version)
  fi

  log_info "  Installed: ${installed_version}"
  log_info "  Remote:    ${remote_version}"

  # Decide if update needed
  if [[ "$force" == "true" ]]; then
    log_info "  Force mode: reinstalling release"
  elif [[ "$installed_version" == "not_installed" ]]; then
    log_info "  McApp not installed, downloading..."
  elif [[ "$remote_version" == "unknown" ]]; then
    log_warn "  Cannot check remote version, skipping update"
    return 0
  elif version_gte "$installed_version" "${remote_version#v}"; then
    log_info "  McApp is up to date"
    return 0
  else
    log_info "  Updating McApp: ${installed_version} → ${remote_version}"
  fi

  download_and_install_release "$remote_version"
}

download_and_install_release() {
  local version="${1:-}"

  # If no version given, fetch latest
  if [[ -z "$version" || "$version" == "unknown" ]]; then
    version=$(get_latest_release_version)
    if [[ "$version" == "unknown" ]]; then
      log_error "  Cannot determine latest release version"
      return 1
    fi
  fi

  log_info "  Downloading McApp ${version}..."

  local tarball_name="mcapp-${version}.tar.gz"
  local checksum_name="mcapp-${version}.tar.gz.sha256"
  local release_url="https://github.com/${GITHUB_REPO}/releases/download/${version}"
  local tmp_dir
  tmp_dir=$(mktemp -d)

  # Download release tarball
  if ! curl -fsSL -o "${tmp_dir}/${tarball_name}" "${release_url}/${tarball_name}"; then
    log_error "  Failed to download release tarball"
    rm -rf "$tmp_dir"
    return 1
  fi

  # Download and verify SHA256 checksum
  if curl -fsSL -o "${tmp_dir}/${checksum_name}" "${release_url}/${checksum_name}" 2>/dev/null; then
    log_info "  Verifying SHA256 checksum..."
    if ! (cd "$tmp_dir" && sha256sum -c "$checksum_name" --quiet 2>/dev/null); then
      log_error "  Checksum verification failed!"
      rm -rf "$tmp_dir"
      return 1
    fi
    log_ok "  Checksum verified"
  else
    log_warn "  No checksum file available, skipping verification"
  fi

  # Backup existing installation if present
  if [[ -d "$INSTALL_DIR" ]] && [[ -f "${INSTALL_DIR}/pyproject.toml" ]]; then
    local backup_dir="${INSTALL_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$INSTALL_DIR" "$backup_dir"
    log_info "  Backed up existing installation to ${backup_dir}"
  fi

  # Create install directory
  mkdir -p "$INSTALL_DIR"

  # Extract tarball with --strip-components=1 to remove top-level dir
  tar -xzf "${tmp_dir}/${tarball_name}" -C "$INSTALL_DIR" --strip-components=1 --warning=no-unknown-keyword

  # Set ownership to the real user (not root)
  local run_user="${SUDO_USER:-$(whoami)}"
  chown -R "$run_user:$run_user" "$INSTALL_DIR"

  # Create runtime directories required by systemd ReadWritePaths
  mkdir -p /var/lib/mcapp
  chown "$run_user:$run_user" /var/lib/mcapp
  mkdir -p /var/log/mcapp
  chown "$run_user:$run_user" /var/log/mcapp

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  McApp ${version} deployed to ${INSTALL_DIR}"
}

#──────────────────────────────────────────────────────────────────
# WEBAPP DEPLOYMENT
#──────────────────────────────────────────────────────────────────

deploy_webapp() {
  local force="${1:-false}"

  log_info "Checking webapp deployment..."

  # New flow: if the tarball included webapp/, use it directly
  if [[ -d "${INSTALL_DIR}/webapp" ]] && [[ -f "${INSTALL_DIR}/webapp/index.html" ]]; then
    deploy_webapp_from_tarball "$force"
  else
    # Fallback: old tarball without bundled webapp — download separately
    log_info "  No bundled webapp in tarball, falling back to download"
    deploy_webapp_download "$force"
  fi
}

# Deploy webapp from the bundled webapp/ directory in the release tarball
deploy_webapp_from_tarball() {
  local force="${1:-false}"

  local installed_version
  local tarball_version

  installed_version=$(get_installed_webapp_version)

  # Read version from the bundled webapp
  if [[ -f "${INSTALL_DIR}/webapp/version.html" ]]; then
    tarball_version=$(cat "${INSTALL_DIR}/webapp/version.html")
  else
    tarball_version="unknown"
  fi

  log_info "  Installed: ${installed_version}"
  log_info "  Bundled:   ${tarball_version}"

  if [[ "$force" != "true" ]] && [[ "$installed_version" != "not_installed" ]] && \
     [[ "$tarball_version" != "unknown" ]] && version_gte "$installed_version" "$tarball_version"; then
    log_info "  Webapp is up to date"
    return 0
  fi

  log_info "  Deploying bundled webapp..."

  # Ensure webapp directory exists
  mkdir -p "$WEBAPP_DIR"

  # Backup existing webapp
  if [[ -d "$WEBAPP_DIR" ]] && [[ -f "${WEBAPP_DIR}/index.html" ]]; then
    local backup_dir="${WEBAPP_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$WEBAPP_DIR" "$backup_dir"
    log_info "  Backed up existing webapp to ${backup_dir}"
  fi

  # Copy from tarball to webapp serve dir
  cp -a "${INSTALL_DIR}/webapp/." "$WEBAPP_DIR/"

  # Set permissions
  chown -R www-data:www-data "$WEBAPP_DIR"
  chmod -R 755 "$WEBAPP_DIR"

  log_ok "  Webapp deployed from tarball to ${WEBAPP_DIR}"
}

# Fallback: download webapp separately (backward compat with old releases)
deploy_webapp_download() {
  local force="${1:-false}"

  local installed_version
  local remote_version

  installed_version=$(get_installed_webapp_version)
  remote_version=$(get_remote_webapp_version)

  log_info "  Installed: ${installed_version}"
  log_info "  Remote:    ${remote_version}"

  # Decide if update needed
  if [[ "$force" == "true" ]]; then
    log_info "  Force mode: reinstalling webapp"
  elif [[ "$installed_version" == "not_installed" ]]; then
    log_info "  Webapp not installed, downloading..."
  elif [[ "$remote_version" == "unknown" ]]; then
    log_warn "  Cannot check remote version, skipping update"
    return 0
  elif version_gte "$installed_version" "$remote_version"; then
    log_info "  Webapp is up to date"
    return 0
  else
    log_info "  Updating webapp: ${installed_version} → ${remote_version}"
  fi

  download_webapp
}

download_webapp() {
  log_info "  Downloading webapp..."

  local webapp_url="${GITHUB_RAW_BASE}/webapp/webapp.tar.gz"
  local checksum_url="${GITHUB_RAW_BASE}/webapp/webapp.tar.gz.sha256"
  local tmp_dir
  tmp_dir=$(mktemp -d)

  # Download webapp archive
  if ! curl -fsSL -o "${tmp_dir}/webapp.tar.gz" "$webapp_url"; then
    log_error "  Failed to download webapp"
    rm -rf "$tmp_dir"
    return 1
  fi

  # Download and verify checksum (if available)
  if curl -fsSL -o "${tmp_dir}/webapp.tar.gz.sha256" "$checksum_url" 2>/dev/null; then
    log_info "  Verifying checksum..."
    if ! (cd "$tmp_dir" && sha256sum -c webapp.tar.gz.sha256 --quiet 2>/dev/null); then
      log_error "  Checksum verification failed!"
      rm -rf "$tmp_dir"
      return 1
    fi
    log_info "  Checksum verified"
  else
    log_warn "  No checksum available, skipping verification"
  fi

  # Ensure webapp directory exists
  mkdir -p "$WEBAPP_DIR"

  # Backup existing webapp
  if [[ -d "$WEBAPP_DIR" ]] && [[ -f "${WEBAPP_DIR}/index.html" ]]; then
    local backup_dir="${WEBAPP_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$WEBAPP_DIR" "$backup_dir"
    log_info "  Backed up existing webapp to ${backup_dir}"
  fi

  # Extract webapp
  tar -xzf "${tmp_dir}/webapp.tar.gz" -C "$WEBAPP_DIR" --strip-components=1 --warning=no-unknown-keyword

  # Set permissions
  chown -R www-data:www-data "$WEBAPP_DIR"
  chmod -R 755 "$WEBAPP_DIR"

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  Webapp deployed to ${WEBAPP_DIR}"
}

#──────────────────────────────────────────────────────────────────
# PYTHON ENVIRONMENT (uv sync)
#──────────────────────────────────────────────────────────────────

setup_python_env() {
  log_info "Setting up Python environment with uv sync..."

  if [[ ! -f "${INSTALL_DIR}/pyproject.toml" ]]; then
    log_error "  No pyproject.toml found in ${INSTALL_DIR}"
    return 1
  fi

  # Resolve uv binary path (may not be in root's PATH)
  local run_user="${SUDO_USER:-$(whoami)}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)
  local uv_bin="${run_home}/.local/bin/uv"

  if [[ ! -x "$uv_bin" ]]; then
    # Fallback: check system PATH
    uv_bin=$(command -v uv 2>/dev/null || true)
    if [[ -z "$uv_bin" ]]; then
      log_error "  uv not found - install it first"
      return 1
    fi
  fi

  # Run uv sync as the real user (not root)
  if [[ "$run_user" != "root" ]]; then
    sudo -u "$run_user" bash -c "cd '${INSTALL_DIR}' && '${uv_bin}' sync"
  else
    (cd "$INSTALL_DIR" && "$uv_bin" sync)
  fi

  if [[ $? -eq 0 ]]; then
    log_ok "  Python environment ready (${INSTALL_DIR}/.venv)"
  else
    log_error "  uv sync failed"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# SERVICE ACTIVATION
#──────────────────────────────────────────────────────────────────

activate_services() {
  log_info "Activating services..."

  configure_systemd_service
  enable_and_start_services
}

configure_systemd_service() {
  log_info "  Configuring systemd services..."

  local run_user="${SUDO_USER:-root}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)

  # --- mcapp.service ---
  local mcapp_service="/etc/systemd/system/mcapp.service"
  local template_dir

  # Find template directory
  if [[ -d "${INSTALL_DIR}/bootstrap/templates" ]]; then
    template_dir="${INSTALL_DIR}/bootstrap/templates"
  elif [[ -d "${SCRIPT_DIR}/templates" ]]; then
    template_dir="${SCRIPT_DIR}/templates"
  else
    log_error "  Cannot find service templates"
    return 1
  fi

  # Render mcapp.service from template
  sed -e "s|{{USER}}|${run_user}|g" \
      -e "s|{{HOME}}|${run_home}|g" \
      "${template_dir}/mcapp.service" > "$mcapp_service"

  log_info "  mcapp.service configured"

  # --- mcapp-ble.service (optional) ---
  if [[ -f "${template_dir}/mcapp-ble.service" ]]; then
    local ble_service="/etc/systemd/system/mcapp-ble.service"

    # Read BLE API key from config if available
    local ble_api_key=""
    if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
      ble_api_key=$(jq -r '.BLE_API_KEY // ""' "$CONFIG_FILE" 2>/dev/null)
    fi

    sed -e "s|{{USER}}|${run_user}|g" \
        -e "s|{{HOME}}|${run_home}|g" \
        -e "s|{{BLE_API_KEY}}|${ble_api_key}|g" \
        "${template_dir}/mcapp-ble.service" > "$ble_service"

    log_info "  mcapp-ble.service configured"
  fi

  systemctl daemon-reload
}

enable_and_start_services() {
  log_info "  Enabling and starting services..."

  local -a services=("lighttpd" "mcapp" "mcapp-ble")
  local failed=false

  for svc in "${services[@]}"; do
    # Enable service
    if ! systemctl enable "$svc" 2>/dev/null; then
      log_warn "  Failed to enable ${svc}"
      failed=true
      continue
    fi

    # Restart service (or start if not running)
    if systemctl is-active --quiet "$svc"; then
      if ! systemctl restart "$svc" 2>/dev/null; then
        log_warn "  Failed to restart ${svc}"
        failed=true
      fi
    else
      if ! systemctl start "$svc" 2>/dev/null; then
        log_warn "  Failed to start ${svc}"
        failed=true
      fi
    fi
  done

  if [[ "$failed" == "true" ]]; then
    log_warn "  Some services failed to start - check logs"
  else
    log_ok "  All services enabled and started"
  fi
}
