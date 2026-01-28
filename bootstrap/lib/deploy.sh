#!/bin/bash
# deploy.sh - Application deployment for MCProxy bootstrap
# Handles: webapp download, Python scripts, version management

#──────────────────────────────────────────────────────────────────
# MAIN DEPLOYMENT FUNCTION
#──────────────────────────────────────────────────────────────────

deploy_app() {
  local force="${1:-false}"

  deploy_webapp "$force"
  deploy_scripts "$force"
  migrate_config
}

#──────────────────────────────────────────────────────────────────
# WEBAPP DEPLOYMENT
#──────────────────────────────────────────────────────────────────

deploy_webapp() {
  local force="${1:-false}"

  log_info "Checking webapp deployment..."

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
  tar -xzf "${tmp_dir}/webapp.tar.gz" -C "$WEBAPP_DIR" --strip-components=1

  # Set permissions
  chown -R www-data:www-data "$WEBAPP_DIR"
  chmod -R 755 "$WEBAPP_DIR"

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  Webapp deployed to ${WEBAPP_DIR}"
}

#──────────────────────────────────────────────────────────────────
# PYTHON SCRIPTS DEPLOYMENT
#──────────────────────────────────────────────────────────────────

deploy_scripts() {
  local force="${1:-false}"

  log_info "Checking Python scripts deployment..."

  local installed_version
  local remote_version

  installed_version=$(get_installed_scripts_version)
  remote_version=$(get_remote_scripts_version)

  log_info "  Installed: ${installed_version}"
  log_info "  Remote:    ${remote_version}"

  # Decide if update needed
  if [[ "$force" == "true" ]]; then
    log_info "  Force mode: reinstalling scripts"
  elif [[ "$installed_version" == "not_installed" ]]; then
    log_info "  Scripts not installed, downloading..."
  elif [[ "$remote_version" == "unknown" ]]; then
    log_warn "  Cannot check remote version, skipping update"
    return 0
  elif version_gte "$installed_version" "$remote_version"; then
    log_info "  Scripts are up to date"
    return 0
  else
    log_info "  Updating scripts: ${installed_version} → ${remote_version}"
  fi

  download_scripts
}

download_scripts() {
  log_info "  Downloading Python scripts..."

  # List of Python files to download
  local -a script_files=(
    "C2-mc-ws.py"
    "message_storage.py"
    "udp_handler.py"
    "websocket_handler.py"
    "ble_handler.py"
    "command_handler.py"
  )

  local tmp_dir
  tmp_dir=$(mktemp -d)

  # Download each script
  for script in "${script_files[@]}"; do
    local script_url="${GITHUB_RAW_BASE}/${script}"

    if ! curl -fsSL -o "${tmp_dir}/${script}" "$script_url"; then
      log_error "  Failed to download ${script}"
      rm -rf "$tmp_dir"
      return 1
    fi
  done

  # Backup existing scripts
  for script in "${script_files[@]}"; do
    if [[ -f "${SCRIPTS_DIR}/${script}" ]]; then
      cp "${SCRIPTS_DIR}/${script}" "${SCRIPTS_DIR}/${script}.bak"
    fi
  done

  # Install scripts
  for script in "${script_files[@]}"; do
    install -m 755 "${tmp_dir}/${script}" "${SCRIPTS_DIR}/${script}"
  done

  # Write version file
  local remote_version
  remote_version=$(get_remote_scripts_version)
  echo "$remote_version" > "${SCRIPTS_DIR}/mcproxy-version"

  # Cleanup
  rm -rf "$tmp_dir"

  log_ok "  Python scripts deployed to ${SCRIPTS_DIR}"
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
  log_info "  Configuring systemd service..."

  local service_file="/etc/systemd/system/mcproxy.service"

  # Get the real user (not root when using sudo)
  local run_user="${SUDO_USER:-root}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)

  cat > "$service_file" << EOF
[Unit]
Description=MCProxy - MeshCom Message Proxy
Documentation=https://github.com/DK5EN/McAdvChat
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
Type=simple
User=${run_user}
Group=${run_user}
WorkingDirectory=${SCRIPTS_DIR}
Environment="PATH=${run_home}/mcproxy-venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="MCADVCHAT_ENV=prod"
ExecStart=${run_home}/mcproxy-venv/bin/python ${SCRIPTS_DIR}/C2-mc-ws.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/var/log/mcproxy ${CONFIG_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload

  log_info "  systemd service configured"
}

enable_and_start_services() {
  log_info "  Enabling and starting services..."

  local -a services=("caddy" "lighttpd" "mcproxy")
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
