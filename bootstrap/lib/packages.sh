#!/bin/bash
# packages.sh - Package management for MCProxy bootstrap
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
# APT DEPENDENCIES
#──────────────────────────────────────────────────────────────────

install_apt_deps() {
  log_info "Installing system dependencies..."

  # Update package lists
  apt-get update -qq

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

    # BlueZ for BLE support
    "bluez"
    "bluetooth"

    # For D-Bus (required by dbus_next)
    "libdbus-1-dev"

    # For SSL certificates
    "ca-certificates"
    "gnupg"
  )

  # Install all packages
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"

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

  local conf_file="/etc/lighttpd/conf-available/99-mcproxy.conf"
  local marker="# MCProxy SPA rewrite"

  # Check if already configured
  if [[ -f "$conf_file" ]] && grep -q "$marker" "$conf_file" 2>/dev/null; then
    log_info "  lighttpd already configured"
    return 0
  fi

  # Create MCProxy-specific config
  cat > "$conf_file" << 'EOF'
# MCProxy SPA rewrite + redirect configuration
# Enables Vue.js SPA routing and root redirect

server.modules += ("mod_rewrite", "mod_redirect")

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
EOF

  # Enable the config (idempotent)
  if [[ ! -L /etc/lighttpd/conf-enabled/99-mcproxy.conf ]]; then
    ln -sf "$conf_file" /etc/lighttpd/conf-enabled/99-mcproxy.conf
  fi

  # Test config before reloading
  if lighttpd -t -f /etc/lighttpd/lighttpd.conf 2>/dev/null; then
    systemctl reload lighttpd 2>/dev/null || systemctl restart lighttpd
  else
    log_warn "  lighttpd config test failed"
  fi
}
