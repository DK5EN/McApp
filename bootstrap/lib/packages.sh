#!/bin/bash
# packages.sh - Package management for McApp bootstrap
# Handles: apt packages, uv, lighttpd

#──────────────────────────────────────────────────────────────────
# MAIN PACKAGE INSTALLATION
#──────────────────────────────────────────────────────────────────

install_packages() {
  install_apt_deps
  install_uv
  install_lighttpd
}

#──────────────────────────────────────────────────────────────────
# TEMPORARY SWAP FOR APT OPERATIONS
#──────────────────────────────────────────────────────────────────

readonly APT_SWAP_FILE="/var/tmp/.mcapp-apt-swap"

# Create temporary 256MB swap if none is active.
# Uses /var/tmp (not /tmp which may be tmpfs after system.sh Phase 3).
# Soft failure — logs warning and continues if swap creation fails.
ensure_apt_swap() {
  # Skip if swap is already active (system swap or previous run)
  if [[ $(swapon --show --noheadings 2>/dev/null | wc -l) -gt 0 ]]; then
    log_info "  Swap already active, skipping temporary swap"
    return 0
  fi

  log_info "  Creating temporary 256MB swap for apt operations..."
  if dd if=/dev/zero of="$APT_SWAP_FILE" bs=1M count=256 status=none 2>/dev/null \
     && chmod 600 "$APT_SWAP_FILE" \
     && mkswap "$APT_SWAP_FILE" >/dev/null 2>&1 \
     && swapon "$APT_SWAP_FILE" 2>/dev/null; then
    log_ok "  Temporary swap activated"
  else
    log_warn "  Could not create temporary swap (continuing without it)"
    rm -f "$APT_SWAP_FILE" 2>/dev/null
  fi
}

# Remove temporary swap file (idempotent)
remove_apt_swap() {
  if swapon --show --noheadings 2>/dev/null | grep -q "$APT_SWAP_FILE"; then
    swapoff "$APT_SWAP_FILE" 2>/dev/null
  fi
  rm -f "$APT_SWAP_FILE" 2>/dev/null
}

#──────────────────────────────────────────────────────────────────
# APT DEPENDENCIES
#──────────────────────────────────────────────────────────────────

install_apt_deps() {
  log_info "Installing system dependencies..."

  # Update package lists and upgrade installed packages
  apt-get update -qq

  # Temporary swap to prevent OOM during large upgrades (Pi Zero 2W has 512MB)
  ensure_apt_swap

  log_info "Upgrading installed packages..."
  DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
  log_ok "  System packages upgraded"

  # Configure locale AFTER upgrade (glibc/locales upgrade invalidates generated locales)
  configure_locale

  # Core dependencies (no python3-venv or python3-pip — uv handles everything)
  local -a packages=(
    # Essential tools
    "curl"
    "jq"
    "bc"
    "screen"
    "git"

    # Python runtime (uv manages venvs and packages)
    "python3"

    # BlueZ for BLE support (only if running BLE service locally)
    "bluez"
    "bluetooth"

    # For D-Bus (no longer needed - dbus-next removed from main package)
    # "libdbus-1-dev"  # Only needed by standalone BLE service

    # For SSL certificates
    "ca-certificates"
    "gnupg"
  )

  # Install all packages
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"

  # Clean up temporary swap
  remove_apt_swap

  log_ok "  System dependencies installed"
}

#──────────────────────────────────────────────────────────────────
# UV PACKAGE MANAGER
#──────────────────────────────────────────────────────────────────

install_uv() {
  log_info "Installing uv package manager..."

  # Check if uv is already available for the real user
  local run_user="${SUDO_USER:-$(whoami)}"
  local run_home
  run_home=$(getent passwd "$run_user" | cut -d: -f6)

  if [[ -x "${run_home}/.local/bin/uv" ]]; then
    local uv_version
    uv_version=$("${run_home}/.local/bin/uv" --version 2>/dev/null | head -1)
    log_info "  uv already installed: ${uv_version}"
    return 0
  fi

  # Install uv as the real user (not root)
  if [[ "$run_user" != "root" ]]; then
    su - "$run_user" -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi

  # Add to PATH for current session
  export PATH="${run_home}/.local/bin:$PATH"

  # Verify installation
  if [[ -x "${run_home}/.local/bin/uv" ]]; then
    local uv_version
    uv_version=$("${run_home}/.local/bin/uv" --version 2>/dev/null | head -1)
    log_ok "  uv installed: ${uv_version}"
  else
    log_error "  Failed to install uv"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# LIGHTTPD (Static File Server)
#──────────────────────────────────────────────────────────────────

install_lighttpd() {
  log_info "Installing lighttpd..."

  # Check if already installed
  if command -v lighttpd &>/dev/null; then
    log_info "  lighttpd already installed"
    configure_lighttpd
    return 0
  fi

  apt-get install -y -qq lighttpd

  configure_lighttpd

  log_ok "  lighttpd installed and configured"
}

configure_lighttpd() {
  log_info "  Configuring lighttpd..."

  local conf_file="/etc/lighttpd/conf-available/99-mcapp.conf"

  # Check if already configured (must include mod_proxy for API/SSE reverse proxy)
  if [[ -f "$conf_file" ]] && grep -q "mod_proxy" "$conf_file" 2>/dev/null; then
    log_info "  lighttpd already configured"
    return 0
  fi

  # Create McApp-specific config
  cat > "$conf_file" << 'EOF'
# McApp SPA rewrite + redirect + API proxy configuration
# Enables Vue.js SPA routing, root redirect, and reverse proxy to FastAPI

server.modules += ("mod_rewrite", "mod_redirect", "mod_proxy")

# Enable streaming for SSE (Server-Sent Events) — prevents buffering
server.stream-response-body = 2

# Redirect root to webapp
$HTTP["url"] == "/" {
    url.redirect = ("^/$" => "/webapp/")
}

# SPA rewrite for Vue.js router (HTML5 history mode)
$HTTP["url"] =~ "^/webapp/" {
    url.rewrite-if-not-file = (
        "^/webapp/(.*)$" => "/webapp/index.html"
    )
}

# Reverse proxy: SSE event stream + REST API → FastAPI on port 2981
$HTTP["url"] =~ "^/events" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}

$HTTP["url"] =~ "^/api/" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}

$HTTP["url"] =~ "^/health" {
    proxy.server = ("" => (("host" => "127.0.0.1", "port" => 2981)))
}
EOF

  # Enable the config (idempotent)
  if [[ ! -L /etc/lighttpd/conf-enabled/99-mcapp.conf ]]; then
    ln -sf "$conf_file" /etc/lighttpd/conf-enabled/99-mcapp.conf
  fi

  # Test config before reloading
  if lighttpd -t -f /etc/lighttpd/lighttpd.conf 2>/dev/null; then
    systemctl reload lighttpd 2>/dev/null || systemctl restart lighttpd
  else
    log_warn "  lighttpd config test failed"
  fi
}
