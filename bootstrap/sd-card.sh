#!/bin/bash
# sd-card.sh - Hardware diagnostics for McApp Raspberry Pi
# Standalone script — no dependency on mcapp.sh or lib/ modules
#
# Usage:
#   sudo ./sd-card.sh                    # Run locally on Pi
#   ssh mcapp.local "sudo bash -s" < bootstrap/sd-card.sh  # Run remotely
set -eo pipefail

#──────────────────────────────────────────────────────────────────
# COLORS & HELPERS
#──────────────────────────────────────────────────────────────────
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'

section() {
  echo ""
  echo "──────────────────────────────────────────────────────────"
  printf "  ${BLUE}%s${NC}\n" "$1"
  echo "──────────────────────────────────────────────────────────"
}

# Color a value by threshold: green < warn_at, yellow < crit_at, red >= crit_at
color_pct() {
  local value="$1" warn_at="${2:-70}" crit_at="${3:-90}"
  if (( value >= crit_at )); then
    printf "${RED}%d%%${NC}" "$value"
  elif (( value >= warn_at )); then
    printf "${YELLOW}%d%%${NC}" "$value"
  else
    printf "${GREEN}%d%%${NC}" "$value"
  fi
}

# Track summary rows: arrays filled by each section
SUMMARY_LABELS=()
SUMMARY_STATUSES=()
SUMMARY_DETAILS=()

summary_add() {
  local label="$1" status="$2" detail="$3"
  SUMMARY_LABELS+=("$label")
  SUMMARY_STATUSES+=("$status")
  SUMMARY_DETAILS+=("$detail")
}

#──────────────────────────────────────────────────────────────────
# 1. UPTIME & SYSTEM
#──────────────────────────────────────────────────────────────────
section_uptime() {
  section "Uptime & System"

  local hostname
  hostname=$(hostname -s 2>/dev/null || echo "unknown")
  local debian_version="unknown"
  if [[ -f /etc/os-release ]]; then
    debian_version=$(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")
  fi

  printf "  %-20s %s\n" "Hostname:" "$hostname"
  printf "  %-20s %s\n" "OS:" "$debian_version"

  local uptime_str
  uptime_str=$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /' | sed 's/,.*load.*//')
  printf "  %-20s %s\n" "Uptime:" "$uptime_str"

  local load
  load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo "n/a")
  printf "  %-20s %s\n" "Load average:" "$load"

  local temp="n/a"
  if command -v vcgencmd &>/dev/null; then
    temp=$(vcgencmd measure_temp 2>/dev/null | sed "s/temp=//" || echo "n/a")
  fi
  printf "  %-20s %s\n" "CPU temperature:" "$temp"
}

#──────────────────────────────────────────────────────────────────
# 2. MEMORY & SWAP
#──────────────────────────────────────────────────────────────────
section_memory() {
  section "Memory & Swap"

  # Parse free -m output (NR==2 avoids locale-dependent header)
  local mem_line
  mem_line=$(free -m | awk 'NR==2 {print $2, $3, $7}')
  local mem_total mem_used mem_avail
  read -r mem_total mem_used mem_avail <<< "$mem_line"

  local mem_pct=0
  if (( mem_total > 0 )); then
    mem_pct=$(( mem_used * 100 / mem_total ))
  fi

  printf "  %-20s %sM / %sM (" "RAM:" "$mem_used" "$mem_total"
  color_pct "$mem_pct"
  printf "), %sM available\n" "$mem_avail"

  # Summary for RAM
  local ram_status="ok"
  if (( mem_pct >= 90 )); then ram_status="fail"
  elif (( mem_pct >= 70 )); then ram_status="warn"; fi
  summary_add "RAM" "$ram_status" "${mem_used}M/${mem_total}M (${mem_pct}%)"

  # Swap (NR==3 avoids locale-dependent header)
  local swap_line
  swap_line=$(free -m | awk 'NR==3 {print $2, $3, $4}')
  local swap_total swap_used swap_free
  read -r swap_total swap_used swap_free <<< "$swap_line"

  if (( swap_total == 0 )); then
    printf "  %-20s no swap configured\n" "Swap:"
    summary_add "Swap" "ok" "no swap configured"
  else
    local swap_pct=$(( swap_used * 100 / swap_total ))
    printf "  %-20s %sM / %sM (" "Swap:" "$swap_used" "$swap_total"
    color_pct "$swap_pct" 50 80
    printf "), %sM free\n" "$swap_free"

    local swap_status="ok"
    if (( swap_pct >= 80 )); then swap_status="fail"
    elif (( swap_pct >= 50 )); then swap_status="warn"; fi
    summary_add "Swap" "$swap_status" "${swap_used}M/${swap_total}M (${swap_pct}%)"

    if (( swap_pct >= 50 )); then
      printf "  ${YELLOW}%s${NC}\n" "Heavy swap usage — possible OOM pressure"
    fi
  fi
}

#──────────────────────────────────────────────────────────────────
# 3. SD CARD HEALTH
#──────────────────────────────────────────────────────────────────
section_sdcard() {
  section "SD Card Health"

  # Card model from sysfs (reliable) or dmesg (fallback)
  local card_info="unknown"
  if [[ -f /sys/block/mmcblk0/device/name ]]; then
    local card_name card_type
    card_name=$(cat /sys/block/mmcblk0/device/name 2>/dev/null || true)
    card_type=$(cat /sys/block/mmcblk0/device/type 2>/dev/null || true)
    if [[ -n "$card_name" ]]; then
      card_info="$card_name"
      [[ -n "$card_type" ]] && card_info="${card_info} (${card_type})"
    fi
  elif card_model=$(dmesg 2>/dev/null | grep -oP 'mmcblk0: \K.*' | head -1); then
    [[ -n "$card_model" ]] && card_info="$card_model"
  fi
  printf "  %-20s %s\n" "Card:" "$card_info"

  # Disk usage
  local disk_line
  disk_line=$(df -h / | awk 'NR==2 {print $2, $3, $5}')
  local disk_total disk_used disk_pct_str
  read -r disk_total disk_used disk_pct_str <<< "$disk_line"
  local disk_pct=${disk_pct_str//%/}
  printf "  %-20s %s used of %s (" "Disk usage:" "$disk_used" "$disk_total"
  color_pct "$disk_pct"
  printf ")\n"

  local sd_status="ok"
  if (( disk_pct >= 90 )); then sd_status="fail"
  elif (( disk_pct >= 80 )); then sd_status="warn"; fi
  summary_add "SD Card" "$sd_status" "${disk_total}, ${disk_pct}% used"

  # Filesystem state (tune2fs)
  local fs_state="unknown" mount_count="?" max_mount_count="?"
  local root_dev
  root_dev=$(findmnt -no SOURCE / 2>/dev/null || df / | awk 'NR==2 {print $1}')
  if [[ -n "$root_dev" ]] && command -v tune2fs &>/dev/null; then
    local tune_out
    if tune_out=$(tune2fs -l "$root_dev" 2>/dev/null); then
      fs_state=$(echo "$tune_out" | awk -F: '/Filesystem state/ {gsub(/^ +/,"",$2); print $2}')
      mount_count=$(echo "$tune_out" | awk -F: '/Mount count/ {gsub(/^ +/,"",$2); print $2}')
      max_mount_count=$(echo "$tune_out" | awk -F: '/Maximum mount count/ {gsub(/^ +/,"",$2); print $2}')
    fi
  fi
  printf "  %-20s %s, %s mounts" "Filesystem:" "$fs_state" "$mount_count"
  if [[ "$max_mount_count" != "?" && "$max_mount_count" != "-1" ]]; then
    printf " (max %s)" "$max_mount_count"
  fi
  echo ""

  local fs_status="ok"
  if [[ "$fs_state" == *"error"* ]]; then fs_status="fail"
  elif [[ "$fs_state" == "unknown" ]]; then fs_status="warn"; fi
  summary_add "Filesystem" "$fs_status" "${fs_state}, ${mount_count} mounts"

  # MMC lifetime (ExtCSD)
  section_mmc_lifetime
}

section_mmc_lifetime() {
  local eol_map=([1]="Normal" [2]="Warning (80% used)" [3]="Urgent (end of life)")
  local life_map=(
    [1]="0-10%" [2]="10-20%" [3]="20-30%" [4]="30-40%" [5]="40-50%"
    [6]="50-60%" [7]="60-70%" [8]="70-80%" [9]="80-90%" [10]="90-100%"
    [11]="exceeded"
  )

  if ! command -v mmc &>/dev/null; then
    printf "  %-20s mmc-utils not installed\n" "Card lifetime:"
    summary_add "Card Lifetime" "na" "mmc-utils not installed"
    return
  fi

  local mmc_dev="/dev/mmcblk0"
  if [[ ! -b "$mmc_dev" ]]; then
    printf "  %-20s %s not found\n" "Card lifetime:" "$mmc_dev"
    summary_add "Card Lifetime" "na" "device not found"
    return
  fi

  local extcsd
  if ! extcsd=$(mmc extcsd read "$mmc_dev" 2>&1); then
    printf "  %-20s ExtCSD not supported\n" "Card lifetime:"
    summary_add "Card Lifetime" "na" "ExtCSD not supported"
    return
  fi

  # PRE_EOL_INFO [EXT_CSD_PRE_EOL_INFO]
  local eol_raw
  eol_raw=$(echo "$extcsd" | grep -i 'PRE_EOL_INFO' | grep -oP '0x[0-9a-fA-F]+' | tail -1)
  local eol_val=0
  if [[ -n "$eol_raw" ]]; then
    eol_val=$((eol_raw))
  fi
  local eol_text="${eol_map[$eol_val]:-unknown (${eol_raw})}"

  # DEVICE_LIFE_TIME_EST_TYP_A
  local life_a_raw
  life_a_raw=$(echo "$extcsd" | grep -i 'LIFE_TIME_EST_TYP_A' | grep -oP '0x[0-9a-fA-F]+' | tail -1)
  local life_a_val=0
  if [[ -n "$life_a_raw" ]]; then
    life_a_val=$((life_a_raw))
  fi
  local life_a_text="${life_map[$life_a_val]:-unknown (${life_a_raw})}"

  # DEVICE_LIFE_TIME_EST_TYP_B
  local life_b_raw
  life_b_raw=$(echo "$extcsd" | grep -i 'LIFE_TIME_EST_TYP_B' | grep -oP '0x[0-9a-fA-F]+' | tail -1)
  local life_b_val=0
  if [[ -n "$life_b_raw" ]]; then
    life_b_val=$((life_b_raw))
  fi
  local life_b_text="${life_map[$life_b_val]:-unknown (${life_b_raw})}"

  local eol_color="${GREEN}"
  if (( eol_val >= 3 )); then eol_color="${RED}"
  elif (( eol_val >= 2 )); then eol_color="${YELLOW}"; fi

  printf "  %-20s ${eol_color}%s${NC}\n" "Pre-EOL status:" "$eol_text"
  printf "  %-20s SLC: %s, MLC: %s\n" "Lifetime estimate:" "$life_a_text" "$life_b_text"

  local life_status="ok"
  if (( eol_val >= 3 )); then life_status="fail"
  elif (( eol_val >= 2 )); then life_status="warn"; fi
  summary_add "Card Lifetime" "$life_status" "$eol_text"
}

#──────────────────────────────────────────────────────────────────
# 3b. I/O ERRORS
#──────────────────────────────────────────────────────────────────
section_io_errors() {
  local io_errors=0
  local error_lines=""

  # Check dmesg for I/O errors
  if dmesg_out=$(dmesg 2>/dev/null); then
    error_lines=$(echo "$dmesg_out" | grep -i 'i/o error\|mmcblk0.*error\|EXT4-fs error' || true)
    if [[ -n "$error_lines" ]]; then
      io_errors=$(echo "$error_lines" | wc -l)
    fi
  fi

  # Check journalctl for kernel I/O errors (7 days)
  local journal_errors=""
  if journal_errors=$(journalctl -k --since "7 days ago" --no-pager 2>/dev/null \
      | grep -i 'i/o error\|mmcblk0.*error\|EXT4-fs error' || true); then
    if [[ -n "$journal_errors" ]]; then
      local journal_count
      journal_count=$(echo "$journal_errors" | wc -l)
      if (( journal_count > io_errors )); then
        io_errors=$journal_count
        error_lines="$journal_errors"
      fi
    fi
  fi

  if (( io_errors > 0 )); then
    printf "  %-20s ${RED}%d errors in last 7 days${NC}\n" "I/O errors:" "$io_errors"
    echo "$error_lines" | tail -3 | while IFS= read -r line; do
      printf "    %s\n" "$line"
    done
    summary_add "I/O Errors" "fail" "${io_errors} in 7 days"
  else
    printf "  %-20s ${GREEN}none in last 7 days${NC}\n" "I/O errors:"
    summary_add "I/O Errors" "ok" "none in 7 days"
  fi
}

#──────────────────────────────────────────────────────────────────
# 4. POWER & THROTTLING
#──────────────────────────────────────────────────────────────────
section_power() {
  section "Power & Throttling"

  if ! command -v vcgencmd &>/dev/null; then
    printf "  %-20s vcgencmd not available\n" "Throttling:"
    summary_add "Power" "na" "vcgencmd not available"
    return
  fi

  local throttled_raw
  if ! throttled_raw=$(vcgencmd get_throttled 2>/dev/null); then
    printf "  %-20s could not read throttle status\n" "Throttling:"
    summary_add "Power" "warn" "could not read"
    return
  fi

  local throttled_hex="${throttled_raw#throttled=}"
  local throttled_val=$((throttled_hex))

  # Bit definitions
  local -a active_flags=(
    [0]="Under-voltage detected"
    [1]="ARM frequency capped"
    [2]="Currently throttled"
    [3]="Soft temperature limit"
  )
  local -a history_flags=(
    [16]="Under-voltage has occurred"
    [17]="ARM frequency capping has occurred"
    [18]="Throttling has occurred"
    [19]="Soft temperature limit has occurred"
  )

  if (( throttled_val == 0 )); then
    printf "  %-20s ${GREEN}no issues since boot${NC}\n" "Throttling:"
    summary_add "Power" "ok" "no issues since boot"
    return
  fi

  local has_active=false has_history=false

  # Check active flags (bits 0-3)
  local bit
  for bit in 0 1 2 3; do
    if (( throttled_val & (1 << bit) )); then
      printf "  ${RED}[ACTIVE]${NC}  %s\n" "${active_flags[$bit]}"
      has_active=true
    fi
  done

  # Check history flags (bits 16-19)
  for bit in 16 17 18 19; do
    if (( throttled_val & (1 << bit) )); then
      printf "  ${YELLOW}[SINCE BOOT]${NC}  %s\n" "${history_flags[$bit]}"
      has_history=true
    fi
  done

  if [[ "$has_active" == "true" ]]; then
    summary_add "Power" "fail" "active issues (${throttled_hex})"
  elif [[ "$has_history" == "true" ]]; then
    summary_add "Power" "warn" "historical issues (${throttled_hex})"
  fi
}

#──────────────────────────────────────────────────────────────────
# 5. TMPFS PROTECTION
#──────────────────────────────────────────────────────────────────
section_tmpfs() {
  section "tmpfs Protection"

  local tmpfs_ok=true
  local tmpfs_list=""

  local mp
  for mp in /tmp /var/log; do
    local fstype
    fstype=$(findmnt -no FSTYPE "$mp" 2>/dev/null || echo "unknown")
    if [[ "$fstype" == "tmpfs" ]]; then
      local size
      size=$(findmnt -no SIZE "$mp" 2>/dev/null || echo "?")
      printf "  %-20s ${GREEN}tmpfs${NC} (%s)\n" "${mp}:" "$size"
      tmpfs_list="${tmpfs_list:+${tmpfs_list} + }${mp}"
    else
      printf "  %-20s ${YELLOW}%s${NC} (not tmpfs — extra SD wear)\n" "${mp}:" "$fstype"
      tmpfs_ok=false
    fi
  done

  if [[ "$tmpfs_ok" == "true" ]]; then
    summary_add "tmpfs Protection" "ok" "${tmpfs_list} on tmpfs"
  else
    summary_add "tmpfs Protection" "warn" "not all on tmpfs"
  fi
}

#──────────────────────────────────────────────────────────────────
# 6. BLE AUTO-RECONNECT HISTORY
#──────────────────────────────────────────────────────────────────
section_ble_reconnect() {
  section "BLE Auto-Reconnect History"

  # McApp BLE service reconnects
  local ble_attempts=0 ble_success=0 ble_exhausted=0 ble_disconnects=0
  local ble_log=""
  if ble_log=$(journalctl -u mcapp-ble.service --no-pager 2>/dev/null); then
    ble_attempts=$(echo "$ble_log" | grep -c "Auto-reconnect attempt" || true)
    ble_success=$(echo "$ble_log" | grep -c "Auto-reconnect successful" || true)
    ble_exhausted=$(echo "$ble_log" | grep -c "Auto-reconnect exhausted" || true)
    ble_disconnects=$(echo "$ble_log" | grep -c "Unexpected disconnect detected" || true)
  fi

  # McApp main service reconnects
  local sse_errors=0 sse_reconnects=0 ble_remote_reconnects=0
  local mcapp_log=""
  if mcapp_log=$(journalctl -u mcapp.service --no-pager 2>/dev/null); then
    sse_errors=$(echo "$mcapp_log" | grep -c "SSE connection error" || true)
    sse_reconnects=$(echo "$mcapp_log" | grep -c "reconnecting in" || true)
    ble_remote_reconnects=$(echo "$mcapp_log" | grep -c "BLE auto-reconnected" || true)
  fi

  local total=$((ble_attempts + ble_disconnects + sse_errors + ble_remote_reconnects))

  if (( total == 0 )); then
    printf "  %-20s ${GREEN}no auto-reconnects recorded${NC}\n" "Status:"
    summary_add "BLE Reconnects" "ok" "no auto-reconnects"
    return
  fi

  if (( ble_attempts > 0 || ble_disconnects > 0 )); then
    echo "  BLE Service (mcapp-ble):"
    printf "    %-22s %d\n" "Disconnect triggers:" "$ble_disconnects"
    printf "    %-22s %d\n" "Reconnect attempts:" "$ble_attempts"
    printf "    %-22s %d\n" "Successful:" "$ble_success"
    if (( ble_exhausted > 0 )); then
      printf "    %-22s ${RED}%d${NC}\n" "Exhausted (gave up):" "$ble_exhausted"
    fi
  fi

  if (( sse_errors > 0 || ble_remote_reconnects > 0 )); then
    echo "  McApp Service (mcapp):"
    printf "    %-22s %d\n" "SSE errors:" "$sse_errors"
    printf "    %-22s %d\n" "SSE reconnects:" "$sse_reconnects"
    printf "    %-22s %d\n" "BLE reconnected:" "$ble_remote_reconnects"
  fi

  # Last 5 reconnect events
  echo ""
  echo "  Recent events:"
  local events=""
  events=$(
    {
      journalctl -u mcapp-ble.service --no-pager 2>/dev/null \
        | grep -E "Auto-reconnect|Unexpected disconnect" || true
      journalctl -u mcapp.service --no-pager 2>/dev/null \
        | grep -E "SSE connection error|BLE auto-reconnected" || true
    } | sort | tail -5
  )
  if [[ -n "$events" ]]; then
    echo "$events" | while IFS= read -r line; do
      printf "    %s\n" "$line"
    done
  else
    printf "    (no detailed events found)\n"
  fi

  local ble_status="ok"
  if (( ble_exhausted > 0 )); then ble_status="fail"
  elif (( total > 0 )); then ble_status="warn"; fi
  summary_add "BLE Reconnects" "$ble_status" "${total} events"
}

#──────────────────────────────────────────────────────────────────
# 7. SUMMARY TABLE
#──────────────────────────────────────────────────────────────────
print_summary() {
  echo ""
  echo "══════════════════════════════════════════════════════════"
  printf "  ${BLUE}%-20s Status${NC}\n" "Category"
  echo "══════════════════════════════════════════════════════════"

  local i
  for i in "${!SUMMARY_LABELS[@]}"; do
    local label="${SUMMARY_LABELS[$i]}"
    local status="${SUMMARY_STATUSES[$i]}"
    local detail="${SUMMARY_DETAILS[$i]}"

    local status_str
    case "$status" in
      ok)   status_str="${GREEN}[OK]${NC}"   ;;
      warn) status_str="${YELLOW}[WARN]${NC}" ;;
      fail) status_str="${RED}[FAIL]${NC}"   ;;
      na)   status_str="${BLUE}[N/A]${NC}"   ;;
      *)    status_str="[??]"                ;;
    esac

    printf "  %-20s ${status_str}   %s\n" "$label" "$detail"
  done

  echo "══════════════════════════════════════════════════════════"
  echo ""
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo "  McApp Hardware Diagnostics"
  echo "  $(date '+%Y-%m-%d %H:%M:%S')"

  section_uptime
  section_memory
  section_sdcard
  section_io_errors
  section_power
  section_tmpfs
  section_ble_reconnect
  print_summary
}

main "$@"
