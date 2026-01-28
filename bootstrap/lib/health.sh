#!/bin/bash
# health.sh - Health checks for MCProxy bootstrap
# Validates services, endpoints, and configuration

#──────────────────────────────────────────────────────────────────
# MAIN HEALTH CHECK
#──────────────────────────────────────────────────────────────────

health_check() {
  local all_passed=true

  echo ""
  log_info "Running health checks..."
  echo "──────────────────────────────────────────────────────────"

  # Check services
  if ! check_service "mcproxy"; then all_passed=false; fi
  if ! check_service "caddy"; then all_passed=false; fi
  if ! check_service "lighttpd"; then all_passed=false; fi

  # Check endpoints
  if ! check_webapp_endpoint; then all_passed=false; fi
  if ! check_websocket_port; then all_passed=false; fi
  if ! check_udp_port; then all_passed=false; fi

  # Check config
  if ! check_config_valid; then all_passed=false; fi

  # Check venv
  if ! check_venv; then all_passed=false; fi

  echo "──────────────────────────────────────────────────────────"

  [[ "$all_passed" == "true" ]]
}

#──────────────────────────────────────────────────────────────────
# SERVICE CHECKS
#──────────────────────────────────────────────────────────────────

check_service() {
  local service="$1"

  if systemctl is-active --quiet "$service" 2>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} running\n" "${service}:"
    return 0
  else
    printf "  %-20s ${RED}[FAIL]${NC} not running\n" "${service}:"
    return 1
  fi
}

#──────────────────────────────────────────────────────────────────
# ENDPOINT CHECKS
#──────────────────────────────────────────────────────────────────

check_webapp_endpoint() {
  local hostname
  hostname=$(hostname -s)

  # Try HTTPS first (via Caddy)
  if curl -fsSL --connect-timeout 5 -k "https://localhost/webapp/index.html" &>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} HTTPS responding\n" "webapp:"
    return 0
  fi

  # Try HTTP fallback (via lighttpd)
  if curl -fsSL --connect-timeout 5 "http://localhost/webapp/index.html" &>/dev/null; then
    printf "  %-20s ${YELLOW}[WARN]${NC} HTTP only (no TLS)\n" "webapp:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} not responding\n" "webapp:"
  return 1
}

check_websocket_port() {
  # Check if WebSocket port is listening
  if ss -tln | grep -q ':2980\b' || ss -tln | grep -q ':2981\b'; then
    printf "  %-20s ${GREEN}[OK]${NC} port listening\n" "websocket:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} port not listening\n" "websocket:"
  return 1
}

check_udp_port() {
  # Check if UDP port is listening
  if ss -uln | grep -q ':1799\b'; then
    printf "  %-20s ${GREEN}[OK]${NC} port listening\n" "udp (meshcom):"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} port not listening\n" "udp (meshcom):"
  return 1
}

#──────────────────────────────────────────────────────────────────
# CONFIGURATION CHECK
#──────────────────────────────────────────────────────────────────

check_config_valid() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    printf "  %-20s ${RED}[FAIL]${NC} file missing\n" "config:"
    return 1
  fi

  # Check if valid JSON
  if ! jq '.' "$CONFIG_FILE" &>/dev/null; then
    printf "  %-20s ${RED}[FAIL]${NC} invalid JSON\n" "config:"
    return 1
  fi

  # Check for template values
  if config_has_template_values; then
    printf "  %-20s ${YELLOW}[WARN]${NC} has template values\n" "config:"
    return 1
  fi

  printf "  %-20s ${GREEN}[OK]${NC} valid\n" "config:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# VENV CHECK
#──────────────────────────────────────────────────────────────────

check_venv() {
  if ! venv_is_valid; then
    printf "  %-20s ${RED}[FAIL]${NC} invalid or missing\n" "python venv:"
    return 1
  fi

  # Check if key packages are importable
  if ! "${VENV_DIR}/bin/python" -c "import websockets, dbus_next" 2>/dev/null; then
    printf "  %-20s ${YELLOW}[WARN]${NC} missing packages\n" "python venv:"
    return 1
  fi

  local python_version
  python_version=$("${VENV_DIR}/bin/python" --version 2>&1 | cut -d' ' -f2)
  printf "  %-20s ${GREEN}[OK]${NC} Python ${python_version}\n" "python venv:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# SUCCESS SUMMARY
#──────────────────────────────────────────────────────────────────

print_success_summary() {
  local hostname
  hostname=$(hostname -s)

  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║              MCProxy Installation Complete               ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Access Points:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Web UI:     https://${hostname}.local/webapp"
  echo "    Root Cert:  https://${hostname}.local/root.crt"
  echo "    WebSocket:  wss://${hostname}.local:2981"
  echo ""
  echo "  Service Management:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Status:     sudo systemctl status mcproxy"
  echo "    Logs:       sudo journalctl -u mcproxy -f"
  echo "    Restart:    sudo systemctl restart mcproxy"
  echo ""
  echo "  Configuration:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Config:     ${CONFIG_FILE}"
  echo "    Reconfig:   sudo ./mcproxy.sh --reconfigure"
  echo ""

  # Show callsign if configured
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local callsign
    callsign=$(jq -r '.CALL_SIGN // "not set"' "$CONFIG_FILE" 2>/dev/null)
    echo "  Station: ${callsign}"
    echo ""
  fi

  echo "  For first-time setup:"
  echo "  1. Download the root certificate from the URL above"
  echo "  2. Install it in your browser/system trust store"
  echo "  3. Navigate to the Web UI"
  echo ""
}

#──────────────────────────────────────────────────────────────────
# DIAGNOSTIC COMMANDS (for troubleshooting)
#──────────────────────────────────────────────────────────────────

print_diagnostic_info() {
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "  DIAGNOSTIC INFORMATION"
  echo "═══════════════════════════════════════════════════════════"
  echo ""

  echo "System:"
  echo "  Debian:     $(get_debian_codename)"
  echo "  Kernel:     $(uname -r)"
  echo "  Memory:     $(free -h | awk '/^Mem:/ {print $2 " total, " $3 " used"}')"
  echo "  Disk:       $(df -h / | awk 'NR==2 {print $2 " total, " $3 " used (" $5 ")"}')"
  echo ""

  echo "Python Environment:"
  echo "  System:     $(python3 --version 2>&1)"
  if [[ -f "${VENV_DIR}/bin/python" ]]; then
    echo "  Venv:       $("${VENV_DIR}/bin/python" --version 2>&1)"
    echo "  Packages:"
    "${VENV_DIR}/bin/pip" list 2>/dev/null | grep -E "websockets|dbus-next|timezonefinder|httpx|zstandard" | sed 's/^/    /'
  fi
  echo ""

  echo "Services:"
  for svc in mcproxy caddy lighttpd bluetooth avahi-daemon; do
    local status
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
      status="running"
    elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      status="stopped (enabled)"
    else
      status="disabled"
    fi
    printf "  %-16s %s\n" "${svc}:" "$status"
  done
  echo ""

  echo "Network Ports:"
  echo "  TCP:"
  ss -tln 2>/dev/null | grep -E ':(443|2980|2981|80)\b' | awk '{print "    " $4}'
  echo "  UDP:"
  ss -uln 2>/dev/null | grep -E ':1799\b' | awk '{print "    " $4}'
  echo ""

  echo "Configuration:"
  if [[ -f "$CONFIG_FILE" ]]; then
    echo "  File: ${CONFIG_FILE}"
    jq '.' "$CONFIG_FILE" 2>/dev/null | head -20 | sed 's/^/  /'
  else
    echo "  Config file not found"
  fi
  echo ""
}
