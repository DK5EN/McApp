#!/bin/bash
# detect.sh - State detection functions for McApp bootstrap
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
      echo "3.13"
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

#──────────────────────────────────────────────────────────────────
# DESKTOP IMAGE DETECTION
#──────────────────────────────────────────────────────────────────

# Detect if user flashed a desktop image instead of Lite
# Desktop images have too many packages and will OOM on Pi Zero 2W
check_desktop_image() {
  # Desktop images ship xserver-xorg-core; Lite images never do
  if dpkg -l xserver-xorg-core &>/dev/null 2>&1; then
    local pkg_count
    pkg_count=$(dpkg --get-selections 2>/dev/null | wc -l)
    log_error "Desktop image detected (${pkg_count} packages installed)"
    log_error ""
    log_error "McApp requires Raspberry Pi OS Lite (headless)."
    log_error "The desktop image has too many packages and will cause"
    log_error "out-of-memory errors on Pi Zero 2W (512MB RAM)."
    log_error ""
    log_error "Please reflash with: Raspberry Pi OS Lite (64-bit, Debian Trixie)"
    log_error "Download: https://www.raspberrypi.com/software/operating-systems/"
    return 1
  fi
  return 0
}

#──────────────────────────────────────────────────────────────────
# INSTALLATION STATE DETECTION
#──────────────────────────────────────────────────────────────────

# Template placeholder used in config.json.tmpl
readonly CONFIG_TEMPLATE_MARKER="DK0XXX-99"

# Note: OLD_VENV_DIR and VENV_DIR are set by init_venv_paths() in mcapp.sh
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
# MIGRATION DETECTION (old scripts → new package layout)
#──────────────────────────────────────────────────────────────────

# Check if this is an old installation that needs migration
needs_migration() {
  # Old-style: scripts in /usr/local/bin but no package layout
  if needs_package_migration; then
    return 0
  fi

  # Old venv exists but new one doesn't
  if [[ -d "$OLD_VENV_DIR" ]] && [[ ! -d "$VENV_DIR" ]]; then
    return 0
  fi

  # systemd service points to old venv path or old C2-mc-ws.py
  local service_file="/etc/systemd/system/mcapp.service"
  if [[ -f "$service_file" ]]; then
    if grep -q "C2-mc-ws.py" "$service_file" 2>/dev/null; then
      return 0
    fi
    if grep -q "~/venv\|/home/.*/venv/bin" "$service_file" 2>/dev/null; then
      if ! grep -q "uv run mcapp" "$service_file" 2>/dev/null; then
        return 0
      fi
    fi
  fi

  return 1
}

# Check if old-style individual scripts exist but no package layout
needs_package_migration() {
  # Old C2-mc-ws.py exists in /usr/local/bin but no pyproject.toml in INSTALL_DIR
  if [[ -f "${SCRIPTS_DIR}/C2-mc-ws.py" ]] && [[ ! -f "${INSTALL_DIR}/pyproject.toml" ]]; then
    return 0
  fi
  return 1
}

# Check if old venv exists
has_old_venv() {
  [[ -d "$OLD_VENV_DIR" ]] && [[ -f "${OLD_VENV_DIR}/bin/python" ]]
}

# Note: get_real_home() is defined in mcapp.sh and available here

# Perform migration from old installation
migrate_old_installation() {
  log_info "Migrating from old installation..."

  # Step 1: Stop the service if running
  if systemctl is-active --quiet mcapp 2>/dev/null; then
    log_info "  Stopping mcapp service..."
    systemctl stop mcapp
  fi

  # Step 2: Note old files (leave in place, harmless)
  if [[ -f "${SCRIPTS_DIR}/C2-mc-ws.py" ]]; then
    log_info "  Found old scripts in ${SCRIPTS_DIR} (will be left as-is)"
  fi

  local user_home
  user_home=$(get_real_home)
  local old_venv="${user_home}/venv"
  if [[ -d "$old_venv" ]]; then
    log_info "  Found old venv at ${old_venv} (will be preserved)"
  fi
  if [[ -d "$VENV_DIR" ]]; then
    log_info "  Found old mcapp-venv at ${VENV_DIR} (will be preserved)"
  fi

  # Step 3: Backup systemd service file
  local service_file="/etc/systemd/system/mcapp.service"
  if [[ -f "$service_file" ]]; then
    log_info "  Backing up old systemd service file..."
    cp "$service_file" "${service_file}.bak.$(date +%Y%m%d%H%M%S)"
  fi

  # Step 4: Check for old config format and migrate if needed
  migrate_old_config

  log_ok "  Migration preparation complete"
  log_info "  Release tarball will be downloaded during deployment"
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
  # Old configs might be missing WEATHER_SERVICE, etc.

  # These will be handled by migrate_config() in config.sh
  # Just log what we found

  if ! jq -e '.WEATHER_SERVICE' "$CONFIG_FILE" &>/dev/null; then
    log_info "    Config missing WEATHER_SERVICE (will be added)"
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

# Get installed webapp version
# Checks version.html first (current format), then version.txt (legacy)
get_installed_webapp_version() {
  # Current format: plain text version.html
  if [[ -f "${WEBAPP_DIR}/version.html" ]]; then
    cat "${WEBAPP_DIR}/version.html" 2>/dev/null || echo "unknown"
    return
  fi

  # Legacy format: version.txt
  if [[ -f "${WEBAPP_DIR}/version.txt" ]]; then
    cat "${WEBAPP_DIR}/version.txt" 2>/dev/null || echo "unknown"
    return
  fi

  echo "not_installed"
}

# Get installed McApp version from version.html (contains full tag like v1.01.1-dev.14)
get_installed_scripts_version() {
  # Primary: version.html from deployed webapp
  if [[ -f "${WEBAPP_DIR}/version.html" ]]; then
    cat "${WEBAPP_DIR}/version.html" 2>/dev/null || echo "not_installed"
    return
  fi

  # Fallback: version.html bundled in install dir
  if [[ -f "${INSTALL_DIR}/webapp/version.html" ]]; then
    cat "${INSTALL_DIR}/webapp/version.html" 2>/dev/null || echo "not_installed"
    return
  fi

  echo "not_installed"
}

# Get remote webapp version from GitHub
# With combined tarballs, the webapp version matches the McApp release version.
# Falls back to the old raw.githubusercontent.com method for backward compat.
get_remote_webapp_version() {
  # Primary: webapp version is the same as the latest release tag
  local tag
  tag=$(curl -fsSL --connect-timeout 5 \
    "${GITHUB_API_BASE}/releases/latest" 2>/dev/null \
    | jq -r '.tag_name // empty' 2>/dev/null)

  if [[ -n "$tag" ]]; then
    echo "$tag"
    return
  fi

  # Fallback: old method via raw.githubusercontent.com
  curl -fsSL --connect-timeout 5 "${GITHUB_RAW_BASE}/webapp/version.html" 2>/dev/null \
    | grep -oP 'v\d+\.\d+\.\d+' | head -1 || echo "unknown"
}

# Get remote McApp version from GitHub Releases API
get_remote_scripts_version() {
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

# Check if venv exists and is functional (checks uv-managed .venv in active slot)
venv_is_valid() {
  # Resolve through symlink to actual slot directory
  local venv_path="${INSTALL_DIR}/.venv"
  [[ -d "$venv_path" ]] && \
    [[ -f "${venv_path}/bin/python" ]] && \
    "${venv_path}/bin/python" -c "import sys; sys.exit(0)" 2>/dev/null
}

# Check if slot layout needs migration from legacy ~/mcapp
needs_slot_migration() {
  local real_home
  real_home=$(get_real_home)
  local legacy_dir="${real_home}/mcapp"
  local slots_dir="${real_home}/mcapp-slots"

  # Legacy dir exists with code, but no slot layout yet
  [[ -d "$legacy_dir" ]] && [[ -f "${legacy_dir}/pyproject.toml" ]] && \
    [[ ! -L "${slots_dir}/current" ]]
}

#──────────────────────────────────────────────────────────────────
# VERSION REPORT (for dry-run)
#──────────────────────────────────────────────────────────────────

check_versions_report() {
  local installed_webapp
  local remote_webapp
  local installed_mcapp
  local remote_mcapp

  installed_webapp=$(get_installed_webapp_version)
  remote_webapp=$(get_remote_webapp_version)
  installed_mcapp=$(get_installed_scripts_version)
  remote_mcapp=$(get_remote_scripts_version)

  echo "  Version comparison:"
  echo ""
  echo "  Component      Installed    Remote"
  echo "  ─────────────────────────────────────"
  printf "  %-14s %-12s %s\n" "Webapp" "$installed_webapp" "$remote_webapp"
  printf "  %-14s %-12s %s\n" "McApp" "$installed_mcapp" "$remote_mcapp"
  echo ""

  # Determine what would be updated
  if [[ "$installed_webapp" == "not_installed" ]] || ! version_gte "$installed_webapp" "$remote_webapp"; then
    echo "  [DEPLOY] Would update webapp: ${installed_webapp} → ${remote_webapp}"
  else
    echo "  [DEPLOY] Webapp is current"
  fi

  if [[ "$installed_mcapp" == "not_installed" ]] || ! version_gte "$installed_mcapp" "${remote_mcapp#v}"; then
    echo "  [DEPLOY] Would update McApp: ${installed_mcapp} → ${remote_mcapp}"
  else
    echo "  [DEPLOY] McApp is current"
  fi
}
