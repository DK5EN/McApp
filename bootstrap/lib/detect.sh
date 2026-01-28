#!/bin/bash
# detect.sh - State detection functions for MCProxy bootstrap
# Determines: fresh install, incomplete, or upgrade

#──────────────────────────────────────────────────────────────────
# DEBIAN VERSION DETECTION
#──────────────────────────────────────────────────────────────────

# Get Debian codename (bookworm, trixie, etc.)
get_debian_codename() {
  if command -v lsb_release &>/dev/null; then
    lsb_release -cs 2>/dev/null
  elif [[ -f /etc/os-release ]]; then
    grep VERSION_CODENAME /etc/os-release | cut -d= -f2
  else
    echo "unknown"
  fi
}

# Get appropriate Python version for this Debian release
get_python_version() {
  case "$(get_debian_codename)" in
    trixie|sid)
      echo "3.14"
      ;;
    bookworm)
      echo "3.11"
      ;;
    bullseye)
      echo "3.9"
      ;;
    *)
      # Default to 3.11 as safe fallback
      echo "3.11"
      ;;
  esac
}

# Get the Python executable path
get_python_executable() {
  local version
  version=$(get_python_version)

  # Try versioned executable first
  if command -v "python${version}" &>/dev/null; then
    echo "python${version}"
  # Try python3 as fallback
  elif command -v python3 &>/dev/null; then
    echo "python3"
  else
    echo ""
  fi
}

# Get Caddy apt repository line for this Debian version
get_caddy_repo() {
  local codename
  codename=$(get_debian_codename)

  # Fall back to bookworm if unknown
  [[ "$codename" == "unknown" ]] && codename="bookworm"

  echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian ${codename} main"
}

#──────────────────────────────────────────────────────────────────
# INSTALLATION STATE DETECTION
#──────────────────────────────────────────────────────────────────

# Template placeholder used in config.json.tmpl
readonly CONFIG_TEMPLATE_MARKER="DK0XXX-99"

# Note: OLD_VENV_DIR and VENV_DIR are set by init_venv_paths() in mcproxy.sh
# They handle the sudo case correctly (using SUDO_USER's home, not root's)

# Detect installation state
# Returns: fresh | incomplete | upgrade | migrate
detect_install_state() {
  # No config file at all → fresh install
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "fresh"
    return
  fi

  # Config exists but still has template values → incomplete
  if config_has_template_values; then
    echo "incomplete"
    return
  fi

  # Check if this is an old installation that needs migration
  if needs_migration; then
    echo "migrate"
    return
  fi

  # Valid config exists → upgrade mode
  echo "upgrade"
}

#──────────────────────────────────────────────────────────────────
# MIGRATION DETECTION (old scripts → new bootstrap)
#──────────────────────────────────────────────────────────────────

# Check if this is an old installation that needs migration
needs_migration() {
  # Old venv exists but new one doesn't
  if [[ -d "$OLD_VENV_DIR" ]] && [[ ! -d "$VENV_DIR" ]]; then
    return 0
  fi

  # systemd service points to old venv path
  local service_file="/etc/systemd/system/mcproxy.service"
  if [[ -f "$service_file" ]]; then
    if grep -q "~/venv\|/home/.*/venv/bin" "$service_file" 2>/dev/null; then
      # Check it's not already pointing to mcproxy-venv
      if ! grep -q "mcproxy-venv" "$service_file" 2>/dev/null; then
        return 0
      fi
    fi
  fi

  return 1
}

# Check if old venv exists
has_old_venv() {
  [[ -d "$OLD_VENV_DIR" ]] && [[ -f "${OLD_VENV_DIR}/bin/python" ]]
}

# Note: get_real_home() is defined in mcproxy.sh and available here

# Perform migration from old installation
migrate_old_installation() {
  log_info "Migrating from old installation..."

  local user_home
  user_home=$(get_real_home)
  local old_venv="${user_home}/venv"
  local new_venv="${user_home}/mcproxy-venv"

  # Step 1: Stop the service if running
  if systemctl is-active --quiet mcproxy 2>/dev/null; then
    log_info "  Stopping mcproxy service..."
    systemctl stop mcproxy
  fi

  # Step 2: Handle old venv
  if [[ -d "$old_venv" ]]; then
    log_info "  Found old venv at ${old_venv}"

    # We don't migrate the old venv - we create a fresh one
    # The old one might have pip-installed packages with different versions
    log_info "  Old venv will be preserved (not deleted)"
    log_info "  Creating new venv at ${new_venv}"
  fi

  # Step 3: Update systemd service file
  local service_file="/etc/systemd/system/mcproxy.service"
  if [[ -f "$service_file" ]]; then
    log_info "  Updating systemd service file..."

    # Backup old service file
    cp "$service_file" "${service_file}.bak.$(date +%Y%m%d%H%M%S)"

    # The new service file will be written by deploy.sh
  fi

  # Step 4: Check for old config format and migrate if needed
  migrate_old_config

  log_ok "  Migration preparation complete"
  log_info "  The new venv will be created during package installation"
}

# Migrate old config format if necessary
migrate_old_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    return 0
  fi

  log_info "  Checking config compatibility..."

  # Check for old field names or missing fields
  local needs_update=false

  # Old configs might use "requests" instead of "httpx"
  # Old configs might be missing WEATHER_SERVICE, BLE_ENABLED, etc.

  # These will be handled by migrate_config() in config.sh
  # Just log what we found

  if ! jq -e '.WEATHER_SERVICE' "$CONFIG_FILE" &>/dev/null; then
    log_info "    Config missing WEATHER_SERVICE (will be added)"
    needs_update=true
  fi

  if ! jq -e '.BLE_ENABLED' "$CONFIG_FILE" &>/dev/null; then
    log_info "    Config missing BLE_ENABLED (will be added)"
    needs_update=true
  fi

  if ! jq -e '.MAX_STORAGE_SIZE_MB' "$CONFIG_FILE" &>/dev/null; then
    log_info "    Config missing MAX_STORAGE_SIZE_MB (will be added)"
    needs_update=true
  fi

  if [[ "$needs_update" == "true" ]]; then
    log_info "  Config will be updated with new fields (preserving existing values)"
  else
    log_info "  Config format is current"
  fi
}

# Check if config still has template placeholder values
config_has_template_values() {
  if ! command -v jq &>/dev/null; then
    # jq not installed yet, check with grep
    grep -q "$CONFIG_TEMPLATE_MARKER" "$CONFIG_FILE" 2>/dev/null
    return $?
  fi

  # Use jq for proper JSON parsing
  local udp_target
  udp_target=$(jq -r '.UDP_TARGET // ""' "$CONFIG_FILE" 2>/dev/null)

  [[ "$udp_target" == *"$CONFIG_TEMPLATE_MARKER"* ]] && return 0

  local callsign
  callsign=$(jq -r '.CALL_SIGN // ""' "$CONFIG_FILE" 2>/dev/null)

  [[ "$callsign" == *"$CONFIG_TEMPLATE_MARKER"* ]] && return 0

  return 1
}

#──────────────────────────────────────────────────────────────────
# VERSION DETECTION
#──────────────────────────────────────────────────────────────────

# Get installed webapp version (from version.html)
get_installed_webapp_version() {
  local version_file="${WEBAPP_DIR}/version.html"

  if [[ -f "$version_file" ]]; then
    grep -oP 'v\d+\.\d+\.\d+' "$version_file" 2>/dev/null | head -1 || echo "unknown"
  else
    echo "not_installed"
  fi
}

# Get installed Python scripts version
get_installed_scripts_version() {
  local version_file="${SCRIPTS_DIR}/mcproxy-version"

  if [[ -f "$version_file" ]]; then
    cat "$version_file"
  else
    echo "not_installed"
  fi
}

# Get remote webapp version from GitHub
get_remote_webapp_version() {
  curl -fsSL --connect-timeout 5 "${GITHUB_RAW_BASE}/webapp/version.html" 2>/dev/null \
    | grep -oP 'v\d+\.\d+\.\d+' | head -1 || echo "unknown"
}

# Get remote scripts version from GitHub
get_remote_scripts_version() {
  curl -fsSL --connect-timeout 5 "${GITHUB_RAW_BASE}/version" 2>/dev/null || echo "unknown"
}

# Compare semantic versions
# Returns: 0 if v1 >= v2, 1 if v1 < v2
version_gte() {
  local v1="$1"
  local v2="$2"

  # Strip 'v' prefix if present
  v1="${v1#v}"
  v2="${v2#v}"

  # Use sort -V for version comparison
  [[ "$(printf '%s\n%s' "$v1" "$v2" | sort -V | head -1)" == "$v2" ]]
}

#──────────────────────────────────────────────────────────────────
# SERVICE STATE DETECTION
#──────────────────────────────────────────────────────────────────

# Check if a systemd service is active
service_is_active() {
  local service="$1"
  systemctl is-active --quiet "$service" 2>/dev/null
}

# Check if a systemd service is enabled
service_is_enabled() {
  local service="$1"
  systemctl is-enabled --quiet "$service" 2>/dev/null
}

# Check if venv exists and is functional
venv_is_valid() {
  [[ -d "$VENV_DIR" ]] && \
    [[ -f "${VENV_DIR}/bin/python" ]] && \
    "${VENV_DIR}/bin/python" -c "import sys; sys.exit(0)" 2>/dev/null
}

#──────────────────────────────────────────────────────────────────
# VERSION REPORT (for dry-run)
#──────────────────────────────────────────────────────────────────

check_versions_report() {
  local installed_webapp
  local remote_webapp
  local installed_scripts
  local remote_scripts

  installed_webapp=$(get_installed_webapp_version)
  remote_webapp=$(get_remote_webapp_version)
  installed_scripts=$(get_installed_scripts_version)
  remote_scripts=$(get_remote_scripts_version)

  echo "  Version comparison:"
  echo ""
  echo "  Component      Installed    Remote"
  echo "  ─────────────────────────────────────"
  printf "  %-14s %-12s %s\n" "Webapp" "$installed_webapp" "$remote_webapp"
  printf "  %-14s %-12s %s\n" "Scripts" "$installed_scripts" "$remote_scripts"
  echo ""

  # Determine what would be updated
  if [[ "$installed_webapp" == "not_installed" ]] || ! version_gte "$installed_webapp" "$remote_webapp"; then
    echo "  [DEPLOY] Would update webapp: ${installed_webapp} → ${remote_webapp}"
  else
    echo "  [DEPLOY] Webapp is current"
  fi

  if [[ "$installed_scripts" == "not_installed" ]] || ! version_gte "$installed_scripts" "$remote_scripts"; then
    echo "  [DEPLOY] Would update scripts: ${installed_scripts} → ${remote_scripts}"
  else
    echo "  [DEPLOY] Scripts are current"
  fi
}
