#!/bin/bash
# health.sh - Health checks for McApp bootstrap
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
  if ! check_service "mcapp"; then all_passed=false; fi
  if ! check_service "lighttpd"; then all_passed=false; fi

  # Check endpoints
  if ! check_webapp_endpoint; then all_passed=false; fi
  if ! check_udp_port; then all_passed=false; fi
  if ! check_sse_endpoint; then all_passed=false; fi

  # Check data
  if ! check_sqlite_db; then all_passed=false; fi

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
  # Check HTTP via lighttpd
  if curl -fsSL --connect-timeout 5 "http://localhost/webapp/index.html" &>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} HTTP responding\n" "webapp:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} not responding\n" "webapp:"
  return 1
}

check_udp_port() {
  # Retry a few times — service may need seconds to bind the port after restart
  local attempts=5
  for ((i=1; i<=attempts; i++)); do
    if ss -uln | grep -q ':1799\b'; then
      printf "  %-20s ${GREEN}[OK]${NC} port listening\n" "udp (meshcom):"
      return 0
    fi
    sleep 2
  done

  printf "  %-20s ${RED}[FAIL]${NC} port not listening\n" "udp (meshcom):"
  return 1
}

check_sse_endpoint() {
  # Retry a few times — SSE server may need seconds to start
  local attempts=5
  for ((i=1; i<=attempts; i++)); do
    if curl -fsSL --connect-timeout 3 "http://localhost:2981/health" &>/dev/null; then
      printf "  %-20s ${GREEN}[OK]${NC} responding\n" "sse (2981):"
      return 0
    fi
    sleep 2
  done

  printf "  %-20s ${RED}[FAIL]${NC} not responding\n" "sse (2981):"
  return 1
}

check_sqlite_db() {
  local db_path="/var/lib/mcapp/messages.db"

  if [[ -f "$db_path" ]]; then
    printf "  %-20s ${GREEN}[OK]${NC} exists\n" "sqlite db:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} missing\n" "sqlite db:"
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
  local venv_path="${INSTALL_DIR}/.venv"

  if ! venv_is_valid; then
    printf "  %-20s ${RED}[FAIL]${NC} invalid or missing\n" "python venv:"
    return 1
  fi

  # Check if key packages are importable
  if ! "${venv_path}/bin/python" -c "import websockets, dbus_next" 2>/dev/null; then
    printf "  %-20s ${YELLOW}[WARN]${NC} missing packages\n" "python venv:"
    return 1
  fi

  local python_version
  python_version=$("${venv_path}/bin/python" --version 2>&1 | cut -d' ' -f2)
  printf "  %-20s ${GREEN}[OK]${NC} Python ${python_version}\n" "python venv:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# SUCCESS SUMMARY
#──────────────────────────────────────────────────────────────────

print_success_summary() {
  local hostname
  hostname=$(hostname -s)

  # Get IP address for alternative URL
  local ip_addr
  ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}')

  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║            McApp Installation Complete                   ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Access Points:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Web UI:     http://${hostname}.local/webapp"
  if [[ -n "$ip_addr" ]]; then
  echo "                http://${ip_addr}/webapp"
  fi
  echo ""
  echo "  Service Management:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    mcapp:      sudo systemctl status|restart mcapp"
  echo "    mcapp-ble:  sudo systemctl status|restart mcapp-ble"
  echo "    Logs:       sudo journalctl -u mcapp -f"
  echo "    Logs BLE:   sudo journalctl -u mcapp-ble -f"
  echo ""
  echo "  Configuration:"
  echo "  ─────────────────────────────────────────────────────────"
  echo "    Config:     ${CONFIG_FILE}"
  echo "    Reconfig:   sudo ~/bootstrap/mcapp.sh --reconfigure"
  echo "    Upgrade:    sudo ~/bootstrap/mcapp.sh --force --dev"
  echo ""

  # Show callsign if configured
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local callsign
    callsign=$(jq -r '.CALL_SIGN // "not set"' "$CONFIG_FILE" 2>/dev/null)
    echo "  Station: ${callsign}"
    echo ""
  fi

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
  local venv_path="${INSTALL_DIR}/.venv"
  if [[ -f "${venv_path}/bin/python" ]]; then
    echo "  Venv:       $(${venv_path}/bin/python --version 2>&1)"
    echo "  Install:    ${INSTALL_DIR}"
    echo "  Packages:"
    "${venv_path}/bin/pip" list 2>/dev/null | grep -E "websockets|dbus-next|timezonefinder|httpx|zstandard" | sed 's/^/    /'
  fi
  echo ""

  echo "Services:"
  for svc in mcapp lighttpd bluetooth avahi-daemon; do
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
  ss -tln 2>/dev/null | grep -E ':(80|2980|2981)\b' | awk '{print "    " $4}'
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
