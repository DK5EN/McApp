#!/bin/bash
# config.sh - Interactive configuration & validation for McApp bootstrap
# Handles user prompts, input validation, and config file management

#──────────────────────────────────────────────────────────────────
# VALIDATION PATTERNS
#──────────────────────────────────────────────────────────────────

# Ham radio callsign pattern (e.g., DK5EN-9, W1ABC-15, OE1ABC-1)
readonly CALLSIGN_PATTERN='^[A-Z]{1,2}[0-9][A-Z]{1,4}(-[0-9]{1,2})?$'

# Latitude range: -90 to 90
readonly LAT_MIN=-90
readonly LAT_MAX=90

# Longitude range: -180 to 180
readonly LON_MIN=-180
readonly LON_MAX=180

#──────────────────────────────────────────────────────────────────
# INPUT PROMPTS
#──────────────────────────────────────────────────────────────────

# Prompt for input with default value
prompt_with_default() {
  local prompt="$1"
  local default="$2"
  local result

  if [[ -n "$default" ]]; then
    read -rp "[?] ${prompt} [${default}]: " result
    echo "${result:-$default}"
  else
    read -rp "[?] ${prompt}: " result
    echo "$result"
  fi
}

# Prompt for callsign with validation
prompt_callsign() {
  local current="$1"
  local callsign

  while true; do
    callsign=$(prompt_with_default "Enter your callsign (e.g., DK5EN-9)" "$current")
    callsign="${callsign^^}" # Convert to uppercase

    if validate_callsign "$callsign"; then
      echo "$callsign"
      return 0
    fi

    log_warn "Invalid callsign format. Use format like: DK5EN-9, W1ABC-15, OE1ABC"
  done
}

# Prompt for MeshCom node address
prompt_node_address() {
  local current="$1"
  local address

  while true; do
    address=$(prompt_with_default "Enter MeshCom node address (hostname or IP)" "$current")

    if validate_node_address "$address"; then
      echo "$address"
      return 0
    fi

    log_warn "Cannot resolve or reach '$address'. Please check the address."
    read -rp "[?] Use anyway? (y/N): " confirm
    if [[ "${confirm,,}" == "y" ]]; then
      echo "$address"
      return 0
    fi
  done
}

# Prompt for latitude
prompt_latitude() {
  local current="$1"
  local lat

  while true; do
    lat=$(prompt_with_default "Enter your latitude (e.g., 48.2082)" "$current")

    if validate_latitude "$lat"; then
      echo "$lat"
      return 0
    fi

    log_warn "Invalid latitude. Must be between ${LAT_MIN} and ${LAT_MAX}."
  done
}

# Prompt for longitude
prompt_longitude() {
  local current="$1"
  local lon

  while true; do
    lon=$(prompt_with_default "Enter your longitude (e.g., 16.3738)" "$current")

    if validate_longitude "$lon"; then
      echo "$lon"
      return 0
    fi

    log_warn "Invalid longitude. Must be between ${LON_MIN} and ${LON_MAX}."
  done
}

# Prompt for station name
prompt_station_name() {
  local current="$1"
  local name

  name=$(prompt_with_default "Enter station name (for weather reports)" "$current")

  # Sanitize: remove special characters, trim whitespace
  name=$(echo "$name" | tr -cd '[:alnum:] _-' | xargs)

  # Default if empty
  [[ -z "$name" ]] && name="McApp Station"

  echo "$name"
}

#──────────────────────────────────────────────────────────────────
# VALIDATION FUNCTIONS
#──────────────────────────────────────────────────────────────────

validate_callsign() {
  local callsign="$1"
  [[ "$callsign" =~ $CALLSIGN_PATTERN ]]
}

validate_node_address() {
  local address="$1"

  # Check if it looks like an IP or hostname
  if [[ -z "$address" ]]; then
    return 1
  fi

  # Try to resolve/ping the address (timeout 2s)
  if ping -c 1 -W 2 "$address" &>/dev/null; then
    return 0
  fi

  # Try DNS resolution only
  if getent hosts "$address" &>/dev/null; then
    return 0
  fi

  return 1
}

validate_latitude() {
  local lat="$1"

  # Check if it's a valid number
  if ! [[ "$lat" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
    return 1
  fi

  # Check range using bc for floating point comparison
  local in_range
  in_range=$(echo "$lat >= $LAT_MIN && $lat <= $LAT_MAX" | bc -l 2>/dev/null)

  [[ "$in_range" == "1" ]]
}

validate_longitude() {
  local lon="$1"

  # Check if it's a valid number
  if ! [[ "$lon" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
    return 1
  fi

  # Check range using bc for floating point comparison
  local in_range
  in_range=$(echo "$lon >= $LON_MIN && $lon <= $LON_MAX" | bc -l 2>/dev/null)

  [[ "$in_range" == "1" ]]
}

#──────────────────────────────────────────────────────────────────
# CONFIG FILE MANAGEMENT
#──────────────────────────────────────────────────────────────────

# Get current config value (or empty if not set/template)
get_config_value() {
  local key="$1"
  local value

  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo ""
    return
  fi

  if ! command -v jq &>/dev/null; then
    echo ""
    return
  fi

  value=$(jq -r ".${key} // \"\"" "$CONFIG_FILE" 2>/dev/null)

  # Return empty if it's a template value
  if [[ "$value" == *"$CONFIG_TEMPLATE_MARKER"* ]]; then
    echo ""
  else
    echo "$value"
  fi
}

# Collect all configuration values
collect_config() {
  local state="$1"

  echo ""
  log_info "McApp Configuration"
  echo "──────────────────────────────────────────────────────────"

  # Get current values (empty if fresh/template)
  local current_callsign
  local current_node
  local current_lat
  local current_lon
  local current_station

  current_callsign=$(get_config_value "CALL_SIGN")
  current_node=$(get_config_value "UDP_TARGET")
  current_lat=$(get_config_value "LAT")
  current_lon=$(get_config_value "LONG")
  current_station=$(get_config_value "STAT_NAME")

  # Collect values interactively
  local callsign
  local node_address
  local latitude
  local longitude
  local station_name

  callsign=$(prompt_callsign "$current_callsign")
  node_address=$(prompt_node_address "$current_node")
  latitude=$(prompt_latitude "$current_lat")
  longitude=$(prompt_longitude "$current_lon")
  station_name=$(prompt_station_name "$current_station")

  echo ""
  echo "Configuration summary:"
  echo "  Callsign:     $callsign"
  echo "  Node address: $node_address"
  echo "  Latitude:     $latitude"
  echo "  Longitude:    $longitude"
  echo "  Station name: $station_name"
  echo ""

  read -rp "[?] Save this configuration? (Y/n): " confirm
  if [[ "${confirm,,}" == "n" ]]; then
    log_warn "Configuration cancelled"
    exit 1
  fi

  # Write config file
  write_config "$callsign" "$node_address" "$latitude" "$longitude" "$station_name"

  log_ok "Configuration saved to ${CONFIG_FILE}"
}

# Write configuration file atomically
write_config() {
  local callsign="$1"
  local node_address="$2"
  local latitude="$3"
  local longitude="$4"
  local station_name="$5"

  # Ensure config directory exists
  mkdir -p "$CONFIG_DIR"

  # Get hostname for TLS certificate
  local hostname
  hostname=$(hostname -s)

  # Generate config from template or create new
  local tmp_config
  tmp_config=$(mktemp)

  cat > "$tmp_config" << EOF
{
  "UDP_TARGET": "${node_address}",
  "UDP_PORT_send": 1799,
  "UDP_PORT_list": 1799,
  "WS_HOST": "127.0.0.1",
  "WS_PORT": 2980,
  "CALL_SIGN": "${callsign}",
  "LAT": ${latitude},
  "LONG": ${longitude},
  "STAT_NAME": "${station_name}",
  "HOSTNAME": "${hostname}",
  "PRUNE_HOURS": 168,
  "MAX_STORAGE_SIZE_MB": 50,
  "WEATHER_SERVICE": "open-meteo",
  "BLE_DEVICE_NAME": "",
  "BLE_ENABLED": false
}
EOF

  # Validate JSON before writing
  if ! jq '.' "$tmp_config" >/dev/null 2>&1; then
    log_error "Generated config is invalid JSON"
    rm -f "$tmp_config"
    return 1
  fi

  # Backup existing config if present
  if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
  fi

  # Atomic move
  mv "$tmp_config" "$CONFIG_FILE"
  chmod 640 "$CONFIG_FILE"
}

# Migrate config to add missing fields (for upgrades)
migrate_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    return 0
  fi

  log_info "Checking config for missing fields..."

  local updated=false
  local tmp_config
  tmp_config=$(mktemp)

  cp "$CONFIG_FILE" "$tmp_config"

  # Add missing fields with defaults
  local -A defaults=(
    ["PRUNE_HOURS"]=168
    ["MAX_STORAGE_SIZE_MB"]=50
    ["WEATHER_SERVICE"]="open-meteo"
    ["BLE_DEVICE_NAME"]=""
    ["BLE_ENABLED"]=false
  )

  for key in "${!defaults[@]}"; do
    local value="${defaults[$key]}"
    local current
    current=$(jq -r ".${key} // \"__MISSING__\"" "$tmp_config" 2>/dev/null)

    if [[ "$current" == "__MISSING__" ]]; then
      log_info "  Adding missing field: ${key}"

      # Handle different types
      if [[ "$value" == "true" ]] || [[ "$value" == "false" ]] || [[ "$value" =~ ^[0-9]+$ ]]; then
        jq ".${key} = ${value}" "$tmp_config" > "${tmp_config}.new" && mv "${tmp_config}.new" "$tmp_config"
      else
        jq ".${key} = \"${value}\"" "$tmp_config" > "${tmp_config}.new" && mv "${tmp_config}.new" "$tmp_config"
      fi
      updated=true
    fi
  done

  if [[ "$updated" == "true" ]]; then
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
    mv "$tmp_config" "$CONFIG_FILE"
    log_ok "Config migrated successfully"
  else
    rm -f "$tmp_config"
    log_info "Config is up to date"
  fi
}
