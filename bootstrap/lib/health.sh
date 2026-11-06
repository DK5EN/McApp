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
  if ! check_service "caddy"; then all_passed=false; fi

  # Check endpoints
  if ! check_webapp_endpoint; then all_passed=false; fi
  if ! check_udp_port; then all_passed=false; fi
  if ! check_sse_endpoint; then all_passed=false; fi
  if ! check_lighttpd_proxy; then all_passed=false; fi

  # Caddy HTTPS probe is advisory only (does NOT flip all_passed) — on first
  # boot Caddy's `tls internal` CA/leaf generation can lag a few seconds, and
  # we don't want that transient state to hard-abort the whole bootstrap.
  check_caddy_https || true

  # Check data
  if ! check_sqlite_db; then all_passed=false; fi

  # Check config
  if ! check_config_valid; then all_passed=false; fi

  # Check venv
  if ! check_venv; then all_passed=false; fi

  # Check versions
  check_versions

  # Coexistence warnings are advisory only (never flip all_passed).
  check_coexistence || true

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
  # Retry generously — on first boot the Pi is slow to start the Python process
  local attempts=8
  for ((i=1; i<=attempts; i++)); do
    if ss -uln | grep -q ':1799\b'; then
      printf "  %-20s ${GREEN}[OK]${NC} port listening\n" "udp (meshcom):"
      return 0
    fi
    sleep 3
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

check_lighttpd_proxy() {
  # Verify lighttpd proxies /api/ and /events to FastAPI on port 2981
  # Uses /health endpoint which exists on both direct and proxied paths.
  # lighttpd is now the backend on 127.0.0.1:8082 (Caddy owns :80/:443),
  # so probe the backend port directly rather than :80.
  if curl -fsSL --connect-timeout 3 "http://127.0.0.1:8082/health" &>/dev/null; then
    printf "  %-20s ${GREEN}[OK]${NC} proxying to FastAPI\n" "lighttpd proxy:"
    return 0
  fi

  printf "  %-20s ${RED}[FAIL]${NC} proxy not working\n" "lighttpd proxy:"
  return 1
}

check_caddy_https() {
  # Verify Caddy terminates TLS on :443 and proxies /health through to FastAPI.
  #
  # Probe the site Caddy ACTUALLY serves — NOT localhost. `tls internal` mints a
  # leaf for the configured site name only, so Caddy has no certificate for
  # "localhost" and https://localhost fails the TLS handshake every time — this
  # is not a CA-timing issue and retrying never helps (it produced a permanent
  # false WARN). caddy_site() (packages.sh) resolves the real site — the
  # Caddyfile :443 name, else config.json TLS_HOSTNAME (public-TLS), else
  # $(hostname).local — and we probe it on the loopback via --resolve so the
  # check is independent of external DNS/mDNS. -k because the LAN default uses a
  # self-signed internal CA the probe may not trust.
  #
  # NOTE: loopback-only — proves Caddy answers HTTPS locally but canNOT detect a
  # firewall-blocked :443 for external clients; that external contract is
  # verified separately by scripts/pwa-smoke.sh <host>.
  #
  # Advisory only (caller uses || true): on a cold boot the internal leaf can
  # take a moment to issue, so retry a few times, then WARN rather than FAIL so
  # a cold start doesn't abort bootstrap.
  local site
  site=$(caddy_site)

  local attempts=5
  for ((i=1; i<=attempts; i++)); do
    if curl -skf --resolve "${site}:443:127.0.0.1" --connect-timeout 3 "https://${site}/health" &>/dev/null; then
      printf "  %-20s ${GREEN}[OK]${NC} HTTPS responding\n" "caddy https:"
      return 0
    fi
    sleep 2
  done

  printf "  %-20s ${YELLOW}[WARN]${NC} Caddy HTTPS not responding on ${site}:443\n" "caddy https:"
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

  # Check if key packages are importable (including BLE service dependencies)
  if ! "${venv_path}/bin/python" -c "import uvicorn, httptools, dbus_next" 2>/dev/null; then
    printf "  %-20s ${YELLOW}[WARN]${NC} missing packages\n" "python venv:"
    return 1
  fi

  local python_version
  python_version=$("${venv_path}/bin/python" --version 2>&1 | cut -d' ' -f2)
  printf "  %-20s ${GREEN}[OK]${NC} Python ${python_version}\n" "python venv:"
  return 0
}

#──────────────────────────────────────────────────────────────────
# COEXISTENCE WARNINGS (advisory: always returns 0)
#──────────────────────────────────────────────────────────────────

# Listening non-loopback TCP ports, one "port process" line each, unique.
# McApp's own internal ports are skipped: the firewall blocks them on purpose,
# and lighttpd binds 0.0.0.0:8082 despite `server.bind = "127.0.0.1"`.
_coex_listening() {
  ss -H -tlnp 2>/dev/null | awk '
    { a = $4; p = a; sub(/.*:/, "", p); h = a; sub(/:[0-9]+$/, "", h)
      if (h ~ /^127\./ || h == "::1" || h == "[::1]" || p !~ /^[0-9]+$/) next
      if (p == 2981 || p == 8081 || p == 8082) next   # API, BLE service, lighttpd backend
      n = "-"; i = index($0, "users:((\"")
      if (i > 0) { n = substr($0, i + 9); sub(/".*/, "", n) }
      if (!(p in seen)) { seen[p] = 1; print p, n } }'
}

# Ordered "A|D lo hi" rules (stdin: nft chain dump) that decide a TCP port.
# A rule counts only when the port is its sole condition besides a source
# restriction: interface-bound, negated and non-NEW conntrack rules are skipped,
# and a drop that is conditional (limit/meter) is not a block. A bare
# accept/drop is the whole range. The verdict is the last word; quotes
# (comment, log prefix) are stripped first so a comment cannot flip it.
_coex_nft_rules() {
  awk '{
    s = $0; gsub(/"[^"]*"/, "", s); sub(/[ \t]+comment[ \t]*$/, "", s)
    sub(/^[ \t]+/, "", s); sub(/[ \t;]+$/, "", s)
    if (s ~ /(^|[ \t])accept$/) v = "A"
    else if (s ~ /(^|[ \t])(drop|reject)([ \t]|$)/) v = "D"
    else next
    t = s; sub(/^counter( packets [0-9]+ bytes [0-9]+)?[ \t]*/, "", t)
    if (t ~ /^(accept|drop|reject)([ \t]|$)/) { print v, 0, 65535; next }
    if (s !~ /tcp dport / || s ~ /!/ || s ~ /(^|[ \t])(iif|iifname)[ \t]/) next
    if (s ~ /ct state/ && s !~ /ct state[ {]+[a-z, ]*new/) next
    if (v == "D" && s ~ /(limit|meter|ct count)/) next
    r = s; sub(/.*tcp dport /, "", r)
    if (r ~ /^\{/) { sub(/^\{/, "", r); sub(/\}.*/, "", r) } else { sub(/ .*/, "", r) }
    gsub(/,/, " ", r); n = split(r, tk, " ")
    for (i = 1; i <= n; i++) {
      if (tk[i] !~ /^[0-9]+(-[0-9]+)?$/) continue
      split(tk[i], pr, "-"); print v, pr[1], (pr[2] == "" ? pr[1] : pr[2]) } }'
}

# Same for `iptables -S INPUT` output (stdin).
_coex_ipt_rules() {
  awk '/^-A INPUT / {
    if ($0 ~ /(^| )-i / || $0 ~ /!/) next
    if ($0 ~ / -j ACCEPT/) v = "A"
    else if ($0 ~ / -j (DROP|REJECT)/) v = "D"
    else next
    if ($0 ~ /^-A INPUT -j /) { print v, 0, 65535; next }
    if ($0 ~ /-p udp/ || $0 !~ /--dports? /) next
    if ($0 ~ /(--state|--ctstate) / && $0 !~ /(--state|--ctstate) [A-Z,]*NEW/) next
    if (v == "D" && $0 ~ /-m (recent|limit|hashlimit|connlimit)/) next
    s = $0; sub(/.*--dports? /, "", s); sub(/ .*/, "", s)
    gsub(/,/, " ", s); n = split(s, tk, " ")
    for (i = 1; i <= n; i++) {
      split(tk[i], pr, ":"); print v, pr[1], (pr[2] == "" ? pr[1] : pr[2]) } }'
}

# First rule that covers the port wins; $2 is the chain policy (A or D).
_coex_verdict() {
  awk -v p="$1" -v d="$2" '$2 + 0 <= p + 0 && p + 0 <= $3 + 0 { print $1; f = 1; exit } END { if (!f) print d }'
}

check_coexistence() {
  local nft_conf="${MCAPP_NFT_CONF:-/etc/nftables.conf}"
  local caddy_conf="${MCAPP_CADDY_CONF:-/etc/caddy/Caddyfile}"
  local caddy_env="${MCAPP_CADDY_ENV:-/etc/mcapp/caddy.env}"
  local warned=0 chain="" ipt="" rules="" dflt="D" mode="" out="" line ruleset="" managed=false

  # McApp's custom folder and boot fallback exist only on a file McApp manages.
  if [ -f "$nft_conf" ] && grep -qF "# McApp firewall rules" "$nft_conf" 2>/dev/null; then
    managed=true
  fi
  if command -v nft >/dev/null 2>&1; then
    ruleset=$(nft list ruleset 2>/dev/null) || ruleset=""
  fi

  if command -v nft >/dev/null 2>&1 && chain=$(nft list chain inet filter input 2>/dev/null) && [ -n "$chain" ]; then
    mode=nft
    if printf '%s\n' "$chain" | grep -q 'policy accept'; then dflt=A; fi
    rules=$(printf '%s\n' "$chain" | _coex_nft_rules)
  elif printf '%s\n' "$ruleset" | grep -q 'hook input'; then
    # A firewall in another table: neither "not active" nor safe to judge port by port.
    mode=foreign
  elif command -v iptables >/dev/null 2>&1 && ipt=$(iptables -S INPUT 2>/dev/null) && [ -n "$ipt" ]; then
    mode=iptables
    if printf '%s\n' "$ipt" | grep -q '^-P INPUT ACCEPT'; then dflt=A; fi
    rules=$(printf '%s\n' "$ipt" | _coex_ipt_rules)
  fi

  if [ -z "$mode" ]; then
    echo "  Firewall not active (no readable nftables/iptables ruleset); skipping port check"
  elif [ "$mode" = "foreign" ]; then
    warned=1
    log_warn "An nftables firewall in another table is active and McApp cannot read it. For McApp to work it must allow loopback, 22, 80, 443 and 2985/tcp, and 1799 and 5353/udp. Any other service needs its own rule there."
  else
    local port name
    while read -r port name; do
      [ -n "$port" ] || continue
      if [ "$(printf '%s\n' "$rules" | _coex_verdict "$port" "$dflt")" = "A" ]; then
        continue
      fi
      [ "$name" = "-" ] && name="unknown process"
      warned=1
      if [ "$mode" = "nft" ] && [ "$managed" = "true" ]; then
        log_warn "Port ${port}/tcp (${name}) is listening but blocked by the firewall. Add \"tcp dport ${port} accept\" to /etc/mcapp/nftables.d/custom.nft, check with \"sudo nft -c -f /etc/nftables.conf\", then \"sudo systemctl reload nftables\""
      elif [ "$mode" = "nft" ]; then
        log_warn "Port ${port}/tcp (${name}) is listening but blocked by the firewall. This firewall is your own: add \"tcp dport ${port} accept\" to its input chain in ${nft_conf}, check with \"sudo nft -c -f ${nft_conf}\", then \"sudo systemctl reload nftables\""
      else
        log_warn "Port ${port}/tcp (${name}) is listening but blocked by the firewall. Run: sudo iptables -A INPUT -p tcp --dport ${port} -j ACCEPT, then sudo sh -c \"iptables-save > /etc/iptables/rules.v4\""
      fi
    done < <(_coex_listening)
  fi

  if command -v nft >/dev/null 2>&1 && [ -f "$nft_conf" ]; then
    if ! out=$(nft -c -f "$nft_conf" 2>&1); then
      line=$(printf '%s\n' "$out" | grep -m1 -i error || printf '%s\n' "$out" | head -n 1)
      if [ "$managed" = "true" ]; then
        log_warn "Custom firewall rules are faulty (${line:-no detail}). A reload will refuse them; at boot the box falls back to McApp's own rules"
      else
        log_warn "${nft_conf} is faulty (${line:-no detail}). A reload will refuse it, and at boot the box could come up with no firewall"
      fi
      warned=1
    fi
  fi

  if command -v caddy >/dev/null 2>&1 && [ -f "$caddy_conf" ]; then
    # Validate in the environment the service sees: public-TLS Caddyfiles use
    # {env.*_TOKEN} from caddy.env. Subshell, so a broken file stays contained.
    if ! out=$(
      set +u
      set -a
      [ -r "$caddy_env" ] && . "$caddy_env" 2>/dev/null || true
      set +a
      HOME=/root caddy validate --config "$caddy_conf" --adapter caddyfile 2>&1
    ); then
      line=$(printf '%s\n' "$out" | grep -m1 -i error || printf '%s\n' "$out" | head -n 1)
      log_warn "Caddy config ${caddy_conf} is faulty (${line:-no detail}). Caddy would not start on its next restart, which takes the McApp web UI down"
      warned=1
    fi
  fi

  if [ "$warned" -eq 0 ] && [ -n "$mode" ]; then
    log_ok "No port or config conflicts found"
  fi
  return 0
}

#──────────────────────────────────────────────────────────────────
# VERSION CHECK
#──────────────────────────────────────────────────────────────────

check_versions() {
  local webapp_version
  webapp_version=$(cat "${INSTALL_DIR}/webapp/version.html" 2>/dev/null || echo "unknown")
  printf "  %-20s ${GREEN}[OK]${NC} %s\n" "webapp version:" "$webapp_version"
  printf "  %-20s ${GREEN}[OK]${NC} McApp Bootstrap v%s\n" "bootstrap:" "$SCRIPT_VERSION"

  # Show active slot info if slot layout exists
  if [[ -n "${SLOTS_DIR:-}" ]] && [[ -L "${SLOTS_DIR}/current" ]]; then
    local active_target
    active_target=$(readlink "${SLOTS_DIR}/current")
    printf "  %-20s ${GREEN}[OK]${NC} %s\n" "active slot:" "$active_target"
  fi
  return 0
}

#──────────────────────────────────────────────────────────────────
# NETWORK NAME DETECTION
#──────────────────────────────────────────────────────────────────

# Does this host:scheme pair actually serve McApp, i.e. is it a real access
# point rather than a name that merely resolves?
#
# The answer must be the /health JSON. "Any HTTP response" is not good enough,
# and neither is `curl -f` on its own: when Caddy's front door does not
# recognise a Host header it replies with an EMPTY 200 — no body, no
# content-type. curl -f sees 200 and reports success, so a check built on exit
# status alone happily advertises a URL that renders a blank page in the
# browser. Matching the "status" key in the body is what separates a genuinely
# proxied answer from a name the front door does not serve. See
# scripts/caddy_config_tests.py, which pins exactly that distinction.
#
# Deliberately array-free so it stays runnable under bash 3.2 (macOS) for the
# regression suite; the rest of this installer is bash-4-only by design.
probe_access_point() {
  local host="$1" scheme="$2" body=""
  if [[ "$scheme" == "https" ]]; then
    # LAN HTTPS uses Caddy's internal CA, which is not in the local trust store
    body=$(curl -fsSk --connect-timeout 2 --max-time 4 "https://${host}/health" 2>/dev/null) || return 1
  else
    body=$(curl -fsS --connect-timeout 2 --max-time 4 "http://${host}/health" 2>/dev/null) || return 1
  fi
  [[ "$body" == *'"status"'* ]]
}

# Populates NETWORK_URLS[] and NETWORK_LABELS[] with verified access points.
# Two-stage verification: `getent hosts` (cheap, weeds out names that do not
# resolve at all) and then a real HTTP probe of the candidate — see
# probe_access_point for why name resolution alone was never enough.
# Sets AVAHI_RUNNING to true/false for the mDNS warning.
detect_network_names() {
  NETWORK_URLS=()
  NETWORK_LABELS=()
  AVAHI_RUNNING=false

  local hostname
  hostname=$(hostname -s)

  local ip_addr
  ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}')

  # Associative array for deduplication (requires Bash 4+)
  declare -A seen_hosts

  # Helper: add a URL if the host hasn't been seen yet AND it passes the probe
  _add_url() {
    local host="$1" scheme="$2" label="$3"
    if [[ -z "$host" || -n "${seen_hosts[$host]+x}" ]]; then
      return
    fi
    # Mark as seen before probing, so a candidate that fails here is not
    # retried by a later rule that happens to derive the same name.
    seen_hosts["$host"]=1
    if ! probe_access_point "$host" "$scheme"; then
      return
    fi
    NETWORK_URLS+=("${scheme}://${host}/webapp")
    NETWORK_LABELS+=("$label")
  }

  # 1. TLS hostname (from config.json)
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local tls_enabled tls_hostname
    tls_enabled=$(jq -r '.TLS_ENABLED // false' "$CONFIG_FILE" 2>/dev/null)
    tls_hostname=$(jq -r '.TLS_HOSTNAME // ""' "$CONFIG_FILE" 2>/dev/null)
    if [[ "$tls_enabled" == "true" && -n "$tls_hostname" ]]; then
      _add_url "$tls_hostname" "https" "TLS"
    fi
  fi

  # 2. mDNS (.local via avahi-daemon)
  if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    AVAHI_RUNNING=true
    if getent hosts "${hostname}.local" &>/dev/null; then
      _add_url "${hostname}.local" "http" "mDNS"
    fi
  fi

  # 3. System domain (from dnsdomainname)
  if command -v dnsdomainname &>/dev/null; then
    local sys_domain
    sys_domain=$(dnsdomainname 2>/dev/null)
    if [[ -n "$sys_domain" ]]; then
      local sys_fqdn="${hostname}.${sys_domain}"
      if getent hosts "$sys_fqdn" &>/dev/null; then
        _add_url "$sys_fqdn" "http" "DNS"
      fi
    fi
  fi

  # 4. Fritz!Box (common for German ham radio operators)
  if getent hosts "${hostname}.fritz.box" &>/dev/null; then
    _add_url "${hostname}.fritz.box" "http" "Fritz!Box"
  fi

  # 5. resolv.conf search/domain entries
  if [[ -f /etc/resolv.conf ]]; then
    local domains
    domains=$(awk '/^(search|domain)/ {for(i=2;i<=NF;i++) print $i}' /etc/resolv.conf)
    local domain
    for domain in $domains; do
      local fqdn="${hostname}.${domain}"
      if getent hosts "$fqdn" &>/dev/null; then
        _add_url "$fqdn" "http" "DNS"
      fi
    done
  fi

  # 6. Reverse DNS of own IP
  if [[ -n "$ip_addr" ]]; then
    local rdns_hosts
    rdns_hosts=$(getent hosts "$ip_addr" 2>/dev/null | awk '{for(i=2;i<=NF;i++) print $i}' || true)
    local rdns_host
    for rdns_host in $rdns_hosts; do
      # Skip bare hostnames (no dot) and localhost entries
      if [[ "$rdns_host" == *"."* && "$rdns_host" != "localhost"* ]]; then
        _add_url "$rdns_host" "http" "rDNS"
      fi
    done
  fi

  # 7. IP address — probed like every other candidate. It used to be added
  # unconditionally ("no verification needed"), which is precisely how the
  # summary ended up printing a blank-page URL: before the :80 catch-all in
  # Caddyfile.mcapp v3, a request by IP matched no site and got the empty 200.
  if [[ -n "$ip_addr" ]]; then
    _add_url "$ip_addr" "http" "IP"
  fi

  # Nothing passed. Rather than print an empty Access Points box, fall back to
  # the IP and label it plainly as unverified — this is the honest outcome when
  # Caddy is down or still restarting, and it must not read as a working install.
  if [[ ${#NETWORK_URLS[@]} -eq 0 && -n "$ip_addr" ]]; then
    NETWORK_URLS+=("http://${ip_addr}/webapp")
    NETWORK_LABELS+=("IP, UNVERIFIED")
  fi

  unset -f _add_url
}

#──────────────────────────────────────────────────────────────────
# SUCCESS SUMMARY
#──────────────────────────────────────────────────────────────────

print_success_summary() {
  detect_network_names

  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║            McApp Installation Complete                   ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Access Points:"
  echo "  ─────────────────────────────────────────────────────────"

  local i
  for i in "${!NETWORK_URLS[@]}"; do
    if [[ $i -eq 0 ]]; then
      printf "    Web UI:     %s  (%s)\n" "${NETWORK_URLS[$i]}" "${NETWORK_LABELS[$i]}"
    else
      printf "                %s  (%s)\n" "${NETWORK_URLS[$i]}" "${NETWORK_LABELS[$i]}"
    fi
  done

  if [[ "$AVAHI_RUNNING" != "true" ]]; then
    echo ""
    echo "    Note: avahi-daemon is not running (mDNS/.local disabled)"
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
  echo "    Upgrade:    sudo ~/bootstrap/mcapp.sh"
  echo ""

  # Show callsign if configured
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    local callsign
    callsign=$(jq -r '.CALL_SIGN // "not set"' "$CONFIG_FILE" 2>/dev/null)
    echo "  Station: ${callsign}"
    echo ""
  fi

  # Reboot recommendation
  if [[ -f /var/run/reboot-required ]]; then
    echo "  ─────────────────────────────────────────────────────────"
    printf "  ${YELLOW}%s${NC}" "Reboot recommended"
    if [[ -f /var/run/reboot-required.pkgs ]]; then
      local pkgs
      pkgs=$(paste -sd', ' /var/run/reboot-required.pkgs)
      printf " (updated: %s)" "$pkgs"
    fi
    echo ""
    echo "    Run: sudo reboot"
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
    "${venv_path}/bin/pip" list 2>/dev/null | grep -E "uvicorn|httptools|uvloop|dbus-next|timezonefinder|httpx|zstandard" | sed 's/^/    /'
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
